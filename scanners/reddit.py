"""
Reddit Scanner — 搜索 Reddit 上泄露的 API key
Reddit 是技术社区，经常有人在帖子/评论中分享代码示例（包含 key）
"""

import aiohttp
import asyncio
from .base import BaseScanner, extract_keys


# 相关子版块
SUBREDDITS = [
    "LocalLLaMA",
    "MachineLearning",
    "ChatGPT",
    "deepseek",
    "artificial",
    "OpenAI",
    "selfhosted",
    "huggingface",
]

# 搜索查询
REDDIT_QUERIES = [
    "deepseek sk-",
    "deepseek api_key",
    "DEEPSEEK_API_KEY",
    "deepseek key leaked",
]


class RedditScanner(BaseScanner):
    """搜索 Reddit 上的 API key 泄露"""
    WEB = "https://www.reddit.com"
    API = "https://oauth.reddit.com"

    def __init__(self, token: str = "", max_posts: int = 100, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.max_posts = max_posts
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0 (by /u/deepseek_scanner)"}

    @property
    def source_name(self) -> str:
        return "reddit"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        queries = REDDIT_QUERIES

        async with aiohttp.ClientSession(headers=self._headers) as session:
            # 搜索相关帖子
            for q in queries:
                if self._should_stop():
                    break
                posts = await self._search_posts(session, q)
                for post in posts[:self.max_posts]:
                    if self._should_stop():
                        break
                    await self._scan_post(session, post)
                await asyncio.sleep(2.0)  # Reddit 速率限制

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

    async def _search_posts(self, session, query: str) -> list:
        """搜索 Reddit 帖子"""
        posts = []
        try:
            url = f"{self.WEB}/search.json"
            params = {"q": query, "limit": 50, "sort": "new"}
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    children = data.get("data", {}).get("children", [])
                    for child in children:
                        post = child.get("data", {})
                        posts.append(post)
        except Exception as e:
            self.log(f"Reddit search error: {e}", "warning")
        return posts

    async def _scan_post(self, session, post: dict):
        """扫描帖子内容和评论"""
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        permalink = post.get("permalink", "")
        url = f"https://www.reddit.com{permalink}" if permalink else ""

        # 扫描标题和正文
        all_text = f"{title}\n{selftext}"
        for k in extract_keys(all_text, self.extra_bad):
            self._add_result(k, url, f"reddit:{post.get('subreddit', '')}", "post", self.source_name)

        # 扫描评论
        comments_url = f"{self.WEB}/comments/{post.get('id', '')}.json"
        try:
            async with session.get(comments_url,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 1:
                        comments = data[1].get("data", {}).get("children", [])
                        for comment in comments[:100]:  # 限制评论数
                            body = comment.get("data", {}).get("body", "")
                            if body:
                                for k in extract_keys(body, self.extra_bad):
                                    self._add_result(k, url, f"reddit:{post.get('subreddit', '')}", "comment", self.source_name)
        except Exception:
            pass
