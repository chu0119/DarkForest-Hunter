"""
HuggingFace Scanner — Search models, datasets, and spaces for leaked DeepSeek keys.
HuggingFace is a major hub for AI projects; many embed API keys in example
notebooks, inference configs, and space secrets.
"""

import asyncio
import re
import time

import aiohttp

from .base import BaseScanner, extract_keys


class HuggingFaceScanner(BaseScanner):
    API = "https://huggingface.co/api"
    HF_HUB = "https://huggingface.co"

    def __init__(self, token: str = "", max_items: int = 200,
                 deadline_s: float = 18.0, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.max_items = max_items
        self._deadline = deadline_s
        self._deadline_end = 0.0  # search() 启动时设置
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    @property
    def source_name(self) -> str:
        return "huggingface"

    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"
        sem = asyncio.Semaphore(self.concurrency)
        self._deadline_end = time.monotonic() + self._deadline

        connector = aiohttp.TCPConnector(limit=10)
        try:
            async with aiohttp.ClientSession(headers=self._headers, connector=connector) as session:
                async with asyncio.timeout(self._deadline):
                    # 三种实体类型并发搜索；spaces 加 SDK 过滤（gradio/streamlit
                    # 有可执行 Python 代码，是泄露高发区；Docker space 代码在容器内不在 repo）
                    models, datasets, spaces = await asyncio.gather(
                        self._hf_search(session, query, "model"),
                        self._hf_search(session, query, "dataset"),
                        self._hf_search(session, query, "space", sdk_filter="gradio"),
                    )
                    # 如果 gradio 结果不够，补充 streamlit
                    if len(spaces) < 10:
                        more_spaces = await self._hf_search(session, query, "space", sdk_filter="streamlit")
                        seen_ids = {s.get("id") for s in spaces}
                        spaces.extend(s for s in more_spaces if s.get("id") not in seen_ids)

                    self.log(f"HF: {len(models)} models, {len(datasets)} datasets, {len(spaces)} spaces")

                    scan_tasks = []
                    # spaces 优先（泄露高发区）：70% 预算
                    space_limit = int(self.max_items * 0.7)
                    other_limit = max(1, (self.max_items - space_limit) // 2)
                    for s in spaces[:space_limit]:
                        scan_tasks.append(self._scan_space(session, sem, s))
                    for m in models[:other_limit]:
                        scan_tasks.append(self._scan_model(session, sem, m))
                    for d in datasets[:other_limit]:
                        scan_tasks.append(self._scan_dataset(session, sem, d))

                    if scan_tasks and not self._should_stop():
                        await asyncio.gather(*scan_tasks, return_exceptions=True)
        except TimeoutError:
            # deadline 到点：保留已累积的 self.results，不当错误
            pass

        return self.results

    async def _hf_search(self, session, q: str, item_type: str, sdk_filter: str = "") -> list:
        all_items = []
        url = f"{self.API}/{item_type}s"
        params = {"search": q, "limit": 50, "full": "False", "sort": "lastModified"}
        if sdk_filter:
            params["filter"] = sdk_filter
        retries_429 = 0
        while url and len(all_items) < self.max_items:
            if self._should_stop():
                break
            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=20, connect=10),
                                       proxy=self._proxy) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", data) if isinstance(data, dict) else data
                        if not isinstance(items, list):
                            break
                        all_items.extend(items)
                        if len(items) < 50:
                            break
                        # HF 使用 base64 cursor 分页，通过 Link 头 rel="next" 传递
                        link = resp.headers.get("Link", "")
                        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
                        if m:
                            url = m.group(1)
                            params = None  # next_url 已包含查询参数
                        else:
                            break
                    elif resp.status == 429:
                        # 最多重试 1 次，避免连续 429 导致无限 sleep/continue
                        if retries_429 >= 1:
                            break
                        retries_429 += 1
                        # 优先使用服务端 Retry-After，否则回退到 8s
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            wait = float(retry_after) if retry_after else 8.0
                        except ValueError:
                            wait = 8.0
                        # 截断到剩余 deadline 与 8s 上限，防止过度等待
                        wait = min(wait, max(0.0, self._deadline_end - time.monotonic()), 8.0)
                        await asyncio.sleep(wait)
                        continue
                    else:
                        break
            except Exception:
                break
            await asyncio.sleep(0.3)
        return all_items

    async def _scan_model(self, session, sem, model: dict):
        repo_id = model.get("id", "")
        if repo_id:
            await self._scan_repo_files(session, sem, repo_id, "model")

    async def _scan_dataset(self, session, sem, dataset: dict):
        repo_id = dataset.get("id", "")
        if repo_id:
            await self._scan_repo_files(session, sem, repo_id, "dataset")

    async def _scan_space(self, session, sem, space: dict):
        repo_id = space.get("id", "")
        if repo_id:
            # README.md 已在 _scan_repo_files 中随文件列表一并扫描，无需重复拉取
            await self._scan_repo_files(session, sem, repo_id, "space")

    # 优先级文件模式（递归树发现的文件按此排序，高优先先下载）
    _FILE_PRIORITY = [
        (re.compile(r'\.env', re.I), 100),           # .env, .env.local, .env.production
        (re.compile(r'docker-compose', re.I), 90),    # docker-compose.yml
        (re.compile(r'\.py$', re.I), 80),             # Python 源码
        (re.compile(r'\.(js|ts|tsx|jsx)$', re.I), 70), # JS/TS
        (re.compile(r'\.(toml|yml|yaml|cfg|ini|properties)$', re.I), 60), # 配置
        (re.compile(r'\.json$', re.I), 50),           # JSON（config.json 等）
        (re.compile(r'\.(sh|bash)$', re.I), 40),      # Shell
        (re.compile(r'\.(md|txt|ipynb)$', re.I), 20), # 文档
    ]
    # 跳过大型二进制模型文件
    _SKIP_EXT = re.compile(r'\.(safetensors|bin|pt|pth|onnx|gguf|ggml|parquet|arrow|csv|pkl|npy|h5|tflite|model)$', re.I)

    async def _scan_repo_files(self, session, sem, repo_id: str, item_type: str):
        """扫描仓库文件：递归 tree 列举 + 智能优先级选文件。

        使用 recursive=true 发现子目录文件（.env, config/, src/ 等），
        按文件模式优先级排序后下载高价值文件（封顶 _MAX_FILES_PER_REPO）。
        """
        _MAX_FILES_PER_REPO = 10  # 每个 repo 最多下载的文件数

        # 尝试 main 分支，失败则尝试 master
        files = None
        branch_used = "main"
        for branch in ("main", "master"):
            # recursive=true 发现全部文件（包括子目录）
            api_url = f"{self.API}/{item_type}s/{repo_id}/tree/{branch}/?recursive=true&limit=1000"
            try:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15, connect=5),
                                       proxy=self._proxy) as resp:
                    if resp.status == 200:
                        files = await resp.json()
                        branch_used = branch
                        break
            except Exception:
                continue

        # 根目录入口文件（space 必有 app.py/main.py，model/dataset 必有 README）
        if item_type == "space":
            entry_candidates = ["app.py", "main.py", "README.md"]
        else:
            entry_candidates = ["README.md", "config.json", ".env"]
        target_paths = set(entry_candidates)

        # 递归树结果：按优先级排序选文件
        if isinstance(files, list):
            scored = []
            for f in files:
                if f.get("type") != "file":
                    continue
                path = f.get("path", "")
                fname = path.split("/")[-1].lower()
                # 跳过大型二进制文件
                if self._SKIP_EXT.search(fname):
                    continue
                # 计算优先级分数
                score = 0
                for pattern, s in self._FILE_PRIORITY:
                    if pattern.search(fname):
                        score = max(score, s)
                if score > 0:
                    scored.append((score, path))
            # 按分数降序，取前 N 个
            scored.sort(reverse=True)
            for _, path in scored[:_MAX_FILES_PER_REPO]:
                target_paths.add(path)

        # Download all target files concurrently (semaphore controls parallelism)
        async def fetch_one(path: str):
            async with sem:
                raw_url = f"{self.HF_HUB}/{repo_id}/raw/{branch_used}/{path}"
                try:
                    async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=15, connect=5),
                                           proxy=self._proxy) as r:
                        if r.status == 200:
                            text = await r.text()
                            for k in extract_keys(text, self.extra_bad):
                                self._add_result(k, f"{self.HF_HUB}/{repo_id}/blob/{branch_used}/{path}",
                                                 repo_id, path, self.source_name)
                except Exception:
                    pass

        await asyncio.gather(*[fetch_one(p) for p in target_paths], return_exceptions=True)
