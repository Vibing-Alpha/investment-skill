"""Classify a ticker into one of 5 staleness states (spec §8.1).

CLI: python3 -m scripts.delta.portfolio_classify --ticker TICKER [--reports-root PATH]

States (as consumed by the portfolio SKILL batch prompt):
- fresh        — last full-tier BQ <14 ET days AND a thesis within last 7 ET days
- stale_bq     — ≥14 ET days since last completed full-tier BQ run, OR no valid BQ
                 while thesis exists (orphan thesis; treat as "run BQ cascade")
- stale_thesis — BQ fresh (<14 days) but >7 ET days since last completed thesis
- bq_only      — has BQ run but no thesis run at all
- none         — no reports for this ticker

This is a READ-ONLY classifier. It approximates spec §8.1 — which
defines `stale_bq` as "probe would return tier > no_op" and
`stale_thesis` as "events reuse gates would fail" — with day-count
thresholds keyed to the partial-tier safety valve (14d) and events
reuse ceiling (7d). The probe itself stays authoritative: portfolio
orchestration cascades `/score-business` or `/investment-thesis` for
any stale-flagged ticker, and those paths re-probe with full
fidelity. Trade-off: one cheap scan per ticker beats probing every
portfolio ticker just to render the batch prompt.
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path
from typing import Optional

from scripts.delta.calendar import today_et
from scripts.delta.constants import (
    SKILL_BQ, SKILL_THESIS,
    STATE_FRESH, STATE_STALE_BQ, STATE_STALE_THESIS,
    STATE_BQ_ONLY, STATE_NONE,
)
from scripts.delta.resolver import find_latest_prior, DEFAULT_REPORTS_ROOT
from scripts.delta.run_meta import RunMeta


BQ_STALE_DAYS = 14       # ≥14 days since last full-tier BQ → stale_bq (partial-tier safety valve)
THESIS_STALE_DAYS = 7    # >7 days since last thesis → stale_thesis (events reuse ceiling_7d)


def _is_date_dirname(name: str) -> bool:
    return len(name) == 8 and name.isdigit()


def _days_since_last_full_bq(
    ticker: str,
    reports_root: Path,
    today: datetime.date,
) -> Optional[int]:
    """Walk {reports_root}/{ticker}/YYYYMMDD dirs in reverse, return days
    since the most recent completed full-tier BQ run, or None if none found.

    Spec §8.1 keys BQ staleness to the last FULL-tier run — partials
    don't reset the full-tier clock (safety valve logic in spec §7).
    Stops at the first full-tier hit, so cost is typically one read.
    Malformed `et_trading_day` or negative clock-skew deltas are
    filtered here so the caller can treat `None` as "no valid full
    anchor, refresh BQ".
    """
    ticker_dir = reports_root / ticker
    if not ticker_dir.exists():
        return None
    date_dirs = sorted(
        (p for p in ticker_dir.iterdir() if p.is_dir() and _is_date_dirname(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    for d in date_dirs:
        rm = RunMeta.load_or_none(d / "run_meta.json")
        if rm is None or rm.bq is None or not rm.bq.completed:
            continue
        if rm.bq.tier != "full":
            continue
        try:
            full_date = datetime.date.fromisoformat(rm.et_trading_day)
        except (ValueError, TypeError):
            continue
        return (today - full_date).days
    return None


def classify(ticker: str, reports_root: Optional[Path] = None) -> str:
    """Return one of: fresh | stale_bq | stale_thesis | bq_only | none."""
    root = reports_root or DEFAULT_REPORTS_ROOT
    bq_dir = find_latest_prior(ticker, SKILL_BQ, reports_root=root, include_today=True)
    thesis_dir = find_latest_prior(ticker, SKILL_THESIS, reports_root=root, include_today=True)

    if bq_dir is None and thesis_dir is None:
        return STATE_NONE
    if bq_dir is not None and thesis_dir is None:
        return STATE_BQ_ONLY
    # Thesis exists without a valid BQ — unusual; force a BQ refresh.
    if bq_dir is None:
        return STATE_STALE_BQ

    today = today_et()

    # BQ: days since last FULL-tier BQ run (not any BQ — partials do not
    # reset the full-tier clock per spec §7). Negative delta (future
    # date from clock skew) and None both fail-closed to stale_bq.
    full_days = _days_since_last_full_bq(ticker, root, today)
    if full_days is None or full_days < 0 or full_days >= BQ_STALE_DAYS:
        return STATE_STALE_BQ

    # Thesis: days since last completed thesis run. Guard malformed
    # `et_trading_day` with fail-closed stale_thesis rather than let
    # the ValueError propagate up the portfolio orchestration.
    thesis_rm = RunMeta.load_or_none(thesis_dir / "run_meta.json")
    if thesis_rm is None:
        return STATE_STALE_THESIS
    try:
        th_date = datetime.date.fromisoformat(thesis_rm.et_trading_day)
    except (ValueError, TypeError):
        return STATE_STALE_THESIS
    th_days = (today - th_date).days
    if th_days < 0 or th_days > THESIS_STALE_DAYS:
        return STATE_STALE_THESIS

    return STATE_FRESH


def _cli():
    import json as _json
    p = argparse.ArgumentParser()
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Classify one ticker; prints state to stdout")
    group.add_argument(
        "--tickers",
        help="Comma-separated tickers; emits one {ticker: state} JSON line to stdout. "
             "Batch mode amortizes the Python interpreter startup across N tickers — "
             "a 20-ticker portfolio drops from ~20 subprocess forks (~4s) to one (~0.3s).",
    )
    p.add_argument("--reports-root", default=None)
    args = p.parse_args()
    root = Path(args.reports_root) if args.reports_root else None

    if args.ticker:
        print(classify(args.ticker, reports_root=root))
        return

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    result = {t: classify(t, reports_root=root) for t in tickers}
    print(_json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
