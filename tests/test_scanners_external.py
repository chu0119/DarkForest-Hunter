"""外部源 scanner（HF / GitLab）异步路径单测。

零新依赖：用 asyncio.run + 自定义 FakeSession（仿 tests/test_verifier.py 的 FakeResponse），
不引 pytest-asyncio。测试不触网、不读真实 key。
"""
import asyncio
import time

import scanners.gitlab as gl_mod
import scanners.huggingface as hf_mod
from scanners.gitlab import GitLabScanner
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
            return FakeResp(200, json_data=[], delay=5.0)

        _patch_session(monkeypatch, slow_responder)
        s = HuggingFaceScanner(deadline_s=0.2, max_items=4)
        t0 = time.monotonic()
        results = asyncio.run(s.search("deepseek"))
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"deadline 取消未生效，耗时 {elapsed:.1f}s"
        assert isinstance(results, list)

    def test_deadline_keeps_partial_results(self, monkeypatch):
        """deadline 触发时，deadline 前已完成扫描的 key 必须保留在结果里。"""
        def responder(url):
            # model 搜索返回 2 个 repo：fast 立即命中 key，slow 卡在 tree
            if "/api/models" in url and "/tree/" not in url and "/resolve/" not in url:
                return FakeResp(200, json_data=[{"id": "fast/repo"}, {"id": "slow/repo"}])
            if ("/api/datasets" in url or "/api/spaces" in url) and "/tree/" not in url and "/resolve/" not in url:
                return FakeResp(200, json_data=[])
            # fast/repo：tree + resolve/raw 都立即返回（含 key）
            if "fast/repo" in url and "/tree/" in url:
                return FakeResp(200, json_data=[{"path": "app.py", "type": "file"}])
            if "fast/repo" in url and ("/resolve/" in url or "/raw/" in url):
                return FakeResp(200, text_data="KEY=sk-ff39e6d2c73c48e3" "bc0e2ebb05e5d04f")
            # slow/repo：tree 卡 10s（deadline 会在期间触发并取消它）
            if "slow/repo" in url and "/tree/" in url:
                return FakeResp(200, json_data=[], delay=10.0)
            return FakeResp(200, json_data=[])

        _patch_session(monkeypatch, responder)
        s = HuggingFaceScanner(deadline_s=1.0, max_items=8)
        t0 = time.monotonic()
        results = asyncio.run(s.search("deepseek"))
        elapsed = time.monotonic() - t0
        keys = [r["key"] for r in results]
        assert "sk-ff39e6d2c73c48e3" "bc0e2ebb05e5d04f" in keys, \
            f"deadline 前完成的 key 未保留: {keys}"
        assert elapsed < 4.0, f"取消未生效，耗时 {elapsed:.1f}s"


class TestHuggingFaceRateLimit:
    def test_429_retries_at_most_once_then_breaks(self, monkeypatch):
        """429 连续返回：最多重试 1 次后 break，不无限 sleep/continue。"""
        calls = {"n": 0}

        def responder(url):
            if "/api/" in url and "/tree/" not in url and "/resolve/" not in url:
                calls["n"] += 1
                return FakeResp(429, headers={"Retry-After": "1"})
            return FakeResp(200, json_data=[])

        _patch_session(monkeypatch, responder)
        s = HuggingFaceScanner(deadline_s=30, max_items=4)
        asyncio.run(s.search("deepseek"))
        # 3 路并发搜索（model/dataset/space），每路最多 1 次重试 = 首次 + 1 重试 = 2 次/路
        # 全部加起来不超过 ~8 次（3 路 × 2 + 余量），远小于"无限循环"
        assert calls["n"] <= 8, f"429 未封顶，调用 {calls['n']} 次"


class TestHuggingFaceTreeFileCap:
    def test_tree_matched_files_capped(self, monkeypatch):
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
                return FakeResp(200, text_data="sk-ff39e6d2c73c48e3" "bc0e2ebb05e5d04f")
            return FakeResp(200, json_data=[])

        _patch_session(monkeypatch, responder)
        s = HuggingFaceScanner(deadline_s=30, max_items=4)
        asyncio.run(s.search("deepseek"))
        # 单个 model 仓库：3 入口文件(README/config.json/.env) + tree 命中封顶 3 = 最多 6
        # 旧代码（tree 无封顶）会下 3+5=8 → <=6 能抓到回退
        assert len(downloaded) <= 6, f"tree 文件未封顶，下载 {len(downloaded)} 个"


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
                return FakeResp(429)
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


# ── github_events(EventsMonitor)deadline + 提取 ──────────────────────

class TestEventsMonitorDeadline:
    def test_search_returns_within_deadline(self, monkeypatch):
        """deadline 到期必须返回(不无限轮询)。"""
        import asyncio
        import time as _t

        import scanners.github_events as ge_mod
        from scanners.github_events import EventsMonitor

        def responder(url):
            return FakeResp(200, json_data=[])  # 空事件 → 不下载任何文件

        monkeypatch.setattr(ge_mod.aiohttp, "ClientSession",
                            lambda **kw: FakeSession(responder))
        m = EventsMonitor(token="", poll_interval=0.01, deadline_s=0.15,
                          max_events_per_poll=5)
        t0 = _t.time()
        asyncio.run(m.search())
        elapsed = _t.time() - t0
        assert elapsed < 1.0, f"deadline 必须在 ~0.15s 内返回,实际 {elapsed:.2f}s"

    def test_extracts_keys_from_push_event(self, monkeypatch):
        """PushEvent 里的 added/modified 文件被下载并提取 key。"""
        import asyncio

        import scanners.github_events as ge_mod
        from scanners.github_events import EventsMonitor

        push = [{
            "type": "PushEvent", "public": True,
            "repo": {"name": "o/r"},
            "payload": {"commits": [{"sha": "s1", "added": ["f.env"], "modified": []}]},
        }]

        def responder(url):
            if "/events" in url:
                return FakeResp(200, json_data=push)
            # raw 文件内容
            return FakeResp(200, text_data='DEEPSEEK_API_KEY = "sk-1e175253812a4948' '86dd8952b56dc19c"')

        monkeypatch.setattr(ge_mod.aiohttp, "ClientSession",
                            lambda **kw: FakeSession(responder))
        m = EventsMonitor(token="t", poll_interval=0.01, deadline_s=0.5,
                          max_events_per_poll=5)
        asyncio.run(m.search())
        assert m.results, "应从 PushEvent 提取到 key"
        assert m.results[0]["key"].startswith("sk-")


# ── v2.5.4: gist 分页终止必须看原始页长 ──

class TestV254GistPaginationRawCount:
    def test_pagination_continues_when_filter_thins_page(self, monkeypatch):
        """筛选后剩 <100 条不得提前停翻页(旧实现用过滤后长度判断,
        开筛选时第一页后必停)。"""
        from scanners.github_gist import GistScanner

        fetched = []
        pages = {
            1: [{"id": f"g{i}", "description": "misc",
                 "files": {"a.txt": {}}, "owner": {"login": "u"}}
                for i in range(100)],
            2: [{"id": f"h{i}", "description": "misc",
                 "files": {"a.txt": {}}, "owner": {"login": "u"}}
                for i in range(100)],
        }

        async def fake_fetch(session, page):
            fetched.append(page)
            return pages.get(page, [])

        async def fake_scan(session, sem, g):
            return None

        s = GistScanner(max_pages=5)
        monkeypatch.setattr(s, "_fetch_page", fake_fetch)
        monkeypatch.setattr(s, "_scan_gist", fake_scan)
        monkeypatch.setattr("scanners.github_gist.aiohttp.ClientSession",
                            lambda **kw: FakeSession(lambda url: FakeResp(200)))

        asyncio.run(s.search("deepseek"))
        assert 2 in fetched, f"筛选变稀后必须继续翻页,实际翻了 {fetched}"
        assert len(fetched) <= 3, f"翻完原始页后应停止,实际翻了 {fetched}"
