"""DarkForest Watch — 持续多源循环扫描 + TUI 面板。

架构：生产者-消费者模式
- 扫描端（生产者）：各数据源只管扫，发现 key 立即提交到验证队列
- 验证 worker（消费者）：独立线程从队列取 key，限速调用 DeepSeek API
- 扫描和验证完全并行，最大化网络/CPU 利用率
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime

import requests

from watch_persistence import (
    _WATCH_CSV_HEADER,  # noqa: F401  兼容既有导入和测试 monkeypatch
    _watch_csv_row,  # noqa: F401
    load_history,  # noqa: F401
    load_watch_state,  # noqa: F401
    save_watch_state,
    write_watch_csv,
)

# ══════════════════════════════════════════════════════════════════
#  纯函数（无副作用，易于测试）
# ══════════════════════════════════════════════════════════════════

def filter_high_value(results: list[dict], min_balance: float) -> list[dict]:
    """从验证结果中筛选 balance_cny > min_balance 的有效 key，按余额降序排列。"""
    high = [r for r in results if r.get("valid") and r.get("balance_cny", 0) > min_balance]
    return sorted(high, key=lambda r: r["balance_cny"], reverse=True)


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
    # 注: key_history 只记录有效 key(store.record_history 硬编码 valid=1),
    # 故 prev.valid 在生产中恒为 1,reactivated 分支实际不可达——
    # "充值后感知"场景由 refill 规则覆盖(valid_zero 0.01→8.0 = 800% 涨幅 → refill)。
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
                    f"{cur_b:.1f}", f"{prev_b:.1f}", f"{cur_b - prev_b:+.1f}",
                    change_type])



def update_first_seen(new_results: list[dict], known_results: list[dict]) -> list[dict]:
    """为新验证结果设置 first_seen（新 key 用 verified_at，已知 key 保留原值）。"""
    known_map = {r["key"]: r for r in known_results if r.get("key")}
    for r in new_results:
        key = r.get("key", "")
        if key in known_map:
            r["first_seen"] = known_map[key].get("first_seen", r.get("verified_at", ""))
        else:
            r["first_seen"] = r.get("verified_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return new_results


# ══════════════════════════════════════════════════════════════════
#  VerificationBroker — 独立验证队列（生产者-消费者）
# ══════════════════════════════════════════════════════════════════

class VerificationBroker:
    """全局验证队列。扫描端 submit() 提交 key，worker 线程限速消费验证。

    设计：
    - PriorityQueue：新发现 key 高优先级，已知低价值 key 低优先级
    - 单 worker 线程：避免 DeepSeek API 限流（默认 ~2 QPS）
    - 429 自动退避：遇到限流指数退避，恢复后继续
    - 结果写回 _results dict，供 TUI 和 CSV 消费
    """

    def __init__(self, engine, min_balance: float = 1.0, interval: float = 0.5,
                 workers: int = 4, db_path: str | None = None,
                 email_notifier=None,
                 hv_email_threshold: float = 5.0, hv_top_threshold: float = 10.0,
                 shrink_warn_pct: float = 30.0,
                 allow_chat_probe: bool = False,
                 probe_unclear: bool = True,
                 provider_rate_limiter=None,
                 on_verified=None,
                 changes_csv: str | None = None):
        self.engine = engine
        self.min_balance = min_balance
        self.interval = interval  # 验证间隔（秒/worker），单 worker QPS ≈ 1/interval
        self._workers = max(1, workers)  # 并发 worker 数（多 worker 并行验证）
        self._email_notifier = email_notifier  # 高价值 key 邮件通知
        # 余额变化检测阈值（规格 §3.3,默认值与 ReverifyScheduler 同款）:
        # 缩水/充值/重新激活 判定 + 邮件阈值;CSV 账本路径默认 results/balance_changes.csv
        self._hv_email_threshold = hv_email_threshold
        self._hv_top_threshold = hv_top_threshold
        self._shrink_warn_pct = shrink_warn_pct
        self.allow_chat_probe = allow_chat_probe
        self.probe_unclear = probe_unclear
        self.on_verified = on_verified
        try:
            from providers import PROVIDER_RATE_INTERVALS, ProviderRateLimiter
            self.provider_rate_limiter = provider_rate_limiter or ProviderRateLimiter(
                intervals=dict(PROVIDER_RATE_INTERVALS))
        except ImportError:
            self.provider_rate_limiter = provider_rate_limiter
        self._changes_csv = changes_csv or os.path.join("results", "balance_changes.csv")
        self._changes_lock = threading.Lock()  # 多 worker 并发写 CSV 串行化
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._counter = 0  # 入队序号，保证同优先级 FIFO
        # 计数器自增统一锁：submit/reverify/enqueue_reverify 三处共用一把锁，
        # 避免两把锁各自保护同一计数器 → 相同 counter → PriorityQueue 比较 dict 抛 TypeError
        self._counter_lock = threading.Lock()
        self._seen: set[str] = set()  # 已提交 key 去重
        self._seen_lock = threading.Lock()
        self._seen_max = 2_000_000  # 去重集上限，防止长期运行内存无限增长
        self._results: dict[str, dict] = {}  # key -> 验证结果
        self._results_lock = threading.Lock()
        self._results_max = 200_000  # 长跑防膨胀：超过则淘汰最低余额（保留高价值）
        # 重启重验：已排入重验队列的 key（每 key 只排一次）+ 重验判不合格的 key（剔除名单）
        self._reverify_queued: set[str] = set()
        self._evicted: set[str] = set()
        self._evicted_lock = threading.Lock()
        self._reverify_max = 100_000  # 防长期运行 OOM
        # 持续重验在途追踪:已排入队列但验证未完成的 key 集合
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        # 启动重验高价值汇总：启动时历史高价值 key 重验后不逐 key 发信，
        # 而是累积到这里，等重验批次结束后合并成**一封**邮件（避免启动刷屏）。
        self._reverify_hv: list[dict] = []
        self._reverify_hv_lock = threading.Lock()
        self._reverify_summary_sent = False
        self._stop = False
        self._worker_threads: list[threading.Thread] = []
        # 空闲追踪：记录当前"阻塞在 get 上、无在途验证"的 worker 线程 ID。
        # 用集合而非计数器：_worker_loop 每轮开头 mark_idle，queue.Empty 时
        # continue 跳过 mark_busy——计数器每空转 1s 净增 workers(4/s 泄漏)，
        # idle() 膨胀后误判"无在途验证"，关闭排空提前 break 丢结果。
        # 集合幂等：同一线程重复 mark_idle 不膨胀。
        self._idle_worker_tids: set = set()
        self._idle_lock = threading.Lock()
        # 统计
        self._submitted = 0
        self._genuinely_new = 0  # 真·新发现：DB 首次入库(区别于复检已知 key)
        self._verified = 0
        self._valid_count = 0
        self._hv_count = 0
        # 按平台计数已验证 key（多平台识别结果，TUI 平台分布展示）
        self._provider_counts: dict[str, int] = {}
        # 验证状态分布（valid_active / valid_zero / valid_no_balance / invalid / error）
        self._status_counts: dict[str, int] = {}
        self._rate_window: deque[tuple[float, int]] = deque()  # (time, cumulative_verified)
        self._stats_lock = threading.Lock()
        # 仅在影响 watch_state/CSV 的结果变化时递增；空闲源轮询据此跳过全量写盘。
        self._state_revision = 0
        # 连接池：每个 worker 一个 Session（requests.Session 非线程安全），
        # 避免每次验证都重建 TCP/TLS 连接。
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()
        # 验证器缓存:每 worker 线程一个 UnifiedKeyVerifier,复用注入的 per-thread
        # requests.Session,省每次验证的 TCP/TLS 握手开销(原每 key 新建 verifier+裸 requests)。
        self._verifiers: dict[int, object] = {}
        # 可选 SQLite 存储：跨运行去重 + 历史。db_path=None → 禁用，行为不变。
        # 注意：连接在主线程创建、worker 线程写入（check_same_thread=False），
        # 写入必须持 _store_lock 串行化（sqlite3 内部虽自串行，显式锁防半写视图）。
        self._store_conn = None
        self._store_lock = threading.Lock()
        if db_path:
            try:
                import store as _store
                self._store_conn = _store.connect(db_path)
                redacted = _store.redact_invalid_keys(self._store_conn)
                if redacted:
                    logging.getLogger("darkforest.store").info(
                        "SQLite invalid-key redaction completed: %d rows", redacted)
                with self._seen_lock:
                    # 种子只保留高价值 key（>min_balance）：它们是"重验才有意义"的 key，
                    # 已确认高价值则不必反复重验（省 API 调用）。
                    # 普通历史 valid key 与 invalid key 一律不占种子 → 扫描器再次扫到
                    # 时会重新提交验证（消费端有全局限速，不浪费扫描端）：
                    # key 可能被恢复/充值/重新激活，这正是"重激活检测"的价值所在。
                    # 注：早期版本误用了 all_known_keys()（SELECT 全部 key）灌入种子，
                    # 把整个历史去重集全量屏蔽 → 扫描到历史 key 全部被丢、永不重验，
                    # 直接导致长跑后"抓不到新 key"。已修正为只播种高价值 key。
                    self._seen |= _store.high_value_keys(self._store_conn, self.min_balance)
            except Exception as e:
                self._store_conn = None
                logging.getLogger("darkforest.store").warning(
                    "SQLite 存储不可用（降级为内存去重）: %s", e)

    def _get_session(self) -> requests.Session:
        """获取当前线程的持久 Session（连接复用）。"""
        tid = threading.get_ident()
        sess = self._sessions.get(tid)
        if sess is None:
            sess = requests.Session()
            with self._sessions_lock:
                self._sessions[tid] = sess
        return sess

    def _get_verifier(self):
        """获取当前线程缓存的 UnifiedKeyVerifier(注入 per-thread Session 复用连接)。

        v2.4.7: 多代理模式下,每个 worker 线程分配独立代理 IP(通过 key hash 路由),
        分散 AI 平台 API 验证请求,避免单 IP 撞到 per-IP 限速。
        """
        tid = threading.get_ident()
        v = self._verifiers.get(tid)
        if v is None:
            try:
                from providers import ALL_PROVIDERS, UnifiedKeyVerifier
                # v2.4.7: 多代理路由——每线程独立 IP
                proxy_url = getattr(self.engine, "proxy", None)
                multi_proxy = getattr(self.engine, "_multi_proxy", None)
                if multi_proxy and multi_proxy.num_proxies > 0:
                    # 用线程 ID 作为路由 key,同一线程始终走同一 IP
                    proxy_config = multi_proxy.get_proxies_for_token(f"worker_{tid}")
                    proxy_url = proxy_config.get("https") if proxy_config else proxy_url
                v = UnifiedKeyVerifier(ALL_PROVIDERS,
                                       proxy=proxy_url,
                                       session_factory=self._get_session,
                                       allow_chat_probe=self.allow_chat_probe,
                                       probe_unclear=getattr(self, "probe_unclear", True),
                                       rate_limiter=self.provider_rate_limiter)
            except Exception:
                return None
            self._verifiers[tid] = v
        return v

    def _mark_idle(self):
        with self._idle_lock:
            self._idle_worker_tids.add(threading.get_ident())

    def _mark_busy(self):
        with self._idle_lock:
            self._idle_worker_tids.discard(threading.get_ident())

    def state_revision(self) -> int:
        """返回影响 state/CSV 的最新结果版本；调用方可用于脏检查。"""
        with self._stats_lock:
            return self._state_revision

    def start(self):
        """启动消费者 worker 线程池。"""
        for _ in range(self._workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self._worker_threads.append(t)

    def stop(self):
        """停止所有 worker。"""
        self._stop = True
        for t in self._worker_threads:
            t.join(timeout=10)
        with self._sessions_lock:
            for s in self._sessions.values():
                try:
                    s.close()
                except Exception:
                    pass
            self._sessions.clear()
        for verifier in list(self._verifiers.values()):
            try:
                verifier.close()
            except Exception:
                pass
        self._verifiers.clear()  # verifier 持有的 session 来自 _sessions,已关闭
        if self._store_conn is not None:
            try:
                with self._store_lock:
                    self._store_conn.close()
            except Exception:
                pass
            self._store_conn = None

    def idle(self, ignore_stop: bool = False) -> bool:
        """全部 worker 空闲且队列空（无在途验证，含 429 退避）。

        ignore_stop=True: 退出排空专用——_stop 置位后 idle() 本身会短路为 True,
        排空循环必须绕过该短路才能真正等待在途验证完成。
        """
        if self._stop and not ignore_stop:
            return True
        with self._idle_lock:
            return (self._queue.qsize() == 0
                    and len(self._idle_worker_tids) >= self._workers)

    def submit(self, key: str, source: str = "unknown",
               repos: list[dict] | None = None, priority: int = 10,
               query: str | None = None) -> bool:
        """扫描端调用：提交一个 key 到验证队列。返回 True 表示接受（新 key）。"""
        if not key:
            return False
        # 非推理类平台 token(HuggingFace/GitHub 凭据):提取正则覆盖它们,但
        # 验证器没有对应平台 → 只会变 unknown 垃圾行(曾积累 1364 条)。直接拒收。
        if key.startswith(("hf_", "ghp_", "gho_", "ghu_", "ghs_", "github_pat_", "glpat-")):
            return False
        # 修剪所需的高价值 key 集合先单独持 _results_lock 拷贝——
        # 旧代码在 _seen_lock 内直接迭代 _results 活视图,worker 并发写时
        # 抛 "dictionary changed size during iteration",被裸 except 吞掉丢 key。
        if len(self._seen) >= self._seen_max:
            with self._results_lock:
                keep = {r.get("key") for r in self._results.values()
                        if r.get("valid") and r.get("balance_cny", 0) > self.min_balance}
            with self._seen_lock:
                if len(self._seen) >= self._seen_max:
                    self._seen = {k for k in self._seen if k in keep} | keep
        with self._seen_lock:
            if key in self._seen:
                return False
            self._seen.add(key)
        # 计数器自增统一持 _counter_lock（submit/reverify/enqueue_reverify 三处共用）
        with self._counter_lock:
            self._counter += 1
            counter = self._counter
        # priority: 0=最高（新 key），数值越大越低
        self._queue.put((priority, counter, {
            "key": key,
            "source": source,
            "repos": repos or [],
            "key_preview": key[:10] + "..." + key[-4:],
            "query": query or "unknown",
        }))
        with self._stats_lock:
            self._submitted += 1
        return True

    def submit_many(self, keys_dict: dict, source: str = "unknown",
                    query: str | None = None) -> int:
        """批量提交 {key: {repos, ...}} 字典。返回实际接受的新 key 数。"""
        accepted = 0
        for key, info in keys_dict.items():
            repos = info.get("repos", []) if isinstance(info, dict) else []
            item_query = info.get("query") if isinstance(info, dict) else None
            if self.submit(key, source=source, repos=repos, priority=0,
                           query=item_query or query):
                accepted += 1
        return accepted

    def reverify(self, key: str, source: str = "unknown",
                 repos: list[dict] | None = None) -> bool:
        """重启重验：绕过 _seen 把历史 key 重新排入验证队列（低优先级，不打断新 key）。

        返回 True 表示已排入（首次排入）；每个 key 本会话只排一次。
        """
        if not key or self._stop:
            return False
        with self._seen_lock:
            if key in self._reverify_queued:
                return False
            # 防长期运行 OOM：超限时跳过新条目（下次重启再验）
            if len(self._reverify_queued) >= self._reverify_max:
                return False
            self._reverify_queued.add(key)
        with self._counter_lock:
            self._counter += 1  # 序号自增，保证同优先级 FIFO（PriorityQueue 比较防 TypeError）
            counter = self._counter
        self._queue.put((20, counter, {  # priority 20 = 低于新 key 的 0
            "key": key,
            "source": source,
            "repos": repos or [],
            "key_preview": key[:10] + "..." + key[-4:],
        }))
        return True

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
        with self._counter_lock:
            self._counter += 1
            counter = self._counter
        self._queue.put((20, counter, {
            "key": key, "source": source, "repos": repos or [],
            "key_preview": key[:10] + "..." + key[-4:], "reverify_src": True,
        }))
        return True

    def evicted_keys(self) -> set[str]:
        """返回重验后判不合格（失效/欠费/低于阈值）的 key 集合，供保存时从 state/CSV 剔除。"""
        with self._evicted_lock:
            return set(self._evicted)

    def send_reverify_summary(self):
        """启动重验结束后，把累积的高价值 key 合并成**一封**邮件发出。

        返回发送的 key 数（0 表示无高价值或未启用邮件）。
        """
        if self._reverify_summary_sent:
            return 0
        with self._reverify_hv_lock:
            items = list(self._reverify_hv)
            self._reverify_hv.clear()
        if not items or not (self._email_notifier and self._email_notifier.enabled):
            self._reverify_summary_sent = True
            return 0
        # 按余额降序
        items.sort(key=lambda r: r.get("balance_cny", 0), reverse=True)
        self._email_notifier.send_summary(items)
        self._reverify_summary_sent = True
        return len(items)

    def all_reverify_done(self) -> bool:
        """所有已排入重验的 key 是否都已处理完（结果已写入 _results）。

        _reverify_queued 由 _seen_lock 保护、_results 由 _results_lock 保护——
        先在 _seen_lock 里快照再换锁检查,避免无锁迭代并发 add 抛
        "Set changed size during iteration"(顺序取两把锁,不嵌套,无死锁风险)。
        """
        with self._seen_lock:
            queued = list(self._reverify_queued)
        if not queued:
            return True
        with self._results_lock:
            return all(k in self._results or k in self._evicted
                       for k in queued)

    def _worker_loop(self):
        """消费者主循环（多 worker 共享）：取 key → 验证 → 保存结果 → 间隔。"""
        while not self._stop:
            # 标记为空闲（阻塞在 get 上），让排空逻辑知道本 worker 没有在途验证
            self._mark_idle()
            try:
                priority, counter, item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            # 取到任务 → 不再空闲（即使 qsize 已为 0，验证仍在进行中）
            self._mark_busy()

            try:
                # 异常护栏:一次验证/落库失败(磁盘满、CSV 权限等)不能杀死 worker——
                # daemon 线程无护栏时异常静默死亡,4 个 worker 逐个死掉后验证停滞。
                result = self._verify_one(item)
                self._store_result(result, item.get("reverify_src") or False)
            except Exception as e:
                try:
                    # 只打 key 哈希 + 异常类型,不打异常消息——
                    # str(e) 可能携带 key(如请求 URL 含 key 参数的 RequestException)。
                    key_hash = hashlib.sha256(
                        str(item.get("key", "")).encode()).hexdigest()[:8]
                    print(f"[worker] 验证异常(已跳过): "
                          f"key_hash={key_hash} {type(e).__name__}", file=sys.stderr)
                except Exception:
                    pass
                # v2.4.9: 持续重验 item 的在途登记必须释放——_store_result 中途
                # 抛异常时其内部释放段不执行,泄漏后 enqueue_reverify 恒 False,
                # 该 key 永久失去重验资格。
                if item.get("reverify_src"):
                    with self._in_flight_lock:
                        self._in_flight.discard(item.get("key", ""))

            # 限速间隔
            self._queue.task_done()
            if not self._stop:
                time.sleep(self.interval)
        # 退出循环(收到 _stop):补标空闲——否则排空等待 idle(ignore_stop=True)
        # 永远差这一个 worker,只能等满整个 shutdown 超时。
        self._mark_idle()

    def _verify_one(self, item: dict) -> dict:
        """验证单个 key（多平台验证，默认 GET-only）。

        通过 UnifiedKeyVerifier 按 key 格式识别平台 → GET models 端点验证有效性
        → 支持余额查询的平台再查余额（只读）；chat 探测仅在显式 opt-in 后发送。
        """
        key = item["key"]
        try:
            from providers import PERCENT_PROVIDERS, USD_PROVIDERS, UnifiedKeyVerifier, VerifyResult
        except Exception:
            UnifiedKeyVerifier = VerifyResult = None

        if UnifiedKeyVerifier is not None:
            try:
                verifier = self._get_verifier()  # 每 worker 线程缓存一个,复用 Session
                if verifier is None:
                    raise RuntimeError("verifier unavailable")
                # 上下文:repo/file 名称帮助识别平台(如 dashscope repo → qwen key)。
                # 无上下文时所有 sk-* 平台并列,只试前 3 个 → 7/10 平台 key 被判死。
                # v2.4.9: 把扫描查询串拼进 context——查询自带平台域名/前缀
                # (如 "api.hunyuan.cloud.tencent.com sk-"),repo 路径里几乎不会
                # 出现完整域名,identify 的 query-term 加分信号此前基本永不生效。
                repos = item.get("repos", [])
                context = item.get("query", "") or ""
                context += " " + " ".join(
                    f"{r.get('repo', '')}/{r.get('file', '')}" for r in repos[:5]
                    if isinstance(r, dict))
                v = verifier.verify_key(key, context=context)
                valid = v.get("status") in (
                    VerifyResult.VALID_ACTIVE.value,
                    VerifyResult.VALID_ZERO.value,
                    VerifyResult.VALID_NO_BALANCE.value,
                )
                balance = v.get("balance") or 0.0
                # 币种推断:PERCENT=Coding Plan 周额度百分比(zhipu_coding);
                # USD_PROVIDERS(海外平台)以 USD 计,其余以 CNY 计
                provider_id = v.get("provider", "unknown")
                if provider_id in PERCENT_PROVIDERS:
                    currency = "PERCENT"
                elif provider_id in USD_PROVIDERS:
                    currency = "USD"
                else:
                    currency = "CNY"
                return {
                    "key": key,
                    "valid": valid,
                    "status": v.get("status", ""),
                    "balance": balance,
                    "primary_currency": currency,
                    "balance_usd": self._to_usd(balance, currency),
                    "balance_cny": self._to_cny(balance, currency),
                    "repos": item.get("repos", []),
                    "source": item.get("source", "unknown"),
                    "provider": provider_id,
                    "query": item.get("query", "unknown"),
                    "key_preview": item.get("key_preview", key[:10] + "..." + key[-4:]),
                    "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    # v2.5.3: message 供 store.upsert 落 last_error——
                    # error 行此前无一句原因,断点诊断只能靠重放探测
                    "message": str(v.get("message") or "")[:200],
                }
            except Exception as e:
                # 只记异常类型(str(e) 可能携带 key,同 worker 护栏原则)
                return {"key": key, "valid": False, "status": "error",
                        "provider": "unknown", "source": item.get("source", "unknown"),
                        "message": f"verify exception: {type(e).__name__}"}

        # 回退：DeepSeek 专用验证（UnifiedKeyVerifier 不可用时）
        url = f"{self.engine.deepseek_api_base}/user/balance"
        headers = {"Authorization": f"Bearer {key}"}
        session = self._get_session()
        for attempt in range(3):
            try:
                r = session.get(url, headers=headers,
                                timeout=self.engine.timeout,
                                proxies=getattr(self.engine, "_proxies", None))
                if r.status_code == 200:
                    data = r.json()
                    balance_infos = data.get("balance_infos", [])
                    total_balance = 0.0
                    primary_currency = "USD"
                    for info in balance_infos:
                        currency = info.get("currency", "unknown")
                        total = float(info.get("total_balance", 0))
                        total_balance += total
                        if currency == "CNY":
                            primary_currency = "CNY"
                    return {
                        "key": key,
                        "valid": True,
                        "status": "valid_active",
                        "provider": "deepseek",
                        "balance": total_balance,
                        "primary_currency": primary_currency,
                        "balance_usd": self._to_usd(total_balance, primary_currency),
                        "balance_cny": self._to_cny(total_balance, primary_currency),
                        "repos": item.get("repos", []),
                    "source": item.get("source", "unknown"),
                    "query": item.get("query", "unknown"),
                    "key_preview": item.get("key_preview", key[:10] + "..." + key[-4:]),
                        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                elif r.status_code == 401:
                    return {"key": key, "valid": False, "status": "invalid",
                            "provider": "deepseek", "source": item.get("source", "unknown")}
                elif r.status_code == 429:
                    # 限流退避(可中断:退出时 _stop 立即置位,退避分段 sleep)
                    wait = min(2 ** attempt * 5, 30)
                    for _ in range(wait):
                        if self._stop:
                            return {"key": key, "valid": False, "status": "error",
                                    "provider": "deepseek", "source": item.get("source", "unknown")}
                        time.sleep(1)
                    continue
                else:
                    return {"key": key, "valid": False, "status": "error",
                            "provider": "deepseek", "source": item.get("source", "unknown")}
            except (requests.Timeout, requests.ConnectionError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return {"key": key, "valid": False, "status": "error",
                        "provider": "deepseek", "source": item.get("source", "unknown")}
            except Exception:
                return {"key": key, "valid": False, "status": "error",
                        "provider": "deepseek", "source": item.get("source", "unknown")}
        return {"key": key, "valid": False, "status": "error",
                "provider": "deepseek", "source": item.get("source", "unknown")}

    def _trim_results(self):
        """_results 超限时淘汰最低余额的有效 key，直到回到上限内。"""
        if len(self._results) <= self._results_max:
            return
        # 按余额升序排，淘汰最差的 (len - max) 个（键是完整 key，内存中按 dict 行比较即可）
        n_drop = len(self._results) - self._results_max
        for key, _ in sorted(self._results.items(),
                             key=lambda kv: (kv[1].get("balance_cny", 0), kv[0]))[:n_drop]:
            del self._results[key]

    def _store_result(self, result: dict, reverify_src: bool = False):
        """保存验证结果，更新统计。

        仅把**有效**结果写入 `_results`（→ watch_state.json / CSV）。无效 key 的去重
        由 `_seen` + SQLite 负责，无需在内存/JSON 常驻——否则 24/7 长跑下无效 key 无限
        堆积，撑爆 `_results` 与每次全量重写的 watch_state.json。计数器对所有结果照常累计。
        `reverify_src`：结果来自持续重验 item（enqueue_reverify）时为真——只有这条路径
        登记了 `_in_flight`，启动 reverify/普通 submit 不登记，故在途释放须以此为准，
        不能纯按 key 判定（同 key 异路径并发时避免误释放对方的在途登记）。"""
        key = result.get("key", "")
        if not key:
            return
        state_changed = False
        with self._stats_lock:
            self._verified += 1
            now = time.time()
            self._rate_window.append((now, self._verified))
            # 保留最近 60 秒的速率窗口
            while self._rate_window and self._rate_window[0][0] < now - 60:
                self._rate_window.popleft()
            if result.get("valid"):
                self._valid_count += 1
                if result.get("balance_cny", 0) > self.min_balance:
                    self._hv_count += 1
            # 平台/状态分布（多平台 TUI 展示；unknown/空归类 unknown）
            provider = result.get("provider") or "unknown"
            self._provider_counts[provider] = self._provider_counts.get(provider, 0) + 1
            status = result.get("status") or ("valid" if result.get("valid") else "unknown")
            self._status_counts[status] = self._status_counts.get(status, 0) + 1
        if result.get("valid"):
            with self._results_lock:
                self._results[key] = result
                if len(self._results) > self._results_max:
                    self._trim_results()
            state_changed = True
            # 回充解除剔除：重验发现余额回升越过阈值 → 从 _evicted 摘除，
            # state/CSV/TUI 才能重新收录该 key（_evicted 只增不减会让
            # "充值/重新激活检测"在会话内永远看不到回充的 key）。
            if result.get("balance_cny", 0) > self.min_balance:
                with self._evicted_lock:
                    self._evicted.discard(key)
            # 高价值 key 邮件通知（余额 > 阈值）——发送完整 key
            balance_cny = result.get("balance_cny", 0)
            is_hv = balance_cny > self.min_balance
            if (self._email_notifier and self._email_notifier.enabled and is_hv):
                if reverify_src:
                    # 持续重验：不发信——汇总邮箱只在启动时排空一次，进队列会静默丢失；
                    # 变化检测（Task 4）按缩水/充值/重新激活决定；余额没变=无新闻=不发信。
                    # 注意：此分支必须排在 `key in _reverify_queued` 之前——同 key 可能
                    # 既在启动重验名单又在持续重验轮询中，若先判 queued 会把持续重验
                    # 结果误累积进一次性汇总邮箱（发完后静默丢失 + 无界累积）。
                    pass
                elif key in self._reverify_queued:
                    # 启动重验的历史高价值 key：不逐 key 发，累积到汇总邮件（防启动刷屏）
                    with self._reverify_hv_lock:
                        self._reverify_hv.append(result)
                else:
                    # 运行中新扫描/新验证发现的高价值 key：即时单独发
                    # (币种透传:PERCENT 显示真实单位"周额度%",不发假金额)
                    self._email_notifier.send_alert(
                        key=key,
                        key_preview=result.get("key_preview", key[:10] + "..." + key[-4:]),
                        provider=result.get("provider", "unknown"),
                        balance=balance_cny,
                        currency=result.get("primary_currency", "CNY"),
                        source=result.get("source", "unknown"),
                        repos=result.get("repos", []),
                    )
            if (key in self._reverify_queued
                    and result.get("balance_cny", 0) <= self.min_balance
                    and (result.get("primary_currency") or "").upper() != "PERCENT"):
                # 重启重验后仍有效但余额已低于阈值 → 从高价值清单剔除。
                # 例外:PERCENT(Coding Plan 周额度)不剔除——额度每周重置,
                # 0% 只是暂时的,记录比剔除有价值。
                with self._evicted_lock:
                    self._evicted.add(key)
        elif key in self._reverify_queued:
            # 重启重验结果不合格 →
            # 只有**确定性无效**才永久剔除；瞬态错误（网络超时/503/rate_limited）
            # 保留历史值，下次重启再验——避免一次网络抖动就永久删除高价值 key。
            status = result.get("status", "")
            is_transient_error = status in ("error", "rate_limited")
            if not is_transient_error:
                with self._evicted_lock:
                    self._evicted.add(key)
                state_changed = True
        # v2.4.9 (P1 watch#1 修复): 持续重验(enqueue_reverify)路径的失效结果
        # 此前完全不触发剔除——剔除只挂在启动重验的 _reverify_queued 上。
        # 调度器重验某 key 判 invalid → 该 key 曾是高价值已被播种进 _seen,
        # 扫描端再扫到被拒收;DB valid=0 又让调度器停验 → 台账永久保留过期
        # "有效"行。确定性无效时剔除(对齐启动重验语义,瞬态 error 不剔)。
        if (not result.get("valid")
                and reverify_src
                and key not in self._reverify_queued
                and result.get("status", "") == "invalid"):
            with self._evicted_lock:
                self._evicted.add(key)
            state_changed = True
        # 已知 key（在 _results 里有旧 valid 行）被确定性判 invalid → 从内存结果
        # 移除。否则该行在 DB(valid=0,调度器不再选)、启动重验名单(仅 balance>
        # 阈值)、剔除名单(仅 reverify 路径)四条清理路径全部够不到,台账每次保存
        # 都用旧对象续写,永久保留过期"有效"行。
        if (not result.get("valid")
                and result.get("status", "") == "invalid"
                and key in self._results):
            with self._results_lock:
                if self._results.pop(key, None) is not None:
                    state_changed = True
        # v2.4.9 (Y5): 首验遇瞬态错误(限流风暴/网络抖动/候选全 rate_limited)
        # 的 key 此前被 _seen 永久挡住,本会话不再验证——真 key 直接丢。
        # 从 _seen 释放,下轮扫描再遇到时重新提交(消费端限速保证不会风暴)。
        # 确定性 invalid 保留 _seen(验证已花过成本,库里有账)。
        if (not result.get("valid")
                and not reverify_src
                and result.get("status") in ("error", "rate_limited")):
            with self._seen_lock:
                self._seen.discard(key)
        if state_changed:
            with self._stats_lock:
                self._state_revision += 1
        # 持久化到 SQLite（若启用）— 历史 + 下次运行的去重种子
        if self._store_conn is not None:
            try:
                import store as _store
                with self._store_lock:
                    # upsert 返回 True = 全新 key(首个 first_seen)；False = 复检已知 key。
                    # 两者分开计数:"提交"是会话内新见(含复检),"真·新发现"才是净新增。
                    is_new = _store.upsert(self._store_conn, result)
                    if is_new:
                        with self._stats_lock:
                            self._genuinely_new += 1
            except Exception as e:
                # 记日志而非静默：存储失败不影响验证/统计，但会丢失跨运行去重
                logging.getLogger("darkforest.store").warning(
                    "SQLite upsert 失败 (key=%s...): %s", key[:8], e)
        # 持续重验在途释放（仅持续重验路径：启动 reverify / 普通 submit 不登记在途，不应误释放）
        # 放 finally 语义:后续余额历史/CSV/邮件任一抛异常也不能泄漏——
        # 泄漏后 enqueue_reverify 永远返回 False,该 key 永久失去重验资格。
        try:
            if reverify_src and key in self._in_flight:
                with self._in_flight_lock:
                    self._in_flight.discard(key)
        except Exception:
            pass
        if self._store_conn is not None and result.get("valid"):
            prev = None
            try:
                import store as _store
                with self._store_lock:
                    # 写新历史前读上一笔（同一把锁内 → 与 record_history 原子配对）
                    prev = _store.get_last_history(self._store_conn, _store.hash_key(key))
                    _store.record_history(self._store_conn, result)
            except Exception:
                pass
            try:
                if reverify_src or key in self._reverify_queued:
                    change_type, notify_email = detect_balance_change(
                        prev, result, self._hv_email_threshold, self._hv_top_threshold,
                        self._shrink_warn_pct)
                    if change_type is not None:
                        with self._changes_lock:
                            log_balance_change(self._changes_csv, result, prev, change_type)
                        if notify_email and self._email_notifier and self._email_notifier.enabled:
                            self._email_notifier.send_alert(
                                key=key,
                                key_preview=result.get("key_preview", key[:10] + "..." + key[-4:]),
                                provider=result.get("provider", "unknown"),
                                balance=result.get("balance_cny", 0),
                                # v2.4.9: 透传真实币种——PERCENT(智谱周额度%)此前被硬编码
                                # 成 CNY 金额,邮件显示"¥45"实为"45%"
                                currency=result.get("primary_currency", "CNY"),
                                source=result.get("source", "unknown"),
                                repos=result.get("repos", []),
                            )
            except Exception as e:
                # 变化检测/CSV/邮件失败只影响预警,不影响 key 结果本身
                logging.getLogger("darkforest.watch").warning(
                    "balance change 处理失败 (key=%s...): %s", key[:8], e)

        # 放最后：查询收益反馈消费最终结果；异常只影响排序学习，不影响验证。
        if self.on_verified is not None:
            try:
                self.on_verified(result)
            except Exception as e:
                logging.getLogger("darkforest.watch").warning(
                    "query outcome callback failed: %s", e)

    def _to_usd(self, balance: float, currency: str) -> float:
        # PERCENT 透传:阈值/台账按百分点理解(周重置,0% 也记录);
        # 金额汇总(TUI 总值等)在求和点排除 PERCENT
        if currency.upper() == "CNY":
            return balance / self.engine.usd_cny_rate if self.engine.usd_cny_rate > 0 else 0
        return balance

    def _to_cny(self, balance: float, currency: str) -> float:
        if currency.upper() == "USD":
            return balance * self.engine.usd_cny_rate
        return balance

    def get_all_results(self) -> list[dict]:
        """返回所有验证结果列表。"""
        with self._results_lock:
            return list(self._results.values())

    def get_high_value(self) -> list[dict]:
        """返回高价值 key（valid + balance_cny > min_balance），按余额降序。"""
        with self._results_lock:
            hv = [r for r in self._results.values()
                  if r.get("valid") and r.get("balance_cny", 0) > self.min_balance]
        return sorted(hv, key=lambda r: r["balance_cny"], reverse=True)

    def snapshot(self) -> dict:
        """返回验证队列状态快照。"""
        with self._stats_lock:
            pending = self._queue.qsize()
            verified = self._verified
            valid = self._valid_count
            hv = self._hv_count
            submitted = self._submitted
        # 计算最近 60 秒速率
        now = time.time()
        with self._stats_lock:
            while self._rate_window and self._rate_window[0][0] < now - 60:
                self._rate_window.popleft()
            rate = len(self._rate_window)  # 最近 60 秒验证数
        return {
            "submitted": submitted,
            "genuinely_new": getattr(self, "_genuinely_new", 0),
            "pending": pending,
            "verified": verified,
            "valid": valid,
            "hv": hv,
            "rate_per_min": rate,
            "queue_size": pending,
            "provider_counts": dict(self._provider_counts),
            "status_counts": dict(self._status_counts),
        }


# ══════════════════════════════════════════════════════════════════
#  ReverifyScheduler — 持续重验调度线程（分层间隔 + 预算令牌桶）
# ══════════════════════════════════════════════════════════════════

class ReverifyScheduler:
    """持续重验调度器:独立线程,按预算令牌桶 + 分层间隔轮询历史有效 key。

    分层: >=top_threshold 每 6h / >=email_threshold 每 12h / 其余按预算均摊。
    入队走 broker.enqueue_reverify(绕过 _reverify_queued,在途不重复)。
    """

    def __init__(self, broker, store_conn, budget_per_day: int = 1500,
                 email_threshold: float = 5.0, top_threshold: float = 10.0,
                 shrink_pct: float = 30.0,
                 zero_interval_hours: int = 168,
                 changes_csv: str = os.path.join("results", "balance_changes.csv"),
                 store_lock=None):
        self.broker = broker
        self._conn = store_conn
        self._store_lock = store_lock  # broker._store_lock:与 worker 写串行化(共享连接)
        self._zero_interval = zero_interval_hours * 3600  # 0 余额 key 周级冷却(充值检测保留但降频)
        self._budget_per_day = max(1, budget_per_day)
        self._email_threshold = email_threshold
        self._top_threshold = top_threshold
        self._shrink_pct = shrink_pct
        self._changes_csv = changes_csv
        # 速率补充令牌桶（规格 §3.1）:预算 1500/天 平滑分布在 24h(≈1.7/min),
        # 桶容量 10 封顶突发——而非每日重置计数器(首轮全量烧光,6h/12h 分层稳态失效)。
        self._bucket_capacity = 10.0
        self._tokens = float(min(self._bucket_capacity, self._budget_per_day))
        self._last_refill = time.time()  # 上次补充时间(速率补充,非每日重置)
        self._last_verified: dict[str, float] = {}  # key -> 上次重验 timestamp
        self._log_error_once = False  # _loop 故障只记首错,不刷屏
        self._stop = False
        self._thread = None

    # 分层间隔(秒)
    _TIER_INTERVAL_TOP = 6 * 3600      # >= top_threshold
    _TIER_INTERVAL_HV = 12 * 3600      # >= email_threshold
    # _TIER_INTERVAL_OTHER 改为实例属性(v2.4.9):与 budget_per_day 解耦——
    # 硬编码 /1500 时 --reverify-budget 300 的令牌补充速率跟不上均摊间隔,饿死。
    _TIER_INTERVAL_OTHER_DEFAULT = 7 * 24 * 3600 / 1500.0  # 预算 1500 时的默认(~403s)

    @staticmethod
    def _parse_ts(s) -> float:
        """'YYYY-MM-DD HH:MM:SS' → epoch;解析失败返回 0。"""
        try:
            return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            return 0.0

    def _query(self, sql: str, args: tuple = ()) -> list:
        """只读查询:持 broker._store_lock 与 worker 的写串行化。

        sqlite 连接 check_same_thread=False 共享于主线程/worker/调度线程,
        读写交错会锁错乱;持有同一把锁即可安全读。"""
        if self._store_lock is not None:
            with self._store_lock:
                return self._conn.execute(sql, args).fetchall()
        return self._conn.execute(sql, args).fetchall()

    def _tier_interval(self, balance: float, trend: float = 0.0) -> float:
        """分层间隔(秒),支持动态频率:余额下降快的 key 重验更勤。

        0 余额 key(valid_zero/valid_no_balance)走周级冷却:充值检测保留但降频,
        把每日预算让给有余额的活跃 key(实测 0 余额回充率为 0,高频重验无收益)。
        有余额 key:trend < 0(余额持续下降)→ 间隔减半(加速检测缩水/耗尽);
        trend > 0(余额上升)→ 间隔翻倍(稳定期少打扰);无趋势 → 基础分层。
        """
        if balance <= 0:
            return float(self._zero_interval)
        # v2.4.9: OTHER 层间隔按实际预算均摊(硬编码 /1500 与 budget 解耦,
        # 小预算配置下会饿死)
        base = 7 * 24 * 3600 / float(self._budget_per_day)
        if balance >= self._top_threshold:
            base = self._TIER_INTERVAL_TOP
        elif balance >= self._email_threshold:
            base = self._TIER_INTERVAL_HV
        # 动态调整:下降趋势 -50%,上升趋势 +100%
        if trend < -0.01:
            return max(60.0, base * 0.5)
        if trend > 0.01:
            return base * 2.0
        return base

    def _refill_tokens(self):
        """速率补充令牌:每秒补充 budget/86400,封顶桶容量(10,≤预算)。

        每日重置桶的缺陷:启动首轮把 1500 预算全量烧光(几分钟内),之后整天
        无令牌 → HV 层(6h/12h 间隔)稳态下到点也没令牌触发。速率补充桶把
        预算平滑铺到 24h,每 tick 按流逝秒数补充,稳态 ≈1.7 次/分钟。
        """
        now = time.time()
        elapsed = now - self._last_refill
        self._last_refill = now
        cap = min(self._bucket_capacity, float(self._budget_per_day))
        if self._tokens < cap:
            self._tokens = min(self._tokens + self._budget_per_day * elapsed / 86400.0, cap)

    def _last_verify_ts(self, key: str, prev: dict | None) -> float:
        """上次验证时间戳(分层间隔起点):key_history 优先,回退 keys.last_seen。

        重启后从库继承间隔——否则首次 _tick 会把全量历史 key 立即排入
        (违反 6h/12h/均摊 的分层节奏);两者都无则返回 0(视为可立即重验)。
        """
        if prev is not None:
            ts = self._parse_ts(prev.get("verified_at"))
            if ts > 0:
                return ts
        try:
            rows = self._query("SELECT last_seen FROM keys WHERE key = ?", (key,))
            if rows:
                return self._parse_ts(rows[0]["last_seen"])
        except Exception:
            pass
        return 0.0

    def _tick(self) -> int:
        """一轮调度:选候选 key 入队,返回入队数。"""
        self._refill_tokens()
        if self._conn is None or self._tokens <= 0:
            return 0
        now = time.time()
        # 全量候选(无 500 截断):低余额 key 也必须进入轮询池,否则第 500 名
        # 之后的 key 永不重验——0 余额充值检测的靶人群全被饿死。
        try:
            rows = self._query(
                "SELECT key, balance, last_seen FROM keys WHERE valid = 1")
        except Exception:
            return 0
        candidates = [{"key": r["key"], "balance_cny": float(r["balance"] or 0),
                       "last_seen": r["last_seen"]} for r in rows]

        def _due(c: dict) -> float:
            """到期时间:上次验证(内存优先,库 last_seen 回退)+ 分层间隔。"""
            last = self._last_verified.get(c["key"], 0.0)
            if last <= 0:
                last = self._parse_ts(c.get("last_seen") or "")
            return last + self._tier_interval(c.get("balance_cny", 0))

        # 到期优先:已到期组先耗预算,未到期组排后(被 gate 跳过,不浪费预算);
        # 到期组内按余额降序——首轮预算有限时高价值先重验,运行中高余额
        # 间隔短(6h/12h)、到期频繁,持续赢得令牌竞争;低余额 403s 到期,
        # 预算有剩即被轮到,不会被高价值排挤。
        candidates.sort(key=lambda c: (0 if now >= _due(c) else 1,
                                       -c.get("balance_cny", 0)))
        enqueued = 0
        for c in candidates:
            if self._tokens <= 0:
                break
            key = c.get("key", "")
            if not key:
                continue
            last = self._last_verified.get(key, 0.0)
            trend = 0.0
            import store as _store
            try:
                kh = _store.hash_key(key)
                lock = self._store_lock if self._store_lock is not None else nullcontext()
                with lock:  # 持锁读:防与 worker 写交错(显式锁契约)
                    prev = _store.get_last_history(self._conn, kh)
                    # 余额趋势(最近 5 次):下降快 → 间隔减半加速检测
                    trend = _store.get_balance_trend(self._conn, kh, 5)
            except Exception:
                prev = None
                trend = 0.0
            interval = self._tier_interval(c.get("balance_cny", 0), trend)
            if last <= 0:
                # 无本会话记录 → 以库内上次验证时间初始化间隔起点(重启继承)
                last = self._last_verify_ts(key, prev)
            if now - last < interval:
                continue
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
                except Exception as e:
                    # 故障不静默:首错记日志(后续静默重试,不刷屏)
                    if not self._log_error_once:
                        self._log_error_once = True
                        logging.getLogger("darkforest.watch").warning(
                            "reverify-scheduler 异常: %s", e)
                time.sleep(60)

        self._thread = threading.Thread(target=_loop, daemon=True, name="reverify-scheduler")
        self._thread.start()

    def stop(self):
        self._stop = True
        # 等调度线程退出再返回(最长 ~tick 间隔)——关闭顺序:
        # scheduler → broker,防止 _tick 打到已关闭连接。
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)


# ══════════════════════════════════════════════════════════════════
#  WatchState — 线程安全共享状态（per-source 独立追踪）
# ══════════════════════════════════════════════════════════════════

class WatchState:
    """主线程（TUI）与各数据源工作线程之间的线程安全共享状态。"""

    def __init__(self, broker: VerificationBroker | None = None):
        self._lock = threading.Lock()
        self.broker = broker  # 验证队列 broker（用于 snapshot 拉取验证统计）
        self._source_status: dict = {}   # {source: {phase, round, keys, submitted}}
        # per-source 累计提交计数（单调递增；TUI"提交"卡片显示累计值，不再每轮骤降）
        self._source_submitted: dict[str, int] = {}
        self.high_value_keys: list[dict] = []
        self._logs: deque = deque(maxlen=10)
        self.start_time = time.time()
        self.should_exit = False
        self._flash_until: dict = {}
        self._new_key_flash: dict = {}
        self._log_file = None  # 文件句柄，设置后每条日志同步写入
        # 看门狗心跳：最后活动时间（每次 add_log 刷新；卡死检测阈值）
        self._last_activity = time.time()

    def seed_from_history(self, results: list[dict], broker: VerificationBroker | None = None,
                          min_balance: float = 1.0, seen: set[str] | None = None):
        """启动时用历史结果（watch_state.json / 旧会话）回填 TUI 与去重集。

        - 高价值 key（balance_cny > min_balance）直接回显到表格，重启不空白
        - 若传入 broker：**只把高价值 key** 播种进 broker._seen——普通历史 key
          重新走验证队列（可能余额变化/重新激活），扫描器扫到即提交不被去重拒
        - 传入 seen 集合时同样只加高价值 key
        """
        hv = [dict(r) for r in results
              if r.get("valid") and r.get("balance_cny", 0) > min_balance]
        hv.sort(key=lambda r: r["balance_cny"], reverse=True)
        seed_keys = {r.get("key") for r in hv if r.get("key")}
        with self._lock:
            self.high_value_keys = hv
            if seen is not None:
                seen.update(seed_keys)
        if broker is not None:
            with broker._seen_lock:
                broker._seen.update(seed_keys)
        return None

    def add_source_submitted(self, source: str, n: int):
        """数据源单轮提交 n 个 key → 累计计数加 n（TUI 卡片单调递增）。"""
        if n <= 0:
            return
        with self._lock:
            self._source_submitted[source] = self._source_submitted.get(source, 0) + n

    def source_total_submitted(self, source: str) -> int:
        """读取某数据源的累计提交数。"""
        with self._lock:
            return self._source_submitted.get(source, 0)

    # 日志轮换：单文件上限 50MB，保留 5 个备份
    _LOG_MAX_BYTES = 50 * 1024 * 1024
    _LOG_BACKUP_COUNT = 5

    def set_log_file(self, path: str):
        """设置日志文件路径，后续每条日志实时写入文件（自动轮换）。"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._log_path = path
        self._log_file = open(path, "a", encoding="utf-8")
        self._log_bytes = os.path.getsize(path) if os.path.exists(path) else 0

    def _rotate_log(self):
        """日志轮换：log → log.1 → log.2 → ... → log.N（删除最旧）。"""
        try:
            self._log_file.close()
        except Exception:
            pass
        # 删除最旧的备份
        for i in range(self._LOG_BACKUP_COUNT, 0, -1):
            src = f"{self._log_path}.{i}"
            dst = f"{self._log_path}.{i + 1}"
            if i == self._LOG_BACKUP_COUNT:
                if os.path.exists(src):
                    os.remove(src)
            elif os.path.exists(src):
                os.rename(src, dst)
        # 当前文件 → .1
        if os.path.exists(self._log_path):
            os.rename(self._log_path, f"{self._log_path}.1")
        # 打开新文件
        self._log_file = open(self._log_path, "a", encoding="utf-8")
        self._log_bytes = 0

    def touch_activity(self):
        """仅刷新看门狗心跳，不产生日志/文件 I/O。

        resting/dormant/skipped 等静默轮次不写任何日志，若不显式喂心跳，
        外部源-only 配置下全局 last_activity 静止超阈值会被看门狗误判
        "全线程卡死"而自杀重启，形成 跑半小时→重启→再休眠 死循环。
        """
        with self._lock:
            self._last_activity = time.time()

    def add_log(self, msg: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._logs.append({"timestamp": ts, "level": level, "message": msg})
            # 心跳：任何日志都刷新最后活动时间（看门狗据此判卡死）
            self._last_activity = time.time()
        if self._log_file:
            try:
                # 文件 I/O 段整体持锁:多线程(源 worker+看门狗+主线程)并发写时
                # 行交错、_log_bytes 读改写竞态、轮换 close 时写已关闭句柄。
                with self._lock:
                    if self._log_file is None:
                        return
                    full_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    line = f"[{full_ts}] [{level.upper():7s}] {msg}\n"
                    self._log_file.write(line)
                    self._log_file.flush()
                    self._log_bytes += len(line.encode("utf-8"))
                    # 日志轮换：超过上限时自动轮换
                    if self._log_bytes >= self._LOG_MAX_BYTES:
                        self._rotate_log()
            except Exception:
                pass

    @property
    def last_activity(self) -> float:
        """最近一次日志活动时间（看门狗心跳）。"""
        with self._lock:
            return self._last_activity

    def close_log_file(self):
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass

    def set_source_status(self, source: str, phase: str, round_num: int = 0,
                          keys: int = None):
        """更新单个数据源的状态（线程安全，仅更新提供的字段）。

        keys 为**累计提交数**（单调递增）——TUI 来源行与"提交"卡片均显示累计，
        轮次交替时不再骤降。扫描路径用 add_source_submitted 累加后传入。
        """
        with self._lock:
            old = self._source_status.get(source, {})
            old["phase"] = phase
            old["round"] = round_num
            if keys is not None:
                old["keys"] = keys
            elif "keys" not in old:
                old["keys"] = 0
            self._source_status[source] = old

    def get_source_status(self, source: str, default: dict | None = None) -> dict:
        """读取单个数据源的状态（线程安全）。"""
        with self._lock:
            return dict(self._source_status.get(source, default or {}))

    def set_high_value_keys(self, keys: list[dict]):
        """更新合并后的高价值 key 列表，追踪新增 key 用于动画高亮。"""
        with self._lock:
            old_keys = {k["key"] for k in self.high_value_keys}
            now = time.time()
            for r in keys:
                if r["key"] not in old_keys:
                    self._new_key_flash[r["key"]] = now + 5.0
            self.high_value_keys = keys

    def snapshot(self) -> dict:
        """返回状态快照，供 TUI 渲染读取。"""
        with self._lock:
            total_submitted = sum(self._source_submitted.values())
            total_hv = len(self.high_value_keys)
            # PERCENT(Coding Plan 周额度%)不是钱——阈值按百分点理解,
            # 但 ¥ 合计只算真金额(此前 85% 会被当 ¥85 计入总值)
            total_cny = sum(r.get("balance_cny", 0) for r in self.high_value_keys
                            if (r.get("primary_currency") or "").upper() != "PERCENT")
            active = sum(1 for s in self._source_status.values()
                         if s.get("phase") in ("scanning", "verifying", "saving"))
            snap = {
                "source_status": {k: dict(v) for k, v in self._source_status.items()},
                "stats": {
                    "submitted": total_submitted,
                    "high_value": total_hv,
                    "total_cny": total_cny,
                },
                "high_value_keys": list(self.high_value_keys),
                "logs": list(self._logs),
                "elapsed": time.time() - self.start_time,
                "flash_until": dict(self._flash_until),
                "new_key_flash": dict(self._new_key_flash),
                "source_count": len(self._source_status),
                "active_count": active,
            }
        # 从 broker 拉取验证队列统计（在锁外调用，broker 自身线程安全）
        if self.broker:
            snap["verify"] = self.broker.snapshot()
        else:
            snap["verify"] = {"submitted": 0, "pending": 0, "verified": 0,
                              "valid": 0, "hv": 0, "rate_per_min": 0, "queue_size": 0}
        return snap


# ══════════════════════════════════════════════════════════════════
#  WatchScanner — 每个数据源独立循环
# ══════════════════════════════════════════════════════════════════

# 各数据源搜索词补充（在平台词轮换之上叠加的源特有词）
# 关键：外部源 0 产出的根因是只搜 "deepseek"——deepseek 官方 SDK 在 npm/HF
# 上占多数且无泄露；代理/轮转器类包（kimi-rotator/qwen-api-proxy 等）才是
# 泄露高发区，需用各平台词才能命中。每轮轮换一个平台（_platform_search_terms
# 按轮次取模），覆盖全部平台。
SOURCE_SEARCH_TERMS = {
    # HF：泄露特征词——free-endpoint/proxy 类 space 是 key 泄露高发区
    "huggingface": ["free endpoint", "api key", "proxy"],
    # GitLab 匿名只有 projects/issues 搜索（code search 需 token 401）。
    # 用泄露特征词命中"项目名/描述即泄露"的项目，比平台名更直接
    "gitlab": ["api key", "sk-", "api token"],
    "gitee": [],
    "pypi": [],                    # 重源：下载 tarball，只用平台词
    "npm": [],                     # 重源：下载 tarball，只用平台词
    "docker": [],
    "codeberg": ["sk-"],
}

# 外部源多平台搜索词池：每轮轮换一个平台，N 轮覆盖全部。
# 含平台名 + 常见泄露特征词（api/key/proxy/rotator）。
_PLATFORM_SEARCH_POOL = [
    ["deepseek", "deepseek api"],
    ["kimi", "moonshot", "kimi api"],
    ["qwen", "dashscope", "qwen api"],
    ["zhipu", "bigmodel", "zhipu api"],
    ["claude", "anthropic"],
    ["minimax"],
    ["doubao", "volces"],
    ["baichuan"],
]

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

# github_search 使用 build_active_queries() 加载完整的 121 条精选查询
# （与 deepseek 命令相同的查询集），保证最大覆盖率

# 默认数据源：github_search（主力）+ github_commits（最近提交 diff，新鲜度最高）
# + npm（唯一有产出的外部源）
# 非核心源（97% 零产出）默认关闭，用 --sources 显式启用
# 可通过 --sources 手动添加
DEFAULT_WATCH_SOURCES = [
    "github_search",
    "github_commits",   # 最近提交 diff 里的 key(新鲜度最高,配额独立于 code search)
    "github_events",    # PushEvent 实时流(分钟级新鲜,core API 配额,独立于 Code Search)
    "gitlab",           # 项目内 blob 搜索(实测 255 项目→1 key,data 字段直接提取)
    "npm",              # 唯一有产出的外部源 (13/2702 db rows)
    # 以下默认关闭，需 --sources 显式启用
    # "huggingface", "paste_sites", "docker",
]


# 平台查询判别：查询含任一平台 api_base 域名即视为"多平台查询"。
# 用于收益排序前的平台优先（多平台查询是新增量，deepseek 已扫 47+ 轮）。
# 兼容层查询（openai/anthropic 接口 + 第三方 key，换皮项目）也是新增量——
# 它们不指定平台，但扫出的 sk- key 由识别器路由到正确平台。
_PLAT_DOMAINS = (
    "moonshot", "bigmodel", "dashscope", "minimaxi", "volces",
    "baichuan-ai", "lingyiwanwu", "xiaomimimo", "stepfun",
    "sensenova", "anthropic", "01.ai", "open.bigmodel",
    # v2.4.1 第二批平台 + 首批国际平台(此前缺失,拿不到首轮优先)
    "openai.com", "openrouter.ai", "api.x.ai", "nvidia",
    "modelscope", "baidubce", "hunyuan.cloud", "longcat",
    "siliconflow", "together.xyz", "fireworks.ai", "novita.ai",
    "deepinfra", "jina.ai", "voyageai", "replicate.com",
    "sk-proj-", "nvapi-", "bce-v3", "aiza",
)
_COMPAT_QUERY_MARKERS = ("openai", "anthropic", "base_url", "OPENAI_API_KEY")


# 外部源多词扩展：从生成池取平台词（纯平台查询子集，避免泛词拖慢轮换）
# 加载 queries_generated.txt 中命中平台域的查询,追加进轮换池(上限 60,防轮换过长)
def _load_platform_terms() -> list[str]:
    try:
        from query_rotation import load_generated_pool
        pool = load_generated_pool()
        return [q for q in pool if any(d in q for d in _PLAT_DOMAINS)][:60]
    except Exception:
        return []


# 统一单词轮换序列：展平平台词 + 兼容词 + 中文别名 + 通用泄露特征 + 生成池平台词。
# 每轮 1 词（_scan_external 按 source_round 取模），~45+ 轮全覆盖。
_ROTATION = (
    [w for pool in _PLATFORM_SEARCH_POOL for w in pool]
    + _COMPAT_TERMS
    + _CN_ALIAS_POOL
    + ["sk-"]
    + _load_platform_terms()
)


def _is_platform_query(query: str) -> bool:
    ql = query.lower()
    if any(d in ql for d in _PLAT_DOMAINS):
        return True
    # 兼容层查询（不指定平台但产出跨平台 key）
    return any(m.lower() in ql for m in _COMPAT_QUERY_MARKERS)


class WatchScanner:
    """每个数据源在独立线程中运行扫描循环。
    扫描 → submit 到 VerificationBroker → 休息 → 重复。
    验证完全由 Broker 的 worker 线程负责，扫描端不验证。"""

    def __init__(
        self,
        state: WatchState,
        broker: VerificationBroker,
        proxy: str | None = None,
        proxy_subscription: str | None = None,
        concurrency: int = 15,
        interval: int = 300,
        min_balance: float = 1.0,
        sources: list[str] | None = None,
        include_github: bool = False,
        once: bool = False,
        output_dir: str = "./results",
        github_pages: int = 1,
        fresh: bool = False,
        commits_since_hours: int | None = None,
        shared_engine=None,
    ):
        self.state = state
        self.broker = broker
        self.proxy = proxy
        self.proxy_subscription = proxy_subscription
        self.concurrency = concurrency
        self.interval = interval
        self.min_balance = min_balance
        self.commits_since_hours = commits_since_hours  # None → engine 回退 config
        self.sources = list(sources) if sources else list(DEFAULT_WATCH_SOURCES)
        if include_github and "github_search" not in self.sources:
            self.sources.insert(0, "github_search")
        self.once = once
        # github_search 每查询取的页数。1 页 ≈ 半数 API 调用 → watch 轮次快一倍，
        # 配合查询轮转在多轮间覆盖完整结果。GitHub Code Search 10次/分钟/token 是硬上限。
        self._github_pages = max(1, github_pages)
        # 查询变异：每轮最多派生多少条变体（0=关闭）。变体来自历史 TOP 收益查询。
        self._max_mutants = 5
        self.output_dir = output_dir
        self.csv_path = os.path.join(output_dir, "watch_high_value.csv")
        self.state_path = os.path.join(output_dir, "watch_state.json")
        # v2.4.9: 复用 run_watch 创建的共享引擎——此前本类懒加载自建第二个
        # 引擎,两个引擎各带 proxy_subscription 各自启动一份 mihomo:第一份
        # 占住全部 17890+ 端口,第二份变无端口空壳(日志只显示后者的启动
        # 流程,前者静默无日志),退出清理也只会停空壳,真 mihomo 永远泄漏。
        self._engine = shared_engine
        self._engine_lock = threading.Lock()
        self._save_lock = threading.Lock()  # CSV/state 文件写入锁（防多线程冲突）
        self._last_save = 0.0               # 节流：长跑时避免高频全量重写
        self._save_min_interval = 15.0
        self._saved_broker_revision = -1    # -1 表示尚未保存过当前进程视图
        self._worker_threads: list = []
        # 历史缓存:启动时一次性 load_history,后续保存只做内存合并,
        # 不再每 15s 反复读 1.5MB JSON + 全表 SELECT + CSV(随账本线性增长的 I/O)。
        self._history_map: dict[str, dict] = {
            r["key"]: r for r in load_history(output_dir) if r.get("key")}
        self._provider_hints: dict[str, str] = {
            k: r["provider"] for k, r in self._history_map.items()
            if (r.get("provider") or "").strip() and r.get("provider") != "unknown"}
        # 查询轮转器（每轮使用不同查询子集）；state 文件持久化轮次，重启续跑。
        # fresh=True 时从头开始（清除已存轮次）。
        # 多平台查询：deepseek 查询库 ∪ 12 家平台特征查询（kimi/智谱/qwen/豆包等），
        # 让 watch 持续扫所有平台的 key 而非只扫 deepseek（deepseek 公开池已近枯竭）。
        from query_rotation import QueryRotator, load_queries
        all_queries = list(load_queries())
        try:
            from providers import QueryGenerator
            for q in QueryGenerator.generate_all(max_per_provider=15):
                all_queries.append(q["query"])
        except Exception:
            pass
        # 去重保序
        seen = set()
        unique = [q for q in all_queries if not (q in seen or seen.add(q))]
        self._rotator_state_file = os.path.join(output_dir, "rotator_state.json")
        self._rotator = QueryRotator(num_buckets=4, queries=unique,
                                     state_file=self._rotator_state_file)
        # 扩展查询池：每轮抽一小批 queries_generated.txt 里的新查询试水，
        # 探索平台盲区（kimi/minimax/doubao/claude 等）而不淹没配额。
        self._gen_pool = None
        try:
            from query_rotation import GeneratedPool
            gp = GeneratedPool(batch=self._gen_pool_batch if hasattr(self, "_gen_pool_batch") else 12)
            if len(gp) > 0:
                self._gen_pool = gp
        except Exception:
            self._gen_pool = None
        if fresh:
            self._rotator.reset()

    def _get_engine(self):
        with self._engine_lock:
            if self._engine is None:
                from scanner_engine import ScannerEngine
                self._engine = ScannerEngine(
                    concurrency=self.concurrency,
                    timeout=15,
                    scan_pages=3,
                    max_duration=0,
                    max_valid_keys=0,
                    output_dir="./results",
                    proxy=self.proxy,
                    proxy_subscription=self.proxy_subscription,
                    log_callback=lambda msg, level="info": self.state.add_log(msg, level),
                )
            # github_commits 源时间窗口（小时）：None → registry 回退 config.ini
            self._engine.commits_since_hours = self.commits_since_hours
        return self._engine

    def _get_all_tokens(self) -> list[str]:
        """返回所有可用的 GitHub Token 列表（可能为空 → 匿名单线程）。"""
        from scanner_engine import ScannerEngine
        tokens = ScannerEngine.get_all_gh_tokens()
        return tokens if tokens else [""]  # 空串代表匿名（_scan_one_query 内部处理）

    def run(self):
        """启动每个数据源的独立扫描线程，立即返回。所有源同时启动。"""
        for source in self.sources:
            self.state.set_source_status(source, "idle", 0)
        for source in self.sources:
            t = threading.Thread(
                target=self._source_worker,
                args=(source,),
                daemon=True,
            )
            t.start()
            self._worker_threads.append(t)
        # 看门狗：检测全线程静默卡死（如 GitHub API 连接雪崩导致所有请求
        # 在退避中空转），超过阈值无任何日志活动 → 自动重启进程自愈。
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    def _watchdog_loop(self, stall_seconds: int = 300, check_interval: int = 30):
        """看门狗循环：全线程心跳监控。

        卡死判据：超过 stall_seconds 没有任何日志活动（add_log 刷新心跳）。
        正常运行时 github 429 退避/提取/外部源轮次都会持续产生日志，
        连续 5 分钟静默 = 线程集体空转（无法从内部恢复）→ 进程自杀重启。
        重启后从 rotator_state / watch_state 续跑（不丢轮次与已捕获 key）。
        """
        while not self.state.should_exit:
            time.sleep(check_interval)
            idle = time.time() - self.state.last_activity
            if idle <= stall_seconds:
                continue
            try:
                self.state.add_log(
                    f"⛔ 看门狗：{idle:.0f}s 无任何活动（疑似卡死），自动重启进程",
                    "error")
                time.sleep(2)  # 让告警落盘
                # 自杀重启：环境变量标记看门狗重启，新进程启动时记录。
                # Windows 无 os.execv（spawn+exit 不可靠，父进程可能残留）→
                # 用 subprocess 拉起新进程 + 硬退出当前进程。
                os.environ["DARKFOREST_WATCHDOG_RESTART"] = "1"
                import subprocess as _sp
                import sys as _sys
                _sys.stdout.flush()
                try:
                    _sp.Popen([_sys.executable, os.path.abspath(_sys.argv[0])] + _sys.argv[1:],
                              creationflags=getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0))
                except Exception:
                    pass
                os._exit(0)  # 硬退出（不跑 finally/atexit，防残留）
            except Exception as e:
                # 重启失败（罕见）：记录并继续循环，避免无声退出
                self.state.add_log(f"看门狗重启失败: {e}", "error")
                time.sleep(60)

    # 外部源降频：github_search 是主源（每轮必扫），外部源每 N 轮才扫一次。
    # 外部源收益低（1141 个 db 行里仅 1 行来自外部源）但每轮扫描要 60-90s，
    # 全频扫描浪费线程时间与日志噪音；低频轮换把预算留给 github_search。
    _EXTERNAL_SOURCE_EVERY_N_ROUNDS = 10  # P1-1: 外部源降频 每3轮 -> 每10轮
    # Task 4.1: 动态休眠 — 连续零产出自动休眠，只偶尔探测恢复
    _DORMANT_AFTER_ZERO_ROUNDS = 50
    _DORMANT_PROBE_INTERVAL = 50

    def _source_worker(self, source: str):
        """单个数据源的独立循环：扫描 → submit 到 broker → 休息 → 重复。"""
        engine = self._get_engine()
        # github_search 使用全部 token 并行；外部源只用一个 token（占位兼容）
        all_tokens = self._get_all_tokens()
        token = all_tokens[0]
        source_round = 0
        consecutive_errors = 0
        consecutive_zeros = 0       # Task 4.1: 连续零产出计数器
        skip_until_round = 0  # 连续失败时跳过的轮次

        consecutive_zeros_global = 0  # track saturation across rounds
        while not self.state.should_exit:
            source_round += 1
            # 每轮都喂看门狗心跳——本轮可能走 resting/dormant/skipped 静默分支
            self.state.touch_activity()

            # 连续失败自动跳过：某源连续失败 N 轮 → 跳过 3 轮（降低负载）
            if source_round <= skip_until_round:
                self.state.set_source_status(source, "skipped", source_round)
                time.sleep(min(30, self.interval))
                continue

            # 外部源低频轮换：非 github_search 且未到轮次 → 休息一轮（省 60-90s 扫描）
            # --once 是显式单次验证，直接扫描；轮换降频只服务 24/7 模式。
            if (source != "github_search"
                    and not self.once
                    and source_round % self._EXTERNAL_SOURCE_EVERY_N_ROUNDS != 0):
                self.state.set_source_status(source, "resting", source_round)
                time.sleep(min(30, self.interval))
                continue

            # Task 4.1: 动态休眠 — 长期零产出源自动跳过，只偶尔探测。
            # 休眠轮也自增计数器(否则冻结在 51: 51%50≠0 永远休眠,
            # 100/150 的探测轮永远到不了 → 外部源永久退出工作)。
            if (source != "github_search"
                    and consecutive_zeros >= self._DORMANT_AFTER_ZERO_ROUNDS
                    and consecutive_zeros % self._DORMANT_PROBE_INTERVAL != 0):
                self.state.set_source_status(source, "dormant", source_round)
                consecutive_zeros += 1
                time.sleep(min(30, self.interval))
                continue

            submitted = 0
            scan_duration = 0

            try:
                # ── 1. 扫描 ──
                self.state.set_source_status(source, "scanning", source_round)
                self.state.add_log(f"[{source}] R{source_round} 扫描中...")
                t0 = time.time()

                if source == "github_search":
                    # Global saturation guard: after 6 consecutive 0-key rounds, skip 3 rounds。
                    # 跳过轮也自增计数器(否则冻结在 7: 7%3≠0 永远跳过,
                    # 10/13 的探测轮永远到不了 → 主数据源永久停摆)。
                    if consecutive_zeros_global >= 6 and consecutive_zeros_global % 3 != 0:
                        consecutive_zeros_global += 1
                        self.state.add_log(f"[github_search] 全局饱和(连续{consecutive_zeros_global}轮0key)，跳过", "info")
                        source_round += 2  # skip rounds
                        # 饱和休息期间也打心跳日志，避免看门狗误判卡死（sleep 300s 无日志）
                        for _ in range(max(1, self.interval // 60)):
                            if self.state.should_exit:
                                break
                            time.sleep(60)
                            self.state.add_log(f"[github_search] 饱和休息中（剩余 {self.interval - 60 * (_ + 1)}s）", "info")
                        continue
                    submitted = self._scan_github_fast(all_tokens)
                else:
                    submitted = self._scan_external(source, engine, token, source_round)

                scan_duration = time.time() - t0
                # 累计计数（TUI 卡片单调递增；status 里也存累计值）。
                # github_search 在 _run_bucket/_scan_github_serial 内已实时累加，勿重复。
                if source != "github_search":
                    self.state.add_source_submitted(source, submitted)
                    self.state.set_source_status(
                        source, "scanning", source_round,
                        keys=self.state.source_total_submitted(source))

                # 成功（或有产出）→ 重置失败计数
                if submitted > 0:
                    consecutive_errors = 0
                    consecutive_zeros = 0
                    consecutive_zeros_global = 0
                else:
                    consecutive_zeros += 1
                    consecutive_zeros_global += 1

                # 日志
                broker_snap = self.broker.snapshot()
                if submitted > 0:
                    self.state.add_log(
                        f"[{source}] R{source_round} 提交 {submitted}key "
                        f"(队列待验: {broker_snap['pending']}) ({scan_duration:.0f}s)")
                else:
                    level = "warning" if scan_duration < 3 else "info"
                    self.state.add_log(
                        f"[{source}] R{source_round} 0key ({scan_duration:.0f}s)", level)

                # 每次扫描后保存最新结果到 CSV
                self._save_from_broker()

            except Exception as e:
                self.state.add_log(f"[{source}] R{source_round} 异常: {e}", "error")
                consecutive_errors += 1
                # 连续 3 次错误 → 跳过 3 轮
                if consecutive_errors >= 3:
                    skip_until_round = source_round + 3
                    self.state.add_log(
                        f"[{source}] 连续 {consecutive_errors} 次错误，跳过 3 轮", "warning")

            if self.once or self.state.should_exit:
                self.state.set_source_status(source, "done", source_round)
                break

            # ── 2. 自适应休息 ──
            # github_search 自带 per-token pacing(≈9/min)，连跑可榨满 10/分钟预算不轮空
            # （原先 burst→rest 模式只用了约 50% 预算）。其它源保持 interval 休息避免被封。
            # 异常快返回(扫描<3s，如无查询/全失败)仍歇 60s 防 CPU 空转。
            if source == "github_search":
                rest = 0  # per-token pacing protects quota, no extra rest needed
            elif submitted > 0:
                rest = self.interval
            else:
                rest = max(60, self.interval // 5)
            if rest:
                self.state.set_source_status(source, "resting", source_round)
                for _ in range(rest):
                    if self.state.should_exit:
                        break
                    # 休息期间每 10 秒保存一次结果（验证 worker 可能在后台产出了新结果）
                    if _ % 10 == 0:
                        self._save_from_broker()
                    # 休息期间每 60s 打一次心跳:外部源 interval=600s 时,休息期
                    # 无日志会触发 300s 看门狗自杀重启(误判卡死)。
                    if _ % 60 == 0:
                        self.state.add_log(
                            f"[{source}] 休息中（{rest - _}s 后下轮）", "info")
                    time.sleep(1)
            # 休息结束/连续轮转前再保存一次
            self._save_from_broker()

    def _scan_github_fast(self, tokens: list[str]) -> int:
        """GitHub Code Search（text_matches 零下载）。
        使用 QueryRotator 获取本轮查询子集（时间切片 + 分桶轮转）。
        关键优化：把查询分桶给多个 token 并发执行 —— 每个 token 有独立的
        10次/分钟 配额，N 个 token 并发可获得近 N× 吞吐（单 token 串行
        的瓶颈在 scanner_engine._gh_search 的 per-token pacing）。
        返回提交的新 key 数。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed


        engine = self._get_engine()
        queries = self._rotator.next_round()
        if not queries:
            return 0

        # 收益优先：时间切片(freshness)优先 + 静态查询按历史命中降序；
        # 跳过长期零收益的静态查询(如裸 "deepseek sk-")，把 10次/分钟预算花在
        # 高产查询(filename:java≈38key/调用、filename:env≈9)上。
        tracker = engine._query_tracker
        tracker.tick_cooldowns()  # P1-2: decrement cooldown counters each round
        # 查询变异：从历史 TOP 收益查询派生变体，自动扩展高产查询空间
        # （封顶 self._max_mutants；变体命中→被记为高产长期保留，0 命中→is_barren 跳过）
        from query_rotation import generate_mutants
        mutants = generate_mutants(tracker.top_queries(8, min_runs=2),
                                   existing=set(queries), max_mutants=self._max_mutants)
        queries = queries + mutants
        # 扩展查询池：每轮注入一小批新查询（探索平台盲区）
        if self._gen_pool is not None:
            gp_batch = self._gen_pool.next_batch(size=24)  # 试水批 12 → 24
            if gp_batch:
                queries = queries + gp_batch
        # 收益优先：按历史新提交收益降序；跳过近 3 轮 0 产出的查询
        # （is_barren 新判据——修复"累计 hits>0 永不跳过"导致的停滞）
        static_qs = [
            q for q in queries
            if not tracker.is_barren(q)
            and not tracker.is_in_cooldown(q)
            and not tracker.is_low_quality(q)
        ]
        # 灭绝保护：当所有查询都被 barren/cooldown 过滤时（长期运行后 99%+），
        # 分批恢复查询——每次只恢复高收益的 top-N，避免瞬间打满配额触发次级限流。
        if not static_qs and queries:
            # 质量熔断不是“枯竭”：零转化查询不能被灭绝保护复活。
            recoverable = [q for q in queries if not tracker.is_low_quality(q)]
            if not recoverable:
                self.state.add_log(
                    "查询质量：当前批次全部低转化，跳过本轮等待新查询/新反馈",
                    "info")
                return 0
            # 按历史收益率排序,每次恢复 top-50(3 token × ~17 查询/token)——15 太慢,
            # 164 条查询全 barren 时多轮才能恢复完;50 一次恢复 1/3,配合 7.5s pacing
            # 每 token ~8 次/分,50 条 ≈ 2 分钟跑完,不会触发次级限流。
            _BARREN_BATCH_SIZE = 50
            sorted_qs = sorted(recoverable, key=tracker.get_quality_yield, reverse=True)
            batch = sorted_qs[:_BARREN_BATCH_SIZE]
            for q in batch:
                s = tracker._stats.get(q, {})
                s["recent"] = []
            self.state.add_log(
                f"查询枯竭：渐进恢复 top-{len(batch)}/{len(queries)} 条（余下分批恢复）",
                "warning")
            queries = batch  # 本轮只跑恢复的这批
        else:
            queries = static_qs
        # 多平台查询首轮保底：runs==0 的平台查询（从未执行过）排最前先跑一遍
        # 建立收益数据——否则纯 yield 排序下平台查询（未知=0.5）永远排 25+ 位，
        # 一轮 20-40 分钟跑不到就被下轮覆盖（"只在扫 deepseek"的根因）。
        # 跑过一次后（runs>0）与 deepseek 统一走 yield 收益机制：高产的保留、
        # 低产的被 is_barren 跳过——机制对多平台查询同样生效。
        unexplored_plat = [q for q in queries
                           if _is_platform_query(q) and tracker.get_runs(q) == 0]
        explored = [q for q in queries if q not in unexplored_plat]
        explored.sort(
            key=lambda q: (not tracker.is_declining(q),
                           tracker.get_quality_yield(q)),
            reverse=True,
        )
        queries = unexplored_plat + explored

        # 单 token（含匿名）→ 保持原串行逻辑，避免线程开销
        if len(tokens) <= 1:
            submitted = self._scan_github_serial(engine, queries, tokens[0] if tokens else "")
            # 单 token：fresh-repo 阶段（最近推送的项目，Repo Search 配额独立）
            try:
                fresh_keys = engine.scan_fresh_repos(tokens[0] if tokens else "")
                accepted = self._submit_fresh_keys(fresh_keys)
                submitted += accepted
            except Exception as e:
                self.state.add_log(f"fresh-repo 扫描异常: {e}", "warning")
            tracker.save()  # 持久化收益学习（跨重启）
            try:
                from trend_monitor import record_round_top_queries
                record_round_top_queries(tracker, f"R{self._rotator.round_num}")
            except Exception:
                pass
            self._rotator.save_state()  # 持久化轮次（重启续跑）
            return submitted

        # 多 token：把查询轮流分给各 token，每 token 一个线程。
        # v2.5.1: 主扫用全部 token(3/3 而非 2/3)——fresh-repo 在池完成**之后**
        # 串行执行(L1881),主扫期间预留 token 纯闲置。旧架构的隔离顾虑
        # ("烧光配额 fresh-repo 撞 sleep-to-reset")在 v2.5 均匀铺排下消失:
        # quota_wait 恒 ≈6s < skip_wait 阈值 10s,fresh-repo 照常拿到槽位。
        fresh_token = tokens[0]
        scan_tokens = list(tokens)
        num_tokens = len(scan_tokens)
        total_submitted = 0
        total_lock = threading.Lock()

        # 按查询索引 % num_tokens 分桶，保证各 token 负载均匀
        buckets: list[list[str]] = [[] for _ in range(num_tokens)]
        for i, q in enumerate(queries):
            buckets[i % num_tokens].append(q)

        def _run_bucket(token: str, bucket: list[str]) -> int:
            """单个 token 的查询桶：串行执行该桶所有查询（per-token pacing 在 engine 内部）。

            流式提交：on_key 回调里每提取到 key 立即 submit + 实时计数，
            不等整条查询结束才批量提交（原批处理 10 分钟才 500+ 条）。"""
            nonlocal total_submitted  # 否则 total_submitted += n 触发 UnboundLocalError
            local = 0

            def _on_key(key_info: dict) -> None:
                """扫到即提交（线程安全：broker.submit 自带锁）。"""
                nonlocal local, total_submitted
                k = key_info.get("key", "")
                if not k:
                    return
                if self.broker.submit(k, source="github_search",
                                      repos=key_info.get("repos", []), priority=0,
                                      query=key_info.get("query", "unknown")):
                    local += 1
                    self.state.add_source_submitted("github_search", 1)
                    with total_lock:
                        total_submitted += 1
                    self.state.set_source_status(
                        "github_search", "scanning", 1,
                        keys=self.state.source_total_submitted("github_search"))

            for query in bucket:
                if self.state.should_exit:
                    break
                try:
                    # 动态页数：高收益查询深挖,watch 封顶 3 页(300 条)——sort=indexed
                    # 第 1 页就是最新结果,深页全是旧 key 且烧配额,只给新查询留带宽
                    pages = min(3, max(self._github_pages,
                                       tracker.suggest_pages(query, default=self._github_pages)))
                    # per-query 流式提交计数:on_key 已把新 key 逐条 submit 进 _seen,
                    # 后续 submit_many 必然全被去重(n 恒 0)——收益统计必须用流式计数。
                    before = local
                    keys = engine._scan_one_query(query, max_pages=pages,
                                                  token=token, on_key=_on_key)
                    n = local - before
                    # 按**新提交数**记录(驱动 suggest_pages 页数分配)——
                    # 提取总数含重复历史 key,会让深挖页数虚高烧配额。
                    tracker.record(query, n)
                    # P0: 报告查询命中数,用于零命中清理
                    self._rotator.report_query_result(query, len(keys))
                    # P1-2: diminishing returns cooldown check
                    tracker.diminishing_rounds(query, n, len(keys))
                except Exception as e:
                    # v2.4.9: 静默吞掉会让整轮 0 产出只能靠看门狗兜底——至少记日志
                    self.state.add_log(f"查询 {query[:40]} 失败: {type(e).__name__}", "warning")
            return local

        # 并发执行所有 token 桶；worker 数 = token 数（受 per-token pacing 约束）。
        # v2.5: stagger 从 30s/token 降到 2s——防爆发已由两层机制原生承担:
        # 同 IP 首帧被 IP 层锁天然串行(6.0s 地板),多代理各走独立 IP 无所谓。
        # 旧 30s stagger 每轮白扣 60s(3 token),是纯开销。
        with ThreadPoolExecutor(max_workers=max(1, num_tokens)) as pool:
            futures = []
            for i in range(num_tokens):
                futures.append(pool.submit(_run_bucket, scan_tokens[i], buckets[i]))
                if i < num_tokens - 1:
                    import time as _t
                    _t.sleep(2)  # 微错峰即可,防爆发由 IP 层锁负责
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    pass

        tracker.save()  # 持久化收益学习（跨重启）
        try:
            from trend_monitor import record_round_top_queries
            record_round_top_queries(tracker, f"R{self._rotator.round_num}")
        except Exception:
            pass
        self._rotator.save_state()  # 持久化轮次（重启续跑）

        # ── Fresh Repo 阶段：扫"最近推送的项目"（Code Search 不支持日期过滤，
        #    但 Repo Search 支持 pushed:>DATE —— 新推送的 repo 用 repo: 限定符精扫）。
        #    fresh_token 固定为 tokens[0]（配额隔离，不抢主扫描预算）。
        try:
            fresh_keys = engine.scan_fresh_repos(fresh_token)
            accepted = self._submit_fresh_keys(fresh_keys)
            total_submitted += accepted
        except Exception as e:
            self.state.add_log(f"fresh-repo 扫描异常: {e}", "warning")
        return total_submitted

    def _submit_fresh_keys(self, fresh_keys: list) -> int:
        """提交 fresh-repo 发现的 key（含 repos 信息），返回新提交数。"""
        accepted = 0
        for k in fresh_keys:
            info = k if isinstance(k, dict) else {"key": k}
            if self.broker.submit(info.get("key", ""), source="github_search",
                                  repos=info.get("repos", []), priority=0,
                                  query="fresh-repo"):
                accepted += 1
        if accepted:
            self.state.add_source_submitted("github_search", accepted)
            self.state.set_source_status(
                "github_search", "scanning", 1,
                keys=self.state.source_total_submitted("github_search"))
            self.state.add_log(f"fresh-repo 提交 {accepted} 个新 key", "info")
        return accepted

    def _record_query_outcome(self, result: dict) -> None:
        """验证完成 -> 查询收益反馈：排序目标从候选量转为高价值产出。"""
        self._get_engine()._record_query_outcomes([result])

    def _scan_github_serial(self, engine, queries: list[str], token: str) -> int:
        """单 token / 匿名 串行扫描（保留原逻辑，避免线程开销）。

        流式提交：on_key 回调里每提取到 key 立即 submit + 实时计数。"""
        total_submitted = 0

        def _on_key(key_info: dict) -> None:
            nonlocal total_submitted
            k = key_info.get("key", "")
            if not k:
                return
            if self.broker.submit(k, source="github_search",
                                  repos=key_info.get("repos", []), priority=0,
                                  query=key_info.get("query", "unknown")):
                total_submitted += 1
                self.state.add_source_submitted("github_search", 1)
                self.state.set_source_status(
                    "github_search", "scanning", 1,
                    keys=self.state.source_total_submitted("github_search"))

        for query in queries:
            if self.state.should_exit:
                break
            try:
                # 动态页数：高收益查询深挖,watch 封顶 3 页(见上)
                pages = min(3, max(self._github_pages,
                                   engine._query_tracker.suggest_pages(
                                       query, default=self._github_pages)))
                # per-query 流式提交计数(与 _run_bucket 同因:on_key 已进 _seen,
                # submit_many 二次提交恒 0)
                before = total_submitted
                keys = engine._scan_one_query(query, max_pages=pages,
                                              token=token, on_key=_on_key)
                n = total_submitted - before
                # 按新提交数记录(见上)——页数分配对齐有效产出
                engine._query_tracker.record(query, n)
                # P0: 报告查询命中数,用于零命中清理
                self._rotator.report_query_result(query, len(keys))
                # P1-2: diminishing returns cooldown check
                engine._query_tracker.diminishing_rounds(query, n, len(keys))
            except Exception as e:
                # v2.4.9: 静默吞掉会让整轮 0 产出只能靠看门狗兜底——至少记日志
                self.state.add_log(f"查询 {query[:40]} 失败: {type(e).__name__}", "warning")
        return total_submitted

    def _scan_external(self, source: str, engine, token: str, source_round: int = 0) -> int:
        """外部源扫描（sub-thread + 超时），结果 submit 到 broker。
        返回提交的新 key 数。

        硬时限：HF/GitLab 的真正取消发生在 scanner 内部 asyncio.timeout（18/28s）；
        本方法的 join 只是"取消未生效"的最终保险（deadline + 7s）。"""
        # 超时对齐内部 deadline：HF 18s→25s，GitLab 28s→35s；其它源维持 60s
        if source == "gitlab":
            # gitlab 内部 deadline 有 token 时 40s / 无 token 28s;join 必须大于内部
            # deadline,否则内部取消未生效就被 join 截断(结果丢弃+僵尸线程)。
            timeout = 45
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
        # 外部源(npm/gitlab/hf...)的结果是多词条合并下载的,无法归因到具体词条;
        # 统一打 ext:<source> 标签——此前误用 terms[0](如 "deepseek")会把
        # 外部源的验证结果记到一条从未作为 GitHub 查询执行的伪查询上,
        # 污染查询收益学习和 metrics by_query。
        ext_label = f"ext:{source}"
        if terms:
            for info in keys.values():
                if isinstance(info, dict):
                    info.setdefault("query", ext_label)
        return self.broker.submit_many(keys, source=source,
                                       query=ext_label)

    def _save_from_broker(self, force: bool = False):
        """从 broker 拉取最新结果，写入 CSV + state + 更新 TUI。
        线程安全：_save_lock 防多源同写；节流(min间隔)防长跑时高频全量重写
        （崩溃最多丢 15s 的 JSON/CSV 视图；SQLite 全量留存，final_save 强制刷盘）。

        **保存 = 合并**：本轮 broker 结果 ∪ 内存历史缓存（启动时一次性 load_history）。
        若直接覆盖，启动初 broker 还是空的（历史 key 仅回显、未重验），首轮保存
        会把上次会话的 key 清光（"重启后东西还在"失效）。
        新验证结果覆盖同名历史（余额/时间更新）；纯历史 key 原样保留。

        **重启重验剔除**：重验判不合格（失效/欠费/低于阈值）的 key 从 state/CSV 移除。

        缓存化后不再每 15s 反复读盘(load_history);历史 map 在内存里增量更新,
        随账本线性增长的 I/O(原每次:1.5MB JSON + 全表 SELECT + CSV 解析)消除。
        broker 无 state/CSV 可见变化时直接返回,空闲源轮询不再产生磁盘写放大。
        """
        broker_revision = self.broker.state_revision()
        if not force and broker_revision == self._saved_broker_revision:
            return
        now = time.time()
        if not force and now - self._last_save < self._save_min_interval:
            return
        self._last_save = now
        all_valid = self.broker.get_all_results()
        evicted = self.broker.evicted_keys()
        with self._save_lock:
            hist = self._history_map
            # 新验证结果覆盖历史 + 更新 provider 提示
            for r in all_valid:
                k = r.get("key")
                if k:
                    hist[k] = r
                    p = (r.get("provider") or "").strip()
                    if p and p != "unknown":
                        self._provider_hints[k] = p
            if evicted:
                for k in evicted:
                    hist.pop(k, None)
            # 平台字段回填:本轮无验证结果的旧行从 provider_hints 补
            for r in hist.values():
                if r.get("valid") and not (r.get("provider") or "").strip():
                    p = self._provider_hints.get(r.get("key"), "")
                    if p:
                        r["provider"] = p
            # 欠费 key（balance_cny < 0）→ 从 CSV 累计行中移除；重验剔除的也一并移除
            arrears = {r.get("key") for r in hist.values() if (r.get("balance_cny") or 0) < 0}
            arrears |= evicted
            for k in arrears:
                hist.pop(k, None)
            # 剔除后再构建快照：dict.pop 不会移除旧 merged 列表里的对象引用。
            merged = list(hist.values())
            merged_hv = filter_high_value(merged, self.min_balance)
            # CSV 保存全部有效 key(不只是高价值)——TUI 高价值表只显示 >min_balance 的,
            # 但本地 CSV 账本要完整记录所有有效 key(含余额 0 但认证通过的)。
            merged_valid = [r for r in merged if r.get("valid")]
            write_watch_csv(self.csv_path, merged_valid, arrears_keys=arrears)
            save_watch_state(self.state_path, merged)
            self._saved_broker_revision = broker_revision
        self.state.set_high_value_keys(merged_hv)

    def final_save(self):
        """退出前的最终保存（强制刷盘，绕过节流）。"""
        self._save_from_broker(force=True)

    def engine_stop(self):
        """停止引擎持有的资源(内嵌 mihomo 子进程)。

        v2.4.9: 此前 watch 退出从不调 engine.stop(),mihomo.exe 每次退出
        都成孤儿进程,下次启动端口被占 → 多代理静默回退单代理。
        引擎可能尚未创建(首次 _get_engine 前),None 时无需清理。
        """
        with self._engine_lock:
            engine = self._engine
        if engine is not None:
            engine.stop()


# ══════════════════════════════════════════════════════════════════
#  TUI 渲染 — 安全仪表盘风格（rich）
# ══════════════════════════════════════════════════════════════════

_SPINNER_FRAMES = "⣾⣽⣻⢿⡿⣟⣯⣷"

_LOG_ICONS = {"error": "❌", "warning": "⚠️", "success": "✅", "info": "•"}


def _format_elapsed(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_countdown(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"


def _spinner_char(now: float) -> str:
    """返回当前时间的 spinner 帧字符（8fps 旋转）。"""
    idx = int(now * 8) % len(_SPINNER_FRAMES)
    return _SPINNER_FRAMES[idx]


def _balance_style(cny: float) -> str:
    """余额颜色分级：≥100 亮绿，≥10 绿，≥1 黄。"""
    if cny >= 100:
        return "bold bright_green"
    if cny >= 10:
        return "green"
    return "yellow"


# ── 各分区渲染 ──────────────────────────────────────────────────────

def _render_header(snap: dict, now: float):
    """顶部标题栏：标题 + 活跃源数 + 运行时长 + 平台分布。"""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    active = snap.get("active_count", 0)
    total = snap.get("source_count", 0)

    line1 = Text.assemble(
        ("🌲 DARKFOREST WATCH", "bold green"),
        ("    ", ""),
        (f"{_spinner_char(now)} " if active > 0 else "● ", "yellow" if active > 0 else "green"),
        (f"{active}/{total} 数据源活跃", "yellow" if active > 0 else "dim"),
    )

    line2 = Text.assemble(
        (f"  已运行 {_format_elapsed(snap['elapsed'])}", "dim"),
        ("  ·  ", "dim"),
        (f"{snap['stats']['high_value']} 高价值 key", "bold yellow"),
        ("  ·  ", "dim"),
        ("Ctrl+C 退出", "dim"),
    )

    # 平台分布：已验证 key 按平台计数，取 Top 5（多平台扫描战报）
    pcounts = snap.get("verify", {}).get("provider_counts", {})
    if pcounts:
        top = sorted(pcounts.items(), key=lambda kv: -kv[1])[:5]
        parts = [(f"{_provider_display_name(pid)} {n}", "cyan") for pid, n in top]
        line3 = Text.assemble(("  平台: ", "dim"), *parts, ("  ", ""))
        return Panel(Group(line1, line2, line3), border_style="green", padding=(0, 1))

    return Panel(Group(line1, line2), border_style="green", padding=(0, 1))


def _render_stats(snap: dict, now: float):
    """统计卡片区：5 个数字卡片并排（含验证队列）。"""
    from rich.console import Group
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.text import Text

    stats = snap["stats"]
    verify = snap.get("verify", {})
    flash = snap.get("flash_until", {})

    def card(value, label, color, flash_key):
        is_flash = now < flash.get(flash_key, 0)
        num_style = "bold bright_white" if is_flash else f"bold {color}"
        content = Group(
            Text(str(value), style=num_style, justify="center"),
            Text(label, style=f"dim {color}", justify="center"),
        )
        return Panel(content, border_style=color, padding=(0, 1), title_align="center")

    layout = Layout()
    layout.split_row(
        Layout(card(stats["submitted"], "提交(含复检)", "cyan", "submitted")),
        Layout(card(verify.get("genuinely_new", 0), "★新发现", "magenta", "genuinely_new")),
        Layout(card(verify.get("pending", 0), "待验", "blue", "pending")),
        Layout(card(verify.get("valid", 0), "有效", "green", "valid")),
        Layout(card(stats["high_value"], "💰 高价值", "yellow", "high_value")),
        Layout(card(f"¥{stats['total_cny']:.2f}", "总值", "green", "total_cny")),
    )
    return layout


def _render_verify_bar(snap: dict):
    """验证队列状态条：进度 + 状态分布 + 速率 + 预估时间。"""
    from rich.panel import Panel
    from rich.text import Text

    verify = snap.get("verify", {})
    submitted = verify.get("submitted", 0)
    pending = verify.get("pending", 0)
    verified = verify.get("verified", 0)
    valid = verify.get("valid", 0)
    rate = verify.get("rate_per_min", 0)
    sc = verify.get("status_counts", {})

    # 进度条
    total_for_progress = max(submitted, verified)
    pct = (verified / total_for_progress * 100) if total_for_progress > 0 else 0
    bar_filled = int(pct / 5)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)

    # 预估剩余时间
    eta_str = ""
    if rate > 0 and pending > 0:
        eta_min = pending / (rate / 60)
        eta_str = f"ETA {eta_min:.0f}m"

    # 状态分布：有余额 / 零余额 / 无余额接口（有余额接口但查到 0 的归零余额）
    n_active = sc.get("valid_active", 0)
    n_zero = sc.get("valid_zero", 0)
    n_nobal = sc.get("valid_no_balance", 0)
    status_str = ""
    if verified > 0:
        parts = []
        if n_active:
            parts.append((f"有钱:{n_active} ", "green"))
        if n_zero:
            parts.append((f"零余额:{n_zero} ", "yellow"))
        if n_nobal:
            parts.append((f"无余额接口:{n_nobal} ", "dim"))
        status_str = Text.assemble(*parts)

    text = Text.assemble(
        ("验证队列 ", "dim"),
        (f"[{bar}]", "cyan"),
        (f" {pct:.0f}%  ", "dim"),
        (f"✓{verified}/{submitted}", "green"),
        ("  ", ""),
        (f"待验:{pending} ", "blue"),
        (f"有效:{valid} ", "green"),
        ("  ", ""),
        status_str,
        (f"速率:{rate}/min ", "yellow"),
        (eta_str, "dim"),
    )
    return Panel(text, border_style="blue", padding=(0, 1))


def _render_sources(snap: dict, now: float):
    """数据源状态面板：每个源一行紧凑显示当前状态。"""
    from rich.panel import Panel
    from rich.text import Text

    source_status = snap.get("source_status", {})
    if not source_status:
        return Panel(Text("  (无数据源)", style="dim"), border_style="cyan", padding=(0, 1))

    text = Text()
    for i, (source, status) in enumerate(source_status.items()):
        if i > 0:
            text.append(" │ ", style="dim")
        phase = status.get("phase", "idle")
        rnd = status.get("round", 0)
        keys = status.get("keys", 0)

        if phase == "scanning":
            icon = _spinner_char(now)
            style = "yellow"
            label = "扫"
        elif phase == "resting":
            icon = "😴"
            style = "dim"
            label = ""
        elif phase == "skipped":
            icon = "⏭"
            style = "dim"
            label = ""
        elif phase == "done":
            icon = "✓"
            style = "green"
            label = ""
        else:
            icon = "●"
            style = "dim"
            label = ""

        k_str = f" {keys}k" if keys > 0 and phase not in ("resting", "done", "idle", "skipped") else ""
        text.append(f" {icon} {source}", style=style)
        if label:
            text.append(f" {label}", style=style)
        if rnd > 0:
            text.append(f"R{rnd}", style="dim")
        if k_str:
            text.append(k_str, style="cyan")
        if phase == "skipped":
            text.append(" ⏭", style="dim")

    return Panel(text, title="📡 数据源", border_style="cyan", padding=(0, 1))


def _provider_display_name(provider_id: str) -> str:
    """平台 id → 中文展示名（providers.py 的 name_cn，未知回退 id）。"""
    try:
        from providers import PROVIDER_MAP
        p = PROVIDER_MAP.get(provider_id)
        if p is not None:
            return p.name_cn
    except Exception:
        pass
    return provider_id


def _render_hv_table(snap: dict, now: float):
    """高价值 key 表格：完整 key + 平台 + 文件链接 + 余额色阶 + 新 key 高亮。

    列：
    - Key：完整显示，仅当窗口不够宽时才截断（Rich 自动处理）
    - 平台：key 所属 AI 平台（多平台识别结果）
    - 余额(¥)：右对齐
    - 来源/文件：repo + 文件名 + url（可点击链接）
    - 验证时间
    """
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    table = Table(expand=True, show_header=True, header_style="bold", pad_edge=False)
    table.add_column("Key", ratio=3, overflow="ellipsis", justify="center")
    table.add_column("平台", ratio=1, justify="center")
    table.add_column("余额(¥)", ratio=1, justify="center")
    table.add_column("来源 / 文件", ratio=3, overflow="ellipsis", justify="center")
    table.add_column("验证时间", ratio=1, justify="center", style="dim")

    new_flash = snap.get("new_key_flash", {})
    hv_keys = snap["high_value_keys"]

    if hv_keys:
        for r in hv_keys[:20]:
            key = r.get("key", "")
            cny = r.get("balance_cny", 0)
            is_new = now < new_flash.get(key, 0)

            # 来源/文件：取第一个 repo 的 repo名 + 文件名 + url（可点击链接）
            repos = r.get("repos", [])
            if repos:
                repo_info = repos[0]
                repo_name = repo_info.get("repo", r.get("source", "?"))
                file_name = repo_info.get("file", "")
                url = repo_info.get("url", "")
                # 来源显示：repo名/文件名，URL 直接显示（终端自动识别可点击）
                if file_name:
                    src_text = Text.assemble(
                        (f"{repo_name}", "cyan"),
                        ("/", "dim"),
                        (f"{file_name}", "dim"),
                    )
                else:
                    src_text = Text(repo_name, style="cyan")
                if url:
                    src_text.append("  ", "")
                    src_text.append(f"🔗 {url}", "blue")
            else:
                src_text = Text(r.get("source", "?"), style="dim")
                url = ""

            key_style = "bold bright_white" if is_new else "cyan"
            provider = _provider_display_name(r.get("provider", ""))
            provider_style = "magenta" if provider != r.get("provider", "") else "dim"
            table.add_row(
                Text(key, style=key_style),
                Text(provider, style=provider_style),
                Text(f"{cny:.2f}", style=_balance_style(cny)),
                src_text,
                (r.get("verified_at", "?") or "?")[-8:],  # "2026-08-08 07:16:47" → "07:16:47"
            )
    else:
        table.add_row(Text("(暂无高价值 Key)", style="dim"), "", "", "", "")

    title = Text.assemble(("💰 高价值 Key", "bold yellow"), ("  ", ""), ("(余额 > ¥1)", "dim"))
    return Panel(table, title=title, border_style="yellow", padding=(0, 0))


def _render_logs(snap: dict):
    """日志面板：最新在上，按级别着色。"""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    lines = []
    for log in reversed(snap["logs"]):  # 最新在上
        ts = log.get("timestamp", "")
        msg = log.get("message", "")
        level = log.get("level", "info")
        style = {"error": "red", "warning": "yellow", "success": "green"}.get(level, "white")
        icon = _LOG_ICONS.get(level, "•")
        lines.append(Text(f"  {ts} {icon} {msg}", style=style))

    if not lines:
        lines.append(Text("  (无日志)", style="dim"))

    return Panel(Group(*lines), title="📋 日志", border_style="blue", padding=(0, 0))


# ── 主渲染入口 ──────────────────────────────────────────────────────

def render_watch_panel(state: WatchState):
    """渲染完整的 TUI 仪表盘面板。6 层分区布局，2fps 刷新。"""
    from rich.layout import Layout

    now = time.time()
    snap = state.snapshot()

    layout = Layout()
    layout.split_column(
        Layout(_render_header(snap, now), name="header", size=3),
        Layout(_render_stats(snap, now), name="stats", size=5),
        Layout(_render_verify_bar(snap), name="verify_bar", size=3),
        Layout(_render_sources(snap, now), name="sources", size=3),
        Layout(_render_hv_table(snap, now), name="table", ratio=1),
        Layout(_render_logs(snap), name="logs", size=9),
    )
    return layout


# ══════════════════════════════════════════════════════════════════
#  run_watch — 双线程入口
# ══════════════════════════════════════════════════════════════════

def run_watch(
    proxy: str | None = None,
    proxy_subscription: str | None = None,
    concurrency: int = 15,
    interval: int = 300,
    min_balance: float = 1.0,
    sources: list[str] | None = None,
    include_github: bool = False,
    once: bool = False,
    verify_interval: float = 0.5,
    verify_workers: int = 4,
    github_pages: int = 1,
    fresh: bool = False,
    no_tui: bool = False,
    reverify_budget: int | None = None,
    hv_email_threshold: float | None = None,
    hv_top_threshold: float | None = None,
    shrink_warn_pct: float | None = None,
    commits_since_hours: int | None = None,
    reverify_zero_hours: int | None = None,
    allow_chat_probe: bool = False,
    probe_unclear: bool = True,
    shutdown_timeout: float = 30.0,
):
    """Watch 模式入口：启动后台扫描线程 + 多验证 worker + 主线程 TUI 面板。

    架构：
    - 扫描线程（每个源一个）：只管扫，发现 key 立即 submit 到 VerificationBroker
    - 验证 worker（多线程并行）：从 broker 队列取 key，限速调 DeepSeek API
    - 主线程：TUI 面板 2fps 刷新
    Ctrl+C → 设置 should_exit → 扫描线程退出 → broker 排空 → 最终保存。
    """
    from rich.console import Console
    from rich.live import Live

    console = Console()

    # 创建共享 engine（扫描端和验证端共用）
    from scanner_engine import ScannerEngine
    engine = ScannerEngine(
        concurrency=concurrency,
        timeout=15,
        scan_pages=3,
        max_duration=0,
        max_valid_keys=0,
        output_dir="./results",
        proxy=proxy,
        proxy_subscription=proxy_subscription,
    )

    # 创建验证队列 broker（扫描端 submit，多 worker 并行消费）
    # 邮件通知：高价值 key 发现时告警
    email_notifier = None
    try:
        from config_loader import config as _cfg
        if _cfg.email_alert_enabled:
            from email_notifier import EmailNotifier
            email_dedup_h = float(_cfg.smtp_config.get("dedup_hours") or 1.0)
            email_notifier = EmailNotifier(_cfg.smtp_config,
                                            dedup_window_seconds=int(email_dedup_h * 3600))
            if email_notifier.enabled:
                console.print(f"[green]📧 邮件通知已启用 → {', '.join(_cfg.smtp_config['recipients'])}[/]")
    except Exception:
        pass

    # 重验/变化检测阈值：CLI 参数优先，回退 config.ini（broker 与 ReverifyScheduler 共用）
    try:
        from config_loader import config as _cfg
        rv_budget = reverify_budget if reverify_budget is not None else _cfg.watch_reverify_budget
        rv_email = hv_email_threshold if hv_email_threshold is not None else _cfg.watch_hv_email_threshold
        rv_top = hv_top_threshold if hv_top_threshold is not None else _cfg.watch_hv_top_threshold
        rv_shrink = shrink_warn_pct if shrink_warn_pct is not None else _cfg.watch_shrink_warn_pct
        rv_zero = reverify_zero_hours if reverify_zero_hours is not None else _cfg.watch_reverify_zero_interval_hours
    except Exception:
        rv_budget, rv_email, rv_top, rv_shrink, rv_zero = 1500, 5.0, 10.0, 30.0, 168

    broker = VerificationBroker(engine=engine, min_balance=min_balance,
                                interval=verify_interval, workers=verify_workers,
                                db_path=os.path.join("./results", "darkforest.db"),
                                email_notifier=email_notifier,
                                hv_email_threshold=rv_email, hv_top_threshold=rv_top,
                                shrink_warn_pct=rv_shrink,
                                allow_chat_probe=allow_chat_probe,
                                probe_unclear=probe_unclear)
    # one-shot 是明确要拿到验证结论的场景；交互 Ctrl+C 保持短等待。
    effective_shutdown_timeout = (max(120.0, shutdown_timeout)
                                  if once else max(1.0, shutdown_timeout))

    # 创建共享状态（持有 broker 引用，TUI 可读取验证统计）
    state = WatchState(broker=broker)

    scanner = WatchScanner(
        state=state,
        broker=broker,
        proxy=proxy,
        proxy_subscription=proxy_subscription,
        concurrency=concurrency,
        interval=interval,
        min_balance=min_balance,
        sources=sources,
        include_github=include_github,
        once=once,
        github_pages=github_pages,
        fresh=fresh,
        commits_since_hours=commits_since_hours,
        shared_engine=engine,  # v2.4.9: 复用共享引擎,杜绝双 mihomo
    )
    # v2.4.9: 共享引擎创建时 state 还不存在(无 log_callback,日志进 print)。
    # state 就绪后补挂回调——否则扫描日志(mihomo/限流/key 提取)全部丢失。
    engine.log_callback = lambda msg, level="info": state.add_log(msg, level)
    broker.on_verified = scanner._record_query_outcome

    # 创建本次会话日志文件
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join("./results", f"watch_session_{session_ts}.log")
    state.set_log_file(log_path)
    from scanner_engine import ScannerEngine
    n_tokens = len(ScannerEngine.get_all_gh_tokens())
    state.add_log(f"Watch 启动 | 代理: {proxy or '无'} | 数据源: {len(scanner.sources)} | "
                  f"阈值: ¥{min_balance} | 验证间隔: {verify_interval}s | GitHub Token: {n_tokens}个")
    # 看门狗重启标记（上次运行疑似卡死被自动重启）
    if os.environ.pop("DARKFOREST_WATCHDOG_RESTART", None):
        state.add_log("⚠️ 上次运行被看门狗判定卡死并自动重启（已续跑轮次与历史）", "warning")
    if fresh:
        state.add_log("--fresh: 查询轮次从头开始（不清除已捕获的 key 历史）", "info")
    # GitHub Code Search 硬上限 10次/分钟/token；token 数直接决定 github_search 吞吐。
    if n_tokens < 2:
        state.add_log(
            f"⚠️ 仅 {n_tokens} 个 GitHub Token → Code Search 限速 10次/分钟，github_search 会偏慢。"
            f"在 config.ini [github].token 填入多个 token(逗号分隔)可获 ~N× 提速", "warning")

    # 历史回填：三路合并（JSON ∪ db ∪ CSV）加载已验 key →
    #   1. 播种 broker._seen：重启后不重复验证历史 key
    #   2. 高价值 key 直接回显到 TUI 表格（重启不空白）
    #   与保存合并共用 load_history()：回显和保存看到同一份历史，首轮保存不冲掉。
    history = load_history("./results")
    if history:
        state.seed_from_history(history, broker=broker, min_balance=min_balance)
        n_hv = sum(1 for r in history if r.get("valid")
                   and r.get("balance_cny", 0) > min_balance)
        state.add_log(
            f"加载历史: {len(history)} 个已验 key，{n_hv} 个高价值 key 回显并跳过重验"
            f"（其余历史 key 重新走验证）", "info")

    # 重启重验：对历史高价值 key 重新验证余额（低优先级队列，不打断新 key）。
    # 仍符合条件的保留；失效/欠费/低于阈值的会被 _store_result 判定并剔除。
    hv_history = [r for r in history if r.get("valid")
                  and r.get("balance_cny", 0) > min_balance and r.get("key")]
    n_reverify = 0
    for r in hv_history:
        if broker.reverify(r["key"], source=r.get("source", "history"),
                           repos=r.get("repos", [])):
            n_reverify += 1
    if n_reverify:
        state.add_log(
            f"重启重验: {n_reverify} 个历史高价值 key 重新验证余额（不合格自动剔除）", "info")

    # 启动验证 worker（消费者）
    broker.start()

    # v2.5.3: error/rate_limited 存量复验——ReverifyScheduler 只选 valid=1,
    # 瞬态失败行(status='error'/'rate_limited' 且 valid=0)永远无人重验,形成
    # 死存量(DB 实证: claude 10 条真格式 key 全卡 error、openai 99、
    # gemini 7 条 rate_limited 可能藏 valid)。启动时一次性入持续重验队列:
    # enqueue_reverify 绕过单次限制、in-flight 去重、结果经 upsert 正常回写
    # (error→invalid 停掉 / error→valid 挖回来 / error 重网恢→再入列)。
    if broker._store_conn is not None:
        try:
            with broker._store_lock:
                rows = broker._store_conn.execute(
                    "SELECT key FROM keys WHERE status IN ('error', 'rate_limited') "
                    "AND key NOT LIKE '[invalid%' LIMIT 2000").fetchall()
            n_stale = 0
            for row in rows:
                if broker.enqueue_reverify(row["key"], source="stale_retry"):
                    n_stale += 1
            if n_stale:
                state.add_log(
                    f"error/rate_limited 存量复验: {n_stale} 条已入持续重验队列"
                    f"（候选 {len(rows)} 条）", "info")
        except Exception as e:
            state.add_log(f"存量复验入队失败: {e}", "warning")

    # 启动重验汇总：历史高价值 key 重验完成后，合并成**一封**邮件（防启动逐 key 刷屏）。
    # 用后台线程等重验批次排空（含超时保护），不阻塞扫描线程。
    if email_notifier is not None and email_notifier.enabled:
        def _summary_worker():
            import time as _t
            deadline = _t.time() + 600  # 最多等 10 分钟
            while _t.time() < deadline:
                if broker.all_reverify_done():
                    break
                _t.sleep(2)
            n = broker.send_reverify_summary()
            if n:
                state.add_log(f"启动重验汇总邮件已发送: {n} 个高价值 key", "info")
        _t_summary = threading.Thread(target=_summary_worker, daemon=True)
        _t_summary.start()

    # 持续重验调度器:按预算轮询历史有效 key,感知余额变化(缩水/充值/重新激活)
    # 阈值复用上面 broker 的 rv_* (CLI 优先,回退 config)
    try:
        rv_scheduler = ReverifyScheduler(
            broker, broker._store_conn,
            budget_per_day=rv_budget, email_threshold=rv_email,
            top_threshold=rv_top, shrink_pct=rv_shrink,
            zero_interval_hours=rv_zero,
            store_lock=broker._store_lock,
        )
        rv_scheduler.start()
        state.add_log(
            f"持续重验调度器已启动: 预算 {rv_budget}/天, 高价值≥¥{rv_email}, "
            f"重高价值≥¥{rv_top}, 缩水预警 {rv_shrink}%", "info")
    except Exception as e:
        state.add_log(f"持续重验调度器启动失败: {e}", "warning")

    # 启动各数据源扫描线程（生产者）
    scanner.run()

    # 主线程：TUI 面板（或 headless 模式：只等扫描完成）
    try:
        if no_tui:
            # Headless mode: wait for scan threads, log to file
            while (any(t.is_alive() for t in scanner._worker_threads)
                   or broker.snapshot()["pending"] > 0
                   or not broker.idle()):
                time.sleep(1)
                # headless 也周期落盘（_save_from_broker 自带 15s 节流+revision
                # 去重,空闲时零写放大）——否则硬崩时丢掉整个会话的 CSV/state 视图
                try:
                    scanner._save_from_broker()
                except Exception:
                    pass  # 文件被占用(Excel/杀软)等 transient 失败,下轮重试
        else:
            from rich.console import Console
            from rich.live import Live
            console = Console()
            with Live(
                render_watch_panel(state),
                refresh_per_second=2,
                console=console,
                screen=True,
            ) as live:
                while (any(t.is_alive() for t in scanner._worker_threads)
                       or broker.snapshot()["pending"] > 0
                       or not broker.idle()):
                    # Periodically flush broker results to TUI.
                    # Reverify finishes in 2-3s; no need to wait for scan round end.
                    # _save_from_broker min_interval throttles to avoid frequent writes.
                    # 保存失败(如 CSV 被 Excel 独占 → os.replace PermissionError)
                    # 只告警不退出——这里是 24/7 进程的主心跳循环,裸抛会让整个
                    # 进程在 finally 的 final_save 里二次抛异常带 traceback 死亡,
                    # 且看门狗只救"活着但卡死"的进程,救不了进程退出。
                    try:
                        scanner._save_from_broker()
                    except Exception as e:
                        state.add_log(f"周期保存失败(继续运行): {e}", "warning")
                    live.update(render_watch_panel(state))
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        state.should_exit = True
        try:
            state.add_log("收到退出信号，等待扫描线程退出...")
            for t in scanner._worker_threads:
                t.join(timeout=3)  # 扫描线程已见 should_exit,3s 足够返回
            # 退出加速:先置 broker._stop 让 worker 停止取新任务(退避 sleep 可中断),
            # 只等"在途验证"完成(短等待),不排空整个队列。
            broker._stop = True
            state.add_log("等待在途验证完成...")
            deadline = time.time() + effective_shutdown_timeout
            while time.time() < deadline:
                # ignore_stop: 绕过 _stop 短路,真正检查在途验证是否清零
                # (此前 idle() 在 _stop 后恒 True,排空等待是 no-op,刚扫到的 key 丢失)
                if broker.idle(ignore_stop=True):
                    break
                time.sleep(0.2)
            # 先停重验调度器再停 broker:调度线程可能正在 _tick 中查库,
            # 反向顺序会打到已关闭连接(靠 except 兜底不崩但有竞态窗口)。
            try:
                rv_scheduler.stop()
            except Exception:
                pass
            broker.stop()
            try:
                scanner.final_save()
            except Exception as e:
                # 文件仍被占用等情况:SQLite 已有全量,下次启动 load_history 兜底;
                # 不能让退出路径再炸一次(主循环同类异常的教训)。
                state.add_log(f"最终保存失败(SQLite 已留存,下次启动恢复): {e}", "error")
            # v2.4.9: 停内嵌 mihomo 子进程——此前从不调用,mihomo.exe 每次退出
            # 都成孤儿进程继续持有 17890+ 端口,下次启动端口被占 → "mihomo
            # 启动失败回退单代理"(per-IP 能力静默丢失)。
            try:
                scanner.engine_stop()
            except Exception:
                pass
        except KeyboardInterrupt:
            pass  # 第二次 Ctrl+C → 强制退出

    # ════════════════════════════════════════════════════════════════
    #  退出总结报告
    # ════════════════════════════════════════════════════════════════
    state.close_log_file()
    snap = state.snapshot()
    elapsed = snap["elapsed"]
    stats = snap["stats"]
    source_status = snap.get("source_status", {})

    from rich.rule import Rule
    from rich.table import Table

    console.print()
    console.print(Rule("🌲 DarkForest Watch — 运行总结", style="bold green"))

    # 概览
    console.print()
    console.print(f"  ⏱  运行时长: [bold cyan]{_format_elapsed(elapsed)}[/]")
    console.print(f"  📡 数据源:   [bold]{snap['source_count']}[/] 个")
    console.print(f"  🔑 提交:     [bold cyan]{stats['submitted']}[/] 个 key 到验证队列")
    verify = snap.get("verify", {})
    console.print(f"  ✅ 已验证:   [bold blue]{verify.get('verified', 0)}[/] 个  "
                  f"([bold green]有效 {verify.get('valid', 0)}[/])")
    console.print(f"  💰 高价值:   [bold yellow]{stats['high_value']}[/] 个  ([bold green]¥{stats['total_cny']:.2f}[/])")
    console.print(f"  ⚡ 验证速率: [bold]{verify.get('rate_per_min', 0)}[/] 个/分钟")

    # 各源统计表
    if source_status:
        console.print()
        table = Table(title="各数据源统计", show_header=True, header_style="bold", expand=True)
        table.add_column("数据源", style="cyan")
        table.add_column("轮次", justify="right")
        table.add_column("提交", justify="right", style="cyan")
        table.add_column("状态")

        for source in scanner.sources:
            s = source_status.get(source, {})
            phase_icon = {"scanning": "⣾ 扫描", "verifying": "⣾ 验证", "saving": "⣾ 保存",
                          "resting": "😴 休息", "done": "✓ 完成", "idle": "● 待命"}.get(
                s.get("phase", ""), s.get("phase", ""))
            table.add_row(
                source,
                str(s.get("round", 0)),
                str(s.get("keys", 0)),
                phase_icon,
            )
        console.print(table)

    # 高价值 key 列表
    hv_keys = snap["high_value_keys"]
    if hv_keys:
        console.print()
        console.print("[bold yellow]💰 高价值 Key 列表[/]")
        for i, r in enumerate(hv_keys[:10]):
            cny = r.get("balance_cny", 0)
            preview = r.get("key_preview", r.get("key", "")[:10] + "...")
            src = r.get("source", "?")
            console.print(f"  {i+1}. [cyan]{preview}[/]  [green]¥{cny:.2f}[/]  [dim]{src}[/]")
        if len(hv_keys) > 10:
            console.print(f"  [dim]...还有 {len(hv_keys) - 10} 个，见 CSV 文件[/]")

    # 文件位置
    console.print()
    console.print(Rule("📁 文件位置", style="blue"))
    console.print(f"  高价值 Key (CSV):  [bold cyan]{scanner.csv_path}[/]")
    console.print(f"  状态文件 (JSON):   [dim]{scanner.state_path}[/]")
    console.print(f"  本次日志:          [dim]{log_path}[/]")
    console.print()
