"""config_loader 配置加载测试。"""


def test_watch_reverify_config_defaults():
    from config_loader import ConfigLoader
    cfg = ConfigLoader()
    assert cfg.watch_reverify_budget == 1500
    assert cfg.watch_hv_email_threshold == 5.0
    assert cfg.watch_hv_top_threshold == 10.0
    assert cfg.watch_shrink_warn_pct == 30.0
    assert cfg.watch_commits_since_hours == 168


def test_chat_probe_is_enabled_by_default():
    """v2.4.2: chat 探测默认开(运营者决策:微额消耗换账本口径校准)。"""
    from config_loader import ConfigLoader
    cfg = ConfigLoader()
    assert cfg.allow_chat_probe is True
    assert cfg.probe_unclear_platforms is True


# ── v2.5.4: 插值禁用与文件级容错 ──

class TestV254ConfigLoaderHardening:
    def test_percent_in_value_not_interpolated(self, monkeypatch):
        """配置值含裸 %(URL 编码代理密码等)不得被 BasicInterpolation 抛错后
        被 _get 静默吞成空串——代理/邮件曾因此无声失效。"""
        import config_loader
        c = config_loader.Config()
        monkeypatch.setattr(config_loader, "_CONFIG_PATHS", ["dummy.ini"])
        monkeypatch.setattr(config_loader.os.path, "exists", lambda p: True)

        def fake_read(path, **kw):
            c._config.read_string("[proxy]\nurl = http://user:p%40ss@host:7890\n")
            return []
        monkeypatch.setattr(c._config, "read", fake_read)
        c._load()
        assert c.proxy_url == "http://user:p%40ss@host:7890"
        assert "%40ss" in c.proxy_url

    def test_broken_config_file_falls_back_to_defaults(self, monkeypatch):
        """文件级解析错误(缺节头等)降级为内置默认值,不得炸掉首次配置访问。"""
        import configparser

        import config_loader
        c = config_loader.Config()
        monkeypatch.setattr(config_loader, "_CONFIG_PATHS", ["dummy.ini"])
        monkeypatch.setattr(config_loader.os.path, "exists", lambda p: True)

        def boom(path, **kw):
            raise configparser.MissingSectionHeaderError("dummy.ini", 1, "broken")
        monkeypatch.setattr(c._config, "read", boom)
        c._load()  # 不得抛异常
        assert c.watch_interval == 300  # 内置默认值
