<p align="right"><a href="README_CN.md">中文</a></p>

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
  <em>"The universe is a dark forest. Every civilization is an armed hunter."</em><br>
  <sub>— <strong>Liu Cixin</strong>, <em>The Dark Forest</em></sub>
</p>

---

> An open-source security research tool that scans public code repositories for leaked AI API keys (DeepSeek first, extended to 12 Chinese and overseas AI platforms), validates each key, and checks its balance. Built because we were shocked by how many live keys with significant balances were sitting in public repos, completely unnoticed.

---

## 🌲 The Dark Forest

In the code forest of GitHub, millions of developers commit code every day. Every line of `API_KEY=sk-...` is a **broadcast** — a civilization revealing its coordinates.

**We are the hunters in this forest.**

Not to destroy, but to warn — **before someone else pulls the trigger.**

This mirrors the Dark Forest theory from Liu Cixin's *Three-Body Problem*: every leaked key is a broadcast revealing coordinates. Except in cybersecurity, the hunters could be automated bots, crypto miners, data thieves, or worse.

**We open-source this tool so that ethical hunters find the prey first.**

## 🔭 Why This Exists

DeepSeek has become one of the most widely used AI APIs. Every day, thousands of developers hardcode API keys in config files, test scripts, Jupyter Notebooks, Docker Compose files, and GitHub Actions — then accidentally push to public repositories.

We built this tool to answer a simple question: **how many DeepSeek keys are exposed in public code?** The answer shocked us — not just keys, but many with **significant balances**. These keys had been sitting exposed for months, completely unnoticed.

## 🎯 What It Does

Automatically scans **16 data sources** with **228 search queries** to find publicly exposed AI API keys across **12 platforms**, then **validates** each one and **checks the balance** (where supported).

### Scanning Sources (16)

| Category | Sources |
|----------|---------|
| GitHub family | GitHub Code Search, Gist, Issues, Commits, GitHub Raw (broad `sk-` scan) |
| Other code hosting | GitLab, Gitee |
| AI platforms | HuggingFace (Models / Datasets / Spaces) |
| Package registries | PyPI, npm |
| Developer communities | Stack Overflow, Reddit |
| Paste sites | Pastebin |
| Containers & archives | Docker Hub, Wayback Machine, Common Crawl |
| Search engines & dorks | Google Dork |

### Supported AI Platforms (12)

DeepSeek, Kimi (Moonshot), Zhipu (GLM), Qwen (Alibaba), MiniMax, Doubao (ByteDance), Baichuan, Yi (01.AI), Xiaomi, StepFun, SenseNova (SenseTime), Claude (Anthropic).

Balance query is supported on: **deepseek / zhipu / qwen / minimax**. Other platforms validate key validity only.

### Use Cases

- **Security Research** — Quantify the scale and patterns of API key exposure
- **Organization Auditing** — Scan your repos for accidental credential leaks
- **Bug Bounty** — Find exposed keys and perform responsible disclosure
- **Security Education** — Demonstrate real-world risks of hardcoded credentials

## 🚀 Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Optional: authenticate GitHub CLI for higher rate limits
gh auth login

# Single-platform DeepSeek scan (recommended entry point)
python run.py deepseek --proxy http://127.0.0.1:7897

# Multi-platform scan (Chinese AI vendors)
python run.py multi --providers deepseek kimi qwen zhipu

# Single data source scan
python run.py source --source huggingface

# List available data sources
python run.py --list-sources
```

### Three Subcommands

| Command | Purpose | Typical duration |
|---------|---------|------------------|
| `run.py deepseek` | Single-platform DeepSeek scan (GitHub + multi-source + validate + balance) | ~100 min (rate-limited) |
| `run.py multi` | Multi-platform AI key scan across 12 vendors | ~60 min |
| `run.py source` | Single data source scan (for debugging / targeted hunting) | varies |

> The unified `run.py` replaces the 12 legacy entry scripts (`ultimate_scan.py`, `full_scan.py`, `fast_scan.py`, etc.), which are preserved under `legacy/` for reference but no longer maintained.

### Programmatic Usage

```python
from scanner_engine import ScannerEngine, build_active_queries

# build_active_queries() merges static queries with dynamic rolling time windows
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

## 📁 Project Structure

```
DarkForest-Hunter/
├── run.py                    # Unified CLI entry (subcommands: deepseek / multi / source)
├── scanner_engine.py         # DeepSeek single-platform engine (queries + GitHub search + verify)
├── providers.py              # 12 AI platform configs + UnifiedKeyMatcher/Verifier
├── multi_provider_scan.py    # Multi-platform scanner (GitHub search + concurrent verify)
├── DarkForestHunter.spec     # PyInstaller config (single portable .exe)
├── requirements.txt          # Python dependencies
├── scanners/                 # 17 scanner modules (all inherit BaseScanner)
│   ├── __init__.py           # Scanner registry exports
│   ├── base.py               # BaseScanner + extract_keys + _get_with_retry (429 backoff)
│   ├── github_gist.py        # GitHub Gist scanner
│   ├── github_issues.py      # GitHub Issues / PRs scanner
│   ├── github_commits.py     # Commit history + diff scanner
│   ├── github_events.py      # Real-time PushEvent monitor
│   ├── github_raw.py         # Broad sk- raw content scan
│   ├── gitlab.py             # GitLab scanner
│   ├── gitee.py              # Gitee (code cloud) scanner
│   ├── huggingface.py        # HuggingFace scanner
│   ├── pypi.py               # PyPI registry scanner
│   ├── npm_registry.py       # npm registry scanner
│   ├── stackoverflow.py      # Stack Overflow scanner
│   ├── docker.py             # Docker Hub scanner
│   ├── wayback.py            # Wayback Machine scanner
│   ├── commoncrawl.py        # Common Crawl scanner
│   ├── pastebin.py           # Pastebin scanner
│   ├── google_dork.py        # Google Dork scanner
│   ├── reddit.py             # Reddit scanner
│   ├── replicate.py          # Replicate scanner
│   └── ai_platforms.py       # Civitai / Together / Modal / Groq / DeepInfra / FalAI
├── legacy/                   # Archived v1.x entry scripts (12 files, not maintained)
├── results/                  # Scan output (JSON / CSV / Markdown)
├── README.md                 # This file (English)
├── README_CN.md              # Chinese version
├── USAGE.md                  # Detailed usage manual (Chinese)
├── DEVELOPER.md              # Developer / extension guide (Chinese)
├── CHANGELOG.md              # Release notes
└── LICENSE                   # MIT License
```

## ⚠️ Disclaimer

This tool is for **authorized security research, penetration testing, and credential auditing only**. Do not use discovered keys for unauthorized access. The authors assume no liability for misuse. If you discover your own key during a scan, rotate it immediately on the corresponding AI platform.

## 📄 License

MIT License — see [LICENSE](LICENSE)

---

<p align="center">
  🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲<br>
  <em>"The universe is a dark forest. Every civilization is an armed hunter."</em><br>
  <sub>— Liu Cixin, <em>The Dark Forest</em></sub><br>
  <br>
  <sub>May the ethical hunters reach the prey first.</sub>
</p>
