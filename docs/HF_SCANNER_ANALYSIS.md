# HuggingFace Scanner — Root-Cause Analysis & Optimization

**Date:** 2026-08-08
**Scanner:** `scanners/huggingface.py` (HuggingFaceScanner)
**Symptom:** Returns 0 keys consistently.

---

## Executive Summary

The HF scanner does **not** have a broken extraction pipeline (verified working). It
fails because its **discovery strategy is fundamentally mismatched to the HuggingFace
platform**:

> HuggingFace has **no public content-search API**. `/api/models?search=` searches only
> **metadata** (repo name / tags / description / README) — it does **NOT** search file
> contents. Scanning 50 `deepseek` Spaces' source files returned **0 keys**, because
> repos *named* "deepseek" are overwhelmingly legitimate (official models, GGUF
> quants, polished demos). The leaked-key repos are needles scattered across the
> platform that metadata search cannot point at.

On top of that, **pagination is broken** (`cursor=<repo_id>` → HTTP 400), so only
page 1 (50 repos/type) is ever scanned, and the proxy IP gets **rate-limited** on
any `sk-` query.

---

## 1. Current Implementation Issues (specific code problems)

### BUG 1 — Broken pagination (PRIMARY functional bug)
`scanners/huggingface.py` L65-66:
```python
params["cursor"] = f"{all_items[-1].get('id', '')}"
```
HF uses **base64 cursor pagination via the `Link` header** (`rel="next"`), NOT a
`cursor` query param set to the last repo id.
- **Actual behavior:** `cursor=<repo_id>` → **HTTP 400** → the `else: break` on L84
  fires → **only page 1 is ever fetched.**
- **Verified:** `GET ...?limit=2&cursor=deepseek-ai/DeepSeek-V3` → `400`; the correct
  cursor from the `Link` header returns the next page with different results.
- **Impact:** caps discovery at 50 repos/type instead of the intended 5×50.

### BUG 2 — Discovery strategy targets the wrong repos (ROOT CAUSE of 0 keys)
`scanner_engine.py` L955 registers HF search terms:
```python
"huggingface": ("HuggingFace", ["deepseek", "deepseek api", "sk- deepseek"]),
```
`HuggingFaceScanner.search()` (L29-57) does, for each term:
1. `_hf_search(...)` → metadata search → repos **named/tagged** "deepseek"
2. `_scan_repo_files(...)` → list + download + scan each repo's files

The repos surfaced by `search=deepseek` are legitimate:
`deepseek-ai/DeepSeek-V3`, `unsloth/*-GGUF`, `prithivMLmods/DeepSeek-OCR-*`,
demo Spaces with `app.py` — **none contain hardcoded `sk-` keys.** Empirically,
scanning 50 such Spaces' `.py`/`.js`/`.json` files → **0 keys.**

### BUG 3 — No content search exists on HF
- `/api/models?search=sk-` → **rate-limited** (429) and would only match metadata anyway.
- The HF **web search** (`/search?q=deepseek+sk-&type=space`) is a **JS-rendered
  SPA**: the HTML is a 70 KB shell with **0 repo links** and **0 `sk-` matches** —
  not scrapeable with `requests`.
- **Conclusion:** there is no HF equivalent of GitHub Code Search. Finding a key
  requires downloading and scanning file contents; metadata search cannot target the
  right repos.

### BUG 4 — Aggressive rate limiting
HF rate-limits the egress proxy IP. Searches containing `sk-` and rapid tree/raw
calls trigger 429s. The scanner has no `HF_TOKEN` support and no Retry-After
handling for HF (only a fixed 10s sleep on 429 at L80-82).

### BUG 5 — Redundant README double-scan for Spaces
`_scan_space()` (L100-117) calls `_scan_repo_files()` (which already scans every
file including `README.md`) **and** separately re-fetches `README.md`. Wastes a
call per Space.

### BUG 6 — Hardcoded `blob/main/` in result URL
`_scan_repo_files()` L169:
```python
self._add_result(k, f"{self.HF_HUB}/{repo_id}/blob/main/{path}", ...)
```
Hardcodes `main` even when the repo uses `master`. Minor (cosmetic URL), but easy
to fix by using the discovered `branch`.

---

## 2. Working API Endpoints (verified via proxy `http://127.0.0.1:7897`)

| Endpoint | Status | Notes |
|---|---|---|
| `GET /api/models?search=deepseek&limit=30` | 200 | List[dict]; keys: `id, tags, downloads, …` |
| `GET /api/datasets?search=deepseek&limit=30` | 200 | List[dict] |
| `GET /api/spaces?search=deepseek&limit=30` | 200 | List[dict]; keys: `id, sdk, tags, …` |
| `GET /api/models/{id}/tree/main/` | 200 | List[{`type, path, size, oid`}] |
| `GET /api/models/{id}/tree/master/` | 404 | when repo uses `main` |
| `GET /api/spaces/{id}/tree/main/` | 200 | branch is `main` for all 30 deepseek Spaces tested |
| `GET /{id}/raw/main/{path}` | 200 | file content (preferred; stable URL) |
| `GET /{id}/resolve/main/{path}` | 200 | redirects to `api/resolve-cache/…` (works, but extra redirect) |
| `Link` header (search responses) | present | `rel="next"` carries the **base64 cursor** for pagination |

**Pagination (correct):**
```
GET /api/models?search=deepseek&limit=2
→ Link: <…?limit=2&cursor=eyIkb3IiOlt…>; rel="next"
GET that URL → next page (different ids).   ✓
GET …?limit=2&cursor=<repo_id>            → 400  ✗ (what the code does)
```

---

## 3. Recommended Optimization Strategy

**A. Fix the concrete bugs (high confidence, low risk)**
1. **Pagination:** parse the `Link` header (`rel="next"`) for the cursor; fall back to
   stopping at page 1 if absent. (Or use `offset`/page-based if preferred.)
2. **Branch:** use the discovered `branch` variable in the result URL.
3. **Remove** the redundant README re-fetch in `_scan_space`.
4. **Add `HF_TOKEN` support** (auth header `Bearer <token>`) — raises rate limits
   dramatically and is required for some repos. Add Retry-After handling for 429s.

**B. Rethink discovery — HF cannot content-search, so cast a wider, smarter net**
Since metadata search can't find `sk-`, value comes from scanning file contents of
repos *likely* to hardcode keys:
1. **Prioritize Spaces** — they have executable code (`app.py`) and are where
   users most often paste a key. Scan them before models/datasets.
2. **Diversify search terms** (see §4) to surface more *demo/personal/tutorial*
   repos, which leak more often than official ones.
3. **Scan deeper:** more repos per term (needs pagination fix) and more files per
   repo (the repo-file filter is fine; consider also scanning `.env*`, `.txt`,
   notebooks, and config files already listed).
4. **Deprioritize models/datasets** for key-hunting — they are mostly weights/
   data, low key density. Keep them but allocate fewer slots.

**C. Accept the platform limit**
Even optimized, HF will be **much lower yield than GitHub** (which has true Code
Search). Treat HF as a secondary source. Do not expect high volume; one real find
per many repos scanned is the realistic baseline.

---

## 4. Alternative Search Terms / Keywords for HF

HF `search=` matches repo **id, author, tags, description, README**. Leaky repos
tend to be *personal demos / free-endpoint / tutorial* projects. Use terms that
surface those. Recommended search-term list (pass each to `_hf_search` for all
three entity types, Spaces first):

```
# DeepSeek wrappers / demos (highest app.py density)
deepseek
deepseek-api
deepseek-chat
deepseek-free
deepseek-free-endpoint
deepseek-api-key        # some leaky READMEs literally say this
deepseek-proxy

# Generic AI-wrapper repos that hardcode keys
openai-api-key
api-key
chatbot
ai-chat
gradio-deepseek
deepseek-gradio

# Platform/tool repos that often embed secrets
deepseek-openwebui
deepseek-litellm
deepseek-vllm
deepseek-ollama
```

**Verified working:** `deepseek`, `deepseek-api`, `deepseek-free`, `deepseek-proxy`,
`deepseek-openwebui`, `chatbot`, `ai-chat`, `gradio-deepseek` all return 200 + lists
when tested individually. (Avoid querying `sk-` directly — it's rate-limited and
matches nothing useful.)

**Note:** Because HF searches metadata, terms that imply a *personal/free/demo*
project outperform bare `deepseek`. Rotate these terms across runs.

---

## 5. Code Fix Suggestions

### 5.1 Fix pagination (`_hf_search`)
Replace the `cursor=<repo_id>` logic with Link-header parsing:
```python
import re
# after a successful fetch:
link = resp.headers.get("Link", "")
m = re.search(r'<([^>]+)>;\s*rel="next"', link)
next_url = m.group(1) if m else None
# on next iteration, if next_url: fetch it directly; else break.
```
Or simpler: keep `all_items[-1]` but drive continuation from `next_url` instead of
faking a cursor. Drop the `cursor` query-param entirely.

### 5.2 Add `HF_TOKEN` + better 429 handling
In `__init__`, accept `token` (already present but only used as a generic token —
rename to `hf_token` for clarity) and set `Authorization: Bearer <hf_token>`. In
`_hf_search`, on 429 read `Retry-After` and sleep that long (cap ~60s).

### 5.3 Prioritize Spaces, fix branch + dedup
- In `search()`, scan Spaces first and allocate them more of `max_items`.
- In `_scan_repo_files`, build the result URL with the discovered `branch`
  instead of hardcoded `main`.
- In `_scan_space`, drop the standalone README re-fetch (already covered by
  `_scan_repo_files`).

### 5.4 Register better terms (`scanner_engine.py` L955)
```python
"huggingface": ("HuggingFace", [
    "deepseek", "deepseek-api", "deepseek-free-endpoint", "deepseek-proxy",
    "deepseek-openwebui", "deepseek-chat", "deepseek-gradio",
    "chatbot", "ai-chat",
]),
```

---

## Verification Done (all via proxy `http://127.0.0.1:7897`)

- HF `/api/{models,datasets,spaces}` → 200, correct JSON. ✓
- `/api/{type}/{id}/tree/main/` → 200, `[{type,path,size,oid}]`. ✓
- `/{id}/raw/main/{path}` → 200, file content. ✓
- Pagination: wrong cursor → 400; correct Link-header cursor → next page. ✓
- Web search `/search?q=deepseek+sk-` → JS SPA, 0 results in HTML (not usable). ✓
- Endpoint `sk-` metadata search → 429 (rate-limited). ✓
- `extract_keys()` on synthetic hardcoded keys → finds both, filters `sk-proj-`. ✓
- **Full scanner run:** 50 models + 50 datasets + 50 spaces found, **0 keys**. ✓
- **Direct scan of 50 deepseek Spaces' source files:** **0 keys**. ✓
- Branch usage across 30 deepseek Spaces: `main=30, master=0`. ✓
