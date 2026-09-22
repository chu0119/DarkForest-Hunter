"""
Docker Hub Scanner — Search Docker Hub for images with "deepseek" references.
Scans image description/full_description for free (no auth needed), then
inspects image config blobs via bearer-token auth for accidentally embedded
API keys in Dockerfile history and env vars.
"""

import asyncio
import json
import time
import urllib.parse

import aiohttp

from .base import BaseScanner, extract_keys


class DockerHubScanner(BaseScanner):
    HUB_API = "https://hub.docker.com/v2"
    REGISTRY = "https://registry-1.docker.io"
    AUTH = "https://auth.docker.io/token"

    def __init__(self, token: str = "", max_images: int = 20,
                 deadline_s: float = 30.0, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.max_images = max_images
        self._deadline = deadline_s
        self._deadline_end = 0.0
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0"}
        self._registry_headers = {
            "User-Agent": "DeepSeekKeyHunter/5.0",
        }
        # 注意：不再往 session headers 塞 "JWT {token}"——那是旧版 Docker 认证格式
        # （已废弃），且会污染所有请求（auth.docker.io 收到 JWT 头返回 500）。
        # registry 拉取走独立的 bearer-token 流程（_get_bearer_token）；
        # Hub API 带 token 用 auth= 参数（Hub API v2 支持 basic auth）。

    @property
    def source_name(self) -> str:
        return "docker_hub"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"
        # 内部 deadline:多页镜像×tags×blob 全跑完可达 5-10 分钟,外部 join(60s)
        # 弃线程后僵尸还在跑——deadline 30s 保证 search() 内主动退出。
        self._deadline_end = time.monotonic() + self._deadline

        sem = asyncio.Semaphore(self.concurrency)

        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(headers=self._headers, connector=connector) as session:
            # 强制超时:deadline 到点主动取消所有在途请求(TimeoutError)
            async with asyncio.timeout(self._deadline):
                images = await self._search_images(session, query)
                if not images:
                    return self.results

                targets = []
                for img_summary in images[:self.max_images]:
                    # Docker Hub 搜索 API 字段：repo_name（含 namespace/name，如 "kimi8122/kimi"）
                    # 而非 name/namespace 分开字段——用错字段导致全空（0 产出根因）。
                    repo_name = img_summary.get("repo_name", "")
                    if not repo_name:
                        n = img_summary.get("name", "")
                        ns = img_summary.get("namespace", "")
                        repo_name = f"{ns}/{n}" if ns and n else n
                    if repo_name:
                        targets.append(repo_name)

                tasks = [self._scan_tags(session, sem, name) for name in targets]
                await asyncio.gather(*tasks)

        return self.results

    async def _search_images(self, session, q: str, pages: int = 5) -> list:
        all_images = []
        for page in range(1, pages + 1):
            if self._should_stop():
                break
            try:
                params = urllib.parse.urlencode({
                    "query": q,
                    "page": page,
                    "page_size": 25,
                })
                url = f"{self.HUB_API}/search/repositories/?{params}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20, connect=10),
                                       proxy=self._proxy,
                                       auth=aiohttp.BasicAuth(self.token, "") if self.token else None) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        all_images.extend(results)

                        # ── Free metadata scan: short_description ──
                        # No auth needed, higher rate limits than the registry.
                        # 搜索 API 只返回 short_description（full_description 需单独拉详情）
                        for img in results:
                            repo_name = img.get("repo_name", "")
                            if not repo_name:
                                continue
                            hub_url = f"https://hub.docker.com/r/{repo_name}"
                            desc = img.get("short_description", "") or ""
                            for k in extract_keys(desc, self.extra_bad):
                                self._add_result(k, hub_url, repo_name,
                                                 "short_description", self.source_name)

                        if len(results) < 25:
                            break
                    elif resp.status == 429:
                        self.log(f"Docker Hub search rate-limited (429) on page {page}", "warning")
                        await asyncio.sleep(10)
                        continue
                    else:
                        self.log(f"Docker Hub search returned HTTP {resp.status}", "warning")
                        break
            except Exception as e:
                self.log(f"Docker Hub search error: {e}", "warning")
                break
            await asyncio.sleep(0.5)
        return all_images

    async def _scan_tags(self, session, sem, repo_name: str):
        """List tags and scan the most recent ones."""
        try:
            # List tags (first 2 pages)
            tags = []
            for page in range(1, 3):
                url = f"{self.HUB_API}/repositories/{repo_name}/tags/?page={page}&page_size=25"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20, connect=10),
                                       proxy=self._proxy,
                                       auth=aiohttp.BasicAuth(self.token, "") if self.token else None) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        page_tags = data.get("results", [])
                        tags.extend(page_tags)
                        if len(page_tags) < 25:
                            break
                    elif resp.status == 429:
                        self.log(f"Docker Hub tags rate-limited for {repo_name}", "warning")
                        await asyncio.sleep(5)
                    else:
                        self.log(f"Docker Hub tags returned HTTP {resp.status} for {repo_name}", "warning")

            layer_tasks = []
            for tag_info in tags[:10]:
                tag_name = tag_info.get("name", "latest")
                images = tag_info.get("images", [])
                for img in images[:3]:
                    layer_tasks.append(self._scan_image_layers(session, sem, repo_name, tag_name, img))
            if layer_tasks:
                await asyncio.gather(*layer_tasks)
        except Exception as e:
            self.log(f"Docker Hub scan tags error for {repo_name}: {e}", "warning")

    async def _get_bearer_token(self, session, repo_name: str) -> str:
        """Obtain a Docker Registry v2 bearer token for pulling the image.

        Docker Registry v2 requires the bearer-token flow:
          1. Unauthenticated request → 401 with WWW-Authenticate header
             containing realm/service/scope.
          2. Request token from the realm URL with those params.
          3. Use the token for subsequent authenticated requests.
        We skip straight to the token endpoint using the repository scope.
        """
        registry_repo = f"library/{repo_name}" if "/" not in repo_name else repo_name
        scope = f"repository:{registry_repo}:pull"
        params = urllib.parse.urlencode({
            "service": "registry.docker.io",
            "scope": scope,
        })
        url = f"{self.AUTH}?{params}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15), proxy=self._proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("token") or data.get("access_token") or ""
                    if token:
                        return token
                    self.log(f"Docker auth token response missing token for {repo_name}", "warning")
                else:
                    self.log(f"Docker auth token returned HTTP {resp.status} for {repo_name}", "warning")
        except Exception as e:
            self.log(f"Docker auth token fetch failed for {repo_name}: {e}", "warning")
        return ""

    async def _scan_image_layers(self, session, sem, repo_name: str, tag: str, img_info: dict):
        """Inspect image config blob (Dockerfile history, env vars) via bearer auth."""
        digest = img_info.get("digest", "")
        if not digest:
            return

        registry_repo = f"library/{repo_name}" if "/" not in repo_name else repo_name
        url = f"{self.REGISTRY}/v2/{registry_repo}/manifests/{tag}"
        headers = dict(self._registry_headers)
        headers["Accept"] = "application/vnd.docker.distribution.manifest.v2+json"

        async with sem:
            try:
                # Get bearer token for registry access (fixes the silent 401s).
                token = await self._get_bearer_token(session, repo_name)
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15), headers=headers, proxy=self._proxy) as resp:
                    if resp.status == 401:
                        self.log(f"Docker registry 401 for {repo_name}:{tag} — auth failed", "warning")
                        return
                    if resp.status != 200:
                        return
                    try:
                        manifest = await resp.json()
                    except ValueError:
                        return
                    # OCI index（application/vnd.oci.image.index）→ 取第一个 platform 的
                    # manifest digest 再拉；Docker manifest v2 直接有 config.digest。
                    config = manifest.get("config", {})
                    config_digest = config.get("digest", "")
                    if not config_digest:
                        manifests = manifest.get("manifests") or []
                        if manifests:
                            sub = manifests[0].get("digest", "")
                            if sub:
                                sub_url = (f"{self.REGISTRY}/v2/{registry_repo}/"
                                           f"manifests/{sub}")
                                sub_headers = dict(self._registry_headers)
                                if token:
                                    sub_headers["Authorization"] = f"Bearer {token}"
                                async with session.get(
                                        sub_url, timeout=aiohttp.ClientTimeout(total=15),
                                        headers=sub_headers,
                                        proxy=self._proxy) as s_resp:
                                    if s_resp.status != 200:
                                        return
                                    try:
                                        manifest = await s_resp.json()
                                    except ValueError:
                                        return
                                    config = manifest.get("config", {})
                                    config_digest = config.get("digest", "")
                        if not config_digest:
                            return

                    config_url = f"{self.REGISTRY}/v2/{registry_repo}/blobs/{config_digest}"
                    config_headers = dict(self._registry_headers)
                    if token:
                        config_headers["Authorization"] = f"Bearer {token}"
                    async with session.get(config_url, timeout=aiohttp.ClientTimeout(total=15),
                                            headers=config_headers, proxy=self._proxy) as c_resp:
                        if c_resp.status != 200:
                            return
                        raw = await c_resp.read()
                        try:
                            config_data = json.loads(raw.decode("utf-8", errors="replace"))
                        except (ValueError, UnicodeDecodeError):
                            # 二进制 layer blob（octet-stream）非 JSON——跳过
                            return
                        # Scan history for env vars and commands
                        history = config_data.get("history", [])
                        for entry in history:
                            created_by = entry.get("created_by", "") or ""
                            for k in extract_keys(created_by, self.extra_bad):
                                self._add_result(k,
                                                 f"https://hub.docker.com/r/{repo_name}",
                                                 repo_name, f"layer:{tag}", self.source_name)

                        # Scan config for exposed env vars
                        config_env = config_data.get("config", {})
                        env_list = config_env.get("Env", []) or []
                        for env_var in env_list:
                            for k in extract_keys(env_var, self.extra_bad):
                                self._add_result(k,
                                                 f"https://hub.docker.com/r/{repo_name}",
                                                 repo_name, f"env:{tag}", self.source_name)
            except Exception as e:
                self.log(f"Docker registry scan error for {repo_name}:{tag}: {e}", "warning")
