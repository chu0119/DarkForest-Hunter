"""RateScheduler 单测——header-driven 配额调度的核心数学。

回归保护：
1. 均匀铺排：wait = (reset-now)/remaining，永不提前打光
2. 窗口尾/窗口外/pushed 三种边界
3. 外部占用告警每窗口只发一次
4. 缺头不污染状态
"""
import time

from rate_scheduler import LONG_WAIT_WARN, RateScheduler


def _headers(remaining, reset_in, limit=10, resource="code_search"):
    return {
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(int(time.time() + reset_in)),
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Resource": resource,
    }


class TestWaitBefore:
    def test_first_request_no_state_no_wait(self):
        s = RateScheduler()
        assert s.wait_before("tok", "/search/code") == 0.0

    def test_even_spread_across_window(self):
        """核心不变式：剩余请求均匀铺到重置时刻。"""
        s = RateScheduler()
        now = 1000.0
        s.observe("tok", "/search/code",
                  {"X-RateLimit-Remaining": "5",
                   "X-RateLimit-Reset": "1060",
                   "X-RateLimit-Limit": "10"}, now=now)
        # 剩 5 次、60s 窗口 → 每次 ~12s(+0.15 余量)
        w = s.wait_before("tok", "/search/code", now=now)
        assert 12.0 <= w <= 12.5

    def test_window_exhausted_waits_to_reset(self):
        s = RateScheduler()
        now = 1000.0
        s.observe("tok", "/search/code",
                  {"X-RateLimit-Remaining": "0",
                   "X-RateLimit-Reset": "1040"}, now=now)
        w = s.wait_before("tok", "/search/code", now=now)
        # remaining=0 → 睡到 reset(+0.3 时钟余量)
        assert 40.2 <= w <= 40.4

    def test_window_rolled_over_no_wait(self):
        """窗口已过期 → 立即可发（新窗口由下个响应刷新）。"""
        s = RateScheduler()
        s.observe("tok", "/search/code",
                  {"X-RateLimit-Remaining": "0",
                   "X-RateLimit-Reset": "1000"}, now=1000.0)
        assert s.wait_before("tok", "/search/code", now=1005.0) == 0.0

    def test_bucket_isolation(self):
        """code_search 与 repo_search 是独立配额桶，互不干扰。"""
        s = RateScheduler()
        now = 1000.0
        s.observe("tok", "/search/code",
                  {"X-RateLimit-Remaining": "0",
                   "X-RateLimit-Reset": "1060"}, now=now)
        # repo 桶无状态 → 不受 code 桶耗尽影响
        assert s.wait_before("tok", "/search/repositories", now=now) == 0.0

    def test_token_isolation(self):
        s = RateScheduler()
        now = 1000.0
        s.observe("tokA", "/search/code",
                  {"X-RateLimit-Remaining": "0",
                   "X-RateLimit-Reset": "1060"}, now=now)
        assert s.wait_before("tokB", "/search/code", now=now) == 0.0

    def test_push_wins_over_spread(self):
        """429/Retry-After push 的等待优先于均匀铺排。"""
        s = RateScheduler()
        now = 1000.0
        s.observe("tok", "/search/code",
                  {"X-RateLimit-Remaining": "9",
                   "X-RateLimit-Reset": "1060"}, now=now)
        s.push("tok", "/search/code", 1050.0, now=now)
        w = s.wait_before("tok", "/search/code", now=now)
        assert 49.9 <= w <= 50.1
        # push 到期后回落到均匀铺排
        w2 = s.wait_before("tok", "/search/code", now=1050.0)
        assert w2 < 10.0


class TestObserve:
    def test_missing_headers_no_state(self):
        s = RateScheduler()
        s.observe("tok", "/search/code", {})  # 401/代理丢头
        assert s.wait_before("tok", "/search/code") == 0.0

    def test_external_contention_warns_once_per_window(self):
        """外部占用长等待：每窗口只告警一次，不刷屏。"""
        logs = []
        s = RateScheduler(log=lambda m, l="info": logs.append((l, m)))
        now = 1000.0
        hdrs = {"X-RateLimit-Remaining": "1",
                "X-RateLimit-Reset": str(int(now + LONG_WAIT_WARN + 20))}
        s.observe("tok", "/search/code", hdrs, now=now)
        s.observe("tok", "/search/code", hdrs, now=now + 5)  # 同窗口重放
        warns = [m for l, m in logs if l == "warning"]
        assert len(warns) == 1
        assert "配额被外部占用" in warns[0]

    def test_normal_spacing_never_warns(self):
        """均匀铺排自身永远产生不了 ≥40s 的等待——不该有告警。"""
        logs = []
        s = RateScheduler(log=lambda m, l="info": logs.append((l, m)))
        now = 1000.0
        # 10 次 / 60s：剩余 10 → 每次 6s
        s.observe("tok", "/search/code",
                  {"X-RateLimit-Remaining": "10",
                   "X-RateLimit-Reset": str(int(now + 60))}, now=now)
        assert not any(l == "warning" for l, _ in logs)
        assert s.wait_before("tok", "/search/code", now=now) < 7.0

    def test_limit_log_once(self):
        logs = []
        s = RateScheduler(log=lambda m, l="info": logs.append((l, m)))
        now = 1000.0
        hdrs = {"X-RateLimit-Remaining": "9",
                "X-RateLimit-Reset": str(int(now + 60)),
                "X-RateLimit-Limit": "10"}
        s.observe("tok", "/search/code", hdrs, now=now)
        s.observe("tok", "/search/code", hdrs, now=now + 6)
        lock_lines = [m for l, m in logs if "配额窗口锁定" in m]
        assert len(lock_lines) == 1
        assert "code_search 10 次/窗口" in lock_lines[0]
