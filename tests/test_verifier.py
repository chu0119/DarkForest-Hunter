"""UnifiedKeyVerifier 验证逻辑测试（全部 mock 网络请求，离线可跑）。

P0-3 后验证改为**只读**：GET {models_endpoint} 判定有效性，GET balance 查余额。
核心安全性质：验证流程绝不调用 requests.post（不消耗 key 持有者配额）。
"""

import pytest

from providers import ProviderRateLimiter, UnifiedKeyVerifier, VerifyResult


class FakeResponse:
    """最小可用的 requests 响应替身。"""

    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text

    def json(self):
        return self._data


def _key(length=32):
    # hex 基底:同时满足 deepseek/qwen 的 hex 预检与 kimi 的 alnum 预检。
    # 长度 32 对齐真实 deepseek/qwen key;调用方传 length 仅作文档,不改变返回。
    return "sk-" + "1e175253812a494886dd8952b56dc19c"[:length] if length <= 32 \
        else "sk-" + "1e175253812a494886dd8952b56dc19c" + "0" * (length - 32)


# ── 验证流程：GET 认证 + 余额参考 + chat 探测为准 ────────────────────

class TestVerificationFlow:
    def test_chat_probe_is_minimal_request(self, monkeypatch):
        """chat 探测必须是 max_tokens=1 的最小请求（微额，不浪费配额）。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)
        payloads = []

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            payloads.append(kw.get("json", {}))
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        verifier.verify_key(_key(), "deepseek")
        assert payloads, "应有 chat 探测请求"
        assert payloads[0].get("max_tokens") == 1, "探测必须 max_tokens=1"
        assert len(payloads[0].get("messages", [])) == 1


# ── DeepSeek 验证 ───────────────────────────────────────────────────

class TestDeepSeekVerify:
    def test_valid_with_balance(self, monkeypatch):
        verifier = UnifiedKeyVerifier(proxy=None)

        def fake_get(url, **kwargs):
            if "balance" in url:
                return FakeResponse(200, {
                    "balance_infos": [{
                        "currency": "USD", "total_balance": 1.5,
                        "granted_balance": 1.5, "tipped_balance": 0.0,
                    }]
                })
            return FakeResponse(200, {"data": []})  # /models

        def fake_post(url, **kw):
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "deepseek")
        assert r["status"] == VerifyResult.VALID_ACTIVE.value
        assert r["balance"] == 1.5
        assert r["provider"] == "deepseek"

    def test_models_url_is_readonly(self, monkeypatch):
        """DeepSeek 验证第一个请求是 GET /models（只读端点优先）。"""
        verifier = UnifiedKeyVerifier()
        urls = []

        def fake_get(url, **kwargs):
            urls.append(url)
            if "balance" in url:
                return FakeResponse(200, {"balance_infos": [{"total_balance": 0}]})
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        verifier.verify_key(_key(), "deepseek")
        # 第一个请求必须是只读 models 端点（先认证，再探测）
        assert urls[0] == "https://api.deepseek.com/models"

    def test_valid_zero_balance(self, monkeypatch):
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "balance" in url:
                return FakeResponse(200, {
                    "balance_infos": [{"currency": "CNY", "total_balance": 0}]
                })
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "deepseek")
        assert r["status"] == VerifyResult.VALID_ZERO.value  # GET+余额判定，不发送生成请求

    def test_invalid_401(self, monkeypatch):
        verifier = UnifiedKeyVerifier()
        monkeypatch.setattr("providers.requests.get",
                            lambda url, **kw: FakeResponse(401))
        r = verifier.verify_key(_key(), "deepseek")
        assert r["status"] == VerifyResult.INVALID.value

    def test_rate_limited_429(self, monkeypatch):
        verifier = UnifiedKeyVerifier()
        monkeypatch.setattr("providers.requests.get",
                            lambda url, **kw: FakeResponse(429))
        r = verifier.verify_key(_key(), "deepseek")
        assert r["status"] == VerifyResult.RATE_LIMITED.value

    def test_insufficient_balance_402_counts_as_valid(self, monkeypatch):
        """HTTP 402 = 认证通过但欠费，必须算有效而非 error。"""
        verifier = UnifiedKeyVerifier()
        monkeypatch.setattr(
            "providers.requests.get",
            lambda url, **kw: FakeResponse(402, text='{"error":{"message":"Insufficient Balance"}}'),
        )
        r = verifier.verify_key(_key(), "deepseek")
        assert r["status"] == VerifyResult.VALID_ZERO.value
        assert r["balance"] == 0.0

    def test_network_error(self, monkeypatch):
        verifier = UnifiedKeyVerifier()

        def boom(url, **kw):
            raise OSError("connection reset")

        monkeypatch.setattr("providers.requests.get", boom)
        r = verifier.verify_key(_key(), "deepseek")
        assert r["status"] == VerifyResult.ERROR.value


# ── 代理策略 ────────────────────────────────────────────────────────

class TestProxyPolicy:
    def test_domestic_provider_direct_connection(self, monkeypatch):
        """国内平台直连：proxies 必须为 None。"""
        verifier = UnifiedKeyVerifier(proxy="http://127.0.0.1:7897")
        captured = {}

        def fake_get(url, **kwargs):
            captured["proxies"] = kwargs.get("proxies")
            return FakeResponse(401)

        monkeypatch.setattr("providers.requests.get", fake_get)
        verifier.verify_key(_key(), "deepseek")
        assert captured["proxies"] is None

    def test_overseas_provider_uses_proxy(self, monkeypatch):
        """海外平台（claude）走代理。"""
        verifier = UnifiedKeyVerifier(proxy="http://127.0.0.1:7897")
        captured = {}

        def fake_get(url, **kwargs):
            captured["proxies"] = kwargs.get("proxies")
            return FakeResponse(200, {"data": []})

        monkeypatch.setattr("providers.requests.get", fake_get)
        verifier.verify_key("sk-ant-" + "a" * 40, "claude")
        assert captured["proxies"] == {
            "http": "http://127.0.0.1:7897",
            "https": "http://127.0.0.1:7897",
        }


# ── Claude 验证 ─────────────────────────────────────────────────────

class TestClaudeVerify:
    def test_uses_x_api_key_and_models_endpoint(self, monkeypatch):
        verifier = UnifiedKeyVerifier()
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            return FakeResponse(200, {"data": []})

        monkeypatch.setattr("providers.requests.get", fake_get)
        key = "sk-ant-" + "a" * 40
        r = verifier.verify_key(key, "claude")

        assert captured["url"] == "https://api.anthropic.com/v1/models"
        assert captured["headers"]["x-api-key"] == key
        assert captured["headers"]["anthropic-version"] == "2023-06-01"
        # claude 无余额查询：不应进入 balance 分支
        assert r["status"] == VerifyResult.VALID_NO_BALANCE.value


# ── 智谱 JWT 与余额解析 ─────────────────────────────────────────────

class TestZhipuVerify:
    def test_jwt_format(self):
        verifier = UnifiedKeyVerifier()
        token = verifier._create_zhipu_jwt("testapikey")
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "testapikey"
        assert parts[1].isdigit()
        assert len(parts[2]) == 64  # sha256 hex

    def test_balance_parse_from_balanceInfos(self, monkeypatch):
        """v2.4.4 口径:zhipu 按量真余额 = users/balance 的 balance_infos 之和。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "users/balance" in url:
                return FakeResponse(200, {"balance_infos": [
                    {"balance": 80.5, "source": "resource_pack"},
                    {"balance": 8.0, "source": "recharge"},
                ]})
            return FakeResponse(405, text="mna")

        def fake_post(url, **kw):
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "zhipu")
        assert r["status"] == VerifyResult.VALID_ACTIVE.value
        assert r["balance"] == 88.5

    def test_kimi_balance_uses_cash_balance(self, monkeypatch):
        """v2.4.3 口径:kimi 余额取 cash_balance(现金),代金券不算钱。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "balance" in url:
                return FakeResponse(200, {"data": {
                    "available_balance": 249.58, "voucher_balance": 200,
                    "cash_balance": 49.58}})
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "kimi")
        assert r["status"] == VerifyResult.VALID_ACTIVE.value
        assert r["balance"] == 49.58, "必须取 cash_balance,而非含代金券的 available_balance"

    def test_kimi_legacy_response_deducts_voucher(self, monkeypatch):
        """旧响应缺 cash_balance 字段 → 用 available - voucher 折算现金。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "balance" in url:
                return FakeResponse(200, {"data": {
                    "available_balance": 49.58, "voucher_balance": 40}})
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "kimi")
        assert r["balance"] == pytest.approx(9.58)

    def test_kimi_negative_cash_is_valid_zero(self, monkeypatch):
        """cash_balance 为负(欠费,官方允许)→ 有效但无钱,不得判 valid_active。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "balance" in url:
                return FakeResponse(200, {"data": {
                    "available_balance": 100, "voucher_balance": 105,
                    "cash_balance": -5}})
            return FakeResponse(200, {"data": []})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key(_key(), "kimi")
        assert r["status"] == VerifyResult.VALID_ZERO.value
        assert r["balance"] == -5.0

    def test_kimi_voucher_balance_but_chat_suspended(self, monkeypatch):
        """kimi bug 回归：余额接口显示 240（全是代金券 voucher_balance）但
        chat 探测 429 欠费 → 必须判 VALID_ZERO 而非有效。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)

        def fake_get(url, **kw):
            if "balance" in url:
                return FakeResponse(200, {"data": {
                    "available_balance": 240, "voucher_balance": 240,
                    "cash_balance": 0}})
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            return FakeResponse(429, text='{"error":{"message":"Your account is '
                                          'suspended due to insufficient balance, '
                                          'please recharge your account"}}')

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "kimi")
        assert r["status"] == VerifyResult.VALID_ZERO.value, \
            "余额接口显示 240 但 chat 欠费 → 必须判欠费"
        assert r["balance"] == 0.0

    def test_kimi_429_pure_rate_limit_keeps_valid(self, monkeypatch):
        """429 但响应体无欠费字样 → 纯限流，不算欠费。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": {"available_balance": 10}})

        def fake_post(url, **kw):
            return FakeResponse(429, text='{"error":{"message":"rate limit exceeded, '
                                          'try again later"}}')

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "kimi")
        assert r["status"] == VerifyResult.RATE_LIMITED.value


# ── chat 探测（无余额接口平台：GET models 200 后补一次真实请求）──────────

class TestChatProbe:
    def test_chat_probe_disabled_by_default(self, monkeypatch):
        """默认不发生成请求：chat 探测必须显式 opt-in。"""
        verifier = UnifiedKeyVerifier()
        posts = []

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            posts.append(url)
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "qwen")
        assert posts == [], "默认验证不得发送 chat POST"
        assert r["status"] == VerifyResult.VALID_NO_BALANCE.value

    def test_chat_probe_explicit_opt_in(self, monkeypatch):
        """显式开启后，无余额平台仍可用最小 chat 请求确认真实可用性。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)
        payloads = []

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            payloads.append(kw.get("json", {}))
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "qwen")
        assert len(payloads) == 1
        assert r["status"] == VerifyResult.VALID_ACTIVE.value

    def test_no_balance_provider_probes_chat(self, monkeypatch):
        """无余额平台（qwen）：GET models 200 后必须 POST chat/completions 确认。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)
        posts = []

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            posts.append(url)
            return FakeResponse(200, {"id": "chatcmpl-1"})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "qwen")
        assert len(posts) == 1
        assert posts[0].endswith("/chat/completions")
        assert r["status"] == VerifyResult.VALID_ACTIVE.value

    def test_probe_401_invalidates_despite_models_200(self, monkeypatch):
        """关键：GET models 200（网关不校验）但 chat 401 → key 必须判无效。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            return FakeResponse(401)

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "qwen")
        assert r["status"] == VerifyResult.INVALID.value

    def test_probe_400_means_auth_passed(self, monkeypatch):
        """400/404/422：端点/模型不符但认证头已被接受（否则是 401）→ 有效。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            return FakeResponse(400, {"error": {"message": "model not found"}})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "qwen")
        assert r["status"] == VerifyResult.VALID_NO_BALANCE.value

    def test_probe_network_failure_falls_back_to_get_auth(self, monkeypatch):
        """chat 探测网络失败 → 回退到 GET 200 认证通过的事实（不误杀）。"""
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fake_post(url, **kw):
            raise OSError("connection reset")

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fake_post)
        r = verifier.verify_key(_key(), "qwen")
        assert r["status"] == VerifyResult.VALID_NO_BALANCE.value

    def test_claude_skips_chat_probe(self, monkeypatch):
        """claude chat_probe=False：GET /v1/models 200 即有效，绝不 POST /v1/messages。"""
        verifier = UnifiedKeyVerifier()
        posts = []

        def fake_get(url, **kw):
            return FakeResponse(200, {"data": []})

        def fail_post(*a, **kw):
            posts.append(a)
            pytest.fail("claude 不应触发 chat 探测 POST")

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post", fail_post)
        r = verifier.verify_key("sk-ant-" + "a" * 40, "claude")
        assert r["status"] == VerifyResult.VALID_NO_BALANCE.value
        assert posts == []


# ── Bug 修复回归: deepinfra pattern 过宽 + HTTP 400 无分类 ─────────────

class TestBugFixes:
    def test_deepinfra_pattern_rejects_other_platform_keys(self):
        """deepinfra 收紧后的 pattern 不应匹配 sk-or-v1-/sk-proj-/JWT。"""
        from providers import DEEPINFRA
        or_body = "abcdefghijklmnopqrstuvwxyz123456789012345678"
        assert not DEEPINFRA.match_key(f"sk-or-v1-{or_body}"), "sk-or-v1- 不应匹配"
        assert not DEEPINFRA.match_key(f"sk-proj-{or_body}"), "sk-proj- 不应匹配"
        assert not DEEPINFRA.match_key("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"), "JWT 不应匹配"
        # 真 deepinfra key(裸 base62 40-60 位)仍应匹配
        assert DEEPINFRA.match_key("uK9vX2mQ7zR4tW8cY1pL3nJ5fH6dS0aB2eG4iM7oP9qR1sT5uV8wX3yZ6")

    def test_verify_rejects_oversized_key_without_network(self, monkeypatch):
        """超长 key(>256)验证前即拒,不发网络请求。"""
        verifier = UnifiedKeyVerifier()
        called = []

        def fake_get(url, **kw):
            called.append(url)
            return FakeResponse(200, {"data": []})

        monkeypatch.setattr("providers.requests.get", fake_get)
        r = verifier.verify_key("sk-" + "x" * 300, "deepseek")
        assert r["status"] == VerifyResult.INVALID.value
        assert called == [], "超长 key 不应发请求"

    def test_http_400_unproven_platform_is_error(self, monkeypatch):
        """v2.5.4: 未实测 400 语义的平台(如 deepseek)400 归 ERROR——判 INVALID
        会触发 store 脱敏链永久抹掉明文(网关拦截/端点改版型 400 不可逆丢 key)。
        实测过 400=确定性无效的平台(gemini/xai)仍归 INVALID,见下方专项测试。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            return FakeResponse(400, {}, "Request Header Or Cookie Too Large")

        monkeypatch.setattr("providers.requests.get", fake_get)
        r = verifier.verify_key(_key(), "deepseek")
        assert r["status"] == VerifyResult.ERROR.value
        assert "400" in r["message"]

    def test_http_400_gemini_xai_still_invalid(self, monkeypatch):
        """活体实测 400=确定性无效的平台(gemini/xai)保持 INVALID 收敛。"""
        verifier = UnifiedKeyVerifier()
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(400, {}, "bad"))
        r = verifier.verify_key("AIza" + "aB3dE" * 7, "gemini")
        assert r["status"] == VerifyResult.INVALID.value
        r2 = verifier.verify_key("xai-" + "aB3dE7fG9" * 8, "xai")
        assert r2["status"] == VerifyResult.INVALID.value


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
        verifier = UnifiedKeyVerifier()
        monkeypatch.setattr("providers.requests.get",
                            lambda *a, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda *a, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key("sk-1e175253812a4948" "86dd8952b56dc19c", "deepseek")
        assert r["message"] != "格式预检失败"

    def test_qwen_ws_prefix_handled(self, monkeypatch):
        """qwen 2026 升级 sk-ws- 前缀 body 也是 hex,预检须先剥前缀再校验。"""
        verifier = UnifiedKeyVerifier()
        monkeypatch.setattr("providers.requests.get",
                            lambda *a, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda *a, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key("sk-ws-1e175253812a494886dd8952b56dc19c", "qwen")
        assert r["message"] != "格式预检失败"


class TestSessionReuse:
    def test_injected_session_used_for_get(self, monkeypatch):
        """注入的 Session 必须被复用,不再走裸 requests.get。"""
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

    def test_session_factory_supplies_session_per_request(self, monkeypatch):
        """候选并发下的每个请求应从工厂取独立 Session，避免共享 Session。"""
        sessions = []

        class FactorySession:
            def __init__(self):
                self.gets = []
                self.posts = []

            def get(self, url, **k):
                self.gets.append(url)
                if "balance" in url:
                    return FakeResponse(200, {
                        "balance_infos": [{"currency": "CNY", "total_balance": 1.0}]
                    })
                return FakeResponse(200, {"data": []})

            def post(self, url, **k):
                self.posts.append(url)
                return FakeResponse(200, {"id": "chatcmpl-1"})

            def close(self):
                pass

        def make_session():
            session = FactorySession()
            sessions.append(session)
            return session

        verifier = UnifiedKeyVerifier(session_factory=make_session,
                                      allow_chat_probe=True)

        def fail_requests(*args, **kwargs):
            raise AssertionError("有请求绕过 session factory")

        monkeypatch.setattr("providers.requests.get", fail_requests)
        monkeypatch.setattr("providers.requests.post", fail_requests)
        verifier.verify_key(_key(), "deepseek")
        assert len(sessions) == 3
        assert all(len(session.gets) + len(session.posts) == 1
                   for session in sessions)


class TestProviderRateLimiting:
    def test_same_provider_slots_are_serialized(self):
        """共享限速器必须给同 provider 预约互不重叠的请求窗口。"""
        clock = {"now": 100.0}

        def fake_sleep(seconds):
            clock["now"] += seconds

        limiter = ProviderRateLimiter(
            min_interval=0.5, clock=lambda: clock["now"], sleeper=fake_sleep)

        waits = [limiter.acquire("deepseek") for _ in range(3)]
        assert waits == [0.0, 0.5, 0.5]
        assert clock["now"] == 101.0
        assert limiter._next_allowed["deepseek"] == 101.5

    def test_different_providers_do_not_wait_for_each_other(self):
        clock = {"now": 100.0}

        def fake_sleep(seconds):
            clock["now"] += seconds

        limiter = ProviderRateLimiter(
            min_interval=0.5, clock=lambda: clock["now"], sleeper=fake_sleep)
        limiter.acquire("deepseek")

        assert limiter.acquire("kimi") == 0.0

    def test_verifier_acquires_before_get_and_balance(self, monkeypatch):
        """models 和 balance 都属于 provider 请求，必须在同一个限速通道内。"""
        acquired = []
        limiter = ProviderRateLimiter()
        monkeypatch.setattr(limiter, "acquire",
                            lambda provider_id: acquired.append(provider_id))
        verifier = UnifiedKeyVerifier(rate_limiter=limiter)

        def fake_get(url, **kw):
            if "balance" in url:
                return FakeResponse(200, {
                    "balance_infos": [{"currency": "CNY", "total_balance": 2.0}]
                })
            return FakeResponse(200, {"data": []})

        monkeypatch.setattr("providers.requests.get", fake_get)
        r = verifier.verify_key(_key(), "deepseek")
        assert acquired == ["deepseek", "deepseek"]
        assert r["status"] == VerifyResult.VALID_ACTIVE.value

    def test_reuses_candidate_pool_across_keys(self, monkeypatch):
        """候选并发器必须复用，不能每个模糊 key 都新建 ThreadPoolExecutor。"""
        verifier = UnifiedKeyVerifier(candidate_workers=2)
        first = verifier._candidate_pool()
        second = verifier._candidate_pool()
        assert first is second
        verifier.close()
        assert verifier._candidate_pool_handle is None


class TestChatProbeWiring:
    def test_scanner_engine_passes_chat_probe_opt_in(self, monkeypatch):
        """单次扫描入口必须能把显式 opt-in 传给验证器。"""
        import providers
        from scanner_engine import ScannerEngine

        captured = {}
        instances = []

        class FakeVerifier:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
                instances.append(self)

            def close(self):
                pass

            def verify_key(self, key, provider_id=None, context=""):
                return {"status": "valid_no_balance", "provider": "deepseek"}

        monkeypatch.setattr(providers, "UnifiedKeyVerifier", FakeVerifier)
        engine = ScannerEngine(concurrency=5, allow_chat_probe=True)
        results = engine._verify_dict({
            "sk-1e175253812a4948" "86dd8952b56dc19c": {"repos": []},
            "sk-2e175253812a4948" "86dd8952b56dc19c": {"repos": []},
        })
        assert captured["allow_chat_probe"] is True
        assert captured["candidate_workers"] == 5
        assert captured["rate_limiter"] is engine.provider_rate_limiter
        assert len(instances) == 1
        assert results[0]["valid"] is True


# ── 2026-09 第二批平台验证（状态码映射均来自假 key 实测）──────────────

class TestSecondBatchVerify:
    def test_gemini_auth_via_query_param(self, monkeypatch):
        """Gemini 用 ?key= 认证(GET /v1beta/models),200 即有效。"""
        urls = []
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: urls.append(u) or FakeResponse(200, {"models": []}))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("AIza" + "aB3dE" * 7, "gemini")
        assert urls and urls[0].endswith("?key=AIza" + "aB3dE" * 7)
        assert r["status"] == VerifyResult.VALID_NO_BALANCE.value

    def test_gemini_invalid_key_400(self, monkeypatch):
        """实测:无效 AIza key → 400 "API key not valid"。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(400, text="API key not valid."))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("AIza" + "aB3dE" * 7, "gemini")
        assert r["status"] == VerifyResult.INVALID.value

    def test_gemini_403_no_access_is_invalid(self, monkeypatch):
        """AIza key 真实但无 Gemini 权限(API 未开通/域受限)→ 403 按 invalid 收敛。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(403, text="PERMISSION_DENIED"))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("AIza" + "aB3dE" * 7, "gemini")
        assert r["status"] == VerifyResult.INVALID.value

    def test_openai_401_invalid(self, monkeypatch):
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(401, text="Incorrect API key"))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("sk-proj-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 5, "openai")
        assert r["status"] == VerifyResult.INVALID.value

    def test_xai_400_invalid(self, monkeypatch):
        """实测 xAI /v1/models 对无效 key 返回 400(非 401),同样按 invalid。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(400, text="Incorrect API key provided"))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("xai-" + "aB3dE7fG9" * 8, "xai")
        assert r["status"] == VerifyResult.INVALID.value

    def test_qianfan_403_invalid(self, monkeypatch):
        """实测千帆 /v2/models 无效 key → 403 AccessDenied,按 invalid 收敛。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(403, text="Access denied"))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("bce-v3/ALTAK-aBcDeFgHiJkLmNoP/4f8a1c9e27b35d6f0a91", "qianfan")
        assert r["status"] == VerifyResult.INVALID.value

    def test_modelscope_public_models_errors_without_probe(self, monkeypatch):
        """/models 公开(假 key 也 200)→ 关闭兜底探测时必须 ERROR,绝不判 valid。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": [{"id": "x"}]}))
        verifier = UnifiedKeyVerifier(probe_unclear=False)
        r = verifier.verify_key("ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c", "modelscope")
        assert r["status"] == VerifyResult.ERROR.value

    def test_modelscope_unclear_probe_runs_by_default(self, monkeypatch):
        """v2.4.1: probe_unclear 默认开——models 公开平台自动兜底一次最小探测。"""
        posts = []
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: posts.append(u) or FakeResponse(401, text="auth failed"))
        verifier = UnifiedKeyVerifier()  # probe_unclear 默认 True,allow_chat_probe 默认 False
        r = verifier.verify_key("ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c", "modelscope")
        assert posts, "默认应兜底探测 models 公开平台"
        assert r["status"] == VerifyResult.INVALID.value

    def test_unclear_probe_never_applies_to_normal_platforms(self, monkeypatch):
        """probe_unclear 只覆盖 models 公开平台——正常平台默认仍零 POST。"""
        posts = []
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: posts.append(u) or FakeResponse(200, {"id": "x"}))
        verifier = UnifiedKeyVerifier()  # deepseek /models 严格鉴权,GET 200 已可判定
        verifier.verify_key("sk-" + "1e175253812a494886dd8952b56dc19c", "deepseek")
        assert posts == [], "probe_unclear 不得扩散到 GET 可判定的平台"

    def test_modelscope_probe_decides_when_enabled(self, monkeypatch):
        posts = []
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: posts.append(u) or FakeResponse(200, {"id": "x"}))
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)
        r = verifier.verify_key("ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c", "modelscope")
        assert posts, "显式开启后应发 chat 探测"
        assert r["status"] == VerifyResult.VALID_ACTIVE.value

    def test_nvidia_probe_403_maps_reverifiable_error(self, monkeypatch):
        """nvidia 403 按 body 二次区分(v2.5.1):
        - "Authorization failed"/"invalid api key" = 确定性鉴权失败 → INVALID
          (活体实测假 key 即此形态;旧策略全归 ERROR 导致 219 条死循环重验)
        - 其他 403(真 WAF/区域拦截,不可区分) → ERROR 可重验,不永久误杀。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": []}))
        # 情形1: 确定性鉴权失败 body → INVALID
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: FakeResponse(403, text="Authorization failed"))
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)
        r = verifier.verify_key("nvapi-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxY" * 2, "nvidia")
        assert r["status"] == VerifyResult.INVALID.value
        # 情形2: 疑似 WAF 的模糊 403 → ERROR 可重验
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: FakeResponse(403, text="Request blocked by WAF"))
        r2 = verifier.verify_key("nvapi-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxY" * 2, "nvidia")
        assert r2["status"] == VerifyResult.ERROR.value

    def test_probe_failure_on_unauth_models_never_valid(self, monkeypatch):
        """models 公开平台:探测网络失败 → 不得"退回 GET 认证"判 valid。"""
        import requests as _rq

        def post_boom(u, **k):
            raise _rq.Timeout("network down")

        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post", post_boom)
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)
        r = verifier.verify_key("ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c", "modelscope")
        assert r["status"] == VerifyResult.ERROR.value


# ── v2.4.1 鉴权审计:OpenRouter /auth/key + Jina 公开目录 + probe_unclear ──

class TestAuthAudit:
    def test_openrouter_authkey_401_invalid(self, monkeypatch):
        """OpenRouter 验证端点已改 /auth/key(严格鉴权):假 key 401 → invalid。"""
        urls = []
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: urls.append(u) or FakeResponse(401, text="User not found."))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("sk-or-v1-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz123456", "openrouter")
        assert any("/auth/key" in u for u in urls), "必须打 /auth/key 而非公开的 /models"
        assert r["status"] == VerifyResult.INVALID.value

    def test_openrouter_balance_from_authkey(self, monkeypatch):
        """/auth/key 返回 usage/limit → 剩余 = limit - usage(USD)。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(
                                200, {"data": {"label": "k", "usage": 3.0, "limit": 10.0}}))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("sk-or-v1-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz123456", "openrouter")
        assert r["status"] == VerifyResult.VALID_ACTIVE.value
        assert r["balance"] == 7.0

    def test_openrouter_unlimited_key_no_balance(self, monkeypatch):
        """limit=null(无上限)→ 无法折算剩余,判 valid_no_balance。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": {"usage": 3.0, "limit": None}}))
        verifier = UnifiedKeyVerifier()
        r = verifier.verify_key("sk-or-v1-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz123456", "openrouter")
        assert r["status"] == VerifyResult.VALID_NO_BALANCE.value

    def test_jina_public_models_stays_error_even_with_global_probe(self, monkeypatch):
        """Jina /models 公开且无 chat 端点(chat_probe=False):任何模式下都不得判 valid。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": [{"id": "jina-embeddings-v3"}]}))
        verifier = UnifiedKeyVerifier(allow_chat_probe=True)
        r = verifier.verify_key("jina_" + "aB3dE7fG9h" * 4, "jina")
        assert r["status"] == VerifyResult.ERROR.value

    def test_yi_disabled_after_service_shutdown(self):
        """零一万物 API 停运(410):退出 ACTIVE 轮询,保留 PROVIDER_MAP。"""
        from providers import ACTIVE_PROVIDERS, PROVIDER_MAP, YI
        assert YI.enabled is False
        assert "yi" in PROVIDER_MAP
        assert all(p.id != "yi" for p in ACTIVE_PROVIDERS)


# ── v2.4.4: 智谱真余额 + GLM Coding Plan 周额度(消灭假余额 2000) ──────

class TestZhipuRealBalance:
    def test_zhipu_sums_balance_infos(self, monkeypatch):
        """按量余额 = balance_infos[].balance 之和(资源包+赠送+充值均可消费)。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "users/balance" in url:
                return FakeResponse(200, {"balance_infos": [
                    {"balance": "12.5", "source": "resource_pack"},
                    {"balance": "3.5", "source": "recharge"},
                ]})
            return FakeResponse(405, text="method not allowed")

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key("78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp",
                                "zhipu")
        assert r["balance"] == 16.0

    def test_zhipu_coding_weekly_remaining_percent(self, monkeypatch):
        """Coding Plan: 取 nextResetTime 最晚(周窗)的剩余百分比作"余额"。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "monitor/usage/quota/limit" in url:
                return FakeResponse(200, {"data": {"limits": [
                    {"type": "TOKENS_LIMIT", "percentage": 40,
                     "nextResetTime": 1758700000000},          # 5h 窗:已用40%
                    {"type": "CREDIT_LIMIT", "usage": 1000, "remaining": 250,
                     "nextResetTime": 1759130000000},          # 周窗:剩 25%
                ]}})
            return FakeResponse(200, {"data": []})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key("78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp",
                                "zhipu_coding")
        assert r["provider"] == "zhipu_coding"
        assert r["balance"] == 25.0, "应取周窗剩余 25%,而非 5h 窗或资源包总额"

    def test_zhipu_coding_no_plan_returns_none_balance(self, monkeypatch):
        """按量 key 打到 monitor 端点无套餐窗口 → None(不折算假值)。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "monitor" in url:
                return FakeResponse(200, {"data": {"limits": []}})
            return FakeResponse(200, {"data": []})

        monkeypatch.setattr("providers.requests.get", fake_get)
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: FakeResponse(200, {"id": "x"}))
        r = verifier.verify_key("78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp",
                                "zhipu_coding")
        assert r["balance"] is None

    def test_monitor_auth_failure_body_is_not_balance(self, monkeypatch):
        """monitor 端点鉴权失败返回 HTTP 200+body code:1000——解析必须得 None/0,
        不得把错误体当余额(200 假阳性防线)。"""
        verifier = UnifiedKeyVerifier()

        def fake_get(url, **kw):
            if "monitor" in url:
                return FakeResponse(200, {"code": 1000, "msg": "身份验证失败。",
                                          "success": False})
            return FakeResponse(401)

        monkeypatch.setattr("providers.requests.get", fake_get)
        r = verifier._check_balance_sync(
            "78a1b2c3d4e5f60718293a4b5c6d7e8f.AbcDefGhIjKlMnOp",
            __import__("providers").PROVIDER_MAP["zhipu_coding"])
        assert r is None


# ══════════ v2.5.4 审查修复回归 ══════════

class TestV254CancelledCandidates:
    def test_cancelled_candidates_return_error_not_invalid(self, monkeypatch):
        """候选池关停取消的候选 → ERROR(可重验),绝不落 INVALID 兜底——
        否则触发脱敏链永久抹明文;CancelledError 也不得杀死 worker 线程。"""
        import concurrent.futures

        from providers import UnifiedKeyVerifier, VerifyResult

        def cancelled(self, key, provider):
            raise concurrent.futures.CancelledError()

        monkeypatch.setattr(UnifiedKeyVerifier, "_verify_with_provider", cancelled)
        v = UnifiedKeyVerifier()
        r = v.verify_key("sk-" + "z" * 40)  # 通用 sk- key → 多候选并行 → 全取消
        assert r["status"] == VerifyResult.ERROR.value


class TestV254BalanceNotFabricated:
    def test_deepseek_missing_infos_returns_none(self, monkeypatch):
        import providers as P
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {}))
        v = UnifiedKeyVerifier()
        got = v._check_balance_sync("sk-" + "1" * 32, P.PROVIDER_MAP["deepseek"])
        assert got is None, "字段缺失 ≠ 0 余额确证,不得把真有钱的 key 降级 VALID_ZERO"

    def test_kimi_missing_fields_returns_none(self, monkeypatch):
        import providers as P
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": {}}))
        v = UnifiedKeyVerifier()
        got = v._check_balance_sync("sk-" + "2" * 32, P.PROVIDER_MAP["kimi"])
        assert got is None

    def test_siliconflow_balance_parsed(self, monkeypatch):
        import providers as P
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": {"balance": "12.50"}}))
        v = UnifiedKeyVerifier()
        got = v._check_balance_sync("sk-" + "3" * 32, P.PROVIDER_MAP["siliconflow"])
        assert got == 12.50, "siliconflow 配了余额端点必须有解析分支(此前 GET 白发)"

    def test_minimax_cp_remains_parsed(self, monkeypatch):
        import providers as P
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": {"remains": 55}}))
        v = UnifiedKeyVerifier()
        got = v._check_balance_sync("sk-" + "4" * 32, P.PROVIDER_MAP["minimax_cp"])
        assert got == 55


class TestV254UnauthenticatedProbe4xx:
    def test_modelscope_probe_404_returns_error(self, monkeypatch):
        """/models 公开平台的探测 4xx:鉴权时序未经证实 → ERROR 重验,
        不得判 VALID_NO_BALANCE(否则格式正确的假 key 批量假 valid)。"""
        import providers as P
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(200, {"data": []}))
        monkeypatch.setattr("providers.requests.post",
                            lambda u, **k: FakeResponse(404, text="no such model"))
        v = UnifiedKeyVerifier()
        r = v.verify_key("ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c", "modelscope")
        assert r["status"] == P.VerifyResult.ERROR.value

    def test_xai_400_still_invalid(self, monkeypatch):
        """实测 400=确定性无效的平台(xai)保持 INVALID(与 gemini 同策略)。"""
        monkeypatch.setattr("providers.requests.get",
                            lambda u, **k: FakeResponse(400, text="Incorrect API key provided"))
        v = UnifiedKeyVerifier()
        r = v.verify_key("xai-" + "aB3dE7fG9" * 8, "xai")
        assert r["status"] == VerifyResult.INVALID.value


class TestV254MatcherSpecificity:
    def test_coding_context_prefers_zhipu_coding(self):
        """同分时,更长(更具体)的上下文命中优先——coding 查询扫到的智谱
        key 应路由 zhipu_coding(周额度口径),而非被按量端点抢先。"""
        from providers import _get_matcher, reset_matcher_singleton
        reset_matcher_singleton()
        try:
            m = _get_matcher()
            key = "1e175253812a494886dd8952b56dc19c.0123456789abcdef"
            cands = m.identify_provider(
                key, "repo: x/y file: c.py open.bigmodel.cn/api/coding")
            assert cands, "应识别出候选"
            assert cands[0][0] == "zhipu_coding", f"实际排序: {cands[:3]}"
        finally:
            reset_matcher_singleton()
