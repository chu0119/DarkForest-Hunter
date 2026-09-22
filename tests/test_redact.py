"""redact_results 的脱敏逻辑测试。关键性质：输入里的完整凭据，
在输出里绝不能原样出现（fail-safe）。"""
from __future__ import annotations

import json

from scripts.redact_results import (
    looks_like_credential,
    mask,
    redact_data,
    redact_file,
)

KEY = "sk-" + "aBcD9fGh2" * 4   # 真实形态的 key
JWT = "eyJ" + "aBcD9fGh2" * 12  # MiniMax JWT 形态


class TestLooksLikeCredential:
    def test_sk_key(self):
        assert looks_like_credential(KEY)

    def test_sk_ant_key(self):
        assert looks_like_credential("sk-ant-" + "a" * 40)

    def test_jwt(self):
        assert looks_like_credential(JWT)

    def test_not_a_credential(self):
        for s in ["deepseek", "balance_usd", "", "sk-short", "https://api.x.com", "1.5"]:
            assert not looks_like_credential(s), s


class TestMask:
    def test_mask_is_not_original(self):
        assert mask(KEY) != KEY

    def test_mask_keeps_prefix_and_suffix(self):
        m = mask(KEY)
        assert m.startswith(KEY[:8])
        assert m.endswith(KEY[-4:])

    def test_short_string_redacted(self):
        assert mask("abc") == "[REDACTED]"


class TestRedactData:
    def test_key_value_masked(self):
        out = redact_data({"key": KEY, "key_preview": "x", "balance": 1.5})
        assert out["key"] != KEY
        assert out["balance"] == 1.5  # 非凭据字段保留

    def test_credential_as_member_name_masked(self):
        """watch_state.json 的 {"keys": {"sk-xxx": {...}}} 形态。"""
        out = redact_data({"keys": {KEY: {"balance": 1.0}}})
        assert KEY not in out["keys"]
        assert any(k != KEY for k in out["keys"])
        # 内部数据保留
        inner = out["keys"]
        assert list(inner.values())[0]["balance"] == 1.0

    def test_nested_list_of_results(self):
        out = redact_data({"results": [{"key": KEY, "repos": [{"url": "u"}]}]})
        assert out["results"][0]["key"] != KEY
        assert out["results"][0]["repos"][0]["url"] == "u"

    def test_jwt_masked(self):
        out = redact_data({"key": JWT})
        assert out["key"] != JWT

    def test_no_credential_survives(self):
        """fail-safe 核心性质：输入里的完整凭据在序列化输出里不出现。"""
        data = {"results": [{"key": KEY}, {"key": JWT}], "keys": {KEY: {"v": 1}}}
        serialized = json.dumps(redact_data(data), ensure_ascii=False)
        assert KEY not in serialized
        assert JWT not in serialized


class TestRedactFile:
    def test_round_trip_masks_key(self, tmp_path):
        f = tmp_path / "deepseek_keys_result.json"
        f.write_text(json.dumps([{"key": KEY, "balance": 2.0}]), encoding="utf-8")
        out = redact_file(f)
        assert out[0]["key"] != KEY
        assert out[0]["balance"] == 2.0

    def test_invalid_json_returns_none(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not json", encoding="utf-8")
        assert redact_file(f) is None
