"""代理解析 + 回退逻辑测试（全部 mock 网络，离线可跑）。"""
import requests

import run


class TestProxyIsReachable:
    def test_true_on_any_response(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: object())  # 收到"响应"
        assert run._proxy_is_reachable("http://127.0.0.1:7897") is True

    def test_false_on_connection_error(self, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.ConnectionError
        monkeypatch.setattr(requests, "get", boom)
        assert run._proxy_is_reachable("http://127.0.0.1:1") is False


class TestResolveProxyFallback:
    def test_falls_back_to_direct_when_unreachable(self, monkeypatch):
        # 配置/传入的代理不可达 → 回退为 None(直连)
        monkeypatch.setattr(run, "_proxy_is_reachable", lambda p, timeout=3.0: False)
        assert run._resolve_proxy("http://127.0.0.1:7897") is None

    def test_keeps_proxy_when_reachable(self, monkeypatch):
        monkeypatch.setattr(run, "_proxy_is_reachable", lambda p, timeout=3.0: True)
        assert run._resolve_proxy("http://127.0.0.1:7897") == "http://127.0.0.1:7897"

    def test_none_when_no_proxy_anywhere(self, monkeypatch):
        monkeypatch.setattr(run, "_proxy_is_reachable", lambda p, timeout=3.0: True)
        monkeypatch.setattr(run, "_detect_local_proxy", lambda timeout=1.0: "")
        # 清空 config + env，且本地探测无果 → 直连
        import config_loader
        monkeypatch.setattr(type(config_loader.config), "proxy_url",
                            property(lambda self: ""))
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("http_proxy", raising=False)
        assert run._resolve_proxy() is None
