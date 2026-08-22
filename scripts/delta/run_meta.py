"""run_meta.json read/write + dataclass model.

One file per date directory. At most one BQ section + one thesis
section. Sections update independently — a BQ re-run leaves the
thesis section alone.

Anti-hallucination exemption: this is internal audit state, not an
analysis artifact. Fields need no source tags.
"""

from __future__ import annotations

import json
import sys
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
class EtfSection:
    """etf-thesis skill section.

    `artifact_sha256` is over the promoted `etf_thesis.json` BYTES. The
    portfolio manifest compares it to the file it selected before loading the
    bundle, so a run_meta pointing at a thesis that was replaced afterwards is
    caught rather than trusted.

    No `tier` / `prior_source`: the ETF flow has no delta reuse — every run
    re-derives the profile and the snapshot, because the eligibility screens
    they feed are the entry gate and a reused screen is a screen nobody ran.
    """
    run_id: str
    artifact_sha256: str
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
    etf: Optional[EtfSection] = None
    warnings: List[str] = field(default_factory=list)
    # Closing round-3 F1: sections from an unloadable/version-mismatched
    # prior file are PARKED here verbatim (with their original
    # output_version) — preserved but never served. Grafting them into
    # the live slots previously laundered a shape-compatible old-version
    # thesis under the freshly-stamped current root version, and the
    # resolver then served it as current.
    preserved_legacy: dict = field(default_factory=dict)

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
        # A legacy run_meta written before the ETF layer has no `etf` key and
        # must still load — absence is the norm for every stock run.
        etf = EtfSection(**data["etf"]) if data.get("etf") else None
        return cls(
            ticker=data["ticker"],
            et_trading_day=data["et_trading_day"],
            output_version=data.get("output_version", "unknown"),
            bq=bq,
            thesis=thesis,
            industry=industry,
            etf=etf,
            warnings=data.get("warnings", []),
            preserved_legacy=(data.get("preserved_legacy")
                              if isinstance(data.get("preserved_legacy"), dict)
                              else {}),
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


def _load_preserving(meta_path: Path, ticker: str) -> RunMeta:
    """Writer-side load (rounds 18/26): NEVER hand back a blank record
    over unparseable prior state.

    load_or_none's fail-lenient None is correct for the read-only
    resolver; a WRITER that then saves would atomically erase every
    section the current schema can't instantiate. On load failure with a
    present file, the raw sections + warnings are grafted verbatim (plain
    dicts pass through asdict()) so the subsequent single-section update
    cannot destroy them. Used by BOTH the `write` and `warn` subcommands
    (one implementation — the warn path originally lacked it and erased
    completed sections while appending one warning).
    """
    from scripts.delta.calendar import session_et

    existing = RunMeta.load_or_none(meta_path)
    if existing is not None:
        return existing
    rm = RunMeta(ticker=ticker, et_trading_day=session_et().isoformat())
    if not meta_path.exists():
        return rm
    try:
        _raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _raw = None
    if isinstance(_raw, dict):
        print(
            f"[WARN] run_meta: existing {meta_path} did not load under "
            f"the current schema — preserving its sections verbatim "
            f"instead of overwriting (resolver treats them as no-prior).",
            file=sys.stderr,
        )
        parked = {
            k: _raw[k] for k in ("bq", "thesis", "industry")
            if isinstance(_raw.get(k), dict)
        }
        if parked:
            parked["output_version"] = _raw.get("output_version")
            rm.preserved_legacy = parked
        if isinstance(_raw.get("warnings"), list):
            rm.warnings = [w for w in _raw["warnings"] if isinstance(w, str)]
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
    w.add_argument("--skill", required=True,
                   choices=["score-business", "investment-thesis",
                            "research-industry", "etf-thesis"])
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
    # Round-20 F3: store_true with default=True made this flag inert —
    # completed could NEVER be written as False, so a failed same-day
    # rerun kept the morning run's completed=true beside its own invalid
    # artifact. BooleanOptionalAction adds --no-completed for the
    # orchestration failure paths.
    w.add_argument("--completed", action=argparse.BooleanOptionalAction,
                   default=True)
    w.add_argument("--cost-json", default=None, help="Path to {tokens, duration_s} dict")
    w.add_argument("--probe-json", default=None, help="Path to probe data dict (BQ only)")
    w.add_argument("--events-reuse-json", default=None, help="Events reuse decision (thesis only)")
    w.add_argument(
        "--artifact-sha256",
        default=None,
        help=("sha256 over the promoted etf_thesis.json bytes (etf-thesis "
              "only). The portfolio manifest compares it to the file it "
              "selected, so a run_meta pointing at a thesis replaced after "
              "the fact is caught rather than trusted."),
    )
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
        if args.skill == "etf-thesis":
            if args.tier is not None:
                print("run_meta write: --tier is not used for --skill "
                      "etf-thesis (the ETF flow has no delta reuse); ignoring.",
                      file=sys.stderr)
            if not args.artifact_sha256:
                print("run_meta write: --artifact-sha256 is required for "
                      "--skill etf-thesis — without it the manifest cannot "
                      "tell whether the thesis it selected is the one this "
                      "run wrote", file=sys.stderr)
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
        # Round-26: same writer-side preservation as `write` — this path
        # previously blank-created on any legacy-shape load failure and
        # atomically erased every completed section while appending one
        # warning (repro: soft-budget summary.md warn after a fresh BQ
        # write beside a legacy thesis section).
        existing = _load_preserving(warn_path, args.ticker)
        for w_msg in args.warning:
            existing.add_warning(w_msg)
        existing.save(warn_path)
        print(warn_path.as_posix())
        return
    run_dir = Path(args.run_dir)
    meta_path = run_dir / "run_meta.json"

    # Load or create. Round-18 F2: load_or_none's fail-lenient semantics
    # (None on any parse/shape/version mismatch) are correct for the
    # RESOLVER (read-only — treat as no-prior), but the WRITER must not
    # clobber what it could not parse: a legacy-shaped completed thesis
    # section beside a fresh BQ run was silently destroyed by the blank
    # RunMeta + single-section overwrite. Preserve the raw sections and
    # warnings; the resolver keeps treating them as no-prior (conservative)
    # until a matching-schema run rewrites them.
    rm = _load_preserving(meta_path, args.ticker)

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
    elif args.skill == "etf-thesis":
        rm.etf = EtfSection(
            run_id=run_id,
            artifact_sha256=args.artifact_sha256,
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
    print(meta_path.as_posix())


if __name__ == "__main__":
    _cli()
