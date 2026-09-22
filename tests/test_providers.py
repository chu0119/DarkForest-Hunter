"""providers.py 的查询生成、key 识别、结果处理测试。"""

from providers import (
    ALL_PROVIDERS,
    CLAUDE,
    DEEPSEEK,
    MINIMAX,
    PROVIDER_MAP,
    QueryGenerator,
    UnifiedKeyMatcher,
    dedup_results,
    is_bad_key_multi_provider,
)

# ── Provider 配置完整性 ─────────────────────────────────────────────

class TestProviderConfig:
    def test_all_providers_registered(self):
        # 12 国内主平台 + 5 coding plan + 10 new(2025-26) + 8 第二批(2026-09) = 35
        assert len(ALL_PROVIDERS) == 35
        assert len(PROVIDER_MAP) == 35

    def test_provider_ids_unique(self):
        ids = [p.id for p in ALL_PROVIDERS]
        assert len(ids) == len(set(ids))

    def test_every_provider_has_key_pattern(self):
        for p in ALL_PROVIDERS:
            assert p.key_patterns, f"{p.id} 缺少 key_patterns"

    def test_minimax_balance_consistency(self):
        # MiniMax 无公开余额 API：has_balance_check 必须为 False
        assert MINIMAX.has_balance_check is False

    def test_balance_check_requires_endpoint(self):
        for p in ALL_PROVIDERS:
            if p.has_balance_check:
                assert p.balance_endpoint, f"{p.id} has_balance_check=True 但缺少 balance_endpoint"


# ── QueryGenerator ──────────────────────────────────────────────────

class TestQueryGenerator:
    def test_generate_for_provider_dedup_and_limit(self):
        q = QueryGenerator.generate_for_provider(DEEPSEEK, 30)
        assert len(q) == len(set(q))
        assert len(q) <= 30

    def test_includes_context_queries(self):
        q = QueryGenerator.generate_for_provider(DEEPSEEK, 30)
        for cq in DEEPSEEK.key_context_queries:
            assert cq in q

    def test_claude_skips_generic_sk_patterns(self):
        # Claude 是 sk-ant- 格式，其 key_context_queries 基于 sk-ant- 而非裸 sk-
        assert any("sk-ant-" in x for x in CLAUDE.key_context_queries)

    def test_rolling_queries_for_claude_use_sk_ant(self):
        q = QueryGenerator.generate_rolling_for_provider(CLAUDE, 4)
        assert q and all("sk-ant-" in x for x in q)

    def test_rolling_queries_include_env(self):
        q = QueryGenerator.generate_rolling_for_provider(DEEPSEEK, 15)
        joined = " ".join(q)
        assert "filename:env" in joined
        assert "pushed:" not in joined  # Code Search 不支持日期过滤

    def test_generate_all_sorted_by_priority(self):
        q = QueryGenerator.generate_all(10)
        priorities = [x["priority"] for x in q]
        assert priorities == sorted(priorities, reverse=True)


# ── UnifiedKeyMatcher ───────────────────────────────────────────────

class TestKeyIdentification:
    def setup_method(self):
        self.matcher = UnifiedKeyMatcher()

    def test_claude_key_format(self):
        # Real Claude keys are sk-ant-api03-... (with hyphens after api03-)
        key = "sk-ant-api03-" + "a" * 60 + "b" * 25
        candidates = self.matcher.identify_provider(key)
        assert candidates[0][0] == "claude"

    def test_minimax_jwt_format(self):
        candidates = self.matcher.identify_provider("eyJ" + "a" * 120)
        assert candidates[0][0] == "minimax"

    def test_generic_sk_key_lists_many(self):
        candidates = self.matcher.identify_provider("sk-" + "aBcD" * 9)
        # 所有 sk- 通用格式平台都应进入候选
        assert len(candidates) >= 5

    def test_context_raises_deepseek_confidence(self):
        context = "api.deepseek.com sk- Authorization Bearer"
        candidates = self.matcher.identify_provider("sk-" + "aBcD" * 9, context)
        assert candidates[0][0] == "deepseek"
        assert candidates[0][1] > 50  # 格式 50 分 + 上下文加分

    def test_scores_sorted_descending(self):
        candidates = self.matcher.identify_provider("sk-" + "aBcD" * 9, "open.bigmodel.cn GLM_API_KEY sk-")
        scores = [c[1] for c in candidates]
        assert scores == sorted(scores, reverse=True)

    def test_single_word_context_not_accumulated(self):
        """回归测试：只命中高频词 sk- 时，各平台得分应一致(50+10)，
        不因 query 条数多而堆分。"""
        candidates = self.matcher.identify_provider("sk-" + "aBcD" * 9, "sk-")
        assert candidates
        scores = [c[1] for c in candidates]
        assert all(s == 60 for s in scores)

    def test_match_keys_by_provider_id(self):
        matched = self.matcher.match_keys("sk-" + "aBcD" * 9, "deepseek")
        assert "deepseek" in matched


# ── sk-proj- 不再被 DeepSeek 匹配（与 base.is_bad_key 一致）─────────

class TestSkProjExcluded:
    def test_deepseek_does_not_match_sk_proj(self):
        """sk-proj- 是 OpenAI 专属前缀；DeepSeek provider 的正则不应匹配它。

        2026-09 接入 OpenAI 后口径变化：is_bad_key 不再整体拒绝 sk-proj-
        （它是正式提取目标，路由到 OpenAI）；但 DeepSeek 自身格式不匹配不变。
        """
        from scanners.base import is_bad_key

        proj_key = "sk-proj-" + "aBcD" * 9
        # provider 正则不再捕获 sk-proj-
        matched = DEEPSEEK.match_key(proj_key)
        assert matched == []
        # OpenAI 接入后：sk-proj- 是合法提取目标，不再被过滤器整体拒绝
        assert not is_bad_key(proj_key)

    def test_deepseek_still_matches_plain_sk(self):
        matched = DEEPSEEK.match_key("sk-" + "aBcD" * 9)
        assert matched


# ── 坏 key 判定（多平台版）──────────────────────────────────────────

class TestIsBadKeyMultiProvider:
    def test_placeholder_rejected(self):
        for key in [
            "sk-yourtoken0000000000000000000000",
            "sk-xxx000000000000000000000000000",
            "sk-changeme000000000000000000000",
        ]:
            assert is_bad_key_multi_provider(key)

    def test_real_looking_accepted(self):
        assert not is_bad_key_multi_provider("sk-" + "aBcD" * 9)


# ── 结果去重 ────────────────────────────────────────────────────────

class TestDedup:
    def test_dedup_same_provider_key_url(self):
        r1 = {"key": "k1", "provider": "deepseek", "url": "https://a"}
        r2 = {"key": "k1", "provider": "deepseek", "url": "https://a"}
        out = dedup_results([r1, r2])
        assert len(out) == 1

    def test_keeps_different_urls(self):
        r1 = {"key": "k1", "provider": "deepseek", "url": "https://a"}
        r2 = {"key": "k1", "provider": "deepseek", "url": "https://b"}
        out = dedup_results([r1, r2])
        assert len(out) == 2

    def test_keeps_different_providers(self):
        r1 = {"key": "k1", "provider": "deepseek", "url": "https://a"}
        r2 = {"key": "k1", "provider": "qwen", "url": "https://a"}
        out = dedup_results([r1, r2])
        assert len(out) == 2

    def test_handles_keyresult_objects(self):
        from providers import KeyResult

        r1 = KeyResult(key="k1", provider="deepseek", url="https://a")
        r2 = KeyResult(key="k1", provider="deepseek", url="https://a")
        out = dedup_results([r1, r2])
        assert len(out) == 1


# ── Coding Plan 双计费方案（key 前缀路由）────────────────────────────

class TestCodingPlanRouting:
    """回归：Coding Plan key 必须路由到独立 api_base——否则像 kimi 的
    sk-kimi- 发到按量地址直接 401（两套体系完全隔离）。"""

    def _identify(self, key):
        from providers import UnifiedKeyMatcher
        m = UnifiedKeyMatcher()
        cands = m.identify_provider(key)
        return cands[0][0] if cands else None

    def test_kimi_coding_prefix(self):
        assert self._identify("sk-kimi-abcdefghij1234567890") == "kimi_coding"

    def test_xiaomi_plan_tp_prefix(self):
        assert self._identify("tp-abcdefghij1234567890") == "xiaomi_plan"

    def test_qwen_coding_sp_prefix(self):
        assert self._identify("sk-sp-abcdefghij1234567890") == "qwen_coding"

    def test_minimax_cp_prefix(self):
        assert self._identify("sk-cp-abcdefghij1234567890") == "minimax_cp"

    def test_plain_sk_still_deepseek(self):
        # 普通 sk- 不被 Coding Plan 前缀劫持
        assert self._identify("sk-abcdefghij1234567890abcdefghij12") == "deepseek"

    def test_coding_plan_has_own_api_base(self):
        from providers import PROVIDER_MAP
        assert "api.kimi.com" in PROVIDER_MAP["kimi_coding"].api_base
        assert "token-plan-cn" in PROVIDER_MAP["xiaomi_plan"].api_base
        assert "coding.dashscope" in PROVIDER_MAP["qwen_coding"].api_base
        assert PROVIDER_MAP["minimax_cp"].api_base == "https://api.minimaxi.com"


class TestVerifyErrorNoPreempt:
    """ERROR/429 不抢占其他 provider 的正确结果(M3 修复)测试。"""

    def test_error_result_does_not_preempt_valid(self, monkeypatch):
        """deepseek 侧网络错误先完成,kimi 侧 valid 稍后完成——必须返回 valid。"""
        import time as _t

        import providers as P
        from providers import UnifiedKeyVerifier, VerifyResult

        class FakeP:
            def __init__(self, pid):
                self.id = pid

        DEEPSEEK, KIMI, ZHIPU = FakeP("deepseek"), FakeP("kimi"), FakeP("zhipu")

        def fake_verify(key, p):
            if p.id == "deepseek":
                return {"status": VerifyResult.ERROR.value}  # 先完成:网络错误
            _t.sleep(0.2)  # 后完成
            if p.id == "zhipu":
                return {"status": VerifyResult.INVALID.value}
            return {"status": VerifyResult.VALID_ZERO.value, "balance": 0.0,
                    "key": key, "provider": p.id, "message": "ok", "model": None}

        monkeypatch.setattr(P, "UnifiedKeyMatcher", lambda: type("M", (), {
            "identify_provider": lambda self, key, context="": [
                ("deepseek", 50), ("kimi", 50), ("zhipu", 50)]})())
        monkeypatch.setattr(P, "PROVIDER_MAP",
                            {"deepseek": DEEPSEEK, "kimi": KIMI, "zhipu": ZHIPU})
        # v2.4.9: 模块级 matcher 单例可能已被其他测试文件(如 test_scanner_engine)
        # 用真 PROVIDER_MAP 编译缓存——monkeypatch 类后必须重置单例,
        # 否则合跑时拿到残留单例 → identify 返回真平台 id → KeyError(fake map 没有)。
        P.reset_matcher_singleton()

        v = UnifiedKeyVerifier(candidate_workers=3)
        v._verify_with_provider = fake_verify
        r = v.verify_key("sk-test1234567890123456789012345678901")
        assert r["status"] == VerifyResult.VALID_ZERO.value
        assert r["provider"] == "kimi"


# ── 2026-09 第二批平台：识别路由 / 查询前缀 / 币种 / models 鉴权标记 ──

class TestSecondBatchProviders:
    SAMPLES = {
        "openai": "sk-proj-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYz" * 5,
        "gemini": "AIza" + "aB3dE" * 7,
        "xai": "xai-" + "aB3dE7fG9" * 8,
        "nvidia": "nvapi-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxY" * 2,
        "modelscope": "ms-0f3a2b1c-8d7e-4f6a-9b0c-1d2e3f4a5b6c",
        "qianfan": "bce-v3/ALTAK-aBcDeFgHiJkLmNoP/4f8a1c9e27b35d6f0a91",
    }

    def test_second_batch_registered(self):
        for pid in self.SAMPLES:
            assert pid in PROVIDER_MAP, f"{pid} 未注册"

    def test_identify_routes_by_format(self):
        """格式即路由:专有前缀 key 无上下文也应识别到正确平台。"""
        from scanners.base import is_bad_key

        matcher = UnifiedKeyMatcher()
        for pid, key in self.SAMPLES.items():
            assert not is_bad_key(key), f"{pid} 样例 key 不应被过滤器拒绝"
            top = matcher.identify_provider(key)[0][0]
            assert top == pid, f"{key[:12]}... 应识别为 {pid},实际 {top}"

    def test_query_context_boosts_platform(self):
        """v2.4.9 (Y1): 扫描查询串拼进 context 后,identify 的平台域名加分必须生效。

        混元 key(sk- 通用格式)在无 query 信号时与 deepseek/kimi 等同分并列,
        只靠前 4 候选碰运气;context 含其 key_context_queries 的域名词时,
        +30 分必须把它排到第一。repo 路径几乎不含完整 API 域名——query 是
        唯一可靠信号源。"""
        matcher = UnifiedKeyMatcher()
        key = "sk-" + "aB3dE7fG9hJ1kL2mN4pQ6rS8tU0vWxYzAbCdEfGh"  # 通用 sk- 40 位
        cands = matcher.identify_provider(key, "api.hunyuan.cloud.tencent.com sk- user/app.py")
        assert cands[0][0] == "hunyuan", f"query 信号未生效: {cands[:3]}"
        assert cands[0][1] >= 80, f"域名 +30 分未加上: {cands[0]}"

    def test_query_prefix_per_provider(self):
        from providers import GEMINI, HUNYUAN, LONGCAT, MODELSCOPE, NVIDIA, OPENAI, QIANFAN, XAI
        expect = {"openai": "sk-proj-", "gemini": "AIza", "xai": "xai-",
                  "nvidia": "nvapi-", "modelscope": "ms-", "qianfan": "bce-v3",
                  "hunyuan": "sk-", "longcat": "sk-"}
        by_id = {"openai": OPENAI, "gemini": GEMINI, "xai": XAI,
                 "nvidia": NVIDIA, "modelscope": MODELSCOPE,
                 "qianfan": QIANFAN, "hunyuan": HUNYUAN, "longcat": LONGCAT}
        for pid, pfx in expect.items():
            assert QueryGenerator._key_prefix(by_id[pid]) == pfx, f"{pid} 查询前缀应为 {pfx}"

    def test_usd_providers_membership(self):
        from providers import USD_PROVIDERS
        for pid in ("claude", "openai", "gemini", "xai", "nvidia", "openrouter"):
            assert pid in USD_PROVIDERS, f"{pid} 应属 USD 平台"
        for pid in ("deepseek", "kimi", "hunyuan", "qianfan", "modelscope", "longcat"):
            assert pid not in USD_PROVIDERS, f"{pid} 应属 CNY 平台"

    def test_models_unauthenticated_flag(self):
        for pid in ("modelscope", "nvidia", "longcat"):
            p = PROVIDER_MAP[pid]
            assert p.models_unauthenticated is True, f"{pid} 应标记 models 不鉴权"
            assert p.chat_probe is True, f"{pid} 必须允许 chat 探测兜底"
        for pid in ("openai", "deepseek", "hunyuan"):
            assert PROVIDER_MAP[pid].models_unauthenticated is False

    def test_gemini_auth_type_is_query(self):
        from providers import GEMINI
        assert GEMINI.auth_type.value == "query"

    def test_domestic_second_batch_direct(self):
        from providers import UnifiedKeyVerifier
        for pid in ("hunyuan", "qianfan", "modelscope", "longcat"):
            assert pid in UnifiedKeyVerifier.DIRECT_PROVIDERS


# ── v2.4.3: 候选平台排序(同分按历史产出优先级)+ 差异化限速 ──────────

class TestCandidateOrdering:
    def test_generic_sk_tie_breaks_by_priority(self):
        """无上下文的通用 sk- key:同分平台按优先级排序,首位应是历史高产平台 deepseek。

        注:zhipu 是 hex.secret 格式,根本不会进入通用 sk- 候选集。
        """
        m = UnifiedKeyMatcher()
        cands = m.identify_provider("sk-" + "aBcD" * 9)
        assert cands[0][0] == "deepseek"
        # 前四名(并行验证的候选集)应覆盖历史高产集合
        top4 = {c[0] for c in cands[:4]}
        assert {"deepseek", "kimi", "qwen"} <= top4


class TestProviderRateIntervals:
    def test_per_provider_interval_override(self):
        """差异化间隔:qianfan 1s,其他平台保持全局 0.25s。"""
        from providers import PROVIDER_RATE_INTERVALS, ProviderRateLimiter
        assert PROVIDER_RATE_INTERVALS.get("qianfan") == 1.0
        t = {"now": 100.0}
        waits = []
        limiter = ProviderRateLimiter(
            clock=lambda: t["now"],
            sleeper=lambda s: waits.append(s) or t.__setitem__("now", t["now"] + s),
            intervals=PROVIDER_RATE_INTERVALS)
        limiter.acquire("deepseek")
        limiter.acquire("deepseek")   # 全局 0.25
        assert waits[-1] == 0.25
        limiter.acquire("qianfan")
        limiter.acquire("qianfan")    # 千帆 1.0
        assert waits[-1] == 1.0
