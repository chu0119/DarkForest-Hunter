<p align="right"><a href="README_CN.md">中文</a></p>

<br>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/Platforms-35-orange?style=flat-square" alt="Platforms">
  <img src="https://img.shields.io/badge/Sources-11-green?style=flat-square" alt="Sources">
  <img src="https://img.shields.io/badge/Queries-~5000-red?style=flat-square" alt="Queries">
  <img src="https://img.shields.io/badge/Tests-479-success?style=flat-square" alt="Tests">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat-square" alt="License">
</p>

<h1 align="center">🌲 DarkForest Hunter</h1>

<p align="center">
  <em>"The universe is a dark forest. Every civilization is an armed hunter."</em><br>
  <sub>— <strong>Liu Cixin</strong>, <em>The Dark Forest</em></sub>
</p>

---

> An open-source security research tool that scans public code repositories for leaked AI API keys across **35 AI platforms**, validates each key (read-only GET models/balance first, then a minimal `max_tokens=1` confirmation probe; both can be disabled), and checks its balance (where supported). Built because we were shocked by how many live keys with significant balances were sitting in public repos, completely unnoticed.

---

## 🌲 The Dark Forest

In the code forest of GitHub, millions of developers commit code every day. Every line of `API_KEY=sk-...` is a **broadcast** — a civilization revealing its coordinates.

**We are the hunters in this forest.**

This mirrors the Dark Forest theory from Liu Cixin's *Three-Body Problem*: every leaked key is a broadcast revealing coordinates. Except in cybersecurity, the hunters are automated bots, crypto miners, data thieves, or worse.

## 🔭 Why This Exists

AI APIs have become essential infrastructure. Every day, thousands of developers hardcode API keys in config files, test scripts, Jupyter Notebooks, Docker Compose files, and GitHub Actions — then accidentally push to public repositories.

We built this tool to answer a simple question: **how many AI keys are exposed in public code?** The answer shocked us — not just keys, but many with **significant balances**. These keys had been sitting exposed for months, completely unnoticed.

## ✨ Highlights

| | |
|---|---|
| 🎯 **Native quota scheduling (v2.5)** | GitHub's `X-RateLimit-*` response headers are the single source of truth — remaining quota is spread evenly across the rate-limit window, so the scanner **never exhausts its quota early**, never triggers sleep-to-reset, and never hits primary-quota 429s. |
| 🌐 **Multi-IP proxy out of the box (v2.4.7+)** | Ships with an **embedded mihomo client** — point it at any VMess/Trojan/Hysteria2/SS subscription and every GitHub token gets its own exit IP with independent pacing. Falls back to single-proxy automatically when the subscription is unavailable. |
| 🛡️ **Layered anti-abuse defense** | IP-level penalty box, tiered 429 cooldown, deep-backoff self-quiet, per-port transport-failure rotation — tuned against real GitHub secondary-limit escalations, not guesses. |
| ✅ **Accuracy-first verification** | 35 platforms with per-platform auth semantics (some `/models` endpoints are public — those get one minimal probe instead of being falsely validated). Balance口径: cash vs voucher vs weekly-quota-percentage are kept distinct, never mixed. |
| 📈 **Yield-learning query engine** | ~5,000 queries with live feedback: queries that burn quota without converting get circuit-broken; high-yield queries get deeper pagination; mutations of top queries are derived automatically. |
| 📬 **24/7 watch with alerts** | Priority verification queue, budgeted re-verification scheduler (balance shrink/refill/reactivation detection), high-value email alerts, TUI dashboard, and one-click start/stop scripts. |

## 🎯 What It Does

**24/7 continuous watch mode** (the only scan entry) scans data sources round-by-round, feeds discovered keys into a verification queue, and alerts on high-value finds via email. Every parameter has a sane default — `python -u run.py watch` alone includes everything.

### Watch Mode Architecture

```
Sources (producers)  ──▶  VerificationBroker (priority queue)  ──▶  Workers (consumers)
  github_search           │  new keys: priority 0                    ├─ GET /models (read-only)
  github_commits          │  reverify:   priority 20                 ├─ balance query (read-only)
  github_events (realtime)│                                         └─ chat probe (on by default, --no-allow-chat-probe to disable)
  gitlab                  │  ──▶  SQLite store + CSV ledger  ──▶  email alert (high-value)
  npm                     │         └──▶  TUI dashboard (2fps)

GitHub Code Search calls ──▶  RateScheduler (header-driven)   ──▶  even spacing across the
                           └─▶  MultiProxyRouter / mihomo     ──▶   rate-limit window, per-IP
                               (one exit IP per token)              pacing, zero quota burn-out
```

### Supported AI Platforms (35)

**Chinese & overseas vendors (12 core):** DeepSeek, Kimi (Moonshot), Zhipu (GLM), Qwen (Alibaba), MiniMax, Doubao (ByteDance), Baichuan, Yi (01.AI), Xiaomi (MiMo), StepFun, SenseNova (SenseTime), Claude (Anthropic).

**Coding Plans / subscriptions (5):** Kimi Code, Zhipu GLM Coding Plan, Qwen Coding Plan, MiniMax Coding Plan, MiMo Token Plan.

**International platforms (10):** OpenRouter, Groq, Replicate, Together AI, Fireworks AI, SiliconFlow, Novita AI, DeepInfra, Jina AI, Voyage AI.

**Second batch (2026-09, 8):** OpenAI, Google Gemini, xAI (Grok), Tencent Hunyuan, Baidu Qianfan, ModelScope (魔搭), NVIDIA NIM, LongCat (Meituan).

> **Verification notes:** ModelScope / NVIDIA NIM / LongCat expose a **public `/models` endpoint** (a fake key still gets HTTP 200), so read-only GET can't decide them — they get one minimal fallback probe by default (`probe_unclear_platforms = true`, disable with `--no-probe-unclear`). Qianfan returns 403 for invalid keys; Gemini authenticates via the `?key=` query parameter; OpenRouter validates against `/auth/key` (its `/models` is public); NVIDIA's probe model is kept current (EOL models return 410 before auth). 01.AI (Yi) API was shut down (HTTP 410) and is disabled.

**Balance query supported on:** deepseek / kimi / zhipu / stepfun / siliconflow / minimax(cp) / openrouter (via `/auth/key`: limit − usage). **GLM Coding Plan reports weekly quota remaining as a percentage** (unit `PERCENT`, recorded even at 0% — never mixed into CNY totals). Kimi balance uses `cash_balance` (vouchers are not money). Other platforms validate key validity only.

> **Probing policy:** read-only GET by default, then one minimal `max_tokens=1` chat confirmation probe per key (disable with `--no-allow-chat-probe`). A probe-passed key whose balance endpoint reports a confirmed ≤0 balance is recorded as `valid_zero` — "active" never implies "has money".

### Data Sources (11 sources, 5 watch-default)

| Category | Sources | Watch-default |
|----------|---------|---------------|
| GitHub family | Code Search, Commits, **Events (realtime PushEvent)**, Gist, Issues, Raw | ✅ Code Search, Commits, Events |
| Code hosting | GitLab | ✅ GitLab |
| AI platforms | HuggingFace (Models / Datasets / Spaces) | — |
| Package registries | npm | ✅ npm |
| Paste sites | Pastebin / Rentry / ControlC | — |
| Containers | Docker Hub | — |

> Non-default sources (Gist/Issues/HF/paste_sites/etc.) can be added via `--sources`. Run `python run.py --list-sources` for the authoritative catalog.

### Use Cases

- **Security Research** — Quantify the scale and patterns of API key exposure
- **Organization Auditing** — Scan your repos for accidental credential leaks
- **Bug Bounty** — Discover exposed keys for bounty programs
- **Continuous Monitoring** — 24/7 watch with email alerts for high-value finds

## 🚀 Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# (optional) GitHub auth for higher rate limits
gh auth login

# 24/7 watch (the only scan entry — runs until Ctrl+C)
python -u run.py watch

# Headless / background (logs to results/watch_session_*.log)
python -u run.py watch --no-tui

# Bare invocation is equivalent to watch
python run.py

# List available data sources
python run.py --list-sources
```

### Local Ledger Maintenance

```bash
# Redact deterministic invalid plaintext; add --prune-invalid-days 90 to purge old rows
python run.py maintenance --db results/darkforest.db --vacuum
```

### Yield Metrics

```bash
# Aggregate candidate/valid/high-value conversion; never prints keys
python run.py metrics --min-balance 1

# Also probe configured GitHub token health
python run.py metrics --min-balance 1 --check-github
```

### Watch Mode Options

```bash
# Headless (background / server) — logs to results/watch_session_*.log
python -u run.py watch --no-tui

# With TUI dashboard (terminal UI, Ctrl+C to exit)
python run.py watch

# Custom data sources + verification tuning
python -u run.py watch --no-tui \
  --sources github_search github_events gitlab npm \
  --verify-workers 8 --verify-interval 0.25 \
  --reverify-budget 1500 --reverify-zero-hours 168

# One round only (testing)
python run.py watch --no-tui --once

# Multi-proxy via subscription (VMess/Trojan/Hysteria2/SS → embedded mihomo,
# one exit IP per GitHub token). Also settable in config.ini [proxy].subscription.
python -u run.py watch --no-tui --proxy-subscription "https://your-subscription-url"
```

| Option | Default | Description |
|--------|---------|-------------|
| `--interval` | 300s | Seconds between scan rounds |
| `--min-balance` | ¥1.0 | Only record keys above this balance |
| `--verify-interval` | 0.25s | Delay between verifications per worker |
| `--verify-workers` | 8 | Concurrent verification workers (QPS ≈ workers/interval) |
| `--reverify-budget` | 1500/day | Daily budget for re-verifying known-valid keys |
| `--reverify-zero-hours` | 168 (7d) | Re-verify interval for zero-balance keys |
| `--hv-email-threshold` | ¥5 | Email alert threshold for new high-value keys |
| `--hv-top-threshold` | ¥10 | Threshold for "top tier" re-verification (6h) |
| `--shrink-warn-pct` | 30% | Balance drop % to trigger shrink warning email |
| `--commits-since-hours` | 168 (7d) | Time window for github_commits source |
| `--github-pages` | 1 | Pages per Code Search query (higher = slower) |
| `--proxy-subscription` | — | Proxy subscription URL (embedded mihomo multi-IP mode) |
| `--no-allow-chat-probe` | probe on | Disable the `max_tokens=1` confirmation probe |
| `--no-probe-unclear` | probe on | Disable fallback probe for public-`/models` platforms |
| `--fresh` | — | Restart query rotation from beginning |

### Programmatic Usage

```python
from scanner_engine import ScannerEngine, build_active_queries

queries = build_active_queries()  # static + dynamic rolling time windows
engine = ScannerEngine(concurrency=15, scan_pages=5, max_duration=3600,
                       output_dir="./results", proxy="http://127.0.0.1:7897")
results = engine.run(queries)
```

## 📁 Project Structure

```
DarkForest-Hunter/
├── run.py                    # Unified CLI (watch = the only scan entry; + maintenance/metrics)
├── watch_tui.py              # 24/7 watch: broker + scheduler + TUI + reverify
├── watch_persistence.py      # watch state / SQLite history / CSV ledger I/O
├── scanner_engine.py         # Scan engine (GitHub search + query tracking + verify)
├── rate_scheduler.py         # Native quota scheduler (header-driven even spacing)
├── proxy_resolver.py         # SmartProxy + MultiProxyRouter (per-IP token routing)
├── mihomo_manager.py         # Embedded mihomo lifecycle (download/config/start/stop)
├── providers.py              # 35 AI platform configs + UnifiedKeyMatcher/Verifier
├── store.py                  # SQLite key store (history + cross-run dedup)
├── query_rotation.py         # Query rotation + mutation engine + generated pool
├── query_enumerator.py       # Offline: regenerate the expanded query pool
├── email_notifier.py         # Async email alerts (dedup + HTML templates)
├── trend_monitor.py          # DB metrics logging (trend.jsonl)
├── config_loader.py          # config.ini loader
├── config.ini.example        # Configuration template
├── scanners/                 # 11 scheduled source scanners (inherit BaseScanner)
│   ├── base.py               # BaseScanner + extract_keys + is_bad_key + 429 backoff
│   ├── github_events.py      # Realtime PushEvent monitor (default watch source)
│   ├── github_commits.py     # Commit history + diff scanner
│   ├── github_gist.py        # GitHub Gist scanner
│   ├── github_issues.py      # GitHub Issues / PRs scanner
│   ├── github_raw.py         # Broad sk- raw content scan
│   ├── gitlab.py             # GitLab blob search
│   ├── huggingface.py        # HuggingFace (Models/Datasets/Spaces)
│   ├── npm_registry.py       # npm registry scanner (default watch source)
│   ├── docker.py             # Docker Hub image layer scan
│   └── paste_sites.py        # Pastebin + Google Dork
├── bin/                      # mihomo binary (downloaded on first multi-proxy use, gitignored)
├── results/                  # Runtime output (gitignored — contains real keys)
│   ├── darkforest.db         # SQLite key store
│   ├── watch_state.json      # Latest verification view
│   ├── watch_high_value.csv  # High-value key ledger
│   ├── query_stats.json      # Query yield learning
│   └── watch_session_*.log   # Per-session logs
├── tests/                    # 479 tests (pytest, all offline/mock)
├── docs/                     # Manuals and analyses
├── README.md                 # This file (English)
├── README_CN.md              # Chinese version
├── CHANGELOG.md              # Release notes
└── LICENSE                   # MIT License
```

## ⚙️ Configuration

Copy `config.ini.example` → `config.ini` and fill in:

- `[proxy]` — HTTP proxy (Clash/V2Ray), auto-detected if omitted; `subscription` = proxy subscription URL for embedded mihomo multi-IP mode
- `[github]` — Token(s), comma-separated for multi-token parallelism (10 req/min/token each)
- `[SMTP]` — Email alerts (SSL 465 or STARTTLS 587)
- `[watch]` — All watch-mode tuning (budgets, thresholds, intervals)
- `[verification]` — Probe switches (`allow_chat_probe`, `probe_unclear_platforms`)

## 📜 Version History

> Full release notes in [CHANGELOG.md](CHANGELOG.md). Summary of every release:

### v2.5.x — Native quota scheduling & retrieval effectiveness (2026-09-22)

| Version | Summary |
|---------|---------|
| **v2.5.3** | **Optimization backlog executed**: 655 stale `error`/`rate_limited` keys enqueued for re-verification at startup (they were unreachable — scheduler only selects `valid=1`); fallback raw downloads now rotate across all proxy egress IPs (`MultiProxyRouter.get_any_proxy`) instead of one fixed exit; new `last_error` column records why each error row failed (auto-cleared on recovery); currency defense (PERCENT providers forced in `upsert`, historical mislabels fixed); probe-passed keys with a confirmed zero balance are now `valid_zero` (242 historical rows corrected). |
| **v2.5.2** | **Three-giant targeted fixes**: Claude `sk-ant-oat01-` (CLI setup tokens) was extracted but never routed by `identify` — 5 DB rows stuck unknown, now added to Claude patterns; Gemini `AQ.Ab` (2026 new format) got its own context queries (the old 4 were AIza-only → 0-score misrouting); +18 queries (Claude's dedicated-query blank filled, `.claude`/`.cursor`/`google-services.json` leak surfaces). |
| **v2.5.1** | **Retrieval-effectiveness batch (7 fixes + query expansion)**: transport failures (TLS reset) now rotate to a healthy proxy port instead of retrying the same dead one (was: 23 queries lost to 3-strikes in one session); main scan uses all 3 tokens (was reserving one idle for fresh-repo); negative lookahead on proprietary prefixes stops short fake `sk-proj-`/`sk-ant-` noise (269 DB rows); CSS/code-literal junk filter (8 empirical word roots); NVIDIA probe model EOL fix (219 errors → live again: 403-body discrimination + 410 alarm); `max_candidates` 4→8 (contextless SiliconFlow keys ranked #7 never got verified); DeepSeek balance summed across all `balance_infos` entries; **query rotation dead slots fixed** — 6 of 22 compat queries were unreachable (`FRESH_PATTERNS` 8→11); +51 researched queries (355→402 after v2.5.2). |
| **v2.5** | **Native quota scheduling** — replaced the fixed-interval "burst-then-sleep" pacing with a header-driven scheduler: `wait = (reset - now) / remaining` spreads each token's 10 requests evenly across the rate-limit window. Root-eliminated (not masked) the once-per-minute "quota will reset, waiting 31s" warnings, primary-quota 429s, and sleep-to-reset stalls. Separate buckets for `code_search` (10/min) and `search` (30/min) resources; >90s waits become silent self-quiet; IP floor unified to 6.0s (half the abuse threshold); token stagger cut 30s→2s (saved 60s per round). Validated by a 4.8-hour monitored run: **0 quota warnings, 0 network-error escalation, 13.7% valid rate**. |

### v2.4.x — Multi-proxy, platform expansion & abuse defense (2026-09-21 → 09-22)

| Version | Summary |
|---------|---------|
| **v2.4.9** | **Full-chain review fixes**: duplicate `_on_ip_rate_limit` definition silently overrode the multi-proxy version (429 IP cooling was dead code — Python class redefinition hazard); `engine.stop()` was never called on watch exit → mihomo orphan processes (root cause of recurring "fallback to single-proxy"); dual-mihomo bug (WatchScanner built a second engine); penalty values clamped `≥ baseline`; yield fixes (query context into platform identify — +30 score signal finally fires; `sk-proj-` truncation at 208 chars; OpenRouter keys killed by `>80` length gate — 0→27 valid after fix; transient errors release `_seen`); **1,129 lines of dead code removed** (deploy scripts with hardcoded root passwords, zombie CI workflow, unused scanners). |
| **v2.4.8** | Rate-limit buckets follow the **exit IP**; baseline follows mode (multi-proxy 4.0s/IP, single-proxy 4.0×N tokens) — fallback no longer keeps multi-proxy speed while sharing one IP. mihomo `log-level` fix (`warning` not `warn` — v1.18.0's "invalid mode" error is misleading). |
| **v2.4.7** | **Embedded mihomo**: native support for VMess/Trojan/Hysteria2/SS subscriptions → one local HTTP port per node, one exit IP per GitHub token, per-IP pacing. Per-IP multi-proxy routing + verification-queue workers each pinned to their own egress IP. Baseline tuned to 4.0s against measured-safe 3.5s. |
| **v2.4.6** | **IP-level penalty box**: GitHub's abuse detection is per-IP — 3 tokens × 9 req/min on one IP tripped escalating punishments. Any Retry-After >60s now triggers a global cool-down (25s/token for 10 minutes). |
| **v2.4.5** | **Rate-limit crash prevention** (from a real 4.5h run that accumulated 5 secondary-limit 429s): baseline pulled back 6.3s→6.6s, tiered 429 cooldown (≥3/hour → deep cool-down), ±8% jitter to break metronome patterns, `remaining ≤ 2` pre-emptive sleep-to-reset. |
| **v2.4.4** | **Zhipu fake-balance fix**: the old endpoint returned the registration gift quota (always ¥2000) — switched to the real `/users/balance` endpoint; **GLM Coding Plan** weekly-quota-percentage as balance with new `PERCENT` currency (never summed into CNY); historical fake-balance rows flagged for re-verification. |
| **v2.4.3** | **Full-chain efficiency**: scan pacing at 95% of quota; ambiguous keys verify 4 candidate platforms in parallel (priority-sorted by historical yield); placeholder-key filter from DB evidence (1,364 junk keys → 18 patterns + slug heuristic); per-provider rate limits (Qianfan 429 at 0.25s → 1.0s); expanded query pool regenerated for batch-2 platforms (4,631 queries); 24h verification funnel in `metrics`; **Kimi balance = `cash_balance`** (vouchers are not money). |
| **v2.4.2** | **Entry convergence**: `watch` is now the only scan entry (`deepseek`/`multi`/`source`/`report` subcommands removed); **chat probe default ON** — one `max_tokens=1` request confirms a key actually works (balance display ≠ callable). |
| **v2.4.1** | **Full-platform auth audit** (fake keys tested against 20+ endpoints): OpenRouter misreport fixed (public `/models` → validate via `/auth/key`, +balance parsing); Jina same class of fix; Yi API shut down (HTTP 410) → disabled; `probe_unclear` fallback for public-`/models` platforms; fresh-cluster queries for all batch-2 platforms. |
| **v2.4.0** | **Second batch of platforms: 27 → 35** — OpenAI, Google Gemini, xAI, Tencent Hunyuan, Baidu Qianfan, ModelScope, NVIDIA NIM, LongCat. |

### v2.3.x — Watch mode maturity (2026-08-26 → 08-29)

| Version | Summary |
|---------|---------|
| **v2.3.1** | Watch **state-resurrection fix** (stale `watch_state.json` could resurrect dead keys); chat probe moved to explicit opt-in (later defaulted back on in v2.4.2); verification concurrency governance (bounded pools, per-provider shared rate limiter, thread-safe sessions); SQLite invalid-plaintext redaction + `maintenance` command; **query yield feedback loop** with quality circuit breaker (zero-yield queries get budget-cycled out); `metrics` yield profile; `watch_persistence` module extracted. |
| **v2.3.0** | **Yield batch (10 fixes)**: `KEY_PATTERN` single source of truth (Zhipu `hex.secret` extraction was silently missing in the engine's private copy); prefix-aware length limits saved MiniMax JWT / Claude long keys from the `>80` gate; hex/alnum charset pre-checks (placeholder keys die before burning an API call); realtime `github_events` source wired; zero-balance keys re-verified weekly instead of daily (freed the budget for keys that can actually change); verification sessions reused (TLS handshake savings); history caching removed per-save linear I/O. |

### v2.2.x & earlier — Foundation (2026-05 → 08)

| Version | Summary |
|---------|---------|
| **v2.2.1** | **Watch statistics root-cause fixes**: SQLite cross-thread writes were silently swallowed (DB stuck at 0 rows, cross-run dedup broken) → `check_same_thread=False` + write lock; cumulative TUI counters (no more zero-flash on round switch); restart history closure; save-merges-history (a restart no longer wiped the ledger); **first real test suite** (78 unit tests). |
| **v2.0.0** | **Architecture overhaul**: unified `run.py` CLI replaced 12 scattered entry scripts; rolling time-window queries (`pushed:>最近7天`) instead of hard-coded dates; unified 429 backoff (`Retry-After`-aware) across all scanners; concurrent multi-platform verification (149 keys in ~10s); connection-pool reuse; domestic platforms bypass the proxy to avoid CN-vendor IP blocks. |
| **v1.0.0** | **Initial release** (2026-05-21): leaked DeepSeek key scanner, 14 platforms, 238 queries, Gist/Issues/Commits/GitLab/Gitee/HF/PyPI/npm/StackOverflow/Docker/Wayback sources, JSON/CSV/Markdown reports. |

## 📄 License

MIT License — see [LICENSE](LICENSE)

---

<p align="center">
  🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲<br>
  <em>"The universe is a dark forest. Every civilization is an armed hunter."</em><br>
  <sub>— Liu Cixin, <em>The Dark Forest</em></sub>
  <br><br>
  <sub>First in the forest.</sub>
</p>
