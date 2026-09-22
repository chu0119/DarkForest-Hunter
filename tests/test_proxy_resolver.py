"""proxy_resolver.py 的测试 — 订阅解析与多代理路由。"""


from proxy_resolver import (
    MultiProxyRouter,
    _parse_trojan,
    _parse_vmess,
)

# ── 链接解析 ────────────────────────────────────────────────────────

class TestParseTrojan:
    def test_basic_trojan(self):
        # 正确格式: trojan://password@host:port?params#name
        link = "trojan://mypassword@1.2.3.4:443?allowInsecure=0&sni=example.com#MyNode"
        info = _parse_trojan(link)
        assert info is not None
        assert info["protocol"] == "trojan"
        assert info["host"] == "1.2.3.4"
        assert info["port"] == 443


class TestParseVMess:
    def test_basic_vmess(self):
        import base64
        import json
        payload = {"v": "2", "ps": "Test", "add": "1.2.3.4", "port": "443",
                   "id": "uuid-here", "aid": "0", "net": "tcp", "type": "none",
                   "host": "", "path": "", "tls": ""}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        link = f"vmess://{encoded}"
        info = _parse_vmess(link)
        assert info is not None
        assert info["protocol"] == "vmess"
        assert info["host"] == "1.2.3.4"
        assert info["port"] == 443


# ── MultiProxyRouter ────────────────────────────────────────────────

class TestMultiProxyRouter:
    def test_pacing_key_per_proxy(self):
        """不同 token 使用不同代理 IP → 不同 pacing key。"""
        router = MultiProxyRouter([
            "http://127.0.0.1:7897",
            "http://127.0.0.1:7898",
        ])
        key1 = router.get_pacing_key("token_abc_123")
        key2 = router.get_pacing_key("token_def_456")
        assert key1 != key2, "不同 token 应分配到不同代理 IP"
        assert "127.0.0.1:7897" in key1 or "127.0.0.1:7898" in key1

    def test_cooldown_isolated(self):
        """一个 IP 被 429 不影响其他 IP。"""
        router = MultiProxyRouter([
            "http://127.0.0.1:7897",
            "http://127.0.0.1:7898",
        ])
        token_a = "token_aaaaaaaaaaaaa"
        token_b = "token_bbbbbbbbbbbbb"
        # v2.4.9: get_pacing_key 只读路由状态,触发 429 前先建立 token→IP 映射
        router.get_pacing_key(token_a)
        router.get_pacing_key(token_b)
        # token_a 触发 429
        router.on_ip_rate_limit(token_a)
        assert router.is_proxy_in_cooldown(token_a) is True
        assert router.is_proxy_in_cooldown(token_b) is False, "IP 隔离:B 不应受影响"

    def test_single_proxy_fallback(self):
        """单代理时 pacing key 回退到 token。"""
        router = MultiProxyRouter([])
        key = router.get_pacing_key("short_token")
        assert key == "short_token"  # token 完整回退(<20 字符)

    def test_transport_failure_rotates_port(self):
        """v2.5.1: TLS/连接重立即换端口——同端口重试无效(实测 23 查询 3 败全丢)。"""
        router = MultiProxyRouter([f"http://127.0.0.1:{p}"
                                   for p in (17890, 17891, 17892)])
        tok = "token_transport_aaa"
        idx1 = router._assign_proxy_for_token(tok)
        router.on_transport_failure(tok)
        idx2 = router._assign_proxy_for_token(tok)
        assert idx1 != idx2, f"传输故障后应轮换端口: {idx1} -> {idx2}"

    def test_transport_failure_no_freeze_when_all_cooling(self):
        """没有其他健康端口时不冷却——否则全体冷却无路可走。"""
        router = MultiProxyRouter(["http://127.0.0.1:17890",
                                   "http://127.0.0.1:17891"])
        tok_a, tok_b = "token_freeze_aaa", "token_freeze_bbb"
        router._assign_proxy_for_token(tok_a)
        router._assign_proxy_for_token(tok_b)
        # 先冷却掉一个(另一个健康)
        router.on_transport_failure(tok_a)
        # 再对映射到同一(已被冷却重绑后的)端口触发——两个端口都冷却时不再冻结
        router.on_transport_failure(tok_b)
        router.on_transport_failure(tok_b)
        # 至少仍能分配到端口(不因全冷却而失败)
        idx = router._assign_proxy_for_token(tok_a)
        assert 0 <= idx < 2

    def test_transport_failure_single_proxy_noop(self):
        """单端口时传输故障是 no-op(冷却了没处去)。"""
        router = MultiProxyRouter(["http://127.0.0.1:17890"])
        tok = "token_single"
        router._assign_proxy_for_token(tok)
        router.on_transport_failure(tok)  # 不应抛异常
        assert router._assign_proxy_for_token(tok) == 0


# ── v2.5.4: 订阅 URL 校验(私网/环回/协议) ──

class TestV254SubscriptionUrlGuard:
    def test_rejects_loopback_host(self):
        from proxy_resolver import parse_subscription_links
        assert parse_subscription_links("http://127.0.0.1:8080/sub") == []

    def test_rejects_non_http_scheme(self):
        from proxy_resolver import parse_subscription_links
        assert parse_subscription_links("ftp://example.com/sub") == []
