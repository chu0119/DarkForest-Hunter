"""配置加载器 — 从 config.ini 读取代理 / 运行参数 / 凭据。

config.ini 已被 .gitignore 排除，不会同步到 git。
首次使用请复制 config.ini.example 为 config.ini 并填入。

读取优先级（高→低）：
  1. 命令行参数（run.py 的 argparse）
  2. config.ini
  3. 环境变量（仅代理）
  4. 代码内默认值
"""
from __future__ import annotations

import configparser
import os
import sys

_CONFIG_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"),
    os.path.join(os.getcwd(), "config.ini"),
]


class Config:
    """统一配置访问点。单例模式，首次访问时加载。"""

    def __init__(self):
        # interpolation=None:配置值(URL 编码代理密码、SMTP 口令等)常含裸 %,
        # 默认 BasicInterpolation 会抛 InterpolationSyntaxError 被 _get 吞成
        # 空串——代理/邮件静默失效且无任何告警
        self._config = configparser.ConfigParser(interpolation=None)
        self._loaded = False

        # 凭据
        self._github_tokens: list[str] = []
        self._gitlab_token: str = ""
        self._gitee_token: str = ""
        self._hf_token: str = ""
        self._docker_token: str = ""

        # 代理
        self._proxy_url: str = ""
        self._proxy_subscription: str = ""

        # watch 运行参数
        self._watch_interval: int = 300
        self._watch_min_balance: float = 1.0
        self._watch_verify_interval: float = 0.5
        self._watch_verify_workers: int = 4
        self._watch_reverify_budget: int = 1500
        self._watch_reverify_zero_interval_hours: int = 168
        self._watch_hv_email_threshold: float = 5.0
        self._watch_hv_top_threshold: float = 10.0
        self._watch_shrink_warn_pct: float = 30.0
        self._watch_commits_since_hours: int = 168
        self._watch_concurrency: int = 15
        self._watch_include_github: bool = False
        self._watch_sources: list[str] | None = None

        # deepseek 运行参数
        self._allow_chat_probe: bool = True
        self._probe_unclear_platforms: bool = True
        self._ds_pages: int = 5
        self._ds_max_duration: int = 0
        self._ds_max_keys: int = 0
        self._ds_concurrency: int = 15
        self._ds_timeout: int = 15
        self._ds_search_delay: float = 2.5

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        for path in _CONFIG_PATHS:
            if os.path.exists(path):
                try:
                    self._config.read(path, encoding="utf-8")
                except configparser.Error as e:
                    # 文件级损坏(缺节头/坏行)此前直接炸掉首次配置访问——
                    # 降级为"无配置+默认值"并留一条诊断
                    print(f"[config] 配置文件解析失败({path}): {e};使用内置默认值",
                          file=sys.stderr)
                break

        # ── 代理 ──
        self._proxy_url = self._get("proxy", "url", "")
        self._proxy_subscription = self._get("proxy", "subscription", "")

        # ── watch 参数 ──
        self._watch_interval = self._getint("watch", "interval", 300)
        self._watch_min_balance = self._getfloat("watch", "min_balance", 1.0)
        self._watch_verify_interval = self._getfloat("watch", "verify_interval", 0.25)
        self._watch_verify_workers = self._getint("watch", "verify_workers", 8)
        self._watch_reverify_budget = self._getint("watch", "reverify_budget_per_day", 1500)
        self._watch_reverify_zero_interval_hours = self._getint("watch", "reverify_zero_interval_hours", 168)
        self._watch_hv_email_threshold = self._getfloat("watch", "hv_email_threshold", 5.0)
        self._watch_hv_top_threshold = self._getfloat("watch", "hv_top_threshold", 10.0)
        self._watch_shrink_warn_pct = self._getfloat("watch", "shrink_warn_pct", 30.0)
        self._watch_commits_since_hours = self._getint("watch", "commits_since_hours", 168)
        self._watch_concurrency = self._getint("watch", "concurrency", 15)
        self._watch_include_github = self._getboolean("watch", "include_github", False)
        src_raw = self._get("watch", "sources", "")
        if src_raw:
            self._watch_sources = [s.strip() for s in src_raw.split() if s.strip()]

        # ── deepseek 参数 ──
        self._allow_chat_probe = self._getboolean("verification", "allow_chat_probe", True)
        self._probe_unclear_platforms = self._getboolean(
            "verification", "probe_unclear_platforms", True)
        self._ds_pages = self._getint("deepseek", "pages", 5)
        self._ds_max_duration = self._getint("deepseek", "max_duration", 0)
        self._ds_max_keys = self._getint("deepseek", "max_keys", 0)
        self._ds_concurrency = self._getint("deepseek", "concurrency", 15)
        self._ds_timeout = self._getint("deepseek", "timeout", 15)
        self._ds_search_delay = self._getfloat("deepseek", "search_delay", 2.5)

        # ── 凭据 ──
        raw = self._get("github", "token", "")
        if raw:
            self._github_tokens = [t.strip() for t in raw.split(",") if t.strip()]
        self._gitlab_token = self._get("gitlab", "token", "")
        self._gitee_token = self._get("gitee", "token", "")
        self._hf_token = self._get("huggingface", "token", "")
        self._docker_token = self._get("docker", "token", "")

        # ── SMTP 邮件通知 ──
        self._smtp_server = self._get("SMTP", "smtp_server", "")
        self._smtp_port = self._getint("SMTP", "smtp_port", 465)
        self._smtp_user = self._get("SMTP", "smtp_user", "")
        self._smtp_password = self._get("SMTP", "smtp_password", "")
        self._smtp_from = self._get("SMTP", "smtp_from", "") or self._smtp_user
        self._smtp_sender_name = self._get("SMTP", "smtp_sender_name", "DarkForest Hunter")
        raw_rcpt = self._get("SMTP", "smtp_recipients", "")
        self._smtp_recipients = [r.strip() for r in raw_rcpt.split(",") if r.strip()]
        # 自动启用：SMTP 配置完整且 email_alert_enabled 未显式设为 false
        enabled_raw = self._get("SMTP", "email_alert_enabled", "")
        if enabled_raw.lower() in ("false", "0", "no"):
            self._email_alert_enabled = False
        elif enabled_raw.lower() in ("true", "1", "yes"):
            self._email_alert_enabled = bool(self._smtp_server and self._smtp_user and self._smtp_recipients)
        else:
            # 未配置时：SMTP 完整则自动开启
            self._email_alert_enabled = bool(self._smtp_server and self._smtp_user and self._smtp_recipients)
        # 安全：邮件默认不发送完整 key（key 可被他方截获）。
        # 仅当显式 email_include_full_key = true 时，才在邮件正文包含完整 key。
        fk_raw = self._get("SMTP", "email_include_full_key", "").strip().lower()
        self._email_include_full_key = fk_raw in ("true", "1", "yes")
        # 同 key 邮件去重窗口（小时）：同一 key 在窗口内只发一次，避免启动重验刷屏。
        try:
            self._email_dedup_hours = float(self._get("SMTP", "email_dedup_hours", ""))
        except (TypeError, ValueError):
            self._email_dedup_hours = 1.0  # 默认 1 小时
        if self._email_dedup_hours <= 0:
            self._email_dedup_hours = 1.0

    # ── 通用读取 ──
    def _get(self, section: str, key: str, default: str = "") -> str:
        try:
            return self._config.get(section, key, fallback=default)
        except (configparser.Error, KeyError):
            return default

    def _getint(self, section: str, key: str, default: int = 0) -> int:
        try:
            return self._config.getint(section, key, fallback=default)
        except (configparser.Error, ValueError):
            return default

    def _getfloat(self, section: str, key: str, default: float = 0.0) -> float:
        try:
            return self._config.getfloat(section, key, fallback=default)
        except (configparser.Error, ValueError):
            return default

    def _getboolean(self, section: str, key: str, default: bool = False) -> bool:
        try:
            return self._config.getboolean(section, key, fallback=default)
        except (configparser.Error, ValueError):
            return default

    # ── 代理 ──
    @property
    def proxy_url(self) -> str:
        """config.ini 中配置的代理地址（可能为空）。"""
        self._load()
        return self._proxy_url

    @property
    def proxy_subscription(self) -> str:
        """订阅链接 URL,用于解析多代理端点(per-IP pacing)。"""
        self._load()
        return self._proxy_subscription

    # ── watch 参数 ──
    @property
    def watch_interval(self) -> int:
        self._load()
        return self._watch_interval

    @property
    def watch_min_balance(self) -> float:
        self._load()
        return self._watch_min_balance

    @property
    def watch_verify_interval(self) -> float:
        self._load()
        return self._watch_verify_interval

    @property
    def watch_verify_workers(self) -> int:
        self._load()
        return self._watch_verify_workers

    @property
    def watch_reverify_budget(self) -> int:
        self._load()
        return self._watch_reverify_budget

    @property
    def watch_reverify_zero_interval_hours(self) -> int:
        self._load()
        return self._watch_reverify_zero_interval_hours

    @property
    def watch_hv_email_threshold(self) -> float:
        self._load()
        return self._watch_hv_email_threshold

    @property
    def watch_hv_top_threshold(self) -> float:
        self._load()
        return self._watch_hv_top_threshold

    @property
    def watch_shrink_warn_pct(self) -> float:
        self._load()
        return self._watch_shrink_warn_pct

    @property
    def watch_commits_since_hours(self) -> int:
        """github_commits 源时间窗口（小时，默认 2=每轮只扫最近 2 小时提交）。"""
        self._load()
        return self._watch_commits_since_hours

    @property
    def watch_concurrency(self) -> int:
        self._load()
        return self._watch_concurrency

    @property
    def watch_include_github(self) -> bool:
        self._load()
        return self._watch_include_github

    @property
    def watch_sources(self) -> list[str] | None:
        """自定义数据源列表，None 表示使用默认。"""
        self._load()
        return self._watch_sources

    # ── deepseek 参数 ──
    @property
    def allow_chat_probe(self) -> bool:
        self._load()
        return self._allow_chat_probe

    @property
    def probe_unclear_platforms(self) -> bool:
        """models 公开无法只读判定的平台(魔搭/NVIDIA/LongCat)是否允许
        兜底发一次 max_tokens=1 最小探测。默认 True——不探测则这些平台
        在只读模式下永远 ERROR。"""
        self._load()
        return self._probe_unclear_platforms

    @property
    def ds_pages(self) -> int:
        self._load()
        return self._ds_pages

    @property
    def ds_max_duration(self) -> int:
        self._load()
        return self._ds_max_duration

    @property
    def ds_max_keys(self) -> int:
        self._load()
        return self._ds_max_keys

    @property
    def ds_concurrency(self) -> int:
        self._load()
        return self._ds_concurrency

    @property
    def ds_timeout(self) -> int:
        self._load()
        return self._ds_timeout

    @property
    def ds_search_delay(self) -> float:
        self._load()
        return self._ds_search_delay

    # ── 凭据 ──
    @property
    def github_tokens(self) -> list[str]:
        self._load()
        return self._github_tokens

    @property
    def github_token(self) -> str:
        """返回第一个 GitHub token（兼容单 token 场景）。"""
        self._load()
        return self._github_tokens[0] if self._github_tokens else ""

    @property
    def gitlab_token(self) -> str:
        self._load()
        return self._gitlab_token

    @property
    def gitee_token(self) -> str:
        self._load()
        return self._gitee_token

    @property
    def hf_token(self) -> str:
        self._load()
        return self._hf_token

    @property
    def docker_token(self) -> str:
        self._load()
        return self._docker_token

    # ── SMTP 邮件通知 ──
    @property
    def email_alert_enabled(self) -> bool:
        self._load()
        return self._email_alert_enabled

    @property
    def smtp_config(self) -> dict:
        """返回 SMTP 配置字典，供 EmailNotifier 使用。"""
        self._load()
        return {
            "server": self._smtp_server,
            "port": self._smtp_port,
            "user": self._smtp_user,
            "password": self._smtp_password,
            "from_addr": self._smtp_from,
            "sender_name": self._smtp_sender_name,
            "recipients": self._smtp_recipients,
            "include_full_key": self._email_include_full_key,
            "dedup_hours": self._email_dedup_hours,
        }


# 兼容别名：类名为 Config，但模块为 config_loader，测试/调用方可按 ConfigLoader 引用
ConfigLoader = Config


# 全局单例
config = Config()


def detect_proxy() -> str:
    """自动检测代理：config.ini > 环境变量 > 无。供 run.py 使用。"""
    cfg_proxy = config.proxy_url
    if cfg_proxy:
        return cfg_proxy
    return os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
