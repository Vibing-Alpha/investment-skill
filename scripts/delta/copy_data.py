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

    Existing-destination guard (cold-round 5 — the data-file sibling of
    copy_dimension_scores' probe-4B guard): a destination file that already
    EXISTS is never overwritten. On a same-session rerun the resolver's
    prior is YESTERDAY, so a blind copy would roll a same-session FRESH
    fetch (e.g. this morning's full-tier 08_institutional) backward to the
    prior day's copy. An existing dst is either that fresher fetch (keep)
    or an identical earlier copy from the same prior (skip = no-op) —
    skip-if-exists is safe in both cases and keeps the copy idempotent.
    """
    import sys

    dst_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    seen: set[Path] = set()

    def _copy_if_absent(src_file: Path) -> None:
        dst_file = dst_dir / src_file.name
        if dst_file.exists():
            print(
                f"[copy_data_categories] SKIP {src_file.name}: destination "
                f"already exists (same-session fresh fetch or prior copy) — "
                f"not rolling it backward.",
                file=sys.stderr,
            )
            return
        shutil.copy2(src_file, dst_file)
        written.append(dst_file)

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
                _copy_if_absent(src_file)
        else:
            src_file = src_dir / f"{cat}.json"
            if src_file.exists() and src_file not in seen:
                seen.add(src_file)
                _copy_if_absent(src_file)
    return written


def copy_dimension_scores(
    src_dir: Path, dst_dir: Path, dimensions: Iterable[str], source_date: str
) -> dict:
    """Copy dimension score JSONs with inline provenance stamp.

    Adds `_source_date` and `_reason` fields to the top level of each
    copied file so downstream readers know it was reused.

    Probe 4B — fresh-destination guard: when the destination score already
    exists WITHOUT a `_source_date`, it is a FRESH same-session recompute
    (fresh agent output never carries the stamp). A later same-session
    no_op/partial rerun resolves its prior to YESTERDAY (the resolver
    excludes today) and would otherwise overwrite today's fresh scores
    with day-old copies. Never downgrade fresh content — skip, log, and
    report. Destinations that are themselves copies (stamped) stay
    overwritable (idempotent re-copy).

    Probe 4H — vintage preservation: when the SOURCE file already carries
    `_source_date` (it was itself a copy), that original fresh vintage is
    preserved verbatim; only a fresh source gets stamped with
    `source_date` (the prior dir's date). Chained no_ops therefore no
    longer launder provenance newer with every hop.

    Returns {"copied": [...], "skipped_fresh": [...]} (dimension names).
    """
    import sys

    dst_dir.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    skipped_fresh: List[str] = []
    for dim in dimensions:
        src_file = src_dir / f"{dim}.json"
        if not src_file.exists():
            continue
        dst_file = dst_dir / src_file.name
        if dst_file.exists():
            try:
                with open(dst_file, "r", encoding="utf-8") as f:
                    dst_data = json.load(f)
                dst_is_fresh = (
                    isinstance(dst_data, dict)
                    and "_source_date" not in dst_data
                )
            except (json.JSONDecodeError, OSError):
                dst_is_fresh = False  # unreadable dst → replace with the copy
            if dst_is_fresh:
                skipped_fresh.append(dim)
                print(
                    f"[copy_dimension_scores] SKIP {dim}: destination is a "
                    f"fresh same-session score (no _source_date) — not "
                    f"overwriting with a prior-run copy (probe 4B guard).",
                    file=sys.stderr,
                )
                continue
        with open(src_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 4H: preserve the original fresh vintage on chained copies.
        if "_source_date" not in data:
            data["_source_date"] = source_date
        data["_reason"] = "copied from prior run"
        # Atomic write (project convention — matches write_output in
        # every other CLI script).
        write_output(data, str(dst_file))
        copied.append(dim)
    return {"copied": copied, "skipped_fresh": skipped_fresh}


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
