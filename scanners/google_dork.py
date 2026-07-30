"""
Google Dork Scanner — 使用 Google 搜索在多个平台上查找泄露的 API key
支持 Google Custom Search API 或直接抓取搜索结果
"""

import aiohttp
import asyncio
import re
from .base import BaseScanner, extract_keys


# Google Dork 查询模板
GOOGLE_DORK_QUERIES = [
    # 搜索 GitHub 上的泄露
    'site:github.com "sk-" "deepseek"',
    'site:github.com "DEEPSEEK_API_KEY"',
    'site:github.com "api.deepseek.com" "sk-"',

    # 搜索 GitLab
    'site:gitlab.com "sk-" "deepseek"',
    'site:gitlab.com "DEEPSEEK_API_KEY"',

    # 搜索 Pastebin
    'site:pastebin.com "sk-" "deepseek"',
    'site:pastebin.com "DEEPSEEK_API_KEY"',

    # 搜索 Stack Overflow
    'site:stackoverflow.com "sk-" "deepseek"',

    # 搜索 HuggingFace
    'site:huggingface.co "sk-" "deepseek"',

    # 搜索 npm
    'site:npmjs.com "deepseek" "api_key"',

    # 搜索 PyPI
    'site:pypi.org "deepseek" "api_key"',

    # 搜索其他代码托管
    'site:bitbucket.org "sk-" "deepseek"',
    'site:sourceforge.net "sk-" "deepseek"',

    # 搜索 AI 平台
    'site:replicate.com "sk-" "deepseek"',
    'site:civitai.com "sk-" "deepseek"',

    # 搜索配置文件
    'filetype:env "sk-" "deepseek"',
    'filetype:yml "sk-" "deepseek"',
    'filetype:yaml "sk-" "deepseek"',
    'filetype:json "sk-" "deepseek"',

    # 搜索中文平台
    'site:gitee.com "sk-" "deepseek"',
    'site:code.csdn.net "sk-" "deepseek"',
]


class GoogleDorkScanner(BaseScanner):
    """使用 Google 搜索查找泄露的 API key"""

    def __init__(self, api_key: str = "", cx: str = "", max_results: int = 100, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.cx = cx  # Custom Search Engine ID
        self.max_results = max_results

    @property
    def source_name(self) -> str:
        return "google_dork"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        # 只使用最重要的查询
        queries = GOOGLE_DORK_QUERIES[:5]  # 减少到 5 个查询

        async with aiohttp.ClientSession() as session:
            for q in queries:
                if self._should_stop():
                    break

                if self.api_key and self.cx:
                    # 使用 Google Custom Search API
                    results = await self._search_with_api(session, q)
                else:
                    # 使用网页抓取（有限制）
                    results = await self._search_with_scraping(session, q)

                # 扫描搜索结果中的内容
                for result in results:
                    url = result.get("url", "")
                    snippet = result.get("snippet", "")

                    # 从 snippet 中提取 key
                    for k in extract_keys(snippet, self.extra_bad):
                        self._add_result(k, url, "google", q[:50], self.source_name)

                await asyncio.sleep(3.0)  # 增加延迟避免被封禁

        return self.results

    async def _search_with_api(self, session, query: str) -> list:
        """使用 Google Custom Search API"""
        results = []
        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "key": self.api_key,
                "cx": self.cx,
                "q": query,
                "num": 10,
            }
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("items", [])
                    for item in items:
                        results.append({
                            "url": item.get("link", ""),
                            "snippet": item.get("snippet", ""),
                            "title": item.get("title", ""),
                        })
        except Exception as e:
            self.log(f"Google API error: {e}", "warning")
        return results

    async def _search_with_scraping(self, session, query: str) -> list:
        """使用网页抓取（无 API key 时的备用方案）"""
        results = []
        try:
            url = "https://www.google.com/search"
            params = {"q": query, "num": 10}
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    # 简单提取搜索结果
                    urls = re.findall(r'<a href="/url\?q=(https?://[^&"]+)', html)
                    for url in urls:
                        results.append({"url": url, "snippet": "", "title": ""})
        except Exception as e:
            self.log(f"Google scraping error: {e}", "warning")
        return results[:10]
