"""Paste 站点扫描器 — 扫描 rentry.co / controlc.com / pastebin 等贴码站点的 DeepSeek key 泄露。

贴码站点是开发者贴代码片段/配置文件泄露 API key 的重灾区。
这些站点通常有简单 API 或可爬取的最新帖子列表。
"""

from __future__ import annotations

import asyncio
import re

import aiohttp

from .base import BaseScanner, extract_keys

# 贴码站点配置：URL 模板 + 提取 PASTE ID 的正则
PASTE_SOURCES = {
    "rentry": {
        "list_url": "https://rentry.co/api/new",
        "item_url": "https://rentry.co/{id}/raw",
        # rentry 没有公开列表 API，但可以尝试常见 ID 模式
    },
    "controlc": {
        "list_url": "https://controlc.com/api/posts/recent",  # 假设的 API
        "item_url": "https://controlc.com/{id}.txt",
    },
    "pastebin": {
        # pastebin 需要 API key 才能用 scraping API
        "trending_url": "https://pastebin.com/api/api_post.php",
    },
}

# 更实际的方案：通过搜索引擎间接扫描贴码站点
# 用 site:rentry.co "sk-" 这类查询在 GitHub/Code Search 之外获取
SITE_DORKS = [
    'site:rentry.co "sk-" deepseek',
    'site:controlc.com "sk-" deepseek',
    'site:rentry.co "DEEPSEEK_API_KEY"',
]


class PasteSiteScanner(BaseScanner):
    """扫描贴码站点（rentry.co, controlc.com 等）的 DeepSeek key 泄露。

    实现方式：直接爬取站点最新帖子列表 + 内容提取。
    """

    def __init__(self, max_pages: int = 10, **kwargs):
        super().__init__(**kwargs)
        self.max_pages = max_pages

    @property
    def source_name(self) -> str:
        return "paste_sites"

    async def search(self, query: str | None = None) -> list[dict]:
        """扫描多个贴码站点。"""
        self.results = []
        connector = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 内部 deadline 50s:对齐外部 join(60s),防僵尸线程
            async with asyncio.timeout(50):
                # 并发扫描多个站点
                tasks = [
                    self._scan_rentry(session),
                    self._scan_controlc(session),
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
        return self.results

    async def _scan_rentry(self, session: aiohttp.ClientSession):
        """扫描 rentry.co 的公开页面。

        rentry.co 没有公开列表 API，但可以通过遍历常见路径或
        使用 rentry.co 的 sitemap 来发现新页面。
        这里用一种保守的方法：尝试获取一些已知的公开页面。
        """
        # rentry.co 的页面是 /{id} 形式，id 通常是 6-8 位字母数字
        # 由于没有公开 API，我们尝试通过 Google/Bing 间接搜索
        # 这里简化实现：直接搜索 rentry 页面上的常见模式
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        # 尝试获取 rentry 的 sitemap
        try:
            async with session.get(
                "https://rentry.co/sitemap.xml",
                timeout=aiohttp.ClientTimeout(total=15),
                headers=headers,
                proxy=self._proxy,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # 提取 URL
                    urls = re.findall(r"<loc>(https://rentry\.co/[^<]+)</loc>", text)
                    # 只取最近 20 个页面
                    for url in urls[-20:]:
                        await self._fetch_and_scan(session, url, "rentry")
        except Exception:
            pass

    async def _scan_controlc(self, session: aiohttp.ClientSession):
        """扫描 controlc.com。"""
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        # controlc 也没有公开 API，尝试 sitemap
        try:
            async with session.get(
                "https://controlc.com/sitemap.xml",
                timeout=aiohttp.ClientTimeout(total=15),
                headers=headers,
                proxy=self._proxy,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    urls = re.findall(r"<loc>(https://controlc\.com/[^<]+)</loc>", text)
                    for url in urls[-20:]:
                        await self._fetch_and_scan(session, url, "controlc")
        except Exception:
            pass

    async def _fetch_and_scan(self, session: aiohttp.ClientSession,
                               url: str, site: str):
        """获取单个页面并扫描 key。"""
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            # rentry raw 路径是 /{id}/raw,controlc 是 /{id}.txt
            if site == "rentry" and "rentry.co" in url and not url.endswith("/raw"):
                raw_url = url.rstrip("/") + "/raw"
            else:
                raw_url = url + ".txt" if not url.endswith((".txt", "/raw")) else url
            async with session.get(
                raw_url,
                timeout=aiohttp.ClientTimeout(total=10),
                headers=headers,
                proxy=self._proxy,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    keys = extract_keys(text, self.extra_bad)
                    for k in keys:
                        self._add_result(k, url, site, "paste", self.source_name)
        except Exception:
            pass


class SiteDorkScanner(BaseScanner):
    """通过 GitHub Code Search 的 site: 操作符间接扫描贴码站点。

    注意：GitHub Code Search 不支持 site: 操作符。
    这个扫描器作为 PasteSiteScanner 的补充，使用其他搜索引擎。
    """

    @property
    def source_name(self) -> str:
        return "site_dork"

    async def search(self, query: str | None = None) -> list[dict]:
        # 暂不实现 — 需要搜索引擎 API key
        return []
