"""Compute the orchestrator-owned `capital_efficiency` on investment_thesis.json.

CE = expected_return / |max_downside| is a pure, deterministic function of two
fields the synthesis agent already produces. Leaving the division to the LLM
made CE LLM-trusted arithmetic — and that drifts: a live 2026-05-22 MRVL
artifact stored `capital_efficiency=0.37` while `ER/|MD| = -17.6/47.0 = -0.37`
(the agent flipped the SIGN — a bearish thesis read as favorable risk/reward in
the portfolio review). Per CLAUDE.md's deterministic-computation principle and
rules/producer-consumer.md (one implementation, not two), CE is now computed
downstream here, NOT by the agent.

Contract:
- ER is None (not-computable valuation) -> CE is None. CE travels with ER
  (CE = ER/|MD|, so CE is not-computable exactly when ER is).
- ER finite + MD a finite non-zero number -> CE = round(ER/|MD|, 4).
- ER finite but MD unusable (None / 0 / non-finite) -> CE None here; the
  schema's max_downside gate (SKILL.md Step 6.4) then fails the run with a
  clear max_downside error. We do NOT crash on a bad MD (defensive), and we do
  NOT fabricate a CE from a zero/garbage denominator.

Idempotent + non-destructive: only `capital_efficiency` is (re)written; every
other field is preserved. Run in SKILL.md Step 6.3, AFTER stamp_thesis_meta and
synthesis write investment_thesis.json, BEFORE the Step 6.4 contract validation.

Usage:
    python3 -m scripts.thesis.compute_thesis_ce \\
        --report-dir reports/AAPL/20260522
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def _finite_number(x: Any) -> bool:
    """True for a real finite int/float (NOT bool, NOT NaN/Inf, NOT str)."""
    return (
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
    )


def compute_ce(
    expected_return: Any, max_downside: Any, *, ndigits: int = 4
) -> float | None:
    """Deterministic capital efficiency CE = ER / |max_downside|.

    Returns None when ER is None (not computable), or when max_downside is
    unusable for division (None / 0 / non-finite / wrong-type) — in the latter
    case the schema's max_downside gate surfaces the bad value; we never divide
    by zero and never invent a CE from a garbage denominator.
    """
    if expected_return is None:
        return None
    if not _finite_number(expected_return):
        # ER present but not a finite number — schema rejects ER; CE undefined.
        return None
    if not _finite_number(max_downside) or max_downside == 0:
        # MD unusable for division — schema rejects MD; CE undefined.
        return None
    ce = round(expected_return / abs(max_downside), ndigits)
    if not math.isfinite(ce):
        # |max_downside| so small the ratio overflowed to inf — an
        # economically absurd denominator. Honor the float|None contract
        # (never return inf/nan); the schema then fail-closes.
        return None
    if ce == 0:
        # Normalize -0.0 (round of a tiny bearish ratio, e.g. ER=-1e-7) to 0.0
        # so the artifact never carries a confusing negative zero.
        return 0.0
    return ce


def stamp_ce(thesis: dict[str, Any]) -> dict[str, Any]:
    """Set `thesis['capital_efficiency']` deterministically from ER + MD."""
    thesis["capital_efficiency"] = compute_ce(
        thesis.get("expected_return"), thesis.get("max_downside")
    )
    return thesis


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report-dir", required=True, help="reports/{T}/{YYYYMMDD}/")
    args = p.parse_args(argv)

    thesis_path = Path(args.report_dir) / "investment_thesis.json"
    if not thesis_path.is_file():
        print(f"FATAL: {thesis_path} not found", file=sys.stderr)
        return 1

    try:
        thesis = json.loads(thesis_path.read_text(encoding="utf-8"))
        if not isinstance(thesis, dict):
            print(
                f"FATAL: {thesis_path} top-level is "
                f"{type(thesis).__name__}, expected object",
                file=sys.stderr,
            )
            return 1
        stamp_ce(thesis)
        # Atomic write (temp + os.replace) — mirrors stamp_thesis_meta /
        # reuse_events so a crash mid-write can't leave a partial artifact.
        from scripts.cli_utils import write_output as _atomic_write
        _atomic_write(thesis, str(thesis_path))
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"FATAL: compute_thesis_ce failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    print(
        f"computed capital_efficiency={thesis['capital_efficiency']} "
        f"(ER={thesis.get('expected_return')}, MD={thesis.get('max_downside')})",
        file=sys.stderr,
    )
    print(str(thesis_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
