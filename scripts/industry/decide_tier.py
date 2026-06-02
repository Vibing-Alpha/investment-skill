"""Decide the research-industry tier (full / partial / no_op).

Industry research has no probe-fetch step (unlike /score-business), so
tier decision is purely a function of (a) prior run existence and (b)
days-since-prior. User-supplied --force-refresh escalates to full.

Thresholds live in `scripts.delta.constants`:
- INDUSTRY_FULL_TIER_DAYS_CEILING (90d)
- INDUSTRY_PARTIAL_TIER_DAYS_SAFETY_VALVE (21d)

Usage:
    # No prior → full
    python3 -m scripts.industry.decide_tier --prior-dir ""

    # 30d-old prior → partial
    python3 -m scripts.industry.decide_tier --prior-dir reports/industry/ai-chips/20260420

    # Force refresh overrides days-since
    python3 -m scripts.industry.decide_tier --prior-dir <dir> --force-refresh
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from scripts.delta.calendar import today_et
from scripts.delta.constants import (
    INDUSTRY_FULL_TIER_DAYS_CEILING,
    INDUSTRY_PARTIAL_TIER_DAYS_SAFETY_VALVE,
    TIER_FULL,
    TIER_NO_OP,
    TIER_PARTIAL,
)


def _parse_date_from_dirname(dirname: str) -> date:
    """Parse YYYYMMDD into a date. Raises ValueError on malformed input.

    The prior-dir basename is the ET trading day of the prior run.
    """
    if len(dirname) != 8 or not dirname.isdigit():
        raise ValueError(
            f"prior-dir basename {dirname!r} is not a YYYYMMDD date string"
        )
    return date(int(dirname[:4]), int(dirname[4:6]), int(dirname[6:8]))


def decide_tier(prior_dir: str | Path | None, *, force_refresh: bool = False,
                today: date | None = None) -> tuple[str, int]:
    """Return (tier, days_since_prior).

    days_since_prior = -1 means no prior existed (first run).
    """
    if today is None:
        today = today_et()

    if force_refresh:
        # User explicitly asked for a fresh full run; days_since is
        # informational but does not gate the decision.
        if not prior_dir:
            return (TIER_FULL, -1)
        try:
            prior_date = _parse_date_from_dirname(Path(prior_dir).name)
            return (TIER_FULL, (today - prior_date).days)
        except ValueError:
            # Force-refresh trumps malformed prior; still go full.
            return (TIER_FULL, -1)

    if not prior_dir:
        return (TIER_FULL, -1)

    prior_path = Path(prior_dir)
    try:
        prior_date = _parse_date_from_dirname(prior_path.name)
    except ValueError as e:
        # Malformed prior dir → fail-close (treat as no prior, force full)
        # so we don't make a tier decision on garbage input.
        print(f"WARN: malformed prior-dir {prior_path.name!r}, treating as no prior: {e}",
              file=sys.stderr)
        return (TIER_FULL, -1)

    days_since = (today - prior_date).days
    if days_since < 0:
        # Prior dir is in the future — clock skew or test fixture. Treat as no prior.
        print(f"WARN: prior-dir {prior_path.name!r} is in the future "
              f"({days_since}d), treating as no prior",
              file=sys.stderr)
        return (TIER_FULL, -1)

    if days_since >= INDUSTRY_FULL_TIER_DAYS_CEILING:
        return (TIER_FULL, days_since)
    if days_since >= INDUSTRY_PARTIAL_TIER_DAYS_SAFETY_VALVE:
        return (TIER_PARTIAL, days_since)
    return (TIER_NO_OP, days_since)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--prior-dir", default="",
        help="Path to prior run directory (e.g. reports/industry/ai-chips/20260420). "
             "Empty string means no prior exists (first run).",
    )
    p.add_argument(
        "--force-refresh", action="store_true",
        help="Escalate to full tier regardless of days-since. "
             "Used when the user explicitly asks for fresh research.",
    )
    p.add_argument(
        "--format", choices=("tier", "json"), default="tier",
        help="'tier' (default) prints just the tier; 'json' prints "
             "{tier, days_since_prior}",
    )
    args = p.parse_args(argv)

    tier, days_since = decide_tier(
        args.prior_dir or None, force_refresh=args.force_refresh,
    )
    if args.format == "json":
        import json as _json
        print(_json.dumps({"tier": tier, "days_since_prior": days_since}))
    else:
        print(tier)
    return 0


if __name__ == "__main__":
    sys.exit(main())
