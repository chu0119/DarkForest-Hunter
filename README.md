<p align="right">
  <a href="#chinese">中文</a> | <a href="#english">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/Scanners-14-green?style=flat-square" alt="Scanners">
  <img src="https://img.shields.io/badge/Queries-238-red?style=flat-square" alt="Queries">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat-square" alt="License">
</p>

<h1 align="center">🌲 DarkForest Hunter</h1>

<p align="center">
  <em>"The universe is a dark forest. Every civilization is an armed hunter."</em><br>
  <em>"宇宙就是一座黑暗森林，每个文明都是带枪的猎人。"</em><br>
  <sub>— <strong>Liu Cixin</strong>, <em>The Dark Forest / 《三体II：黑暗森林》</em></sub>
</p>

---

> **TL;DR** — A tool that scans 14 platforms with 238 search patterns to find exposed DeepSeek API keys, then validates them and checks their balance. Built because we were shocked by how many live keys with big balances are sitting in public repos, completely unnoticed.

> **太长不看** — 一个用 238 条搜索模式扫描 14 个平台的工具，找出公开暴露的 DeepSeek API Key，然后验证有效性并查询余额。做这个工具是因为我们发现公开仓库里泄漏的高余额 key 多到让人震惊。

---

<h2 id="chinese">🇨🇳 中文</h2>

## 🌲 黑暗森林

在 GitHub 这片代码森林中，数以千万计的开发者日复一日地提交代码。每一行 `API_KEY=sk-...` 都是一次**"广播"**——一个暴露了自己坐标的文明。

**我们，是这片森林里的猎人。**

不是为了猎杀，而是为了在别人开枪之前，**告诉他们：你暴露了。**

这和《三体》中的黑暗森林法则惊人地相似。每个泄露的 key 都是一次广播，暴露了自己的位置。只不过，在安全领域，猎人可能是：自动化脚本、加密货币矿工、数据窃贼，或其他恶意行为者。

**我们把这个工具开源，是希望让善意的猎人先到达现场。**

## 🔭 项目背景

DeepSeek 已经成为全球开发者最常用的 AI API 之一。每天，成千上万的开发者将 API Key 硬编码在配置文件、测试脚本、Jupyter Notebook、Docker Compose 甚至 GitHub Actions 中，然后不小心推送到公开仓库。

我们最初做这个工具，是为了研究一个命题：**在公开代码中，到底有多少 DeepSeek key 被意外泄露？** 几次扫描之后，我们发现数字远比想象中惊人——不仅有 key，而且**很多还有高额余额**。这意味着这些 key 已经暴露了几个月甚至更久，却无人知晓。

## 🎯 它能做什么

全自动扫描 **14 个平台**，使用 **238 条搜索模式**，发现公开暴露的 DeepSeek API Key，然后**验证有效性**并**查询余额**。

### 覆盖的扫描源

| 类别 | 来源 |
|------|------|
| 代码托管 | GitHub Code Search, GitHub Gist, GitHub Issues, GitHub Commits, GitLab, Gitee |
| AI 平台 | HuggingFace (Models, Datasets, Spaces) |
| 包管理器 | PyPI, npm |
| 开发者社区 | Stack Overflow |
| 镜像/归档 | Docker Hub, Wayback Machine, Common Crawl |
| 实时监控 | GitHub Events (PushEvent stream) |

### 主要用途

- **安全研究** — 量化分析 API key 泄露的规模和模式
- **企业安全审计** — 扫描你的组织仓库，确保没有 key 意外泄露
- **漏洞赏金** — 发现泄露 key 后进行负责任披露
- **安全意识教育** — 用真实数据展示硬编码凭据的风险

## 🚀 快速开始

```bash
pip install aiohttp requests

# 可选：认证 GitHub CLI 以提升速率限制
gh auth login

# 全量扫描（10-14小时）
python ultimate_scan.py

# 或快速测试（15分钟）
python quick_batch.py
```

| 脚本 | 说明 | 时长 |
|------|------|------|
| `ultimate_scan.py` | 全 5 阶段扫描 | 10-14h |
| `expanded_scan.py` | 扩展多源扫描 | 3-5h |
| `max_scan.py` | 最大吞吐量 | 2h |
| `deep_scan.py --hours 3` | 深度优化扫描 | 自定义 |
| `quick_batch.py` | 快速测试 | 15min |

## 📁 项目结构

```
DarkForest-Hunter/
├── scanner_engine.py        # 核心引擎
├── scanners/
│   ├── base.py              # 基础扫描器
│   ├── github_gist.py       # Gist 扫描
│   ├── github_issues.py     # Issues/PRs 扫描
│   ├── github_commits.py    # 提交历史 + diff 扫描
│   ├── github_events.py     # 实时 PushEvent 监控
│   ├── gitlab.py            # GitLab 扫描
│   ├── gitee.py             # Gitee (码云) 扫描
│   ├── huggingface.py       # HuggingFace 扫描
│   ├── pypi.py              # PyPI 扫描
│   ├── npm_registry.py      # npm 扫描
│   ├── stackoverflow.py     # Stack Overflow 扫描
│   ├── docker.py            # Docker Hub 扫描
│   ├── commoncrawl.py       # Common Crawl 扫描
│   └── wayback.py           # Wayback Machine 扫描
├── ultimate_scan.py         # 终极扫描脚本
├── queries_v4.txt           # 查询库 (238条)
├── results/                 # 扫描结果目录
├── README.md                # 本文件
└── USAGE.md                 # 详细使用指南
```

---

<h2 id="english">🇺🇸 English</h2>

## 🌲 The Dark Forest

In the code forest of GitHub, millions of developers commit code every day. Every line of `API_KEY=sk-...` is a **"broadcast"** — a civilization revealing its coordinates.

**We are the hunters in this forest.**

Not to destroy, but to warn — **before someone else pulls the trigger.**

This mirrors the Dark Forest theory from Liu Cixin's *Three-Body Problem*: every leaked key is a broadcast revealing coordinates. Except in cybersecurity, the hunters could be automated bots, crypto miners, data thieves, or worse.

**We open-source this tool so that ethical hunters find the prey first.**

## 🔭 Why This Exists

DeepSeek has become one of the most widely used AI APIs. Every day, thousands of developers hardcode API keys in config files, test scripts, Jupyter Notebooks, Docker Compose files, and GitHub Actions — then accidentally push to public repositories.

We built this tool to answer a simple question: **how many DeepSeek keys are exposed in public code?** The answer shocked us — not just keys, but many with **significant balances**. These keys had been sitting exposed for months, completely unnoticed.

## 🎯 What It Does

Automatically scans **14 platforms** with **238 search patterns** to find publicly exposed DeepSeek API keys, then **validates** each one and **checks the balance**.

### Scanning Sources

| Category | Sources |
|----------|---------|
| Code Hosting | GitHub Code Search, GitHub Gist, GitHub Issues, GitHub Commits, GitLab, Gitee |
| AI Platforms | HuggingFace (Models, Datasets, Spaces) |
| Package Registries | PyPI, npm |
| Dev Communities | Stack Overflow |
| Archives | Docker Hub, Wayback Machine, Common Crawl |
| Real-time | GitHub Events (PushEvent stream) |

### Use Cases

- **Security Research** — Quantify the scale of API key exposure
- **Org Auditing** — Scan your organization's repos for accidental leaks
- **Bug Bounty** — Find exposed keys for responsible disclosure
- **Security Education** — Show real-world consequences of hardcoded credentials

## 🚀 Quick Start

```bash
pip install aiohttp requests

# Optional: authenticate GitHub CLI for higher rate limits
gh auth login

# Full scan (10-14 hours)
python ultimate_scan.py

# Or quick test (15 minutes)
python quick_batch.py
```

| Script | Description | Duration |
|--------|-------------|----------|
| `ultimate_scan.py` | Full 5-phase scan | 10-14h |
| `expanded_scan.py` | Expanded multi-source | 3-5h |
| `max_scan.py` | Max throughput | 2h |
| `deep_scan.py --hours 3` | Deep optimized | Custom |
| `quick_batch.py` | Quick test | 15min |

## 📁 Project Structure

```
DarkForest-Hunter/
├── scanner_engine.py        # Core engine
├── scanners/
│   ├── base.py              # Base scanner
│   ├── github_gist.py       # Gist scanner
│   ├── github_issues.py     # Issues/PRs scanner
│   ├── github_commits.py    # Commit history + diff scanner
│   ├── github_events.py     # Real-time PushEvent monitor
│   ├── gitlab.py            # GitLab scanner
│   ├── gitee.py             # Gitee scanner
│   ├── huggingface.py       # HuggingFace scanner
│   ├── pypi.py              # PyPI scanner
│   ├── npm_registry.py      # npm scanner
│   ├── stackoverflow.py     # Stack Overflow scanner
│   ├── docker.py            # Docker Hub scanner
│   ├── commoncrawl.py       # Common Crawl scanner
│   └── wayback.py           # Wayback Machine scanner
├── ultimate_scan.py         # Ultimate scan script
├── queries_v4.txt           # Query library (238 patterns)
├── results/                 # Scan output
├── README.md                # This file
└── USAGE.md                 # Detailed usage guide
```

---

## ⚠️ Disclaimer / 免责声明

| 🇨🇳 中文 | 🇺🇸 English |
|----------|-------------|
| 本工具仅用于**授权的安全研究、渗透测试和凭据审计**。使用本工具发现的 API Key 不应被用于未经授权的访问。作者不对任何滥用行为承担责任。如果你在扫描中发现了属于你的 key，请立即在 DeepSeek 平台轮换。 | This tool is for **authorized security research, penetration testing, and credential auditing only**. Do not use discovered keys for unauthorized access. The authors assume no liability for misuse. If you discover your own key, rotate it immediately on the DeepSeek platform. |

## 📄 License

MIT License — see [LICENSE](LICENSE)

---

<p align="center">
  🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲<br>
  <em>"The universe is a dark forest. Every civilization is an armed hunter."</em><br>
  <em>"宇宙就是一座黑暗森林，每个文明都是带枪的猎人。"</em><br>
  <sub>May the ethical hunters reach the prey first.</sub><br>
  <sub>愿善意的猎人率先抵达。</sub>
</p>
