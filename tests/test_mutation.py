"""查询变异引擎测试（query_rotation.mutate_query / generate_mutants）。"""
import os

from query_rotation import QueryRotator, generate_mutants, mutate_query


class TestMutateQuery:
    def test_adds_not_test_not_example(self):
        out = mutate_query("deepseek sk- filename:env")
        assert any("NOT test" in m for m in out)
        assert any("NOT example" in m for m in out)

    def test_filename_cluster_java_to_kt(self):
        out = mutate_query("deepseek sk- filename:java")
        assert "deepseek sk- filename:kt" in out
        assert "deepseek sk- filename:scala" in out

    def test_suffix_swap_api_key(self):
        out = mutate_query("DEEPSEEK_API_KEY sk-")
        # _API_KEY → _KEY / _TOKEN
        assert "DEEPSEEK_KEY sk-" in out
        assert "DEEPSEEK_TOKEN sk-" in out

    def test_no_duplicate_of_original(self):
        q = "deepseek sk- filename:java"
        assert q not in mutate_query(q)

    def test_no_internal_duplicates(self):
        out = mutate_query("deepseek sk- filename:java")
        assert len(out) == len(set(out))


class TestGenerateMutants:
    def test_empty_when_no_top(self):
        assert generate_mutants([], set()) == []

    def test_caps_at_max(self):
        top = [("deepseek sk- filename:java", 10.0), ("deepseek sk- filename:env", 5.0)]
        out = generate_mutants(top, existing=set(), max_mutants=3)
        assert len(out) <= 3

    def test_excludes_existing(self):
        top = [("deepseek sk- filename:java", 10.0)]
        out = generate_mutants(top, existing={"deepseek sk- filename:kt"}, max_mutants=20)
        assert "deepseek sk- filename:kt" not in out

    def test_dedup_across_sources(self):
        # 两个 top 查询可能产生相同变体 → 去重
        top = [("deepseek sk- filename:java", 10.0), ("deepseek sk- filename:kt", 8.0)]
        out = generate_mutants(top, existing=set(), max_mutants=50)
        assert len(out) == len(set(out))


class TestRotatorPersistence:
    """回归：轮次持久化——重启续跑（不重复扫近期查询）+ --fresh 从头。"""

    def test_restore_round_from_state_file(self, tmp_path):
        state_file = str(tmp_path / "rotator_state.json")
        r = QueryRotator(num_buckets=2, state_file=state_file)
        assert r.round_num == 0
        r.next_round()  # round 1
        r.next_round()  # round 2
        r.save_state()
        # 重启：新实例从 state 恢复轮次
        r2 = QueryRotator(num_buckets=2, state_file=state_file)
        assert r2.round_num == 2, "重启后应从上次轮次继续"
        r2.next_round()  # 应是 round 3（对应 bucket 1），不是从 1 重来
        assert r2.round_num == 3

    def test_no_state_file_starts_fresh(self, tmp_path):
        r = QueryRotator(num_buckets=2,
                         state_file=str(tmp_path / "nope.json"))
        assert r.round_num == 0

    def test_corrupt_state_ignored(self, tmp_path):
        state_file = str(tmp_path / "rotator_state.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("{not json")
        r = QueryRotator(num_buckets=2, state_file=state_file)
        assert r.round_num == 0  # 损坏文件忽略，不崩溃

    def test_reset_clears_state(self, tmp_path):
        state_file = str(tmp_path / "rotator_state.json")
        r = QueryRotator(num_buckets=2, state_file=state_file)
        r.next_round()
        r.save_state()
        r.reset()  # --fresh
        assert r.round_num == 0
        assert not os.path.exists(state_file)
        # 新实例（fresh 后）不再恢复旧轮次
        r2 = QueryRotator(num_buckets=2, state_file=state_file)
        assert r2.round_num == 0

    def test_state_saved_atomically(self, tmp_path):
        import json
        state_file = str(tmp_path / "rotator_state.json")
        r = QueryRotator(num_buckets=2, state_file=state_file)
        r.next_round()
        r.save_state()
        with open(state_file, encoding="utf-8") as f:
            assert json.load(f)["round"] == 1


# ── 兼容层查询（OpenAI/Anthropic 接口 + 第三方 key）─────────────────

class TestCompatQueries:
    def test_compat_queries_exist(self):
        from query_rotation import COMPAT_QUERIES
        assert len(COMPAT_QUERIES) >= 10
        # 必须覆盖 openai/anthropic 标识
        joined = " ".join(COMPAT_QUERIES)
        assert "api.openai.com" in joined
        assert "OPENAI_API_KEY" in joined
        assert "api.anthropic.com" in joined
        assert "ANTHROPIC_API_KEY" in joined
        assert "base_url" in joined

    def test_fresh_queries_include_compat(self):
        from query_rotation import generate_fresh_queries
        # 多轮轮转后兼容层查询应被覆盖
        seen = set()
        for p in range(15):
            seen.update(generate_fresh_queries(p))
        assert any("api.openai.com" in q or "OPENAI_API_KEY" in q for q in seen)

    def test_compat_query_spacing(self):
        from query_rotation import generate_fresh_queries
        # v2.6: expanded to include openrouter/siliconflow/dashscope compat queries
        q = generate_fresh_queries(0)
        compat = [x for x in q if any(k in x for k in
                  ("openai", "anthropic", "base_url", "openrouter", "siliconflow", "dashscope"))]
        assert 1 <= len(compat) <= 3

    def test_history_queries_include_compat(self):
        from query_rotation import QueryRotator
        r = QueryRotator(num_buckets=1, queries=["x"], state_file=None)
        hq = r._generate_history_queries()
        assert any("openai" in q or "anthropic" in q for q in hq)


# ── is_barren 新判据：提取>0 但新提交=0 ────────────────────────────

class TestBarrenNewSubmissions:
    """回归：查询反复提取历史 key（提取>0）但新提交恒 0 → 必须被 barren 跳过。

    修复前：is_barren 只看提取数，提取恒>0 → 永不跳过 → 每轮重复提取
    同一批历史 key，提交恒 0（长跑"没产出"的根因）。"""

    def _tracker(self):
        from scanner_engine import QueryTracker
        t = QueryTracker.__new__(QueryTracker)
        t._path = None
        t._stats = {}
        t._round_counts = {}
        t._cooldown = {}
        t._cooldown_rounds = 20
        return t

    def test_high_extraction_zero_new_is_barren(self):
        t = self._tracker()
        t.record("q1", 50)            # 提取 50
        t.diminishing_rounds("q1", 0, 50)   # 新提交 0
        t.record("q1", 30)
        t.diminishing_rounds("q1", 0, 30)
        assert t.is_barren("q1"), "提取>0 但 2 轮新提交=0 应判 barren"

    def test_zero_extraction_still_barren(self):
        t = self._tracker()
        t.record("q1", 0)
        t.record("q1", 0)
        assert t.is_barren("q1")

    def test_new_submissions_not_barren(self):
        t = self._tracker()
        t.record("q2", 50)
        t.diminishing_rounds("q2", 5, 50)   # 新提交 5
        t.record("q2", 30)
        t.diminishing_rounds("q2", 3, 30)
        assert not t.is_barren("q2")

    def test_single_round_zero_new_not_barren(self):
        t = self._tracker()
        t.record("q3", 50)
        t.diminishing_rounds("q3", 0, 50)   # 只有 1 轮 → 不跳过（阈值 2 轮）
        assert not t.is_barren("q3")
