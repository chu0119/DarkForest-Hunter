"""Scanner package — all live data-source scanners inherit BaseScanner.

注册表口径：本模块导出 scanner_engine._get_scanner_registry 实际调度用的全部
扫描器类，加上 base 层的 key 提取/过滤/去重工具函数。新增扫描器时务必在此登记。
"""
from .base import (
    BaseScanner,
    dedup_results,
    extract_keys,
    is_bad_key,
)
from .docker import DockerHubScanner
from .github_commits import CommitsScanner
from .github_events import EventsMonitor
from .github_gist import GistScanner
from .github_issues import IssuesScanner
from .github_raw import GitHubRawScanner
from .gitlab import GitLabScanner
from .huggingface import HuggingFaceScanner
from .npm_registry import NpmScanner
from .paste_sites import PasteSiteScanner, SiteDorkScanner

__all__ = [
    # base
    "BaseScanner", "extract_keys", "is_bad_key", "dedup_results",
    # GitHub family
    "GistScanner", "IssuesScanner", "CommitsScanner", "GitHubRawScanner",
    "EventsMonitor",
    # code hosts
    "GitLabScanner",
    # AI hubs
    "HuggingFaceScanner",
    # package registries
    "NpmScanner",
    # paste / search
    "PasteSiteScanner", "SiteDorkScanner",
    # containers
    "DockerHubScanner",
]
