"""ReverifyScheduler 持续重验调度测试。"""
import os
import tempfile
from datetime import datetime, timedelta

import store
from watch_tui import ReverifyScheduler


def _ts(days_ago: float) -> str:
    """相对当前时间的 last_seen 字符串——硬编码日期会随时间过期(曾致测试失效)。"""
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


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

    broker = FakeBroker()
    s = ReverifyScheduler(broker, conn, budget_per_day=kw.get("budget", 100000),
                          email_threshold=5.0, top_threshold=10.0, shrink_pct=30.0,
                          zero_interval_hours=kw.get("zero_interval_hours", 168))
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
    conn.execute("UPDATE keys SET last_seen=datetime('now','localtime','-1 hour') WHERE key='sk-top'")
    conn.commit()
    s._tick()  # top(>=10元) 每 6h → last_seen 1h 前 → 跳过
    assert "sk-top" not in broker.enqueued
    assert "sk-low" in broker.enqueued  # 低价值按预算轮询


def test_tick_no_500_cap():
    """全量候选:第 500 名之后的低余额 key 也能被轮询到(无截断)。

    回归:早期实现用 get_valid_keys(batch=500)(余额前 500 名),
    第 500 名后的 key 永远进不了候选 → 永不重验(0 余额充值检测失效)。
    速率补充桶下单 tick 最多消耗桶容量(10)——用多 tick + 时间流逝模拟全量可达。
    """
    s, broker, conn = _make_scheduler(budget=100000)
    for i in range(501):
        k = f"sk-bulk-{i:03d}"
        conn.execute("INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,valid,balance,currency,status,first_seen,last_seen) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (store.hash_key(k), k, k, "deepseek", "github_search", "", "", "",
                      1, 0.01, "CNY", "valid_active", "2026-08-01 00:00:00", "2026-08-01 00:00:00"))
    conn.commit()
    for _ in range(300):  # 每轮模拟 1 小时流逝 → 桶补满 10 个令牌
        s._last_refill -= 3600
        s._tick()
        if len(broker.enqueued) >= 503:
            break
    assert len(broker.enqueued) >= 503       # 预算充足 → 全部到期 key 可达
    assert "sk-bulk-500" in broker.enqueued  # 第 500 名之后也轮询到


def test_tick_bucket_cap_ten_per_tick():
    """速率补充桶:预算充足时单 tick 也只消耗桶容量(10),不首轮烧光。

    回归(I2): 旧实现每日重置桶启动即全量烧光 1500 预算,之后整天无令牌,
    6h/12h 分层稳态失效;速率补充桶把预算平滑铺到 24h(≈1.7/min)。
    """
    s, broker, conn = _make_scheduler(budget=1500)
    for i in range(25):
        k = f"sk-bucket-{i:02d}"
        conn.execute("INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,valid,balance,currency,status,first_seen,last_seen) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (store.hash_key(k), k, k, "deepseek", "github_search", "", "", "",
                      1, 0.01, "CNY", "valid_active", "2026-08-01 00:00:00", "2026-08-01 00:00:00"))
    conn.commit()
    n1 = s._tick()
    assert n1 == 10, f"单 tick 最多桶容量 10,实际 {n1}"
    assert len(broker.enqueued) == 10
    assert s._tokens <= 0  # 桶耗尽
    # 模拟 1 小时流逝:补充 1500*3600/86400 ≈ 62.5 → 封顶 10,再消耗 10
    s._last_refill -= 3600
    n2 = s._tick()
    assert n2 == 10
    assert len(broker.enqueued) == 20


def test_tick_hv_interval_six_hours_smooth_budget():
    """HV key(>=10 元)在预算平滑分布下按 6h 间隔被重验。

    规格 §3.1:>=10 元每 6h。速率补充桶在到点时有令牌可用(HV 层稳态触发),
    而非每日重置桶首轮烧光后整天无令牌。
    """
    s, broker, conn = _make_scheduler(budget=1500)
    conn.execute("UPDATE keys SET last_seen=datetime('now','localtime','-5 hours') WHERE key='sk-top'")
    conn.commit()
    s._tick()  # 5h < 6h → 未到期,跳过
    assert "sk-top" not in broker.enqueued
    # 6.5h 前验证(过 6h 间隔)→ 且时间流逝补充令牌(模拟 1h) → 入队
    conn.execute("UPDATE keys SET last_seen=datetime('now','localtime','-390 minutes') WHERE key='sk-top'")
    conn.commit()
    s._last_refill -= 3600
    s._tick()
    assert "sk-top" in broker.enqueued
    # 入队后间隔重置:立即再 tick → 不再入队(6h 未到)
    s._last_refill -= 3600
    s._tick()
    assert broker.enqueued.count("sk-top") == 1


def test_tick_includes_zero_balance_valid():
    """非 deepseek 0 余额有效 key(valid_zero/valid_no_balance)也纳入重验轮询。

    回归:DB 里 valid 列与"有余额"无关——store.upsert 写
    valid = int(bool(result.get("valid"))),valid_zero 状态同样 valid=1;
    _tick 候选 SQL 的 WHERE valid=1 必须包含它们,否则 0 余额有效 key
    永不重验——一旦充值无法感知(218 个 valid_zero + 15 个 valid_no_balance)。
    """
    s, broker, conn = _make_scheduler(budget=100000)
    # 插入一个非 deepseek 0 余额有效 key
    conn.execute("INSERT INTO keys (key_hash, key, key_preview, provider, source, repo, file, url, valid, balance, currency, status, first_seen, last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-kimi-zero"), "sk-kimi-zero", "sk-kimi-zero", "kimi", "github_search", "", "", "",
                  1, 0.0, "CNY", "valid_zero", "2026-08-01 00:00:00", "2026-08-01 00:00:00"))
    conn.commit()
    s._tick()
    assert "sk-kimi-zero" in broker.enqueued  # 0 余额有效 key 也要轮询(可能充值)


def test_tick_dynamic_frequency_on_declining_balance():
    """余额下降趋势 → 间隔减半(加速检测缩水)。"""
    s, broker, conn = _make_scheduler(budget=100000)
    # 写入下降趋势历史: 5 次 10→9→8→7→6
    kh = store.hash_key("sk-trend")
    for i, bal in enumerate([10.0, 9.0, 8.0, 7.0, 6.0]):
        conn.execute("INSERT INTO key_history (key_hash, verified_at, balance_cny, status, valid) VALUES (?,?,?,?,?)",
                     (kh, f"2026-08-12 0{i}:00:00", bal, "valid_active", 1))
    conn.commit()
    # 余额 5.0 → HV 层(12h),下降趋势 → 减半为 6h
    interval = s._tier_interval(5.0, -0.5)
    assert interval == s._TIER_INTERVAL_HV * 0.5, f"下降趋势应减半: {interval} vs {s._TIER_INTERVAL_HV * 0.5}"
    # 上升趋势 → 翻倍为 24h
    interval_up = s._tier_interval(5.0, 0.5)
    assert interval_up == s._TIER_INTERVAL_HV * 2.0, f"上升趋势应翻倍: {interval_up} vs {s._TIER_INTERVAL_HV * 2.0}"
    # 无趋势 → 基础间隔
    assert s._tier_interval(5.0, 0.0) == s._TIER_INTERVAL_HV


def test_zero_balance_recently_verified_skipped():
    """0 余额 key 刚验过(< 7d)→ 不重验,把预算让给活跃 key。

    回归: 旧实现 0 余额层 ~403s 间隔,2000 个 0 余额 key 每天吃掉几乎全部 1500 预算,
    53 个 valid_active 反而排不上。改周级后 0 余额需求降到 ~286/天,预算富余。
    """
    s, broker, conn = _make_scheduler(budget=100000)
    conn.execute("INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,valid,balance,currency,status,first_seen,last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-zero-recent"), "sk-zero-recent", "sk-zero-recent", "kimi", "github_search", "", "", "",
                  1, 0.0, "CNY", "valid_zero", "2026-08-01 00:00:00", _ts(1)))  # 1 天前 → 7d 未到
    conn.commit()
    s._tick()
    assert "sk-zero-recent" not in broker.enqueued


def test_zero_balance_due_after_seven_days():
    s, broker, conn = _make_scheduler(budget=100000)
    conn.execute("INSERT INTO keys (key_hash,key,key_preview,provider,source,repo,file,url,valid,balance,currency,status,first_seen,last_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (store.hash_key("sk-zero-old"), "sk-zero-old", "sk-zero-old", "kimi", "github_search", "", "", "",
                  1, 0.0, "CNY", "valid_zero", "2026-08-01 00:00:00", _ts(16)))  # 16 天前 → 7d 已过
    conn.commit()
    s._tick()
    assert "sk-zero-old" in broker.enqueued


def test_zero_interval_configurable():
    s, broker, conn = _make_scheduler(budget=100000, zero_interval_hours=1)
    assert s._tier_interval(0.0, 0.0) == 3600.0
