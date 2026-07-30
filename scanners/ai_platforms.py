"""
AI Platform Scanner — 扫描多个 AI 平台的模型和 API key 泄露
支持: Civitai, Fal.ai, Together AI, Modal, Groq, DeepInfra, Cohere
"""

import aiohttp
import asyncio
import json
from .base import BaseScanner, extract_keys


class CivitaiScanner(BaseScanner):
    """搜索 Civitai.com (Stable Diffusion 模型平台)"""
    API = "https://civitai.com/api/v1"
    WEB = "https://civitai.com"

    def __init__(self, max_items: int = 50, **kwargs):
        super().__init__(**kwargs)
        self.max_items = max_items

    @property
    def source_name(self) -> str:
        return "civitai"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"

        async with aiohttp.ClientSession() as session:
            models = await self._search_models(session, query)
            self.log(f"Civitai models: {len(models)} found")

            for model in models[:self.max_items]:
                if self._should_stop():
                    break
                await self._scan_model(session, model)

        return self.results

    async def _search_models(self, session, query: str) -> list:
        try:
            url = f"{self.API}/models"
            params = {"query": query, "limit": 100, "nsfw": "false"}
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("items", [])
        except Exception as e:
            self.log(f"Civitai search error: {e}", "warning")
        return []

    async def _scan_model(self, session, model: dict):
        model_id = model.get("id", "")
        name = model.get("name", "")
        description = model.get("description", "")
        url = f"{self.WEB}/{model_id}"

        # 扫描模型描述
        for k in extract_keys(description, self.extra_bad):
            self._add_result(k, url, f"civitai:{model_id}", "description", self.source_name)

        # 扫描模型版本
        for version in model.get("modelVersions", []):
            v_desc = version.get("description", "")
            for k in extract_keys(v_desc, self.extra_bad):
                self._add_result(k, url, f"civitai:{model_id}", f"version:{version.get('name', '')}", self.source_name)


class TogetherAIScanner(BaseScanner):
    """搜索 Together AI 平台"""
    WEB = "https://api.together.xyz"

    def __init__(self, token: str = "", max_items: int = 50, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.max_items = max_items
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    @property
    def source_name(self) -> str:
        return "together_ai"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        # Together AI 主要通过 API 提供服务，搜索有限
        # 通过搜索公开的模型列表
        async with aiohttp.ClientSession(headers=self._headers) as session:
            try:
                url = f"{self.WEB}/v1/models"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = data if isinstance(data, list) else data.get("data", [])
                        for m in models[:self.max_items]:
                            if self._should_stop():
                                break
                            # 扫描模型描述
                            desc = m.get("description", "")
                            name = m.get("id", "")
                            for k in extract_keys(desc, self.extra_bad):
                                self._add_result(k, f"{self.WEB}/v1/models/{name}", name, "description", self.source_name)
            except Exception as e:
                self.log(f"Together AI error: {e}", "warning")

        return self.results


class ModalScanner(BaseScanner):
    """搜索 Modal.com 平台 (Serverless AI compute)"""
    WEB = "https://modal.com"

    def __init__(self, max_items: int = 50, **kwargs):
        super().__init__(**kwargs)
        self.max_items = max_items

    @property
    def source_name(self) -> str:
        return "modal"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        # Modal 主要通过 GitHub 示例和文档泄露
        # 搜索 Modal 相关的 GitHub 仓库
        async with aiohttp.ClientSession() as session:
            try:
                url = "https://api.github.com/search/code"
                headers = {"Accept": "application/vnd.github.text-match+json"}
                params = {"q": f"modal.com deepseek sk-", "per_page": 30}
                async with session.get(url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", [])
                        for item in items[:self.max_items]:
                            if self._should_stop():
                                break
                            html_url = item.get("html_url", "")
                            # 从 text_matches 提取
                            for match in item.get("text_matches", []):
                                fragment = match.get("fragment", "")
                                for k in extract_keys(fragment, self.extra_bad):
                                    self._add_result(k, html_url, "modal_github", item.get("path", ""), self.source_name)
            except Exception as e:
                self.log(f"Modal search error: {e}", "warning")

        return self.results


class GroqScanner(BaseScanner):
    """搜索 Groq 平台"""
    WEB = "https://api.groq.com"

    def __init__(self, token: str = "", max_items: int = 50, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.max_items = max_items
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    @property
    def source_name(self) -> str:
        return "groq"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        # Groq API key 格式: gsk_...
        # 搜索 GitHub 上包含 groq api_key 的代码
        async with aiohttp.ClientSession(headers=self._headers) as session:
            try:
                url = "https://api.github.com/search/code"
                headers = {"Accept": "application/vnd.github.text-match+json"}
                params = {"q": f"groq api_key sk-", "per_page": 30}
                async with session.get(url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", [])
                        for item in items[:self.max_items]:
                            if self._should_stop():
                                break
                            html_url = item.get("html_url", "")
                            for match in item.get("text_matches", []):
                                fragment = match.get("fragment", "")
                                for k in extract_keys(fragment, self.extra_bad):
                                    self._add_result(k, html_url, "groq_github", item.get("path", ""), self.source_name)
            except Exception as e:
                self.log(f"Groq search error: {e}", "warning")

        return self.results


class DeepInfraScanner(BaseScanner):
    """搜索 DeepInfra 平台"""
    WEB = "https://deepinfra.com"

    def __init__(self, max_items: int = 50, **kwargs):
        super().__init__(**kwargs)
        self.max_items = max_items

    @property
    def source_name(self) -> str:
        return "deepinfra"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        # 搜索 GitHub 上包含 deepinfra api_key 的代码
        async with aiohttp.ClientSession() as session:
            try:
                url = "https://api.github.com/search/code"
                headers = {"Accept": "application/vnd.github.text-match+json"}
                params = {"q": f"deepinfra api_key sk-", "per_page": 30}
                async with session.get(url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", [])
                        for item in items[:self.max_items]:
                            if self._should_stop():
                                break
                            html_url = item.get("html_url", "")
                            for match in item.get("text_matches", []):
                                fragment = match.get("fragment", "")
                                for k in extract_keys(fragment, self.extra_bad):
                                    self._add_result(k, html_url, "deepinfra_github", item.get("path", ""), self.source_name)
            except Exception as e:
                self.log(f"DeepInfra search error: {e}", "warning")

        return self.results


class FalAIScanner(BaseScanner):
    """搜索 Fal.ai 平台"""
    WEB = "https://fal.ai"

    def __init__(self, max_items: int = 50, **kwargs):
        super().__init__(**kwargs)
        self.max_items = max_items

    @property
    def source_name(self) -> str:
        return "fal_ai"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        # 搜索 GitHub 上包含 fal.ai api_key 的代码
        async with aiohttp.ClientSession() as session:
            try:
                url = "https://api.github.com/search/code"
                headers = {"Accept": "application/vnd.github.text-match+json"}
                params = {"q": f"fal.ai api_key sk-", "per_page": 30}
                async with session.get(url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", [])
                        for item in items[:self.max_items]:
                            if self._should_stop():
                                break
                            html_url = item.get("html_url", "")
                            for match in item.get("text_matches", []):
                                fragment = match.get("fragment", "")
                                for k in extract_keys(fragment, self.extra_bad):
                                    self._add_result(k, html_url, "fal_ai_github", item.get("path", ""), self.source_name)
            except Exception as e:
                self.log(f"Fal.ai search error: {e}", "warning")

        return self.results
