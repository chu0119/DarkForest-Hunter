#!/usr/bin/env python3
"""
Fast Scan — 统一入口，替代 full_scan/ultimate_scan/multi_platform_scan
优化: 智能分页, 降低延迟, 增量扫描, 并行外部平台

用法:
  python fast_scan.py                    # 完整扫描
  python fast_scan.py --duration 600     # 限时10分钟
  python fast_scan.py --phase 1          # 只跑 Phase 1 (GitHub Code)
  python fast_scan.py --phase 2          # 只跑 Phase 2 (GitHub 生态)
  python fast_scan.py --phase 3          # 只跑 Phase 3 (外部平台)
  python fast_scan.py --dry-run          # 预览查询集
  python fast_scan.py --no-external      # 跳过外部平台
"""
import sys
import os
import time
import json
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, ".")

from scanner_engine import (
    ScannerEngine, BUILTIN_QUERIES,
    load_tiered_queries, get_flat_queries,
    is_bad_key, convert_to_usd, convert_to_cny,
)

# ── 配置 ──
OUTPUT_FILE = "results/deepseek_keys_result.json"
QUERIES_FILE = "queries_v5.txt"
os.makedirs("results", exist_ok=True)

# 代理: 优先环境变量, 其次自动检测
PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None


def log(msg, level="info"):
    prefix = {"warning": "[!] ", "error": "[ERR] "}.get(level, "")
    t = time.strftime("%m-%d %H:%M:%S")
    print(f"[{t}] {prefix}{msg}", flush=True)


# ── 加载已有结果 (增量扫描) ──
EXISTING = {}
if os.path.exists(OUTPUT_FILE):
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            for r in json.load(f):
                EXISTING[r["key"]] = r
        log(f"Loaded {len(EXISTING)} existing keys")
    except (json.JSONDecodeError, KeyError) as e:
        log(f"Warning: Could not load existing results: {e}", "warning")


def save_results():
    merged = sorted(EXISTING.values(), key=lambda x: x.get("balance_usd", 0), reverse=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def verify_key(key_info, max_retries=3):
    """验证单个 key 的余额，支持 429 重试"""
    import requests as _req
    key = key_info["key"]
    url = "https://api.deepseek.com/user/balance"
    headers = {"Authorization": f"Bearer {key}"}

    for attempt in range(max_retries):
        try:
            r = _req.get(url, headers=headers, timeout=10)
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
                    "balance_details": balance_infos,
                    "repos": key_info.get("repos", []),
                    "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            elif r.status_code == 401:
                return {"key": key, "valid": False, "reason": "invalid"}
            elif r.status_code == 429:
                # 限流重试：等待后重试
                wait = min(2 ** attempt * 2, 10)  # 2s, 4s, 8s
                time.sleep(wait)
                continue
            else:
                return {"key": key, "valid": False, "reason": f"HTTP {r.status_code}"}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return {"key": key, "valid": False, "reason": str(e)[:50]}

    # 所有重试都失败（429）
    return {"key": key, "valid": False, "reason": "rate_limited"}


def verify_and_save(new_keys, source_name):
    """验证一批 key 并保存到 EXISTING"""
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
                    # 更新余额或合并 repo 信息
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
#  PHASE 1: GitHub Code Search — 智能分页 + 快速模式
# ═══════════════════════════════════════════════════════════════════
def run_phase1(queries_tiered, max_duration=0):
    """Phase 1: GitHub Code Search with text_matches fast mode"""
    log(f"\n{'='*60}")
    log("PHASE 1: GitHub Code Search — 智能分页 + text_matches")
    log(f"{'='*60}")

    # 检测认证状态, 自动调整延迟
    # Code Search API 限流: 10 次/分钟 (认证用户)
    has_auth = ScannerEngine.check_gh_auth()
    search_delay = 6.5 if has_auth else 6.5  # 统一 6.5s 确保不超过 10 次/分钟
    page_delay = 6.5 if has_auth else 6.5
    log(f"GitHub 认证: {'✓ 已认证' if has_auth else '✗ 未认证 (延迟较高)'}")
    log(f"搜索延迟: {search_delay}s/query, {page_delay}s/page (Code Search 限流: 10次/分钟)")

    engine = ScannerEngine(
        concurrency=15,
        timeout=15,
        search_delay=search_delay,
        scan_pages=5,
        max_duration=max_duration,
        max_valid_keys=0,
        output_dir="./results",
        log_callback=log,
        proxy=PROXY,
    )

    t0 = time.time()
    total_new = 0

    for i, q in enumerate(queries_tiered):
        if engine._should_stop():
            break

        query = q["query"]
        tier = q["tier"]
        max_pages = q["pages"]

        log(f"\n[T{i+1}/{len(queries_tiered)}] Tier {tier} | {query} (max {max_pages}p)")

        all_keys = {}
        for page in range(1, max_pages + 1):
            if page > 1:
                time.sleep(page_delay)

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
        time.sleep(search_delay)

    elapsed = time.time() - t0
    log(f"\nPHASE 1 完成: 耗时 {elapsed:.0f}s | 新增 {total_new}")
    return total_new


# ═══════════════════════════════════════════════════════════════════
#  PHASE 2: GitHub 生态 (Gist + Issues + Commits)
# ═══════════════════════════════════════════════════════════════════
def run_phase2(queries_flat):
    """Phase 2: GitHub ecosystem scanners"""
    log(f"\n{'='*60}")
    log("PHASE 2: GitHub 生态 — Gist + Issues + Commits")
    log(f"{'='*60}")

    engine = ScannerEngine(
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

    t0 = time.time()
    total_new = 0

    for src, label in [("gist", "GitHub Gists"), ("issues", "GitHub Issues"), ("commits", "GitHub Commits")]:
        log(f"\n[{label}] 扫描...")
        try:
            results = engine.run_multi_source([src], queries=queries_flat[:30])
            keys_list = [{"key": r["key"], "key_preview": r.get("key_preview", ""), "repos": r.get("repos", [])} for r in results]
            new = verify_and_save(keys_list, label)
            total_new += new
        except Exception as e:
            log(f"  [{label}] 异常: {e}", "error")

    elapsed = time.time() - t0
    log(f"\nPHASE 2 完成: 耗时 {elapsed:.0f}s | 新增 {total_new}")
    return total_new


# ═══════════════════════════════════════════════════════════════════
#  PHASE 3: 外部平台 (并行, 每个平台独立线程+timeout)
# ═══════════════════════════════════════════════════════════════════
def run_phase3(queries_flat):
    """Phase 3: External platforms with parallel execution"""
    log(f"\n{'='*60}")
    log("PHASE 3: 外部平台 (并行扫描)")
    log(f"{'='*60}")

    engine = ScannerEngine(
        concurrency=10,
        timeout=15,
        search_delay=2.0,
        scan_pages=3,
        max_duration=0,
        max_valid_keys=0,
        output_dir="./results/.tmp_ext",
        log_callback=log,
        proxy=PROXY,
    )

    # 平台列表: (source_name, label, timeout_seconds, queries_to_use)
    # 已移除零产出 AI 平台: Replicate, Civitai, Together AI, Modal, Groq, DeepInfra, Fal.ai
    platforms = [
        # 代码托管平台
        ("github_raw", "GitHub Raw (宽泛sk-搜索)", 180, None),
        ("stackoverflow", "StackOverflow", 120, queries_flat[:15]),
        ("npm", "npm", 90, queries_flat[:15]),
        ("gitee", "Gitee", 120, queries_flat[:15]),
        ("gitlab", "GitLab", 120, queries_flat[:15]),
        ("pypi", "PyPI", 90, queries_flat[:15]),
        ("huggingface", "HuggingFace", 120, queries_flat[:15]),
        ("pastebin", "Pastebin", 90, None),

        # 搜索和社交平台
        ("reddit", "Reddit", 90, None),
        ("google_dork", "Google Dork", 180, None),

        # 归档和镜像
        ("wayback", "Wayback Machine", 90, queries_flat[:10]),
        ("commoncrawl", "Common Crawl", 90, queries_flat[:10]),
        ("docker", "Docker Hub", 60, queries_flat[:10]),
    ]

    t0 = time.time()
    total_new = 0

    for src, label, timeout_s, qs in platforms:
        if engine._should_stop():
            break

        log(f"\n[{label}] 扫描 (timeout: {timeout_s}s)...")
        result_box = [None]
        error_box = [None]

        def _run_scanner(src=src, qs=qs):
            try:
                result_box[0] = engine.run_multi_source([src], queries=qs)
            except Exception as e:
                error_box[0] = e

        t = threading.Thread(target=_run_scanner, daemon=True)
        t.start()
        t.join(timeout=timeout_s)

        if t.is_alive():
            log(f"  [{label}] 超时 ({timeout_s}s), 跳过", "warning")
            continue

        if error_box[0]:
            log(f"  [{label}] 异常: {error_box[0]}", "error")
            continue

        results = result_box[0] or []
        keys_list = [{"key": r["key"], "key_preview": r.get("key_preview", ""), "repos": r.get("repos", [])} for r in results]
        new = verify_and_save(keys_list, label)
        total_new += new

    elapsed = time.time() - t0
    log(f"\nPHASE 3 完成: 耗时 {elapsed:.0f}s | 新增 {total_new}")
    return total_new


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Fast Scan — DeepSeek Key Hunter 统一入口")
    parser.add_argument("--duration", type=int, default=0, help="最大扫描时长(秒), 0=无限")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3], help="只运行指定阶段 (1=GitHub Code, 2=GitHub Eco, 3=External)")
    parser.add_argument("--no-external", action="store_true", help="跳过外部平台")
    parser.add_argument("--dry-run", action="store_true", help="预览查询集, 不实际扫描")
    parser.add_argument("--queries-file", type=str, default=QUERIES_FILE, help="查询文件路径")
    parser.add_argument("--parallel", action="store_true", help="启用多平台并行扫描")
    parser.add_argument("--workers", type=int, default=5, help="并行扫描 worker 数量")
    args = parser.parse_args()

    # 加载查询
    tiered = load_tiered_queries(args.queries_file)
    if not tiered:
        log(f"Warning: 无法加载 {args.queries_file}, 使用内置查询", "warning")
        tiered = [{"tier": 1, "query": q, "pages": 3} for q in BUILTIN_QUERIES]

    flat = get_flat_queries(tiered)

    log(f"{'='*60}")
    log(f"Fast Scan — DeepSeek Key Hunter")
    log(f"{'='*60}")
    log(f"查询数: {len(tiered)} (来自 {args.queries_file})")
    log(f"已有结果: {len(EXISTING)} 个 Key")
    log(f"代理: {PROXY or '无 (直连)'}")
    log(f"GitHub 认证: {'✓' if ScannerEngine.check_gh_auth() else '✗'}")

    # 显示 tier 分布
    tier_counts = {}
    for q in tiered:
        tier_counts[q["tier"]] = tier_counts.get(q["tier"], 0) + 1
    log(f"Tier 分布: {dict(sorted(tier_counts.items()))}")

    if args.dry_run:
        log(f"\n{'='*60}")
        log("DRY RUN — 查询预览")
        log(f"{'='*60}")
        for q in tiered:
            log(f"  Tier {q['tier']:2d} | {q['pages']}p | {q['query']}")
        log(f"\n总计: {len(tiered)} 条查询")
        return

    t0 = time.time()

    if args.phase:
        # 只运行指定阶段
        if args.phase == 1:
            run_phase1(tiered, max_duration=args.duration)
        elif args.phase == 2:
            run_phase2(flat)
        elif args.phase == 3:
            if args.parallel:
                # 并行扫描模式
                from parallel_scanner import ParallelScanner
                scanner = ParallelScanner(max_workers=args.workers, log_callback=log)
                results = scanner.scan_all_groups()
                keys_list = [{"key": r["key"], "key_preview": r.get("key_preview", ""), "repos": r.get("repos", [])} for r in results]
                verify_and_save(keys_list, "Parallel Scan")
            else:
                run_phase3(flat)
    else:
        # 完整扫描
        run_phase1(tiered, max_duration=args.duration)
        if not args.no_external:
            run_phase2(flat)
            if args.parallel:
                # 并行扫描模式
                from parallel_scanner import ParallelScanner
                scanner = ParallelScanner(max_workers=args.workers, log_callback=log)
                results = scanner.scan_all_groups()
                keys_list = [{"key": r["key"], "key_preview": r.get("key_preview", ""), "repos": r.get("repos", [])} for r in results]
                verify_and_save(keys_list, "Parallel Scan")
            else:
                run_phase3(flat)

    # 最终报告
    elapsed = time.time() - t0
    merged = save_results()
    valid = [r for r in merged if r.get("valid")]
    positive = [r for r in valid if r.get("balance_usd", 0) > 0]
    zero = [r for r in valid if r.get("balance_usd", 0) == 0]
    negative = [r for r in valid if r.get("balance_usd", 0) < 0]

    log(f"\n{'='*60}")
    log(f"SCAN COMPLETE | 耗时 {elapsed:.0f}s ({elapsed/60:.1f} min)")
    log(f"{'='*60}")
    log(f"总 Key 数: {len(merged)}")
    log(f"有效: {len(valid)} | 正余额: {len(positive)} | 零余额: {len(zero)} | 欠费: {len(negative)}")

    if positive:
        total_usd = sum(r["balance_usd"] for r in positive)
        total_cny = sum(r["balance_cny"] for r in positive)
        log(f"\n正余额总价值: ${total_usd:.2f} / ¥{total_cny:.2f}")
        log(f"\nTop 10:")
        for i, r in enumerate(sorted(positive, key=lambda x: x["balance_usd"], reverse=True)[:10]):
            src = r["repos"][0]["repo"] if r.get("repos") else "N/A"
            log(f"  {i+1}. {r['key'][:20]}... | ${r['balance_usd']:.2f} | {src[:50]}")

    log(f"\n结果已保存: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
