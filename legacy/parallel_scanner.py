"""
并行扫描器 — 多平台并行扫描架构
支持分组扫描、结果合并、去重
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

from scanner_engine import ScannerEngine, load_tiered_queries, get_flat_queries
from scanners.base import extract_keys


class ResultMerger:
    """结果合并器（线程安全）"""

    def __init__(self):
        self.seen_keys = set()
        self.results = []
        self.lock = threading.Lock()

    def merge(self, new_results: list) -> int:
        """合并新结果，返回新增数量"""
        added = 0
        with self.lock:
            for r in new_results:
                key = r.get("key", "")
                if key and key not in self.seen_keys:
                    self.seen_keys.add(key)
                    self.results.append(r)
                    added += 1
        return added

    def get_all(self) -> list:
        """获取所有结果"""
        with self.lock:
            return list(self.results)


class PlatformScanner:
    """单平台扫描器"""

    def __init__(self, scanner_cls, scanner_kwargs: dict, queries: list = None,
                 timeout: int = 120, log_callback=None):
        self.scanner_cls = scanner_cls
        self.scanner_kwargs = scanner_kwargs
        self.queries = queries
        self.timeout = timeout
        self.log = log_callback or (lambda msg, level="info": print(msg))

    def scan(self) -> list:
        """执行扫描"""
        results = []
        try:
            scanner = self.scanner_cls(**self.scanner_kwargs)
            # 运行异步搜索
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if self.queries:
                    for q in self.queries:
                        if hasattr(scanner, '_stop_requested') and scanner._stop_requested:
                            break
                        search_results = loop.run_until_complete(scanner.search(q))
                        results.extend(search_results)
                else:
                    search_results = loop.run_until_complete(scanner.search())
                    results.extend(search_results)
            finally:
                loop.close()
        except Exception as e:
            self.log(f"Scanner error: {e}", "error")

        return results


class ParallelScanner:
    """多平台并行扫描控制器"""

    # 平台分组配置
    PLATFORM_GROUPS = {
        "ai_platforms": {
            "scanners": [
                ("replicate", "ReplicateScanner", {}),
                ("civitai", "CivitaiScanner", {}),
                ("together_ai", "TogetherAIScanner", {}),
                ("modal", "ModalScanner", {}),
                ("groq", "GroqScanner", {}),
                ("deepinfra", "DeepInfraScanner", {}),
                ("fal_ai", "FalAIScanner", {}),
            ],
            "timeout": 120,
        },
        "code_hosting": {
            "scanners": [
                ("gitlab", "GitLabScanner", {}),
                ("gitee", "GiteeScanner", {}),
                ("github_raw", "GitHubRawScanner", {}),
            ],
            "timeout": 180,
        },
        "package_registries": {
            "scanners": [
                ("npm", "NpmScanner", {}),
                ("pypi", "PyPIScanner", {}),
                ("docker", "DockerHubScanner", {}),
            ],
            "timeout": 90,
        },
        "search_social": {
            "scanners": [
                ("stackoverflow", "StackOverflowScanner", {}),
                ("reddit", "RedditScanner", {}),
                ("google_dork", "GoogleDorkScanner", {}),
            ],
            "timeout": 90,
        },
        "archive": {
            "scanners": [
                ("wayback", "WaybackScanner", {}),
                ("commoncrawl", "CommonCrawlScanner", {}),
            ],
            "timeout": 90,
        },
    }

    def __init__(self, max_workers: int = 5, log_callback=None):
        self.max_workers = max_workers
        self.log = log_callback or (lambda msg, level="info": print(msg))
        self.result_merger = ResultMerger()
        self.engine = ScannerEngine(log_callback=self.log_callback)

    def log_callback(self, msg, level="info"):
        """适配 ScannerEngine 的日志回调"""
        self.log(msg, level)

    def scan_group(self, group_name: str, group_config: dict) -> list:
        """扫描一个平台组"""
        self.log(f"\n{'='*50}")
        self.log(f"  [{group_name}] 开始并行扫描...")
        self.log(f"{'='*50}")

        scanners = group_config["scanners"]
        timeout = group_config.get("timeout", 120)
        all_results = []

        # 串行扫描同组内的 scanner（避免资源竞争）
        for scanner_name, scanner_cls_name, extra_kwargs in scanners:
            if self.engine._should_stop():
                break

            self.log(f"  [{scanner_name}] 扫描中...")

            # 获取 scanner 类
            scanner_cls = self._get_scanner_class(scanner_cls_name)
            if not scanner_cls:
                self.log(f"  [{scanner_name}] Scanner not found, skipping", "warning")
                continue

            # 创建并运行 scanner
            try:
                platform_scanner = PlatformScanner(
                    scanner_cls=scanner_cls,
                    scanner_kwargs={**extra_kwargs, "concurrency": 10, "timeout": timeout},
                    timeout=timeout,
                    log_callback=self.log
                )
                results = platform_scanner.scan()
                added = self.result_merger.merge(results)
                self.log(f"  [{scanner_name}] 完成: {len(results)} 发现, {added} 新增")
                all_results.extend(results)
            except Exception as e:
                self.log(f"  [{scanner_name}] 错误: {e}", "error")

        return all_results

    def scan_all_groups(self) -> list:
        """并行扫描所有平台组"""
        self.log(f"\n{'='*60}")
        self.log("多平台并行扫描启动")
        self.log(f"{'='*60}")

        t0 = time.time()
        all_results = []

        # 使用线程池并行扫描不同组
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for group_name, group_config in self.PLATFORM_GROUPS.items():
                if self.engine._should_stop():
                    break
                future = executor.submit(self.scan_group, group_name, group_config)
                futures[future] = group_name

            for future in as_completed(futures):
                group_name = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    self.log(f"Group {group_name} error: {e}", "error")

        elapsed = time.time() - t0
        final_results = self.result_merger.get_all()

        self.log(f"\n{'='*60}")
        self.log(f"多平台扫描完成: {elapsed:.0f}s | 发现 {len(final_results)} 个 Key")
        self.log(f"{'='*60}")

        return final_results

    def _get_scanner_class(self, class_name: str):
        """获取 scanner 类"""
        scanner_map = {
            "GitLabScanner": "scanners.gitlab",
            "GiteeScanner": "scanners.gitee",
            "GitHubRawScanner": "scanners.github_raw",
            "NpmScanner": "scanners.npm_registry",
            "PyPIScanner": "scanners.pypi",
            "DockerHubScanner": "scanners.docker",
            "StackOverflowScanner": "scanners.stackoverflow",
            "WaybackScanner": "scanners.wayback",
            "CommonCrawlScanner": "scanners.commoncrawl",
            "ReplicateScanner": "scanners.replicate",
            "CivitaiScanner": "scanners.ai_platforms",
            "TogetherAIScanner": "scanners.ai_platforms",
            "ModalScanner": "scanners.ai_platforms",
            "GroqScanner": "scanners.ai_platforms",
            "DeepInfraScanner": "scanners.ai_platforms",
            "FalAIScanner": "scanners.ai_platforms",
            "GoogleDorkScanner": "scanners.google_dork",
            "RedditScanner": "scanners.reddit",
        }

        module_path = scanner_map.get(class_name)
        if not module_path:
            return None

        try:
            import importlib
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        except Exception:
            return None


def run_parallel_scan(queries_file: str = "queries_v6.txt", max_workers: int = 5):
    """运行并行扫描"""
    import sys
    import os
    sys.path.insert(0, ".")

    def log(msg, level="info"):
        prefix = {"warning": "[!] ", "error": "[ERR] "}.get(level, "")
        t = time.strftime("%m-%d %H:%M:%S")
        print(f"[{t}] {prefix}{msg}", flush=True)

    # 加载查询
    tiered = load_tiered_queries(queries_file)
    flat = get_flat_queries(tiered)

    log(f"查询数: {len(tiered)}")
    log(f"并行 workers: {max_workers}")

    # 创建并行扫描器
    scanner = ParallelScanner(max_workers=max_workers, log_callback=log)

    # 运行扫描
    results = scanner.scan_all_groups()

    return results


if __name__ == "__main__":
    run_parallel_scan()
