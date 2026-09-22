"""watch_tui.py 的单元测试。"""

import time

from watch_tui import (
    VerificationBroker,
    filter_high_value,
    update_first_seen,
)

# ── filter_high_value ──────────────────────────────────────────────


def test_persistence_module_reexports_are_stable():
    """watch_tui 的持久化入口迁移后，外部调用和测试 monkeypatch 语义不变。"""
    import watch_persistence
    import watch_tui

    names = [
        "save_watch_state", "load_watch_state", "load_history",
        "_WATCH_CSV_HEADER", "_watch_csv_row", "write_watch_csv",
    ]
    for name in names:
        assert getattr(watch_tui, name) is getattr(watch_persistence, name)

class TestFilterHighValue:
    def _result(self, key, cny):
        return {"key": key, "balance_cny": cny, "valid": True}

    def test_keeps_above_threshold(self):
        results = [self._result("sk-a", 5.0), self._result("sk-b", 0.5)]
        out = filter_high_value(results, min_balance=1.0)
        assert len(out) == 1
        assert out[0]["key"] == "sk-a"

    def test_sorted_descending(self):
        results = [self._result("sk-a", 3.0), self._result("sk-b", 10.0)]
        out = filter_high_value(results, min_balance=1.0)
        assert out[0]["key"] == "sk-b"
        assert out[1]["key"] == "sk-a"

    def test_empty_input(self):
        assert filter_high_value([], 1.0) == []

    def test_excludes_invalid(self):
        r = {"key": "sk-a", "balance_cny": 5.0, "valid": False}
        assert filter_high_value([r], 1.0) == []

    def test_exact_threshold_excluded(self):
        r = {"key": "sk-a", "balance_cny": 1.0, "valid": True}
        assert filter_high_value([r], 1.0) == []


# ── update_first_seen ──────────────────────────────────────────────

class TestUpdateFirstSeen:
    def test_new_key_gets_verified_at(self):
        results = [{"key": "sk-a", "verified_at": "2026-08-08 10:00:00"}]
        out = update_first_seen(results, [])
        assert out[0]["first_seen"] == "2026-08-08 10:00:00"

    def test_known_key_keeps_original(self):
        known = [{"key": "sk-a", "first_seen": "2026-08-01 00:00:00"}]
        results = [{"key": "sk-a", "verified_at": "2026-08-08 10:00:00"}]
        out = update_first_seen(results, known)
        assert out[0]["first_seen"] == "2026-08-01 00:00:00"


# ── VerificationBroker ────────────────────────────────────────────

class TestVerificationBroker:
    """测试 VerificationBroker 的 submit / 去重 / 统计。"""

    def _make_broker(self):
        # 用 mock engine（不需要真正验证）
        class MockEngine:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None
        return VerificationBroker(engine=MockEngine(), min_balance=1.0, interval=0.1)

    def test_submit_new_key_returns_true(self):
        b = self._make_broker()
        assert b.submit("sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa", source="github") is True

    def test_submit_duplicate_returns_false(self):
        b = self._make_broker()
        b.submit("sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa", source="github")
        assert b.submit("sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa", source="github") is False

    def test_submit_many_counts_new_keys(self):
        b = self._make_broker()
        keys = {
            "sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa": {"repos": []},
            "sk-bbbbbbbbbbbbbbbb" "bbbbbbbbbbbbbbbb": {"repos": []},
            "sk-cccccccccccccccc" "cccccccccccccccc": {"repos": []},
        }
        n = b.submit_many(keys, source="github")
        assert n == 3
        snap = b.snapshot()
        assert snap["submitted"] == 3

    def test_get_high_value_filters_by_balance(self):
        b = self._make_broker()
        # 手动注入结果（绕过实际 HTTP 验证）
        b._store_result({"key": "sk-a", "valid": True, "balance_cny": 5.0})
        b._store_result({"key": "sk-b", "valid": True, "balance_cny": 0.5})
        b._store_result({"key": "sk-c", "valid": False, "balance_cny": 0.0})
        hv = b.get_high_value()
        assert len(hv) == 1
        assert hv[0]["key"] == "sk-a"

    def test_get_all_results_keeps_valid_only(self):
        # 无效 key 不留存（_seen 负责去重），避免 24/7 长跑撑爆内存/watch_state.json
        b = self._make_broker()
        b._store_result({"key": "sk-a", "valid": True})
        b._store_result({"key": "sk-b", "valid": False})
        results = b.get_all_results()
        assert len(results) == 1
        assert results[0]["key"] == "sk-a"


# ── 重启重验 & 长跑稳定性 ──────────────────────────────────────────

class TestReverifyAndStability:
    def _make_broker(self):
        class MockEngine:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None
        return VerificationBroker(engine=MockEngine(), min_balance=1.0, interval=0.01)

    def test_reverify_queues_below_new_keys(self):
        b = self._make_broker()
        # 重验排入低优先级（20），新 key 高优先级（0）——PriorityQueue 先出小的
        assert b.reverify("sk-old-111", source="history") is True
        assert b.reverify("sk-old-111", source="history") is False  # 每 key 只排一次
        assert b.reverify("sk-old-222", source="history") is True
        assert b.snapshot()["pending"] == 2

    def test_reverify_invalid_evicts(self):
        b = self._make_broker()
        b.reverify("sk-old-111", source="history")
        b._store_result({"key": "sk-old-111", "valid": False})  # 重验失效
        assert "sk-old-111" in b.evicted_keys()

    def test_reverify_below_threshold_evicts(self):
        b = self._make_broker()
        b.reverify("sk-old-111", source="history")
        b._store_result({"key": "sk-old-111", "valid": True, "balance_cny": 0.5})  # 低于 ¥1
        assert "sk-old-111" in b.evicted_keys()

    def test_reverify_still_high_kept(self):
        b = self._make_broker()
        b.reverify("sk-old-111", source="history")
        b._store_result({"key": "sk-old-111", "valid": True, "balance_cny": 9.9})  # 仍高价值
        assert "sk-old-111" not in b.evicted_keys()

    def test_reverify_transient_error_not_evicted(self):
        """reverify 遇瞬态错误（网络超时/503/rate_limited）不剔除——保留历史值，下次再验。"""
        b = self._make_broker()
        b.reverify("sk-old-111", source="history")
        b._store_result({"key": "sk-old-111", "valid": False, "status": "error"})
        assert "sk-old-111" not in b.evicted_keys()

        b2 = self._make_broker()
        b2.reverify("sk-old-222", source="history")
        b2._store_result({"key": "sk-old-222", "valid": False, "status": "rate_limited"})
        assert "sk-old-222" not in b2.evicted_keys()

    def test_new_invalid_not_evicted(self):
        # 只有重验的 key 才进剔除名单（新扫描的无效 key 由 _seen 去重，不进名单）
        b = self._make_broker()
        b._store_result({"key": "sk-new-222", "valid": False})
        assert "sk-new-222" not in b.evicted_keys()

    def test_results_capped(self):
        # 长跑防膨胀：_results 超上限淘汰最低余额，高价值保留
        b = self._make_broker()
        b._results_max = 5
        for i in range(10):
            b._store_result({"key": f"sk-{i:03d}", "valid": True, "balance_cny": float(i)})
        results = b.get_all_results()
        assert len(results) == 5
        assert "sk-009" in {r["key"] for r in results}  # 最高余额保留
        assert "sk-000" not in {r["key"] for r in results}  # 最低被淘汰


# ── VerificationBroker idle / drain ────────────────────────────────


class TestVerificationBrokerIdle:
    """Regression tests for the idle flag that the shutdown drain waits on.

    The drain loop must wait for queue-empty *and* worker-idle, because
    qsize() hits 0 the instant the worker dequeues -- BEFORE _verify_one
    finishes (it can sleep up to 30s on a 429 backoff). If the drain only
    checked qsize, it would kill a mid-backoff worker and lose that key's
    balance result.
    """

    def _make_broker(self, verify_delay=0.0):

        class MockEngine:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        b = VerificationBroker(engine=MockEngine(), min_balance=1.0, interval=0.05,
                               workers=1)  # 单 worker 测试 idle 语义

        # Replace _verify_one with a deterministic fake that optionally blocks,
        # simulating a slow/429 request -- WITHOUT hitting the real network.
        def fake_verify(item):
            if verify_delay:
                time.sleep(verify_delay)
            return {
                "key": item["key"],
                "valid": True,
                "balance": 10.0,
                "primary_currency": "CNY",
                "balance_usd": 1.38,
                "balance_cny": 72.5,
                "repos": item.get("repos", []),
                "source": item.get("source", "unknown"),
                "key_preview": item.get("key_preview", ""),
                "verified_at": "2026-08-08 10:00:00",
            }

        b._verify_one = fake_verify
        return b

    def test_idle_true_when_idle(self):
        b = self._make_broker()
        b.start()
        # No work submitted → worker blocks on get() → idle.
        time.sleep(0.3)
        assert b.idle() is True
        b.stop()

    def test_idle_false_during_inflight_verification(self):
        b = self._make_broker(verify_delay=0.5)
        b.start()
        b.submit("sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa", source="github")
        # Within the 0.5s verification window the worker must report not-idle.
        # Poll (don't fixed-sleep): the worker thread may not have dequeued
        # within a hard-coded window under heavy suite load.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not b.idle():
                break
            time.sleep(0.05)
        assert b.idle() is False, "worker should not be idle while verifying"
        # After verification completes it returns to idle.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if b.idle():
                break
            time.sleep(0.05)
        assert b.idle() is True
        b.stop()

    def test_drain_waits_for_inflight_then_writes_result(self):
        """Simulate the shutdown drain: even with qsize==0, an in-flight
        verification must complete and its result be captured."""
        b = self._make_broker(verify_delay=0.4)
        b.start()
        b.submit("sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa", source="github")
        # Wait until dequeued (qsize==0) but verification still running.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if b.snapshot()["pending"] == 0 and not b.idle():
                break
            time.sleep(0.05)
        assert b.snapshot()["pending"] == 0, "key should be dequeued"
        assert b.idle() is False, "verification should still be in-flight"

        # Drain the way run_watch does: wait for pending==0 AND idle.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            snap = b.snapshot()
            if snap["pending"] == 0 and b.idle():
                break
            time.sleep(0.05)
        b.stop()

        # The in-flight verification's result must be present (not lost).
        all_results = b.get_all_results()
        assert len(all_results) == 1
        assert all_results[0]["key"] == "sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa"

    def test_multi_worker_drains_faster(self):
        """多 worker 应能并行验证多个 key（总吞吐 ≈ workers/interval）。"""
        b = self._make_broker(verify_delay=0.2)
        b._workers = 4
        b._worker_threads = []
        b.start()
        # 提交 4 个 key，单 worker 需 ~0.85s+，4 worker 应 ~0.25s+
        for i in range(4):
            b.submit(f"sk-{'a'*32}_{i}", source="test")
        # 等待全部验证完成
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if b.idle():
                break
            time.sleep(0.05)
        b.stop()
        assert b.idle(), "4 workers should drain 4 keys quickly"
        assert b.snapshot()["verified"] == 4

    def test_seed_only_high_value_keys_from_db(self, tmp_path):
        """回归（P0）：_seen 种子只能含高价值 key。

        早期 bug：__init__ 里用 all_known_keys()（SELECT 全部 key）播种 _seen，
        把历史 invalid/低余额 key 也全量屏蔽 → 扫描器扫到它们会被 submit 直接丢、
        永不重验（key 重新激活/充值后拿不到）。修复后只播种 high_value_keys。
        """
        import store as _store
        conn = _store.connect(str(tmp_path / "boostrap.db"))

        def _r(key, cny, valid):
            return {"key": key, "valid": valid, "balance_cny": cny,
                    "provider": "deepseek", "source": "github"}

        # 高价值 key → 应进种子
        _store.upsert(conn, _r("sk-" + "1" * 36, 9.0, True))
        # 低余额 valid key → 重验，不应进种子
        _store.upsert(conn, _r("sk-" + "2" * 36, 0.3, True))
        # invalid key → 重验，不应进种子
        _store.upsert(conn, _r("sk-" + "3" * 36, 5.0, False))
        conn.close()

        class MockEngine:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        b = VerificationBroker(engine=MockEngine(), min_balance=1.0, interval=0.1,
                               db_path=str(tmp_path / "boostrap.db"))
        # 只有高价值 key 在种子；低余额 valid 与 invalid 必须留出来（可重新验证）
        assert "sk-" + "1" * 36 in b._seen
        assert "sk-" + "2" * 36 not in b._seen
        assert "sk-" + "3" * 36 not in b._seen
        # 且新增重验能通过（低余额 key 可再次 submit/reverify）
        assert b.submit("sk-" + "2" * 36, source="github") is True
        assert b.submit("sk-" + "1" * 36, source="github") is False
        b.stop()

    def test_seen_set_capped(self):
        """_seen 达到上限后应被清空，防止内存无限增长。"""
        b = self._make_broker()
        b._seen_max = 100  # 用小上限测试
        for i in range(150):
            b.submit(f"sk-{'a'*32}_{i:04d}", source="test")
        # 超过上限后 _seen 应被清空（≤ 100 或刚清空后的少量）
        assert len(b._seen) <= 100, f"_seen should be capped, got {len(b._seen)}"


# ── 持久化 ─────────────────────────────────────────────────────────

class TestSaveLoadState:
    def test_roundtrip(self, tmp_path):
        from watch_tui import load_watch_state, save_watch_state
        results = [
            {"key": "sk-a", "key_preview": "sk-a...a", "balance_cny": 5.0, "valid": True},
            {"key": "sk-b", "key_preview": "sk-b...b", "balance_cny": 0.0, "valid": True},
        ]
        path = str(tmp_path / "state.json")
        save_watch_state(path, results)
        loaded = load_watch_state(path)
        assert len(loaded) == 2
        keys = {r["key"] for r in loaded}
        assert keys == {"sk-a", "sk-b"}

    def test_load_missing_returns_empty(self, tmp_path):
        from watch_tui import load_watch_state
        assert load_watch_state(str(tmp_path / "nope.json")) == []

    def test_dedup_by_key(self, tmp_path):
        from watch_tui import load_watch_state, save_watch_state
        results = [
            {"key": "sk-a", "balance_cny": 1.0},
            {"key": "sk-a", "balance_cny": 2.0},
        ]
        path = str(tmp_path / "state.json")
        save_watch_state(path, results)
        loaded = load_watch_state(path)
        assert len(loaded) == 1


# ── CSV 写入 ───────────────────────────────────────────────────────

class TestWriteWatchCSV:
    def test_creates_file_with_header(self, tmp_path):
        from watch_tui import write_watch_csv
        path = str(tmp_path / "hv.csv")
        write_watch_csv(path, [])
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "Key预览" in content
        assert "余额(CNY)" in content

    def test_writes_rows_sorted(self, tmp_path):
        from watch_tui import write_watch_csv
        keys = [
            {"key": "sk-a", "key_preview": "sk-a...a", "balance_cny": 3.0,
             "balance_usd": 0.41, "balance": 3.0, "primary_currency": "CNY",
             "first_seen": "2026-08-08 10:00:00", "verified_at": "2026-08-08 12:00:00",
             "source": "gitlab", "repos": [{"repo": "u/r"}]},
            {"key": "sk-b", "key_preview": "sk-b...b", "balance_cny": 10.0,
             "balance_usd": 1.38, "balance": 10.0, "primary_currency": "CNY",
             "first_seen": "2026-08-08 09:00:00", "verified_at": "2026-08-08 12:00:00",
             "source": "huggingface", "repos": []},
        ]
        path = str(tmp_path / "hv.csv")
        write_watch_csv(path, keys)
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 3  # header + 2 rows
        assert "sk-b" in lines[1]  # higher balance first
        assert "sk-a" in lines[2]

    def _hk(self, key, cny, vt="2026-08-08 10:00:00", src="g"):
        return {"key": key, "key_preview": f"{key[:4]}...{key[-2:]}", "balance_cny": cny,
                "balance_usd": cny / 7.25, "balance": cny, "primary_currency": "CNY",
                "verified_at": vt, "source": src, "repos": []}

    def test_existing_rows_preserved_across_saves(self, tmp_path):
        # 增量：旧 key 不在当前快照也必须保留（不被覆盖删除）
        from watch_tui import write_watch_csv
        path = str(tmp_path / "hv.csv")
        write_watch_csv(path, [self._hk("sk-a", 5.0)])
        write_watch_csv(path, [self._hk("sk-b", 3.0)])  # 快照只剩 sk-b
        content = open(path, encoding="utf-8").read()
        assert "sk-a" in content and "sk-b" in content

    def test_arrears_keys_removed(self, tmp_path):
        # 验证欠费（balance_cny < 0）的 key 才允许删除
        from watch_tui import write_watch_csv
        path = str(tmp_path / "hv.csv")
        write_watch_csv(path, [self._hk("sk-a", 5.0)])
        write_watch_csv(path, [], arrears_keys={"sk-a"})
        assert "sk-a" not in open(path, encoding="utf-8").read()

    def test_existing_row_not_overwritten(self, tmp_path):
        # 已有行不覆盖：再次出现同 key（余额变化）时保持原值
        from watch_tui import write_watch_csv
        path = str(tmp_path / "hv.csv")
        write_watch_csv(path, [self._hk("sk-a", 5.0)])
        write_watch_csv(path, [self._hk("sk-a", 50.0)])
        row = open(path, encoding="utf-8").read().splitlines()[1]
        assert "5.00" in row and "50.00" not in row


class TestSaveThrottle:
    def test_rapid_second_call_skipped_force_overrides(self, tmp_path, monkeypatch):
        # 长跑节流：15s 内的二次保存被跳过；force=True（final_save）绕过节流
        import watch_tui
        from watch_tui import VerificationBroker, WatchScanner, WatchState

        class E:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        broker = VerificationBroker(engine=E())
        broker._store_result({"key": "sk-a", "valid": True, "balance_cny": 5.0})
        scanner = WatchScanner(state=WatchState(broker=broker), broker=broker,
                               output_dir=str(tmp_path))
        calls = []
        monkeypatch.setattr(watch_tui, "write_watch_csv", lambda *a, **k: calls.append(a))

        scanner._save_from_broker(force=True)   # 首次：写
        scanner._save_from_broker()             # 紧接第二次：节流跳过
        assert len(calls) == 1
        scanner._save_from_broker(force=True)   # 强制：再写
        assert len(calls) == 2


class TestSaveRemovesInvalidatedHistory:
    def test_evicted_and_arrears_keys_leave_watch_state(self, tmp_path):
        """重验剔除/欠费的 key 不能经 watch_state.json 在下次启动时复活。"""
        import csv
        import json

        from watch_tui import WatchScanner, WatchState

        class Engine:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        broker = VerificationBroker(engine=Engine())
        broker._evicted.add("sk-evicted")
        broker._results["sk-arrears"] = {
            "key": "sk-arrears", "valid": True, "balance_cny": -2.0,
        }
        state = WatchState(broker=broker)
        scanner = WatchScanner(state=state, broker=broker,
                               output_dir=str(tmp_path))
        scanner._history_map = {
            "sk-evicted": {"key": "sk-evicted", "valid": True,
                           "balance_cny": 20.0},
            "sk-keep": {"key": "sk-keep", "valid": True,
                        "balance_cny": 5.0},
        }

        scanner._save_from_broker(force=True)

        saved = json.load(open(scanner.state_path, encoding="utf-8"))["keys"]
        assert "sk-evicted" not in saved
        assert "sk-arrears" not in saved
        assert "sk-keep" in saved
        with open(scanner.csv_path, encoding="utf-8", newline="") as f:
            csv_keys = {row["完整Key"] for row in csv.DictReader(f)}
        assert "sk-evicted" not in csv_keys
        assert "sk-arrears" not in csv_keys
        assert "sk-keep" in csv_keys

    def test_all_evicted_history_clears_stale_state(self, tmp_path):
        """全部历史被剔除时，不能让旧 state 原样留存到下次启动。"""
        import csv
        import json

        from watch_tui import WatchScanner, WatchState, save_watch_state

        class Engine:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        broker = VerificationBroker(engine=Engine())
        broker._evicted.add("sk-old")
        stale_path = str(tmp_path / "watch_state.json")
        save_watch_state(stale_path, [{"key": "sk-old", "valid": True,
                                       "balance_cny": 20.0}])
        scanner = WatchScanner(state=WatchState(broker=broker), broker=broker,
                               output_dir=str(tmp_path))
        scanner._history_map = {
            "sk-old": {"key": "sk-old", "valid": True, "balance_cny": 20.0},
        }

        scanner._save_from_broker(force=True)

        assert json.load(open(scanner.state_path, encoding="utf-8"))["keys"] == {}
        with open(scanner.csv_path, encoding="utf-8", newline="") as f:
            assert list(csv.DictReader(f)) == []


class TestStateSaveDirtyTracking:
    def test_broker_revision_tracks_state_visible_results(self):
        from watch_tui import VerificationBroker

        broker = VerificationBroker(engine=None)
        assert broker.state_revision() == 0

        broker._store_result({"key": "sk-invalid", "valid": False,
                              "status": "invalid"})
        assert broker.state_revision() == 0

        broker._store_result({"key": "sk-valid", "valid": True,
                              "balance_cny": 5.0})
        assert broker.state_revision() == 1

        broker._reverify_queued.add("sk-evicted")
        broker._store_result({"key": "sk-evicted", "valid": False,
                              "status": "invalid"})
        assert broker.state_revision() == 2

    def test_save_skips_when_broker_state_unchanged(self, tmp_path, monkeypatch):
        """空闲轮询不应反复重写 JSON/CSV；结果变化后才写。"""
        import watch_tui
        from watch_tui import VerificationBroker, WatchScanner, WatchState

        class Engine:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        broker = VerificationBroker(engine=Engine())
        scanner = WatchScanner(state=WatchState(broker=broker), broker=broker,
                               output_dir=str(tmp_path))
        calls = []
        monkeypatch.setattr(watch_tui, "write_watch_csv",
                            lambda *a, **k: calls.append(a))

        scanner._save_from_broker(force=True)
        assert len(calls) == 1
        assert scanner._saved_broker_revision == 0

        scanner._save_from_broker()
        assert len(calls) == 1

        broker._store_result({"key": "sk-a", "valid": True,
                              "balance_cny": 5.0})
        scanner._last_save = 0.0
        scanner._save_from_broker()
        assert len(calls) == 2
        assert scanner._saved_broker_revision == 1


class TestBrokerChatProbeWiring:
    def test_broker_passes_chat_probe_opt_in(self, monkeypatch):
        """watch 验证 worker 也必须继承默认关闭/显式开启的探测开关。"""
        import providers
        import watch_tui

        captured = {}

        class FakeVerifier:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(providers, "UnifiedKeyVerifier", FakeVerifier)
        broker = watch_tui.VerificationBroker(engine=None, allow_chat_probe=True)
        assert broker._get_verifier() is not None
        assert captured["allow_chat_probe"] is True
        assert captured["rate_limiter"] is broker.provider_rate_limiter


class TestQueryOutcomeFeedback:
    def test_broker_publishes_verified_result_to_callback(self):
        """验证结果回调必须带查询上下文，供排序器学习。"""
        from watch_tui import VerificationBroker

        events = []
        broker = VerificationBroker(engine=None, on_verified=events.append)
        broker._store_result({"key": "sk-outcome", "valid": True,
                              "balance_cny": 12.0, "query": "q-high"})
        assert len(events) == 1
        assert events[0]["query"] == "q-high"

    def test_scanner_records_valid_and_high_value_outcomes(self):
        """scanner 回调把最终验证结果映射为查询质量信号。"""
        from scanner_engine import QueryTracker, ScannerEngine
        from watch_tui import WatchScanner

        tracker = QueryTracker(stats_path=":memory:")
        scanner = WatchScanner.__new__(WatchScanner)
        scanner.min_balance = 1.0
        scanner.commits_since_hours = None
        scanner.concurrency = 1
        scanner.proxy = None
        scanner.state = None
        fake_engine = ScannerEngine.__new__(ScannerEngine)
        fake_engine._query_tracker = tracker
        scanner._engine = fake_engine
        scanner._engine_lock = __import__("threading").Lock()

        scanner._record_query_outcome({"query": "q", "valid": True,
                                       "balance_cny": 20.0})
        scanner._record_query_outcome({"query": "q", "valid": True,
                                       "balance_cny": 0.0})
        scanner._record_query_outcome({"query": "q", "valid": False,
                                       "balance_cny": 0.0})

        assert tracker._stats["q"]["validated"] == 3
        assert tracker._stats["q"]["valid_hits"] == 2
        assert tracker._stats["q"]["hv_hits"] == 1

    def test_low_quality_queries_are_not_resurrected_by_extinction_guard(self):
        """全部查询被质量熔断时，灭绝保护不能把它们重新投入预算。"""
        from scanner_engine import QueryTracker
        from watch_tui import WatchScanner

        tracker = QueryTracker(stats_path=":memory:")
        tracker.record("q-low", 10)
        for _ in range(10):
            tracker.record_outcome("q-low", valid=False, high_value=False)

        scanner = WatchScanner.__new__(WatchScanner)
        scanner.state = type("S", (), {
            "should_exit": False,
            "add_log": lambda self, *args, **kwargs: None,
        })()
        scanner._engine = type("E", (), {"_query_tracker": tracker})()
        scanner._engine_lock = __import__("threading").Lock()
        scanner._rotator = type("R", (), {
            "next_round": lambda self: ["q-low"],
        })()
        scanner._gen_pool = None
        scanner._github_pages = 1
        scanner.commits_since_hours = None
        scanner._max_mutants = 0
        scanner._scan_github_serial = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("低质量查询不应进入扫描"))

        assert scanner._scan_github_fast(["token"]) == 0


class TestOnceExternalSource:
    def test_once_scans_external_source_immediately(self, monkeypatch):
        """--once 不应让外部源先空转 9 轮等待轮次调度。"""
        import watch_tui
        from watch_tui import WatchScanner

        class State:
            should_exit = False

            def set_source_status(self, *args, **kwargs):
                pass

            def add_log(self, *args, **kwargs):
                pass

            def touch_activity(self, *args, **kwargs):
                pass  # v2.5.4 看门狗心跳:源循环每轮调用

        class Broker:
            def snapshot(self):
                return {"pending": 0}

        scanner = WatchScanner.__new__(WatchScanner)
        scanner.state = State()
        scanner.broker = Broker()
        scanner.once = True
        scanner.interval = 1
        scanner.min_balance = 1.0
        scanner._get_engine = lambda: object()
        scanner._get_all_tokens = lambda: ["token"]
        scanner._save_from_broker = lambda *args, **kwargs: None
        scans = []

        def fake_scan(*args, **kwargs):
            scans.append(args)
            return 0

        scanner._scan_external = fake_scan
        monkeypatch.setattr(watch_tui.time, "sleep",
                            lambda *args, **kwargs: (_ for _ in ()).throw(
                                AssertionError("once 外部源不应等待轮次调度")))
        scanner._source_worker("npm")
        assert len(scans) == 1
        assert scans[0][0] == "npm"


class TestSaveMergesHistory:
    """回归：重启后首轮保存不能清掉历史 key。

    启动时 broker 是空的（历史 key 仅回显、未重验），若保存直接覆盖，
    首轮保存会把上次会话的 key 清光 → "重启后东西还在"失效。
    保存必须 = broker 结果 ∪ 磁盘历史（watch_state.json）。
    """

    def _scanner(self, tmp_path):
        from watch_tui import VerificationBroker, WatchScanner, WatchState

        class E:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        broker = VerificationBroker(engine=E())
        state = WatchState(broker=broker)
        return broker, WatchScanner(state=state, broker=broker, output_dir=str(tmp_path))

    def test_first_save_keeps_disk_history(self, tmp_path):
        # 1. 磁盘已有上次会话的高价值 key（¥7.98，valid）
        from watch_tui import save_watch_state
        save_watch_state(str(tmp_path / "watch_state.json"),
                         [{"key": "sk-old", "valid": True, "balance_cny": 7.98,
                           "verified_at": "2026-08-08 07:16:47", "source": "gh"}])

        # 2. 模拟重启：broker 空 + 仅回显（不重验）
        broker, scanner = self._scanner(tmp_path)

        # 3. 首轮保存（force 绕过 15s 节流）——历史 key 必须还在
        scanner._save_from_broker(force=True)
        from watch_tui import load_watch_state
        saved = load_watch_state(str(tmp_path / "watch_state.json"))
        assert {r["key"] for r in saved} == {"sk-old"}, "首轮保存清掉了历史 key!"

    def test_new_result_overwrites_same_key_keeps_others(self, tmp_path):
        from watch_tui import save_watch_state
        save_watch_state(str(tmp_path / "watch_state.json"),
                         [{"key": "sk-old", "valid": True, "balance_cny": 7.98,
                           "verified_at": "2026-08-08 07:16:47", "source": "gh"},
                          {"key": "sk-old2", "valid": True, "balance_cny": 2.0,
                           "source": "gh"}])

        broker, scanner = self._scanner(tmp_path)
        # 本轮新验证：覆盖 sk-old（余额变化）+ 全新 sk-new
        broker._store_result({"key": "sk-old", "valid": True, "balance_cny": 9.5,
                              "verified_at": "2026-08-08 09:00:00", "source": "gh"})
        broker._store_result({"key": "sk-new", "valid": True, "balance_cny": 3.0,
                              "verified_at": "2026-08-08 09:00:01", "source": "gh"})

        scanner._save_from_broker(force=True)
        from watch_tui import load_watch_state
        by_key = {r["key"]: r for r in load_watch_state(str(tmp_path / "watch_state.json"))}
        assert set(by_key) == {"sk-old", "sk-old2", "sk-new"}
        assert by_key["sk-old"]["balance_cny"] == 9.5, "同名 key 应被新验证覆盖"
        assert by_key["sk-old2"]["balance_cny"] == 2.0, "纯历史 key 应原样保留"
        assert by_key["sk-new"]["balance_cny"] == 3.0

    def test_hv_table_keeps_history_before_any_verify(self, tmp_path):
        # TUI 表格在首轮保存后仍显示历史高价值 key（曾因覆盖清空变"暂无"）
        from watch_tui import _render_hv_table, save_watch_state
        save_watch_state(str(tmp_path / "watch_state.json"),
                         [{"key": "sk-old", "valid": True, "balance_cny": 7.98,
                           "verified_at": "2026-08-08 07:16:47", "source": "gh"}])
        broker, scanner = self._scanner(tmp_path)
        scanner._save_from_broker(force=True)  # 首轮保存
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=120, record=True).print(
            _render_hv_table(scanner.state.snapshot(), 0))
        assert "sk-old" in buf.getvalue(), "首轮保存后表格应保留历史高价值 key"
        assert "暂无高价值 Key" not in buf.getvalue()

    def test_hv_table_keeps_csv_db_history_after_save(self, tmp_path):
        # 回归：历史只存在 CSV/db（JSON 被清空）时，首轮保存后表格仍显示高价值 key。
        # 曾因回显与保存历史源不一致（回显看 CSV/db、保存只读 JSON）导致回显被冲掉。
        import csv as _csv

        import store
        from watch_tui import _render_hv_table, load_history
        # JSON 空；CSV 有 ¥7.98；db 有 1 个有效 key
        (tmp_path / "watch_state.json").write_text('{"keys": {}}', encoding="utf-8")
        with open(tmp_path / "watch_high_value.csv", "w", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["Key预览", "完整Key", "余额(CNY)", "余额(USD)", "原始余额",
                        "币种", "首次发现", "最后验证", "数据源", "仓库"])
            w.writerow(["sk-csv...", "sk-csv-key-0001", "7.98", "1.10", "1.1",
                        "USD", "2026-08-08 07:16:47", "2026-08-08 07:16:47",
                        "github_search", "repo/x"])
        conn = store.connect(str(tmp_path / "darkforest.db"))
        store.upsert(conn, {"key": "sk-db-key-0002", "valid": True, "balance_cny": 2.5,
                            "source": "gitlab", "verified_at": "2026-08-08 07:00:00"})
        conn.close()
        # 重启：seed（三路）→ 首轮保存（force）
        hist = load_history(str(tmp_path))
        assert {r["key"] for r in hist} == {"sk-csv-key-0001", "sk-db-key-0002"}
        broker, scanner = self._scanner(tmp_path)
        scanner.state.seed_from_history(hist, broker=broker, min_balance=1.0)
        scanner._save_from_broker(force=True)
        # 保存合并（同样三路）→ 回显不能丢
        assert any(r["key"] == "sk-csv-key-0001"
                   for r in scanner.state.high_value_keys), "CSV 高价值 key 被冲掉"
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=120, record=True).print(
            _render_hv_table(scanner.state.snapshot(), 0))
        assert "sk-csv-key-0001" in buf.getvalue()
        assert "暂无高价值 Key" not in buf.getvalue()

    def test_load_history_normalizes_balance_to_balance_cny(self, tmp_path):
        """回归（P0）: watch_state.json 用 `balance` 字段存储，但重验/邮件/filter
        读 `balance_cny`。若归一化缺失，高价值 key（如 859 元那个）会被判定为
        balance_cny=0 → 永不重验、永不触发邮件。必须用 balance 补齐 balance_cny。"""
        import json

        from watch_tui import load_history
        bal = 859.4
        # JSON：只有 balance 字段（正是 save_watch_state 实际写出的结构）
        json_rec = {
            "key": "sk-9f286133ee1c4b4b" "bd4be76711580b02",
            "valid": 1, "balance": bal, "currency": "CNY",
            "provider": "deepseek", "status": "valid_active",
            "source": "github_search", "repo": "Ch1ps-dot/voltron",
            "file": "config/configs.yaml",
            "first_seen": "2026-08-11 03:00:37",
            "last_seen": "2026-08-11 03:00:37",
        }
        (tmp_path / "watch_state.json").write_text(
            json.dumps({"keys": {json_rec["key"]: json_rec}}), encoding="utf-8")
        hist = load_history(str(tmp_path))
        assert len(hist) == 1
        rec = hist[0]
        # 核心断言：balance 必须归一化为 balance_cny
        assert rec.get("balance_cny") == bal, \
            f"balance 未归一化为 balance_cny: {rec.get('balance_cny')}"
        # 归一化后必须能通过 filter_high_value（否则不会重验/发邮件）
        from watch_tui import filter_high_value
        assert filter_high_value(hist, min_balance=1.0), "高价值 key 被错误过滤"
        # 且 seed_from_history 应把它列为高价值（触发重验/邮件）
        broker, scanner = self._scanner(tmp_path)
        scanner.state.seed_from_history(hist, broker=broker, min_balance=1.0)
        assert [r["key"] for r in scanner.state.high_value_keys] == [json_rec["key"]]


# ── WatchState ─────────────────────────────────────────────────────

class TestWatchState:
    def test_initial_values(self):
        from watch_tui import WatchState
        s = WatchState()
        snap = s.snapshot()
        assert snap["stats"]["submitted"] == 0
        assert snap["stats"]["high_value"] == 0

    def test_add_log_appends(self):
        from watch_tui import WatchState
        s = WatchState()
        s.add_log("hello", "info")
        snap = s.snapshot()
        assert len(snap["logs"]) == 1
        assert snap["logs"][0]["message"] == "hello"

    def test_log_maxlen(self):
        from watch_tui import WatchState
        s = WatchState()
        for i in range(20):
            s.add_log(f"msg{i}")
        snap = s.snapshot()
        assert len(snap["logs"]) == 10
        assert snap["logs"][-1]["message"] == "msg19"

    def test_set_source_status(self):
        from watch_tui import WatchState
        s = WatchState()
        s.set_source_status("github_search", "scanning", 1, keys=5)
        snap = s.snapshot()
        assert snap["source_status"]["github_search"]["phase"] == "scanning"
        assert snap["source_status"]["github_search"]["keys"] == 5
        # "提交"统计来自累计计数器（set_source_status 只存展示值，不计入统计）
        assert snap["stats"]["submitted"] == 0
        s.add_source_submitted("github_search", 5)
        assert s.snapshot()["stats"]["submitted"] == 5

    def test_snapshot_is_copy(self):
        from watch_tui import WatchState
        s = WatchState()
        snap1 = s.snapshot()
        s.add_log("new")
        snap2 = s.snapshot()
        assert len(snap1["logs"]) == 0
        assert len(snap2["logs"]) == 1

    def test_source_submitted_cumulative(self):
        # 回归：提交卡片必须单调递增——多轮提交后累计不回落（曾被当轮数覆盖而骤降）
        from watch_tui import WatchState
        s = WatchState()
        s.add_source_submitted("github_search", 5)
        s.add_source_submitted("github_search", 8)
        s.add_source_submitted("docker", 3)
        assert s.source_total_submitted("github_search") == 13
        assert s.source_total_submitted("docker") == 3
        assert s.snapshot()["stats"]["submitted"] == 16
        # 零/负提交不改变累计
        s.add_source_submitted("github_search", 0)
        assert s.source_total_submitted("github_search") == 13

    def test_seed_from_history_seeds_broker_seen(self):
        # 回归：重启后必须用历史 key 播种 _seen，否则全量重验
        from watch_tui import VerificationBroker, WatchState

        class E:
            deepseek_api_base = "https://api.deepseek.com"
            timeout = 5
            usd_cny_rate = 7.25
            _proxies = None

        broker = VerificationBroker(engine=E())
        state = WatchState(broker=broker)
        history = [
            {"key": "sk-hist-1", "valid": True, "balance_cny": 5.0, "source": "old"},
            {"key": "sk-hist-2", "valid": True, "balance_cny": 0.5, "source": "old"},
            {"key": "sk-hist-3", "valid": False, "balance_cny": 0.0, "source": "old"},
        ]
        state.seed_from_history(history, broker=broker, min_balance=1.0)
        # 只播种高价值 key（>阈值）：普通历史 key 重新走验证（扫描器扫到即提交不被拒）
        assert "sk-hist-1" in broker._seen
        assert "sk-hist-2" not in broker._seen  # 低余额历史 key 重新验证
        assert "sk-hist-3" not in broker._seen  # invalid 重新验证
        # 仅高价值 key 回显到 TUI 表格
        assert [r["key"] for r in state.high_value_keys] == ["sk-hist-1"]

    def test_seed_from_history_sorts_by_balance(self):
        from watch_tui import WatchState
        s = WatchState()
        history = [
            {"key": "sk-lo", "valid": True, "balance_cny": 2.0},
            {"key": "sk-hi", "valid": True, "balance_cny": 99.0},
        ]
        s.seed_from_history(history, min_balance=1.0)
        assert [r["key"] for r in s.high_value_keys] == ["sk-hi", "sk-lo"]

    def test_hv_table_renders_time(self):
        # 回归：验证时间列必须显示 HH:MM:SS（曾误写 [-8] 单字符 → 恒显示 "0"）
        from watch_tui import WatchState, _render_hv_table
        s = WatchState()
        s.seed_from_history(
            [{"key": "sk-aaa", "valid": True, "balance_cny": 5.0,
              "verified_at": "2026-08-08 07:16:47", "source": "t"}],
            min_balance=1.0)
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=120, record=True).print(_render_hv_table(s.snapshot(), 0))
        assert "07:16:47" in buf.getvalue(), "验证时间列应显示 HH:MM:SS"

    def test_hv_table_renders_provider_column(self):
        # 多平台：高价值表必须显示平台列（kimi → 中文名"月之暗面"）
        from watch_tui import WatchState, _render_hv_table
        s = WatchState()
        s.seed_from_history(
            [{"key": "sk-aaa", "valid": True, "balance_cny": 5.0,
              "provider": "kimi", "source": "t"}],
            min_balance=1.0)
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=120, record=True).print(_render_hv_table(s.snapshot(), 0))
        out = buf.getvalue()
        assert "月之暗面" in out, f"平台列应显示 kimi 中文名，实际: {out[:200]}"
        assert "Kimi" in out

    def test_hv_table_unknown_provider_fallback(self):
        # 未知 provider 回退显示原 id，不崩溃
        from watch_tui import WatchState, _render_hv_table
        s = WatchState()
        s.seed_from_history(
            [{"key": "sk-aaa", "valid": True, "balance_cny": 5.0,
              "provider": "some_unknown_platform", "source": "t"}],
            min_balance=1.0)
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=200, record=True).print(_render_hv_table(s.snapshot(), 0))
        # 超长未知 id 被表格 ellipsis 截断是正常行为——断言前缀存在
        assert "some_unknown_platfo" in buf.getvalue()

    def test_provider_display_name_maps_cn(self):
        from watch_tui import _provider_display_name
        assert _provider_display_name("kimi") == "月之暗面 Kimi"
        assert _provider_display_name("deepseek") == "深度求索"
        assert _provider_display_name("no_such") == "no_such"  # 未知回退

    def test_render_verify_bar_status_distribution(self):
        # 验证条显示状态分布（有钱/零余额/无余额接口）
        from watch_tui import WatchState, _render_verify_bar
        s = WatchState()
        # 直接注入 broker 快照模拟（WatchState 无 broker 时用默认空统计）
        snap = s.snapshot()
        snap["verify"] = {
            "submitted": 10, "pending": 2, "verified": 8, "valid": 3,
            "hv": 1, "rate_per_min": 5, "queue_size": 2,
            "provider_counts": {"deepseek": 5, "kimi": 3},
            "status_counts": {"valid_active": 1, "valid_zero": 2,
                              "valid_no_balance": 3, "invalid": 2},
        }
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=140, record=True).print(_render_verify_bar(snap))
        out = buf.getvalue()
        assert "有钱:1" in out
        assert "零余额:2" in out
        assert "无余额接口:3" in out

    def test_render_header_provider_distribution(self):
        from watch_tui import WatchState, _render_header
        s = WatchState()
        snap = s.snapshot()
        snap["verify"] = {
            "provider_counts": {"deepseek": 50, "kimi": 30, "qwen": 2},
        }
        import io

        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=140, record=True).print(_render_header(snap, 0))
        out = buf.getvalue()
        assert "深度求索" in out and "月之暗面" in out
        assert "qwen" in out or "通义" in out


# ── 平台查询优先排序（多平台新增量优先于 deepseek 存量）────────────

class TestPlatformQueryPriority:
    def test_is_platform_query_detection(self):
        from watch_tui import _is_platform_query
        # 平台查询：api_base 域名
        assert _is_platform_query("api.moonshot.cn sk- filename:py")
        assert _is_platform_query("dashscope.aliyuncs.com sk-")
        assert _is_platform_query("open.bigmodel.cn sk- filename:js")
        assert _is_platform_query("api.anthropic.com sk-ant-")
        # deepseek 基础查询：不含平台域名
        assert not _is_platform_query("deepseek sk- filename:env")
        assert not _is_platform_query("DEEPSEEK_API_KEY sk-")
        assert not _is_platform_query("deepseek sk- path:src/main/resources NOT test")

    def test_platform_queries_sorted_first(self):
        """回归：多平台查询必须排在 deepseek 前面——否则一轮 20-40 分钟
        跑不到平台查询（用户观察"只在扫 deepseek"的根因）。"""
        from watch_tui import _is_platform_query
        mixed = [
            "deepseek sk- filename:env",
            "api.moonshot.cn sk- filename:py",
            "DEEPSEEK_API_KEY sk-",
            "dashscope.aliyuncs.com sk-",
        ]
        plat = [q for q in mixed if _is_platform_query(q)]
        deep = [q for q in mixed if not _is_platform_query(q)]
        ordered = plat + deep
        assert ordered[0].startswith("api.moonshot")
        assert ordered[1].startswith("dashscope")
        assert ordered[2].startswith("deepseek")


# ── 多平台查询探索优先：首轮保底 → 之后统一收益机制 ────────────────

class TestPlatformExplorationPriority:
    def _tracker(self, tmp_path):
        from scanner_engine import QueryTracker
        return QueryTracker(stats_path=str(tmp_path / "q.json"))

    def test_unexplored_platform_queries_first(self, tmp_path):
        """回归：runs==0 的平台查询必须排最前（首轮保底建立收益）。
        否则 yield 排序下平台查询永远排 25+ 位，一轮跑不到（'只在扫 deepseek'）。"""
        from watch_tui import _is_platform_query
        t = self._tracker(tmp_path)
        # deepseek 查询跑过 3 轮且高产
        for _ in range(3):
            t.record("deepseek sk- filename:env", 30)
        qs = [
            "deepseek sk- filename:env",       # runs=3, yield=30
            "api.moonshot.cn sk- filename:py",  # runs=0 平台
            "DEEPSEEK_API_KEY sk-",             # runs=0 deepseek
        ]
        static = [q for q in qs if not t.is_barren(q)]
        unexplored_plat = [q for q in static
                           if _is_platform_query(q) and t.get_runs(q) == 0]
        explored = [q for q in static if q not in unexplored_plat]
        explored.sort(key=t.get_yield, reverse=True)
        ordered = unexplored_plat + explored
        # 未跑过的平台查询第一
        assert ordered[0] == "api.moonshot.cn sk- filename:py"

    def test_explored_platform_joins_yield_ranking(self, tmp_path):
        """跑过一次的平台查询 → 与 deepseek 统一收益排序（不再插队）。"""
        from watch_tui import _is_platform_query
        t = self._tracker(tmp_path)
        # 平台查询已跑过且高产
        t.record("api.moonshot.cn sk- filename:py", 20)
        t.record("api.moonshot.cn sk- filename:py", 20)
        # deepseek 低产
        t.record("deepseek sk- filename:env", 1)
        qs = ["deepseek sk- filename:env", "api.moonshot.cn sk- filename:py"]
        static = [q for q in qs if not t.is_barren(q)]
        unexplored_plat = [q for q in static
                           if _is_platform_query(q) and t.get_runs(q) == 0]
        explored = [q for q in static if q not in unexplored_plat]
        explored.sort(key=t.get_yield, reverse=True)
        ordered = unexplored_plat + explored
        # 平台查询已跑过 → 按收益排在 deepseek 低产前
        assert ordered[0] == "api.moonshot.cn sk- filename:py"
        assert t.get_yield("api.moonshot.cn sk- filename:py") == 20.0

    def test_get_runs_tracks_executions(self, tmp_path):
        t = self._tracker(tmp_path)
        assert t.get_runs("never_ran") == 0
        t.record("q1", 5)
        t.record("q1", 0)
        assert t.get_runs("q1") == 2


# ── 看门狗心跳（全线程卡死自动重启）───────────────────────────────

class TestWatchdogHeartbeat:
    def test_add_log_refreshes_last_activity(self):
        from watch_tui import WatchState
        s = WatchState()
        t0 = s.last_activity
        time.sleep(0.01)
        s.add_log("test")
        assert s.last_activity > t0

    def test_last_activity_initialized(self):
        from watch_tui import WatchState
        s = WatchState()
        assert s.last_activity > 0

    def test_watchdog_judgement_logic(self):
        """卡死判据：idle 超阈值 → 重启；正常活动 → 不重启。"""
        from watch_tui import WatchState
        s = WatchState()
        # 模拟静止 400s（阈值 300s）→ 应判卡死
        s._last_activity = time.time() - 400
        assert time.time() - s.last_activity > 300
        # 模拟刚有活动 → 不应判卡死
        s.add_log("alive")
        assert time.time() - s.last_activity < 5


# ── 外部源多平台搜索词轮换 ─────────────────────────────────────────

class TestExternalSearchTermRotation:
    def test_pool_covers_all_platforms(self):
        from watch_tui import _PLATFORM_SEARCH_POOL
        all_terms = [t for pool in _PLATFORM_SEARCH_POOL for t in pool]
        # 覆盖所有主流平台词
        for kw in ["deepseek", "kimi", "moonshot", "qwen", "dashscope",
                   "zhipu", "bigmodel", "claude", "anthropic", "minimax",
                   "doubao", "volces", "baichuan"]:
            assert any(kw in t for t in all_terms), f"缺平台词: {kw}"

    def test_rotation_cycles_platforms(self):
        from watch_tui import _PLATFORM_SEARCH_POOL
        # 轮次取模 → 每个平台都会被选到
        seen = {i % len(_PLATFORM_SEARCH_POOL) for i in range(32)}
        assert seen == set(range(len(_PLATFORM_SEARCH_POOL)))


# ── 外部源统一平台词轮换（gitee 曾固定 deepseek 词）─────────────────

class TestExternalTermRotationAllSources:
    def test_all_external_sources_rotate_platform_terms(self, tmp_path):
        """回归：所有外部源（含 gitee）必须走平台词轮换，不能固定 deepseek。
        SOURCE_SEARCH_TERMS 只作补充词（HF 泄露特征词等）。"""
        from watch_tui import _PLATFORM_SEARCH_POOL
        # 平台词池必须覆盖主流平台
        all_terms = [t for pool in _PLATFORM_SEARCH_POOL for t in pool]
        joined = " ".join(all_terms)
        for kw in ["deepseek", "kimi", "qwen", "zhipu", "claude", "minimax"]:
            assert kw in joined, f"平台词池缺 {kw}"
        # 轮换覆盖：8 组词池，8 轮全轮换
        seen = {i % len(_PLATFORM_SEARCH_POOL) for i in range(16)}
        assert seen == set(range(len(_PLATFORM_SEARCH_POOL)))

    def test_hf_has_leak_feature_terms(self):
        from watch_tui import SOURCE_SEARCH_TERMS
        hf = SOURCE_SEARCH_TERMS.get("huggingface", [])
        joined = " ".join(hf)
        assert any(k in joined for k in ["free endpoint", "api key", "proxy"]), \
            "HF 应有泄露特征词（free endpoint/proxy 类 space 是泄露区）"

    def test_gitlab_on_by_default_configurable_via_sources(self):
        from watch_tui import DEFAULT_WATCH_SOURCES
        # 2026-08: gitlab blob 搜索优化后实测有产出(255 项目→1 key),默认启用
        assert "gitlab" in DEFAULT_WATCH_SOURCES
        assert "github_search" in DEFAULT_WATCH_SOURCES
        assert "npm" in DEFAULT_WATCH_SOURCES  # only productive external source


def test_default_watch_sources_includes_commits():
    from watch_tui import DEFAULT_WATCH_SOURCES
    assert "github_commits" in DEFAULT_WATCH_SOURCES


class TestRotationBuckets:
    def test_compat_terms_present(self):
        from watch_tui import _COMPAT_TERMS
        joined = " ".join(_COMPAT_TERMS)
        for kw in ["api-key", "chatbot", "gradio", "proxy", "free"]:
            assert kw in joined, f"兼容词缺 {kw}"

    def test_cn_alias_pool_present(self):
        from watch_tui import _CN_ALIAS_POOL
        joined = " ".join(_CN_ALIAS_POOL)
        for kw in ["智谱", "月之暗面", "通义千问", "豆包", "百川", "deepseek"]:
            assert kw in joined, f"中文别名缺 {kw}"

    def test_rotation_flattens_all_buckets(self):
        from watch_tui import _CN_ALIAS_POOL, _COMPAT_TERMS, _PLATFORM_SEARCH_POOL, _ROTATION
        all_platform = [w for pool in _PLATFORM_SEARCH_POOL for w in pool]
        for w in all_platform[:3]:
            assert w in _ROTATION
        for w in _COMPAT_TERMS[:3]:
            assert w in _ROTATION
        for w in _CN_ALIAS_POOL[:3]:
            assert w in _ROTATION
        assert "sk-" in _ROTATION

    def test_rotation_one_term_per_round(self):
        from watch_tui import _ROTATION
        seen = {i % len(_ROTATION) for i in range(len(_ROTATION) * 2)}
        assert seen == set(range(len(_ROTATION)))


class TestScanExternalSingleTerm:
    def test_one_term_per_round_from_rotation(self):
        """每轮从 _ROTATION 取 1 个词，不叠加整桶。"""
        import watch_tui
        terms_seen = []

        class StubEngine:
            def _run_one_scanner(self, src, queries=None, github_token=""):
                terms_seen.append(list(queries))
                return []

        broker = watch_tui.VerificationBroker.__new__(watch_tui.VerificationBroker)
        broker.submit_many = lambda keys, source="": 0

        class StubState:
            logs = []
            def add_log(self, msg, level="info"): self.logs.append(msg)
            should_exit = False

        scanner = watch_tui.WatchScanner.__new__(watch_tui.WatchScanner)
        scanner.state = StubState()
        scanner.broker = broker
        for r in (1, 2, 3):
            scanner._scan_external("huggingface", StubEngine(), "", source_round=r)
        for terms in terms_seen:
            assert len(terms) == 1, f"每轮应 1 词，实际 {terms}"
        flat = [t[0] for t in terms_seen]
        assert len(set(flat)) == 3


def test_enqueue_reverify_bypasses_queued():
    """持续重验不受 _reverify_queued 单次限制;在途 key 不重复入队。"""
    import queue

    from watch_tui import VerificationBroker

    broker = VerificationBroker.__new__(VerificationBroker)
    broker._queue = queue.PriorityQueue()
    broker._counter = 0
    broker._counter_lock = __import__("threading").Lock()
    broker._seen = set()
    broker._seen_lock = __import__("threading").Lock()
    broker._reverify_queued = {"sk-old"}
    broker._reverify_max = 100_000
    broker._stop = False
    broker._in_flight = set()
    broker._in_flight_lock = __import__("threading").Lock()

    assert broker.enqueue_reverify("sk-new", source="history") is True
    assert broker.enqueue_reverify("sk-new", source="history") is False  # 在途
    # 不受 _reverify_queued 限制:sk-old 已在启动重验名单中,仍可入队
    assert broker.enqueue_reverify("sk-old", source="history") is True
    assert broker.enqueue_reverify("sk-other", source="history") is True
    assert broker._queue.qsize() == 3


# ── 余额变化检测 + balance_changes.csv ────────────────────────────

def test_detect_balance_change():
    from watch_tui import detect_balance_change

    # 缩水: >30% 且当前 >= 重高价值阈值
    prev = {"balance_cny": 50.0, "valid": 1}
    cur = {"balance_cny": 30.0, "valid": True, "key": "sk-x"}
    assert detect_balance_change(prev, cur, 5.0, 10.0, 30.0) == ("shrink", True)
    # 缩水但未达重高价值 → 不邮件
    prev2 = {"balance_cny": 8.0, "valid": 1}
    cur2 = {"balance_cny": 5.0, "valid": True, "key": "sk-y"}
    assert detect_balance_change(prev2, cur2, 5.0, 10.0, 30.0)[1] is False
    # 充值: 涨 >50% 且 >= 邮件阈值
    prev3 = {"balance_cny": 2.0, "valid": 1}
    cur3 = {"balance_cny": 8.0, "valid": True, "key": "sk-z"}
    assert detect_balance_change(prev3, cur3, 5.0, 10.0, 30.0) == ("refill", True)
    # 重新激活: 上次无效 → 现在有效
    prev4 = {"balance_cny": 0.0, "valid": 0}
    cur4 = {"balance_cny": 6.0, "valid": True, "key": "sk-w"}
    assert detect_balance_change(prev4, cur4, 5.0, 10.0, 30.0) == ("reactivated", True)
    # 微小变化 → other, 不邮件
    prev5 = {"balance_cny": 7.0, "valid": 1}
    cur5 = {"balance_cny": 7.5, "valid": True, "key": "sk-v"}
    assert detect_balance_change(prev5, cur5, 5.0, 10.0, 30.0) == ("other", False)
    # 无历史 → None
    assert detect_balance_change(None, cur, 5.0, 10.0, 30.0) == (None, False)


def test_log_balance_change(tmp_path):
    import csv

    from watch_tui import log_balance_change
    p = tmp_path / "changes.csv"
    log_balance_change(str(p), {"key": "sk-a", "balance_cny": 8.0, "status": "valid_active"},
                       {"balance_cny": 5.0}, "refill")
    with open(p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["change_type"] == "refill"
    assert rows[0]["balance_cny"] == "8.0"
    assert rows[0]["prev_balance"] == "5.0"


# ── C1: 变化检测接线(_store_result reverify 路径) ──────────────────

class _FakeNotifier:
    """假邮件通知器:记录 send_alert 调用。"""

    enabled = True

    def __init__(self):
        self.alerts = []

    def send_alert(self, **kw):
        self.alerts.append(kw)


def _mk_broker_with_store(tmp_path):
    """带 SQLite store + 假通知器 + 独立 changes CSV 的 broker。"""
    class MockEngine:
        deepseek_api_base = "https://api.deepseek.com"
        timeout = 5
        usd_cny_rate = 7.25
        _proxies = None

    db = tmp_path / "t.db"
    changes_csv = tmp_path / "changes.csv"
    broker = VerificationBroker(
        engine=MockEngine(), min_balance=1.0, interval=0.05, workers=1,
        db_path=str(db), email_notifier=_FakeNotifier(),
        hv_email_threshold=5.0, hv_top_threshold=10.0, shrink_warn_pct=30.0,
        changes_csv=str(changes_csv))
    return broker, changes_csv


def _seed_history(broker, key, cny):
    """往 key_history 写一笔历史(模拟上次重验)。"""
    import store as _store
    with broker._store_lock:
        _store.record_history(broker._store_conn,
                              {"key": key, "valid": True, "balance_cny": cny,
                               "status": "valid_active"})


def test_store_result_reverify_detects_shrink_and_emails(tmp_path):
    """C1: 持续重验结果写历史前读 prev → 缩水 >30% 且 >=10 元 → CSV + 预警邮件。"""
    import csv
    broker, changes_csv = _mk_broker_with_store(tmp_path)
    _seed_history(broker, "sk-shrink", 50.0)
    notifier = broker._email_notifier
    broker._store_result({"key": "sk-shrink", "valid": True, "balance_cny": 30.0,
                          "provider": "deepseek", "source": "history",
                          "repos": [{"repo": "a/b", "file": "c"}],
                          "key_preview": "sk-shrink"}, reverify_src=True)
    with open(changes_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["change_type"] == "shrink"
    assert rows[0]["balance_cny"] == "30.0"
    assert rows[0]["prev_balance"] == "50.0"
    assert rows[0]["delta"] == "-20.0"
    assert len(notifier.alerts) == 1, "缩水达重高价值阈值应发预警邮件"
    assert notifier.alerts[0]["key"] == "sk-shrink"
    assert notifier.alerts[0]["balance"] == 30.0
    assert notifier.alerts[0]["repos"] == [{"repo": "a/b", "file": "c"}]


def test_store_result_sub_threshold_change_csv_only(tmp_path):
    """缩水但当前余额未达重高价值阈值(>=10 元)→ 只记 CSV,不发邮件。"""
    import csv
    broker, changes_csv = _mk_broker_with_store(tmp_path)
    _seed_history(broker, "sk-small", 8.0)
    notifier = broker._email_notifier
    broker._store_result({"key": "sk-small", "valid": True, "balance_cny": 5.0,
                          "provider": "d", "source": "history", "repos": [],
                          "key_preview": "s"}, reverify_src=True)
    with open(changes_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["change_type"] == "shrink"
    assert notifier.alerts == []


def test_store_result_refill_detected(tmp_path):
    """充值: 涨 >50% 且 >=5 元 → CSV + 邮件。"""
    import csv
    broker, changes_csv = _mk_broker_with_store(tmp_path)
    _seed_history(broker, "sk-refill", 2.0)
    notifier = broker._email_notifier
    broker._store_result({"key": "sk-refill", "valid": True, "balance_cny": 8.0,
                          "provider": "d", "source": "history", "repos": [],
                          "key_preview": "r"}, reverify_src=True)
    with open(changes_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["change_type"] == "refill"
    assert len(notifier.alerts) == 1


def test_store_result_new_scan_no_prev_no_detection(tmp_path):
    """新扫描路径(非重验)不做变化检测:无 CSV 行、无邮件。

    注意:新扫描的高价值 key 走"即时发信"分支(既有行为),此处用非 HV
    余额(0.5)排除该分支,只验证变化检测不触发。
    """
    import os
    broker, changes_csv = _mk_broker_with_store(tmp_path)
    notifier = broker._email_notifier
    # 1. 首次发现(无历史)→ 无 CSV、无邮件
    broker._store_result({"key": "sk-first", "valid": True, "balance_cny": 0.5,
                          "provider": "d", "source": "github_search", "repos": [],
                          "key_preview": "f"})
    assert not os.path.exists(changes_csv), "无 prev 不写变化账本"
    assert notifier.alerts == []
    # 2. 有历史但走新扫描路径(reverify_src=False)→ 仍不触发变化检测
    _seed_history(broker, "sk-first", 50.0)
    broker._store_result({"key": "sk-first", "valid": True, "balance_cny": 0.5,
                          "provider": "d", "source": "github_search", "repos": [],
                          "key_preview": "f"})
    assert not os.path.exists(changes_csv), "新扫描路径不触发变化检测"
    assert notifier.alerts == []


def test_docker_not_in_default_sources():
    """docker 历史产出仅 1 行,不应占默认源;github_events 应在默认源。"""
    from watch_tui import DEFAULT_WATCH_SOURCES
    assert "docker" not in DEFAULT_WATCH_SOURCES
    assert "github_events" in DEFAULT_WATCH_SOURCES


def test_history_loaded_once_not_per_save(tmp_path, monkeypatch):
    """_save_from_broker 不应每次都调 load_history 读盘(随账本线性增长的 I/O)。"""
    import watch_tui as w
    calls = {"n": 0}
    orig = w.load_history
    def counting(d):
        calls["n"] += 1
        return orig(d)
    monkeypatch.setattr(w, "load_history", counting)
    state = w.WatchState()
    broker = w.VerificationBroker(engine=None, db_path=None)
    sc = w.WatchScanner(state=state, broker=broker, output_dir=str(tmp_path))
    assert calls["n"] == 1  # 启动构造时载一次
    for _ in range(3):
        sc._save_from_broker(force=True)
    assert calls["n"] == 1, f"缓存化后不应重复读盘,实际 {calls['n']}"


def test_csv_provider_backfilled_from_history(tmp_path):
    """旧 CSV 行平台字段空 → 从缓存里的 DB provider 回填。"""
    import watch_tui as w
    state = w.WatchState()
    broker = w.VerificationBroker(engine=None, db_path=None)
    sc = w.WatchScanner(state=state, broker=broker, output_dir=str(tmp_path))
    sc._history_map = {"sk-deadkey123": {"key": "sk-deadkey123", "valid": True,
                                          "balance_cny": 0.0, "provider": "deepseek",
                                          "source": "github_search"}}
    sc._provider_hints = {"sk-deadkey123": "deepseek"}
    sc._save_from_broker(force=True)
    import csv
    rows = list(csv.reader(open(sc.csv_path, encoding="utf-8")))
    for r in rows:
        if "sk-deadkey123" in r:
            assert r[2] == "deepseek", f"平台列应回填 deepseek,实际 {r[2]!r}"
            return
    assert False, "CSV 里应能找到该 key 行"


# ── v2.4.3: broker 拒收非推理类平台 token ────────────────────────────

def test_submit_rejects_non_inference_tokens(tmp_path):
    """hf_/ghp_ 等平台凭据不属于 AI 推理 key,直接拒收不占队列(曾积累 1364 unknown)。"""
    import watch_tui as w
    broker = w.VerificationBroker(engine=None, db_path=None)
    for bad in ("hf_" + "aB3dE7" * 5, "ghp_" + "aB3dE7" * 5, "github_pat_" + "aB3dE7" * 5):
        assert broker.submit(bad, source="github_search") is False, bad
    assert broker._queue.qsize() == 0
    # 正常 key 不受影响
    assert broker.submit("sk-" + "aBcD" * 9, source="github_search") is True


# ── v2.4.3: CSV 排序对非数字余额列容错 ───────────────────────────────

def test_write_watch_csv_tolerates_bad_balance_cell(tmp_path):
    """手改/旧 schema 的 CSV 余额列非数字时不得炸掉落盘(曾致台账永久停更)。"""
    import csv as _csv

    import watch_persistence as wp
    csv_path = str(tmp_path / "hv.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(wp._WATCH_CSV_HEADER)
        writer.writerow(["bad", "sk-badcell", "kimi", "not-a-number", "", "", "",
                         "", "", "", "", "valid"])
    wp.write_watch_csv(csv_path, [])
    rows = list(_csv.reader(open(csv_path, encoding="utf-8")))
    assert any("sk-badcell" in r for r in rows), "坏行应被保留并参与排序"


# ── v2.4.4: PERCENT 币种(Coding Plan 周额度)不被重验剔除 ─────────────

def test_percent_key_not_evicted_on_low_balance():
    """GLM Coding Plan key 周额度 0%(balance=0):重验后不剔除——额度每周重置,
    记录比剔除有价值(用户明确要求 0% 也要留)。"""
    b = VerificationBroker(engine=None, db_path=None, min_balance=1.0)
    b.reverify("78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp", source="history")
    b._store_result({"key": "78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp",
                     "valid": True, "balance_cny": 0.0, "status": "valid_zero",
                     "primary_currency": "PERCENT"})
    assert "78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp" not in b.evicted_keys()
    # 对照:CNY 0 余额仍按原语义剔除
    b2 = VerificationBroker(engine=None, db_path=None, min_balance=1.0)
    b2.reverify("sk-cny-zero-111111111111111111111111111", source="history")
    b2._store_result({"key": "sk-cny-zero-111111111111111111111111111",
                      "valid": True, "balance_cny": 0.0, "status": "valid_zero",
                      "primary_currency": "CNY"})
    assert "sk-cny-zero-111111111111111111111111111" in b2.evicted_keys()


# ── v2.4.5: 提交计数区分"复检已知"与"真·新发现" ─────────────────────

def test_genuinely_new_counter_separates_refinds(tmp_path):
    """提交计数是会话内新见(含复检);真·新发现 = DB 首次入库。"""
    import store as _store
    conn = _store.connect(str(tmp_path / "t.db"))
    b = VerificationBroker(engine=None, db_path=None)
    b._store_conn = conn
    b._store_lock = __import__("threading").Lock()
    key = "sk-" + "aBcD9fGh2" * 4
    b.submit(key, source="github_search")
    b._store_result({"key": key, "valid": True, "balance_cny": 5.0})   # 首次入库
    b._store_result({"key": key, "valid": True, "balance_cny": 4.0})   # 复检已知
    assert b.snapshot()["submitted"] == 1          # 会话内只见到 1 次
    assert b.snapshot()["genuinely_new"] == 1      # DB 净新增 1
    assert b.snapshot()["submitted"] == b.snapshot()["genuinely_new"]
    conn.close()


# ── v2.5.4 审查修复回归 ─────────────────────────────────────────────

class TestV254BrokerEvictedRefill:
    def test_refill_removes_evicted(self):
        """余额回充越过阈值必须解除 _evicted 剔除——否则 state/CSV/TUI
        在本会话内永远看不到回充的 key,"充值检测"形同虚设。"""
        b = VerificationBroker(engine=None, db_path=None)
        k = "sk-" + "9" * 40
        b._evicted.add(k)
        b._store_result({"key": k, "valid": True, "status": "valid_active",
                         "balance_cny": 8.0, "source": "github_search"})
        assert k not in b._evicted

    def test_invalid_result_cleans_stale_row(self):
        """确定性 invalid 必须清掉 _results 里的旧 valid 行——否则四条
        清理路径全部够不到,台账永久保留过期"有效"行。"""
        b = VerificationBroker(engine=None, db_path=None)
        k = "sk-" + "8" * 40
        b._results[k] = {"key": k, "valid": True, "balance_cny": 2.0}
        b._store_result({"key": k, "valid": False, "status": "invalid",
                         "source": "github_search"})
        assert k not in b._results


class TestV254WatchStateTotals:
    def test_total_cny_excludes_percent(self):
        """周额度%(PERCENT)不是钱——TUI 总值合计必须排除(阈值仍按百分点)。"""
        from watch_tui import WatchState
        ws = WatchState()
        ws.set_high_value_keys([
            {"key": "k1", "valid": True, "balance_cny": 85.0,
             "primary_currency": "PERCENT"},
            {"key": "k2", "valid": True, "balance_cny": 8.0,
             "primary_currency": "CNY"},
        ])
        snap = ws.snapshot()
        assert snap["stats"]["total_cny"] == 8.0


class TestV254WatchPersistenceBak:
    def test_load_prefers_main_over_stale_bak(self, tmp_path):
        """主文件是原子写,永远优先;只有主文件缺失/损坏才回退 .bak
        (旧「bak 优先」会把 copy 失败轮留下的旧备份当最新状态回滚)。"""
        from watch_tui import load_watch_state, save_watch_state
        path = str(tmp_path / "watch_state.json")
        save_watch_state(path, [{"key": "k-new", "valid": True, "balance_cny": 5.0}])
        (tmp_path / "watch_state.json.bak").write_text(
            '{"keys": {"k-old": {"key": "k-old", "valid": true}}}', encoding="utf-8")
        got = load_watch_state(path)
        assert [r["key"] for r in got] == ["k-new"]

    def test_load_falls_back_to_bak_when_main_corrupt(self, tmp_path):
        from watch_tui import load_watch_state
        (tmp_path / "watch_state.json").write_text("{truncated", encoding="utf-8")
        (tmp_path / "watch_state.json.bak").write_text(
            '{"keys": {"k-bak": {"key": "k-bak", "valid": true}}}', encoding="utf-8")
        got = load_watch_state(str(tmp_path / "watch_state.json"))
        assert [r["key"] for r in got] == ["k-bak"]

    def test_save_survives_bak_copy_failure(self, tmp_path, monkeypatch):
        """bak copy 失败(磁盘满/权限)不得让整次保存抛异常炸掉主循环。"""
        import shutil as _shutil

        from watch_tui import load_watch_state, save_watch_state

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(_shutil, "copy2", boom)
        path = str(tmp_path / "watch_state.json")
        save_watch_state(path, [{"key": "k1", "valid": True}])
        assert [r["key"] for r in load_watch_state(path)] == ["k1"]
