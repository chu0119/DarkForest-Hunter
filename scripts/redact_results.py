"""Redact full API keys from scan-result files before they leave the machine.

Why: CI artifacts, shared reports, and any external hand-off must never contain
a usable credential. This module masks anything that looks like an API key
(``sk-…``, ``sk-ant-…``, ``sk-proj-…``, MiniMax-style ``eyJ…`` JWT) — both when
it appears as a JSON object *value* (field named ``key``) and when it appears as
a JSON object *member name* (e.g. ``watch_state.json``'s ``{"keys": {"sk-…": …}}``).

Public API:
    - ``looks_like_credential(s)`` — heuristic predicate
    - ``mask(s)`` — render a credential non-usable (``sk-aBcD9fGh…Gh2a``)
    - ``redact_data(obj)`` — recursively redact a parsed JSON structure
    - ``redact_file(path)`` — read+redact one JSON file, return the new object
    - ``main()`` — CLI: redact every ``*.json`` / ``*.csv`` under a results dir

Exit code is non-zero if any *output* still contains a credential string that
appeared in the input — a fail-safe so a bug here can't silently leak keys.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

# Credential shapes we treat as sensitive. Order matters: longer prefixes first.
_CRED_RE = re.compile(r"^(sk-proj-|sk-ant-|sk-|eyJ)[A-Za-z0-9_\-]{20,}$")


def looks_like_credential(s: str) -> bool:
    """True if ``s`` looks like a full API key / JWT we must mask."""
    return isinstance(s, str) and bool(_CRED_RE.match(s))


def mask(s: str) -> str:
    """Return a non-usable preview of a credential (first 8 … last 4)."""
    if not isinstance(s, str) or len(s) < 12:
        return "[REDACTED]"
    return f"{s[:8]}…{s[-4:]}"


def redact_data(obj):
    """Recursively redact credentials from a parsed JSON structure.

    Masks:
      - dict values stored under a field literally named ``key`` (or ``完整Key``)
      - any JSON member *name* that is itself a credential (watch_state layout)
    """
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            new_key = mask(k) if looks_like_credential(str(k)) else k
            new[new_key] = redact_data(v)
        # Mask credential-valued fields regardless of recursion outcome.
        for fname in ("key", "完整Key", "api_key", "token"):
            if fname in new and looks_like_credential(str(new[fname])):
                new[fname] = mask(str(new[fname]))
        return new
    if isinstance(obj, list):
        return [redact_data(x) for x in obj]
    return obj


def redact_file(path: str | Path):
    """Read a JSON result file and return its redacted object (or None on error)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return redact_data(data)


def _redact_csv(src: Path, dst: Path) -> bool:
    """Write a CSV with any full-key column dropped/masked. Returns True if written."""
    try:
        with open(src, encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except OSError:
        return False
    if not rows:
        return False
    header = rows[0]
    # Drop columns whose header indicates a full credential.
    keep = [i for i, h in enumerate(header)
            if h.strip() not in ("完整Key", "key", "api_key", "token")]
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        for row in rows:
            writer.writerow([row[i] if i < len(row) else "" for i in keep])
    return True


def _contains_any(text: str, creds: set[str]) -> str | None:
    """Return the first credential from ``creds`` still present in ``text``."""
    for c in creds:
        if c and c in text:
            return c
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    src = Path(argv[argv.index("--src") + 1]) if "--src" in argv else Path("results")
    dst = Path(argv[argv.index("--dst") + 1]) if "--dst" in argv else Path("results_redacted")

    if not src.is_dir():
        print(f"[redact] source dir not found: {src}", file=sys.stderr)
        return 1

    dst.mkdir(parents=True, exist_ok=True)
    seen_creds: set[str] = set()
    leaks: list[str] = []

    # Collect every credential-looking string from raw inputs first (for fail-safe).
    raw_blobs: list[str] = []
    for p in sorted(src.glob("*.json")):
        try:
            raw_blobs.append(p.read_text(encoding="utf-8"))
        except OSError:
            pass
    for blob in raw_blobs:
        for m in re.findall(r"(sk-proj-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_\-]{20,})", blob):
            seen_creds.add(m)

    # JSON redaction.
    n_json = 0
    for p in sorted(src.glob("*.json")):
        redacted = redact_file(p)
        if redacted is None:
            continue
        out_path = dst / p.name
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(redacted, f, ensure_ascii=False, indent=2)
        # Fail-safe: ensure no input credential survives in the output.
        serialized = json.dumps(redacted, ensure_ascii=False)
        leaked = _contains_any(serialized, seen_creds)
        if leaked:
            leaks.append(f"{p.name}: still contains credential")
        n_json += 1

    # CSV redaction (drop full-key column).
    n_csv = 0
    for p in sorted(src.glob("*.csv")):
        if _redact_csv(p, dst / p.name):
            n_csv += 1

    print(f"[redact] wrote {n_json} json + {n_csv} csv → {dst}")
    if leaks:
        print("[redact] FAIL-SAFE: credential still present in output:", file=sys.stderr)
        for lk in leaks:
            print(f"  - {lk}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
