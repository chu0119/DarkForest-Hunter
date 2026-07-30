<p align="right"><a href="README.md">English</a></p>

<br>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/Sources-16-green?style=flat-square" alt="Sources">
  <img src="https://img.shields.io/badge/Queries-228-red?style=flat-square" alt="Queries">
  <img src="https://img.shields.io/badge/Platforms-12-orange?style=flat-square" alt="Platforms">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat-square" alt="License">
</p>

<h1 align="center">🌲 DarkForest Hunter</h1>

<p align="center">
  <em>"宇宙就是一座黑暗森林，每个文明都是带枪的猎人。"</em><br>
  <sub>— <strong>刘慈欣</strong>，《三体II：黑暗森林》</sub>
</p>

---

> 一个开源安全研究工具，扫描公开代码仓库里泄露的 AI API Key（以 DeepSeek 为主，扩展到 12 家国内及海外 AI 平台），验证每个 key 的有效性并查询余额。做这个工具是因为我们发现公开仓库里泄露的高余额 key 多到让人震惊，而且已经暴露了几个月甚至更久，完全无人知晓。

---

## 🌲 黑暗森林

在 GitHub 这片代码森林中，数以千万计的开发者日复一日地提交代码。每一行 `API_KEY=sk-...` 都是一次**"广播"**——一个暴露了自己坐标的文明。

**我们，是这片森林里的猎人。**

不是为了猎杀，而是为了在别人开枪之前，**告诉他们：你暴露了。**

这和《三体》中的黑暗森林法则惊人地相似——每个泄露的 key 都是一次广播，暴露了自己的位置。只不过，在安全领域，猎人可能是：自动化脚本、加密货币矿工、数据窃贼，或其他恶意行为者。

**我们把这个工具开源，是希望让善意的猎人先到达现场。**

## 🔭 项目背景

DeepSeek 已经成为全球开发者最常用的 AI API 之一。每天，成千上万的开发者将 API Key 硬编码在配置文件、测试脚本、Jupyter Notebook、Docker Compose 甚至 GitHub Actions 中，然后不小心推送到公开仓库。

我们最初做这个工具，是为了研究一个命题：**在公开代码中，到底有多少 DeepSeek key 被意外泄露？** 几次扫描之后，我们发现数字远比想象中惊人——不仅有 key，而且**很多还有高额余额**。这意味着这些 key 在被我们发现之前，已经暴露了几个月甚至更久，完全无人知晓。

## 🎯 它能做什么

全自动扫描 **16 个数据源**，使用 **228 条查询**，发现公开暴露的 AI API Key（覆盖 **12 家平台**），然后**验证有效性**并**查询余额**（在支持的平台）。

### 覆盖的扫描源（16 个）

| 类别 | 来源 |
|------|------|
| GitHub 系列 | GitHub Code Search、Gist、Issues、Commits、GitHub Raw（宽泛 `sk-` 搜索） |
| 其他代码托管 | GitLab、Gitee（码云） |
| AI 平台 | HuggingFace（Models / Datasets / Spaces） |
| 包管理器 | PyPI、npm |
| 开发者社区 | Stack Overflow、Reddit |
| 粘贴站 | Pastebin |
| 容器与归档 | Docker Hub、Wayback Machine、Common Crawl |
| 搜索引擎 | Google Dork |

### 支持的 AI 平台（12 家）

DeepSeek、Kimi（Moonshot）、智谱（GLM）、通义（阿里）、MiniMax、豆包（字节）、百川、零一（01.AI）、小米、StepFun、商汤（SenseNova）、Claude（Anthropic）。

支持余额查询的平台：**deepseek / zhipu / qwen / minimax**，其余平台只验证有效性。

### 主要用途

- **安全研究** — 量化分析 API key 泄露的规模和模式
- **企业安全审计** — 扫描你的组织仓库，确保没有 key 意外泄露
- **漏洞赏金** — 发现泄露 key 后进行负责任披露
- **安全意识教育** — 用真实数据展示硬编码凭据的风险

## 🚀 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 可选：认证 GitHub CLI 以提升速率限制
gh auth login

# 单平台 DeepSeek 扫描（推荐入口）
python run.py deepseek --proxy http://127.0.0.1:7897

# 多平台扫描（国内 AI 厂商）
python run.py multi --providers deepseek kimi qwen zhipu

# 单数据源扫描
python run.py source --source huggingface

# 查看可用数据源
python run.py --list-sources
```

### 三个子命令

| 命令 | 用途 | 典型时长 |
|------|------|----------|
| `run.py deepseek` | 单平台 DeepSeek 扫描（GitHub + 多源 + 验证 + 查余额） | 约 100 分钟（受限流制约） |
| `run.py multi` | 多平台 AI Key 扫描（覆盖 12 家厂商） | 约 60 分钟 |
| `run.py source` | 单数据源扫描（用于调试 / 定向猎取） | 视数据源而定 |

> 统一入口 `run.py` 替代了旧的 12 个入口脚本（`ultimate_scan.py`、`full_scan.py`、`fast_scan.py` 等），旧脚本已归档到 `legacy/` 目录，仅作参考，不再维护。

### 程序化调用

```python
from scanner_engine import ScannerEngine, build_active_queries

# build_active_queries() 合并静态查询与动态滚动时间窗口查询
queries = build_active_queries()

engine = ScannerEngine(
    concurrency=15,
    scan_pages=5,
    max_duration=3600,
    output_dir="./results",
    proxy="http://127.0.0.1:7897",
)
results = engine.run(queries)
```

## 📁 项目结构

```
DarkForest-Hunter/
├── run.py                    # 统一 CLI 入口（子命令：deepseek / multi / source）
├── scanner_engine.py         # DeepSeek 单平台引擎（查询库 + GitHub 搜索 + 验证）
├── providers.py              # 12 家 AI 平台配置 + UnifiedKeyMatcher/Verifier
├── multi_provider_scan.py    # 多平台扫描器（GitHub 搜索 + 并发验证）
├── DarkForestHunter.spec     # PyInstaller 配置（单文件便携 exe）
├── requirements.txt          # Python 依赖
├── scanners/                 # 17 个扫描器模块（均继承 BaseScanner）
│   ├── __init__.py           # 扫描器注册导出
│   ├── base.py               # BaseScanner + extract_keys + _get_with_retry（429 退避）
│   ├── github_gist.py        # GitHub Gist 扫描器
│   ├── github_issues.py      # GitHub Issues / PR 扫描器
│   ├── github_commits.py     # 提交历史 + diff 扫描器
│   ├── github_events.py      # 实时 PushEvent 监控
│   ├── github_raw.py         # 宽泛 sk- raw 内容扫描
│   ├── gitlab.py             # GitLab 扫描器
│   ├── gitee.py              # Gitee（码云）扫描器
│   ├── huggingface.py        # HuggingFace 扫描器
│   ├── pypi.py               # PyPI 注册表扫描器
│   ├── npm_registry.py       # npm 注册表扫描器
│   ├── stackoverflow.py      # Stack Overflow 扫描器
│   ├── docker.py             # Docker Hub 扫描器
│   ├── wayback.py            # Wayback Machine 扫描器
│   ├── commoncrawl.py        # Common Crawl 扫描器
│   ├── pastebin.py           # Pastebin 扫描器
│   ├── google_dork.py        # Google Dork 扫描器
│   ├── reddit.py             # Reddit 扫描器
│   ├── replicate.py          # Replicate 扫描器
│   └── ai_platforms.py       # Civitai / Together / Modal / Groq / DeepInfra / FalAI
├── legacy/                   # v1.x 旧入口脚本归档（12 个文件，不再维护）
├── results/                  # 扫描结果输出（JSON / CSV / Markdown）
├── README.md                 # 英文说明
├── README_CN.md              # 中文说明（本文件）
├── USAGE.md                  # 详细使用手册
├── DEVELOPER.md              # 开发者 / 扩展指南
├── CHANGELOG.md              # 更新日志
└── LICENSE                   # MIT 许可证
```

## ⚠️ 免责声明

本工具仅用于**授权的安全研究、渗透测试和凭据审计**。使用本工具发现的 API Key 不应被用于未经授权的访问。作者不对任何滥用行为承担责任。如果你在扫描中发现了属于你的 key，请立即在对应的 AI 平台轮换（吊销并重新生成）。

## 📄 开源许可

MIT License — 详见 [LICENSE](LICENSE)

---

<p align="center">
  🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲<br>
  <em>"宇宙就是一座黑暗森林，每个文明都是带枪的猎人。"</em><br>
  <sub>—— 刘慈欣，《三体II：黑暗森林》</sub><br>
  <br>
  <sub>愿善意的猎人率先抵达。</sub>
</p>
