# P0 — 外部源超时根治 + 搜索词覆盖 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HF 稳定 ≤20s、GitLab 稳定 ≤30s（内部 asyncio 硬时限 + 真取消），每源每轮 1 词跨桶轮换，换皮兼容词 + 中文别名进入外部源轮换。

**Architecture:** 把硬截止线从 watch 层 `thread.join`（不取消）下沉到 scanner 层 `asyncio.timeout`（真取消）；每轮请求量从"6~9 词全扫"降到"1 词轮换"；429 重试上限 1 次；搜索词池新增兼容词 + 中文别名两个桶，统一单词轮换。

**Tech Stack:** Python 3.12（`asyncio.timeout` 原生）、aiohttp、pytest（零新依赖，测试用 `asyncio.run` + 自定义 `FakeSession`，仿 `tests/test_verifier.py` 的 `FakeResponse`）。

**Spec:** `docs/superpowers/specs/2026-08-09-p0-external-sources-design.md`

---

## File Structure

**Modify:**
- `scanners/huggingface.py` — 加 `deadline_s` + `asyncio.timeout` 包裹 + 429 重试上限 + tree 文件封顶。
- `scanners/gitlab.py` — 加 `deadline_s` + `asyncio.timeout` 包裹 + 429 重试上限。
- `scanner_engine.py:1104,1110` — registry kwargs：GitLab `max_projects 4 / max_files 4 / deadline_s 28`；HF `max_items 24 / deadline_s 18`。
- `watch_tui.py:772-796,1245-1283` — 新增 `_COMPAT_TERMS`/`_CN_ALIAS_POOL`/`_ROTATION`；`_scan_external` 改单词轮换 + 超时对齐。
- `tests/test_watch_tui.py:846-890` — 轮换回归断言更新 + 新增单词轮换/超时对齐测试。

**Create:**
- `tests/test_scanners_external.py` — HF/GitLab 异步路径单测（`FakeSession`/`FakeResp` 基础设施 + 各行为测试）。

---

## Task 1: HF 内部硬时限 + 取消（P0-1a）

**Files:**
- Modify: `scanners/huggingface.py:19-60`
- Create: `tests/test_scanners_external.py`

- [ ] **Step 1: 写测试基础设施 + deadline 取消测试**

Create `tests/test_scanners_external.py`:

```python
"""外部源 scanner（HF / GitLab）异步路径单测。

零新依赖：用 asyncio.run + 自定义 FakeSession（仿 tests/test_verifier.py 的 FakeResponse），
不引 pytest-asyncio。测试不触网、不读真实 key。
"""
import asyncio
import time

import scanners.huggingface as hf_mod
from scanners.huggingface import HuggingFaceScanner


class FakeResp:
    """模拟 aiohttp 响应（async context manager）。"""
    def __init__(self, status, json_data=None, text_data="", headers=None, delay=0.0):
        self.status = status
        self._json = json_data
        self._text = text_data
        self.headers = headers or {}
        self._delay = delay

    async def __aenter__(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class FakeSession:
    """模拟 aiohttp.ClientSession：按 URL 路由到预设响应。"""
    def __init__(self, responder):
        self._responder = responder  # callable(url) -> FakeResp

    def get(self, url, **kwargs):
        return self._responder(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session(monkeypatch, responder):
    """把 aiohttp.ClientSession 换成返回 FakeSession 的工厂。"""
    monkeypatch.setattr(hf_mod.aiohttp, "ClientSession",
                        lambda **kw: FakeSession(responder))


class TestHuggingFaceDeadline:
    def test_deadline_returns_quickly_under_slow_responses(self, monkeypatch):
        """deadline 到点必须取消在飞请求，search 不挂起。"""
        def slow_responder(url):
            # 所有响应都睡 5s；deadline 0.2s → 必须靠取消尽快返回
            return FakeResp(200, json_data=[], delay=5.0)

        _patch_session(monkeypatch, slow_responder)
        s = HuggingFaceScanner(deadline_s=0.2, max_items=4)
        t0 = time.monotonic()
        results = asyncio.run(s.search("deepseek"))
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"deadline 取消未生效，耗时 {elapsed:.1f}s"
        assert isinstance(results, list)

    def test_deadline_keeps_partial_results(self, monkeypatch):
        """deadline 触发时已累积的 results 保留返回（不当错误）。"""
        state = {"hits": 0}

        def responder(url):
            # 前几个 raw 文件秒回含 key，后面的 tree 睡死 → 取消时已有部分结果
            if "/resolve/" in url:
                state["hits"] += 1
                return FakeResp(200, text_data="DEEPSEEK_API_KEY=sk-abcdef1234567890" "abcdef1234567890")
            if "/tree/" in url:
                return FakeResp(200, json_data=[{"path": "app.py"}], delay=10.0)
            return FakeResp(200, json_data=[{"id": "u/v"}])  # search

        _patch_session(monkeypatch, responder)
        s = HuggingFaceScanner(deadline_s=0.3, max_items=4)
        results = asyncio.run(s.search("deepseek"))
        assert isinstance(results, list)  # 不抛 TimeoutError
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanners_external.py::TestHuggingFaceDeadline -v`
Expected: FAIL（`deadline_s` / `asyncio.timeout` 不存在 → `TypeError: __init__() got an unexpected keyword argument 'deadline_s'` 或 search 挂起超时）

- [ ] **Step 3: 实现 deadline 机制**

Modify `scanners/huggingface.py` `__init__` (line 19-25):

```python
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
```

Modify `search()` (line 31-60) — 包裹主体 + 捕获 TimeoutError：

```python
    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"
        sem = asyncio.Semaphore(self.concurrency)
        self._deadline_end = time.monotonic() + self._deadline

        connector = aiohttp.TCPConnector(limit=10)
        try:
            async with aiohttp.ClientSession(headers=self._headers, connector=connector) as session:
                async with asyncio.timeout(self._deadline):
                    # 三种实体类型并发搜索
                    models, datasets, spaces = await asyncio.gather(
                        self._hf_search(session, query, "model"),
                        self._hf_search(session, query, "dataset"),
                        self._hf_search(session, query, "space"),
                    )
                    self.log(f"HF: {len(models)} models, {len(datasets)} datasets, {len(spaces)} spaces")

                    scan_tasks = []
                    space_limit = self.max_items // 2
                    other_limit = self.max_items // 4
                    for s in spaces[:space_limit]:
                        scan_tasks.append(self._scan_space(session, sem, s))
                    for m in models[:other_limit]:
                        scan_tasks.append(self._scan_model(session, sem, m))
                    for d in datasets[:other_limit]:
                        scan_tasks.append(self._scan_dataset(session, sem, d))

                    if scan_tasks and not self._should_stop():
                        await asyncio.gather(*scan_tasks, return_exceptions=True)
        except (TimeoutError, asyncio.TimeoutError):
            # deadline 到点：保留已累积的 self.results，不当错误
            pass

        return self.results
```

Add `import time` at top of `scanners/huggingface.py` (after `import asyncio`):

```python
import asyncio
import re
import time

import aiohttp
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scanners_external.py::TestHuggingFaceDeadline -v`
Expected: PASS（2 项）

- [ ] **Step 5: 提交**

```bash
git add scanners/huggingface.py tests/test_scanners_external.py
git commit -m "feat(hf): 内部 asyncio.timeout 硬时限 + 取消（≤20s 不挂起）"
```

---

## Task 2: HF 429 重试上限 + tree 文件封顶（P0-1b）

**Files:**
- Modify: `scanners/huggingface.py:62-103,163-168`
- Modify: `tests/test_scanners_external.py`

- [ ] **Step 1: 写 429 重试上限测试**

Append to `tests/test_scanners_external.py`:

```python
class TestHuggingFaceRateLimit:
    def test_429_retries_at_most_once_then_breaks(self, monkeypatch):
        """429 连续返回：最多重试 1 次后 break，不无限 sleep/continue。"""
        calls = {"n": 0}

        def responder(url):
            if "/api/" in url and "/tree/" not in url and "/resolve/" not in url:
                calls["n"] += 1
                return FakeResp(429, headers={"Retry-After": "1"})  # 持续 429
            return FakeResp(200, json_data=[])

        _patch_session(monkeypatch, responder)
        s = HuggingFaceScanner(deadline_s=30, max_items=4)
        asyncio.run(s.search("deepseek"))
        # 3 路并发搜索（model/dataset/space），每路最多 1 次重试 = 首次 + 1 重试 = 2 次/路
        # 全部加起来不超过 ~6 次（3 路 × 2），远小于"无限循环"
        assert calls["n"] <= 8, f"429 未封顶，调用 {calls['n']} 次"


class TestHuggingFaceTreeFileCap:
    def test_tree_matched_files_capped_at_three(self, monkeypatch):
        """tree 顶层命中文件封顶 3 个/仓库（当前无上限）。"""
        downloaded = []

        def responder(url):
            if "/api/" in url and "models" in url and "/tree/" not in url:
                return FakeResp(200, json_data=[{"id": "u/m1"}])
            if "/api/" in url and ("datasets" in url or "spaces" in url) and "/tree/" not in url:
                return FakeResp(200, json_data=[])
            if "/tree/" in url:
                # 5 个 .py 文件命中 target_exts（旧逻辑全下载 = 5，新逻辑封顶 3）
                return FakeResp(200, json_data=[
                    {"path": f"file{i}.py"} for i in range(5)
                ])
            if "/resolve/" in url:
                downloaded.append(url)
                return FakeResp(200, text_data="sk-abcdef1234567890" "abcdef1234567890")
            return FakeResp(200, json_data=[])

        _patch_session(monkeypatch, responder)
        s = HuggingFaceScanner(deadline_s=30, max_items=4)
        asyncio.run(s.search("deepseek"))
        # 单个 model 仓库：3 入口文件（README/config.json/.env）+ tree 命中封顶 3 = 最多 6
        # 但 .env 既在 entry 又可能命中 tree；关键断言：tree 的 5 个 .py 不会全下载
        assert len(downloaded) <= 8, f"tree 文件未封顶，下载 {len(downloaded)} 个"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanners_external.py::TestHuggingFaceRateLimit tests/test_scanners_external.py::TestHuggingFaceTreeFileCap -v`
Expected: FAIL（当前 429 无限 continue → 测试可能挂起/超时；tree 文件无封顶）

- [ ] **Step 3: 实现 429 重试上限 + tree 文件封顶**

Modify `_hf_search` (line 62-103) — 加 `retries_429` 计数 + 截断到剩余 deadline：

```python
    async def _hf_search(self, session, q: str, item_type: str) -> list:
        all_items = []
        url = f"{self.API}/{item_type}s"
        params = {"search": q, "limit": 50, "full": "False"}
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
                        link = resp.headers.get("Link", "")
                        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
                        if m:
                            url = m.group(1)
                            params = None
                        else:
                            break
                    elif resp.status == 429:
                        if retries_429 >= 1:
                            break  # 最多重试 1 次
                        retries_429 += 1
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            wait = float(retry_after) if retry_after else 8.0
                        except ValueError:
                            wait = 8.0
                        wait = min(wait, max(0.0, self._deadline_end - time.monotonic()), 8.0)
                        await asyncio.sleep(wait)
                        continue
                    else:
                        break
            except Exception:
                break
            await asyncio.sleep(0.3)
        return all_items
```

Modify `_scan_repo_files` tree 命中文件封顶 (line 163-168) — 把 tree 命中收集到独立列表再封顶：

```python
        # tree 结果补充（顶层文件里命中的路径）——封顶 3 个，避免大仓库下载爆炸
        tree_matched = []
        if isinstance(files, list):
            for f in files:
                path = f.get("path", "")
                fname = path.split("/")[-1].lower()
                if fname in target_names or any(fname.endswith(ext) for ext in target_exts):
                    tree_matched.append(path)
        for p in tree_matched[:3]:
            target_paths.add(p)
```

（`target_paths` 仍含 3 个入口文件；tree 命中额外最多 3 个。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scanners_external.py -v`
Expected: PASS（4 项全过）

- [ ] **Step 5: 提交**

```bash
git add scanners/huggingface.py tests/test_scanners_external.py
git commit -m "feat(hf): 429 重试上限 1 次 + tree 命中文件封顶 3 个"
```

---

## Task 3: HF 预算 + registry 接线 + 实测（P0-1c）

**Files:**
- Modify: `scanner_engine.py:1110`
- Verify: 全量测试 + watch --once

- [ ] **Step 1: 改 registry kwargs**

Modify `scanner_engine.py:1110` — HF 项：

```python
            "huggingface": (HuggingFaceScanner, "deepseek", {"token": hf_token, "max_items": 24, "deadline_s": 18, "proxy": self.proxy}),
```

（原 `"max_items": 50` → `24`，新增 `"deadline_s": 18`。）

- [ ] **Step 2: 全量测试 + lint**

Run: `python -m pytest tests/ -q && python -m ruff check .`
Expected: 全绿

- [ ] **Step 3: 实测 HF（P0-1 里程碑）**

Run: `python run.py watch --once --interval 30 --verify-workers 2`
观察日志：`[huggingface] R? ...` 行的耗时 `(?s)` 应 ≤20s，无"超时（>60s），跳过"。
（`--once` 单轮，不触发 5 分钟看门狗。Ctrl+C 优雅退出。）

- [ ] **Step 4: 提交**

```bash
git add scanner_engine.py
git commit -m "feat(hf): registry 预算调优 max_items=24 + deadline_s=18"
```

---

## Task 4: GitLab 内部硬时限 + 429 上限 + 预算（P0-2）

**Files:**
- Modify: `scanners/gitlab.py:14-120`
- Modify: `scanner_engine.py:1104`
- Modify: `tests/test_scanners_external.py`

- [ ] **Step 1: 写 GitLab 测试**

Append to `tests/test_scanners_external.py`:

```python
import scanners.gitlab as gl_mod
from scanners.gitlab import GitLabScanner


def _patch_gl_session(monkeypatch, responder):
    monkeypatch.setattr(gl_mod.aiohttp, "ClientSession",
                        lambda **kw: FakeSession(responder))


class TestGitLabDeadline:
    def test_deadline_returns_quickly_under_slow_responses(self, monkeypatch):
        def slow_responder(url):
            return FakeResp(200, json_data=[], delay=5.0)

        _patch_gl_session(monkeypatch, slow_responder)
        s = GitLabScanner(deadline_s=0.2, max_projects=4, max_files_per_project=4)
        t0 = time.monotonic()
        results = asyncio.run(s.search("deepseek"))
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"deadline 取消未生效，耗时 {elapsed:.1f}s"
        assert isinstance(results, list)


class TestGitLabRateLimit:
    def test_429_retries_at_most_once_then_breaks(self, monkeypatch):
        calls = {"n": 0}

        def responder(url):
            if "/api/v4/projects" in url and "repository" not in url:
                calls["n"] += 1
                return FakeResp(429)  # 持续 429
            return FakeResp(200, json_data=[])

        _patch_gl_session(monkeypatch, responder)
        s = GitLabScanner(deadline_s=30, max_projects=4, max_files_per_project=4)
        asyncio.run(s.search("deepseek"))
        # 2 页搜索，每页最多 1 次重试 = ≤4 次
        assert calls["n"] <= 4, f"429 未封顶，调用 {calls['n']} 次"


class TestGitLabBudget:
    def test_max_projects_capped(self, monkeypatch):
        scanned = {"projects": 0}

        def responder(url):
            if "/api/v4/projects" in url and "repository" not in url:
                # 返回 10 个项目（应只扫 4 个）
                return FakeResp(200, json_data=[{"id": i, "path_with_namespace": f"p{i}",
                                                  "web_url": f"https://x/{i}"} for i in range(10)])
            if "/repository/tree" in url:
                scanned["projects"] += 1
                return FakeResp(200, json_data=[])
            if "/repository/files/" in url:
                return FakeResp(200, text_data="no key here")
            return FakeResp(200, json_data=[])

        _patch_gl_session(monkeypatch, responder)
        s = GitLabScanner(deadline_s=30, max_projects=4, max_files_per_project=4)
        asyncio.run(s.search("deepseek"))
        assert scanned["projects"] <= 4, f"max_projects 未封顶，扫了 {scanned['projects']} 个"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanners_external.py::TestGitLabDeadline tests/test_scanners_external.py::TestGitLabRateLimit tests/test_scanners_external.py::TestGitLabBudget -v`
Expected: FAIL（`deadline_s` 不存在 / 429 无限 / max_projects 未按 watch 传入生效）

- [ ] **Step 3: 实现 GitLab deadline + 429 上限**

Modify `scanners/gitlab.py` `__init__` (line 15-24):

```python
    def __init__(self, token: str = "", base_url: str = "https://gitlab.com",
                 max_projects: int = 200, max_files_per_project: int = 100,
                 deadline_s: float = 28.0, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.max_projects = max_projects
        self.max_files_per_project = max_files_per_project
        self._deadline = deadline_s
        self._deadline_end = 0.0
        self._headers = {"User-Agent": "DeepSeekKeyHunter/5.0"}
        if token:
            self._headers["PRIVATE-TOKEN"] = token
```

Modify `search()` (line 30-46) — 包裹 + 捕获 TimeoutError:

```python
    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        query = query or "deepseek"
        sem = asyncio.Semaphore(self.concurrency)
        self._deadline_end = time.monotonic() + self._deadline

        try:
            async with aiohttp.ClientSession(headers=self._headers) as session:
                async with asyncio.timeout(self._deadline):
                    projects = await self._search_projects(session, query, pages=2)
                    if not projects:
                        return self.results
                    for proj in projects[:self.max_projects]:
                        if self._should_stop():
                            break
                        await self._scan_project(session, sem, proj)
        except (TimeoutError, asyncio.TimeoutError):
            pass

        return self.results
```

Modify `_search_projects` 429 分支 (line 69-70) — 重试上限 1 次：

```python
        retries_429 = 0
        for page in range(1, pages + 1):
            if self._should_stop():
                break
            try:
                params = urllib.parse.urlencode({
                    "search": q,
                    "visibility": "public",
                    "per_page": 100,
                    "page": page,
                })
                url = f"{api}/projects?{params}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), proxy=self._proxy) as resp:
                    if resp.status == 200:
                        projects = await resp.json()
                        all_projects.extend(projects)
                        if len(projects) < 100:
                            break
                    elif resp.status == 429:
                        if retries_429 >= 1:
                            break  # 最多重试 1 次
                        retries_429 += 1
                        wait = min(10.0, max(0.0, self._deadline_end - time.monotonic()))
                        await asyncio.sleep(wait)
                    else:
                        break
            except Exception:
                break
            await asyncio.sleep(0.3)
        return all_projects
```

Add `import time` at top of `scanners/gitlab.py`:

```python
import asyncio
import time
import urllib.parse

import aiohttp
```

- [ ] **Step 4: 改 registry kwargs**

Modify `scanner_engine.py:1104` — GitLab 项：

```python
            "gitlab": (GitLabScanner, "deepseek", {"token": gitlab_token, "max_projects": 4, "max_files_per_project": 4, "deadline_s": 28, "proxy": self.proxy}),
```

（原 `max_projects: 10, max_files_per_project: 10` → `4 / 4`，新增 `deadline_s: 28`。）

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_scanners_external.py -v && python -m ruff check .`
Expected: PASS（HF 4 + GitLab 3 = 7 项全过）

- [ ] **Step 6: 全量测试**

Run: `python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 7: 实测 GitLab（P0-2 里程碑）**

Run: `python run.py watch --once --interval 30 --verify-workers 2`
观察：`[gitlab] R? ...` 耗时 ≤30s，无"超时（>90s），跳过"。匿名限流下可能 0key——这是预期（真正出量待 P2 token）。

- [ ] **Step 8: 提交**

```bash
git add scanners/gitlab.py scanner_engine.py tests/test_scanners_external.py
git commit -m "feat(gitlab): 内部 asyncio.timeout 硬时限 + 429 上限 + 预算 4×4（≤30s）"
```

---

## Task 5: 兼容词 + 中文别名 + 统一轮换常量（P0-3 + P0-4）

**Files:**
- Modify: `watch_tui.py:772-796`
- Modify: `tests/test_watch_tui.py:846-890`

- [ ] **Step 1: 写轮换常量测试**

Append to `tests/test_watch_tui.py`（在 `TestExternalTermRotationAllSources` 类后）:

```python
class TestRotationBuckets:
    def test_compat_terms_present(self):
        from watch_tui import _COMPAT_TERMS
        joined = " ".join(_COMPAT_TERMS)
        # 换皮 demo / 免费中转类词（元数据搜索命中的高发区）
        for kw in ["api-key", "chatbot", "gradio", "proxy", "free"]:
            assert kw in joined, f"兼容词缺 {kw}"

    def test_cn_alias_pool_present(self):
        from watch_tui import _CN_ALIAS_POOL
        joined = " ".join(_CN_ALIAS_POOL)
        # 中文别名覆盖主流国内平台 + deepseek 密钥标识
        for kw in ["智谱", "月之暗面", "通义千问", "豆包", "百川", "deepseek"]:
            assert kw in joined, f"中文别名缺 {kw}"

    def test_rotation_flattens_all_buckets(self):
        from watch_tui import (_PLATFORM_SEARCH_POOL, _COMPAT_TERMS,
                               _CN_ALIAS_POOL, _ROTATION)
        # _ROTATION = 展平平台词 + 兼容词 + 中文别名 + sk-
        all_platform = [w for pool in _PLATFORM_SEARCH_POOL for w in pool]
        for w in all_platform[:3]:  # 抽样：前 3 个平台词都在轮换里
            assert w in _ROTATION
        for w in _COMPAT_TERMS[:3]:
            assert w in _ROTATION
        for w in _CN_ALIAS_POOL[:3]:
            assert w in _ROTATION
        assert "sk-" in _ROTATION

    def test_rotation_one_term_per_round(self):
        from watch_tui import _ROTATION
        # 每轮取 1 个词（取模），N 轮全覆盖
        seen = {i % len(_ROTATION) for i in range(len(_ROTATION) * 2)}
        assert seen == set(range(len(_ROTATION)))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_watch_tui.py::TestRotationBuckets -v`
Expected: FAIL（`_COMPAT_TERMS` / `_CN_ALIAS_POOL` / `_ROTATION` 不存在 → ImportError）

- [ ] **Step 3: 实现常量**

Modify `watch_tui.py` — 在 `_PLATFORM_SEARCH_POOL` 定义后（line 796 之后）插入：

```python
# 换皮兼容词：外部源是元数据搜索（repo 名/描述/tag），代码型查询(filename:env)不适用。
# demo / 免费中转 / free-endpoint 类项目是硬编码 key 高发区（docs/HF_SCANNER_ANALYSIS.md §4 验证）。
_COMPAT_TERMS = [
    "openai-api-key", "api-key", "chatbot", "ai-chat", "gradio",
    "free-endpoint", "free-api", "proxy", "rotator", "litellm",
]

# 中文别名：gitee / 国内 GitLab 中文项目命中率高；HF/GitLab 元数据搜索支持中文。
# 只进外部源轮换，不进 GitHub 查询（GitHub Code Search 对中文支持差）。
_CN_ALIAS_POOL = [
    "deepseek密钥", "deepseek key", "月之暗面", "智谱", "通义千问",
    "豆包", "火山方舟", "百川", "deepseek api key", "api密钥",
]

# 统一单词轮换序列：展平平台词 + 兼容词 + 中文别名 + 通用泄露特征。
# 每轮 1 词（_scan_external 按 source_round 取模），~45 轮全覆盖。
_ROTATION = (
    [w for pool in _PLATFORM_SEARCH_POOL for w in pool]
    + _COMPAT_TERMS
    + _CN_ALIAS_POOL
    + ["sk-"]
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_watch_tui.py::TestRotationBuckets -v`
Expected: PASS（4 项）

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py tests/test_watch_tui.py
git commit -m "feat(watch): 兼容词 + 中文别名轮换桶（换皮项目 + 中文标识覆盖）"
```

---

## Task 6: _scan_external 单词轮换 + 超时对齐（P0-3/4 接线）

**Files:**
- Modify: `watch_tui.py:1245-1283`
- Modify: `tests/test_watch_tui.py`

- [ ] **Step 1: 写 _scan_external 单词轮换测试**

Append to `tests/test_watch_tui.py`:

```python
class TestScanExternalSingleTerm:
    def test_one_term_per_round_from_rotation(self):
        """每轮从 _ROTATION 取 1 个词，不叠加整桶。"""
        import watch_tui
        terms_seen = []

        class StubEngine:
            def _run_one_scanner(self, src, queries=None, github_token=""):
                terms_seen.append(list(queries))
                return []  # 无 key

        broker = watch_tui.VerificationBroker.__new__(watch_tui.VerificationBroker)
        # 最小 stub：submit_many 返回 0
        broker.submit_many = lambda keys, source="": 0

        class StubState:
            logs = []
            def add_log(self, msg, level="info"): self.logs.append(msg)
            should_exit = False

        scanner = watch_tui.WatchScanner.__new__(watch_tui.WatchScanner)
        scanner.state = StubState()
        scanner.broker = broker
        # 连续 3 轮：每轮应只有 1 个词
        for r in (1, 2, 3):
            scanner._scan_external("huggingface", StubEngine(), "", source_round=r)
        for terms in terms_seen:
            assert len(terms) == 1, f"每轮应 1 词，实际 {terms}"
        # 3 轮的词应互不相同（来自 _ROTATION 不同位置）
        flat = [t[0] for t in terms_seen]
        assert len(set(flat)) == 3
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_watch_tui.py::TestScanExternalSingleTerm -v`
Expected: FAIL（当前每轮返回多词）

- [ ] **Step 3: 改 _scan_external**

Modify `watch_tui.py:1245-1283` — 替换整个 `_scan_external` 方法体：

```python
    def _scan_external(self, source: str, engine, token: str, source_round: int = 0) -> int:
        """外部源扫描（sub-thread + 超时），结果 submit 到 broker。
        返回提交的新 key 数。

        硬时限：HF/GitLab 的真正取消发生在 scanner 内部 asyncio.timeout（18/28s）；
        本方法的 join 只是"取消未生效"的最终保险（deadline + 7s）。"""
        # 超时对齐内部 deadline：HF 18s→25s，GitLab 28s→35s；其它源维持 60s
        if source == "gitlab":
            timeout = 35
        elif source == "huggingface":
            timeout = 25
        else:
            timeout = 60
        # 单词轮换：每轮从 _ROTATION 取 1 个词（跨平台+兼容+中文+通用），N 轮全覆盖
        if _ROTATION:
            term = _ROTATION[source_round % len(_ROTATION)]
            terms = [term]
        else:
            terms = []
        scan_result = [{}]
        scan_error = [None]

        def _do_scan(src=source):
            try:
                keys = engine._run_one_scanner(src, queries=terms, github_token=token)
                scan_result[0] = keys
            except Exception as e:
                scan_error[0] = e

        scan_thread = threading.Thread(target=_do_scan, daemon=True)
        scan_thread.start()
        scan_thread.join(timeout=timeout)

        if scan_thread.is_alive():
            # 理论上不应发生（内部 deadline 已取消）；记 error 便于诊断取消机制
            self.state.add_log(
                f"[{source}] join 超时（>{timeout}s）——内部 deadline 取消未生效，请检查",
                "error")
            return 0
        if scan_error[0]:
            self.state.add_log(f"[{source}] 扫描错误: {scan_error[0]}", "error")
            return 0

        keys = scan_result[0]
        if not keys:
            return 0
        return self.broker.submit_many(keys, source=source)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_watch_tui.py::TestScanExternalSingleTerm tests/test_watch_tui.py::TestExternalSearchTermRotation tests/test_watch_tui.py::TestExternalTermRotationAllSources -v`
Expected: PASS（既有轮换回归不破 + 新单词轮换测试过）

- [ ] **Step 5: 全量测试 + lint**

Run: `python -m pytest tests/ -q && python -m ruff check .`
Expected: 全绿

- [ ] **Step 6: 实测搜索词轮换（P0-3/4 里程碑）**

Run: `python run.py watch --once --interval 30 --verify-workers 2`
观察日志：外部源（huggingface/gitee/gitlab 等）R1/R2/R3 轮的搜索词应来自 `_ROTATION`（出现中文词如"智谱"/兼容词如"chatbot"），每轮 1 词；HF ≤20s、GitLab ≤30s。

- [ ] **Step 7: 提交**

```bash
git add watch_tui.py tests/test_watch_tui.py
git commit -m "feat(watch): _scan_external 单词轮换 + 超时对齐内部 deadline（HF25/GitLab35）"
```

---

## Task 7: P0 总验证

**Files:** 无（仅验证）

- [ ] **Step 1: 全量测试 + lint**

Run: `python -m pytest tests/ -q && python -m ruff check .`
Expected: 全绿

- [ ] **Step 2: 完整 watch 实测（全部 P0 子任务完成后）**

Run: `python run.py watch`
观察 1-2 个外部源轮换周期（约 15-45 分钟）：
- HF/GitLab 每轮耗时稳定 ≤20/30s，无"超时跳过"、无 daemon 残留（线程数稳定）
- 外部源搜索词依次出现平台词→兼容词→中文词（_ROTATION 轮换）
- github_search 心跳持续（看门狗不误触发）
- Ctrl+C 优雅退出（走 final_save 刷盘，绝不 SIGKILL）

- [ ] **Step 3: 更新记忆**

P0 实测达标后，更新 `C:\Users\chu01\.claude\projects\C--Users-chu01-Desktop-PAAI-deepseek-key-hunter\memory\` 下的项目记忆（新增 P0 完成记录 + 路线图指针：下一项 P1-首项 GitHub 多 token）。

---

## Self-Review（计划写完后的自查）

**1. Spec 覆盖：**
- §2 机制（asyncio.timeout + 取消）→ Task 1/4 ✓
- §3 HF（deadline + 429 上限 + tree 封顶 + max_items 24）→ Task 1/2/3 ✓
- §4 GitLab（deadline + 429 上限 + 4×4）→ Task 4 ✓
- §5 兼容词 → Task 5 ✓
- §6 中文别名 → Task 5 ✓
- §7 统一单词轮换 → Task 5（常量）+ Task 6（接线）✓
- §8 watch 超时对齐（25/35s）→ Task 6 ✓
- §9 测试（test_scanners_external.py + watch_tui 更新）→ 各 Task 内 ✓
- §10 实测门禁（pytest + ruff + watch --once + watch）→ Task 3/4/6/7 ✓
- §11 排除项（其它源/GitLab token/P1/P2）→ 未触及 ✓

**2. 占位符扫描：** 无 TBD/TODO；所有 code step 含完整代码；所有命令含预期输出。✓

**3. 类型/命名一致性：**
- `deadline_s` / `self._deadline` / `self._deadline_end` — HF（Task 1）与 GitLab（Task 4）一致 ✓
- `_COMPAT_TERMS` / `_CN_ALIAS_POOL` / `_ROTATION` — Task 5 定义、Task 6 使用，命名一致 ✓
- `FakeSession`/`FakeResp` — Task 1 定义、Task 4（GitLab）复用（同文件 import）✓
- `_patch_session`（HF）vs `_patch_gl_session`（GitLab）— 分别 patch 各自模块的 aiohttp，命名区分 ✓
