"""scanner_engine.py 的查询构建、货币转换、过滤逻辑测试。"""

import os
import time
from types import SimpleNamespace

import pytest

import scanner_engine as scanner_engine_module
from scanner_engine import (
    ScannerEngine,
    build_active_queries,
    convert_to_cny,
    convert_to_usd,
    generate_rolling_time_queries,
    load_tiered_queries,
)

# ── 货币转换 ────────────────────────────────────────────────────────

class TestCurrencyConversion:
    def test_cny_to_usd(self):
        assert convert_to_usd(100, "CNY", 7.25) == 100 / 7.25

    def test_usd_to_usd_unchanged(self):
        assert convert_to_usd(42.5, "USD") == 42.5

    def test_unknown_currency_passthrough(self):
        assert convert_to_usd(10, "EUR") == 10

    def test_zero_rate_guard(self):
        assert convert_to_usd(100, "CNY", rate=0) == 0

    def test_usd_to_cny(self):
        assert convert_to_cny(10, "USD", 7.25) == 72.5

    def test_cny_to_cny_unchanged(self):
        assert convert_to_cny(100, "CNY") == 100

    def test_case_insensitive(self):
        assert convert_to_usd(100, "cny") == 100 / 7.25


# ── 动态滚动时间窗口查询 ───────────────────────────────────────────

class TestRollingQueries:
    def test_generates_high_yield_filetypes(self):
        q = generate_rolling_time_queries()
        assert q[0] == "deepseek sk-"
        assert "deepseek sk- filename:env" in q
        assert "deepseek sk- filename:java" in q
        assert "DEEPSEEK_API_KEY sk-" in q

    def test_dedup(self):
        q = generate_rolling_time_queries()
        assert len(q) == len(set(q))

    def test_reasonable_size(self):
        q = generate_rolling_time_queries()
        assert 20 <= len(q) <= 50

    def test_no_pushed_queries(self):
        # generate_rolling_time_queries 本身不生成 pushed:（由 build_active_queries 动态注入）
        q = generate_rolling_time_queries()
        assert all("pushed:" not in x for x in q)


# ── build_active_queries ────────────────────────────────────────────

class TestBuildActiveQueries:
    def test_filters_expired_hardcoded_dates(self):
        q = build_active_queries()
        # 旧的硬编码日期（queries_optimized.txt 中的）被过滤
        # 动态生成的 pushed:> 查询使用当前日期（7天/30天/90天前）
        for query in q:
            # 不应包含旧的硬编码日期
            assert "pushed:>2026-05-02" not in query
            assert "pushed:>2026-07-01" not in query
            assert "pushed:>2026-07-24" not in query

    def test_keeps_wide_backfill_window(self):
        q = build_active_queries()
        # 动态注入了 pushed:> 查询（7天/30天/90天时间窗口）
        pushed_queries = [x for x in q if "pushed:>" in x]
        assert len(pushed_queries) >= 5  # 至少5条动态时间窗口查询

    def test_appends_rolling_queries(self):
        q = build_active_queries()
        assert any(x == "deepseek sk- filename:env" for x in q)

    def test_no_duplicates(self):
        q = build_active_queries()
        assert len(q) == len(set(q))

    def test_large_corpus(self):
        # 优化后从 200+ 精简到 ~120（精选高收益查询 + 高产出文件类型）
        assert len(build_active_queries()) >= 100

    def test_custom_base(self):
        base = ["custom query 1", "deepseek sk- pushed:>2026-05-01"]
        q = build_active_queries(base)
        assert "custom query 1" in q
        assert not any("pushed:>2026-05-01" in x for x in q)


# ── load_tiered_queries ─────────────────────────────────────────────

class TestLoadTieredQueries:
    def test_loads_default_file(self):
        q = load_tiered_queries()
        assert len(q) > 100  # queries_optimized.txt 117 条
        assert all({"tier", "query", "pages"} <= set(item) for item in q)

    def test_tiered_format(self, tmp_path):
        f = tmp_path / "q.txt"
        f.write_text(
            "# comment line\n"
            "1|deepseek sk- filename:java\n"
            "7|deepseek sk- filename:env pushed:>2026-07-01\n",
            encoding="utf-8",
        )
        q = load_tiered_queries(str(f))
        assert len(q) == 2
        assert q[0]["tier"] == 1 and q[0]["pages"] == 5
        assert q[1]["tier"] == 7 and q[1]["pages"] == 3

    def test_plain_query_lines_get_default_tier(self, tmp_path):
        f = tmp_path / "q.txt"
        f.write_text("deepseek sk- filename:env\n", encoding="utf-8")
        q = load_tiered_queries(str(f))
        assert q[0]["tier"] == 5 and q[0]["pages"] == 5
        assert q[0]["query"] == "deepseek sk- filename:env"

    def test_missing_file_returns_empty(self):
        assert load_tiered_queries("no_such_file_xyz.txt") == []


# ── 低价值文件预过滤 ────────────────────────────────────────────────

class TestLikelyTestKey:
    def setup_method(self):
        self.engine = ScannerEngine()

    def test_build_artifacts_skipped(self):
        assert self.engine._is_likely_test_key("/target/site/index.html", "a/b")
        assert self.engine._is_likely_test_key("/target/classes/Foo.class", "a/b")
        assert self.engine._is_likely_test_key("/build/resources/main/x.properties", "a/b")

    def test_known_noise_files_skipped(self):
        assert self.engine._is_likely_test_key("testdeepseek/x.py", "a/b")
        assert self.engine._is_likely_test_key("foo/TongYiChatModelTests.kt", "a/b")

    def test_real_source_files_kept(self):
        assert not self.engine._is_likely_test_key("src/main/java/Foo.java", "a/b")
        assert not self.engine._is_likely_test_key("src/main.py", "foo/bar")
        # demo/example 有意保留（真实 key 常出现在这些文件里）
        assert not self.engine._is_likely_test_key("demo/deepseek_demo.py", "a/b")
        assert not self.engine._is_likely_test_key("examples/config.env", "a/b")


# ── QueryTracker 收益追踪（watch 优先级的核心）─────────────────────

class TestQueryTracker:
    def _tracker(self, tmp_path):
        from scanner_engine import QueryTracker
        return QueryTracker(stats_path=str(tmp_path / "q.json"))

    def test_is_barren_after_zero_yield_runs(self, tmp_path):
        t = self._tracker(tmp_path)
        for _ in range(4):
            t.record("barren_q", 0)
        assert t.is_barren("barren_q")
        assert not t.is_barren("never_run")  # 未知查询不判为 barren

    def test_is_barren_after_recent_zero_runs(self, tmp_path):
        # 回归：累计 hits>0 但近 3 轮 0 产出 → 应判 barren（曾因累计永远不跳过→停滞）
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("scraped", 100)  # 早期高产
        for _ in range(3):
            t.record("scraped", 0)    # 近 3 轮 0 产出
        assert t.is_barren("scraped"), "累计 hits>0 但近 3 轮 0 产出应跳过"

    def test_not_barren_with_recent_hits(self, tmp_path):
        # 近 3 轮有产出 → 不跳过（即使早期有 0）
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("active", 0)
        t.record("active", 5)
        assert not t.is_barren("active")

    def test_not_barren_if_ever_hits(self, tmp_path):
        t = self._tracker(tmp_path)
        for _ in range(5):
            t.record("q", 0)
        t.record("q", 3)  # 命中过一次 → 不再 barren
        assert not t.is_barren("q")

    def test_sort_by_yield_desc(self, tmp_path):
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("low", 0)
        for _ in range(3):
            t.record("high", 10)
        qs = ["low", "high", "unknown"]
        qs.sort(key=t.get_yield, reverse=True)
        assert qs[0] == "high"  # 高收益优先
        assert "unknown" in qs  # 未知(0.5)排在 low(0) 前

    def test_top_queries_only_productive(self, tmp_path):
        # 供查询变异用：只返回 runs≥min_runs 且 hits>0 的查询，按收益降序
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("rich", 10)
        for _ in range(3):
            t.record("poor", 0)  # hits=0 → 排除
        for _ in range(2):
            t.record("few", 5)  # runs=2
        top = t.top_queries(5, min_runs=2)
        assert [q for q, _ in top] == ["rich", "few"]

    def test_top_queries_excludes_recently_barren_high_volume(self, tmp_path):
        """历史候选量大但最近 0 候选的查询，不应继续驱动变异。"""
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("fresh", 8)
        t.record("stale", 52)
        t.record("stale", 0)

        top = dict(t.top_queries(10, min_runs=2))
        assert "stale" not in top
        assert "fresh" in top

    def test_outcome_feedback_downgrades_low_quality_high_volume(self, tmp_path):
        """候选命中多但全零余额的查询，不应压过低量高转化查询。"""
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("volume", 100)
            t.record_outcome("volume", valid=False, high_value=False)
        for _ in range(3):
            t.record("quality", 2)
            t.record_outcome("quality", valid=True, high_value=True)

        assert t.get_quality_yield("quality") > t.get_quality_yield("volume")
        assert t.sort_by_yield(["volume", "quality"])[0] == "quality"

    def test_low_quality_circuit_breaker(self, tmp_path):
        """验证样本足够且高价值/双低时跳过；样本不足或转化尚可时保留。"""
        t = self._tracker(tmp_path)
        for _ in range(10):
            t.record("wasted", 10)
            t.record_outcome("wasted", valid=False, high_value=False)
        for _ in range(3):
            t.record("new", 10)
            t.record_outcome("new", valid=False, high_value=False)
        for _ in range(10):
            t.record("productive", 10)
            t.record_outcome("productive", valid=True, high_value=False)

        assert t.is_low_quality("wasted")
        assert not t.is_low_quality("new")
        assert not t.is_low_quality("productive")
        assert not t.is_low_quality("unknown")

    def test_value_capable_platform_outweighs_unverifiable_valid(self, tmp_path):
        """无余额接口平台的 valid 不能和可判余额平台的 valid 等权。"""
        t = self._tracker(tmp_path)
        for _ in range(5):
            t.record("balance-capable", 10)
            t.record_outcome("balance-capable", valid=True, high_value=False,
                             value_capable=True)
            t.record("no-balance-api", 10)
            t.record_outcome("no-balance-api", valid=True, high_value=False,
                             value_capable=False)

        assert t.get_quality_yield("balance-capable") > \
            t.get_quality_yield("no-balance-api")
        assert t.sort_by_yield(["no-balance-api", "balance-capable"])[0] == \
            "balance-capable"




# ── suggest_pages 深挖建议（回归：曾被 min(default) 钳死）───────────

class TestSuggestPages:
    def _tracker(self, tmp_path):
        from scanner_engine import QueryTracker
        return QueryTracker(stats_path=str(tmp_path / "q.json"))

    def test_high_yield_deep_dig_10_pages(self, tmp_path):
        # 回归：default=1 时高产查询也必须深挖 10 页（曾被 min(default,10) 钳成 1）
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("rich", 30)
        assert t.suggest_pages("rich", default=1) == 10

    def test_medium_yield_5_pages(self, tmp_path):
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("mid", 2)
        assert t.suggest_pages("mid", default=1) == 5

    def test_low_yield_1_page(self, tmp_path):
        t = self._tracker(tmp_path)
        for _ in range(3):
            t.record("poor", 0)
        assert t.suggest_pages("poor", default=1) == 1

    def test_unknown_uses_default(self, tmp_path):
        t = self._tracker(tmp_path)
        assert t.suggest_pages("never_run", default=3) == 3


class TestFreshRepoKeywordRotation:
    """fresh-repo 关键词每轮轮换(修复按日期卡死)测试。"""

    def test_diminishing_does_not_cool_1_key_per_round(self):
        """1 新 key/轮的边缘高产查询不被冷藏(旧阈值 avg_new<2 会误杀)。"""
        from scanner_engine import QueryTracker
        engine = QueryTracker.__new__(QueryTracker)
        engine._cooldown = {}
        engine._cooldown_rounds = 20
        engine._round_counts = {}
        # 5 轮,每轮 1 个新 key、15 个提取——旧逻辑 avg_new=1 < 2 触发冷却
        for i in range(5):
            triggered = engine.diminishing_rounds("q_edge", 1, 15)
            if i < 3:
                assert not triggered
        # 4 轮之后才可能触发;1/轮 不应触发
        assert not engine.diminishing_rounds("q_edge", 1, 15)
        assert "q_edge" not in engine._cooldown

    def test_diminishing_cools_zero_new_rounds(self):
        """完全零新提交(提取>0)的查询仍触发冷却。"""
        from scanner_engine import QueryTracker
        engine = QueryTracker.__new__(QueryTracker)
        engine._cooldown = {}
        engine._cooldown_rounds = 20
        engine._round_counts = {}
        for _i in range(5):
            engine.diminishing_rounds("q_stale", 0, 15)
        assert engine._cooldown.get("q_stale", 0) == 20


    def test_keyword_rotates_each_round(self, monkeypatch):
        """每轮换一个关键词,不按日期卡死(旧 bug: 整天同一个词 0 结果)。"""
        import scanner_engine as _se
        monkeypatch.setattr(_se.time, "sleep", lambda *a: None)  # 3 词尝试间 6.5s 跳过
        from scanner_engine import ScannerEngine
        engine = ScannerEngine.__new__(ScannerEngine)
        engine._fresh_repo_round = {}
        engine._FRESH_REPO_LOOKBACK_DAYS = 7
        engine._FRESH_REPO_PER_TOKEN = 20
        engine._FRESH_REPO_BATCH = 3
        engine.log = lambda *a, **kw: None
        engine._gh_repo_search = lambda *a, **kw: []  # 空结果,不深入
        engine._stop_requested = False
        engine._is_token_healthy = lambda t: True
        engine._route_for_token = lambda t: None

        kw1 = engine._FRESH_REPO_KEYWORDS[
            engine._fresh_repo_round.get("t", 0) % len(engine._FRESH_REPO_KEYWORDS)]
        engine.scan_fresh_repos("t")
        kw2 = engine._FRESH_REPO_KEYWORDS[
            engine._fresh_repo_round.get("t", 0) % len(engine._FRESH_REPO_KEYWORDS)]
        assert kw1 != kw2, "每轮必须换关键词"
        # 跑完一轮所有关键词后循环
        for _ in range(len(engine._FRESH_REPO_KEYWORDS) - 1):
            engine.scan_fresh_repos("t")
        kw_full = engine._FRESH_REPO_KEYWORDS[
            engine._fresh_repo_round.get("t", 0) % len(engine._FRESH_REPO_KEYWORDS)]
        assert kw_full == kw1, "满一轮后回到第一个关键词"


def test_engine_key_pattern_extracts_zhipu_hex_secret():
    """scanner_engine.KEY_PATTERN 必须与 scanners.base 同源,含智谱 hex.secret 格式。

    回归: 旧实现本地复制了一份 KEY_PATTERN,缺 hex.secret → github_search 提不出智谱 key。
    """
    from scanner_engine import KEY_PATTERN
    from scanners.base import KEY_PATTERN as BASE_PATTERN
    # 两份正则必须等价(同一 pattern 字符串)
    assert KEY_PATTERN.pattern == BASE_PATTERN.pattern
    # 智谱真实格式 hex.secret 必须能被提取
    text = 'ZHIPU_API_KEY = "78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp"'
    m = KEY_PATTERN.search(text)
    assert m is not None, "智谱 hex.secret 格式必须能被提取"
    assert "." in m.group() and len(m.group()) >= 49


class TestVerifyDictMultiPlatform:
    def test_kimi_key_routed_to_moonshot_not_deepseek(self, monkeypatch):
        """deepseek 子命令扫到的多平台 key 不应被 deepseek 余额端点误判 invalid。"""
        from scanner_engine import ScannerEngine
        eng = ScannerEngine()
        hits = {"urls": []}
        class R:
            def __init__(self, code=200, data=None, text=""):
                self.status_code = code
                self._d = data or {}
                self.text = text
            def json(self): return self._d
        def fake_get(url, **k):
            hits["urls"].append(url)
            if "moonshot" in url:
                return R(200, {"data": [{"id": "m1"}]})
            return R(401)  # deepseek 端点对 kimi key 应 401
        def fake_post(url, **k):
            hits["urls"].append(url)
            return R(200, {"id": "x"})
        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        # kimi 真实格式 48 位 base62
        kimi_key = "sk-aUbQL11X1SVze8SyD1sdbHyalmeLlJSLXqZL1vn8iumXdqVN"
        results = eng._verify_dict({kimi_key: {
            "key_preview": "sk-aUbQL...", "repos": [{"repo": "o/r", "file": "f.py", "url": "u"}]}})
        assert len(results) == 1
        assert any("moonshot" in u for u in hits["urls"]), \
            f"kimi key 应路由到 moonshot,实际 {hits['urls']}"

    def test_verify_dict_keeps_source_for_yield_attribution(self, monkeypatch):
        """单源扫描结果必须保留 source，否则无法评估各源真实转化率。"""
        import providers
        from scanner_engine import ScannerEngine

        class FakeVerifier:
            def __init__(self, *args, **kwargs):
                pass

            def close(self):
                pass

            def verify_key(self, key, provider_id=None, context=""):
                return {"status": "valid_zero", "provider": "deepseek",
                        "balance": 0.0}

        monkeypatch.setattr(providers, "UnifiedKeyVerifier", FakeVerifier)
        engine = ScannerEngine(output_dir=".")
        results = engine._verify_dict({
            "sk-1e175253812a4948" "86dd8952b56dc19c": {
                "source": "npm", "query": "npm-high-value", "repos": [],
            },
        })
        assert results[0]["source"] == "npm"
        assert results[0]["query"] == "npm-high-value"


class TestEngineLedgerPersistence:
    def test_persist_results_writes_valid_and_invalid_ledger(self, tmp_path):
        """source 单次扫描不能只写 JSON；SQLite 账本要保留转化率证据。"""
        import store
        from scanner_engine import ScannerEngine

        engine = ScannerEngine(output_dir=str(tmp_path))
        valid = {
            "key": "sk-1e175253812a4948" "86dd8952b56dc19c",
            "valid": True, "status": "valid_zero", "provider": "deepseek",
            "source": "npm", "balance": 0.0, "balance_cny": 0.0,
            "query": "npm-high-value",
            "verified_at": "2026-08-29 00:00:00",
        }
        invalid = {
            "key": "sk-2e175253812a4948" "86dd8952b56dc19c",
            "valid": False, "status": "invalid", "provider": "deepseek",
            "source": "npm", "verified_at": "2026-08-29 00:00:00",
        }
        engine._persist_results([valid, invalid])

        conn = store.connect(tmp_path / "darkforest.db")
        try:
            assert conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0] == 2
            assert conn.execute(
                "SELECT query FROM keys WHERE valid=1").fetchone()[0] == "npm-high-value"
            assert conn.execute(
                "SELECT COUNT(*) FROM keys WHERE valid=1").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM key_history").fetchone()[0] == 1
        finally:
            conn.close()

    def test_run_multi_source_attributes_results_to_source(self, tmp_path):
        """调度层负责把 scanner 结果和 source 绑定，验证层不能丢掉归因。"""
        engine = ScannerEngine.__new__(ScannerEngine)
        engine.output_dir = str(tmp_path)
        engine._start_time = time.time()
        engine.concurrency = 1
        engine.max_duration = 0
        engine.max_valid_keys = 0
        engine.log_callback = lambda *args, **kwargs: None
        engine._should_stop = lambda: False
        engine._run_one_scanner = lambda *args, **kwargs: {
            "sk-1e175253812a4948" "86dd8952b56dc19c": {"repos": []},
        }

        captured = {}
        def fake_verify(keys_dict):
            captured.update(next(iter(keys_dict.values())))
            return []

        engine._verify_dict = fake_verify
        engine._persist_results = lambda *args, **kwargs: None
        engine._save_incremental = lambda *args, **kwargs: None
        engine._save_final = lambda *args, **kwargs: None
        engine._query_tracker = SimpleNamespace(save=lambda: None)

        assert engine.run_multi_source(["npm"]) == []
        assert captured["source"] == "npm"

    def test_run_multi_source_verifies_batch_once(self, tmp_path):
        """回归:verify/persist 块曾被错误嵌套在 per-key 归因循环内——
        N 个 key 触发 N² 次验证、N 份重复落库、validated 计数 xN 误触熔断。"""
        engine = ScannerEngine.__new__(ScannerEngine)
        engine.output_dir = str(tmp_path)
        engine._start_time = time.time()
        engine.concurrency = 1
        engine.max_duration = 0
        engine.max_valid_keys = 0
        engine.log_callback = lambda *args, **kwargs: None
        engine._should_stop = lambda: False
        keys = {
            "sk-1e175253812a4948" "86dd8952b56dc19c": {"repos": []},
            "sk-2e175253812a4948" "86dd8952b56dc19c": {"repos": []},
            "sk-3e175253812a4948" "86dd8952b56dc19c": {"repos": []},
        }
        engine._run_one_scanner = lambda *args, **kwargs: dict(keys)

        verify_calls = []
        engine._verify_dict = lambda d: verify_calls.append(dict(d)) or []
        engine._persist_results = lambda *args, **kwargs: None
        engine._record_query_outcomes = lambda *args, **kwargs: None
        engine._save_incremental = lambda *args, **kwargs: None
        engine._save_final = lambda *args, **kwargs: None
        engine._query_tracker = SimpleNamespace(save=lambda: None)

        engine.run_multi_source(["npm"])
        assert len(verify_calls) == 1, \
            f"_verify_dict 必须整批调用一次,实际 {len(verify_calls)} 次"
        assert len(verify_calls[0]) == 3


class TestQueryContextPropagation:
    def test_scan_one_query_attaches_query_to_stream_items(self, tmp_path):
        """流式提交必须携带查询上下文，验证后才能反馈给查询排序。"""
        engine = ScannerEngine(output_dir=str(tmp_path))
        engine._authed = True
        engine.search_delay = 0
        engine._gh_search = lambda *args, **kwargs: [{
            "repository": {"full_name": "owner/repo"},
            "path": ".env",
            "html_url": "https://github.com/owner/repo/blob/main/.env",
            "text_matches": [{"fragment":
                'DEEPSEEK_API_KEY="sk-1e175253812a4948' '86dd8952b56dc19c"'}],
        }]

        keys = engine._scan_one_query("high-value-query", max_pages=1, token="t")
        assert list(keys.values())[0]["query"] == "high-value-query"

    def test_engine_records_verified_outcomes_for_query(self):
        """单次扫描不依赖 watch 回调，也能把 valid/hv 结果反馈给查询器。"""
        from scanner_engine import QueryTracker, ScannerEngine

        engine = ScannerEngine.__new__(ScannerEngine)
        engine._query_tracker = QueryTracker(stats_path=":memory:")
        engine.hv_balance_threshold = 1.0
        results = [
            {"query": "q-good", "valid": True, "balance_cny": 20.0},
            {"query": "q-good", "valid": True, "balance_cny": 0.0},
            {"query": "q-bad", "valid": False, "balance_cny": 0.0},
            {"query": None, "valid": True, "balance_cny": 99.0},
        ]

        engine._record_query_outcomes(results)

        assert engine._query_tracker._stats["q-good"]["validated"] == 2
        assert engine._query_tracker._stats["q-good"]["valid_hits"] == 2
        assert engine._query_tracker._stats["q-good"]["hv_hits"] == 1
        assert engine._query_tracker._stats["q-bad"]["validated"] == 1
        assert engine._query_tracker._stats["q-bad"]["valid_hits"] == 0
        assert "None" not in engine._query_tracker._stats

    def test_main_pipeline_skips_low_quality_query(self, tmp_path, monkeypatch):
        """deepseek 流水线不能重复烧已知 0 valid 查询的搜索配额。"""
        engine = ScannerEngine(output_dir=str(tmp_path), scan_pages=1)
        for _ in range(10):
            engine._query_tracker.record("wasted", 10)
            engine._query_tracker.record_outcome("wasted", valid=False,
                                                 high_value=False)
        engine._load_known_keys = lambda: None

        def fail_scan(*args, **kwargs):
            raise AssertionError("低质量查询不应触发扫描")

        monkeypatch.setattr(engine, "_scan_one_query", fail_scan)
        assert engine.run(["wasted"]) == []

    def test_github_source_scanner_skips_low_quality_query(self, monkeypatch):
        """source github_search 也遵守质量熔断，而不是只靠 watch 过滤。"""
        engine = ScannerEngine(output_dir=".")
        for _ in range(10):
            engine._query_tracker.record("wasted", 10)
            engine._query_tracker.record_outcome("wasted", valid=False,
                                                 high_value=False)

        def fail_scan(*args, **kwargs):
            raise AssertionError("低质量查询不应触发扫描")

        monkeypatch.setattr(engine, "_scan_one_query", fail_scan)
        assert engine._run_one_scanner("github_search", ["wasted"]) == {}


class TestGithubTokenHealth:
    def test_health_reports_valid_and_invalid_without_secrets(self, monkeypatch):
        from scanner_engine import ScannerEngine

        engine = ScannerEngine()
        monkeypatch.setattr(ScannerEngine, "get_all_gh_tokens",
                            lambda self: ["bad-token-value", "good-token-value"])

        def fake_get(url, **kwargs):
            token = kwargs["headers"]["Authorization"].split(" ", 1)[1]
            code = 200 if token == "good-token-value" else 401
            return type("R", (), {"status_code": code})()

        monkeypatch.setattr("scanner_engine.requests.get", fake_get)
        health = engine.github_token_health()
        assert health == {"configured": 2, "valid": 1, "invalid": 1,
                          "unknown": 0, "code_search_ready": True}
        assert "bad-token" not in str(health)
        assert "good-token" not in str(health)


class TestSourceRegistryContract:
    def test_available_sources_match_scanner_registry(self):
        """CLI 展示的源必须都能被 engine 调度；registry 不得有隐藏源。"""
        from scanner_engine import AVAILABLE_SOURCES, GITHUB_SEARCH_SOURCES
        engine = ScannerEngine.__new__(ScannerEngine)
        engine.proxy = None
        registry = engine._get_scanner_registry()

        assert set(registry) == set(AVAILABLE_SOURCES) - GITHUB_SEARCH_SOURCES

    def test_run_py_publishes_engine_source_catalog(self):
        """防止 run.py 再复制一份会漂移的源列表。"""
        import run
        from scanner_engine import AVAILABLE_SOURCES

        assert run.AVAILABLE_SOURCES is AVAILABLE_SOURCES

    def test_watch_defaults_are_dispatchable(self):
        """watch 默认源必须全部来自真实源目录，禁止旧文档里的幽灵源。"""
        from scanner_engine import AVAILABLE_SOURCES
        from watch_tui import DEFAULT_WATCH_SOURCES

        assert set(DEFAULT_WATCH_SOURCES) <= set(AVAILABLE_SOURCES)

    @pytest.mark.parametrize("source", sorted(
        scanner_engine_module.AVAILABLE_SOURCES))
    def test_run_multi_source_accepts_every_listed_source(self, source, tmp_path):
        """registry 支持但旧 scanner_map 缺失的源曾触发 KeyError。"""
        engine = ScannerEngine.__new__(ScannerEngine)
        engine.output_dir = str(tmp_path)
        engine._start_time = time.time()
        engine.concurrency = 1
        engine.max_duration = 0
        engine.max_valid_keys = 0
        engine.log_callback = lambda *args, **kwargs: None
        engine._should_stop = lambda: False
        engine._run_one_scanner = lambda *args, **kwargs: {}
        engine._persist_results = lambda *args, **kwargs: None
        engine._save_final = lambda *args, **kwargs: None
        engine._query_tracker = SimpleNamespace(save=lambda: None)

        assert engine.run_multi_source([source]) == []


# ── v2.4.0 第二批平台查询覆盖 ────────────────────────────────────────

class TestSecondBatchQueryCoverage:
    """第二批平台必须进静态查询库——watch 主源 QueryRotator 直接吃
    queries_optimized.txt，文件里没有就永远扫不到这些平台。"""

    @staticmethod
    def _library_lines() -> list[str]:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "queries_optimized.txt")
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f
                    if l.strip() and not l.startswith("#")]

    def test_every_second_batch_platform_has_queries(self):
        lines = self._library_lines()
        markers = {
            "openai": "sk-proj-",
            "gemini": "GEMINI_API_KEY",
            "xai": "XAI_API_KEY",
            "hunyuan": "hunyuan.cloud.tencent.com",
            "qianfan": "bce-v3",
            "modelscope": "modelscope",
            "nvidia": "nvapi-",
            "longcat": "longcat",
        }
        for pid, marker in markers.items():
            assert any(marker in q for q in lines), f"{pid} 在查询库中缺失"

    def test_library_has_no_dated_queries(self):
        """watch 的 load_queries 不做 pushed: 过滤——文件里的日期查询会过期失效。"""
        assert all("pushed:" not in q for q in self._library_lines())

    def test_second_batch_freshness_in_dynamic_queries(self):
        """专有前缀的新鲜度查询由 build_active_queries 动态注入(带当前日期)。"""
        q = build_active_queries()
        assert any(x.startswith("sk-proj- pushed:>") for x in q)
        assert any(x.startswith("GEMINI_API_KEY AIza pushed:>") for x in q)
        assert any(x.startswith("nvapi- pushed:>") for x in q)
        assert any(x.startswith("bce-v3 pushed:>") for x in q)

    def test_static_and_dynamic_no_dupes(self):
        """文件静态行 + 动态注入合并后仍无重复(build_active_queries 去重契约)。"""
        q = build_active_queries()
        assert len(q) == len(set(q))


# ══════════ v2.5.4 审查修复回归 ══════════

class TestV254QueryLearningNotPoisonedByErrors:
    def test_error_results_not_counted_as_validated(self, tmp_path):
        """error/rate_limited 是瞬态结果——不得计入 validated 毒化有效率
        分母(否则一轮网络故障就能把正常查询跨重启永久熔断)。"""
        from scanner_engine import QueryTracker, ScannerEngine
        t = QueryTracker(stats_path=str(tmp_path / "v254qs.json"))
        eng = ScannerEngine.__new__(ScannerEngine)
        eng._query_tracker = t
        eng._record_query_outcomes([
            {"query": "q1", "valid": False, "status": "error", "provider": "deepseek"},
            {"query": "q1", "valid": False, "status": "rate_limited", "provider": "kimi"},
            {"query": "q1", "valid": True, "status": "valid_active", "provider": "deepseek"},
        ])
        assert t._stats["q1"]["validated"] == 1
        assert t._stats["q1"]["valid_hits"] == 1


class TestV254QueryTrackerDurability:
    def test_failed_save_keeps_previous_file(self, tmp_path, monkeypatch):
        """原子写:save 中途异常(看门狗 os._exit 模拟)后,主文件仍是上一次
        的完整内容——旧 truncate-write 会丢光全部收益学习。"""
        import scanner_engine as se
        f = tmp_path / "v254qs.json"
        t = se.QueryTracker(stats_path=str(f))
        t.record("good", 5)
        t.save()
        first = f.read_text(encoding="utf-8")

        t.record("new", 1)

        def half_dump(obj, fh, **kw):
            fh.write('{"half":')
            raise OSError("simulated hard exit mid-dump")
        monkeypatch.setattr(se.json, "dump", half_dump)
        t.save()  # 生产实现吞异常只记日志——原子替换不得发生
        assert f.read_text(encoding="utf-8") == first
        assert "half" not in f.read_text(encoding="utf-8")
        t2 = se.QueryTracker(stats_path=str(f))
        assert "good" in t2._stats and "new" not in t2._stats

    def test_corrupt_file_load_does_not_crash(self, tmp_path):
        import scanner_engine as se
        f = tmp_path / "v254broken.json"
        f.write_text("{broken", encoding="utf-8")
        t = se.QueryTracker(stats_path=str(f))  # 不得抛异常
        assert t._stats == {}


class TestV254TickCooldownsConcurrency:
    def test_tick_while_recording_no_exception(self, tmp_path):
        """tick_cooldowns 迭代 _stats 与 record_outcome 插入并发——曾抛
        dictionary changed size during iteration。"""
        import threading

        from scanner_engine import QueryTracker
        t = QueryTracker(stats_path=str(tmp_path / "v254tick.json"))
        errors = []

        def ticker():
            for _ in range(200):
                try:
                    t.tick_cooldowns()
                except RuntimeError as e:
                    errors.append(e)

        def recorder():
            for i in range(200):
                t.record_outcome(f"q{i}", valid=True, high_value=False)

        threads = [threading.Thread(target=ticker), threading.Thread(target=recorder)]
        for x in threads:
            x.start()
        for x in threads:
            x.join()
        assert errors == []


class TestV254DisabledTokensFiltered:
    def test_disabled_token_excluded_from_rotation(self, monkeypatch):
        """401 禁用的 token 必须被轮转过滤——否则其查询桶静默空返回,
        查询统计被打成 barren 毒化收益学习。"""
        from scanner_engine import ScannerEngine
        eng = ScannerEngine.__new__(ScannerEngine)
        eng._token_disabled = set()
        eng._token_401_count = {}
        eng.log_callback = lambda *a, **k: None
        monkeypatch.setattr(ScannerEngine, "get_all_gh_tokens",
                            lambda self: ["tok-a", "tok-b"])
        for _ in range(3):
            eng._record_token_401("tok-a")
        live = [t for t in eng.get_all_gh_tokens() if eng._is_token_healthy(t)]
        assert live == ["tok-b"]


class TestV254PercentThresholdSemantics:
    def test_percent_conversion_passes_through(self):
        """PERCENT 行换算透传(阈值">1% 留存"的设计语义);金额合计层
        (TUI 总值/引擎汇总/ledger_metrics)负责排除 PERCENT——不在换算层
        归零,也不把 85% 当 ¥85 累加。"""
        from scanner_engine import convert_to_cny, convert_to_usd
        assert convert_to_cny(85.0, "PERCENT") == 85.0
        assert convert_to_usd(85.0, "PERCENT") == 85.0
        assert convert_to_cny(8.0, "USD") == 58.0  # 真金额照常换算
        assert convert_to_usd(58.0, "CNY") == 8.0
