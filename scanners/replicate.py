"""
Replicate Scanner — 搜索 Replicate.com 上的模型和 API key 泄露
Replicate 是一个流行的 AI 模型托管平台，开发者经常硬编码 API key
"""

import aiohttp
import asyncio
from .base import BaseScanner, extract_keys


class ReplicateScanner(BaseScanner):
    """搜索 Replicate 平台"""
    API = "https://api.replicate.com"
    WEB = "https://replicate.com"

    def __init__(self, token: str = "", max_items: int = 100, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.max_items = max_items
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    @property
    def source_name(self) -> str:
        return "replicate"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"

        async with aiohttp.ClientSession(headers=self._headers) as session:
            # 搜索模型
            models = await self._search_models(session, query)
            self.log(f"Replicate models: {len(models)} found")

            for model in models[:self.max_items]:
                if self._should_stop():
                    break
                await self._scan_model(session, model)

        return self.results

    async def _search_models(self, session, query: str) -> list:
        """搜索 Replicate 模型"""
        all_models = []
        try:
            url = f"{self.API}/models"
            params = {"query": query, "limit": 100}
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    all_models.extend(results)
        except Exception as e:
            self.log(f"Replicate search error: {e}", "warning")
        return all_models

    async def _scan_model(self, session, model: dict):
        """扫描模型的配置和文件"""
        model_name = model.get("name", "")
        model_url = model.get("url", f"{self.WEB}/{model_name}")

        # 扫描模型描述
        description = model.get("description", "")
        for k in extract_keys(description, self.extra_bad):
            self._add_result(k, model_url, model_name, "description", self.source_name)

        # 扫描模型的 latest_version
        latest = model.get("latest_version", {})
        if latest:
            # 扫描模型的 cog.yaml 配置
            config = latest.get("cog_version", "")
            # 扫描环境变量
            openapi_schema = latest.get("openapi_schema", {})
            if openapi_schema:
                schema_str = str(openapi_schema)
                for k in extract_keys(schema_str, self.extra_bad):
                    self._add_result(k, model_url, model_name, "openapi_schema", self.source_name)


# 备用：通过网页搜索 Replicate 上的 DeepSeek 相关内容
REPLICATE_SEARCH_QUERIES = [
    "deepseek api_key",
    "deepseek sk-",
    "DEEPSEEK_API_KEY",
]
