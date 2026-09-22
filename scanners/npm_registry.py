"""
npm Registry Scanner — Search npm for packages related to DeepSeek.
Scans package metadata (readme + description) for free before downloading
tarballs, and prioritizes non-SDK packages (tools/proxies) where leaks
are more common. Many leaks live in README code blocks.
"""

import asyncio
import io
import tarfile
import time
import urllib.parse

import aiohttp

from .base import BaseScanner, extract_keys

# Official/well-known SDK package names — lower priority, fewer real leaks.
_KNOWN_SDK_PREFIXES = ("@deepseek-ai/",)


class NpmScanner(BaseScanner):
    REGISTRY = "https://registry.npmjs.org"

    def __init__(self, max_packages: int = 100, deadline_s: float = 45.0, **kwargs):
        super().__init__(**kwargs)
        self.max_packages = max_packages
        # 内部 deadline:tarball 下载×多包叠加可达数分钟,外部 join(60s)弃线程后
        # 僵尸还在跑(每轮 +1)——45s 内主动退出,对齐 gitlab 的 deadline 模式。
        self._deadline = deadline_s
        self._deadline_end = 0.0

    @property
    def source_name(self) -> str:
        return "npm"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"
        self._deadline_end = time.monotonic() + self._deadline

        sem = asyncio.Semaphore(self.concurrency)

        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with asyncio.timeout(self._deadline):
                await self._search_body(session, sem, query)
        return self.results

    async def _search_body(self, session, sem, query: str):
        # 多轮搜索：原 query + 针对泄露的限定词搜索
        # MCP server/CLI/agent 类包是泄露高发区（个人项目，.env 常被发布）
        queries = [query]
        if query and len(query) < 20:
            queries.append(f"mcp {query}")
            queries.append(f"{query} cli")
            queries.append(f"{query} agent")

        all_packages = []
        seen_names = set()
        for q in queries:
            packages = await self._search_packages(session, q)
            for p in packages:
                name = p.get("package", {}).get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name)
                    all_packages.append(p)
            if len(all_packages) >= self.max_packages * 2:
                break

        if not all_packages:
            return

        # 优先级排序：
        # 1. 非 SDK 包（工具/proxy/agent 类）
        # 2. 低下载量包（个人项目更可能泄露）
        def _sort_key(o):
            is_sdk = 1 if self._is_sdk(o) else 0
            # search API 不返回下载量，用包名长度和版本号估算
            name = o.get("package", {}).get("name", "")
            # 短包名 / scoped 包名往往更个人化
            name_score = 0 if len(name) < 20 else 1
            return (is_sdk, name_score)

        all_packages.sort(key=_sort_key)

        self.log(f"npm: {len(all_packages)} packages (queries={len(queries)})")
        tasks = [self._scan_package(session, sem, p)
                 for p in all_packages[:self.max_packages]]
        await asyncio.gather(*tasks)

    @staticmethod
    def _is_sdk(pkg_obj: dict) -> bool:
        name = pkg_obj.get("package", {}).get("name", "")
        return any(name.startswith(p) for p in _KNOWN_SDK_PREFIXES)

    async def _search_packages(self, session, q: str, size: int = 250, offset: int = 0) -> list:
        """npm registry search API。支持分页（from 参数）和限定词搜索。"""
        params = urllib.parse.urlencode({
            "text": q,
            "size": size,
            "from": offset,
        })
        url = f"{self.REGISTRY}/-/v1/search?{params}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30, connect=10), proxy=self._proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("objects", [])
        except Exception:
            pass
        return []

    async def _scan_package(self, session, sem, pkg_obj: dict):
        pkg = pkg_obj.get("package", {})
        name = pkg.get("name", "")
        version = pkg.get("version", "")
        npm_url = pkg.get("links", {}).get("npm", "")
        if not name or not version:
            return

        # ── Free metadata scan: readme + description from search results ──
        readme = pkg.get("readme") or ""
        description = pkg.get("description") or ""
        for k in extract_keys(readme, self.extra_bad):
            self._add_result(k, npm_url, name, "readme", self.source_name)
        for k in extract_keys(description, self.extra_bad):
            self._add_result(k, npm_url, name, "description", self.source_name)

        async with sem:
            try:
                # Get package metadata for tarball URL
                pkg_url = f"{self.REGISTRY}/{name}"
                async with session.get(pkg_url, timeout=aiohttp.ClientTimeout(total=20, connect=10), proxy=self._proxy) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                tarball_url = data.get("versions", {}).get(version, {}).get("dist", {}).get("tarball", "")
                if not tarball_url:
                    return

                async with session.get(tarball_url, timeout=aiohttp.ClientTimeout(total=45, connect=15), proxy=self._proxy) as resp:
                    if resp.status != 200:
                        return
                    # 跳过大包（>500KB）——先看 Content-Length 头,不用完整下载
                    # 后才丢(几百 MB 的包会耗光 45s deadline)。
                    cl = resp.headers.get("Content-Length")
                    if cl and int(cl) > 500_000:
                        return
                    tarball_data = await resp.read()

                if len(tarball_data) > 500_000:
                    return

                for k in self._scan_tarball(tarball_data):
                    self._add_result(k, npm_url, name, f"v{version}", self.source_name)

            except Exception as e:
                self.log(f"npm scan {name} failed: {e}", "warning")

    def _scan_tarball(self, data: bytes) -> list[str]:
        keys = []
        _extra = self.extra_bad or []
        # .env 文件是泄露最高发的位置（个人项目常忘记 .npmignore）
        # 优先扫描 .env，然后 config，最后其他文件
        _HIGH_PRIORITY = {".env", ".env.local", ".env.production", ".env.example",
                          ".env.sample", ".npmrc", ".pypirc", ".dockercfg"}
        _CONFIG_EXTS = (".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".conf")
        _CODE_EXTS = (".js", ".ts", ".py", ".sh", ".md", ".txt", ".properties")

        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                members = tar.getmembers()

                # 按优先级分组：.env → config → code → 其他
                env_files, config_files, code_files = [], [], []
                for member in members:
                    fname = member.name.split("/")[-1].lower()
                    if fname in _HIGH_PRIORITY:
                        env_files.append(member)
                    elif fname.endswith(_CONFIG_EXTS):
                        config_files.append(member)
                    elif fname.endswith(_CODE_EXTS):
                        code_files.append(member)

                # 按优先级顺序扫描
                for member in env_files + config_files + code_files:
                    if not member.isfile() or member.size > 500_000:
                        continue
                    try:
                        f = tar.extractfile(member)
                        if f:
                            content = f.read().decode("utf-8", errors="replace")
                            file_keys = extract_keys(content, self.extra_bad)
                            if file_keys:
                                keys.extend(file_keys)
                    except Exception:
                        pass
        except (tarfile.ReadError, EOFError):
            pass

        return keys
