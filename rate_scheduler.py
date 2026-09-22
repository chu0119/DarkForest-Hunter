"""GitHub 原生配额调度器 — 以 X-RateLimit 响应头为唯一真相。

设计（native，非补丁）：
- 每个 api.github.com 响应携带 Limit/Remaining/Reset——配额状态由 GitHub
  主动告知，不需要猜固定速率。
- 调度：下次请求时间 = 把 remaining 次请求均匀铺到 reset 时刻
  （wait = (reset - now) / remaining）。窗口内永不提前耗尽 →
  永不 sleep-to-reset → 永不主配额 429 → 调用方没有周期性卡顿。
- 分桶：(token, endpoint_class)——按路径推断桶名（observe 与 wait_before
  必须用同一套推断，否则查不到状态），配额**数值**全部来自响应头。
  实测（2026-09-22，带 token）：
    /search/code        → X-RateLimit-Resource: code_search, Limit=10/min
    /search/repositories → X-RateLimit-Resource: search,     Limit=30/min
  两个独立配额桶；GitHub 若调整限额/分类，响应头自动跟随，无需改代码。
- 与 IP 滥用层正交：这里只管主配额（hard limit）；429/次级限流
  （Retry-After）由上层惩罚箱防御，但可 push 进来对齐下次请求。
"""
from __future__ import annotations

import threading
import time

_ENDPOINT_CLASSES = (
    ("/search/code", "code_search"),
    ("/search/repositories", "repo_search"),
    ("/search/", "search_other"),
)

# ≥40s 的窗口尾等待才告警（正常均匀调度永远产生不了这种等待；
# 出现即意味着配额被其他实例占用或窗口尾部状态异常）
LONG_WAIT_WARN = 40.0


def _endpoint_class(path: str) -> str:
    for prefix, cls in _ENDPOINT_CLASSES:
        if path.startswith(prefix):
            return cls
    return "core"


class RateScheduler:
    """Header-driven 固定窗口配额调度器（线程安全）。"""

    def __init__(self, log=None):
        self._lock = threading.Lock()
        # (token, cls) -> {"remaining", "reset", "pushed", "warned_reset", "limit"}
        self._state: dict[tuple[str, str], dict] = {}
        self._log = log or (lambda msg, level="info": None)

    @staticmethod
    def _key(token: str | None, path: str) -> tuple[str, str]:
        return (token or "", _endpoint_class(path))

    @staticmethod
    def _int_header(headers, name: str) -> int | None:
        try:
            raw = headers.get(name)
            if raw is None:
                return None
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    def observe(self, token: str | None, path: str, headers, now: float | None = None):
        """从响应头更新配额状态。缺头/坏头不更新（防 401/代理丢头污染）。"""
        now = time.time() if now is None else now
        remaining = self._int_header(headers, "X-RateLimit-Remaining")
        reset = self._int_header(headers, "X-RateLimit-Reset")
        if remaining is None or reset is None:
            return
        limit = self._int_header(headers, "X-RateLimit-Limit")
        key = self._key(token, path)
        with self._lock:
            st = self._state.get(key)
            if st is None:
                st = {"remaining": -1, "reset": 0, "pushed": 0.0,
                      "warned_reset": -1, "limit": None, "logged": False}
                self._state[key] = st
            if not st["logged"] and limit:
                st["logged"] = True
                st["limit"] = limit
                cls = key[1]
                tok_preview = (token or "匿名")[:6] + "…" if token else "匿名"
                self._log(f"配额窗口锁定: {cls} {limit} 次/窗口 (token {tok_preview})",
                          "info")
            st["remaining"] = remaining
            st["reset"] = reset
            # 窗口尾异常长等待（配额被外部实例占用）→ 每窗口只告警一次
            implied = ((reset - now) / max(remaining, 1)
                       if remaining > 0 else reset - now)
            if (implied >= LONG_WAIT_WARN
                    and st["warned_reset"] != reset):
                st["warned_reset"] = reset
                self._log(
                    f"GitHub 配额被外部占用（剩 {remaining} 次，窗口 "
                    f"{implied:.0f}s 后重置）——自动退让",
                    "warning")

    def push(self, token: str | None, path: str, until_ts: float,
             now: float | None = None):
        """429/Retry-After 对齐：不早于 until_ts 发起该端点请求。"""
        now = time.time() if now is None else now
        key = self._key(token, path)
        with self._lock:
            st = self._state.get(key)
            if st is None:
                st = {"remaining": -1, "reset": 0, "pushed": 0.0,
                      "warned_reset": -1, "limit": None, "logged": False}
                self._state[key] = st
            st["pushed"] = max(st["pushed"], until_ts)

    def wait_before(self, token: str | None, path: str,
                    now: float | None = None) -> float:
        """返回下次请求前应等待的秒数（0 = 可立即发起）。"""
        now = time.time() if now is None else now
        key = self._key(token, path)
        with self._lock:
            st = self._state.get(key)
            if not st:
                return 0.0  # 无状态：首发请求，发完 observe 即可
            wait = 0.0
            if now < st["pushed"]:
                wait = st["pushed"] - now
            elif st["reset"] <= now:
                wait = 0.0  # 窗口已滚动：新窗口可立即发起（下个响应刷新状态）
            elif st["remaining"] <= 0:
                wait = st["reset"] - now + 0.3  # 窗口尾 + 时钟偏差余量
            else:
                # 均匀铺排：剩余请求平分到重置时刻——永不提前打光
                wait = (st["reset"] - now) / st["remaining"] + 0.15
            return max(wait, 0.0)

    def snapshot(self) -> dict:
        """调试/TUI 用：当前各配额桶状态。"""
        with self._lock:
            return {f"{k[0][:8]}|{k[1]}": dict(v)
                    for k, v in self._state.items()}
