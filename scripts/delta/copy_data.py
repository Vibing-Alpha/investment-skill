"""Copy unchanged data/scores files between run dirs with provenance stamps.

Pure shutil.copy2 (preserves mtime for debugging). Never symlinks or
hardlinks — the self-contained-dir invariant in spec §3.1 requires
real copies.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterable, List

from scripts.cli_utils import write_output


def copy_data_categories(
    src_dir: Path, dst_dir: Path, categories: Iterable[str]
) -> List[Path]:
    """Copy each `{category}.json` (or glob matches for patterns like `05_filing_*`)
    from src_dir to dst_dir. Returns list of destination paths actually written.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    seen: set[Path] = set()
    for cat in categories:
        if "*" in cat:
            # Glob pattern (e.g. 05_filing_*). A single `{cat}.*` glob
            # matches any extension including .json, so de-dup across
            # matches instead of doing two overlapping globs.
            pattern = cat if "." in cat else f"{cat}.*"
            for src_file in sorted(src_dir.glob(pattern)):
                if src_file in seen:
                    continue
                seen.add(src_file)
                dst_file = dst_dir / src_file.name
                shutil.copy2(src_file, dst_file)
                written.append(dst_file)
        else:
            src_file = src_dir / f"{cat}.json"
            if src_file.exists() and src_file not in seen:
                seen.add(src_file)
                dst_file = dst_dir / src_file.name
                shutil.copy2(src_file, dst_file)
                written.append(dst_file)
    return written


def copy_dimension_scores(
    src_dir: Path, dst_dir: Path, dimensions: Iterable[str], source_date: str
) -> None:
    """Copy dimension score JSONs with inline provenance stamp.

    Adds `_source_date` and `_reason` fields to the top level of each
    copied file so downstream readers know it was reused.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    for dim in dimensions:
        src_file = src_dir / f"{dim}.json"
        if not src_file.exists():
            continue
        with open(src_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["_source_date"] = source_date
        data["_reason"] = "copied from prior run"
        # Atomic write (project convention — matches write_output in
        # every other CLI script).
        write_output(data, str(dst_dir / src_file.name))


import re
from datetime import datetime

# Explicit allow-list of date keys that get rewritten on reuse.
# Using a broad suffix pattern (`.*_date$`) would silently corrupt
# provenance fields like `prior_bq_analysis_date` (observed in real
# ONTO events.json) — they are date values but must NOT be rewritten.
#
# Keep this list tight. If a new date-like field needs to be rewritten
# on reuse, add it here explicitly — don't broaden the pattern.
# EXCLUSIONS from rewrite (provenance fields — must never be touched):
#   - `generated_at` — the original generation timestamp IS the anchor for
#     "when was this content freshly produced". Rewriting it to today
#     breaks anchor-preservation across chains of reuse.
#   - `reuse_meta.copied_at` — tracks each copy's timestamp (provenance).
#   - `reuse_meta.reused_from` — the preserved fresh anchor itself.
# These are intentionally NOT in the allow-list below.
_DATE_KEYS_TO_REWRITE = {
    "as_of_date",
    "analysis_date",
    "market_asof_date",
    "generated_date",
    # NOTE: "generated_at" is DELIBERATELY excluded (provenance, not analysis date).
}
_ISO_DATETIME_PREFIX_LEN = 10  # "YYYY-MM-DD" prefix of an ISO-8601 datetime


def rewrite_stale_date_fields(obj: dict, today_iso: str) -> None:
    """In-place rewrite of date-typed keys to today_iso, at the top level
    AND one level deep into a nested `meta` object (defensive for
    legacy events.json files that stuck dates under meta.*).

    Accepts both plain `YYYY-MM-DD` (length 10) AND ISO datetime strings
    (e.g. `2026-04-15T11:33:11Z`). ISO datetimes are rewritten to just
    today's date (dropping the time) — the reused-file semantics is
    "this content applies to today", not "today at the original time".

    Rationale for the meta.* recursion: inspection of actual production
    events.json files (Apr 2026) showed ~50% of them nest
    `meta.analysis_date` instead of using a top-level date field. The
    post-delta `evaluate-events.md` prompt is amended to stop emitting
    these nested dates, but reused files from pre-amendment runs will
    still carry them. Recursing one level into meta catches those.
    Does NOT recurse into other nested structures (arbitrary depth
    rewriting is risky — only `meta` has an established convention
    for date fields).
    """
    _rewrite_in_place(obj, today_iso)
    meta = obj.get("meta")
    if isinstance(meta, dict):
        _rewrite_in_place(meta, today_iso)


def _rewrite_in_place(d: dict, today_iso: str) -> None:
    for key in list(d.keys()):
        # Case-insensitive match against the explicit allow-list
        if key.lower() not in _DATE_KEYS_TO_REWRITE:
            continue
        value = d[key]
        if not isinstance(value, str):
            continue
        if len(value) == 10 and _looks_like_iso_date(value):
            d[key] = today_iso
            continue
        if len(value) >= _ISO_DATETIME_PREFIX_LEN and _looks_like_iso_date(
            value[:_ISO_DATETIME_PREFIX_LEN]
        ):
            d[key] = today_iso


def _looks_like_iso_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False
