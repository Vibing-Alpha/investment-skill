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
    rename succeeds but the text rename fails, JSON lands alone and
    the text tmp is removed — which is acceptable because `decisions.json`
    is the audit canonical and the MD is a view over it; a rerun
    regenerates the MD from the same committed JSON.

    Per-file atomicity is guaranteed by os.replace; the pair is NOT
    atomic with respect to an external observer reading both files
    between the two replaces (brief window where JSON is new but MD
    is old/absent). Callers that need strict joint consistency should
    treat missing/stale MD as "rerun required".
    """
    json_out = json.dumps(json_data, indent=2, ensure_ascii=False) + "\n"
    json_dir = Path(json_path).resolve().parent
    text_dir = Path(text_path).resolve().parent
    json_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    j_fd, j_tmp = tempfile.mkstemp(dir=str(json_dir), suffix=".tmp")
    t_fd, t_tmp = tempfile.mkstemp(dir=str(text_dir), suffix=".tmp")
    try:
        with os.fdopen(j_fd, "w", encoding="utf-8") as f:
            f.write(json_out)
        j_fd = None
        with os.fdopen(t_fd, "w", encoding="utf-8") as f:
            f.write(text)
        t_fd = None
        # Both tmps are on disk; commit in canonical-first order.
        os.replace(j_tmp, json_path)
        j_tmp = None
        os.replace(t_tmp, text_path)
        t_tmp = None
    finally:
        # Cleanup any unclaimed tmp paths on early failure.
        for tmp in (j_tmp, t_tmp):
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
