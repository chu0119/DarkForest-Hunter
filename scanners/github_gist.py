"""
GitHub Gist Scanner — Public Gist API.
Uses auto-detected gh CLI token for authenticated access (5000 req/hour).
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


class GistScanner(BaseScanner):
    BASE = "https://api.github.com"

    def __init__(self, token: str = "", max_pages: int = 100, **kwargs):
        super().__init__(**kwargs)
        self.token = token or _auto_token()
        self.max_pages = max_pages
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DeepSeekKeyHunter/5.0",
        }
        if self.token:
            self._headers["Authorization"] = f"Bearer {self.token}"

    @property
    def source_name(self) -> str:
        return "github_gist"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        sem = asyncio.Semaphore(self.concurrency)
        # 解析查询关键词用于过滤
        search_terms = []
        if query:
            search_terms = [t.strip().strip('"') for t in query.split() if len(t.strip()) > 1]

        # 产出熔断：连续 N 页无新 key 即停止翻页（避免空跑几十页浪费时间）
        max_barren_pages = 3
        barren_streak = 0

        async with aiohttp.ClientSession(headers=self._headers) as session:
            for page in range(1, self.max_pages + 1):
                if self._should_stop():
                    break

                gists = await self._fetch_page(session, page)
                if not gists:
                    break

                # 如果有搜索关键词，先过滤 gists
                if search_terms:
                    filtered = []
                    for g in gists:
                        desc = (g.get("description") or "").lower()
                        files_str = " ".join(g.get("files", {}).keys()).lower()
                        if any(t.lower() in desc or t.lower() in files_str for t in search_terms):
                            filtered.append(g)
                    gists = filtered

                results_before = len(self.results)
                tasks = [self._scan_gist(session, sem, g) for g in gists]
                await asyncio.gather(*tasks)

                # 产出熔断判断：本页是否带来了新 key
                if len(self.results) > results_before:
                    barren_streak = 0
                else:
                    barren_streak += 1
                    if barren_streak >= max_barren_pages:
                        # 连续多页无产出，继续翻页大概率也是空的，提前结束
                        break

                if len(gists) < 100:
                    break

        return self.results

    async def _fetch_page(self, session: aiohttp.ClientSession, page: int) -> list:
        url = f"{self.BASE}/gists/public?per_page=100&page={page}"
        for attempt in range(3):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 403:
                        if "rate limit" in (await resp.text()).lower():
                            await asyncio.sleep(10 * (attempt + 1))
                            continue
                        return []
                    elif resp.status == 429:
                        await asyncio.sleep(10 * (attempt + 1))
                        continue
                    return []
            except asyncio.TimeoutError:
                await asyncio.sleep(2)
            except Exception:
                return []
        return []

    async def _scan_gist(self, session, sem, gist: dict):
        gist_id = gist.get("id", "")
        files = gist.get("files", {})
        description = gist.get("description", "") or ""
        html_url = gist.get("html_url", "")

        for k in extract_keys(description, self.extra_bad):
            self._add_result(k, html_url, f"gist:{gist_id}", "description", self.source_name)

        for fname, finfo in files.items():
            raw_url = finfo.get("raw_url", "")
            if not raw_url:
                continue

            async with sem:
                for attempt in range(2):
                    try:
                        async with session.get(raw_url,
                                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                text = await resp.text()
                                for k in extract_keys(text, self.extra_bad):
                                    self._add_result(k, html_url, f"gist:{gist_id}", fname, self.source_name)
                            break
                    except (asyncio.TimeoutError, aiohttp.ClientError):
                        await asyncio.sleep(1)
                    except Exception:
                        break
