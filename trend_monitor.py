"""
闭环趋势记录器 (trend_monitor.py)

配合 supervisor 闭环使用：每 N 分钟把 DB 关键指标 + watch 轮次摘要 + supervisor 动作
追加写入 results/trend.jsonl，供 1 小时测试窗口结束后分析趋势。

用法：python trend_monitor.py --interval 600 --duration 3600
"""
import argparse
import glob
import importlib.util
import json
import os
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(ROOT, "results")
DB_PATH = os.path.join(RESULTS, "darkforest.db")
TREND = os.path.join(RESULTS, "trend.jsonl")


def db_stats():
    try:
        spec = importlib.util.spec_from_file_location("store", os.path.join(ROOT, "store.py"))
        store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(store)
        conn = store.connect(DB_PATH)
        total = conn.execute("select count(*) from keys").fetchone()[0]
        valid = conn.execute("select count(*) from keys where valid=1").fetchone()[0]
        high = conn.execute("select count(*) from keys where valid=1 and balance>1.0").fetchone()[0]
        row = conn.execute("select max(last_seen) from keys").fetchone()
        conn.close()
        return {"total": total, "valid": valid, "high": high, "last_seen": row[0] if row else None}
    except Exception as e:
        return {"error": str(e)}


def watch_rounds():
    """从最新 watch log 提取最近几轮的提交摘要。"""
    logs = sorted(glob.glob(os.path.join(RESULTS, "watch_session_*.log")),
                  key=os.path.getmtime)
    if not logs:
        return []
    with open(logs[-1], encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    out = []
    for ln in lines:
        # 括号固定优先级:含「提交」的行同样要排除「扫描中」噪音行
        if ("提交" in ln or "key (" in ln) and "扫描中" not in ln:
            t = ln.split("]")[0].strip(" [")
            out.append(t + " " + ln.split("]")[-1].strip())
    return out[-6:]


def supervisor_actions():
    """最近 supervisor 周期里的动作。"""
    try:
        with open(os.path.join(RESULTS, "supervisor_cycles.json"), encoding="utf-8") as f:
            cycles = json.load(f)
        acted = [c for c in cycles if c.get("actions")]
        return acted[-3:] if acted else []
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--duration", type=int, default=3600)
    args = ap.parse_args()

    end = time.time() + args.duration
    while time.time() < end:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db": db_stats(),
            "watch_rounds": watch_rounds(),
            "supervisor_actions": supervisor_actions(),
        }
        with open(TREND, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        time.sleep(args.interval)
    print("trend_monitor done", flush=True)


if __name__ == "__main__":
    main()


def record_round_top_queries(tracker, round_label: str,
                             path: str = os.path.join("results", "trend.jsonl"),
                             top_n: int = 5) -> None:
    """每轮结束后记录 top 收益查询到 trend.jsonl(与现有指标同文件)。"""
    try:
        top = tracker.top_queries(top_n, min_runs=1)
        row = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "round": round_label,
               "top_queries": [[q, float(y)] for q, y in top]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
