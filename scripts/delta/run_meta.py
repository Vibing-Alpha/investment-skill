"""run_meta.json read/write + dataclass model.

One file per date directory. At most one BQ section + one thesis
section. Sections update independently — a BQ re-run leaves the
thesis section alone.

Anti-hallucination exemption: this is internal audit state, not an
analysis artifact. Fields need no source tags.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from scripts.cli_utils import write_output

OUTPUT_VERSION = "1.0"  # run_meta schema version (distinct from artifact versions)
SYSTEM_VERSION = "8.0"  # must match the delta-era artifact output_version


@dataclass
class BQSection:
    run_id: str
    tier: str  # "full" | "partial" | "no_op"
    prior_source: Optional[str]
    probe: dict
    data_fetched: List[str]
    data_copied_from_prior: List[str]
    agents_run: List[str]
    completed_at: str
    completed: bool
    cost: dict


@dataclass
class ThesisSection:
    run_id: str
    events_reuse: dict  # {status, from_date, gates_passed, ...} when reused; {status: "fresh"} when not
    agents_run: List[str]
    completed_at: str
    completed: bool
    cost: dict


@dataclass
class IndustrySection:
    """research-industry skill section. Mirrors BQSection structure but
    swaps `probe` (BQ-specific) for `framing_refresh` (industry-specific
    audit slot recording what was refreshed in this run).
    """
    run_id: str
    tier: str  # "full" | "partial" | "no_op"
    prior_source: Optional[str]
    framing_refresh: dict  # {tam_refreshed: bool, players_refreshed: bool, etf_refreshed: bool}
    candidates_count: int
    agents_run: List[str]
    completed_at: str
    completed: bool
    cost: dict


@dataclass
class RunMeta:
    ticker: str
    et_trading_day: str
    output_version: str = SYSTEM_VERSION
    bq: Optional[BQSection] = None
    thesis: Optional[ThesisSection] = None
    industry: Optional[IndustrySection] = None
    warnings: List[str] = field(default_factory=list)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def save(self, path: Path) -> None:
        # Atomic write via temp+rename (project convention, matches every
        # other CLI-emitting script). Crash-safe: a partial run_meta.json
        # would collapse the resolver to "no prior"; atomic replace avoids
        # that class of torn-write.
        write_output(asdict(self), str(path))

    @classmethod
    def load(cls, path: Path) -> "RunMeta":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        bq = BQSection(**data["bq"]) if data.get("bq") else None
        thesis = ThesisSection(**data["thesis"]) if data.get("thesis") else None
        industry = IndustrySection(**data["industry"]) if data.get("industry") else None
        return cls(
            ticker=data["ticker"],
            et_trading_day=data["et_trading_day"],
            output_version=data.get("output_version", "unknown"),
            bq=bq,
            thesis=thesis,
            industry=industry,
            warnings=data.get("warnings", []),
        )

    @classmethod
    def load_or_none(cls, path: Path) -> Optional["RunMeta"]:
        """Resolver-safe load: returns None on any failure or version mismatch."""
        if not path.exists():
            return None
        try:
            rm = cls.load(path)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            return None
        if rm.output_version != SYSTEM_VERSION:
            return None  # schema mismatch → treat as pre-delta
        return rm


def _cli():
    """CLI for run_meta write subcommand.

    Usage:
      python3 -m scripts.delta.run_meta write --run-dir PATH --ticker T --skill score-business --tier TIER [--cost-json PATH]

    Reads the existing run_meta.json at {run-dir}/run_meta.json if present,
    updates only the relevant section (bq or thesis) with the new run data,
    and writes it back. Section-level updates preserve the other section.
    """
    import argparse, json, datetime, sys
    from pathlib import Path
    from scripts.delta.calendar import session_et

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write")
    w.add_argument("--run-dir", required=True, help="reports/{T}/{YYYYMMDD}/")
    w.add_argument("--ticker", required=True)
    w.add_argument("--skill", required=True, choices=["score-business", "investment-thesis", "research-industry"])
    w.add_argument(
        "--tier",
        default=None,
        choices=[None, "full", "partial", "no_op"],
        help=(
            "Tier (full|partial|no_op). Required when --skill in {score-business, "
            "research-industry}; ignored for investment-thesis (thesis status lives "
            "in events_reuse.status from --events-reuse-json)."
        ),
    )
    w.add_argument(
        "--framing-refresh-json",
        default=None,
        help="Path to {tam_refreshed, players_refreshed, etf_refreshed} dict (industry only)",
    )
    w.add_argument(
        "--candidates-count",
        type=int,
        default=0,
        help="Number of candidate_tickers in industry_analysis.json (industry only)",
    )
    w.add_argument("--run-id", default=None, help="Timestamp-based run id; defaults to now UTC")
    w.add_argument("--completed", action="store_true", default=True)
    w.add_argument("--cost-json", default=None, help="Path to {tokens, duration_s} dict")
    w.add_argument("--probe-json", default=None, help="Path to probe data dict (BQ only)")
    w.add_argument("--events-reuse-json", default=None, help="Events reuse decision (thesis only)")
    w.add_argument("--agents-run", default="", help="Comma-separated agent names")
    w.add_argument("--data-fetched", default="", help="Comma-separated category prefixes")
    w.add_argument("--data-copied-from-prior", default="", help="Comma-separated category prefixes copied")
    w.add_argument("--prior-source", default=None, help="Path to prior run dir (BQ only)")
    w.add_argument(
        "--warning",
        action="append",
        default=[],
        help="Append a warning string to run_meta.warnings. Repeatable.",
    )

    # Standalone warn subcommand — appends warnings WITHOUT touching
    # bq/thesis sections. Safer follow-up path for orchestrators that
    # want to record a post-write warning (e.g. summary.md word count
    # exceeded) without clobbering cost/agents_run with partial args.
    wn = sub.add_parser(
        "warn",
        help="Append warnings to run_meta.warnings (does not touch bq/thesis sections).",
    )
    wn.add_argument("--run-dir", required=True)
    wn.add_argument("--ticker", required=True, help="Used only if run_meta.json doesn't exist yet")
    wn.add_argument(
        "--warning",
        action="append",
        required=True,
        help="Warning message(s) to append. Repeatable.",
    )

    args = p.parse_args()

    # Enforce skill-specific --tier semantics:
    #   score-business → --tier required (writes bq.tier)
    #   research-industry → --tier required (writes industry.tier)
    #   investment-thesis → --tier forbidden (thesis "tier" lives in
    #     events_reuse.status from --events-reuse-json)
    if args.cmd == "write":
        if args.skill in ("score-business", "research-industry") and args.tier is None:
            print(
                f"run_meta write: --tier is required for --skill {args.skill} "
                "(full|partial|no_op)",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.skill == "investment-thesis" and args.tier is not None:
            print(
                "run_meta write: --tier is not used for --skill investment-thesis "
                "(thesis tier derives from events_reuse.status); ignoring.",
                file=sys.stderr,
            )

    if args.cmd == "warn":
        warn_dir = Path(args.run_dir)
        warn_path = warn_dir / "run_meta.json"
        existing = RunMeta.load_or_none(warn_path)
        if existing is None:
            existing = RunMeta(ticker=args.ticker, et_trading_day=session_et().isoformat())
        for w_msg in args.warning:
            existing.add_warning(w_msg)
        existing.save(warn_path)
        print(str(warn_path))
        return
    run_dir = Path(args.run_dir)
    meta_path = run_dir / "run_meta.json"

    # Load or create
    existing = RunMeta.load_or_none(meta_path)
    if existing is None:
        rm = RunMeta(ticker=args.ticker, et_trading_day=session_et().isoformat())
    else:
        rm = existing

    now = datetime.datetime.now(datetime.timezone.utc)
    run_id = args.run_id or now.strftime("%Y%m%dT%H%M%SZ")
    completed_at = now.isoformat().replace("+00:00", "Z")

    cost = {}
    if args.cost_json and Path(args.cost_json).exists():
        cost = json.loads(Path(args.cost_json).read_text(encoding="utf-8"))

    agents_run = [a for a in args.agents_run.split(",") if a]
    data_fetched = [c for c in args.data_fetched.split(",") if c]
    data_copied = [c for c in args.data_copied_from_prior.split(",") if c]

    if args.skill == "score-business":
        probe = {}
        if args.probe_json and Path(args.probe_json).exists():
            probe = json.loads(Path(args.probe_json).read_text(encoding="utf-8"))
        rm.bq = BQSection(
            run_id=run_id,
            tier=args.tier,
            prior_source=args.prior_source,
            probe=probe,
            data_fetched=data_fetched,
            data_copied_from_prior=data_copied,
            agents_run=agents_run,
            completed_at=completed_at,
            completed=args.completed,
            cost=cost,
        )
    elif args.skill == "research-industry":
        framing_refresh = {}
        if args.framing_refresh_json and Path(args.framing_refresh_json).exists():
            framing_refresh = json.loads(
                Path(args.framing_refresh_json).read_text(encoding="utf-8")
            )
        rm.industry = IndustrySection(
            run_id=run_id,
            tier=args.tier,
            prior_source=args.prior_source,
            framing_refresh=framing_refresh,
            candidates_count=args.candidates_count,
            agents_run=agents_run,
            completed_at=completed_at,
            completed=args.completed,
            cost=cost,
        )
    else:  # investment-thesis
        events_reuse = {}
        if args.events_reuse_json and Path(args.events_reuse_json).exists():
            events_reuse = json.loads(Path(args.events_reuse_json).read_text(encoding="utf-8"))
        rm.thesis = ThesisSection(
            run_id=run_id,
            events_reuse=events_reuse,
            agents_run=agents_run,
            completed_at=completed_at,
            completed=args.completed,
            cost=cost,
        )

    for w_msg in args.warning:
        rm.add_warning(w_msg)

    rm.save(meta_path)
    print(str(meta_path))


if __name__ == "__main__":
    _cli()
