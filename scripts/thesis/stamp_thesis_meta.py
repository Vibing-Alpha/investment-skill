"""Stamp the orchestrator-owned `meta` block onto investment_thesis.json.

`investment_thesis.json` is 100% LLM-authored (unlike `bq_analysis.json`,
which `scripts/assemble.py` assembles mechanically). The typed loader
`scripts/schemas/investment_thesis.py:_validate_meta` REQUIRES
`meta.{ticker, analysis_date, generated_at}` and the orchestration skill
fails closed (Step 6.4) if they are absent or malformed.

Leaving those required fields to the synthesis agent makes the contract
depend on probabilistic LLM compliance: a fresh /investment-thesis run on
2026-05-24 (LITE) produced an artifact with no `meta` at all and aborted
the run. This stamper makes the three fields DETERMINISTIC — the
orchestrator already owns ticker + run-date context, so it stamps them
authoritatively, mirroring:
  - `assemble.py` mechanically stamping `bq_analysis.json.meta`, and
  - `evaluate-events.md` delegating date-stamping to the orchestrator
    rather than the agent.

Idempotent + non-destructive: any agent-emitted `meta.*` extras
(`current_price`, `current_price_source`, …) are preserved; only the three
loader-required fields are set authoritatively.

Run in SKILL.md Step 6.3, AFTER synthesis writes investment_thesis.json and
BEFORE the Step 6.4 contract validation.

Usage:
    python3 -m scripts.thesis.stamp_thesis_meta \\
        --report-dir reports/AAPL/20260522 \\
        --ticker AAPL \\
        [--analysis-date 2026-05-24]   # default: today_et() = ET calendar run date (matches bq)
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

from scripts.delta.calendar import today_et


def stamp_meta(
    thesis: dict[str, Any],
    *,
    ticker: str,
    analysis_date: str,
    now_utc: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Return `thesis` with a loader-valid `meta` block.

    Sets `meta.{ticker, analysis_date, generated_at}` authoritatively while
    preserving any pre-existing `meta.*` keys the agent emitted (e.g.
    `current_price`, `current_price_source`). `generated_at` is the stamping
    time in UTC ISO-8601 with a `Z` suffix — guaranteed to satisfy the
    loader's timestamp regex regardless of what (if anything) the agent
    produced.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    generated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    existing = thesis.get("meta")
    meta: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    meta["ticker"] = ticker
    meta["analysis_date"] = analysis_date
    meta["generated_at"] = generated_at
    thesis["meta"] = meta
    return thesis


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report-dir", required=True, help="reports/{T}/{YYYYMMDD}/")
    p.add_argument("--ticker", required=True)
    p.add_argument(
        "--analysis-date",
        default="",
        help="ISO YYYY-MM-DD; defaults to today's ET CALENDAR date via "
        "today_et() (the run date, not the session being analyzed — that "
        "is market_asof_date=last_closed_trading_day in bq_analysis). This "
        "is the SAME source assemble.py stamps onto "
        "bq_analysis.json.meta.analysis_date, so the two artifacts in one "
        "run dir always agree (a UTC default diverged by a day near the "
        "ET/UTC midnight boundary).",
    )
    args = p.parse_args(argv)

    # Fail-closed on a missing/blank ticker rather than stamp an empty
    # meta.ticker that the loader would then reject with a less obvious
    # error. SKILL.md Step 0 already validates the ticker, but this keeps
    # the standalone CLI self-defending (producer-consumer rule #4).
    ticker = args.ticker.strip()
    if not ticker:
        print("FATAL: --ticker must be non-empty", file=sys.stderr)
        return 2

    report_dir = Path(args.report_dir)
    thesis_path = report_dir / "investment_thesis.json"
    if not thesis_path.is_file():
        print(f"FATAL: {thesis_path} not found", file=sys.stderr)
        return 1

    analysis_date = args.analysis_date or today_et().isoformat()

    try:
        thesis = json.loads(thesis_path.read_text(encoding="utf-8"))
        if not isinstance(thesis, dict):
            print(
                f"FATAL: {thesis_path} top-level is "
                f"{type(thesis).__name__}, expected object",
                file=sys.stderr,
            )
            return 1
        stamp_meta(thesis, ticker=ticker, analysis_date=analysis_date)
        # Atomic write (temp + os.replace) so a crash mid-write can't leave a
        # partial artifact for Step 6.4 to ingest — mirrors reuse_events.py.
        from scripts.cli_utils import write_output as _atomic_write
        _atomic_write(thesis, str(thesis_path))
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"FATAL: stamp_thesis_meta failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    print(f"stamped meta: ticker={ticker} analysis_date={analysis_date}",
          file=sys.stderr)
    print(str(thesis_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
