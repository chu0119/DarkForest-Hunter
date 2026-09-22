def test_record_round_top_queries(tmp_path):
    import json

    from trend_monitor import record_round_top_queries

    class FakeTracker:
        def top_queries(self, n=5, min_runs=1):
            return [("q1", 5.0), ("q2", 3.0)]

    p = tmp_path / "trend.jsonl"
    record_round_top_queries(FakeTracker(), "R1", path=str(p))
    with open(p, encoding="utf-8") as f:
        row = json.loads(f.readline())
    assert row["round"] == "R1"
    assert len(row["top_queries"]) == 2
    assert row["top_queries"][0][0] == "q1"
