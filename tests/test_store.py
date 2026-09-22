"""store.py SQLite key store 测试。"""
from __future__ import annotations

import threading

import store

KEY = "sk-" + "aBcD9fGh2" * 4


def _result(key=KEY, cny=10.0, valid=True, provider="deepseek"):
    return {
        "key": key,
        "balance_cny": cny,
        "valid": valid,
        "provider": provider,
        "source": "github",
        "repos": [{"repo": "acme/x", "file": "config.env", "url": "https://gh/x"}],
        "verified_at": "2026-08-08 10:00:00",
    }


class TestUpsert:
    def test_new_key_returns_true(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        assert store.upsert(conn, _result()) is True

    def test_duplicate_returns_false_and_updates_last_seen(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        store.upsert(conn, _result(cny=5.0))
        # 同 key 再次 upsert（余额变化）→ 不是新 key，余额应更新
        is_new = store.upsert(conn, _result(cny=20.0))
        assert is_new is False
        rows = store.query(conn)
        assert len(rows) == 1
        assert rows[0]["balance"] == 20.0

    def test_update_refreshes_provider_on_reverify(self, tmp_path):
        """回归：历史行 provider 曾写 source（'github_search'），重新验证后应更新
        为识别出的平台——否则 db 永远挂着过时 provider。"""
        conn = store.connect(str(tmp_path / "k.db"))
        store.upsert(conn, _result(provider="github_search"))
        rows = store.query(conn)
        assert rows[0]["provider"] == "github_search"
        # 重新验证：识别为 kimi → provider 必须刷新
        store.upsert(conn, _result(provider="kimi", cny=3.0))
        rows = store.query(conn)
        assert len(rows) == 1
        assert rows[0]["provider"] == "kimi"
        assert rows[0]["balance"] == 3.0

    def test_update_refreshes_status(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        store.upsert(conn, _result())
        store.upsert(conn, {**_result(), "valid": False, "status": "invalid"})
        rows = store.query(conn)
        assert rows[0]["status"] == "invalid"
        assert rows[0]["valid"] == 0

    def test_distinct_keys_both_stored(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        store.upsert(conn, _result(key="sk-" + "a" * 36, cny=1))
        store.upsert(conn, _result(key="sk-" + "b" * 36, cny=2))
        assert len(store.query(conn)) == 2


class TestQuery:
    def test_filter_by_min_balance_and_valid(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        store.upsert(conn, _result(key="sk-" + "a" * 36, cny=5, valid=True))
        store.upsert(conn, _result(key="sk-" + "b" * 36, cny=0, valid=True))
        store.upsert(conn, _result(key="sk-" + "c" * 36, cny=50, valid=False))
        # min_balance 过滤掉 cny=0；invalid 不受 min_balance 影响(valid_only 未开)
        hi = store.query(conn, min_balance=1.0)
        assert {r["key"] for r in hi} == {"sk-" + "a" * 36, "sk-" + "c" * 36}
        valid_only = store.query(conn, valid_only=True, min_balance=0.0)
        assert {r["key"] for r in valid_only} == {"sk-" + "a" * 36, "sk-" + "b" * 36}

    def test_ordered_by_balance_desc(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        for k, cny in [("a", 1), ("b", 30), ("c", 5)]:
            store.upsert(conn, _result(key=f"sk-{k*36}", cny=cny))
        rows = store.query(conn)
        balances = [r["balance"] for r in rows]
        assert balances == sorted(balances, reverse=True)


class TestHashKey:
    def test_stable_and_distinct(self):
        a = store.hash_key("sk-same")
        assert a == store.hash_key("sk-same")
        assert a != store.hash_key("sk-other")


class TestCrossThreadWrite:
    """回归：Broker 连接在主线程创建、worker 线程写入 —— 必须跨线程可用。

    修复前 check_same_thread=True 抛 ProgrammingError 且被调用方 except pass 吞掉，
    导致 darkforest.db 永远 0 行（跨运行去重失效，重启后全量重验）。
    """

    def test_write_from_other_thread(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))  # 主线程创建（同 Broker.__init__）
        errs = []

        def work():
            try:
                store.upsert(conn, _result(key="sk-" + "a" * 36, cny=3.0))
            except Exception as e:  # 记录异常而非吞掉
                errs.append(e)

        t = threading.Thread(target=work)
        t.start()
        t.join()
        assert not errs, f"worker 线程 upsert 不应抛异常: {errs}"
        assert store.query(conn)[0]["key"] == "sk-" + "a" * 36

    def test_concurrent_writes_no_corruption(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        lock = threading.Lock()  # 与 Broker._store_lock 相同的串行化方式

        def work(i: int):
            try:
                with lock:
                    store.upsert(conn, _result(key=f"sk-{chr(97+i)*36}", cny=float(i)))
            except Exception as e:
                raise AssertionError(f"并发 upsert 失败: {e}") from e

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = store.query(conn)
        assert len(rows) == 4  # 无丢失、无损坏


class TestInvalidKeyRedaction:
    def test_new_invalid_key_is_redacted(self, tmp_path):
        """确定无效的 key 只需保留 hash 供跨运行统计，不需要明文驻留。"""
        conn = store.connect(str(tmp_path / "k.db"))
        invalid = {**_result(), "valid": False, "status": "invalid"}
        assert store.upsert(conn, invalid) is True

        row = conn.execute(
            "SELECT key, key_preview FROM keys WHERE key_hash=?",
            (store.hash_key(KEY),)).fetchone()
        assert row["key"] == store.INVALID_REDACTED_KEY
        assert row["key_preview"] == "[invalid]"

    def test_transient_error_keeps_key_for_recovery(self, tmp_path):
        """error/rate_limited 可能是误判，保留明文便于同一连接内后续恢复。"""
        conn = store.connect(str(tmp_path / "k.db"))
        transient = {**_result(), "valid": False, "status": "error"}
        store.upsert(conn, transient)
        rows = conn.execute("SELECT key FROM keys").fetchall()
        assert rows[0]["key"] == KEY

    def test_invalid_to_valid_transition_restores_key(self, tmp_path):
        """平台数据修正或 key 恢复时，后续 valid 结果必须恢复完整 key。"""
        conn = store.connect(str(tmp_path / "k.db"))
        store.upsert(conn, {**_result(), "valid": False, "status": "invalid"})
        store.upsert(conn, _result(cny=8.0))

        row = conn.execute(
            "SELECT key, valid, balance FROM keys WHERE key_hash=?",
            (store.hash_key(KEY),)).fetchone()
        assert row["key"] == KEY
        assert row["valid"] == 1
        assert row["balance"] == 8.0

    def test_redact_invalid_keys_migrates_existing_rows(self, tmp_path):
        """旧库存量 invalid 明文可在启动维护时一次性脱敏。"""
        conn = store.connect(str(tmp_path / "k.db"))
        conn.execute(
            "INSERT INTO keys (key_hash,key,key_preview,valid,status,first_seen,last_seen)"
            " VALUES (?,?,?,?,?,?,?)",
            (store.hash_key(KEY), KEY, "[preview]", 0, "invalid",
             "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
        conn.commit()

        changed = store.redact_invalid_keys(conn)
        assert changed == 1
        row = conn.execute(
            "SELECT key,key_preview FROM keys WHERE key_hash=?",
            (store.hash_key(KEY),)).fetchone()
        assert row["key"] == store.INVALID_REDACTED_KEY
        assert row["key_preview"] == "[invalid]"


class TestInvalidRetention:
    def _insert(self, conn, key, *, valid, status, seen):
        conn.execute(
            "INSERT INTO keys "
            "(key_hash,key,key_preview,valid,status,first_seen,last_seen)"
            " VALUES (?,?,?,?,?,?,?)",
            (store.hash_key(key), key, "[preview]", int(valid), status,
             seen, seen))

    def test_prunes_only_old_invalid_keys(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        self._insert(conn, "sk-old-invalid", valid=False, status="invalid",
                     seen="2020-01-01 00:00:00")
        self._insert(conn, "sk-new-invalid", valid=False, status="invalid",
                     seen="2026-08-29 00:00:00")
        self._insert(conn, "sk-old-valid", valid=True, status="valid_active",
                     seen="2020-01-01 00:00:00")

        assert store.prune_invalid_older_than(conn, days=90) == 1
        remaining = {row["key"] for row in conn.execute("SELECT key FROM keys")}
        assert remaining == {"[invalid:redacted]", "sk-old-valid"}

    def test_prunes_orphan_history_for_deleted_keys(self, tmp_path):
        """invalid key 可能有早先 valid 余额历史；删除 key 时必须一并清理。"""
        conn = store.connect(str(tmp_path / "k.db"))
        key = "sk-old-with-history"
        conn.execute(
            "INSERT INTO keys "
            "(key_hash,key,key_preview,valid,status,first_seen,last_seen)"
            " VALUES (?,?,?,?,?,?,?)",
            (store.hash_key(key), key, "[preview]", 0, "invalid",
             "2020-01-01 00:00:00", "2020-01-01 00:00:00"))
        conn.execute(
            "INSERT INTO key_history "
            "(key_hash,verified_at,balance_cny,status,valid)"
            " VALUES (?,?,?,?,?)",
            (store.hash_key(key), "2020-01-01 00:00:00", 10.0,
             "valid_active", 1))
        conn.commit()

        assert store.prune_invalid_older_than(conn, days=90) == 1
        assert conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM key_history").fetchone()[0] == 0


def test_maintenance_command_redacts_and_prunes(tmp_path, capsys):
    """run.py maintenance 是显式清理入口，先脱敏再删除过期 invalid。"""
    import sqlite3
    from types import SimpleNamespace

    from run import cmd_maintenance

    db = tmp_path / "k.db"
    conn = sqlite3.connect(db)
    conn.executescript(store._SCHEMA)
    conn.execute(
        "INSERT INTO keys "
        "(key_hash,key,key_preview,valid,status,first_seen,last_seen)"
        " VALUES (?,?,?,?,?,?,?)",
        (store.hash_key(KEY), KEY, "[preview]", 0, "invalid",
         "2020-01-01 00:00:00", "2020-01-01 00:00:00"))
    conn.commit()
    conn.close()

    cmd_maintenance(SimpleNamespace(
        db=str(db), prune_invalid_days=90, vacuum=True))
    out = capsys.readouterr().out
    assert "redacted=1" in out
    assert "pruned=1" in out
    assert "vacuum=ok" in out


class TestLedgerMetrics:
    def test_query_column_is_migrated_and_persisted(self, tmp_path):
        """旧 SQLite 账本升级后必须支持 query 归因，不用重建丢数据。"""
        import sqlite3

        db = tmp_path / "legacy.db"
        old = sqlite3.connect(db)
        old.executescript("""
            CREATE TABLE keys (
                key_hash TEXT PRIMARY KEY,
                key TEXT NOT NULL,
                key_preview TEXT,
                provider TEXT,
                source TEXT,
                repo TEXT,
                file TEXT,
                url TEXT,
                valid INTEGER,
                balance REAL,
                currency TEXT,
                status TEXT,
                first_seen TEXT,
                last_seen TEXT
            );
        """)
        old.commit()
        old.close()

        conn = store.connect(db)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(keys)")}
        assert "query" in columns

        result = {**_result(key="sk-" + "5" * 36, cny=3.0),
                  "source": "github_search", "provider": "deepseek",
                  "query": "MOONSHOT_API_KEY sk- filename:yml"}
        store.upsert(conn, result)
        row = conn.execute("SELECT query FROM keys").fetchone()
        assert row["query"] == "MOONSHOT_API_KEY sk- filename:yml"

    def test_ledger_metrics_are_aggregate_only(self, tmp_path):
        """metrics 必须能量化各源/平台转化，但绝不能返回明文 key。"""
        conn = store.connect(str(tmp_path / "k.db"))
        store.upsert(conn, {**_result(key="sk-" + "1" * 36, cny=2.0),
                            "source": "npm", "provider": "deepseek"})
        store.upsert(conn, {**_result(key="sk-" + "2" * 36, cny=0.0),
                            "source": "npm", "provider": "deepseek"})
        store.upsert(conn, {**_result(key="sk-" + "3" * 36, cny=5.0, valid=False),
                            "status": "invalid",
                            "source": "npm", "provider": "deepseek"})
        store.upsert(conn, {**_result(key="sk-" + "6" * 36, cny=0.0),
                            "source": "github_search", "provider": "kimi",
                            "query": "MOONSHOT_API_KEY sk- filename:yml"})

        m = store.ledger_metrics(conn, min_balance=1.0)
        assert m["totals"] == {"candidates": 4, "valid": 3, "invalid": 1,
                               "high_value": 1, "high_value_balance": 2.0}
        by_source = {r["source"]: r for r in m["by_source"]}
        assert by_source["npm"]["candidates"] == 3
        assert by_source["npm"]["valid"] == 2
        assert by_source["npm"]["invalid"] == 1
        assert by_source["npm"]["high_value"] == 1
        assert by_source["npm"]["high_value_balance"] == 2.0
        by_provider = {r["provider"]: r for r in m["by_provider"]}
        assert by_provider["deepseek"]["candidates"] == 3
        assert by_provider["deepseek"]["high_value"] == 1
        by_query = {r["query"]: r for r in m["by_query"]}
        assert by_query["MOONSHOT_API_KEY sk- filename:yml"]["candidates"] == 1
        assert by_query["MOONSHOT_API_KEY sk- filename:yml"]["valid"] == 1
        assert m["status_counts"]["invalid"] == 1
        assert not any("key" in str(field).lower()
                       for section in (m["by_source"], m["by_provider"], m["by_query"])
                       for row in section for field in row)


def test_metrics_command_prints_aggregate_picture(tmp_path, capsys):
    """CLI metrics 是动态调参入口，输出聚合画像且不泄露凭据。"""
    from types import SimpleNamespace

    from run import cmd_metrics

    conn = store.connect(str(tmp_path / "k.db"))
    store.upsert(conn, {**_result(key="sk-" + "4" * 36, cny=8.0),
                        "source": "npm", "provider": "deepseek",
                        "query": "MOONSHOT_API_KEY sk- filename:yml"})
    conn.close()

    cmd_metrics(SimpleNamespace(db=str(tmp_path / "k.db"), min_balance=1.0))
    out = capsys.readouterr().out
    assert "candidates=1" in out
    assert "valid=1" in out
    assert "high_value=1" in out
    assert "source=npm" in out
    assert "query=MOONSHOT_API_KEY sk- filename:yml" in out
    assert "query=unknown（尚无带 query 归因的结果）" not in out
    assert KEY not in out



# ── v2.4.3: 并发读守卫 + redact 对"曾有效 key"的护栏 ─────────────────

def test_busy_timeout_configured():
    conn = store.connect(":memory:")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_redact_spares_once_valid_keys(tmp_path):
    """回归:upsert 的误判护栏(曾有效 key 保留明文)曾启动脱敏即被击穿——
    key_history 有 valid=1 的 invalid 行必须保留明文,否则凭据永久丢失。"""
    conn = store.connect(str(tmp_path / "t.db"))
    try:
        kh = store.hash_key("sk-live-once")
        conn.execute(
            "INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,"
            "valid,balance,currency,status,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kh, "sk-live-once", "sk-live-once", "qianfan", "s", "", "", "",
             0, 0, "CNY", "invalid", "2026-09-20", "2026-09-21"))
        conn.execute(
            "INSERT INTO key_history (key_hash,verified_at,balance_cny,status,valid) "
            "VALUES (?, '2026-09-19', 5, 'valid_active', 1)", (kh,))
        # 无历史的纯 invalid 行(对照)
        kh2 = store.hash_key("sk-never-valid")
        conn.execute(
            "INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,"
            "valid,balance,currency,status,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kh2, "sk-never-valid", "sk-never-valid", "qianfan", "s", "", "", "",
             0, 0, "CNY", "invalid", "2026-09-20", "2026-09-21"))
        conn.commit()
        store.redact_invalid_keys(conn)
        k1 = conn.execute("SELECT key FROM keys WHERE key_hash=?", (kh,)).fetchone()["key"]
        k2 = conn.execute("SELECT key FROM keys WHERE key_hash=?", (kh2,)).fetchone()["key"]
        assert k1 == "sk-live-once", "曾有效 key 的明文必须保留"
        assert k2 == store.INVALID_REDACTED_KEY
    finally:
        conn.close()


class TestV254StoreFixes:
    def test_double_misjudge_preserves_plaintext(self, tmp_path):
        """曾有效 key 连续两次误判 invalid,明文也不得被抹(v2.5.4 前
        upsert 护栏只看当前行 valid,与 redact 的 key_history 不变量矛盾)。"""
        conn = store.connect(str(tmp_path / "k.db"))
        try:
            key = "sk-" + "1e175253812a494886dd8952b56dc19c"
            store.upsert(conn, {"key": key, "valid": True,
                                "status": "valid_active", "balance_cny": 8.0})
            store.record_history(conn, {"key": key, "valid": True,
                                        "balance_cny": 8.0,
                                        "status": "valid_active"})
            bad = {"key": key, "valid": False, "status": "invalid"}
            store.upsert(conn, bad)
            store.upsert(conn, bad)  # 第二次误判:修复前这里抹掉明文
            row = conn.execute("SELECT key FROM keys WHERE key_hash=?",
                               (store.hash_key(key),)).fetchone()
            assert row["key"] == key
        finally:
            conn.close()

    def test_upsert_non_numeric_balance_no_crash(self, tmp_path):
        conn = store.connect(str(tmp_path / "k.db"))
        try:
            store.upsert(conn, {"key": "sk-" + "a" * 32, "valid": True,
                                "status": "valid_active", "balance_cny": "N/A"})
            row = conn.execute("SELECT balance FROM keys").fetchone()
            assert row["balance"] == 0.0
        finally:
            conn.close()

    def test_ledger_metrics_excludes_percent_from_high_value(self, tmp_path):
        """PERCENT(周额度%)不是钱——metrics 金额合计必须排除。"""
        conn = store.connect(str(tmp_path / "k.db"))
        try:
            store.upsert(conn, {"key": "sk-" + "b" * 32, "valid": True,
                                "status": "valid_active", "balance": 85.0,
                                "provider": "zhipu_coding"})
            store.upsert(conn, {"key": "sk-" + "c" * 32, "valid": True,
                                "status": "valid_active", "balance_cny": 8.37})
            m = store.ledger_metrics(conn, min_balance=1.0)
            assert m["totals"]["high_value"] == 1
            assert m["totals"]["high_value_balance"] == 8.37
        finally:
            conn.close()
