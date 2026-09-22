#!/usr/bin/env python3
"""
DarkForest Hunter — 统一入口

唯一的扫描入口是 watch（24/7 持续多源扫描 + 验证 + 告警）：
  python -u run.py watch           # 挂机跑（TUI 面板）
  python -u run.py watch --no-tui  # 无头/后台跑（日志进 results/watch_session_*.log）
  python run.py                    # 裸启动 = watch

所有参数都有合理默认（config.ini 可覆盖），一条命令即包含全部：
35 平台验证 / 兜底探测默认开 / 新鲜度优先 / 收益自学习查询轮转。

辅助工具（非扫描入口）：
  python run.py metrics            # 产出画像（调参依据）
  python run.py maintenance        # 账本脱敏/清理/VACUUM
  python run.py --list-sources     # 可用数据源目录
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_loader import config as _cfg
from scanner_engine import AVAILABLE_SOURCES


def _proxy_is_reachable(proxy: str, timeout: float = 3.0) -> bool:
    """快速探测代理是否可用（能否真正转发请求到外网）。"""
    import requests
    try:
        requests.get("https://api.github.com",
                     proxies={"http": proxy, "https": proxy}, timeout=timeout)
        return True  # 收到任意 HTTP 响应 = 代理转发成功
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError,
            requests.exceptions.Timeout, OSError):
        return False  # 连不上代理本身
    except Exception:
        return True  # 非连接类异常(如 SSL/HTTP 错) → 代理通了，问题在目标侧


def _detect_local_proxy(timeout: float = 1.0) -> str:
    """无显式代理时，探测常见本地代理端口(Clash 7897 / V2Ray 10809 / 7890 等)。
    命中第一个可达的即返回；全不通返回空串。"""
    import requests
    for port in (7897, 10809, 7890, 10808, 1080, 8080):
        cand = f"http://127.0.0.1:{port}"
        try:
            requests.get("https://api.github.com",
                         proxies={"http": cand, "https": cand}, timeout=timeout)
            return cand
        except Exception:
            continue
    return ""


def _resolve_proxy(cli_proxy=None):
    """代理解析 + 回退: CLI > config.ini > 环境变量 > 探测本地端口 > 直连。
    选定的代理不可达时自动降级为直连(None)并告警。返回 proxy(str|None)。"""
    from config_loader import config
    log = _make_log()
    proxy = (cli_proxy or config.proxy_url
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    if not proxy:
        proxy = _detect_local_proxy()
        if proxy:
            log(f"未配置代理，自动探测到本地代理 {proxy}")
    if not proxy:
        log("无代理，使用直连")
        return None
    if _proxy_is_reachable(proxy):
        log(f"代理: {proxy}")
        return proxy
    log(f"代理 {proxy} 不可达，回退为直连", "warning")
    return None


def _make_log(prefix=""):
    """带时间戳的日志函数"""
    def log(msg, level="info"):
        sym = {"warning": "⚠️", "error": "❌", "success": "✅"}.get(level, "ℹ️")
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {sym} {prefix}{msg}", flush=True)
    return log


# ──────────────────────────────────────────────────────────────────────────────
#  命令: watch（唯一扫描入口）
# ──────────────────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    """检查进程是否存活（Windows 用 OpenProcess，POSIX 用 kill 0）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_watch_pidfile() -> bool:
    """单实例锁：results/watch.pid 记录存活实例 PID。

    返回 False = 已有存活实例（拒绝启动，防双开重复烧配额）。
    PID 文件残留但进程已死 → 覆盖，正常启动。
    """
    pid_path = os.path.join("results", "watch.pid")
    try:
        os.makedirs("results", exist_ok=True)
        if os.path.exists(pid_path):
            old = open(pid_path, encoding="ascii").read().strip()
            if old.isdigit() and int(old) != os.getpid() and _pid_alive(int(old)):
                print(f"[!] Watch 已在运行 (PID {old})。"
                      f"先运行 一键停止扫描.bat，或执行: "
                      f"powershell \"Stop-Process -Id {old}\"")
                return False
        with open(pid_path, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        print(f"[!] PID 文件读写失败({e})——继续启动,但可能双开")
        return True


def cmd_watch(args):
    """持续多源循环扫描 + TUI 面板（Ctrl+C 退出）"""
    if not _acquire_watch_pidfile():
        return
    from watch_tui import run_watch

    proxy = _resolve_proxy(args.proxy)
    # 数据源：命令行 > config.ini > 内置默认
    sources = args.sources or _cfg.watch_sources
    run_watch(
        proxy=proxy,
        concurrency=args.concurrency,
        interval=args.interval,
        min_balance=args.min_balance,
        sources=sources,
        include_github=args.include_github,
        once=args.once,
        verify_interval=args.verify_interval,
        verify_workers=args.verify_workers,
        reverify_budget=args.reverify_budget,
        hv_email_threshold=args.hv_email_threshold,
        hv_top_threshold=args.hv_top_threshold,
        shrink_warn_pct=args.shrink_warn_pct,
        commits_since_hours=args.commits_since_hours,
        reverify_zero_hours=args.reverify_zero_hours,
        allow_chat_probe=args.allow_chat_probe,
        probe_unclear=args.probe_unclear,
        proxy_subscription=args.proxy_subscription,
        github_pages=args.github_pages,
        fresh=args.fresh,
        no_tui=args.no_tui,
        shutdown_timeout=args.shutdown_timeout,
    )


def cmd_maintenance(args):
    """显式维护本地 SQLite：脱敏 invalid 明文，并按需清理过期记录。"""
    import store

    log = _make_log("[maintenance] ")
    conn = store.connect(args.db)
    try:
        redacted = store.redact_invalid_keys(conn)
        pruned = 0
        if args.prune_invalid_days > 0:
            pruned = store.prune_invalid_older_than(
                conn, days=args.prune_invalid_days)
        vacuum_state = "skipped"
        if args.vacuum:
            conn.execute("VACUUM")
            vacuum_state = "ok"
        log(f"redacted={redacted} pruned={pruned} vacuum={vacuum_state}", "success")
    finally:
        conn.close()


def cmd_metrics(args):
    """输出本地扫描账本的聚合产出画像，用于动态调参。"""
    import store

    conn = store.connect(args.db)
    try:
        m = store.ledger_metrics(conn, min_balance=args.min_balance)
    finally:
        conn.close()

    totals = m["totals"]
    candidates = totals["candidates"] or 1
    print("📊 扫描产出画像")
    print(f"ledger candidates={candidates} valid={totals['valid']} "
          f"invalid={totals['invalid']} "
          f"valid_rate={totals['valid'] / candidates:.2%} "
          f"high_value={totals['high_value']} "
          f"high_value_balance={totals['high_value_balance']:.2f}")

    print("\n按数据源:")
    for row in m["by_source"]:
        n = row["candidates"] or 1
        print(f"source={row['source']} candidates={row['candidates']} "
              f"valid={row['valid']} invalid={row['invalid']} "
              f"valid_rate={row['valid'] / n:.2%} "
              f"high_value={row['high_value']} "
              f"hv_rate={row['high_value'] / n:.2%} "
              f"hv_balance={row['high_value_balance']:.2f} "
              f"last_seen={row['last_seen'] or '-'}")

    print("\n按平台:")
    for row in m["by_provider"]:
        n = row["candidates"] or 1
        print(f"provider={row['provider']} candidates={row['candidates']} "
              f"valid={row['valid']} invalid={row['invalid']} "
              f"valid_rate={row['valid'] / n:.2%} "
              f"high_value={row['high_value']} "
              f"hv_rate={row['high_value'] / n:.2%} "
              f"hv_balance={row['high_value_balance']:.2f} "
              f"last_seen={row['last_seen'] or '-'}")

    print("\n按查询 (Top 10):")
    query_rows = sorted(
        (row for row in m["by_query"] if row["query"] != "unknown"),
        key=lambda row: (row["high_value_balance"], row["high_value"],
                         row["valid"], row["candidates"]),
        reverse=True,
    )[:10]
    if not query_rows:
        print("query=unknown（尚无带 query 归因的结果）")
    for row in query_rows:
        n = row["candidates"] or 1
        print(f"query={row['query']} candidates={row['candidates']} "
              f"valid={row['valid']} invalid={row['invalid']} "
              f"valid_rate={row['valid'] / n:.2%} "
              f"high_value={row['high_value']} "
              f"hv_rate={row['high_value'] / n:.2%} "
              f"hv_balance={row['high_value_balance']:.2f} "
              f"last_seen={row['last_seen'] or '-'}")

    print("\n验证状态:")
    for status, count in m["status_counts"].items():
        print(f"status={status} count={count}")

    # ── 近 24h 全链路漏斗:验证状态 / 平台错误率 / unknown 新格式样本 ──
    try:
        import store as _store
        conn = _store.connect(args.db)
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) n FROM keys "
                "WHERE last_seen > datetime('now','localtime','-1 day') "
                "GROUP BY status ORDER BY n DESC").fetchall()
            total = sum(r["n"] for r in rows) or 1
            print("\n近 24h 验证漏斗:")
            for r in rows:
                print(f"  {r['status'] or '-':<18} {r['n']:>6}  ({r['n'] / total:.1%})")
            rows = conn.execute(
                "SELECT provider, COUNT(*) n, SUM(status='error') err, "
                "SUM(status='rate_limited') rl FROM keys "
                "WHERE last_seen > datetime('now','localtime','-1 day') "
                "GROUP BY provider HAVING n >= 5 "
                "ORDER BY err + rl DESC LIMIT 8").fetchall()
            print("\n近 24h 平台错误/限流 TOP (验证≥5 次的平台):")
            for r in rows:
                print(f"  {r['provider'] or '-':<12} n={r['n']:<6} "
                      f"error={r['err'] or 0:<4} rate_limited={r['rl'] or 0}")
            rows = conn.execute(
                "SELECT DISTINCT key_preview FROM keys WHERE provider='unknown' "
                "AND last_seen > datetime('now','localtime','-7 day') LIMIT 8").fetchall()
            if rows:
                print("\nunknown 样本 (仅预览,用于发现未覆盖的新 key 格式):")
                for r in rows:
                    print(f"  {r['key_preview']}")
        finally:
            conn.close()
    except Exception as e:
        print(f"(漏斗统计失败: {e})")

    if getattr(args, "check_github", False):
        from scanner_engine import ScannerEngine
        health = ScannerEngine().github_token_health()
        print("\nGitHub 源健康:")
        print(f"github configured={health['configured']} valid={health['valid']} "
              f"invalid={health['invalid']} unknown={health['unknown']} "
              f"code_search_ready={health['code_search_ready']}")


# ──────────────────────────────────────────────────────────────────────────────
#  CLI 定义
# ──────────────────────────────────────────────────────────────────────────────

def main():
    # 裸启动 / 只带 flag → 默认 watch 主模式（唯一扫描入口）
    if len(sys.argv) == 1:
        sys.argv.append("watch")
    elif sys.argv[1].startswith("-") and sys.argv[1] not in ("-h", "--help", "--list-sources"):
        sys.argv.insert(1, "watch")

    parser = argparse.ArgumentParser(
        description="DarkForest Hunter — 泄露 AI API Key 持续扫描器（watch 唯一入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", help="模式")

    # 公共参数
    def _add_common(p):
        p.add_argument("--proxy", help="HTTP 代理地址 (默认读 config.ini [proxy].url > 环境变量 > 自动探测本地 Clash)")
        p.add_argument("--concurrency", type=int, default=_cfg.ds_concurrency, help="并发数")
        p.add_argument("--no-allow-chat-probe", action="store_false", dest="allow_chat_probe",
                       default=_cfg.allow_chat_probe,
                       help="关闭全平台 chat 探测确认（默认开：每 key 一次 max_tokens=1 最小请求，"
                            "实测口径校准账本，微额消耗）")
        p.add_argument("--no-probe-unclear", action="store_false", dest="probe_unclear",
                       default=_cfg.probe_unclear_platforms,
                       help="关闭不明确平台的兜底探测（魔搭/NVIDIA/LongCat 等 /models 公开平台，"
                            "默认允许一次 max_tokens=1 最小探测判定，关闭后这些平台只读模式下无法判定)")
        p.add_argument("--proxy-subscription", default=_cfg.proxy_subscription,
                       help="代理订阅链接 URL(支持 VMess/Trojan/Hysteria2/SS),解析多代理端点实现 per-IP pacing。"
                            "每个代理 IP 独立限速,某 IP 被 429 不影响其他 IP。HTTP/SOCKS 直链直接使用,"
                            "其他协议需本地 Clash/V2Ray 客户端转换。")
        p.add_argument("--verbose", action="store_true", help="详细日志（DEBUG 级诊断输出到控制台）")
        p.add_argument("--log-file", default=None, help="诊断日志文件路径（DEBUG 级，含完整诊断轨迹）")

    # watch
    p_watch = sub.add_parser("watch", help="24/7 持续多源扫描 + 验证 + 告警（唯一扫描入口）")
    _add_common(p_watch)
    p_watch.add_argument("--interval", type=int, default=_cfg.watch_interval,
                         help="每轮间隔秒数 (默认 300=5分钟)")
    p_watch.add_argument("--min-balance", type=float, default=_cfg.watch_min_balance,
                         help="CNY 余额阈值，仅记录高于此值的 key")
    p_watch.add_argument("--sources", nargs="+", default=None,
                         help="自定义数据源列表 (默认 config.ini watch.sources 或内置默认)")
    p_watch.add_argument("--include-github", action="store_true", default=_cfg.watch_include_github,
                         help="额外纳入 github_search 慢速全量源")
    p_watch.add_argument("--once", action="store_true",
                         help="只跑一轮（测试用）")
    p_watch.add_argument("--verify-interval", type=float, default=_cfg.watch_verify_interval,
                         help="验证请求间隔秒数 (默认 0.25,单 worker QPS ≈ 1/interval,准确性优先)")
    p_watch.add_argument("--verify-workers", type=int, default=_cfg.watch_verify_workers,
                         help="验证 worker 并发数 (默认 8,每 worker 独立 IP+Session,准确性优先)")
    p_watch.add_argument("--reverify-budget", type=int, default=_cfg.watch_reverify_budget,
                         help="每日重验预算 (默认 1500)")
    p_watch.add_argument("--reverify-zero-hours", type=int, default=_cfg.watch_reverify_zero_interval_hours,
                         help="0 余额 key 重验间隔小时 (默认 168=7天)")
    p_watch.add_argument("--hv-email-threshold", type=float, default=_cfg.watch_hv_email_threshold,
                         help="高价值邮件阈值 CNY (默认 5)")
    p_watch.add_argument("--hv-top-threshold", type=float, default=_cfg.watch_hv_top_threshold,
                         help="重高价值阈值 CNY (默认 10)")
    p_watch.add_argument("--shrink-warn-pct", type=float, default=_cfg.watch_shrink_warn_pct,
                         help="缩水预警百分比 (默认 30)")
    p_watch.add_argument("--commits-since-hours", type=int, default=_cfg.watch_commits_since_hours,
                         help="github_commits 源时间窗口小时数 (默认 168=最近1周提交; 0=全量)")
    p_watch.add_argument("--github-pages", type=int, default=1,
                         help="github_search 每查询页数 (默认 1≈快轮次；2=更全但慢一倍)")
    p_watch.add_argument("--fresh", action="store_true",
                         help="查询轮次从头开始（默认自动续上次轮次；不清除已捕获 key）")
    p_watch.add_argument("--no-tui", action="store_true",
                         help="无头模式：跳过 TUI，日志输出到 stdout（适合后台运行/监控）")
    p_watch.add_argument("--shutdown-timeout", type=float, default=30.0,
                         help="退出时等待在途验证的秒数（--once 自动至少 120s）")
    p_watch.set_defaults(func=cmd_watch)

    # maintenance
    p_maint = sub.add_parser("maintenance", help="维护本地 SQLite 账本")
    p_maint.add_argument("--db", default="results/darkforest.db",
                         help="SQLite 账本路径 (默认 results/darkforest.db)")
    p_maint.add_argument("--prune-invalid-days", type=int, default=0,
                         help="删除早于 N 天的确定性 invalid 行 (0=只脱敏不删除)")
    p_maint.add_argument("--vacuum", action="store_true",
                         help="清理后重建数据库文件以回收磁盘空间")
    p_maint.set_defaults(func=cmd_maintenance)

    # metrics
    p_metrics = sub.add_parser("metrics", help="查看 SQLite 聚合产出画像")
    p_metrics.add_argument("--db", default="results/darkforest.db",
                           help="SQLite 账本路径 (默认 results/darkforest.db)")
    p_metrics.add_argument("--min-balance", type=float, default=1.0,
                           help="高价值余额阈值 (默认 ¥1)")
    p_metrics.add_argument("--check-github", action="store_true",
                           help="探测配置的 GitHub token 健康状态（聚合输出，不显示 token）")
    p_metrics.set_defaults(func=cmd_metrics)

    # --list-sources
    parser.add_argument("--list-sources", action="store_true", help="列出可用数据源")

    args = parser.parse_args()

    # 结构化诊断日志（独立于各子命令的用户叙事 log_callback）
    from logging_setup import configure_logging
    configure_logging(
        verbose=getattr(args, "verbose", False),
        log_file=getattr(args, "log_file", None),
    )

    if args.list_sources:
        print("可用数据源 (用于 'python run.py watch --sources <名> ...'):")
        print("-" * 50)
        for sid, name in AVAILABLE_SOURCES.items():
            print(f"  {sid:<16} {name}")
        return

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
