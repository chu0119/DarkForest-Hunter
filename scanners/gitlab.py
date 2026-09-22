"""
GitLab Scanner — Search GitLab.com public projects for leaked DeepSeek keys.
Supports both gitlab.com and self-hosted instances.
"""

import asyncio
import time
import urllib.parse

import aiohttp

from .base import TARGET_FILE_EXTS, TARGET_FILENAMES, BaseScanner, extract_keys


class GitLabScanner(BaseScanner):
    def __init__(self, token: str = "", base_url: str = "https://gitlab.com",
                 max_projects: int = 200, max_files_per_project: int = 100,
                 deadline_s: float = 28.0, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.max_projects = max_projects
        self.max_files_per_project = max_files_per_project
        self._deadline = deadline_s
        self._deadline_end = 0.0
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0"}
        if token:
            self._headers["PRIVATE-TOKEN"] = token

    @property
    def source_name(self) -> str:
        return "gitlab"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"
        sem = asyncio.Semaphore(self.concurrency)
        self._deadline_end = time.monotonic() + self._deadline

        try:
            connector = aiohttp.TCPConnector(limit=10)
            async with aiohttp.ClientSession(headers=self._headers, connector=connector) as session:
                async with asyncio.timeout(self._deadline):
                    # 多词搜索：原 query + 泄露特征词（覆盖更多项目）
                    queries = [query]
                    if self.token:
                        queries.extend(["api key", "sk-", "deepseek api"])

                    all_projects = []
                    seen_ids = set()
                    for q in queries:
                        projects = await self._search_projects(session, q, pages=3)
                        for p in projects:
                            pid = p.get("id")
                            if pid and pid not in seen_ids:
                                seen_ids.add(pid)
                                all_projects.append(p)
                        if len(all_projects) >= self.max_projects:
                            break

                    if not all_projects:
                        return self.results
                    self.log(f"GitLab: {len(all_projects)} projects (queries={len(queries)})")

                    for proj in all_projects[:self.max_projects]:
                        if self._should_stop():
                            break
                        if self.token:
                            # 有 token：用项目内代码搜索（精准定位含 sk-* 的文件）
                            await self._scan_project_code_search(session, sem, proj)
                        else:
                            # 无 token：回退到文件树扫描
                            await self._scan_project(session, sem, proj)
        except TimeoutError:
            pass

        return self.results

    async def _search_projects(self, session, q: str, pages: int = 10) -> list:
        all_projects = []
        api = f"{self.base_url}/api/v4"
        retries_429 = 0

        for page in range(1, pages + 1):
            if self._should_stop():
                break
            try:
                params = urllib.parse.urlencode({
                    "search": q,
                    "visibility": "public",
                    "per_page": 100,
                    "page": page,
                })
                url = f"{api}/projects?{params}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), proxy=self._proxy) as resp:
                    if resp.status == 200:
                        projects = await resp.json()
                        all_projects.extend(projects)
                        if len(projects) < 100:
                            break
                    elif resp.status == 429:
                        if retries_429 >= 1:
                            break  # 最多重试 1 次
                        retries_429 += 1
                        wait = min(10.0, max(0.0, self._deadline_end - time.monotonic()))
                        await asyncio.sleep(wait)
                    else:
                        break
            except Exception:
                break
            await asyncio.sleep(0.3)
        return all_projects

    async def _scan_project(self, session, sem, proj: dict):
        proj_id = proj.get("id", 0)
        proj_name = proj.get("path_with_namespace", "")
        web_url = proj.get("web_url", "")
        api = f"{self.base_url}/api/v4/projects/{proj_id}"

        # 顶层 tree（不 recursive——大项目全树可达上千条，是 90s 超时根因；
        # 顶层 + 高频入口文件直拉已覆盖多数泄露场景）
        target_files = set()
        try:
            tree_url = f"{api}/repository/tree?per_page=100&recursive=false"
            async with session.get(tree_url, timeout=aiohttp.ClientTimeout(total=10), proxy=self._proxy) as resp:
                if resp.status == 200:
                    tree = await resp.json()
                    for node in tree:
                        path = node.get("path", "")
                        name = node.get("name", "")
                        fname_lower = name.lower()
                        if any(name.endswith(ext) for ext in TARGET_FILE_EXTS):
                            target_files.add(path)
                        elif fname_lower in TARGET_FILENAMES:
                            target_files.add(path)
        except Exception:
            pass

        # 高频入口文件直拉（README/.env/config 等，不依赖 tree 也能命中）
        for entry in ("README.md", ".env", "config.json", "config.py",
                      "application.yml", "settings.py", "requirements.txt"):
            target_files.add(entry)

        for fpath in list(target_files)[:self.max_files_per_project]:
            async with sem:
                try:
                    encoded = urllib.parse.quote(fpath, safe="")
                    raw_url = f"{api}/repository/files/{encoded}/raw?ref=HEAD"
                    async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=12), proxy=self._proxy) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            for k in extract_keys(text, self.extra_bad):
                                self._add_result(k, f"{web_url}/-/blob/HEAD/{fpath}",
                                                 proj_name, fpath, self.source_name)
                except Exception:
                    pass

    async def _scan_project_code_search(self, session, sem, proj: dict):
        """有 token 时用项目内代码搜索（GET /projects/:id/search?scope=blobs），
        精准定位含 sk-* 的文件，无需遍历文件树。

        关键优化：blob 搜索结果的 `data` 字段**已含匹配内容**——
        直接从中提取 key，不再下载整个文件（快 10x 且避免 404/超限）。
        """
        proj_id = proj.get("id", 0)
        proj_name = proj.get("path_with_namespace", "")
        web_url = proj.get("web_url", "")
        api = f"{self.base_url}/api/v4/projects/{proj_id}"

        # 项目内代码搜索：直接找含 key 特征的文件（多词覆盖不同泄露格式）
        search_terms = ["sk-", "api_key", "api-key", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"]
        before = len(self.results)  # 本项目的起始快照（兜底下载门控用）
        found_files = set()
        for term in search_terms:
            try:
                params = urllib.parse.urlencode({
                    "scope": "blobs",
                    "search": term,
                    "per_page": 20,
                })
                url = f"{api}/search?{params}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                       proxy=self._proxy) as resp:
                    if resp.status == 200:
                        results = await resp.json()
                        for item in results:
                            fpath = item.get("path", "")
                            # blob 搜索结果的 data 字段含匹配内容——直接提取 key
                            data = item.get("data", "")
                            if data:
                                for k in extract_keys(data, self.extra_bad):
                                    self._add_result(
                                        k, f"{web_url}/-/blob/HEAD/{fpath}",
                                        proj_name, fpath, self.source_name)
                            if fpath:
                                found_files.add(fpath)
            except Exception:
                pass

        # 兜底：data 字段没提取到（如 key 在文件其他位置），下载命中文件再提一次。
        # 用**本函数起始快照**做门控(旧逻辑用累计 self.results——只要先前任一
        # 项目产出过 key,之后所有项目的兜底下载永不执行,漏掉 B 项目的 key)。
        if len(self.results) == before and found_files:
            for fpath in list(found_files)[:self.max_files_per_project]:
                async with sem:
                    try:
                        encoded = urllib.parse.quote(fpath, safe="")
                        raw_url = f"{api}/repository/files/{encoded}/raw?ref=HEAD"
                        async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=12),
                                               proxy=self._proxy) as resp:
                            if resp.status == 200:
                                text = await resp.text()
                                for k in extract_keys(text, self.extra_bad):
                                    self._add_result(k, f"{web_url}/-/blob/HEAD/{fpath}",
                                                     proj_name, fpath, self.source_name)
                    except Exception:
                        pass
