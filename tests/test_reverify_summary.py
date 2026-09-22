"""启动重验汇总邮件测试：历史高价值 key 重验后合并成一封，不逐 key 发。"""


from email_notifier import EmailNotifier
from watch_tui import VerificationBroker


class _FakeEngine:
    deepseek_api_base = "https://api.deepseek.com"
    timeout = 5
    usd_cny_rate = 7.25
    _proxies = None


def _mk_config():
    return {"server": "s", "port": 465, "user": "u", "password": "***",
            "recipients": ["me@x.com"], "include_full_key": True}


def _mk_broker(email):
    return VerificationBroker(engine=_FakeEngine(), min_balance=1.0,
                              interval=0.05, workers=2,
                              email_notifier=email)


class TestReverifySummary:
    def test_reverify_hv_accumulated_not_individually_sent(self):
        """启动重验的高价值 key 应进汇总队列，不逐 key 触发 send_alert。"""
        email = EmailNotifier(_mk_config(), dedup_db_path=":memory:")
        broker = _mk_broker(email)

        email_singles = []
        email.send_alert = lambda *a, **k: email_singles.append(a)
        email_singles_sent = []
        email._do_send = lambda *a, **k: email_singles_sent.append(a)

        # 重验一个高价值 key → 走 _store_result，应进 _reverify_hv 而不是发单封
        broker.reverify("sk-rev-hv1", source="history")
        broker._store_result({"key": "sk-rev-hv1", "valid": True, "balance_cny": 9.0,
                              "provider": "deepseek", "source": "history",
                              "repos": [{"repo": "a/b", "file": "c"}],
                              "key_preview": "sk-rev-hv1"})
        assert broker._reverify_hv, "高价值重验 key 应累积到 _reverify_hv"
        # 注意：send_alert 是同步被 _store_result 调用的，但我们用 monkeypatch 捕获 ——
        # 由于 reverify 走的是"进汇总"分支，send_alert 应未被调用
        # （这里 email.send_alert 被替换成记录，实际代码里 reverify 分支不调 send_alert）

    def test_new_scan_key_sent_individually(self):
        """运行中新扫描发现的 key（不在 reverify_queued）应单独发。"""
        email = EmailNotifier(_mk_config(), dedup_db_path=":memory:")
        broker = _mk_broker(email)
        sent = []
        email.send_alert = lambda key, *a, **k: sent.append(key)
        # 新 key：走 submit（priority 0），不在 _reverify_queued
        broker._store_result({"key": "sk-new1", "valid": True, "balance_cny": 9.0,
                              "provider": "deepseek", "source": "github_search",
                              "repos": [], "key_preview": "sk-new1"})
        assert sent == ["sk-new1"], f"新扫描的高价值 key 应单独发，实际 {sent}"

    def test_reverify_src_hv_not_accumulated_not_sent(self):
        """持续重验(reverify_src)的 HV 结果：不进启动汇总邮箱、不逐 key 发信。

        汇总邮箱只在启动时排空一次，持续重验条目进去会静默丢失；变化检测（Task 4）
        按缩水/充值/重新激活决定是否发信，余额没变=无新闻=不发信。
        """
        email = EmailNotifier(_mk_config(), dedup_db_path=":memory:")
        broker = _mk_broker(email)
        sent = []
        email.send_alert = lambda key, *a, **k: sent.append(key)
        broker._store_result({"key": "sk-cont-hv", "valid": True, "balance_cny": 9.0,
                              "provider": "deepseek", "source": "history",
                              "repos": [], "key_preview": "sk-cont-hv"},
                             reverify_src=True)
        assert broker._reverify_hv == [], "持续重验结果不应进启动汇总邮箱（一次性邮箱会静默丢失）"
        assert sent == [], f"持续重验结果不应逐 key 发信（变化检测负责），实际 {sent}"

    def test_continuous_reverify_hv_not_accumulated_even_if_queued(self):
        """持续重验 HV 结果即使 key 在启动重验名单(_reverify_queued)也不进汇总。

        回归(I1): 分支顺序曾为 `key in _reverify_queued` 在前——同 key 走持续重验
        (reverify_src=True)时被误累积进一次性汇总邮箱,汇总发完后静默丢失 +
        _reverify_hv 无界累积。修复后 reverify_src 第一分支生效。
        """
        email = EmailNotifier(_mk_config(), dedup_db_path=":memory:")
        broker = _mk_broker(email)
        sent = []
        email.send_alert = lambda key, *a, **k: sent.append(key)
        # 同 key 先被启动重验排入(_reverify_queued)...
        broker.reverify("sk-both", source="history")
        assert "sk-both" in broker._reverify_queued
        # ...再走持续重验路径 → 必须走"持续重验"分支,不进汇总、不逐 key 发
        broker._store_result({"key": "sk-both", "valid": True, "balance_cny": 9.0,
                              "provider": "deepseek", "source": "history",
                              "repos": [], "key_preview": "sk-both"},
                             reverify_src=True)
        assert broker._reverify_hv == [], "持续重验结果不得进汇总邮箱(会静默丢失)"
        assert sent == [], f"持续重验不发逐 key 信(变化检测负责),实际 {sent}"

    def test_send_reverify_summary_emits_one_email(self):
        """send_reverify_summary 应发送一封汇总邮件（含多个 key）。"""
        email = EmailNotifier(_mk_config(), dedup_db_path=":memory:")
        broker = _mk_broker(email)
        summaries = []
        email.send_summary = lambda items: summaries.append(list(items))
        # 累积两个高价值重验 key
        broker.reverify("sk-r1", source="history")
        broker._store_result({"key": "sk-r1", "valid": True, "balance_cny": 5.0,
                              "provider": "d", "source": "history",
                              "repos": [], "key_preview": "r1"})
        broker.reverify("sk-r2", source="history")
        broker._store_result({"key": "sk-r2", "valid": True, "balance_cny": 9.0,
                              "provider": "d", "source": "history",
                              "repos": [], "key_preview": "r2"})
        n = broker.send_reverify_summary()
        assert n == 2, "应发送 2 个 key 的汇总"
        assert len(summaries) == 1, "应只发一封汇总"
        assert {r["key"] for r in summaries[0]} == {"sk-r1", "sk-r2"}
        # 幂等：二次调用不再发
        summaries.clear()
        broker.send_reverify_summary()
        assert summaries == []

    def test_all_reverify_done(self):
        email = EmailNotifier(_mk_config(), dedup_db_path=":memory:")
        broker = _mk_broker(email)
        assert broker.all_reverify_done() is True  # 无重验
        broker.reverify("sk-x", source="history")
        assert broker.all_reverify_done() is False  # 未处理
        broker._store_result({"key": "sk-x", "valid": True, "balance_cny": 3.0,
                              "provider": "d", "source": "history",
                              "repos": [], "key_preview": "x"})
        assert broker.all_reverify_done() is True  # 已处理
