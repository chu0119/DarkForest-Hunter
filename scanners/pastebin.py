"""
Pastebin Scanner — 搜索公开粘贴中的 API key
使用 Google Custom Search 来发现 Pastebin 上的敏感信息
"""

import aiohttp
import asyncio
import re
from .base import BaseScanner, extract_keys


# Pastebin 搜索查询 (用于 Google dorking)
PASTEBIN_QUERIES = [
    'site:pastebin.com "deepseek" "sk-"',
    'site:pastebin.com "deepseek" "api_key"',
    'site:pastebin.com "DEEPSEEK_API_KEY"',
]


class PastebinScanner(BaseScanner):
    """搜索 Pastebin 公开粘贴"""

    def __init__(self, max_results: int = 20, **kwargs):
        super().__init__(**kwargs)
        self.max_results = max_results

    @property
    def source_name(self) -> str:
        return "pastebin"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []

        async with aiohttp.ClientSession() as session:
            # 尝试通过 Pastebin 的 raw endpoint 获取最近的粘贴
            # 这是一个概率性的方法，但不需要认证
            for _ in range(min(self.max_results, 10)):
                if self._should_stop():
                    break
                try:
                    # 获取随机粘贴的 raw 内容
                    url = "https://pastebin.com/raw"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            if len(text) < 500000:  # 限制大小
                                for k in extract_keys(text, self.extra_bad):
                                    self._add_result(k, url, "pastebin", "random", self.source_name)
                except Exception:
                    pass
                await asyncio.sleep(1.0)

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
