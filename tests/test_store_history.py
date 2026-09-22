"""store.key_history 余额历史表测试。"""
import os
import tempfile

import store


def _conn():
    return store.connect(os.path.join(tempfile.mkdtemp(), "test.db"))


def _result(key, balance, valid=True, status="valid_active"):
    return {"key": key, "valid": valid, "status": status,
            "balance_cny": balance, "provider": "deepseek"}


def test_record_and_get_history():
    conn = _conn()
    store.record_history(conn, _result("sk-aaa", 5.0))
    store.record_history(conn, _result("sk-aaa", 3.0))
    last = store.get_last_history(conn, store.hash_key("sk-aaa"))
    assert last is not None
    assert last["balance_cny"] == 3.0
    assert last["valid"] == 1
    # 多条 → 取最新
    row = conn.execute(
        "SELECT COUNT(*) FROM key_history WHERE key_hash=?", (store.hash_key("sk-aaa"),)).fetchone()
    assert row[0] == 2


def test_record_skips_invalid():
    conn = _conn()
    store.record_history(conn, _result("sk-bbb", 5.0, valid=False))
    assert store.get_last_history(conn, store.hash_key("sk-bbb")) is None


def test_get_last_history_missing():
    conn = _conn()
    assert store.get_last_history(conn, store.hash_key("sk-nope")) is None
