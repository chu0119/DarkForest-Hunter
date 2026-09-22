# 产出现状修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复两个正在损失产出的 bug（KEY_PATTERN 缺智谱 / len>80 误杀 eyJ·Claude）并落实一批验证资源与新鲜度优化，让被堵住的增量通道重新打开。

**Architecture:** 纯 Python 改动，不动 watch 的生产者-消费者骨架。核心是校正提取/过滤口径、把 github_events 实时流接入、把重验预算从 0 余额长尾挪开、给验证链路接连接池。

**Tech Stack:** Python 3.11+（项目已用 `asyncio.timeout`）、requests、aiohttp、sqlite3、pytest、ruff。

## Global Constraints

- 不改 watch 的公开 CLI 行为（子命令/参数名保持兼容，新增参数给默认值）。
- 每个任务独立提交，提交前 `python -m pytest tests/ -q` 全绿 + `python -m ruff check .` 无新增告警。
- 不入库 `results/*`、`queries_generated.txt` 等运行时产物。
- 历史数据口径：darkforest.db 有效 deepseek 824/824 为 32 位小写 hex；qwen 753/753 hex32；kimi 48 位 base62。字符集预检以此为证据基线。

---

### Task 1: 统一 KEY_PATTERN 单一真相源（修智谱提取缺口）

**Files:**
- Modify: `scanner_engine.py:24`（import 行）、`scanner_engine.py:51-61`（删除本地定义）、`scanner_engine.py:789-792`（更正注释）
- Test: `tests/test_scanner_engine.py`

**Interfaces:**
- Produces: `scanner_engine.KEY_PATTERN` 现在等价于 `scanners.base.KEY_PATTERN`（含智谱 `\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b`）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scanner_engine.py`：

```python
def test_engine_key_pattern_extracts_zhipu_hex_secret():
    """scanner_engine.KEY_PATTERN 必须与 scanners.base 同源,含智谱 hex.secret 格式。

    回归: 旧实现本地复制了一份 KEY_PATTERN,缺 hex.secret → github_search 提不出智谱 key。
    """
    import re
    from scanner_engine import KEY_PATTERN
    from scanners.base import KEY_PATTERN as BASE_PATTERN
    # 两份正则必须等价(同一 pattern 对象或编译后 pattern 字符串一致)
    assert KEY_PATTERN.pattern == BASE_PATTERN.pattern
    # 智谱真实格式 hex.secret 必须能被提取
    text = 'ZHIPU_API_KEY = "78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp"'
    m = KEY_PATTERN.search(text)
    assert m is not None, "智谱 hex.secret 格式必须能被提取"
    assert "." in m.group() and len(m.group()) >= 49
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanner_engine.py::test_engine_key_pattern_extracts_zhipu_hex_secret -v`
Expected: FAIL（pattern 不一致 / 智谱未命中）

- [ ] **Step 3: 改 import，删本地定义**

`scanner_engine.py:24` 改为：
```python
from scanners.base import KEY_PATTERN, is_bad_key as _scanner_is_bad_key
```
删除 `scanner_engine.py:51-61` 的 `KEY_PATTERN = re.compile(...)` 整块。
`scanner_engine.py:789-792` 注释改为：
```python
        # 复用 scanners/base.py 的 KEY_PATTERN(单一真相源)——顶部 import,不再本地复制。
        self.key_pattern = KEY_PATTERN
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_scanner_engine.py::test_engine_key_pattern_extracts_zhipu_hex_secret tests/test_base.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scanner_engine.py tests/test_scanner_engine.py
git commit -m "fix: KEY_PATTERN 单一真相源(import scanners.base),补智谱 hex.secret 提取"
```

---

### Task 2: is_bad_key 前缀感知长度上限（救 MiniMax JWT / Claude 长 key）

**Files:**
- Modify: `scanners/base.py:74-82`（长度过滤段）
- Test: `tests/test_base.py`

**Interfaces:**
- Consumes: `KEY_PATTERN`（已统一）
- Produces: `is_bad_key` 不再误杀 `eyJ...`（≤250）与 `sk-ant-api03-...`（≤150）；普通 `sk-` 家族仍 ≤80。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_base.py` 的 `TestIsBadKey` 类内：

```python
    def test_eyJ_jwt_passes_under_prefix_aware_limit(self):
        # MiniMax JWT: eyJ + 三段 base64url,最小总长 83,真实 key 常见 150-220
        jwt = "eyJ" + "aB3dE7fG9hJ1kL2m" * 6 + "." + "xY7zK9" * 12 + "." + "pQ1rS8" * 10
        assert len(jwt) > 80
        assert not is_bad_key(jwt), f"eyJ JWT 不应被长度>80 误杀(len={len(jwt)})"

    def test_eyj_too_long_still_rejected(self):
        jwt = "eyJ" + "a" * 260
        assert is_bad_key(jwt)

    def test_claude_long_key_passes(self):
        # Claude sk-ant-api03- 真实 key 总长 ~104-120
        claude = "sk-ant-api03-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 3
        assert len(claude) > 80
        assert not is_bad_key(claude), f"Claude 长 key 不应被长度>80 误杀(len={len(claude)})"

    def test_generic_sk_over_80_still_rejected(self):
        # 普通 sk- 家族仍限 80(fresh-repo env 长内容误匹配仍需拦截)
        assert is_bad_key("sk-" + "a" * 100)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_base.py::TestIsBadKey -v`
Expected: 4 个新测试 FAIL

- [ ] **Step 3: 改实现**

`scanners/base.py:78-82` 把：
```python
    # 长度过滤:超长 key 不是真 API key(真 key 通常 <80 字符)
    # 统计:invalid key 中 >80 字符占 5.3%,这些是 JWT/代码变量/长字符串误匹配
    # fresh-repo 精扫 sk- filename:env 时常匹配到超长 env 内容,必须在此过滤
    if len(key) > 80:
        return True
```
改为：
```python
    # 前缀感知长度上限:eyJ(MiniMax JWT 三段,最小总长 83)与 sk-ant-api03-(Claude,
    # 真实总长 ~104-120)天然超长,统一 >80 会把它们全杀(回归 27f2496);
    # 普通 sk-/tp-/gsk_ 等家族仍限 80(fresh-repo env 长内容误匹配仍需拦截)。
    # 验证端已有 >256 拒验护栏兜底,提取端按前缀放宽上限与之对齐。
    if key.startswith("eyJ"):
        if len(key) > 250:
            return True
    elif key.startswith("sk-ant-api03-"):
        if len(key) > 150:
            return True
    elif len(key) > 80:
        return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_base.py tests/test_scanner_engine.py -v`
Expected: PASS（含原有 `test_length_bounds` 等）

- [ ] **Step 5: 提交**

```bash
git add scanners/base.py tests/test_base.py
git commit -m "fix: is_bad_key 前缀感知长度上限(救 eyJ JWT/sk-ant-api03-,普通 sk- 仍限 80)"
```

---

### Task 3: deepseek/qwen hex 字符集预检（砍占位 key 的无效验证）

**Files:**
- Modify: `providers.py:1120-1127`（`_pre_check_key_format` deepseek/kimi/qwen 分支）
- Test: `tests/test_verifier.py`

**Interfaces:**
- Produces: `_pre_check_key_format` 对 `deepseek`/`qwen`/`dashscope` 要求 body 为 32 位小写 hex；`kimi` 要求 body 全 base62 alnum。预检失败 → 不发 HTTP 直接判 INVALID。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_verifier.py`：

```python
class TestPreCheckCharset:
    def test_deepseek_non_hex_rejected_without_network(self, monkeypatch):
        """占位串(sk-uikoukw... 纯字母)不该浪费一次 API 调用。"""
        verifier = UnifiedKeyVerifier()
        calls = []
        monkeypatch.setattr("providers.requests.get",
                            lambda *a, **k: calls.append(k) or FakeResponse(200, {}))
        r = verifier.verify_key("sk-uikoukwhkyzabcdef0123456789ab", "deepseek")
        assert r["status"] == "invalid"
        assert calls == [], "非 hex body 应被预检拦下,不发任何 HTTP"

    def test_deepseek_valid_hex_passes_precheck(self, monkeypatch):
        monkeypatch.setattr("providers.requests.get",
                            lambda *a, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda *a, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key("sk-1e175253812a4948" "86dd8952b56dc19c", "deepseek")
        assert r["status"] != "invalid" or r["message"] != "格式预检失败"

    def test_qwen_ws_prefix_handled(self, monkeypatch):
        """qwen 2026 升级 sk-ws- 前缀 body 也是 hex,预检须先剥前缀再校验。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda *a, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda *a, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key("sk-ws-1e175253812a494886dd8952b56dc19c", "qwen")
        assert r["status"] != "invalid" or r["message"] != "格式预检失败"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_verifier.py::TestPreCheckCharset -v`
Expected: FAIL（纯字母串当前会走到 HTTP；qwen sk-ws- 当前 body 含 '-' 非 hex 被预检杀）

- [ ] **Step 3: 改实现**

`providers.py:1120-1127` 把：
```python
        elif pid in ("deepseek", "kimi", "qwen", "dashscope"):
            # sk- 后跟 32-64 位字母数字(主流格式)
            if not k.startswith("sk-"):
                return False
            body = k[len("sk-"):]
            if len(body) < 28 or len(body) > 70:
                return False
```
改为：
```python
        elif pid in ("deepseek", "qwen", "dashscope"):
            # DB 证据: 有效样本 100% 为 32 位小写 hex(deepseek 824/824, qwen 753/75753)。
            # 纯字母拼凑的占位串(uikoukw 类)在此被杀,省一次验证 API(0.5-2s)。
            if not k.startswith("sk-"):
                return False
            body = k[len("sk-"):]
            if body.startswith("ws-"):  # qwen 2026 升级前缀
                body = body[len("ws-"):]
            if not re.fullmatch(r"[0-9a-f]{28,64}", body):
                return False
        elif pid == "kimi":
            # kimi 真 key body 为 48 位 base62 alnum(DB 240/240)
            if not k.startswith("sk-"):
                return False
            body = k[len("sk-"):]
            if len(body) < 28 or len(body) > 70:
                return False
            if not body.isalnum():
                return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_verifier.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add providers.py tests/test_verifier.py
git commit -m "fix: deepseek/qwen hex + kimi alnum 字符集预检(砍占位 key 无效验证)"
```

---

### Task 4: 接线 github_events 实时 PushEvent 流

**Files:**
- Modify: `scanners/github_events.py:18,38-54`（加 deadline + 超时返回）、`scanner_engine.py:24-33`（import EventsMonitor）、`scanner_engine.py:1280`（registry 注册）、`watch_tui.py:1369-1377`（DEFAULT_WATCH_SOURCES）、`scanners/__init__.py`（导出）、`email_notifier.py:30-43`（SOURCE_LABELS）、`run.py:353-366`（--list-sources）
- Test: `tests/test_scanners_external.py`

**Interfaces:**
- Produces: `EventsMonitor` 支持 `deadline_s` 参数，`search()` 在 deadline 内返回部分结果后退出（可被 `_run_one_scanner` 的 `asyncio.run` 调用）；源名 `github_events` 注册进 engine registry 与 watch 默认源。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scanners_external.py` 末尾：

```python
class TestEventsMonitorDeadline:
    def test_search_returns_within_deadline(self, monkeypatch):
        """deadline 到期必须返回(不无限轮询)。"""
        import asyncio
        import scanners.github_events as ge_mod
        from scanners.github_events import EventsMonitor

        class StaticResp(FakeResp):
            def __init__(self):
                super().__init__(200, json_data=[])  # 空事件 → 不下载任何文件

        def responder(url):
            return StaticResp()

        _patch_session(monkeypatch, responder)
        m = EventsMonitor(token="", poll_interval=0.01, deadline_s=0.15, max_events_per_poll=5)
        import time as _t
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
            return FakeResp(200, text_data='DEEPSEEK_API_KEY = "sk-' + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" + '"')

        _patch_session(monkeypatch, responder)
        m = EventsMonitor(token="t", poll_interval=0.01, deadline_s=0.5, max_events_per_poll=5)
        asyncio.run(m.search())
        assert m.results, "应从 PushEvent 提取到 key"
        assert m.results[0]["key"].startswith("sk-")
```

注意：`_patch_session` 现有实现 patch 的是 `hf_mod.aiohttp.ClientSession`，需在测试里改为 patch `ge_mod.aiohttp.ClientSession`。在测试函数内加：
```python
        monkeypatch.setattr(ge_mod.aiohttp, "ClientSession",
                            lambda **kw: FakeSession(responder))
```
（替代 `_patch_session` 调用，或新增专用 patch）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanners_external.py::TestEventsMonitorDeadline -v`
Expected: FAIL（`deadline_s` 参数不存在 / search 无限轮询超时）

- [ ] **Step 3: 改 EventsMonitor 加 deadline**

`scanners/github_events.py:18-19` 构造函数加 `deadline_s`：
```python
    def __init__(self, token: str = "", poll_interval: int = 60,
                 max_events_per_poll: int = 30, deadline_s: float = 45.0, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.poll_interval = poll_interval or self.POLL_INTERVAL
        self.max_events_per_poll = max_events_per_poll
        self._deadline = deadline_s
```
`search()` 方法（38-54）包进 `asyncio.timeout`：
```python
    async def search(self, query: str | None = None) -> list[dict]:
        self.results = []
        sem = asyncio.Semaphore(self.concurrency)
        try:
            async with asyncio.timeout(self._deadline):
                async with aiohttp.ClientSession(headers=self._headers) as session:
                    while not self._should_stop():
                        events = await self._fetch_events(session)
                        if events:
                            push_events = [e for e in events
                                           if e.get("type") == "PushEvent" and e.get("public")]
                            for ev in push_events[:self.max_events_per_poll]:
                                if self._should_stop():
                                    break
                                await self._handle_push(session, sem, ev)
                        await asyncio.sleep(self.poll_interval)
        except TimeoutError:
            pass  # deadline 到期,返回已采集的部分结果
        return self.results
```

- [ ] **Step 4: 注册进 engine registry + watch 默认源 + 导出 + 标签**

`scanner_engine.py` 顶部 import 区（24 行附近）加：
```python
from scanners.github_events import EventsMonitor
```
`scanner_engine.py` 的 `_get_scanner_registry` 返回 dict 内加一行：
```python
            "github_events": (EventsMonitor, None, {"token": github_token, "poll_interval": 10,
                                                      "max_events_per_poll": 30, "deadline_s": 45,
                                                      "proxy": self.proxy}),
```
`watch_tui.py` 的 `DEFAULT_WATCH_SOURCES` 改为：
```python
DEFAULT_WATCH_SOURCES = [
    "github_search",
    "github_commits",   # 最近提交 diff 里的 key(新鲜度最高,配额独立于 code search)
    "github_events",    # PushEvent 实时流(分钟级新鲜,core API 配额,独立于 Code Search)
    "gitlab",           # 项目内 blob 搜索
    "npm",              # 唯一有产出的外部源
    # 以下默认关闭，需 --sources 显式启用
    # "huggingface", "paste_sites", "docker",
]
```
`scanners/__init__.py` import 加 `from .github_events import EventsMonitor`，`__all__` 加 `"EventsMonitor"`，顶部 docstring 删掉"未接入"那句。
`email_notifier.py` 的 `SOURCE_LABELS` 加 `"github_events": "GitHub 实时推送",`。
`run.py` 的 `--list-sources` 列表加 `("github_events", "GitHub PushEvent 实时流"),`。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_scanners_external.py tests/test_watch_tui.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add scanners/github_events.py scanners/__init__.py scanner_engine.py watch_tui.py email_notifier.py run.py tests/test_scanners_external.py
git commit -m "feat: 接线 github_events 实时 PushEvent 流(deadline 限时+注册默认源)"
```

---

### Task 5: 重验分层——0 余额 key 周级冷却（预算让给活跃 key）

**Files:**
- Modify: `watch_tui.py:934-960`（ReverifyScheduler 构造 + tier 常量）、`watch_tui.py:980-997`（`_tier_interval`）、`watch_tui.py:2471-2485`/`2571-2581`（run_watch 接线）、`run.py:295-325`（CLI 参数）、`config_loader.py:45-83`（config 字段）、`config.ini.example:11-27`
- Test: `tests/test_reverify_scheduler.py`

**Interfaces:**
- Produces: `ReverifyScheduler(..., zero_interval_hours=168)`；`_tier_interval(balance<=0)` 返回 `zero_interval_hours*3600`（默认 7 天），趋势不再对 0 余额生效。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_reverify_scheduler.py`：

```python
def test_zero_balance_recently_verified_skipped():
    """0 余额 key 刚验过(< 7d)→ 不重验,把预算让给活跃 key。

    回归: 旧实现 0 余额层 ~403s 间隔,2000 个 0 余额 key 每天吃掉几乎全部 1500 预算,
    53 个 valid_active 反而排不上。改周级后 0 余额需求降到 ~286/天,预算富余。
    """
    s, broker, conn = _make_scheduler(budget=100000)
    conn.execute("INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,valid,balance,currency,status,first_seen,last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-zero-recent"), "sk-zero-recent", "sk-zero-recent", "kimi", "github_search", "", "", "",
                  1, 0.0, "CNY", "valid_zero", "2026-08-01 00:00:00",
                  "2026-08-25 00:00:00"))  # 1 天前 → 7d 未到
    conn.commit()
    s._tick()
    assert "sk-zero-recent" not in broker.enqueued


def test_zero_balance_due_after_seven_days():
    s, broker, conn = _make_scheduler(budget=100000)
    conn.execute("INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,valid,balance,currency,status,first_seen,last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-zero-old"), "sk-zero-old", "sk-zero-old", "kimi", "github_search", "", "", "",
                  1, 0.0, "CNY", "valid_zero", "2026-08-01 00:00:00",
                  "2026-08-10 00:00:00"))  # 16 天前 → 7d 已过
    conn.commit()
    s._tick()
    assert "sk-zero-old" in broker.enqueued


def test_zero_interval_configurable():
    s, broker, conn = _make_scheduler(budget=100000, zero_interval_hours=1)
    assert s._tier_interval(0.0, 0.0) == 3600.0
```

`_make_scheduler` 签名要加 `zero_interval_hours` 参数转发（见 Step 3）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reverify_scheduler.py -v`
Expected: FAIL（`_make_scheduler` 不接受 `zero_interval_hours`；0 余额层返回 ~403s，recent 被 enqueue）

- [ ] **Step 3: 改实现**

`watch_tui.py` `ReverifyScheduler.__init__`（934 附近）加参数：
```python
    def __init__(self, broker, store_conn, budget_per_day: int = 1500,
                 email_threshold: float = 5.0, top_threshold: float = 10.0,
                 shrink_pct: float = 30.0,
                 zero_interval_hours: int = 168,
                 changes_csv: str = os.path.join("results", "balance_changes.csv"),
                 store_lock=None):
        ...
        self._zero_interval = zero_interval_hours * 3600
```
类常量区（958 附近）保留 `_TIER_INTERVAL_TOP/_HV/_OTHER`；`_tier_interval`（980-997）改为：
```python
    def _tier_interval(self, balance: float, trend: float = 0.0) -> float:
        # 0 余额 key(valid_zero/valid_no_balance)走周级冷却:充值检测保留但降频,
        # 把每日预算让给有余额的活跃 key(实测 0 余额回充率为 0,高频重验无收益)。
        if balance <= 0:
            return float(self._zero_interval)
        base = self._TIER_INTERVAL_OTHER
        if balance >= self._top_threshold:
            base = self._TIER_INTERVAL_TOP
        elif balance >= self._email_threshold:
            base = self._TIER_INTERVAL_HV
        if trend < -0.01:
            return max(60.0, base * 0.5)
        if trend > 0.01:
            return base * 2.0
        return base
```
`tests/test_reverify_scheduler.py` 的 `_make_scheduler` 加参数转发：
```python
def _make_scheduler(**kw):
    ...
    s = ReverifyScheduler(broker, conn, budget_per_day=kw.get("budget", 100000),
                          email_threshold=5.0, top_threshold=10.0, shrink_pct=30.0,
                          zero_interval_hours=kw.get("zero_interval_hours", 168))
    return s, broker, conn
```

`config_loader.py` 加字段（45 行附近 init、83 行附近 load）：
```python
        self._watch_reverify_zero_interval_hours: int = 168
        ...
        self._watch_reverify_zero_interval_hours = self._getint("watch", "reverify_zero_interval_hours", 168)
```
加 property（227 附近）：
```python
    @property
    def watch_reverify_zero_interval_hours(self) -> int:
        return self._watch_reverify_zero_interval_hours
```
`watch_tui.py` `run_watch`（2471 附近）读取：
```python
    rv_zero = ... _cfg.watch_reverify_zero_interval_hours
    rv_scheduler = ReverifyScheduler(..., zero_interval_hours=rv_zero, ...)
```
`run.py` watch 子命令加 `--reverify-zero-hours` 参数（默认 None → config）并传给 `run_watch`。
`run_watch` 签名加 `reverify_zero_hours: int | None = None`。
`config.ini.example` `[watch]` 加 `reverify_zero_interval_hours = 168`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_reverify_scheduler.py tests/test_watch_tui.py tests/test_config_loader.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py config_loader.py run.py config.ini.example tests/test_reverify_scheduler.py
git commit -m "opt: 0 余额 key 重验改周级冷却(预算让给 valid_active)"
```

---

### Task 6: 验证链路连接复用（verifier 单例 + Session 注入）

**Files:**
- Modify: `providers.py:826-830`（UnifiedKeyVerifier 构造）、`providers.py:950-953,1183,1230-1231`（GET/POST 走 Session）、`watch_tui.py:625-673`（`_verify_one` 缓存 verifier）
- Test: `tests/test_verifier.py`

**Interfaces:**
- Produces: `UnifiedKeyVerifier(providers, proxy, session)` 可选注入 `requests.Session`；内部所有 HTTP 走 `self._http`（None 时回退 `requests`）。`VerificationBroker` 每 worker 线程缓存一个 verifier + 复用其 per-thread Session。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_verifier.py`：

```python
class TestSessionReuse:
    def test_injected_session_used_for_get(self, monkeypatch):
        """注入的 Session 必须被复用,不再走裸 requests.get。"""
        import requests
        class FakeSession:
            def __init__(self):
                self.gets = []
                self.posts = []
            def get(self, url, **k):
                self.gets.append(url)
                return FakeResponse(200, {"data": []})
            def post(self, url, **k):
                self.posts.append(url)
                return FakeResponse(200, {"id": "x"})
            def close(self): pass
        sess = FakeSession()
        bare_calls = []
        monkeypatch.setattr("providers.requests.get",
                            lambda *a, **k: bare_calls.append(k) or FakeResponse(200, {}))
        monkeypatch.setattr("providers.requests.post",
                            lambda *a, **k: bare_calls.append(k) or FakeResponse(200, {}))
        v = UnifiedKeyVerifier(session=sess)
        v.verify_key(_key(), "deepseek")
        assert sess.gets, "应调用注入的 Session.get"
        assert bare_calls == [], "不应再走裸 requests"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_verifier.py::TestSessionReuse -v`
Expected: FAIL（UnifiedKeyVerifier 不接受 session 参数）

- [ ] **Step 3: 改 UnifiedKeyVerifier**

`providers.py:826-830` 构造：
```python
    def __init__(self, providers: list[AIProvider] = None, proxy: str = None,
                 session: "requests.Session | None" = None):
        self.providers = providers or ACTIVE_PROVIDERS
        self.proxy = proxy
        self._session = None
        self._http = session  # 注入复用(VerificationBroker 每 worker 一个)
```
加两个内部辅助方法（紧跟 `_proxies_for`）：
```python
    def _http_get(self, url, *, headers, proxies, timeout):
        getter = self._http.get if self._http is not None else requests.get
        return getter(url, headers=headers, proxies=proxies, timeout=timeout)

    def _http_post(self, url, *, headers, proxies, timeout, json):
        poster = self._http.post if self._http is not None else requests.post
        return poster(url, json=json, headers=headers, proxies=proxies, timeout=timeout)
```
替换 `_verify_with_provider` 里 `resp = requests.get(url, ...)` → `resp = self._http_get(url, headers=headers, proxies=self._proxies_for(provider), timeout=30)`；
`_check_balance_sync` 里 `resp = requests.get(url, ...)` → `resp = self._http_get(url, headers=headers, proxies=proxies, timeout=10)`；
`_probe_chat` 里 `resp = requests.post(url, ...)` → `resp = self._http_post(url, headers=headers, proxies=self._proxies_for(provider), timeout=30, json=payload)`。

- [ ] **Step 4: 改 broker 缓存 verifier**

`watch_tui.py` `VerificationBroker.__init__` 末尾加：
```python
        self._verifiers: dict[int, object] = {}
```
`_verify_one`（625 附近）开头的 `from providers import ...` 那段改成调缓存方法：
```python
    def _get_verifier(self):
        tid = threading.get_ident()
        v = self._verifiers.get(tid)
        if v is None:
            try:
                from providers import ALL_PROVIDERS, UnifiedKeyVerifier
                v = UnifiedKeyVerifier(ALL_PROVIDERS,
                                       proxy=getattr(self.engine, "proxy", None),
                                       session=self._get_session())
            except Exception:
                return None
            self._verifiers[tid] = v
        return v
```
`_verify_one` 用 `verifier = self._get_verifier()` 取，`None` 时走原 deepseek 回退路径。`stop()` 里清理 `self._verifiers.clear()` + 对每个 verifier 的 session close（其 session 来自 broker `_sessions`，已被 stop 关闭，无需重复）。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_verifier.py tests/test_watch_tui.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add providers.py watch_tui.py tests/test_verifier.py
git commit -m "opt: 验证链路 Session 注入复用(verifier 每 worker 单例,省 TLS 握手)"
```

---

### Task 7: 移除 docker 默认源（历史产出 1 行）

**Files:**
- Modify: `watch_tui.py:1369-1377`（已在 Task 4 改过，此任务确认 docker 注释化）
- Test: `tests/test_watch_tui.py`

- [ ] **Step 1: 写测试**

追加到 `tests/test_watch_tui.py`：
```python
def test_docker_not_in_default_sources():
    from watch_tui import DEFAULT_WATCH_SOURCES
    assert "docker" not in DEFAULT_WATCH_SOURCES, "docker 历史产出仅 1 行,不应占默认源"
    assert "github_events" in DEFAULT_WATCH_SOURCES
```

- [ ] **Step 2: 跑测试**

Run: `python -m pytest tests/test_watch_tui.py::test_docker_not_in_default_sources -v`
Expected: PASS（Task 4 已移除 docker）；若 FAIL 说明 Task 4 未移除，补改。

- [ ] **Step 3: 提交（若 Task 4 已含则跳过）**

```bash
git add tests/test_watch_tui.py
git commit -m "test: 锁定 docker 不在默认源"
```

---

### Task 8: _save_from_broker 历史缓存化 + CSV 平台回填

**Files:**
- Modify: `watch_tui.py:1462-1466`（WatchScanner.__init__ 加缓存）、`watch_tui.py:2015-2063`（`_save_from_broker` + 删 `_merge_with_history`）
- Test: `tests/test_watch_tui.py`

**Interfaces:**
- Produces: `WatchScanner` 持有 `self._history_map: dict[str, dict]`，启动时从 `load_history()` 一次性载入；每次保存只做内存合并 + 淘汰，不再反复读盘。CSV 旧行空平台字段从缓存里的 DB provider 回填。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_watch_tui.py`：

```python
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
    # 启动构造时载一次
    assert calls["n"] == 1
    # 多次保存不应再触发 load_history
    for _ in range(3):
        sc._save_from_broker(force=True)
    assert calls["n"] == 1, f"缓存化后不应重复读盘,实际 {calls['n']}"

def test_csv_provider_backfilled_from_history(tmp_path):
    """旧 CSV 行平台字段空 → 从 DB/历史 provider 回填。"""
    import watch_tui as w
    # 构造一个无 provider 的历史记录
    state = w.WatchState()
    broker = w.VerificationBroker(engine=None, db_path=None)
    sc = w.WatchScanner(state=state, broker=broker, output_dir=str(tmp_path))
    sc._history_map = {"sk-deadkey123": {"key": "sk-deadkey123", "valid": True,
                                          "balance_cny": 0.0, "provider": "deepseek",
                                          "source": "github_search"}}
    sc._provider_hints = {"sk-deadkey123": "deepseek"}
    # broker 无新结果,保存只写历史
    sc._save_from_broker(force=True)
    import csv
    rows = list(csv.reader(open(sc.csv_path, encoding="utf-8")))
    # 找到该 key 行,平台列应非空
    for r in rows:
        if "sk-deadkey123" in r:
            assert r[2] == "deepseek", f"平台列应回填 deepseek,实际 {r[2]!r}"
            return
    assert False, "CSV 里应能找到该 key 行"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_watch_tui.py::test_history_loaded_once_not_per_save tests/test_watch_tui.py::test_csv_provider_backfilled_from_history -v`
Expected: FAIL（`_history_map`/`_provider_hints` 不存在；`_save_from_broker` 仍调 `_merge_with_history→load_history`）

- [ ] **Step 3: 改实现**

`watch_tui.py` `WatchScanner.__init__`（1462 附近，`self._save_lock` 之后）加：
```python
        # 历史缓存:启动时一次性 load_history,后续保存只做内存合并,
        # 不再每 15s 反复读 1.5MB JSON + 全表 SELECT + CSV(随账本线性增长的 I/O)。
        self._history_map: dict[str, dict] = {
            r["key"]: r for r in load_history(output_dir) if r.get("key")}
        self._provider_hints: dict[str, str] = {
            k: r["provider"] for k, r in self._history_map.items()
            if (r.get("provider") or "").strip() and r.get("provider") != "unknown"}
```
`_save_from_broker`（2015 附近）改为：
```python
    def _save_from_broker(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_save < self._save_min_interval:
            return
        self._last_save = now
        all_valid = self.broker.get_all_results()
        evicted = self.broker.evicted_keys()
        with self._save_lock:
            hist = self._history_map
            for r in all_valid:
                k = r.get("key")
                if k:
                    hist[k] = r  # 新验证覆盖历史
                    p = (r.get("provider") or "").strip()
                    if p and p != "unknown":
                        self._provider_hints[k] = p
            if evicted:
                for k in evicted:
                    hist.pop(k, None)
            merged = list(hist.values())
            # 平台字段回填:本轮无验证结果的旧行从 provider_hints 补
            for r in merged:
                if r.get("valid") and not (r.get("provider") or "").strip():
                    p = self._provider_hints.get(r.get("key"), "")
                    if p:
                        r["provider"] = p
            arrears = {r.get("key") for r in merged if (r.get("balance_cny") or 0) < 0}
            arrears |= evicted
            for k in arrears:
                hist.pop(k, None)
            merged = list(hist.values())  # 淘汰后重取
            merged_valid = [r for r in merged if r.get("valid")]
            merged_hv = filter_high_value(merged, self.min_balance)
            write_watch_csv(self.csv_path, merged_valid, arrears_keys=arrears)
            save_watch_state(self.state_path, merged)
        self.state.set_high_value_keys(merged_hv)
```
删除 `_merge_with_history` 方法（2051-2063）。

- [ ] **Step 4: 跑相关测试**

Run: `python -m pytest tests/test_watch_tui.py -v`
Expected: PASS（含 `test_first_save_keeps_disk_history` 等——缓存初始化已含磁盘历史，语义等价）

- [ ] **Step 5: 提交**

```bash
git add watch_tui.py tests/test_watch_tui.py
git commit -m "opt: _save_from_broker 历史缓存化(去线性 I/O)+ CSV 平台回填"
```

---

### Task 9: 死代码清理（store/broker/base 未引用 API）

**Files:**
- Modify: `store.py:117-142`（删 `known_keys`/`all_known_keys`）、`watch_tui.py:547-558`（删 `get_valid_keys`）、`scanners/base.py:107-142`（删 `extract_high_entropy`/`shannon_entropy`/`_HIGH_ENTROPY_TOKEN`）、`scanners/__init__.py`（删导出）、`tests/test_entropy.py`（删整个文件）
- Test: 相关 test_store.py / test_base.py 引用处一并清理

- [ ] **Step 1: grep 确认无生产引用**

Run:
```bash
grep -rn "known_keys\|all_known_keys" --include="*.py" . | grep -v _backup | grep -v test
grep -rn "get_valid_keys" --include="*.py" . | grep -v test
grep -rn "extract_high_entropy\|shannon_entropy" --include="*.py" . | grep -v _backup
```
Expected: 仅定义与测试引用,无生产调用。若有生产引用 → 停止本任务。

- [ ] **Step 2: 清理 store.py / watch_tui.py / base.py / __init__.py**

删除上述函数定义与 `scanners/__init__.py` 的对应 import/`__all__` 项与 docstring 提及。
`tests/test_store.py`、`tests/test_base.py` 里引用被删函数的测试用例一并删除。
`tests/test_entropy.py` 整文件删除。

- [ ] **Step 3: 跑全套测试**

Run: `python -m pytest tests/ -q`
Expected: PASS（数量比基线少,删的测试用例不再计入）

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: 清理死代码(known_keys/all_known_keys/get_valid_keys/entropy 工具未使用)"
```

---

### Task 10: run.py deepseek 子命令验证口径对齐多平台 + 文档校准

**Files:**
- Modify: `scanner_engine.py:1645-1750`（`_verify_dict` 改用 UnifiedKeyVerifier）、`README.md:63-65`、`README_CN.md` 对应行、`CHANGELOG.md`
- Test: `tests/test_scanner_engine.py`

**Interfaces:**
- Produces: `ScannerEngine._verify_dict` 对每个 key 用 `UnifiedKeyVerifier.verify_key(key, context=repos)` 判定,返回结构与 watch 的 broker 结果对齐(含 provider/status/balance_cny/usd)。`python run.py deepseek` 不再把 kimi/qwen 等 sk- key 误判 invalid。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scanner_engine.py`：

```python
class TestVerifyDictMultiPlatform:
    def test_kimi_key_routed_to_moonshot_not_deepseek(self, monkeypatch):
        """deepseek 子命令扫到的多平台 key 不应被 deepseek 余额端点误判 invalid。"""
        from scanner_engine import ScannerEngine
        eng = ScannerEngine()
        # 捕获 verify 请求的 URL:应打 moonshot 而非 deepseek
        hits = {"urls": []}
        class R:
            def __init__(self, code=200, data=None, text=""):
                self.status_code = code; self._d = data or {}; self.text = text
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
        from providers import UnifiedKeyVerifier
        results = eng._verify_dict({"sk-aUbQL11X1SVze8SyD1sdbHyalmeLlJSLXqZL1vn8iumXdqV": {
            "key_preview": "sk-aUbQL...", "repos": [{"repo":"o/r","file":"f.py","url":"u"}]}})
        assert len(results) == 1
        # 至少有一次请求命中 moonshot(kimi 路由)
        assert any("moonshot" in u for u in hits["urls"]), f"kimi key 应路由到 moonshot,实际 {hits['urls']}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scanner_engine.py::TestVerifyDictMultiPlatform -v`
Expected: FAIL（旧实现只打 deepseek /user/balance,kimi key 返回 401 → invalid,无 moonshot 命中）

- [ ] **Step 3: 改 `_verify_dict`**

`scanner_engine.py:1645-1750` 的 `verify_one` 闭包替换为走 UnifiedKeyVerifier：
```python
    def _verify_dict(self, keys_dict: dict) -> list:
        if not keys_dict:
            return []
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from providers import UnifiedKeyVerifier, VerifyResult
        results = []
        lock = threading.Lock()
        total = len(keys_dict)
        done_count = [0]

        def verify_one(api_key, info):
            verifier = UnifiedKeyVerifier(proxy=self.proxy)
            repos = info.get("repos", []) if isinstance(info, dict) else []
            context = " ".join(f"{r.get('repo','')}/{r.get('file','')}" for r in repos[:5]
                               if isinstance(r, dict))
            v = verifier.verify_key(api_key, context=context)
            status = v.get("status", "")
            valid = status in (
                VerifyResult.VALID_ACTIVE.value, VerifyResult.VALID_ZERO.value,
                VerifyResult.VALID_NO_BALANCE.value)
            balance = v.get("balance") or 0.0
            provider_id = v.get("provider", "unknown")
            currency = "USD" if provider_id == "claude" else "CNY"
            return {
                "key": api_key,
                "key_preview": info.get("key_preview", api_key[:10]+"..."+api_key[-4:]),
                "valid": valid,
                "status": status,
                "provider": provider_id,
                "balance": balance,
                "balance_usd": convert_to_usd(balance, currency, self.usd_cny_rate),
                "balance_cny": convert_to_cny(balance, currency, self.usd_cny_rate),
                "primary_currency": currency,
                "repos": info.get("repos", []),
                "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = {executor.submit(verify_one, k, info): (k, info)
                       for k, info in keys_dict.items()}
            for future in as_completed(futures):
                k, info = futures[future]
                try:
                    r = future.result(timeout=120)
                except Exception as e:
                    r = {"key": k, "valid": False, "status": "error",
                         "provider": "unknown", "repos": info.get("repos", []),
                         "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                with lock:
                    done_count[0] += 1
                    results.append(r)
        return results
```

- [ ] **Step 4: 校准文档**

`README.md:65`：
```
Balance query is supported on: **deepseek / kimi / zhipu / qwen / stepfun / siliconflow / minimax(cp)**. Other platforms validate key validity only.
```
`README_CN.md` 对应行同步。
`CHANGELOG.md` 顶部加 `## v2.21 (2026-08-26)` 段,列本批修复。

- [ ] **Step 5: 跑全套测试 + ruff**

Run: `python -m pytest tests/ -q && python -m ruff check .`
Expected: PASS / 无新增告警

- [ ] **Step 6: 提交**

```bash
git add scanner_engine.py README.md README_CN.md CHANGELOG.md tests/test_scanner_engine.py
git commit -m "fix: run.py deepseek 验证口径对齐多平台(UnifiedKeyVerifier)+ 文档校准"
```

---

## 执行说明

10 个任务相互基本独立，建议按序执行（Task 1→2→3→4→5→6→7→8→9→10），每个任务结束提交。Task 4 改了 `DEFAULT_WATCH_SOURCES`，Task 7 是其补充测试锁定。Task 8 删了 `_merge_with_history`，需确认无测试直接调用该方法。全程 `python -m pytest tests/ -q` 保持绿。
