"""
多平台 AI Key 扫描器
支持国内主流 AI 模型的 Key 扫描和验证
"""

import asyncio
import aiohttp
import json
import os
import re
import time
import subprocess
import sys
from datetime import datetime
from typing import Optional

from providers import (
    ALL_PROVIDERS, ACTIVE_PROVIDERS, PROVIDER_MAP,
    UnifiedKeyMatcher, UnifiedKeyVerifier, QueryGenerator,
    KeyResult, VerifyResult,
    DEEPSEEK, KIMI, ZHIPU, QWEN, MINIMAX, DOUBAO,
    BAICHUAN, YI, XIAOMI, STEPFUN, SENSERNOVA, CLAUDE,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════════════════════

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

# 默认使用的 providers（可自定义）
DEFAULT_PROVIDERS = [
    "deepseek",
    "kimi",
    "zhipu",
    "qwen",
    "minimax",
    "doubao",
    "baichuan",
    "yi",
    "stepfun",
]


# ═══════════════════════════════════════════════════════════════════════════════
#  多平台 Key 扫描器
# ═══════════════════════════════════════════════════════════════════════════════

class MultiProviderScanner:
    """多平台 AI Key 扫描器"""

    def __init__(self, providers: list = None, proxy: str = None):
        """
        初始化扫描器

        Args:
            providers: 要扫描的 provider ID 列表
            proxy: HTTP 代理地址
        """
        self.proxy = proxy or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")

        # 设置 providers
        if providers:
            self.providers = [PROVIDER_MAP[p] for p in providers if p in PROVIDER_MAP]
        else:
            self.providers = ACTIVE_PROVIDERS

        self.matcher = UnifiedKeyMatcher(self.providers)
        self.verifier = UnifiedKeyVerifier(self.providers, self.proxy)

        # 扫描统计
        self.stats = {
            "total_keys_found": 0,
            "valid_keys": 0,
            "total_balance_usd": 0,
            "by_provider": {},
        }

        # 加载已有的 key（用于去重）—— 必须在 self.log 可用后调用
        self.existing_keys = set()
        self._load_existing_keys()

    def _load_existing_keys(self):
        """加载已有的 key 用于去重。
        兼容多种结果文件格式：
          - 顶层 dict 带 "results"/"valid_keys" 列表
          - 顶层 list（旧格式）
        """
        if not os.path.isdir(RESULTS_DIR):
            return
        for filename in os.listdir(RESULTS_DIR):
            # 匹配 *_result.json 与 *_keys_*.json 两种命名
            if not (filename.endswith("_result.json") or
                    filename.startswith("multi_provider_keys_")):
                continue
            filepath = os.path.join(RESULTS_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 统一抽成 key 列表
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = (data.get("results") or
                             data.get("valid_keys") or [])
                for key_info in items:
                    if isinstance(key_info, dict):
                        k = key_info.get("key", "")
                        if k:
                            self.existing_keys.add(k)
            except Exception:
                pass
        if self.existing_keys:
            self.log(f"  已加载 {len(self.existing_keys)} 个已有 Key（用于去重）")

    def log(self, msg: str, level: str = "info"):
        """日志输出"""
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {
            "info": "ℹ️",
            "success": "✅",
            "warning": "⚠️",
            "error": "❌",
            "scan": "🔍",
        }.get(level, "ℹ️")
        print(f"[{ts}] {prefix} {msg}")

    # ──────────────────────────────────────────────────────────────────────────
    #  GitHub 代码搜索
    # ──────────────────────────────────────────────────────────────────────────

    def _get_github_token(self) -> Optional[str]:
        """获取 GitHub token"""
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            return token

        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _github_search_sync(self, query: str, token: str,
                             max_pages: int = 3) -> list:
        """执行 GitHub 代码搜索 (同步版本，使用 requests)"""
        import requests as req

        results = []
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3.text-match+json",
        }

        for page in range(1, max_pages + 1):
            url = "https://api.github.com/search/code"
            params = {"q": query, "per_page": 100, "page": page}

            try:
                resp = req.get(url, headers=headers, params=params, timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])
                    if not items:
                        break

                    for item in items:
                        # 从 text_matches 中提取 key
                        text_matches = item.get("text_matches", [])
                        for match in text_matches:
                            fragment = match.get("fragment", "")
                            if fragment:
                                # 使用 matcher 提取 key
                                matched_keys = self.matcher.match_keys(fragment)
                                for provider_id, keys in matched_keys.items():
                                    for key in keys:
                                        results.append({
                                            "key": key,
                                            "provider": provider_id,
                                            "url": item.get("html_url", ""),
                                            "repo": item.get("repository", {}).get("full_name", ""),
                                            "file": item.get("path", ""),
                                            "context": fragment[:200],
                                        })

                    # Rate limit: Code Search API 10 req/min
                    time.sleep(6.5)
                elif resp.status_code == 403:
                    # Rate limited
                    reset_time = resp.headers.get("X-RateLimit-Reset")
                    if reset_time:
                        wait = max(int(reset_time) - int(time.time()), 10)
                        self.log(f"GitHub API 限速，等待 {wait}s...", "warning")
                        time.sleep(wait)
                    else:
                        time.sleep(60)
                elif resp.status_code == 422:
                    # Validation failed
                    break
                else:
                    break

            except Exception as e:
                self.log(f"搜索出错: {e}", "error")
                time.sleep(5)

        return results

    def scan_github(self, queries: list = None, max_pages: int = 2) -> list:
        """扫描 GitHub"""
        if not queries:
            queries = self._generate_github_queries()

        token = self._get_github_token()
        if not token:
            self.log("未找到 GitHub token，跳过 GitHub 扫描", "warning")
            return []

        self.log(f"开始 GitHub 扫描，共 {len(queries)} 个查询...", "scan")
        all_results = []

        for i, query_info in enumerate(queries):
            query = query_info["query"] if isinstance(query_info, dict) else query_info
            provider = query_info.get("provider", "unknown") if isinstance(query_info, dict) else "unknown"

            self.log(f"  [{i+1}/{len(queries)}] 搜索: {query[:50]}...")
            results = self._github_search_sync(query, token, max_pages)

            # 标记 provider
            for r in results:
                if r["provider"] == "unknown":
                    r["provider"] = provider

            all_results.extend(results)
            self.log(f"  找到 {len(results)} 个候选 Key")

        # 去重
        seen = set()
        unique_results = []
        for r in all_results:
            key_hash = f"{r['provider']}:{r['key']}"
            if key_hash not in seen:
                seen.add(key_hash)
                unique_results.append(r)

        self.log(f"GitHub 扫描完成: 共 {len(unique_results)} 个唯一候选 Key", "success")
        return unique_results

    def _generate_github_queries(self) -> list:
        """生成 GitHub 搜索查询"""
        queries = []

        for provider in self.providers:
            provider_queries = QueryGenerator.generate_for_provider(provider, 15)
            for q in provider_queries:
                queries.append({
                    "query": q,
                    "provider": provider.id,
                    "priority": provider.priority,
                })

        # 按优先级排序
        queries.sort(key=lambda x: x["priority"], reverse=True)
        return queries

    # ──────────────────────────────────────────────────────────────────────────
    #  Key 验证
    # ──────────────────────────────────────────────────────────────────────────

    def verify_keys(self, candidates: list) -> list:
        """验证候选 keys (并发版本，用 ThreadPoolExecutor)"""
        self.log(f"开始验证 {len(candidates)} 个候选 Key...", "scan")
        results = []

        # 分离已存在的 key
        new_candidates = []
        for c in candidates:
            if c["key"] not in self.existing_keys:
                new_candidates.append(c)

        if len(new_candidates) < len(candidates):
            self.log(f"跳过 {len(candidates) - len(new_candidates)} 个已有 Key")

        if not new_candidates:
            self.log("没有新的候选 Key 需要验证", "warning")
            return []

        # 并发验证（原来逐个串行，100+ key 要数分钟；现用线程池）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        total = len(new_candidates)
        done = [0]
        lock = threading.Lock()

        def _verify_one(candidate):
            # 加微小间隔，避免高频请求触发 API 封锁(如 DeepSeek ConnectionReset 10054)
            import time as _t, random as _r
            _t.sleep(_r.uniform(0.1, 0.3))
            r = self.verifier.verify_key(candidate["key"], candidate["provider"])
            r["url"] = candidate.get("url", "")
            r["repo"] = candidate.get("repo", "")
            r["file"] = candidate.get("file", "")
            r["context"] = candidate.get("context", "")
            return r

        # 并发降到 5（原 15 会导致 DeepSeek API 连接被封锁 ConnectionReset 10054）
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_verify_one, c): c for c in new_candidates}
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=30)
                except Exception as e:
                    c = futures[future]
                    result = {
                        "key": c["key"], "provider": c["provider"],
                        "status": VerifyResult.ERROR.value,
                        "balance": None, "message": str(e)[:80],
                        "url": c.get("url", ""), "repo": c.get("repo", ""),
                        "file": c.get("file", ""), "context": c.get("context", ""),
                    }
                results.append(result)

                with lock:
                    done[0] += 1
                    i = done[0]
                    status = result["status"]
                    key = result["key"]
                    provider = result["provider"]
                    balance = result.get("balance") or 0

                    if status == "valid_active":
                        self.log(f"  ✅ [{provider}] {key[:10]}...{key[-4:]} -> 余额 ${balance:.2f}", "success")
                    elif status == "valid_zero":
                        self.log(f"  ⚠️ [{provider}] {key[:10]}...{key[-4:]} -> 有效但余额为0")
                    elif status == "valid_no_balance":
                        self.log(f"  ℹ️ [{provider}] {key[:10]}...{key[-4:]} -> 有效")

                    if i % 20 == 0:
                        self.log(f"  已验证 {i}/{total} 个 Key...")

        self.log(f"验证完成: {len(results)} 个 Key", "success")
        return results

    # ──────────────────────────────────────────────────────────────────────────
    #  结果保存
    # ──────────────────────────────────────────────────────────────────────────

    def save_results(self, results: list, provider: str = "multi"):
        """保存扫描结果"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{provider}_keys_{timestamp}.json"
        filepath = os.path.join(RESULTS_DIR, filename)

        # 分类结果
        valid_keys = [r for r in results if r["status"] in (
            VerifyResult.VALID_ACTIVE.value,
            VerifyResult.VALID_ZERO.value,
            VerifyResult.VALID_NO_BALANCE.value,
        )]

        total_balance = sum(r.get("balance", 0) or 0 for r in valid_keys)

        output = {
            "scan_time": datetime.now().isoformat(),
            "provider": provider,
            "total_keys_found": len(results),
            "valid_keys_count": len(valid_keys),
            "total_balance_usd": total_balance,
            "results": [r for r in results if isinstance(r, dict)],
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        self.log(f"结果已保存: {filepath}", "success")
        return filepath

    # ──────────────────────────────────────────────────────────────────────────
    #  统计报告
    # ──────────────────────────────────────────────────────────────────────────

    def print_report(self, results: list):
        """打印扫描报告"""
        print("\n" + "=" * 60)
        print("📊 多平台扫描报告")
        print("=" * 60)

        # 按 provider 统计
        by_provider = {}
        for r in results:
            provider = r.get("provider", "unknown")
            if provider not in by_provider:
                by_provider[provider] = {"total": 0, "valid": 0, "balance": 0}

            by_provider[provider]["total"] += 1
            if r.get("status") in ("valid_active", "valid_zero", "valid_no_balance"):
                by_provider[provider]["valid"] += 1
                by_provider[provider]["balance"] += r.get("balance", 0) or 0

        # 打印每个 provider 的统计
        print("\n📌 各平台统计:")
        print("-" * 50)
        print(f"{'平台':<15} {'发现':>8} {'有效':>8} {'余额':>10}")
        print("-" * 50)

        total_found = 0
        total_valid = 0
        total_balance = 0

        for provider_id, stats in sorted(by_provider.items()):
            provider_name = PROVIDER_MAP.get(provider_id, type("", (), {"name": provider_id})()).name
            print(f"{provider_name:<15} {stats['total']:>8} {stats['valid']:>8} ${stats['balance']:>9.2f}")
            total_found += stats["total"]
            total_valid += stats["valid"]
            total_balance += stats["balance"]

        print("-" * 50)
        print(f"{'总计':<15} {total_found:>8} {total_valid:>8} ${total_balance:>9.2f}")

        # 有效 key 列表
        valid_keys = [r for r in results if r.get("status") in ("valid_active", "valid_zero", "valid_no_balance")]
        if valid_keys:
            print("\n🔑 有效 Key 列表:")
            print("-" * 50)
            for r in sorted(valid_keys, key=lambda x: x.get("balance", 0) or 0, reverse=True):
                provider_name = PROVIDER_MAP.get(r["provider"], type("", (), {"name": r["provider"]})()).name
                balance = r.get("balance", 0) or 0
                print(f"  [{provider_name}] {r['key'][:15]}...{r['key'][-4:]} -> ${balance:.2f}")

        print("\n" + "=" * 60)

    # ──────────────────────────────────────────────────────────────────────────
    #  主扫描流程
    # ──────────────────────────────────────────────────────────────────────────

    def run(self, queries: list = None, max_pages: int = 2):
        """执行完整扫描流程"""
        start_time = time.time()

        self.log("=" * 50)
        self.log("🚀 多平台 AI Key 扫描器启动")
        self.log(f"   支持平台: {', '.join(p.name for p in self.providers)}")
        self.log("=" * 50)

        # Step 1: GitHub 搜索
        candidates = self.scan_github(queries, max_pages)

        if not candidates:
            self.log("未找到候选 Key，扫描结束", "warning")
            return []

        # Step 2: 验证 Key
        results = self.verify_keys(candidates)

        # Step 3: 保存结果
        self.save_results(results, "multi_provider")

        # Step 4: 打印报告
        self.print_report(results)

        # 扫描统计
        elapsed = time.time() - start_time
        self.log(f"\n⏱️ 总耗时: {elapsed:.1f}s")

        return results


# ═══════════════════════════════════════════════════════════════════════════════
#  单平台快速扫描
# ═══════════════════════════════════════════════════════════════════════════════

class SingleProviderScanner:
    """单平台快速扫描器"""

    def __init__(self, provider_id: str, proxy: str = None):
        if provider_id not in PROVIDER_MAP:
            raise ValueError(f"未知的 provider: {provider_id}")

        self.provider = PROVIDER_MAP[provider_id]
        self.scanner = MultiProviderScanner([provider_id], proxy)

    def run(self, queries: list = None, max_pages: int = 3):
        """执行单平台扫描"""
        return self.scanner.run(queries, max_pages)


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="多平台 AI Key 扫描器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
支持的平台:
  deepseek   - DeepSeek (深度求索)
  kimi       - Kimi (月之暗面)
  zhipu      - Zhipu AI (智谱AI)
  qwen       - Qwen (通义千问)
  minimax    - MiniMax (稀宇科技)
  doubao     - Doubao (字节豆包)
  baichuan   - Baichuan (百川智能)
  yi         - 01.AI (零一万物)
  stepfun    - StepFun (阶跃星辰)
  sensnova   - SenseNova (商汤日日新)
  claude     - Claude (Anthropic)

示例:
  # 扫描所有平台
  python multi_provider_scan.py

  # 扫描指定平台
  python multi_provider_scan.py --providers deepseek kimi qwen

  # 只扫描 DeepSeek
  python multi_provider_scan.py --single deepseek

  # 使用代理
  python multi_provider_scan.py --proxy http://127.0.0.1:7890
        """
    )

    parser.add_argument("--providers", "-p", nargs="+", default=None,
                        help="要扫描的平台列表 (默认: 所有平台)")
    parser.add_argument("--single", "-s", type=str, default=None,
                        help="只扫描指定的单个平台")
    parser.add_argument("--proxy", type=str, default=None,
                        help="HTTP 代理地址")
    parser.add_argument("--max-pages", type=int, default=2,
                        help="每个查询的最大页数 (默认: 2)")
    parser.add_argument("--list-providers", action="store_true",
                        help="列出所有支持的平台")

    args = parser.parse_args()

    # 列出平台
    if args.list_providers:
        print("\n支持的 AI 平台:")
        print("-" * 60)
        print(f"{'ID':<12} {'名称':<20} {'中文名':<20}")
        print("-" * 60)
        for p in ALL_PROVIDERS:
            status = "✅" if p.enabled else "❌"
            print(f"{status} {p.id:<10} {p.name:<20} {p.name_cn:<20}")
        return

    # 确定要扫描的平台
    if args.single:
        providers = [args.single]
    elif args.providers:
        providers = args.providers
    else:
        providers = DEFAULT_PROVIDERS

    # 执行扫描 —— run() 是同步函数，直接调用（不要用 asyncio.run 包裹）
    scanner = MultiProviderScanner(providers, args.proxy)
    scanner.run(max_pages=args.max_pages)


if __name__ == "__main__":
    main()
