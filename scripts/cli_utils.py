"""Shared CLI helper functions for scripts with JSON I/O.

Used by scripts/adr/detect.py, scripts/adr/correct.py, and scripts/normalize.py.
Each caller passes a `prefix` string (e.g. "adr.detect") for diagnostic messages.
"""

import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

# CJK ideographs (incl. Ext-A) + Japanese kana + Korean Hangul syllables.
# These scripts have no inter-word whitespace, so `wc -w` / str.split()
# undercounts them. CJK punctuation/full-width blocks are deliberately
# excluded — they are not "words".
_CJK_CHAR_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")
# Conservative chars-per-word for the SOFT summary budget. Real EN<->CJK
# translation density is ~1.5-1.7; 2.0 biases a soft warning toward
# false-negatives (don't nag on good one-pagers) over false-positives.
_CJK_CHARS_PER_WORD = 2


def read_json(path_str, label, prefix):
    """Read and parse a JSON file. Exit with stderr diagnostic on failure.

    Args:
        path_str: Path to the JSON file.
        label: Human-readable label for error messages (e.g. "--facts-json").
        prefix: Caller name for the diagnostic prefix (e.g. "adr.detect").
    """
    path = Path(path_str)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{prefix}: failed to read {label} {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def write_output(result, output_path):
    """Write JSON result to stdout or file (atomic write via temp+rename).

    Args:
        result: Data structure to serialize as JSON.
        output_path: File path string, or None/empty for stdout.
    """
    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        out_dir = Path(output_path).resolve().parent
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(out_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
                tmp_f.write(output_json)
                tmp_f.write("\n")
            os.replace(tmp_path, output_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    else:
        print(output_json)


def emit_dl3c_root_marker(result: dict, version: int = 1) -> dict:
    """Return a new dict with `_dl3c_version` as the FIRST key (insertion
    order = serialization order per PEP 468). Other keys preserved in
    their original order. Idempotent: re-running on an already-marked
    dict produces the same shape."""
    new = {"_dl3c_version": version}
    for k, v in result.items():
        if k != "_dl3c_version":
            new[k] = v
    return new


def write_text_atomic(text, output_path):
    """Write text to file via temp+rename (atomic).

    Mirrors write_output() but takes pre-rendered text (e.g. markdown)
    instead of a JSON-serializable dict. Crash-safe: a partial write
    never leaves a torn file at output_path.
    """
    out_dir = Path(output_path).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(out_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(text)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_pair_atomic(json_data, json_path, text, text_path):
    """Atomically-ish write a JSON+text pair sharing a logical commit boundary.

    Stages both files as tmp siblings first, then replaces in order:
    JSON (canonical) → text (derived). If either tmp write raises,
    both tmps are cleaned up and nothing is committed. If the JSON
    rename succeeds but the text rename FAILS (round-35: the realistic
    trigger is a Windows editor lock on the .md), the canonical JSON is
    ROLLED BACK to its prior content (or removed when there was none),
    so a run-B JSON never persists beside a run-A MD — the human
    deliverable and the audit canonical stay the same generation.

    Per-file atomicity is guaranteed by os.replace. Residual (documented):
    a hard process kill in the microseconds between the two replaces
    cannot roll back — the next run's archive step archives the MD under
    its OWN embedded run_id, so the torn pair never archives mixed.
    """
    json_out = json.dumps(json_data, indent=2, ensure_ascii=False) + "\n"
    json_dir = Path(json_path).resolve().parent
    text_dir = Path(text_path).resolve().parent
    json_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    j_fd, j_tmp = tempfile.mkstemp(dir=str(json_dir), suffix=".tmp")
    t_fd, t_tmp = tempfile.mkstemp(dir=str(text_dir), suffix=".tmp")
    j_backup = None
    try:
        with os.fdopen(j_fd, "w", encoding="utf-8") as f:
            f.write(json_out)
        j_fd = None
        with os.fdopen(t_fd, "w", encoding="utf-8") as f:
            f.write(text)
        t_fd = None
        # Snapshot the current canonical JSON so a failed text commit can
        # roll it back, then PARK it at a deterministic dot-file sibling
        # (feedback 2026-08-30(b) ①). The cleanup unlink below is
        # best-effort, and on the Cowork FUSE mount — which refuses unlink
        # of an existing file while still allowing writes and renames — a
        # mkstemp random name meant every same-day rerun accumulated
        # another ~100KB `tmpXXXXXX.bak` nobody could remove, visible to
        # every `*` glob of the run dir. A fixed name is overwritten by the
        # next run instead, so at most one hidden snapshot remains. Parked
        # by rename, not by an in-place open(): that mount truncates an
        # overwrite to the OLD length, which would hand the rollback path a
        # CORRUPT snapshot to restore over the canonical JSON. Parking is
        # housekeeping only — a refusal here (a rename-over-existing lock,
        # which the old unique name could not hit) degrades to the old
        # behaviour rather than costing the caller its commit.
        if os.path.exists(json_path):
            b_fd, j_backup = tempfile.mkstemp(dir=str(json_dir), suffix=".tmp")
            with os.fdopen(b_fd, "wb") as bf:
                with open(json_path, "rb") as cf:
                    bf.write(cf.read())
            j_parked = str(json_dir / ("." + Path(json_path).name + ".rollback"))
            try:
                os.replace(j_backup, j_parked)
                j_backup = j_parked
            except OSError:
                pass
        # Both tmps are on disk; commit in canonical-first order.
        os.replace(j_tmp, json_path)
        j_tmp = None
        try:
            os.replace(t_tmp, text_path)
            t_tmp = None
        except OSError:
            # Roll the canonical JSON back so the pair stays one generation.
            try:
                if j_backup is not None:
                    os.replace(j_backup, json_path)
                    j_backup = None
                else:
                    os.unlink(json_path)
            except OSError:
                pass  # rollback best-effort; the raise below is loud
            raise
        if j_backup is not None:
            try:
                os.unlink(j_backup)
            except OSError:
                pass
            j_backup = None
    finally:
        # Cleanup any unclaimed tmp paths on early failure.
        for tmp in (j_tmp, t_tmp, j_backup):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def parse_bool_flag(value, flag_name, prefix):
    """Parse a string bool flag. Exit with stderr diagnostic on invalid value.

    Args:
        value: The raw string value to parse.
        flag_name: Flag name for error messages (e.g. "--is-adr").
        prefix: Caller name for the diagnostic prefix (e.g. "adr.detect").
    """
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    print(f"{prefix}: {flag_name} must be 'true' or 'false', got '{value}'", file=sys.stderr)
    sys.exit(1)


import re as _re

# Canonical US-ticker symbology filter (probe 3A/3C/3D — ONE
# implementation; scripts/screen.py aliases this). Accepts AAPL, BRK.B,
# BRK-B, BF-B, TEST1. Rejects $AAPL, NVDA;, ../etc, unicode, glob junk
# like AGENTS.MD (stem >5 chars), anything path-unsafe.
TICKER_RE = _re.compile(r"^[A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?$")


def normalize_ticker(raw):
    """Canonicalize a user-supplied ticker: strip + upper, then validate.

    Probe 3A: portfolio-state.yaml is hand-edited — a lowercase /
    whitespace-padded ticker exact-matched nothing in the resolver and
    SILENTLY excluded the position from /portfolio and /monitor (worst
    failure mode for a real-money tool). Probe 3B: the same missing
    boundary let path-y strings (`AMD/../NVDA`, absolute paths) reach
    resolver filesystem lookups via direct calls.

    Returns the canonical form. Raises ValueError (loud, fail-closed) on
    empty / non-str / path-unsafe / non-symbology input — callers at user
    boundaries surface the message telling the user which entry to fix.
    """
    if not isinstance(raw, str):
        raise ValueError(
            f"ticker must be a string, got {type(raw).__name__}"
        )
    t = raw.strip().upper()
    if not t:
        raise ValueError(f"empty ticker (from {raw!r})")
    if not TICKER_RE.match(t):
        raise ValueError(
            f"invalid ticker {raw!r} (canonical form {t!r} does not match "
            f"US symbology {TICKER_RE.pattern!r}) — fix the entry in "
            "portfolio-state.yaml / the command argument"
        )
    return t


def normalize_percent_fraction(value):
    """Coerce a constraint value to a [0.0, 1.0] decimal fraction.

    Accepts either:
    - decimal in [0.0, 1.0] — returned unchanged (e.g. 0.35 == 35%)
    - percent-point in (1, 100] — divided by 100 (e.g. 35 → 0.35)

    Rejects: negative, >100, non-numeric, booleans, non-finite.

    None passes through unchanged so the helper can be used in
    optional-field contexts.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"percent value must be numeric (int or float), "
            f"got {type(value).__name__}"
        )
    if not math.isfinite(value):
        raise ValueError(f"percent value must be finite, got {value}")
    if value < 0:
        raise ValueError(f"percent value must be >= 0, got {value}")
    if value > 100:
        raise ValueError(f"percent value must be <= 100, got {value}")
    if value > 1.0:
        return value / 100.0
    return float(value)


def count_word_equivalents(text):
    """Language-robust word count for the soft one-page summary budget.

    `wc -w` (and `str.split()`) counts whitespace-delimited tokens, which
    drastically undercounts CJK text — Chinese/Japanese/Korean have no
    inter-word spaces, so an entire paragraph reads as one "word". Because
    the default `output_language` is zh-CN, the score-business /
    investment-thesis word-budget gates were a silent no-op for every
    Chinese summary.

    This counts non-CJK whitespace tokens (identical to `wc -w` when the
    text has no CJK — the existing English 800/600 thresholds are
    unchanged) plus CJK characters at ~2 chars/word. The gate stays a
    soft-fail; this only makes it actually fire for the default language.
    """
    cjk_chars = len(_CJK_CHAR_RE.findall(text))
    non_cjk_tokens = len(_CJK_CHAR_RE.sub(" ", text).split())
    return non_cjk_tokens + round(cjk_chars / _CJK_CHARS_PER_WORD)


# --- Segmented-revenues filing-notes fallback (ONE implementation) ----------
# The structured segmentation feed answers empty for many issuers, but the
# revenue mix is often recoverable from the filing's revenue-disaggregation
# note. When that note was persisted, a FAILED segmented category is demoted
# to PARTIAL and re-sourced rather than left as a flat failure.
#
# TWO call sites need this rule and they see the evidence in DIFFERENT shapes:
#   * scripts/fetch.py Category F2 reads `filing_content` (item -> text), which
#     is populated only when Category F ran in the SAME process.
#   * scripts/score_business/validation_merge.py reads the merged filing
#     entry's `items_detail` (item -> char count), which is how the fact
#     survives into the artifact.
# score-business fetches in two phases with DISJOINT category sets, so F2
# (gated on 02_financial_data) and the filing (gated on 05_filing_summary)
# never run in the same process there — which is why the merge has to
# re-evaluate the rule once both halves are known. Keeping the rule here
# rather than copying it into the merge is `.claude/rules/producer-consumer.md`
# §3: one implementation, not two.
_REVENUE_NOTES_10K = "10k_revenue_notes"
_REVENUE_NOTES_10Q = "10q_revenue_notes"


def revenue_notes_availability(item_keys):
    """Return (has_10k, has_10q) from any iterable of filing item names.

    Accepts either mapping the callers hold — `filing_content` (item -> text)
    or `items_detail` (item -> char count) — since both are keyed by the same
    item names. A None/empty input reads as "no notes", never as unknown.
    """
    keys = set(item_keys or ())
    return (_REVENUE_NOTES_10K in keys, _REVENUE_NOTES_10Q in keys)


def promote_segmented_on_filing_notes(entry, *, has_10k, has_10q):
    """Apply the FAILED -> PARTIAL filing-notes promotion to a category entry.

    Returns a NEW dict (callers hold references into their own inputs, so this
    must not mutate in place). Idempotent and FAILED-only: a PASSED feed, an
    already-promoted PARTIAL, or a run with no persisted notes comes back
    unchanged apart from the availability flags, which always describe what
    was actually persisted.
    """
    if not isinstance(entry, dict):
        return entry
    updated = dict(entry)
    available = has_10k or has_10q
    updated["filing_revenue_notes"] = {
        "10k_available": has_10k,
        "10q_available": has_10q,
        "fallback_status": "AVAILABLE" if available else "UNAVAILABLE",
    }
    if updated.get("status") == "FAILED" and available:
        updated["status"] = "PARTIAL"
        updated["source"] = "filing_revenue_notes"
    return updated
