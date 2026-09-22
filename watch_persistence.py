"""Watch 持久化与台账读写：state JSON、SQLite 历史、CSV 快照。"""

from __future__ import annotations

import csv
import json
import os
import shutil


def save_watch_state(path: str, valid_results: list[dict]) -> None:
    """将有效 key 结果保存到 JSON（按 key 去重）。

    即使结果为空也写入空快照：这是显式清理动作，防止全部 key 被剔除后，
    旧 state 在下次启动时复活。

    原子写：先写同目录 tmp 再 os.replace——崩溃/断电不会留下截断的半文件
    （曾有过 JSON 损坏导致历史丢失的隐患）。
    Phase 5.1 加固：保存成功后再写 .bak，启动时优先从 .bak 恢复。
    """
    pool = {r["key"]: r for r in valid_results if r.get("key")}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"keys": pool}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    # 保存成功后写备份。bak 只是冗余副本——copy 失败（磁盘满/权限）不应让
    # 整次保存抛异常，更不应留下比主文件旧的 bak 在启动时回滚新状态。
    try:
        bak = f"{path}.bak"
        shutil.copy2(path, bak)
    except OSError:
        pass


def load_watch_state(path: str) -> list[dict]:
    """加载 watch_state.json，返回验证结果列表。文件不存在/损坏返回空列表。

    主文件本身是 tmp+replace 原子写，永远不会是半截文件——因此优先主文件，
    仅当主文件缺失或损坏时才回退 .bak（旧的「bak 优先」会把上一轮 copy 失败
    时留下的旧备份当成最新状态，静默回滚较新的会话数据）。
    """
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return list(data.get("keys", {}).values())
        except (OSError, json.JSONDecodeError):
            pass
    # 主文件缺失/损坏才走备份恢复
    bak = f"{path}.bak"
    if os.path.exists(bak):
        try:
            with open(bak, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("keys"):
                shutil.copy2(bak, path)  # restore from backup
                return list(data["keys"].values())
        except (OSError, json.JSONDecodeError):
            pass
    return []


def load_history(results_dir: str = "./results") -> list[dict]:
    """三路合并加载历史 key（启动种子 & 保存合并共用的唯一历史源）。

    - watch_state.json：最新视图（可能被清空/覆盖）
    - darkforest.db：SQLite 全量账本（仅取有效 key，与 JSON 语义一致）
    - watch_high_value.csv：增量账本（高价值 key，含 first_seen/verified_at）

    按 key 去重，优先 JSON 记录（字段最全）。任一源缺失不影响其他源。
    """
    merged: dict[str, dict] = {}

    def _add(rec: dict):
        key = rec.get("key") or ""
        if not key:
            return
        # 字段归一化：验证结果 dict 用 `balance`，但重验/邮件/filter 读 `balance_cny`。
        # watch_state.json 只存 `balance` → 若缺 balance_cny，从 balance 补齐，
        # 否则高价值 key 判定（balance_cny > min_balance）会恒失败 → 不重验、不发邮件。
        b = rec.get("balance_cny")
        if b is None and rec.get("balance") is not None:
            rec["balance_cny"] = rec.get("balance")
        b_usd = rec.get("balance_usd")
        if b_usd is None:
            bal_cny = rec.get("balance_cny") or 0
            rec["balance_usd"] = bal_cny / 7.25 if bal_cny else 0.0
        if key not in merged:
            merged[key] = rec
        else:
            # 已存在记录缺 balance_cny / balance 而新记录有 → 补齐（保留已在的优先字段）
            old = merged[key]
            for f in ("balance_cny", "balance", "balance_usd"):
                if (old.get(f) in (None, "") or (f == "balance_cny" and old.get(f) is None)) \
                        and rec.get(f) not in (None, ""):
                    old[f] = rec[f]

    # 1. JSON 视图
    for r in load_watch_state(os.path.join(results_dir, "watch_state.json")):
        _add(r)
    # 2. SQLite 账本（仅有效 key）
    try:
        import store as _store
        conn = _store.connect(os.path.join(results_dir, "darkforest.db"))
        try:
            for row in _store.query(conn, valid_only=True, min_balance=0, limit=100000):
                _add(dict(row))
        finally:
            conn.close()
    except Exception:
        pass
    # 3. CSV 增量账本
    try:
        csv_path = os.path.join(results_dir, "watch_high_value.csv")
        if os.path.exists(csv_path):
            with open(csv_path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    k = (row.get("完整Key") or "").strip()
                    if k:
                        _add({"key": k, "valid": True,
                              "balance_cny": float(row.get("余额(CNY)", 0) or 0),
                              "source": row.get("数据源", "csv"),
                              "verified_at": row.get("最后验证", ""),
                              "first_seen": row.get("首次发现", "")})
    except Exception:
        pass
    return list(merged.values())


_WATCH_CSV_HEADER = [
    "Key预览", "完整Key", "平台", "余额(CNY)", "余额(USD)",
    "原始余额", "币种", "首次发现", "最后验证", "数据源", "仓库", "状态",
]


def _fmt_money(v) -> str:
    """余额列格式化容错：手改/外来行可能是非数字字符串，:2f 会炸整个保存。"""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _watch_csv_row(r: dict) -> list[str]:
    repos_str = "; ".join(x.get("repo", "") for x in r.get("repos", [])[:3])
    try:
        raw_balance = f'{float(r.get("balance", 0)):.4f}'
    except (TypeError, ValueError):
        raw_balance = "0.0000"
    return [
        r.get("key_preview", ""),
        r.get("key", ""),
        r.get("provider", "unknown"),  # 哪家的 key(deepseek/kimi/qwen/...)
        _fmt_money(r.get("balance_cny", 0)),
        _fmt_money(r.get("balance_usd", 0)),
        raw_balance,
        r.get("primary_currency") or "USD",
        r.get("first_seen", "") or r.get("verified_at", ""),
        r.get("verified_at", ""),
        r.get("source", "unknown"),
        repos_str,
        r.get("status", "valid"),  # valid_active/valid_zero/valid_no_balance
    ]


def write_watch_csv(path: str, high_value_keys: list[dict],
                    arrears_keys: set[str] | None = None) -> None:
    """增量写入高价值 key CSV：已有行保留（不覆盖），追加当前高价值 key，
    仅移除验证为欠费（balance_cny < 0）的 key。最终按 余额(CNY) 降序。

    arrears_keys: 本次验证判定为欠费的 key 集合 → 从已有行中删除。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    arrears_keys = arrears_keys or set()

    # 本轮验证结果的平台映射,用于回填已有行缺失的平台字段
    # (旧 CSV 无平台列,保留已有行时该字段空白)。
    provider_map = {r.get("key", ""): r.get("provider", "") for r in high_value_keys}

    # 1. 读已有行 → {key: row}，保留历史字段（首次发现等），剔除欠费 key。
    #    旧 schema 行(缺平台列)自动补齐到新 schema 长度。
    rows_by_key: dict[str, list[str]] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                existing = list(csv.reader(f))
            if existing:
                header = existing[0]
                ki = header.index("完整Key") if "完整Key" in header else 1
                old_has_platform = "平台" in header
                for row in existing[1:]:
                    key = row[ki] if len(row) > ki else ""
                    if key and key not in arrears_keys:
                        # 旧 schema(无平台列)→ 在索引 2 插入平台占位,对齐新列
                        if not old_has_platform:
                            row = row[:2] + [provider_map.get(key, "")] + row[2:]
                        elif len(row) > 2 and not row[2] and provider_map.get(key):
                            # 新 schema 但平台字段空(本轮有该 key 的验证结果)→ 回填
                            row[2] = provider_map[key]
                        rows_by_key[key] = row
        except (OSError, ValueError):
            pass

    # 2. 追加当前结果中尚未记录的 key（不覆盖已有行）
    for r in sorted(high_value_keys, key=lambda r: r.get("balance_cny", 0), reverse=True):
        k = r.get("key", "")
        if k and k not in rows_by_key:
            rows_by_key[k] = _watch_csv_row(r)

    # 3. 按余额降序写出（已有 + 新增合并）；原子写：tmp + os.replace，防截断损坏
    # 余额列索引 3（Key预览=0, 完整Key=1, 平台=2, 余额CNY=3）。
    # float 解析容错:手改/外来/旧 schema 的 CSV 该列可能非数字——
    # 排序抛 ValueError 会炸掉每次 _save_from_broker(台账从此不再落盘)。
    def _balance_sort_key(row):
        try:
            return float(row[3]) if len(row) > 3 and row[3] else 0.0
        except (TypeError, ValueError):
            return 0.0

    all_rows = sorted(rows_by_key.values(), key=_balance_sort_key, reverse=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_WATCH_CSV_HEADER)
        writer.writerows(all_rows)
    os.replace(tmp, path)


__all__ = [
    "save_watch_state",
    "load_watch_state",
    "load_history",
    "write_watch_csv",
]
