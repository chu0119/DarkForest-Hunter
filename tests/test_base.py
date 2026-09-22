"""scanners/base.py 的 key 提取与过滤逻辑测试。"""


from scanners.base import (
    BAD_PATTERNS,
    KEY_PATTERN,
    LOW_VALUE_PATH_KEYWORDS,
    TARGET_FILE_EXTS,
    TARGET_FILENAMES,
    extract_keys,
    is_bad_key,
)


def _gen_key(length: int = 32, char: str = "a") -> str:
    """生成指定长度的测试 key（char 重复填充，避免 is_bad_key 低熵拦截）。"""
    return "sk-" + char * length


# ── KEY_PATTERN ─────────────────────────────────────────────────────

class TestKeyPattern:
    def test_matches_plain_sk(self):
        key = _gen_key(32)
        assert KEY_PATTERN.fullmatch(key)

    def test_matches_proj_prefix(self):
        # v2.5.1 语义变更: 真 OpenAI sk-proj key body 142-222 位(总长 150-230)。
        # 短 body(如 40)的 sk-proj 是假 key——DB 实证 269 条 unknown 噪声
        # 正是从旧通用分支的可选组溜进来的,负向断言后必须提取不到。
        real = "sk-proj-" + "a" * 100  # body 100 ≥ 70,真实形态
        assert KEY_PATTERN.fullmatch(real)
        fake = "sk-proj-" + "a" * 40    # body 40 < 70,假 key 不提取
        assert KEY_PATTERN.search(fake) is None

    def test_length_bounds(self):
        # v2.7: body {20,95} for Claude (sk-ant-api03-... can be 95+ chars)
        assert not KEY_PATTERN.fullmatch(_gen_key(19))   # body=19, total=22 < 23
        assert KEY_PATTERN.fullmatch(_gen_key(20))        # body=20, total=23 = min
        assert KEY_PATTERN.fullmatch(_gen_key(95))        # body=95, total=98 = max (Claude)
        assert not KEY_PATTERN.fullmatch(_gen_key(96))   # body=96, total=99 > 98

    def test_allows_hyphens_and_underscores(self):
        # New pattern allows - and _ (needed for sk-ant-, sk-or-v1-, etc.)
        key = "sk-" + "a" * 20 + "-" + "b" * 11
        assert KEY_PATTERN.fullmatch(key)

    def test_case_insensitive_alnum_ok(self):
        key = "sk-" + "A1B2" * 8  # 32 位大写+数字
        assert KEY_PATTERN.fullmatch(key)


# ── is_bad_key ──────────────────────────────────────────────────────

class TestIsBadKey:
    def test_placeholder_words(self):
        for word in ["your", "xxx", "example", "placeholder", "replace", "here",
                     "fake", "dummy", "changeme", "insert"]:
            assert is_bad_key("sk-" + word + "a" * 30), f"{word} 应被判定为坏 key"

    def test_repeated_patterns(self):
        assert is_bad_key("sk-xxxx" + "a" * 28)
        assert is_bad_key("sk-0000" + "a" * 28)
        assert is_bad_key("sk-aaaa" + "a" * 28)

    def test_all_digits_rejected(self):
        assert is_bad_key("sk-" + "1" * 32)

    def test_low_entropy_rejected(self):
        # 少于 4 种不同字符
        assert is_bad_key("sk-" + "abab" * 8)
        assert is_bad_key("sk-" + "aaaa" * 8)

    def test_high_entropy_accepted(self):
        key = "sk-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz"  # 32 位，多种字符
        assert not is_bad_key(key)

    def test_extra_bad_patterns(self):
        # extra_bad 中的子串出现在 key 中 → 拦截
        key = "sk-mypattern" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWx"  # 42 位，含 mypattern
        assert is_bad_key(key, extra_bad=["mypattern"])
        # 不相关的 extra_bad 不影响高熵 key
        assert not is_bad_key("sk-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz",
                              extra_bad=["otherpattern"])

    # ── Task 2: 前缀感知长度上限(救 eyJ JWT / Claude 长 key) ──────────────

    def test_eyj_jwt_passes_under_prefix_aware_limit(self):
        # MiniMax JWT: eyJ + 三段 base64url,最小总长 83,真实 key 常见 150-220
        jwt = "eyJ" + "aB3dE7fG9hJ1kL2m" * 6 + "." + "xY7zK9" * 12 + "." + "pQ1rS8" * 10
        assert len(jwt) > 80
        assert not is_bad_key(jwt), f"eyJ JWT 不应被长度>80 误杀(len={len(jwt)})"

    def test_eyj_too_long_still_rejected(self):
        jwt = "eyJ" + "a" * 260
        assert is_bad_key(jwt)

    def test_claude_long_key_passes(self):
        # Claude sk-ant-api03- 真实 key 总长 ~104-120
        claude = "sk-ant-api03-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 3
        assert len(claude) > 80
        assert not is_bad_key(claude), f"Claude 长 key 不应被长度>80 误杀(len={len(claude)})"

    def test_generic_sk_over_80_still_rejected(self):
        # 普通 sk- 家族仍限 80(fresh-repo env 长内容误匹配仍需拦截)
        assert is_bad_key("sk-" + "a" * 100)


# ── extract_keys ────────────────────────────────────────────────────

class TestExtractKeys:
    def test_extracts_valid_keys_only(self):
        text = (
            "sk-aaaaaaaaaaaaaaaa" "aaaaaaaaaaaaaaaa"  # 低熵，应被过滤
            " sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"  # 有效
        )
        keys = extract_keys(text)
        assert len(keys) == 1
        assert keys[0] == "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"

    def test_empty_text(self):
        assert extract_keys("") == []

    def test_no_false_positives(self):
        assert extract_keys("no keys here at all") == []


# ── 常量完整性 ──────────────────────────────────────────────────────

class TestConstants:
    def test_target_exts_include_key_ones(self):
        for ext in [".env", ".yml", ".json", ".py", ".java", ".ipynb"]:
            assert ext in TARGET_FILE_EXTS

    def test_target_filenames_include_env_files(self):
        for name in [".env", "docker-compose.yml", "application.properties"]:
            assert name in TARGET_FILENAMES

    def test_bad_patterns_all_substring_matchable(self):
        # 所有 BAD_PATTERNS 都能被 is_bad_key 命中（无空串/异常模式）
        for p in BAD_PATTERNS:
            assert p
            assert is_bad_key("sk-" + p + "a" * 30)

    def test_low_value_path_keywords_nonempty(self):
        assert LOW_VALUE_PATH_KEYWORDS
        assert any("/test/" in kw for kw in LOW_VALUE_PATH_KEYWORDS)


# ── 流式回调（扫到即提交）──────────────────────────────────────────

class TestOnKeyStreaming:
    """回归：_extract_keys_from_text_matches 的 on_key 回调——每提取到 key 立即触发，
    不必等整条查询结束才批量提交（watch 端实时提交/计数）。"""

    def _make_engine(self):
        from scanner_engine import ScannerEngine
        eng = ScannerEngine.__new__(ScannerEngine)  # 跳过 __init__（无网络）
        eng.exclude_repos = []
        eng.extra_bad_patterns = []
        eng.key_pattern = __import__('re').compile(r"sk-[a-zA-Z0-9]{32,}")
        eng.log = lambda *a, **k: None
        eng.concurrency = 4
        return eng

    def test_on_key_fires_per_new_key(self):
        eng = self._make_engine()
        items = [{
            "repository": {"full_name": "acme/x"},
            "path": "config.env",
            "html_url": "https://gh/acme/x/blob/main/config.env",
            "text_matches": [{"fragment": "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"}],
        }]
        fired = []
        keys = eng._extract_keys_from_text_matches(items, on_key=fired.append)
        assert len(fired) == 1, "每个新 key 都应触发一次回调"
        assert fired[0]["key"] == "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"
        assert keys["sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"]["repos"][0]["repo"] == "acme/x"

    def test_on_key_same_key_dedup(self):
        # 同一 key 出现在多个 repo → 只回调一次（首次）
        eng = self._make_engine()
        items = [
            {"repository": {"full_name": "acme/x"}, "path": "a.env",
             "html_url": "u1", "text_matches": [{"fragment": "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"}]},
            {"repository": {"full_name": "acme/y"}, "path": "b.env",
             "html_url": "u2", "text_matches": [{"fragment": "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"}]},
        ]
        fired = []
        eng._extract_keys_from_text_matches(items, on_key=fired.append)
        assert len(fired) == 1

    def test_on_key_exception_does_not_break(self):
        # 回调抛异常不应中断提取（watch 端 submit 可能失败）
        eng = self._make_engine()
        items = [{
            "repository": {"full_name": "acme/x"}, "path": "a.env", "html_url": "u",
            "text_matches": [{"fragment": "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p"}],
        }]
        def boom(_):
            raise RuntimeError("callback failure")
        keys = eng._extract_keys_from_text_matches(items, on_key=boom)
        assert "sk-1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p" in keys  # 提取不受影响


# ── 2026-09 第二批平台的 KEY_PATTERN / is_bad_key ────────────────────

class TestSecondBatchKeyPattern:
    def test_openai_long_key_extracted_in_full(self):
        """sk-proj- 长 key 必须完整提取(不得被通用 sk- 分支的 {20,95} 截断)。

        回归风险:通用分支 sk-(?:proj-)?[a-zA-Z0-9_-]{20,95} 会把 160 位 body
        截到 95 位——残 key 永远 401,泄露发现直接归零。
        """
        key = "sk-proj-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 5  # 总长 168
        m = KEY_PATTERN.search(f'OPENAI_API_KEY="{key}"')
        assert m is not None
        assert m.group() == key, f"提取被截断: len={len(m.group())}"

    def test_openai_three_prefixes_extracted(self):
        for pfx in ("sk-proj-", "sk-svcacct-", "sk-admin-"):
            key = pfx + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 3
            assert KEY_PATTERN.search(key), f"{pfx} 应能提取"

    def test_is_bad_key_allows_real_length_openai(self):
        assert not is_bad_key("sk-proj-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 5)
        assert not is_bad_key("sk-svcacct-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 3)

    def test_is_bad_key_still_rejects_overlong_openai(self):
        assert is_bad_key("sk-proj-" + "a" * 260)

    def test_openai_over_208_not_truncated(self):
        """v2.4.9: 总长 >208 的 sk-proj- 真 key 不得被 {70,200} 封顶截成残 key。

        旧正则 {70,200} 贪婪但封顶,匹配成功即返回不要求吃完整串 →
        230 位真 key 被截成 208 位残 key → 验证 401 静默丢失。
        """
        key = "sk-proj-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 7  # 总长 232
        m = KEY_PATTERN.search(f'OPENAI_API_KEY="{key}"')
        assert m is not None
        assert m.group() == key, f"截断: len={len(m.group())} < {len(key)}"

    def test_claude_ant_api03_over_108_not_truncated(self):
        """v2.4.9: sk-ant-api03- 总长 >108 的真 key 不得被通用分支截断。

        真实 Claude key 总长 104-120,旧通用分支 {20,95} 会在 108 处截断约一半。"""
        key = "sk-ant-api03-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 4  # 总长 128
        m = KEY_PATTERN.search(f'ANTHROPIC_API_KEY={key}')
        assert m is not None
        assert m.group() == key, f"截断: len={len(m.group())} < {len(key)}"
        assert not is_bad_key(key)

    def test_openrouter_over_80_allowed(self):
        """v2.4.9: OpenRouter sk-or-v1- 总长 81-82 的真 key 不得被 >80 通用门限杀掉。

        pattern 允许 body 到 73 → 总长 82,旧 80 门限把全部真 key 杀光。"""
        key = "sk-or-v1-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYzAbCdEf01"  # 总长 82
        m = KEY_PATTERN.search(f'OPENROUTER_API_KEY="{key}"')
        assert m is not None
        assert not is_bad_key(key), "82 位 OpenRouter key 被 is_bad_key 误杀"

    def test_short_fake_proprietary_keys_not_extracted(self):
        """v2.5.1: 短假专有前缀 key 必须提取不到(DB 实证 269 条 unknown 噪声)。

        此前通用分支的可选组不启用时 proj-/ant-api03- 全当 body 蒙混过关;
        负向断言后专有前缀必须走各自分支的长度约束。"""
        for k in ("sk-proj-MLNkacQOabcdefgh",      # len 24
                  "sk-proj-a1b2c3d4e5f6",          # len 21
                  "sk-ant-api03-yyy",              # len 19
                  "sk-or-v1-7211dc1",              # len 17
                  "sk-proj-" + "aB3" * 16):        # len 56(DB 实证)
            assert KEY_PATTERN.search(k) is None, f"短假 key 不应提取: {k[:24]}"

    def test_real_proprietary_keys_still_extracted(self):
        """负向断言不得误伤真 key——各专有前缀完整提取。"""
        cases = [
            "sk-proj-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 5,
            "sk-ant-api03-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 4,
            "sk-or-v1-" + "aB3dE7fG9" * 7,
            "sk-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 2,   # 通用 deepseek 形态
            "sk-kimi-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz",  # kimi 前缀仍走通用可选组
        ]
        for k in cases:
            m = KEY_PATTERN.search(k)
            assert m is not None and m.group() == k, f"真 key 提取失败: {k[:20]}"

    def test_css_and_code_literal_junk_blocked(self):
        """v2.5.1: DB 实证的 CSS/JS minified 变量与代码字面量必须被 BAD_PATTERNS 拦。"""
        for k in ("sk-lineHei3-lg", "sk-fontSizerHeader",
                  "sk-colors-DEFAULT", "sk-None-6b75NJ",
                  "sk-sensitiveKEY123", "sk-internalCache9x"):
            assert is_bad_key(k), f"垃圾未拦: {k}"

    def test_generic_sk_over_80_still_rejected(self):
        # 接入 OpenAI 不放宽普通 sk- 家族的长度护栏
        assert is_bad_key("sk-" + "a" * 100)

    def test_gemini_aiza_39_extracted(self):
        key = "AIza" + "aB3dE" * 7  # 总长恰 39
        assert KEY_PATTERN.search(f'GEMINI_API_KEY="{key}"')
        assert not is_bad_key(key)

    def test_gemini_2026_auth_key_extracted(self):
        key = "AQ.Ab" + "aB3dE7fG9hJ1kL2m" * 6
        assert KEY_PATTERN.search(key)
        assert not is_bad_key(key)

    def test_xai_and_nvidia_extracted(self):
        assert KEY_PATTERN.search("XAI_API_KEY=xai-" + "aB3dE7fG9" * 8)
        assert KEY_PATTERN.search("NVIDIA_API_KEY=nvapi-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxY" * 2)

    def test_modelscope_uuid_extracted(self):
        key = "ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c"
        assert KEY_PATTERN.search(f'MODELSCOPE_TOKEN="{key}"')
        assert not is_bad_key(key)

    def test_qianfan_bce_v3_extracted(self):
        key = "bce-v3/ALTAK-aBcDeFgHiJkLmNoP/4f8a1c9e27b35d6f0a91"
        m = KEY_PATTERN.search(f'QIANFAN_API_KEY="{key}"')
        assert m is not None and m.group() == key
        assert not is_bad_key(key)


# ── v2.4.3: 占位符描述短语 slug 过滤(DB 实证 unknown 1364 全是垃圾) ──

class TestPlaceholderSlugFilter:
    def test_placeholder_slugs_rejected(self):
        # 来自真实 DB unknown 样本的形态
        assert is_bad_key("sk-change_me_get_from_www_deepseek_com")
        assert is_bad_key("sk-or-v1-COLOQUE_SUA_CHAVE_AQUI")  # 大写形态走 BAD_PATTERNS 小写匹配
        assert is_bad_key("sk-deepseek-test-not-real-extra")
        assert is_bad_key("sk-test-not-real")
        assert is_bad_key("sk-master-key-local-litellm")

    def test_real_keys_not_caught_by_slug_filter(self):
        # 真实 key:混合大小写/纯 hex,slug 启发式不误伤
        assert not is_bad_key("sk-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 2)
        assert not is_bad_key("ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c")  # hex 段非纯字母
        assert not is_bad_key("sk-ant-api03-" + "aB3dE7fG" * 12)          # 段含大写

    def test_two_word_slug_still_allowed(self):
        # 2 段词不构成描述短语(避免过杀),3 段起才拦
        assert not is_bad_key("sk-abc123-def456")


# ── v2.4.3: 长度门限放行新平台长 key(KEY_PATTERN 放行但 >80 门限曾静默丢弃) ──

class TestSecondBatchLengthGate:
    def test_long_xai_nvapi_bce_pass_through_extract(self):
        """85 位 xai / 90 位 nvapi / 86 位千帆 key:提取正则放行的必须真正被提取。"""
        xai = "xai-" + "aB3dE7fG9h" * 8 + "x1"        # 94 位(正则上限)
        nvapi = "nvapi-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz1234" * 1 + "aB3dE7fG9hJ1kL2"  # ~96
        nvapi = "nvapi-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz1aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz"[:90]
        bce = "bce-v3/ALTAK-" + "aB3dE7fG9hJ1kL2mN4pQ" + "/" + "4f8a1c9e27b35d6f0a91b2c3"  # ~86
        for key in (xai, nvapi, bce):
            got = extract_keys(f'KEY="{key}"')
            assert key in got, f"长 key 被长度门限静默丢弃: {key[:16]}... len={len(key)}"

    def test_overlong_new_platform_still_rejected(self):
        assert is_bad_key("xai-" + "a" * 200)
        assert is_bad_key("bce-v3/ALTAK-" + "a" * 200)


# ── v2.5.4: oat01 提取链 ──

class TestV254Oat01Extraction:
    def test_long_oat01_key_extracted_intact(self):
        """Claude CLI setup token:识别侧 v2.5.2 已收,提取链曾因通用分支
        截断 + 80 长度门槛断裂。"""
        body = "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 3  # 96 chars
        key = "sk-ant-oat01-" + body
        assert not is_bad_key(key)
        assert key in extract_keys(f'token = "{key}"')

    def test_short_oat01_not_matched_by_generic_branch(self):
        """短于专属分支下限的 oat01 残串不得经通用分支溜进结果。"""
        assert extract_keys('k = "sk-ant-oat01-short"') == []
