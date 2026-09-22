# DarkForest Hunter — Hardening & Maturation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move DarkForest Hunter from "works, has tests" to a responsibly-open-source-grade tool: fix credential-handling security risks, clear tech debt, add engineering guardrails, and close the responsible-disclosure loop the README promises.

**Architecture:** No paradigm shift. Same producer–consumer core (scan → verify → store). Changes: (1) verification switches from quota-consuming `POST /chat/completions` to read-only `GET /models` (+ balance where supported); (2) one canonical key-filter/dedup module; (3) SQLite store replaces "load all result JSONs" dedup; (4) new disclosure + report layer; (5) CI stops leaking plaintext keys and gains a test gate.

**Tech Stack:** Python 3.10+, requests, aiohttp (scanners), rich (TUI), pytest, ruff. No new runtime deps required.

---

## File Map

**Modified:**
- `providers.py` — add `verify_method`/`models_endpoint`; drop `sk-proj-` from DeepSeek; unify `is_bad_key`/`dedup_results` to re-export from `scanners.base`.
- `scanner_engine.py` — delete dead aiohttp verify path + 8 unused methods; drop `import aiohttp`.
- `multi_provider_scan.py` — use read-only verify; delete dead `SingleProviderScanner`.
- `scanners/base.py` — canonical `is_bad_key`/`extract_keys`/`dedup_results` (extend BAD_PATTERNS conservatively).
- `scanners/__init__.py` — export all 26 scanners.
- `watch_tui.py` — optional SQLite-backed dedup.
- `.github/workflows/darkforest-scan.yml` — redact keys before artifact upload.
- `.gitignore` — add `results/*.db`.
- `README.md` / `DEVELOPER.md` — sync counts/facts.

**Created:**
- `scripts/redact_results.py` — mask full keys → preview for CI artifacts.
- `store.py` — SQLite key store (dedup + history).
- `disclosure.py` — disclosure-report generator.
- `logging_setup.py` — structured logging config.
- `pyproject.toml` — packaging + ruff config.
- `.github/workflows/test.yml` — pytest + ruff gate.
- tests: `tests/test_verify_readonly.py`, `tests/test_store.py`, `tests/test_disclosure.py`, `tests/test_redact.py`.

---

## P0 — Security & Correctness

### Task P0-1: Stop CI leaking plaintext keys
- [ ] Create `scripts/redact_results.py`: read `results/*.json`, write `results_redacted/*.json` with `key` → `key_preview`, drop raw `key`.
- [ ] Test `tests/test_redact.py` (mask full key, preserve counts/balance/repo).
- [ ] Update `.github/workflows/darkforest-scan.yml`: add redact step, upload `results_redacted/` not `results/`.

### Task P0-2: Remove orphaned tracked DB
- [ ] `git rm --cached results/darkforest.db` + delete file (0 code refs confirmed).
- [ ] Add `results/*.db` + `*.sqlite*` to `.gitignore`.

### Task P0-3: Read-only verification (no quota consumption)
- [ ] Add `models_endpoint` to each provider (`/v1/models` OpenAI-compatible; `/api/paas/v4/models` zhipu; `/v1/models` claude). Add `verify_method: str = "models_get"`.
- [ ] Refactor `UnifiedKeyVerifier._verify_with_provider`: `GET models_endpoint` → 200 valid / 401 invalid / 402 valid-no-balance / 429 rate-limited; then optional balance GET for deepseek/zhipu. No POST chat.
- [ ] Tests `tests/test_verify_readonly.py` with mocked `requests` (valid/invalid/402/429/balance).
- [ ] Set deepseek+zhipu `verify_method="balance_get"` (models + balance), others `"models_get"`.

### Task P0-4: Resolve sk-proj- contradiction
- [ ] Remove `r"sk-proj-..."` from `DEEPSEEK.key_patterns` (consistent with `base.is_bad_key` which rejects sk-proj-).
- [ ] Test: DeepSeek provider no longer matches sk-proj- strings; sk- still matches.

---

## P1 — Tech Debt

### Task P1-5: Delete dead code in scanner_engine.py
- [ ] Remove `_verify_one`, `_verify_all_async`, `verify_keys`, `scan_github`, `save_results`, `save_progress`, `load_progress`, `load_keys_from_file`, `load_queries_file`, `suggested_search_delay`.
- [ ] Remove `SingleProviderScanner` from `multi_provider_scan.py`.
- [ ] Drop now-unused `import aiohttp` from `scanner_engine.py`.
- [ ] Run full suite green.

### Task P1-6: Unify key filter & dedup
- [ ] `scanners/base.py` remains canonical. Conservatively add `"sample"` to BAD_PATTERNS (not `"test"` — substring FP risk on real keys).
- [ ] `providers.py`: replace local `is_bad_key_multi_provider` + `dedup_results` with re-exports from `scanners.base`.
- [ ] Tests: one source of truth, behavior preserved on fixtures.

### Task P1-7: Fix stale scanners registry
- [ ] `scanners/__init__.py`: export all 26 scanner classes + `ai_platforms` group.
- [ ] Verify `python -c "import scanners; print(len(scanners.__all__))"`.

---

## P2 — Engineering

### Task P2-8: pyproject.toml + ruff
- [ ] Create `pyproject.toml` (project metadata, `[tool.ruff]` line-length=100, select E/F/I/UP/B).
- [ ] `ruff check .` clean (autofix where safe).

### Task P2-9: CI test gate
- [ ] `.github/workflows/test.yml`: on push/PR, `pip install -r requirements.txt`, `ruff check`, `pytest -q`.

### Task P2-10: Structured logging (scoped)
- [ ] `logging_setup.py`: `configure_logging(verbose, log_file)` using stdlib `logging`.
- [ ] Wire into `run.py`; convert scanner_engine network/rate-limit diagnostics to `logger.warning`. Keep `log_callback` for user-facing pipeline narrative.

### Task P2-11: Doc sync
- [ ] README badges/sources/scanner counts; DEVELOPER.md query counts + scanner count; remove stale `legacy/` mention.

---

## P3 — Product / Expansion

### Task P3-12: Responsible-disclosure workflow
- [ ] `disclosure.py`: `generate_disclosure_report(results)` → markdown (operator summary) + per-key templates (platform abuse email, repo-owner notice).
- [ ] `run.py report` subcommand reading `results/deepseek_keys_result.json`.
- [ ] Tests for report formatting + key masking in templates.

### Task P3-13: SQLite key store
- [ ] `store.py`: schema `keys(key_hash PK, provider, source, repo, file, url, valid, balance, currency, first_seen, last_seen, status)`. Functions `upsert`, `known_hashes`, `query`.
- [ ] Wire `watch_tui` dedup to `known_hashes()` (fallback to current JSON load if DB absent).
- [ ] Tests for upsert idempotency + dedup.

### Task P3-14: verify_method provider attribute
- [ ] Covered by P0-3 (data-structure refactor). Validate enum values via test.

### Task P3-15: Coverage expansion (stretch)
- [ ] Add Shannon-entropy detector to `scanners/base.py` `extract_keys` (catch high-entropy tokens regex misses). Gated, low FP.
- [ ] Note Sourcegraph / GH Secret Scanning API as future work.

---

## Self-Review (completed before execution)
- **Spec coverage:** every P0–P3 item from the assessment maps to a task. ✓
- **Dead-code scope corrected:** only `scanner_engine.py` aiohttp path + helpers; `multi_provider_scan` methods are alive. ✓
- **verify change is uniform:** every provider gets a read-only models GET; no provider posts chat after P0-3. ✓
