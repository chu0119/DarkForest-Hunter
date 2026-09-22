"""_gh_search 重试/放弃策略测试（离线 mock，零真实网络）。

回归保护：401/403-forbidden 必须立即放弃（旧逻辑会重试 3 次、每查询浪费 ~15s）。
"""
import scanner_engine
from scanner_engine import ScannerEngine


class FakeResp:
    def __init__(self, status, headers=None, data=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self._data = data
        self.text = text  # needed by self-quiet secondary rate limit check

    def json(self):
        return self._data or {}


def _engine():
    e = ScannerEngine(log_callback=lambda m, l="info": None)
    e._gh_pacing_interval = {}  # dict (per-token), default 0 used by .get()
    # 跳过 SmartProxy：初始化时 _test_direct() 会调 requests.get 干扰 mock 序列
    e._smart_proxy._direct_ok = True
    e._smart_proxy._last_test = 9999999999.0
    e._gh_pacing_calls.clear()  # 清空 pacing 状态避免干扰
    return e


class TestGhSearchRetryPolicy:
    def test_401_returns_immediately_no_retry(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: FakeResp(401))
        assert _engine()._gh_search("q", token="tok") == []
        assert sleeps == []  # 401 不应触发任何退避

    def test_403_forbidden_no_ratelimit_returns_immediately(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: sleeps.append(s))
        # 403 但无 Retry-After、配额未耗尽 → 非限流，立即放弃
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: FakeResp(403, headers={}))
        assert _engine()._gh_search("q", token="tok") == []
        assert sleeps == []

    def test_403_ratelimit_retries_then_succeeds(self, monkeypatch):
        e = _engine()  # 先创建引擎（SmartProxy 探测在 monkeypatch 之前）
        sleeps = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: sleeps.append(s))
        seq = [
            FakeResp(429, headers={"Retry-After": "1"}),
            FakeResp(200, headers={"X-RateLimit-Remaining": "9"}, data={"items": [{"a": 1}]}),
        ]
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: seq.pop(0))
        assert e._gh_search("q", token="tok") == [{"a": 1}]
        assert len(sleeps) == 1  # 退避一次后重试成功

    def test_500_retries_then_gives_up(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: FakeResp(500))
        assert _engine()._gh_search("q", token="tok") == []
        assert len(sleeps) == 2  # 3 次尝试 → 2 次退避

    def test_long_penalty_bail_out_no_dead_wait(self, monkeypatch):
        """惩罚期限流（Retry-After>90s，多实例竞争触发）→ 立即放弃本轮，不死等。

        回归：曾死等 736s 卡死整条 github_search 线程。"""
        sleeps = []
        logs = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(scanner_engine.requests, "get",
                            lambda *a, **k: FakeResp(429, headers={"Retry-After": "736"}))
        e = ScannerEngine(log_callback=lambda m, l="info": logs.append((l, m)))
        e._gh_pacing_interval = {}
        assert e._gh_search("q", token="tok") == []
        assert sleeps == []  # 不 sleep 736s
        assert any("惩罚期" in m or "次级限流" in m for _, m in logs)


class TestRateLimitPreCheckNoise:
    """pre-check 在 remaining≤1 时等待，但正常短等待不应刷屏告警。"""

    def _engine_with_logs(self, logs):
        e = ScannerEngine(log_callback=lambda m, l="info": logs.append((l, m)))
        e._gh_pacing_interval = {}
        return e

    def test_short_end_of_window_wait_is_silent(self, monkeypatch):
        logs = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: None)
        now = int(scanner_engine.time.time())
        resp = FakeResp(200, headers={"X-RateLimit-Remaining": "1",
                                      "X-RateLimit-Reset": str(now + 6)},
                        data={"items": []})
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: resp)
        self._engine_with_logs(logs)._gh_search("q", token="tok")
        # 正常窗口轮转(等待<20s)不应有"配额/限流"告警
        assert not any(("配额" in m or "限流" in m) for _, m in logs)

    def test_long_wait_still_warns(self, monkeypatch):
        logs = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: None)
        now = int(scanner_engine.time.time())
        resp = FakeResp(200, headers={"X-RateLimit-Remaining": "1",
                                      "X-RateLimit-Reset": str(now + 60)},
                        data={"items": []})
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: resp)
        self._engine_with_logs(logs)._gh_search("q", token="tok")
        # 真实长等待(≥40s，配额被外部占用/多实例竞争)仍应告警——
        # v2.4.9.1: 20-39s 的正常窗口轮转已降级(不再告警),阈值从 20 提到 40
        assert any(l == "warning" and "配额" in m for l, m in logs)

    def test_window_rotation_wait_is_demoted(self, monkeypatch):
        """v2.4.9.1: 31s 的正常窗口轮转(3h 实测 60+ 条刷屏)不再告警。"""
        logs = []
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: None)
        now = int(scanner_engine.time.time())
        resp = FakeResp(200, headers={"X-RateLimit-Remaining": "1",
                                      "X-RateLimit-Reset": str(now + 31)},
                        data={"items": []})
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: resp)
        self._engine_with_logs(logs)._gh_search("q", token="tok")
        assert not any(l == "warning" and "配额" in m for l, m in logs)


class TestFallbackDownloadCap:
    """回退下载上限：全量下载会卡死 github 线程（实测 20 分钟空白）。"""

    def test_fallback_caps_items_at_30(self, monkeypatch):
        logs = []
        e = ScannerEngine(log_callback=lambda m, l="info": logs.append((l, m)))
        e._gh_pacing_interval = {}
        e._authed = True
        e.search_delay = 0

        big_items = [{"repository": {"full_name": f"r{i}"}, "path": f"p{i}.py",
                      "html_url": f"https://gh/r{i}/blob/main/p{i}.py"} for i in range(80)]
        called = {}

        def fake_search(query, per_page=100, page=1, with_text_matches=True, token=None):
            return big_items

        def fake_extract(items, on_key=None):
            called["n"] = len(items)
            return {}

        monkeypatch.setattr(e, "_gh_search", fake_search)
        monkeypatch.setattr(
            e, "_extract_keys_from_text_matches",
            lambda items, on_key=None, query=None: {})
        monkeypatch.setattr(
            e, "_scan_one_query_threaded",
            lambda items, on_key=None, query=None: fake_extract(items))
        e._scan_one_query("q", max_pages=1)
        assert called["n"] <= 30, f"回退下载应限 30 个，实际 {called['n']}"


class TestAdaptivePacing:
    """自适应限速：429 后放大间隔防持续撞窗口，长时间无 429 回降。"""

    def _engine(self):
        e = _engine()
        e._gh_pacing_interval = {'test_tok': 6.0}
        return e

    def test_429_widens_interval(self, monkeypatch):
        e = self._engine()
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: None)
        now = [1000.0]
        monkeypatch.setattr(scanner_engine.time, "time", lambda: now[0])
        monkeypatch.setattr(scanner_engine.requests, "get",
                            lambda *a, **k: FakeResp(429, headers={"Retry-After": "3"}))
        e._gh_search("q", token="tok")
        # 429 已记录时刻(v2.4.8: pacing key = __ip_shared__,所有 token 共桶)
        assert e._gh_last_429.get("__ip_shared__") == 1000.0
        # 下一次调用时读到 429 记录 → 间隔放大（自适应生效）
        e._gh_search("q", token="tok")
        assert e._gh_pacing_interval.get('__ip_shared__', 0) > 7.0

    def test_no_429_returns_to_base(self, monkeypatch):
        e = self._engine()
        e._gh_last_429["__ip_shared__"] = 500.0  # 5 分钟前的 429
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: None)
        monkeypatch.setattr(scanner_engine.requests, "get",
                            lambda *a, **k: FakeResp(200, data={"items": []}))
        e._gh_search("q", token="tok")
        # v2.5: IP 层基线统一 6.0s/出口IP(滥用阈值一半);
        # per-token 节奏由配额层(header-driven ≈6s/token)原生负责
        assert e._gh_pacing_interval.get("__ip_shared__", 0) == 6.0

    def test_recent_429_keeps_widened(self, monkeypatch):
        e = self._engine()
        e._gh_last_429["__ip_shared__"] = 900.0  # 100s 前的 429（窗口内）
        monkeypatch.setattr(scanner_engine.time, "sleep", lambda s: None)
        now = [1000.0]
        monkeypatch.setattr(scanner_engine.time, "time", lambda: now[0])
        monkeypatch.setattr(scanner_engine.requests, "get",
                            lambda *a, **k: FakeResp(200, data={"items": []}))
        e._gh_search("q", token="tok")
        assert e._gh_pacing_interval.get('__ip_shared__', 0) == 15.0  # 仍在降档


# ── Fresh Repo 扫描：最近推送的项目 ─────────────────────────────────

class TestFreshRepoScan:
    """Repo Search(pushed:>DATE) → 分组 Code Search(repo:) 精扫。

    Code Search 不支持日期过滤（pushed:/created: 实测 0 结果），
    但 Repo Search 支持 pushed:>DATE（独立配额）——用 repo: 限定符补上
    "扫最近推送项目"的能力。
    """

    def _engine(self):
        e = _engine()
        e._stop_requested = False
        return e

    def test_repo_search_success(self, monkeypatch):
        e = self._engine()
        items = [{"full_name": "acme/new-repo", "pushed_at": "2026-08-10T00:00:00Z"}]
        monkeypatch.setattr(scanner_engine.requests, "get",
                            lambda *a, **k: FakeResp(200, data={"items": items}))
        repos = e._gh_repo_search("deepseek pushed:>2026-08-01", token="tok")
        assert repos == items

    def test_repo_search_401_returns_empty(self, monkeypatch):
        e = self._engine()
        monkeypatch.setattr(scanner_engine.requests, "get", lambda *a, **k: FakeResp(401))
        assert e._gh_repo_search("q", token="tok") == []

    def test_repo_search_429_no_error(self, monkeypatch):
        e = self._engine()
        monkeypatch.setattr(scanner_engine.requests, "get",
                            lambda *a, **k: FakeResp(429, headers={"Retry-After": "60"}))
        # 限流时静默跳过，不抛异常
        assert e._gh_repo_search("q", token="tok") == []

    def test_scan_fresh_repos_flow(self, monkeypatch):
        """完整流程：Repo Search 返回 repo → 分组精扫 → 返回 key 列表。"""
        e = self._engine()
        repos = [{"full_name": f"acme/r{i}"} for i in range(4)]
        monkeypatch.setattr(e, "_gh_repo_search", lambda q, per_page=30, token=None: repos)

        scanned = []

        def fake_gh_search(query, per_page=100, page=1, with_text_matches=False, token=None,
                           skip_wait=False):
            scanned.append(query)
            assert skip_wait is True  # fresh-repo 精扫必须 skip_wait（不阻塞主轮次）
            # 第一组返回 1 个 item（含 text_matches key），第二组返回空
            if "acme/r0" in query:
                return [{
                    "repository": {"full_name": "acme/r0"},
                    "path": ".env",
                    "html_url": "https://gh/acme/r0/blob/main/.env",
                    "text_matches": [{"fragment": "sk-Fr3shK3yQw9Zx8Vc2Mn5Lp7Tr4Yb6Ua1Sd3"}],
                }]
            return []

        monkeypatch.setattr(e, "_gh_search", fake_gh_search)
        keys = e.scan_fresh_repos(token="tok", days=7)
        # 分组：4 个 repo → 2 组（每组 3 个封顶）
        assert len(scanned) == 2
        assert all("repo:" in q for q in scanned)
        assert keys == ["sk-Fr3shK3yQw9Zx8Vc2Mn5Lp7Tr4Yb6Ua1Sd3"]

    def test_scan_fresh_repos_empty(self, monkeypatch):
        e = self._engine()
        monkeypatch.setattr(e, "_gh_repo_search", lambda q, per_page=30, token=None: [])
        assert e.scan_fresh_repos(token="tok", days=7) == []

    def test_fresh_sliding_window(self, monkeypatch):
        """滚动窗口：每轮都查「最近 lookback 天」,窗口不推进(旧推进式窗口
        推到昨天就永久卡死,新推送 repo 再也扫不到)。"""
        e = self._engine()
        repos = [{"full_name": "acme/r"}]
        queries = []
        def fake_repo_search(q, per_page=30, token=None):
            queries.append(q)
            return repos
        monkeypatch.setattr(e, "_gh_repo_search", fake_repo_search)
        monkeypatch.setattr(e, "_gh_search", lambda *a, **k: [])
        # 冻结时间
        import datetime
        frozen = datetime.datetime(2026, 8, 10, 12, 0, 0).timestamp()
        monkeypatch.setattr(scanner_engine.time, "time", lambda: frozen)

        e.scan_fresh_repos(token="tok", days=14)
        assert len(queries) == 1
        q1 = queries[0]
        assert "pushed:>2026-07-27" in q1  # now(08-10) - 14 天 = 07-27
        # 第二轮:相同滚动窗口(now-14d),关键词轮换继续
        e.scan_fresh_repos(token="tok", days=14)
        assert len(queries) == 2
        assert "pushed:>2026-07-27" in queries[1]  # 窗口不推进


# ── v2.4.1 新鲜簇接入第二批平台 ──────────────────────────────────────

class TestNewPlatformFreshCluster:
    def test_fresh_queries_cover_second_batch(self):
        """8 轮新鲜簇模式应覆盖全部第二批平台(每轮 2 条轮换)。"""
        from query_rotation import generate_fresh_queries
        seen = set()
        for pattern in range(8):
            seen.update(generate_fresh_queries(pattern))
        for marker in ("sk-proj-", "GEMINI_API_KEY", "nvapi-", "bce-v3",
                       "XAI_API_KEY", "hunyuan.cloud", "MODELSCOPE", "longcat"):
            assert any(marker in q for q in seen), f"新鲜簇缺 {marker}"

    def test_platform_query_recognizes_second_batch(self):
        """新平台查询要被 _is_platform_query 认出(否则拿不到首轮优先)。"""
        from watch_tui import _is_platform_query
        for q in ("api.openai.com sk-proj-", "qianfan.baidubce.com bce-v3",
                  "nvapi- filename:env", "api.hunyuan.cloud.tencent.com sk-"):
            assert _is_platform_query(q), f"{q} 未被识别为平台查询"
