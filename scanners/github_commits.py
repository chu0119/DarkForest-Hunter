"""
GitHub Commits Scanner — Search commit history and diffs for keys that were
committed and later "removed". The key still exists in git history even if
deleted from current files. This catches keys that regular code search misses.
"""

import asyncio
import re

import aiohttp

from .base import BaseScanner, extract_keys


class CommitsScanner(BaseScanner):
    BASE = "https://api.github.com"
    KEY_PATTERN = re.compile(
        r"(?:sk-(?:ant-[a-zA-Z0-9_-]{24,}|kimi-[a-zA-Z0-9_-]{24,}|"
        r"sp-[a-zA-Z0-9_-]{24,}|proj-[a-zA-Z0-9_-]{24,}|or-v1-[a-zA-Z0-9_-]{24,}|"
        r"[a-zA-Z0-9]{32,64}))"
    )

    def __init__(self, token: str = "", max_repos: int = 100, since_hours: int = 0,
                 deadline_s: float = 50.0, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.max_repos = max_repos
        self.since_hours = since_hours
        self._deadline = deadline_s  # 内部总时限：watch 模式 60s join 的最终保险前先自行取消
        # GitHub API 限速：Code Search 10 req/min 是搜索端点；核心 API 常规 60 req/min,
        # 但 watch 模式多源并发时保守对齐 ~30 req/min (2s/请求),避免 429 触发次级限流。
        self._rate_limiter = asyncio.Semaphore(1)
        self._min_interval = 2.0  # 每请求最小间隔
        self._last_req = 0.0
        self._rate_lock = asyncio.Lock()
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DeepSeekKeyHunter/5.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    @property
    def source_name(self) -> str:
        return "github_commits"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        sem = asyncio.Semaphore(self.concurrency)

        try:
            async with asyncio.timeout(self._deadline):
                # Step 1: Find repos mentioning the rotation term (or deepseek defaults)
                repos = await self._search_repos(query)
                if not repos:
                    return self.results

                async with aiohttp.ClientSession(headers=self._headers) as session:
                    for repo in repos[:self.max_repos]:
                        if self._should_stop():
                            break
                        await self._scan_repo_commits(session, sem, repo)
        except TimeoutError:
            # 内部总时限到点：保留已累积的 self.results（watch _scan_external 60s join 仅作最终保险）
            pass

        return self.results

    def _repo_queries(self, query: str | None) -> list[str]:
        """轮换词 → repo 搜索词列表。

        query 是 watch 的 _ROTATION 轮换词（deepseek/sk-/平台词），可能是
        code-search 风格串（含空格/限定符）——用于 repo 搜索时截断到第一个
        空格前的词，跳过含 ':' 的限定符串；保底追加固定 deepseek 查询。
        """
        queries = []
        head = (query or "").strip()
        word = head.split()[0] if head else ""
        if word and ":" not in word:
            queries.append(word)
        queries += [
            "deepseek in:readme",
            "deepseek-ai",
            "deepseek language:python",
            "deepseek-api",
        ]
        return queries

    async def _search_repos(self, query: str | None = None) -> list:
        """Find repos mentioning deepseek that may have commit history leaks."""
        repos = []
        queries = self._repo_queries(query)
        for q in queries:
            url = f"{self.BASE}/search/repositories?q={q.replace(' ', '+')}&sort=updated&per_page=30"
            try:
                async with aiohttp.ClientSession(headers=self._headers) as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                     proxy=self._proxy) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for item in data.get("items", []):
                                repos.append(item.get("full_name", ""))
                        elif resp.status == 403:
                            await asyncio.sleep(60)
            except Exception:
                pass
            await asyncio.sleep(1)
        return list(dict.fromkeys(repos))  # dedup preserving order

    async def _scan_repo_commits(self, session, sem, repo: str):
        """Scan recent commits for diffs containing sk- keys."""
        url = f"{self.BASE}/repos/{repo}/commits?per_page=30"
        if self.since_hours > 0:
            import datetime as _dt
            since = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=self.since_hours)
            # ISO 格式用 Z 后缀——isoformat() 的 "+00:00" 里 + 在 query string
            # 会被服务端按 form 规则解码成空格,时间窗失效(返回全量)或 422
            url += f"&since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                   proxy=self._proxy) as resp:
                if resp.status != 200:
                    return
                commits = await resp.json()
                if not isinstance(commits, list):
                    return
        except Exception:
            return

        tasks = []
        for commit in commits:
            sha = commit.get("sha", "")
            if not sha:
                continue
            tasks.append(self._scan_commit_diff(session, sem, repo, sha))

        if tasks:
            await asyncio.gather(*tasks)

    async def _throttle(self):
        """每请求前限速：保持 >=2s 间隔,避免多仓库并发 diff 撞 429。"""
        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_req)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_req = asyncio.get_event_loop().time()

    async def _scan_commit_diff(self, session, sem, repo: str, sha: str):
        """Fetch a single commit diff and extract keys from added/removed lines."""
        url = f"{self.BASE}/repos/{repo}/commits/{sha}"
        await self._throttle()
        async with sem:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                       proxy=self._proxy) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()

                    # Scan commit message
                    msg = data.get("commit", {}).get("message", "")
                    for k in extract_keys(msg, self.extra_bad):
                        self._add_result(k, f"https://github.com/{repo}/commit/{sha}",
                                         repo, f"commit:{sha[:7]}", self.source_name)

                    # Scan patch diffs
                    files = data.get("files", [])
                    for f in files:
                        patch = f.get("patch", "")
                        if not patch:
                            continue
                        # Only look at added lines (lines starting with +)
                        added_lines = "\n".join(
                            line[1:] for line in patch.split("\n")
                            if line.startswith("+") and not line.startswith("+++")
                        )
                        for k in extract_keys(added_lines, self.extra_bad):
                            self._add_result(k, f.get("blob_url", f"https://github.com/{repo}/commit/{sha}"),
                                             repo, f.get("filename", ""), self.source_name)
            except Exception:
                pass
