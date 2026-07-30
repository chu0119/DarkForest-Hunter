"""
GitHub Raw Scanner — 宽泛搜索 sk- 前缀的 API key
不限于 deepseek 关键词，搜索所有包含 sk- 的文件
用于发现非 DeepSeek 但可能有余额的 key
"""

import aiohttp
import asyncio
import subprocess
from .base import BaseScanner, extract_keys


def _auto_token() -> str:
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=5,
                           encoding="utf-8", errors="replace")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# 搜索查询: 宽泛的 sk- 搜索
RAW_SEARCH_QUERIES = [
    # 直接搜 sk- + 常见变量名
    '"sk-" "api_key" filename:.env',
    '"sk-" "apiKey" filename:.env',
    '"sk-" "secret_key" filename:.env',
    '"sk-" "token" filename:.env',
    '"sk-" "bearer" filename:.env',

    # 配置文件中的 sk-
    '"sk-" "api_key" filename:config',
    '"sk-" "api_key" filename:settings',
    '"sk-" filename:credentials',
    '"sk-" filename:secrets',

    # 多AI平台
    '"sk-" "deepseek" OR "moonshot" OR "qwen"',
    '"sk-" "zhipu" OR "kimi" OR "baichuan"',

    # 环境变量赋值
    '"sk-" "OPENAI_API_KEY" filename:.env',
    '"sk-" "DEEPSEEK_API_KEY" filename:.env',
    '"sk-" "API_KEY" filename:.env',

    # Docker/CI
    '"sk-" filename:dockerfile',
    '"sk-" path:.github/workflows',

    # 中文关键词
    '"sk-" "密钥" OR "接口密钥" OR "API密钥"',
]


class GitHubRawScanner(BaseScanner):
    """宽泛搜索 GitHub 上包含 sk- 的文件"""
    BASE = "https://api.github.com"

    def __init__(self, token: str = "", max_pages: int = 3,
                 queries: list = None, **kwargs):
        super().__init__(**kwargs)
        self.token = token or _auto_token()
        self.max_pages = max_pages
        self._queries = queries or RAW_SEARCH_QUERIES
        self._headers = {
            "Accept": "application/vnd.github.text-match+json",
            "User-Agent": "DeepSeekKeyHunter/5.0",
        }
        if self.token:
            self._headers["Authorization"] = f"Bearer {self.token}"

    @property
    def source_name(self) -> str:
        return "github_raw"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        sem = asyncio.Semaphore(self.concurrency)
        # 只使用前 5 个最重要的查询
        queries = self._queries[:5]

        async with aiohttp.ClientSession(headers=self._headers) as session:
            for qi, q in enumerate(queries):
                if self._should_stop():
                    break

                for page in range(1, 2):  # 只搜索第 1 页
                    items = await self._search_page(session, q, page)
                    if not items:
                        break

                    # 从 text_matches 提取 key
                    for item in items:
                        repo = item.get("repository", {}).get("full_name", "")
                        path = item.get("path", "")
                        html_url = item.get("html_url", "")
                        if not repo or not path:
                            continue

                        text_matches = item.get("text_matches", [])
                        for match in text_matches:
                            fragment = match.get("fragment", "")
                            for k in extract_keys(fragment, self.extra_bad):
                                self._add_result(k, html_url, repo, path, self.source_name)

                    if len(items) < 30:  # GitHub search returns fewer when no more
                        break

                    await asyncio.sleep(6.5)  # 增加延迟避免限流

        # 去重
        seen = set()
        unique = []
        for r in self.results:
            key = f"{r['key']}:{r['url']}"
            if key not in seen:
                seen.add(key)
                unique.append(r)
        self.results = unique

        return self.results

    async def _search_page(self, session, query, page):
        url = f"{self.BASE}/search/code"
        params = {"q": query, "per_page": 30, "page": page}
        for attempt in range(3):
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("items", [])
                    elif resp.status in (403, 429):
                        wait = 10 * (attempt + 1)
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after:
                                wait = max(wait, int(retry_after))
                        await asyncio.sleep(wait)
                        continue
                    return []
            except (asyncio.TimeoutError, aiohttp.ClientError):
                await asyncio.sleep(2)
            except Exception:
                return []
        return []
