"""GeneratedPool（扩展查询池）测试 —— query_rotation.GeneratedPool"""
import os

import pytest

from query_rotation import GeneratedPool


def _tmp_pool_file(tmp_path, lines: list[str]):
    p = tmp_path / "g.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


class TestGeneratedPool:
    def test_skips_comments_and_blanks(self, tmp_path):
        p = _tmp_pool_file(tmp_path, [
            "# header comment",
            "api.moonshot.cn sk- filename:.env",
            "",
            "   ",
            "DEEPSEEK_API_KEY filename:mcp.json",
            "# another",
        ])
        gp = GeneratedPool(filepath=p, batch=2)
        assert len(gp) == 2

    def test_batches_are_disjoint_and_cycle(self, tmp_path):
        p = _tmp_pool_file(tmp_path, [f"q{i}" for i in range(6)])
        gp = GeneratedPool(filepath=p, batch=4)
        # 池 6 条、每批 4 条：b1=q0..q3, b2=q4,q5(剩余), b3=[](耗尽自动重置), b4 从头
        b1 = gp.next_batch()
        b2 = gp.next_batch()
        b3 = gp.next_batch()
        b4 = gp.next_batch()
        assert b1 == ["q0", "q1", "q2", "q3"]
        assert b2 == ["q4", "q5"]            # 剩余不足一批
        assert b3 == []                      # 耗尽：本轮返回空并自动重置
        assert b4 == ["q0", "q1", "q2", "q3"]  # 重置后从头轮转
        # 一个完整轮次 b1+b2 覆盖全部 6 条，无重复
        all_seen = b1 + b2
        assert set(all_seen) == {"q0", "q1", "q2", "q3", "q4", "q5"}

    def test_empty_pool_returns_empty(self, tmp_path):
        p = _tmp_pool_file(tmp_path, ["# only comment"])
        gp = GeneratedPool(filepath=p, batch=3)
        assert len(gp) == 0
        assert gp.next_batch() == []

    def test_missing_file_ok(self, tmp_path):
        gp = GeneratedPool(filepath=str(tmp_path / "nope.txt"), batch=3)
        assert len(gp) == 0

    @pytest.mark.skipif(not os.path.exists("queries_generated.txt"),
                        reason="需先运行 query_enumerator.py")
    def test_real_pool_loads(self):
        gp = GeneratedPool(filepath="queries_generated.txt", batch=12)
        assert len(gp) > 100
        assert all(isinstance(q, str) and len(q) > 0 for q in gp._queries[:20])


def test_next_batch_size_param():
    import os
    import tempfile

    from query_rotation import GeneratedPool
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(f"q{i}" for i in range(30)))
        path = f.name
    try:
        pool = GeneratedPool(path, batch_size=12)
        b1 = pool.next_batch(size=24)
        assert len(b1) == 24
        b2 = pool.next_batch(size=24)
        assert len(b2) == 6  # 剩余不足 24
        assert not pool.next_batch(size=24)  # 池耗尽,自动重置
    finally:
        os.unlink(path)
