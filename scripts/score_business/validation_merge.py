"""Merge two-phase 00_validation.json after phase-2 fetch.

Replaces the inline `python3 -c` block previously in score-business
SKILL.md Step 4.5. Promoting to a script lets us:
1. Unit-test the merge logic
2. Avoid bash heredoc parameter quoting traps
3. Surface the rule explicitly: phase 2 wins on conflicts UNLESS phase 2's
   entry is a SKIPPED placeholder, in which case phase 1's live entry wins.

The reason: phase 2's fetch only ran for a subset of categories
(--categories 05_filing_summary etc.). For categories phase 2 didn't
fetch, phase 2 writes a SKIPPED stub; without this guard that stub
clobbers phase 1's PASSED entry and assemble's build_meta loses
data_freshness.

Usage:
    python3 -m scripts.score_business.validation_merge \\
        --phase1 /tmp/validation_phase1.json \\
        --phase2 reports/$T/$DATE/data/00_validation.json \\
        --output reports/$T/$DATE/data/00_validation.json

The --output defaults to --phase2 path (in-place merge).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def is_live_entry(entry: Any) -> bool:
    """An entry is 'live' (carries real data) if it's a dict whose
    status is NOT 'SKIPPED'."""
    return isinstance(entry, dict) and entry.get("status") != "SKIPPED"


def merge_validation(
    phase1: dict[str, Any], phase2: dict[str, Any]
) -> dict[str, Any]:
    """Merge phase 2 over phase 1 with the live-entry rule.

    - Per category: phase 2 wins IF it's a live entry; otherwise keep
      phase 1's live entry; otherwise emit phase 2's stub.
    - Top-level fields (tier_decided, validated_at, ticker, etc.): phase 2
      is terminal truth.
    - EXCEPTION — `is_adr` is monotonic within a run: only phase 1 fetches
      company+news and runs the authoritative ADR profile detector; the
      phase-2 subset fetch (filing/institutional) cannot determine ADR
      status and defaults to False. Taking phase 2's value verbatim would
      re-clobber a correctly-detected ADR (MRAAY/Murata) back to False. OR
      the two phases: a non-ADR stays False (neither phase sets True), a
      detected ADR stays True.
    """
    merged: dict[str, Any] = dict(phase2)  # phase 2 baseline (terminal truth)
    merged["is_adr"] = bool(phase1.get("is_adr")) or bool(phase2.get("is_adr"))
    p1_cats = phase1.get("categories", {}) if isinstance(phase1, dict) else {}
    p2_cats = phase2.get("categories", {}) if isinstance(phase2, dict) else {}

    merged_cats: dict[str, Any] = {}
    for key in set(p1_cats) | set(p2_cats):
        p2_entry = p2_cats.get(key)
        if is_live_entry(p2_entry):
            merged_cats[key] = p2_entry  # phase 2 has real data, wins
        elif key in p1_cats:
            merged_cats[key] = p1_cats[key]  # keep phase 1's live entry
        else:
            merged_cats[key] = p2_entry  # phase 2-only SKIPPED stub (legitimate)
    merged["categories"] = merged_cats
    return merged


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase1", required=True,
                   help="Path to phase 1 validation JSON (the saved baseline)")
    p.add_argument("--phase2", required=True,
                   help="Path to phase 2 validation JSON (terminal post-fetch)")
    p.add_argument("--output", default=None,
                   help="Output path. Defaults to --phase2 (in-place merge).")
    args = p.parse_args(argv)

    p1_path = Path(args.phase1)
    p2_path = Path(args.phase2)

    if not p1_path.exists():
        print(f"FATAL: phase1 path {p1_path} not found", file=sys.stderr)
        return 1
    if not p2_path.exists():
        print(f"FATAL: phase2 path {p2_path} not found", file=sys.stderr)
        return 1

    try:
        phase1 = json.loads(p1_path.read_text(encoding="utf-8"))
        phase2 = json.loads(p2_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"FATAL: validation merge failed to parse JSON: {e}",
              file=sys.stderr)
        return 1

    merged = merge_validation(phase1, phase2)
    out_path = Path(args.output) if args.output else p2_path
    # F13 (codex review cycle 2): atomic write via write_output instead of
    # raw write_text. Interrupted writes would otherwise leave a torn
    # validation file that the downstream assemble step consumes.
    from scripts.cli_utils import write_output as _atomic_write
    _atomic_write(merged, str(out_path))
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
