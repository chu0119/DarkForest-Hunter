#!/usr/bin/env python3
"""
DarkForest Hunter — 统一扫描入口（重构后推荐用法）

整合三大扫描能力到单一 CLI：
  1. deepseek   单平台 DeepSeek 扫描（GitHub + 多源 + 验证查余额）
  2. multi      多平台 AI Key 扫描（DeepSeek/Kimi/智谱/通义/豆包等9+家）
  3. source     单数据源扫描（gist/issues/gitlab/huggingface/wayback 等）

替代旧的 8 个入口脚本（ultimate/full/fast/smart/stable/max/deep/expanded），
旧脚本保留在原位作为参考，不再维护。

示例:
  # 单平台 DeepSeek 全量扫描（默认，最常用）
  python run.py deepseek --proxy http://127.0.0.1:7897

  # 快速测试（15分钟，动态时间窗口优先抓最新提交）
  python run.py deepseek --max-duration 900

  # 多平台扫描（国内AI厂商，免费额度多可能有余额）
  python run.py multi --providers deepseek kimi qwen zhipu

  # 单数据源调试（只跑 HuggingFace）
  python run.py source --source huggingface

  # 查看可用数据源/平台
  python run.py --list-sources
  python run.py multi --list-providers
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _detect_proxy():
    """自动检测代理：命令行 > 环境变量 > 无"""
    return os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")


def _make_log(prefix=""):
    """带时间戳的日志函数"""
    def log(msg, level="info"):
        sym = {"warning": "⚠️", "error": "❌", "success": "✅"}.get(level, "ℹ️")
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {sym} {prefix}{msg}", flush=True)
    return log


# ──────────────────────────────────────────────────────────────────────────────
#  命令: deepseek
# ──────────────────────────────────────────────────────────────────────────────

def cmd_deepseek(args):
    """单平台 DeepSeek 扫描：GitHub Code Search + 多源 + 验证查余额"""
    from scanner_engine import ScannerEngine, build_active_queries

    proxy = args.proxy or _detect_proxy()
    log = _make_log()

    # 使用动态滚动时间窗口查询（P2-1：逆转产出下降）
    queries = build_active_queries()
    log(f"DeepSeek 扫描启动: {len(queries)} 条查询（含动态时间窗口）")
    log(f"代理: {proxy or '无'} | 并发: {args.concurrency} | 时长限制: {args.max_duration or '无'}s")

    engine = ScannerEngine(
        concurrency=args.concurrency,
        timeout=15,
        search_delay=2.5,
        scan_pages=args.pages,
        max_duration=args.max_duration,
        max_valid_keys=args.max_keys,
        output_dir="./results",
        log_callback=log,
        proxy=proxy,
    )

    t0 = time.time()
    results = engine.run(queries)
    elapsed = time.time() - t0

    valid = [r for r in results if r.get("valid")]
    positive = [r for r in valid if r.get("balance_usd", 0) > 0]
    log(f"完成 | 耗时 {elapsed:.0f}s | 有效 {len(valid)} | 正余额 {len(positive)}", "success")
    if positive:
        total_usd = sum(r["balance_usd"] for r in positive)
        log(f"正余额总价值: ${total_usd:.2f}", "success")


# ──────────────────────────────────────────────────────────────────────────────
#  命令: multi
# ──────────────────────────────────────────────────────────────────────────────

def cmd_multi(args):
    """多平台 AI Key 扫描（国内9+家AI厂商）"""
    from multi_provider_scan import MultiProviderScanner, DEFAULT_PROVIDERS

    proxy = args.proxy or _detect_proxy()

    if args.list_providers:
        # 复用 multi_provider_scan 的列表逻辑
        from providers import ALL_PROVIDERS
        print("\n支持的 AI 平台:")
        print("-" * 60)
        for p in ALL_PROVIDERS:
            mark = "✅" if p.enabled else "❌"
            print(f"{mark} {p.id:<12} {p.name:<18} {p.name_cn}")
        return

    providers = args.providers or DEFAULT_PROVIDERS
    scanner = MultiProviderScanner(providers, proxy)
    scanner.run(max_pages=args.max_pages)


# ──────────────────────────────────────────────────────────────────────────────
#  命令: source
# ──────────────────────────────────────────────────────────────────────────────

def cmd_source(args):
    """单数据源扫描（调试/定向扫描）"""
    import asyncio
    from scanner_engine import ScannerEngine

    proxy = args.proxy or _detect_proxy()
    log = _make_log(f"[{args.source}] ")

    engine = ScannerEngine(
        concurrency=args.concurrency,
        scan_pages=args.pages,
        max_duration=args.max_duration,
        output_dir="./results",
        log_callback=log,
        proxy=proxy,
    )

    log(f"单源扫描: {args.source}")
    t0 = time.time()
    results = engine.run_multi_source([args.source])
    elapsed = time.time() - t0
    log(f"完成 | 耗时 {elapsed:.0f}s | 发现 {len(results)} 个有效 key", "success")


# ──────────────────────────────────────────────────────────────────────────────
#  CLI 定义
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="DarkForest Hunter — DeepSeek/多平台 API Key 扫描器（统一入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", help="扫描模式")

    # 公共参数
    def _add_common(p):
        p.add_argument("--proxy", help="HTTP 代理地址 (默认读 HTTP_PROXY 环境变量)")
        p.add_argument("--concurrency", type=int, default=15, help="并发数 (默认 15)")

    # deepseek
    p_ds = sub.add_parser("deepseek", help="单平台 DeepSeek 扫描（推荐）")
    _add_common(p_ds)
    p_ds.add_argument("--pages", type=int, default=5, help="每条查询的搜索页数 (默认 5)")
    p_ds.add_argument("--max-duration", type=int, default=0, help="最大运行秒数 (0=不限)")
    p_ds.add_argument("--max-keys", type=int, default=0, help="达到N个有效key即停 (0=不限)")
    p_ds.set_defaults(func=cmd_deepseek)

    # multi
    p_multi = sub.add_parser("multi", help="多平台 AI Key 扫描")
    _add_common(p_multi)
    p_multi.add_argument("--providers", nargs="+", default=None,
                         help="平台列表 (默认9家国内AI厂商)")
    p_multi.add_argument("--max-pages", type=int, default=2, help="每查询页数 (默认 2)")
    p_multi.add_argument("--list-providers", action="store_true", help="列出支持的平台")
    p_multi.set_defaults(func=cmd_multi)

    # source
    p_src = sub.add_parser("source", help="单数据源扫描")
    _add_common(p_src)
    p_src.add_argument("--source", required=True,
                       help="数据源名 (gist/issues/commits/gitlab/gitee/huggingface/"
                            "pypi/npm/stackoverflow/docker/wayback/commoncrawl/...)")
    p_src.add_argument("--pages", type=int, default=3, help="搜索页数 (默认 3)")
    p_src.add_argument("--max-duration", type=int, default=0, help="最大运行秒数 (0=不限)")
    p_src.set_defaults(func=cmd_source)

    # --list-sources
    parser.add_argument("--list-sources", action="store_true", help="列出可用数据源")

    args = parser.parse_args()

    if args.list_sources:
        print("可用数据源 (用于 'python run.py source --source <名>'):")
        print("-" * 50)
        sources = [
            ("gist", "GitHub Gists"), ("issues", "GitHub Issues/PRs"),
            ("commits", "GitHub 提交历史"), ("gitlab", "GitLab"),
            ("gitee", "Gitee 码云"), ("huggingface", "HuggingFace"),
            ("pypi", "PyPI 注册表"), ("npm", "npm 注册表"),
            ("stackoverflow", "Stack Overflow"), ("docker", "Docker Hub"),
            ("wayback", "Wayback Machine"), ("commoncrawl", "Common Crawl"),
            ("github_raw", "GitHub 宽泛sk-搜索"), ("pastebin", "Pastebin"),
            ("reddit", "Reddit"), ("google_dork", "Google Dork"),
        ]
        for sid, name in sources:
            print(f"  {sid:<16} {name}")
        return

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
