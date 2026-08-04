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
    """An entry is 'live' (carries real data) if it's a dict with a PRESENT,
    non-empty status that is not 'SKIPPED'.

    The presence requirement is load-bearing (eleventh cold round): fetch.py
    writes a status into every entry, so a statusless `{}` is corruption —
    and "not SKIPPED" alone counted it as live, letting a corrupt phase-2
    stub REPLACE a real phase-1 failure record (status, error_code, detail).
    A merge must never prefer corruption over evidence. A non-canonical but
    present status (e.g. "WARN") still counts as live — vocabulary policing
    is assemble's job, evidence-preservation is this function's."""
    return (isinstance(entry, dict)
            and bool(entry.get("status"))
            and entry.get("status") != "SKIPPED")


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
    - EXCEPTION — `fmp_fallback`, for the same reason. Only phase 1 fetches
      the categories FMP back-fills (financials/metrics/analyst/earnings);
      the phase-2 subset fetch never runs the fallback and writes `None`.
      Since phase 2 is the baseline for every top-level field, that `None`
      replaced phase 1's audit record while phase 1's CATEGORY statuses
      survived — so a category left at `FAILED/not_found` by a fallback that
      was rate-limited kept its status but lost the evidence that the
      fallback never confirmed the absence. `assemble._summarize_degradation`
      reads exactly that record to decide whether the structural exemption
      applies, and with it gone it read the run as legacy and exempted the
      category: reproduced as `["earnings"]` before the merge and `[]` after.
      Corpus confirms the reach — the only two stored runs that still carry a
      record are `tier=probe` and `tier=no_op`, the two that never merge.
      Phase 2 wins only if it actually has a record.
    """
    merged: dict[str, Any] = dict(phase2)  # phase 2 baseline (terminal truth)
    merged["is_adr"] = bool(phase1.get("is_adr")) or bool(phase2.get("is_adr"))
    # Probe-2 review round-3: `is_tech_stock` is monotonic within a run for
    # the same reason as is_adr — only phase 1 fetches company data, so the
    # phase-2 subset defaults it False and would re-clobber a correct True.
    merged["is_tech_stock"] = (bool(phase1.get("is_tech_stock"))
                               or bool(phase2.get("is_tech_stock")))
    p2_fb = phase2.get("fmp_fallback") if isinstance(phase2, dict) else None
    if not isinstance(p2_fb, dict):
        merged["fmp_fallback"] = (phase1.get("fmp_fallback")
                                  if isinstance(phase1, dict) else None)
    # Probe-2 review round-3: same evidence-preservation rule for the
    # yfinance_fallback audit record — the phase-2 subset never attempts
    # the fallback (attempted=False stub), which replaced phase 1's real
    # record while phase 1's rescued category data survived. Phase 2 wins
    # only if it actually ATTEMPTED.
    p2_yf = phase2.get("yfinance_fallback") if isinstance(phase2, dict) else None
    if not (isinstance(p2_yf, dict) and p2_yf.get("attempted")):
        p1_yf = phase1.get("yfinance_fallback") if isinstance(phase1, dict) else None
        if isinstance(p1_yf, dict):
            merged["yfinance_fallback"] = p1_yf
    p1_cats = phase1.get("categories", {}) if isinstance(phase1, dict) else {}
    p2_cats = phase2.get("categories", {}) if isinstance(phase2, dict) else {}

    merged_cats: dict[str, Any] = {}
    for key in set(p1_cats) | set(p2_cats):
        p2_entry = p2_cats.get(key)
        if is_live_entry(p2_entry):
            merged_cats[key] = p2_entry  # phase 2 has real data, wins
        elif key in p1_cats:
            merged_cats[key] = p1_cats[key]  # keep phase 1's live entry
        # else: OMIT the key (thirtieth cold round). Both phases write EVERY
        # category — SKIPPED stubs included — so a key absent from phase 1 is
        # truncation evidence, and synthesizing phase 2's SKIPPED stub into
        # the gap LAUNDERED that evidence past the downstream completeness
        # checks: the merged map read gating-complete, SKIPPED read clean,
        # and an unknown loss became an explicitly clean run. A LIVE phase-2
        # entry (the branch above) is real evidence and stands on its own;
        # a stub is not, and the missing key is exactly what the
        # stored-validation shape check exists to catch.
    merged["categories"] = merged_cats

    # Probe-2 C2a: the top-level status must describe the MERGED map, not
    # phase 2's subset-local run. Pre-fix a clean merged map stayed
    # labeled PARTIAL (phase 2's local truth: everything-else-SKIPPED),
    # contradicting its own categories all the way into
    # bq_analysis.meta.validation_status. Recompute via the ONE shared
    # matrix (scripts.fetch.derive_overall_validation_status).
    from scripts.fetch import derive_overall_validation_status
    merged["status"] = derive_overall_validation_status(merged_cats)

    # Probe-2 C2b: root `growth_stock_mode` follows the live-entry rule
    # like any category — phase 2's subset fetch never runs the detector
    # and writes a SKIPPED stub, which previously replaced phase 1's live
    # record (root said SKIPPED/disabled while categories.growth_stock_mode
    # was live — contradictory disclosure in the stored artifact).
    p2_growth = phase2.get("growth_stock_mode")
    p1_growth = phase1.get("growth_stock_mode") if isinstance(phase1, dict) else None
    if not is_live_entry(p2_growth) and is_live_entry(p1_growth):
        merged["growth_stock_mode"] = p1_growth

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
    print(out_path.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
