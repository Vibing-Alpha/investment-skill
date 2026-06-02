"""Skill-aware resolver for the latest valid prior run directory.

Walks reports/{TICKER}/{YYYYMMDD}/ in reverse ET-day order and returns
the first dir whose run_meta.json is parseable, schema-matching, and
has the relevant skill section marked completed=True, AND whose
artifact file (bq_analysis.json or investment_thesis.json) exists.

Pre-delta dirs (no run_meta.json) are silently skipped → caller
treats as first-run for that skill.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from scripts.delta.constants import SKILL_BQ, SKILL_INDUSTRY, SKILL_THESIS
from scripts.delta.run_meta import RunMeta

DEFAULT_REPORTS_ROOT = Path("reports")


def find_latest_prior(
    ticker: str,
    skill: str,
    reports_root: Path | None = None,
    exclude_date: str | None = None,
    include_today: bool = False,
) -> Optional[Path]:
    """Return the most recent valid prior run dir for this (ticker, skill), or None.

    skill: "score-business" for BQ, "investment-thesis" for thesis.

    **Default behavior — safe by construction**: today's ET date dir is
    excluded. This prevents the same-day-rerun self-comparison bug where
    today's completed run is returned as its own "prior", collapsing tier
    probe comparisons (silent wrong tier).

    include_today=True: explicit opt-in to include today's dir. Used only
      by status checks that want to know "does today's run exist?" —
      NEVER by tier/reuse probe orchestration.
    exclude_date: explicit override for testing (pass a YYYYMMDD string
      to skip a specific dir). Takes precedence over include_today when set.
    """
    from scripts.delta.calendar import session_et as _session_et
    root = reports_root or DEFAULT_REPORTS_ROOT
    ticker_dir = root / ticker
    if not ticker_dir.exists():
        return None

    if skill == SKILL_BQ:
        section_attr = "bq"
        artifact = "bq_analysis.json"
    elif skill == SKILL_THESIS:
        section_attr = "thesis"
        artifact = "investment_thesis.json"
    elif skill == SKILL_INDUSTRY:
        section_attr = "industry"
        artifact = "industry_analysis.json"
    else:
        raise ValueError(f"unknown skill: {skill!r}")

    # Compute exclusion set: explicit exclude_date overrides; else
    # exclude today unless include_today=True.
    if exclude_date is not None:
        excluded = {exclude_date}
    elif not include_today:
        excluded = {_session_et().strftime("%Y%m%d")}
    else:
        excluded = set()

    date_dirs = sorted(
        (p for p in ticker_dir.iterdir()
         if p.is_dir() and _is_date_dirname(p.name) and p.name not in excluded),
        key=lambda p: p.name,
        reverse=True,
    )
    for d in date_dirs:
        rm = RunMeta.load_or_none(d / "run_meta.json")
        if rm is None:
            continue
        section = getattr(rm, section_attr)
        if section is None or not section.completed:
            continue
        artifact_path = d / artifact
        if not artifact_path.exists():
            continue
        # Artifact must be parseable JSON — a corrupted artifact that
        # passed the run_meta.completed=true bar (e.g. user manually
        # edited it and broke JSON) must not be returned as valid prior.
        # Spec §12: "if the file fails JSON parse, resolver skips".
        try:
            with open(artifact_path, "r", encoding="utf-8") as f:
                json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        return d

    return None


def _is_date_dirname(name: str) -> bool:
    return len(name) == 8 and name.isdigit()


def _cli():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    fp = sub.add_parser("find-latest-prior")
    fp.add_argument("--ticker", required=True)
    fp.add_argument("--skill", required=True)
    fp.add_argument(
        "--include-today",
        action="store_true",
        help="Include today's ET date dir (default: excluded to prevent self-comparison)",
    )
    fp.add_argument(
        "--exclude-date",
        default=None,
        help="Explicit YYYYMMDD to skip (overrides --include-today). Testing/advanced.",
    )
    fp.add_argument(
        "--reports-root",
        default=None,
        help="Override reports/ root (default: DEFAULT_REPORTS_ROOT). For testing / alt roots.",
    )

    ap = sub.add_parser("allocate-bq-run")
    ap.add_argument("--ticker", required=True)
    ap.add_argument(
        "--reports-root",
        default=None,
        help="Override reports/ root (default: 'reports'). For testing / alt roots.",
    )

    ai = sub.add_parser(
        "allocate-industry-run",
        help="Allocate reports/industry/<slug>/<YYYYMMDD>/ for research-industry skill.",
    )
    ai.add_argument("--slug", required=True, help="Industry slug, e.g. 'ai-chips'")
    ai.add_argument(
        "--reports-root",
        default=None,
        help="Override reports/ root (default: 'reports').",
    )

    args = p.parse_args()
    if args.cmd == "find-latest-prior":
        root = Path(args.reports_root) if args.reports_root else None
        d = find_latest_prior(
            args.ticker, args.skill,
            reports_root=root,
            exclude_date=args.exclude_date,
            include_today=args.include_today,
        )
        print(str(d) if d else "")
    elif args.cmd == "allocate-bq-run":
        from scripts.delta.calendar import session_et
        root = Path(args.reports_root) if args.reports_root else Path("reports")
        path = root / args.ticker / session_et().strftime("%Y%m%d")
        path.mkdir(parents=True, exist_ok=True)
        (path / "data").mkdir(exist_ok=True)
        (path / "scores").mkdir(exist_ok=True)
        print(str(path))
    elif args.cmd == "allocate-industry-run":
        from scripts.delta.calendar import session_et
        import re as _re
        # Validate slug format here so the orchestrator can't pass a junk value.
        if not _re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", args.slug):
            import sys as _sys
            print(
                f"FATAL: invalid slug {args.slug!r} — must match ^[a-z0-9]+(-[a-z0-9]+)*$",
                file=_sys.stderr,
            )
            _sys.exit(2)
        root = Path(args.reports_root) if args.reports_root else Path("reports")
        path = root / "industry" / args.slug / session_et().strftime("%Y%m%d")
        path.mkdir(parents=True, exist_ok=True)
        (path / "data").mkdir(exist_ok=True)
        print(str(path))


if __name__ == "__main__":
    _cli()
