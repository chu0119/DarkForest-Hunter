#!/usr/bin/env python3
"""
全量扫描 — 边扫边验证边保存
优化: text_matches 快速提取, requests 验证, 即时保存
"""
import sys, time, os, json, requests, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
sys.path.insert(0, ".")

from scanner_engine import ScannerEngine, BUILTIN_QUERIES, is_bad_key, convert_to_usd, convert_to_cny

def log(msg, level="info"):
    prefix = {"warning": "[!] ", "error": "[ERR] "}.get(level, "")
    t = time.strftime("%H:%M:%S")
    print(f"[{t}] {prefix}{msg}", flush=True)

# ── 配置 ──
OUTPUT_FILE = "results/deepseek_keys_result.json"
PROXY = "http://127.0.0.1:7897"  # 本地代理
os.makedirs("results", exist_ok=True)

# ── 加载已有结果 ──
EXISTING = {}
if os.path.exists(OUTPUT_FILE):
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            for r in json.load(f):
                EXISTING[r["key"]] = r
        log(f"Loaded {len(EXISTING)} existing keys")
    except (json.JSONDecodeError, KeyError, IOError):
        pass

def save_results():
    merged = sorted(EXISTING.values(), key=lambda x: x.get("balance_usd", 0), reverse=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged

def verify_key(key_info):
    key = key_info["key"]
    url = "https://api.deepseek.com/user/balance"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            balance_infos = data.get("balance_infos", [])
            total_balance = 0.0
            primary_currency = "USD"
            for info in balance_infos:
                currency = info.get("currency", "unknown")
                total = float(info.get("total_balance", 0))
                total_balance += total
                if currency == "CNY":
                    primary_currency = "CNY"
            return {
                "key": key,
                "key_preview": key_info.get("key_preview", key[:10] + "..." + key[-4:]),
                "valid": True,
                "balance": total_balance,
                "balance_usd": convert_to_usd(total_balance, primary_currency),
                "balance_cny": convert_to_cny(total_balance, primary_currency),
                "primary_currency": primary_currency,
                "repos": key_info.get("repos", []),
                "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        elif r.status_code == 401:
            return {"key": key, "valid": False, "reason": "invalid"}
        elif r.status_code == 429:
            return {"key": key, "valid": False, "reason": "rate_limited"}
        else:
            return {"key": key, "valid": False, "reason": f"HTTP {r.status_code}"}
    except requests.Timeout:
        return {"key": key, "valid": False, "reason": "timeout"}
    except Exception as e:
        return {"key": key, "valid": False, "reason": str(e)[:50]}

def verify_and_save(new_keys, source_name):
    if not new_keys:
        log(f"  [{source_name}] 无新 Key")
        return 0

    log(f"  [{source_name}] 验证 {len(new_keys)} 个 Key...")
    new_count = 0
    valid_count = 0
    positive_count = 0

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(verify_key, ki): ki for ki in new_keys}
        for future in as_completed(futures):
            try:
                result = future.result(timeout=15)
            except Exception as e:
                result = {"key": futures[future]["key"], "valid": False, "reason": str(e)[:50]}

            if result.get("valid"):
                valid_count += 1
                k = result["key"]
                if k not in EXISTING:
                    EXISTING[k] = result
                    new_count += 1
                else:
                    old = EXISTING[k]
                    if result.get("balance_usd", 0) != old.get("balance_usd", 0):
                        EXISTING[k] = result
                    else:
                        old_repos = {x["repo"] for x in old.get("repos", [])}
                        for repo in result.get("repos", []):
                            if repo["repo"] not in old_repos:
                                old.setdefault("repos", []).append(repo)

                if result.get("balance_usd", 0) > 0:
                    positive_count += 1
                    log(f"    [+] {result['key'][:10]}... | ${result['balance_usd']:.2f}")

    merged = save_results()
    valid_total = len([r for r in merged if r.get("valid")])
    positive_total = len([r for r in merged if r.get("valid") and r.get("balance_usd", 0) > 0])
    log(f"  [{source_name}] 完成: 新增 {new_count} | 有效 {valid_count} | 正余额 {positive_count}")
    log(f"  累计: {len(merged)} 总 | {valid_total} 有效 | {positive_total} 正余额")
    return new_count

# ═══════════════════════════════════════════════════════════════════
#  PHASE 1: GitHub Code Search (text_matches 快速模式)
# ═══════════════════════════════════════════════════════════════════
log(f"{'='*60}")
log("PHASE 1: GitHub Code Search — text_matches 快速模式")
log(f"{'='*60}")

engine = ScannerEngine(
    concurrency=15,
    timeout=15,
    search_delay=4.0,
    scan_pages=3,
    max_duration=0,
    max_valid_keys=0,
    output_dir="./results",
    log_callback=log,
    proxy=PROXY,
)

t0 = time.time()
total_new = 0
queries = list(BUILTIN_QUERIES)

for i, query in enumerate(queries):
    if engine._should_stop():
        break

    log(f"\n[{i+1}/{len(queries)}] {query}")

    all_keys = {}
    for page in range(1, 4):
        if page > 1:
            time.sleep(4.0)
        items = engine._gh_search(query, per_page=100, page=page, with_text_matches=True)
        if not items:
            break

        for item in items:
            repo = item.get("repository", {}).get("full_name", "")
            path = item.get("path", "")
            html_url = item.get("html_url", "")
            if not repo or not path:
                continue

            text_matches = item.get("text_matches", [])
            for match in text_matches:
                fragment = match.get("fragment", "")
                keys = engine.key_pattern.findall(fragment)
                for k in keys:
                    if not is_bad_key(k, engine.extra_bad_patterns):
                        if k not in all_keys:
                            all_keys[k] = {"key": k, "key_preview": k[:10] + "..." + k[-4:], "repos": []}
                        if repo not in [x["repo"] for x in all_keys[k]["repos"]]:
                            all_keys[k]["repos"].append({"repo": repo, "file": path, "url": html_url})

        if len(items) < 100:
            break

    log(f"  发现 {len(all_keys)} 个 Key")
    new = verify_and_save(list(all_keys.values()), "GitHub Code")
    total_new += new
    time.sleep(4.0)

# ═══════════════════════════════════════════════════════════════════
#  PHASE 2: GitHub 生态
# ═══════════════════════════════════════════════════════════════════
log(f"\n{'='*60}")
log("PHASE 2: GitHub 生态 — Gist + Issues + Commits")
log(f"{'='*60}")

engine2 = ScannerEngine(
    concurrency=10,
    timeout=15,
    search_delay=2.0,
    scan_pages=3,
    max_duration=0,
    max_valid_keys=0,
    output_dir="./results/.tmp_batch",
    log_callback=log,
    proxy=PROXY,
)

for src, label in [("gist", "GitHub Gists"), ("issues", "GitHub Issues"), ("commits", "GitHub Commits")]:
    log(f"\n[{label}] 扫描...")
    try:
        results = engine2.run_multi_source([src], queries=BUILTIN_QUERIES[:20])
        keys_list = [{"key": r["key"], "key_preview": r.get("key_preview", ""), "repos": r.get("repos", [])} for r in results]
        new = verify_and_save(keys_list, label)
        total_new += new
    except Exception as e:
        log(f"  [{label}] 异常: {e}", "error")

# ═══════════════════════════════════════════════════════════════════
#  最终报告
# ═══════════════════════════════════════════════════════════════════
elapsed = time.time() - t0
merged = save_results()
valid = [r for r in merged if r.get("valid")]
positive = [r for r in valid if r.get("balance_usd", 0) > 0]
zero = [r for r in valid if r.get("balance_usd", 0) == 0]
negative = [r for r in valid if r.get("balance_usd", 0) < 0]

log(f"\n{'='*60}")
log(f"扫描完成 | 耗时 {elapsed:.0f}s ({elapsed/60:.1f} min)")
log(f"{'='*60}")
log(f"总 Key 数: {len(merged)}")
log(f"有效: {len(valid)} | 正余额: {len(positive)} | 零余额: {len(zero)} | 欠费: {len(negative)}")
log(f"本轮新增: {total_new}")

if positive:
    total_usd = sum(r["balance_usd"] for r in positive)
    total_cny = sum(r["balance_cny"] for r in positive)
    log(f"\n正余额总价值: ${total_usd:.2f} / ¥{total_cny:.2f}")
    log(f"\nTop 10:")
    for i, r in enumerate(sorted(positive, key=lambda x: x["balance_usd"], reverse=True)[:10]):
        src = r["repos"][0]["repo"] if r.get("repos") else "N/A"
        log(f"  {i+1}. {r['key']} | ${r['balance_usd']:.2f} | {src[:50]}")

log(f"\n结果已保存: {OUTPUT_FILE}")
