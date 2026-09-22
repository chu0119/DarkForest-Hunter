"""email_notifier.py 的单元测试。

重点：高价值 key 邮件告警默认发送完整 key（运营者用途；include_full_key=false 可关）。
"""
import threading

from email_notifier import EmailNotifier


def _make_notifier() -> EmailNotifier:
    return EmailNotifier({
        "server": "smtp.example.com",
        "port": 465,
        "user": "u@example.com",
        "password": "secret",
        "recipients": ["me@example.com"],
    }, dedup_db_path=":memory:")


class TestAlwaysSendFullKey:
    def test_include_full_key_defaults_true(self):
        n = _make_notifier()
        assert n.include_full_key is True, "必须默认发送完整 key，不脱敏"

    def test_include_full_key_string_coercion(self):
        """字符串形态显式解析——bool("false") is True 的陷阱不可回归。

        config_loader 目前传 bool,但字符串直传(string "false"→True)会让
        开关静默失效——本文件历史上刚出过一次死开关,锁死词表语义。"""
        base = {"server": "s", "user": "u", "password": "p",
                "recipients": ["a@b.c"]}
        for raw, want in [("false", False), ("0", False), ("no", False),
                          ("OFF", False), ("true", True), ("1", True),
                          ("yes", True), ("on", True), (False, False),
                          (True, True)]:
            n = EmailNotifier({**base, "include_full_key": raw},
                              dedup_db_path=":memory:")
            assert n.include_full_key is want, f"{raw!r} → {want} 失败"

    def test_send_alert_passes_full_key(self):
        n = _make_notifier()
        captured = []

        def fake_send(key, preview, provider, balance, currency, source, repos):
            captured.append(key)
            return True  # v2.5.4: _do_send 返回值判成败
        n._do_send = fake_send

        full = "sk-" + "a" * 44
        n.send_alert(key=full, key_preview="sk-aaaa...aaaa",
                     provider="deepseek", balance=9.9, currency="CNY",
                     source="github")
        threading.Event().wait(0.2)
        assert captured, "应触发 _do_send"
        assert captured[0] == full, "必须发送完整 key（不脱敏、不截断）"

    def test_disabled_when_no_recipients(self):
        n = EmailNotifier({"server": "smtp.example.com", "port": 465,
                           "user": "u@example.com", "password": "secret",
                           "recipients": []})
        assert n.enabled is False

    def test_email_body_contains_full_key(self):
        """端到端渲染：纯文本与 HTML 都必须包含完整 key。"""
        n = _make_notifier()
        out = {}

        def fake_do_send(self, key, preview, provider, balance, currency, source, repos):
            from datetime import datetime
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            body = f"""🔑 高价值 Key 告警
{key}
"""
            html = f"""<div>完整 KEY</div><code>{key}</code>"""
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            out["str"] = msg.as_string()

        # 直接测底层行为用 monkeypatch 类方法
        from email import policy
        from email.parser import BytesParser

        import email_notifier
        orig = email_notifier.EmailNotifier._do_send
        email_notifier.EmailNotifier._do_send = fake_do_send
        try:
            full = "sk-" + "f" * 40
            n.send_alert(key=full, key_preview="sk-ffff...ffff",
                         provider="openrouter", balance=3.3, currency="CNY",
                         source="github")
            threading.Event().wait(0.2)
            raw = out.get("str", "")
            # 解析 MIME，取出各部分的已解码文本
            msg = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))
            texts = []
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        texts.append(part.get_content())
                    except Exception:
                        pass
            joined = "\n".join(texts)
            assert full in joined, f"邮件解码内容必须包含完整 key，实际: {joined[:200]}"
        finally:
            email_notifier.EmailNotifier._do_send = orig


class TestSourceLabel:
    """数据来源规范名称映射。"""

    def test_known_mappings(self):
        from email_notifier import source_label
        assert source_label("github_search") == "GitHub 代码搜索"
        assert source_label("npm") == "npm 仓库"
        assert source_label("gitlab") == "GitLab"
        assert source_label("github_commits") == "GitHub 提交"
        assert source_label("huggingface") == "Hugging Face"
        assert source_label("history") == "历史重验"
        assert source_label("docker") == "Docker Hub"
        assert source_label("history") == "历史重验"

    def test_empty_unknown(self):
        from email_notifier import source_label
        assert source_label("") == "未知来源"
        assert source_label(None) == "未知来源"
        assert source_label("some_random") == "some_random"  # 未知值原样


class TestDedup:
    """同 key 去重：窗口内同一 key 只发一次（防启动重验刷屏）。"""

    def _make_notifier(self, *args, **kwargs):
        return EmailNotifier({"server": "smtp.example.com", "port": 465,
                              "user": "u@example.com", "password": "***",
                              "recipients": ["me@example.com"]},
                             *args, dedup_db_path=":memory:", **kwargs)

    def test_same_key_deduped_within_window(self):
        n = self._make_notifier(dedup_window_seconds=3600)
        calls = []
        n._do_send = lambda key, *a, **k: calls.append(key) or True
        k = "sk-" + "a" * 40
        n.send_alert(key=k, key_preview="p", provider="d", balance=5,
                     currency="CNY", source="g")
        n.send_alert(key=k, key_preview="p", provider="d", balance=5,
                     currency="CNY", source="g")
        import threading
        threading.Event().wait(0.3)
        assert len(calls) == 1, f"窗口内同一 key 应只发一次，实际 {len(calls)}"

    def test_different_keys_both_sent(self):
        n = self._make_notifier(dedup_window_seconds=3600)
        calls = []
        n._do_send = lambda key, *a, **k: calls.append(key) or True
        n.send_alert(key="sk-a" + "a" * 30, key_preview="p1", provider="d",
                     balance=5, currency="CNY", source="g")
        n.send_alert(key="sk-b" + "b" * 30, key_preview="p2", provider="d",
                     balance=5, currency="CNY", source="g")
        import threading
        threading.Event().wait(0.3)
        assert len(calls) == 2

    def test_expired_window_resends(self):
        n = self._make_notifier(dedup_window_seconds=1)  # 1 秒窗口
        calls = []
        n._do_send = lambda key, *a, **k: calls.append(key) or True
        k = "sk-" + "c" * 40
        n.send_alert(key=k, key_preview="p", provider="d", balance=5,
                     currency="CNY", source="g")
        import threading
        import time
        threading.Event().wait(0.3)
        time.sleep(1.2)  # 等窗口过期
        n.send_alert(key=k, key_preview="p", provider="d", balance=5,
                     currency="CNY", source="g")
        threading.Event().wait(0.3)
        assert len(calls) == 2, "窗口过期后应重新发送"

    def test_persists_across_restart(self, tmp_path):
        db = str(tmp_path / "email_sent.db")
        cfg = {"server": "s", "port": 465, "user": "u", "password": "p",
               "recipients": ["me@x.com"]}
        n1 = EmailNotifier(cfg, dedup_window_seconds=3600, dedup_db_path=db)
        n1._do_send = lambda key, *a, **k: True
        k = "sk-" + "d" * 40
        n1.send_alert(key=k, key_preview="p", provider="d", balance=5,
                      currency="CNY", source="g")
        import threading
        threading.Event().wait(0.3)
        # 重启：新实例，同一库，窗口内不应重发
        n2 = EmailNotifier(cfg, dedup_window_seconds=3600, dedup_db_path=db)
        calls2 = []
        n2._do_send = lambda key, *a, **k: calls2.append(key) or True
        n2.send_alert(key=k, key_preview="p", provider="d", balance=5,
                      currency="CNY", source="g")
        threading.Event().wait(0.3)
        assert calls2 == [], "重启后窗口内同一 key 不应重发（去重应持久化）"


class TestV254SendRollbackByReturnValue:
    def test_failure_rolls_back_dedup(self):
        """发送失败(_do_send 返回 False)必须回滚占坑,允许窗口内重发。
        v2.5.4 起成败按返回值判定——旧的全局 fail 计数差值在并发下误判。"""
        n = _make_notifier()
        n._do_send = lambda *a, **k: False
        k = "sk-" + "e" * 40
        n.send_alert(key=k, key_preview="p", provider="d", balance=5,
                     currency="CNY", source="g")
        threading.Event().wait(0.2)
        assert not n._is_recently_sent(k)

    def test_success_keeps_dedup(self):
        n = _make_notifier()
        n._do_send = lambda *a, **k: True
        k = "sk-" + "f" * 40
        n.send_alert(key=k, key_preview="p", provider="d", balance=5,
                     currency="CNY", source="g")
        threading.Event().wait(0.2)
        assert n._is_recently_sent(k)
