"""Classify a ticker into one of 5 staleness states (spec §8.1).

CLI: python3 -m scripts.delta.portfolio_classify --ticker TICKER [--reports-root PATH]

States (as consumed by the portfolio SKILL batch prompt):
- fresh        — last full-tier BQ <14 ET days AND a thesis within last 7 ET days
- stale_bq     — ≥14 ET days since last completed full-tier BQ run, OR no valid BQ
                 while thesis exists (orphan thesis; treat as "run BQ cascade"),
                 OR the BQ's own run lost important data (`bq_degradation_state`)
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
import json as _json_mod
from pathlib import Path
from typing import Optional

from scripts.delta.calendar import today_et
from scripts.delta.constants import (
    SKILL_BQ, SKILL_THESIS,
    SKILL_ETF,
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
        # The run must have a USABLE artifact, mirroring the resolver's rule
        # (thirtieth cold round — a producer-consumer mismatch): the resolver
        # SKIPS a completed run whose bq_analysis.json is missing or fails
        # JSON parse, so a later partial copies its scores from an OLDER
        # full — but this clock stopped at the unusable run's date and the
        # copied scores rode a reset 14/90-day clock back to fresh. A full
        # run the resolver cannot serve must not anchor the clock either.
        artifact = d / "bq_analysis.json"
        try:
            with open(artifact, "r", encoding="utf-8") as f:
                _json_mod.load(f)
        except (ValueError, OSError):
            continue
        try:
            full_date = datetime.date.fromisoformat(rm.et_trading_day)
        except (ValueError, TypeError):
            continue
        delta = (today - full_date).days
        if delta < 0:
            # Probe 4C: a FUTURE-dated full run (clock skew / old-date
            # rerun) must not anchor the freshness clock — a negative age
            # read as "fresh" silently suppressed the 14/90-day re-score
            # valves. The docstring always promised this filter; the code
            # never implemented it. Walk on to an older (sane) full.
            continue
        return delta
    return None


def bq_degradation_state(bq_dir, *, validation_may_be_stale=False):
    """Was the BQ in `bq_dir` built on a fetch that lost important data?

    Returns True / False, or None when there is no usable artifact there.
    The tri-state matters because "no answer" means different things per site.
    `classify()` tests `is not False`, so a present-but-unloadable artifact
    (None) is stale rather than clean. `build_bq_tier_inputs` ORs the two
    directories but reads them ASYMMETRICALLY: `prior_dir` also uses
    `is not False` — the resolver only returns a completed run, so an
    unloadable artifact there is corruption and a `no_op` must not copy its
    scores forward — while `report_dir` collapses via `bool()`, because
    today's directory legitimately holds no artifact on the first run of a day
    and forcing `full` on that would kill the delta layer.

    `validation_may_be_stale` says whether THIS process has already rewritten
    `bq_dir/data/00_validation.json` since the artifact was produced. The tier
    builder passes True (its probe fetch runs before the tier decision);
    `classify()` passes False — /monitor and /portfolio fetch nothing before
    calling it, so the stored validation is still the one that produced the
    artifact beside it. Applying the conservative branch there would flag a
    genuinely clean legacy run as stale_bq on its own session day.

    Never raises: this runs on every ticker in the monitor and portfolio
    orchestrations, and a corrupt file must not take them down.
    """
    import json as _json
    # Lazy imports (module convention — no back-edge: assemble imports
    # delta.calendar, never this module).
    from scripts.assemble import _summarize_degradation
    from scripts.delta.calendar import session_et
    from scripts.schemas.bq_analysis import load_bq_analysis

    if bq_dir is None:
        return None
    try:
        meta = load_bq_analysis(Path(bq_dir) / "bq_analysis.json").meta
    except (ValueError, OSError):
        return None
    # Order matters. A non-empty list is self-evidently a post-change artifact
    # reporting damage — decide on it before the generation test below, which
    # would otherwise send a genuinely degraded run down the legacy path.
    if meta.degraded_categories:
        return True
    if meta.validation_status is not None:
        return False        # post-change and explicitly clean — trust it

    # LEGACY artifact: neither key exists, so its absence says nothing. The
    # run's own `data/00_validation.json` is the only record of what it
    # fetched — but it is trustworthy ONLY if today's probe cannot have
    # overwritten it. `allocate-bq-run` reuses today's directory, and
    # `scripts/fetch.py` rewrites `00_validation.json` on EVERY fetch
    # including subsets (fetch.py:3302), during the probe step that runs
    # BEFORE the tier decision (score-business/SKILL.md:159 then :177). So in
    # today's directory that file describes this run's probe, not the artifact
    # sitting beside it — reading it would let a recovered probe declare a
    # gutted morning artifact clean.
    # Forty-sixth round widened this from the probe-aware caller to ALL
    # callers: a SECOND same-day run's probe can rewrite today's
    # 00_validation.json and then abort, leaving a clean probe record
    # beside an artifact it never produced — so for TODAY's directory the
    # file's ownership is unverifiable to ANY caller, not just the one
    # that fetched. Zero production cost: post-change artifacts carry the
    # meta pair and never reach this branch.
    if Path(bq_dir).name == session_et().strftime("%Y%m%d"):
        return True         # cannot verify → force one refresh
    try:
        raw = _json.loads(
            (Path(bq_dir) / "data" / "00_validation.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Conservative, not clean. This is the ONLY record of what that run
        # fetched; unable to read it we do not know — and "missing data is a
        # failure, not zero" (Global Constraints; producer-consumer.md #4).
        # Blast radius is nil today: all 42 stored legacy runs have a readable
        # validation file, so this fires only on a run whose data/ was pruned.
        return True
    # Parseable but contract-hollow is the same "no evidence" state as
    # unparseable (tenth cold round): fetch.py always writes a non-empty
    # categories dict, so a list / missing key / empty dict all mean the
    # record was truncated — and _summarize_degradation's empty list for
    # those shapes means "could not enumerate", not "nothing was lost".
    # All 42 stored legacy runs carry a readable non-empty dict, so this
    # fires only on genuine corruption.
    if not _stored_validation_shape_ok(raw):
        return True
    return bool(_summarize_degradation(raw)[1])


def _stored_validation_shape_ok(raw) -> bool:
    """True when a stored 00_validation.json has an evidence-bearing shape:
    a non-empty categories dict that carries every GATING category key.

    fetch.py writes EVERY category on every run — SKIPPED stubs included —
    so a map missing gating keys is truncation evidence, the same
    no-evidence state as an empty or non-dict map (nineteenth cold round:
    a `{"price": PASSED}`-only map was certified explicitly clean; the
    twenty-first widened the requirement from the critical four to the
    full gating set — a map that kept them but dropped `news` hid that
    loss just as silently). The set is DERIVED from CATEGORIES plus
    assemble's extra-gating map, so membership tracks the gate itself and
    a rename cannot blind this check; auxiliary keys (the version-growth
    axis, e.g. growth_stock_mode) are deliberately NOT required. Measured:
    all 43 stored runs carry all nine. Consumers: the legacy path of
    `bq_degradation_state` and `degradation_summary` — both answer
    conservative when this is False."""
    cats = raw.get("categories") if isinstance(raw, dict) else None
    if not isinstance(cats, dict) or not cats:
        return False
    from scripts.constants import CATEGORIES
    from scripts.assemble import (_CANONICAL_TOP_STATUSES,
                                  _GATING_EXTRA_CATEGORIES, _GATING_IMPORTANCE)
    # The top-level status must be canonical too (twenty-ninth round):
    # fetch only ever writes the four canonical values, so "CORRUPT" or a
    # missing status is the same corruption evidence as a truncated map —
    # and the round-24 precedent (a corrupt audit envelope must not restore
    # an exemption) says corruption evidence wins over an otherwise-healthy
    # map. All 43 stored runs carry a canonical status, so this moves
    # nothing.
    if raw.get("status") not in _CANONICAL_TOP_STATUSES:
        return False
    gating = {k for k, v in CATEGORIES.items()
              if isinstance(v, dict) and v.get("importance") in _GATING_IMPORTANCE}
    gating |= set(_GATING_EXTRA_CATEGORIES)
    return gating <= cats.keys()


def thesis_orphaned(run_dir) -> bool:
    """True when `run_dir`'s run_meta shows its thesis was COMPLETED BEFORE
    the current BQ artifact — i.e. a same-day full/partial re-score
    overwrote the BQ (and its 00_validation.json) the thesis was built
    from, so the thesis's source state is unrecoverable from disk.

    ONE implementation, two callers: classify()'s same-dir lineage branch,
    and the `--thesis-orphaned` CLI the /portfolio skip-stale path uses to
    decide whether a same-dir thesis needs a `thesis_source_degraded:
    unreadable` note (twenty-third round — the classifier detected the
    orphan and answered stale_thesis, but after `[s]` the skill delivered
    the clean overwritten BQ beside the contaminated thesis with no gate
    input, and the overwritten validation can no longer testify). A no_op
    re-run does not orphan (summary regeneration only); missing/legacy
    timestamps answer False — the age rules own those."""
    rm_pair = RunMeta.load_or_none(Path(run_dir) / "run_meta.json")
    return bool(
        rm_pair is not None and rm_pair.bq is not None
        and rm_pair.thesis is not None
        and rm_pair.bq.completed and rm_pair.thesis.completed
        and rm_pair.bq.tier in ("full", "partial")
        and rm_pair.bq.completed_at and rm_pair.thesis.completed_at
        and rm_pair.thesis.completed_at < rm_pair.bq.completed_at)


def degradation_summary(bq_dir) -> dict:
    """Return `{"validation_status": str|None, "degraded_categories": list|None}`
    for the run in `bq_dir`, computed from its STORED data/00_validation.json
    via assemble's canonical summarizer (one implementation).

    For the /portfolio skip-stale path (eighteenth cold round): a LEGACY
    artifact carries no meta degradation fields, so after `[s] skip stale`
    the decision layer read a degradation-derived stale_bq as a mere age
    warning — and the 2026-05-27 outage cohort are all legacy artifacts, so
    the named failure mode this feature was built on could walk through the
    skip door with no gate input (`stale_bq` conflates age with
    degradation). The skill substitutes this summary for the missing meta
    fields.

    `degraded_categories` is None — UNKNOWN, which the skill must treat as
    degraded — when the stored validation is missing, unparseable, or its
    categories section is not a non-empty dict (the same no-evidence shapes
    `bq_degradation_state`'s legacy path fails closed on). Never raises.
    """
    import json as _json
    from scripts.assemble import _summarize_degradation

    unknown = {"validation_status": None, "degraded_categories": None}
    if bq_dir is None:
        return unknown
    # TODAY's stored validation has unverifiable ownership (forty-sixth
    # round — the same-day probe-rewrite laundering): any same-day run's
    # probe may have replaced it, so it cannot testify for the artifact
    # beside it. Unknown, which the skill treats as degraded.
    from scripts.delta.calendar import session_et as _session_et
    try:
        if Path(bq_dir).name == _session_et().strftime("%Y%m%d"):
            return unknown
    except Exception:
        return unknown
    try:
        raw = _json.loads(
            (Path(bq_dir) / "data" / "00_validation.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return unknown
    if not _stored_validation_shape_ok(raw):
        return unknown
    vs, deg = _summarize_degradation(raw)
    return {"validation_status": vs, "degraded_categories": deg}


def _classify_etf(ticker: str, root: Path) -> Optional[str]:
    """`fresh` / `stale_thesis` for a fund, or None when this is not one.

    The ARTIFACT is the discriminator, not a separate identity lookup: a
    ticker with an `etf_thesis.json` is a fund whatever else sits beside it,
    and the ETF artifact takes precedence over coexisting stock artifacts.
    That precedence matters for a real case — a ticker scored as a stock
    BEFORE the forwarding detector existed still has a `bq_analysis.json`,
    and reading it would report a business-quality verdict for a fund.

    Only two states are reachable. There is no `stale_bq` for a fund (no BQ
    layer exists), no `bq_only`, and `none` is answered by the caller when
    there is no ETF artifact at all.

    Never raises: a mis-filed or corrupt ETF artifact must not take a whole
    portfolio batch down with it, so anything unreadable reads as
    `stale_thesis` — the state that asks for a refresh.
    """
    etf_dir = find_latest_prior(ticker, SKILL_ETF, reports_root=root,
                                include_today=True)
    if etf_dir is None:
        return None
    rm = RunMeta.load_or_none(Path(etf_dir) / "run_meta.json")
    if rm is None or rm.etf is None:
        return STATE_STALE_THESIS
    try:
        age = (today_et() - datetime.date.fromisoformat(rm.et_trading_day)).days
    except (ValueError, TypeError):
        return STATE_STALE_THESIS
    if age < 0 or age > THESIS_STALE_DAYS:
        return STATE_STALE_THESIS
    return STATE_FRESH


def classify(ticker: str, reports_root: Optional[Path] = None) -> str:
    """Return one of: fresh | stale_bq | stale_thesis | bq_only | none."""
    # Probe 3A: canonicalize once — the resolver normalizes its own lookups,
    # but the full-tier clock walk below keys the ticker dir directly.
    from scripts.cli_utils import normalize_ticker
    ticker = normalize_ticker(ticker)
    root = reports_root or DEFAULT_REPORTS_ROOT

    # Funds first. Without this an ETF with a complete, day-old thesis
    # classifies as `none` — indistinguishable from one that was never
    # analysed — and `/portfolio` excludes `none` from its refresh plan, so
    # the thesis could age indefinitely without ever being offered a refresh.
    etf_state = _classify_etf(ticker, root)
    if etf_state is not None:
        return etf_state

    bq_dir = find_latest_prior(ticker, SKILL_BQ, reports_root=root, include_today=True)
    thesis_dir = find_latest_prior(ticker, SKILL_THESIS, reports_root=root, include_today=True)

    if bq_dir is None and thesis_dir is None:
        return STATE_NONE
    # Checked BEFORE the bq_only return: a degraded BQ needs re-running whether
    # or not a thesis exists on top of it, and bq_only is excluded from the
    # portfolio refresh plan (SKILL.md:278) — so a degraded watchlist candidate
    # would be blocked from entry with no in-band way to clear the block.
    #
    # `is not False`, NOT truthiness: the helper's third state, None, means the
    # artifact is present but will not load. `find_latest_prior` only json-parses,
    # never typed-loads, so a schema-invalid bq_analysis.json reaches here — and
    # reading None as "not degraded" would classify a BQ we cannot even read as
    # `fresh`. That is the fail-open this module's docstring rules out ("no valid
    # BQ → stale_bq") and the Global Constraint forbids ("missing data is a
    # failure, not zero"). Only an explicit False — a loadable artifact that says
    # it is clean — may fall through.
    if bq_dir is not None and bq_degradation_state(bq_dir) is not False:
        return STATE_STALE_BQ
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

    # Thesis lineage, by ADJACENCY (fifteenth cold round). The thesis was
    # built from the bq_analysis.json sitting in its OWN date dir (every
    # stored thesis dir carries one), so when the selected thesis is not in
    # the selected BQ's dir — the day-after-recovery shape: yesterday's
    # degraded BQ grew a thesis, today's stale_bq routing refreshed the BQ
    # alone — check the thesis's source BQ, not just its age. A degraded or
    # corrupt source marks the thesis stale: its ER/CE/entry logic was
    # computed from the very inputs the gate exists to flag, and age checks
    # cannot see that. Same-dir pairs are excluded — the degradation branch
    # above already owns them. A MISSING adjacent artifact flags too
    # (nineteenth round): missing lineage evidence is UNKNOWN, and unknown
    # fails toward a refresh — production never produces the shape (40 of
    # 40 stored thesis dirs carry the artifact), so the conservative answer
    # costs nothing, while an earlier exists() fall-through let a pruned
    # day-1 thesis ride the age rules back to fresh.
    if (thesis_dir is not None and thesis_dir != bq_dir
            and bq_degradation_state(thesis_dir) is not False):
        return STATE_STALE_THESIS

    # Same-dir lineage (seventeenth round) — the adjacency check's blind
    # spot: a same-day RE-SCORE overwrites bq_analysis.json in place while
    # run_meta keeps the thesis section, so the artifact beside the thesis
    # is no longer the one it was built from (the recovery-day flow:
    # degraded morning BQ grows a thesis, the stale_bq routing re-scores
    # the BQ in today's dir). Ordering testifies instead: a thesis
    # COMPLETED BEFORE the current BQ artifact's completion was built from
    # a replaced BQ. Scoped to recomputing tiers — a no_op regenerates the
    # summary without touching the dims, so it does not orphan the thesis —
    # and to present timestamps; legacy/fixture run_metas with empty
    # completed_at fall through to the age rules. 0 of 42 stored run_metas
    # carry the inverted ordering.
    if thesis_dir is not None and bq_dir is not None and thesis_dir == bq_dir:
        if thesis_orphaned(bq_dir):
            return STATE_STALE_THESIS

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
    group.add_argument(
        "--thesis-orphaned", metavar="RUN_DIR",
        help="Print {orphaned: bool} — whether the run dir's thesis was "
             "completed BEFORE its current (re-scored) BQ artifact. The "
             "/portfolio skip-stale path uses this for same-dir pairs, where "
             "the overwritten validation can no longer testify.",
    )
    group.add_argument(
        "--degradation-summary", metavar="RUN_DIR",
        help="Print {validation_status, degraded_categories} JSON for the run "
             "dir's STORED validation — the /portfolio skip-stale path uses "
             "this to substitute a legacy artifact's missing meta fields. "
             "degraded_categories: null = unreadable record, treat as degraded.",
    )
    p.add_argument("--reports-root", default=None)
    args = p.parse_args()
    root = Path(args.reports_root) if args.reports_root else None

    if args.degradation_summary:
        print(_json.dumps(degradation_summary(Path(args.degradation_summary)),
                          ensure_ascii=False))
        return

    if args.thesis_orphaned:
        print(_json.dumps({"orphaned": thesis_orphaned(Path(args.thesis_orphaned))}))
        return

    if args.ticker:
        print(classify(args.ticker, reports_root=root))
        return

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    result = {t: classify(t, reports_root=root) for t in tickers}
    print(_json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
