"""Tier decision + events-reuse gate evaluation.

LLM-dependent steps (invoking the classifier) live in materiality.py;
this module takes classifier outputs as parameters so it stays pure
and unit-testable.

Tier precedence: full > partial > no_op (spec §6.1). The first
matching tier row wins.

Events reuse: all 5 gates must pass AND user_force_refresh must be
False. Any failure → rerun (fail-open posture).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Tuple


FULL_TIER_DAYS_CEILING = 90
PARTIAL_TIER_DAYS_SAFETY_VALVE = 14
THESIS_FRESHNESS_SLA_DAYS = 7


# ---- Estimates hash (Gate 2 + partial-tier trigger) ----

_ESTIMATES_HASHED_FIELDS = ("count", "period", "estimates", "yfinance_analyst")


def hash_estimates(estimates: dict) -> str:
    """SHA-256 of the stable subset of 06_analyst_estimates.

    Fields hashed: count, period, estimates[] (fiscal_period, period,
    revenue, earnings_per_share), yfinance_analyst (price_targets,
    recommendations). Absent fields hash as JSON null for stability.
    """
    subset = {}
    for f in _ESTIMATES_HASHED_FIELDS:
        subset[f] = estimates.get(f) if isinstance(estimates, dict) else None
        # Normalize None / missing into null for consistent hashing
        if subset[f] is None:
            subset[f] = None
    payload = json.dumps(subset, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---- Catalyst diff ----

def diff_catalysts_in_window(
    current: List[dict],
    prior: List[dict],
    today: datetime.date,
    window_days: int = 14,
) -> List[dict]:
    """Entries in `current` within [today, today+window_days] that are NOT
    present in `prior` by (event, normalized-date) equality.

    Dates in both lists are normalized to YYYY-MM-DD before comparison,
    so `2026-04-25T00:00:00Z` (prior) and `2026-04-25` (current) key
    identically. Without normalization a format mismatch would produce
    false 'new catalyst' detections and trip Gate 3 spuriously.
    """
    horizon = today + datetime.timedelta(days=window_days)

    def _norm_date(raw) -> str:
        """Return canonical YYYY-MM-DD or '' if unparseable."""
        if not isinstance(raw, str):
            return ""
        try:
            return datetime.date.fromisoformat(raw[:10]).isoformat()
        except ValueError:
            return ""

    def _key(e: dict) -> tuple:
        return (e.get("event", ""), _norm_date(e.get("date")))

    prior_keys = {_key(e) for e in (prior or []) if isinstance(e, dict)}
    new_entries = []
    for entry in (current or []):
        if not isinstance(entry, dict):
            continue
        raw = entry.get("date")
        norm = _norm_date(raw)
        if not norm:
            continue
        d = datetime.date.fromisoformat(norm)
        if not (today <= d <= horizon):
            continue
        if (entry.get("event", ""), norm) not in prior_keys:
            new_entries.append(entry)
    return new_entries


# ---- BQ tier decision ----

@dataclass
class BQTierInputs:
    new_financial_period: bool  # 02_financial_data income_statements report_period newer than prior (or current unreadable → fail-open full)
    new_earnings_release: bool  # 07_earnings earnings.report_period newer than prior
    days_since_last_full: int
    material_news_count: int
    estimates_hash_changed: bool


def decide_bq_tier(inputs: BQTierInputs) -> str:
    # full tier
    if inputs.new_financial_period:
        return "full"
    # INVARIANT (since the 779bd492 source-drift fix): as built by
    # build_bq_tier_inputs, `new_earnings_release` implies `new_financial_period`
    # — the source-drift guard suppresses it whenever the source-stable 02 income
    # quarter-end is present-and-unchanged, so it can only stay True when 02 also
    # advanced (and then the branch above already returned "full"). This branch is
    # therefore redundant-as-sole-trigger for built inputs; it is kept for the
    # typed-dataclass contract + direct unit tests. Do NOT "restore" 07 as an
    # independent new-quarter signal: 07_earnings.report_period is NOT
    # source-stable (FDS quarter-end vs FMP announcement date) and a cross-run
    # change can be pure representation drift. 02 is authoritative for quarter
    # identity. (Enforced by test_bq_tier_inputs_earnings_source_drift_suppressed.)
    if inputs.new_earnings_release:
        return "full"
    if inputs.days_since_last_full >= FULL_TIER_DAYS_CEILING:
        return "full"
    # partial tier
    if inputs.material_news_count > 0:
        return "partial"
    if inputs.estimates_hash_changed:
        return "partial"
    if inputs.days_since_last_full >= PARTIAL_TIER_DAYS_SAFETY_VALVE:
        return "partial"
    # no_op
    return "no_op"


# ---- Events reuse decision ----

@dataclass
class EventsReuseInputs:
    classifier_material_count: int
    classifier_input_healthy: bool
    estimates_hash_changed: bool
    new_catalysts_in_window: List[dict]
    events_schema_version: str
    days_since_last_events_run: int
    user_force_refresh: bool
    new_quarter_reported: bool  # a new fiscal quarter (newer 02 income-statement period OR 07 earnings report_period) was reported since the prior thesis run → reused (pre-print) events.json predates it → rerun. Mirrors decide_bq_tier's new_financial_period/new_earnings_release full triggers.


@dataclass
class EventsReuseDecision:
    decision: str  # "reuse" | "rerun"
    gates_passed: List[str]
    gates_failed: List[str]
    override_reason: str | None  # "user_force_refresh" if rerun was forced by user; else None


def decide_events_reuse(
    inputs: EventsReuseInputs, current_system_version: str = "8.0"
) -> EventsReuseDecision:
    """Evaluate the 5 gates + user override.

    Returns decision=reuse only if ALL 5 gates pass AND user did not
    force refresh. Gate names are kept pure (never includes
    "user_override") — override is a separate channel in the return.
    """
    gates_passed: List[str] = []
    gates_failed: List[str] = []

    # Gate 1: news classifier
    if inputs.classifier_input_healthy and inputs.classifier_material_count == 0:
        gates_passed.append("news")
    else:
        gates_failed.append("news")

    # Gate 2: estimates stable
    if not inputs.estimates_hash_changed:
        gates_passed.append("estimates")
    else:
        gates_failed.append("estimates")

    # Gate 3: no new catalysts in window
    if not inputs.new_catalysts_in_window:
        gates_passed.append("catalysts")
    else:
        gates_failed.append("catalysts")

    # Gate 4: schema version
    if inputs.events_schema_version == current_system_version:
        gates_passed.append("schema")
    else:
        gates_failed.append("schema")

    # Gate 5: 7-day hard ceiling
    if inputs.days_since_last_events_run <= THESIS_FRESHNESS_SLA_DAYS:
        gates_passed.append("ceiling_7d")
    else:
        gates_failed.append("ceiling_7d")

    # Gate 6: no new fiscal quarter reported since the prior thesis run.
    # A newer 10-Q period or earnings report after the events anchor means the
    # reused (pre-print) events.json predates it. decide_bq_tier already forces
    # the BQ `full` tier on new_financial_period / new_earnings_release; the
    # events-reuse path previously lacked the analogous gate, so a thesis run a
    # few days after an earnings print could reuse pre-print events whenever
    # forward consensus was stale (Gate 2) AND the earnings news was
    # non-whitelisted (Gate 1) — and the 7-day ceiling (Gate 5) gives no
    # protection in exactly that post-print window. Confirmed on MRVL 2026-05-29.
    if not inputs.new_quarter_reported:
        gates_passed.append("new_quarter")
    else:
        gates_failed.append("new_quarter")

    override_reason = "user_force_refresh" if inputs.user_force_refresh else None
    decision = "reuse" if (not gates_failed and not override_reason) else "rerun"
    return EventsReuseDecision(
        decision=decision,
        gates_passed=gates_passed,
        gates_failed=gates_failed,
        override_reason=override_reason,
    )


# ---- Events anchor extraction (Task 3.3) ----

from pathlib import Path as _Path
from typing import Optional
from scripts.delta.calendar import ET as _ET


def read_prior_events_run_date(prior_events_json: "_Path") -> Optional[str]:
    """Return canonical YYYY-MM-DD (ET) for the date events was last
    generated fresh. Preserves the anchor across chained reuses.

    Returns None on any failure (missing file, malformed JSON, no
    extractable date). Orchestrator interprets None as fail-open
    → events rerun, log warning to run_meta.warnings.

    Three cases (in order):
    1. prior has meta.reuse_meta.reused_from → already the fresh
       anchor from an earlier generation; preserve verbatim
       (after normalization).
    2. prior has meta.generated_at → normalize its UTC ISO to ET
       date (YYYY-MM-DD).
    3. neither present → None.
    """
    try:
        doc = json.loads(prior_events_json.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    meta = doc.get("meta", {})
    if not isinstance(meta, dict):
        return None
    reuse_meta = meta.get("reuse_meta") if isinstance(meta.get("reuse_meta"), dict) else {}

    anchor = reuse_meta.get("reused_from")
    if anchor:
        return _safe_normalize_to_et_date(anchor)

    generated_at = meta.get("generated_at")
    if generated_at:
        return _safe_normalize_to_et_date(generated_at)

    return None


def _safe_normalize_to_et_date(s) -> Optional[str]:
    """Accept YYYY-MM-DD, YYYYMMDD, or ISO-8601 datetime (with tz).
    Return YYYY-MM-DD in ET, or None for any other/malformed input.
    Never raises — callers use None to fail-open.
    """
    if not isinstance(s, str):
        return None
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            datetime.datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:  # fail-open-ok: bad date-format input → None per docstring contract
            return None
    if len(s) == 8 and s.isdigit():
        try:
            datetime.datetime.strptime(s, "%Y%m%d")
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        except ValueError:  # fail-open-ok: bad date-format input → None per docstring contract
            return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(_ET).date().isoformat()
    except ValueError:  # fail-open-ok: non-ISO-8601 string → None per docstring contract
        return None


def _cli():
    """CLI entrypoint for probe utilities.

    Subcommands:
      decide-bq-tier  Compute BQ tier from (report_dir, prior_dir, classifier_output).
                      Mirrors the inline `python3 -c` block previously in
                      score-business SKILL.md Step 2.

    Usage:
      python3 -m scripts.delta.probe decide-bq-tier --report-dir DIR [--prior-dir DIR] [--classifier-output PATH]
    """
    import argparse
    import sys as _sys
    from pathlib import Path
    from scripts.delta.probe_inputs import build_bq_tier_inputs

    p = argparse.ArgumentParser(description=_cli.__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    dbt = sub.add_parser(
        "decide-bq-tier",
        help="Compute BQ tier (full|partial|no_op) from report state + prior + classifier.",
    )
    dbt.add_argument("--report-dir", required=True,
                     help="Current run dir; reads 03_company_news, etc.")
    dbt.add_argument("--prior-dir", default="",
                     help="Prior run dir (or empty for first-time run)")
    dbt.add_argument("--classifier-output", default=None,
                     help="Path to classifier JSON output (optional)")

    # canonical-events-anchor — replaces inline 6-line python -c in
    # investment-thesis SKILL.md Step 2.
    cea = sub.add_parser(
        "canonical-events-anchor",
        help="Extract canonical events anchor from a prior events.json. "
             "Empty stdout if no prior or unreadable. Never raises.",
    )
    cea.add_argument("--prior-events", default="",
                     help="Path to prior events.json (empty for first run)")

    # decide-events-reuse — replaces inline 18-line python -c in
    # investment-thesis SKILL.md Step 3.
    der = sub.add_parser(
        "decide-events-reuse",
        help="Compute events reuse decision. Output: "
             "'decision|gates_passed|gates_failed|override_reason|anchor'",
    )
    der.add_argument("--report-dir", required=True)
    der.add_argument("--prior-thesis-dir", default="")
    der.add_argument("--classifier-output", default=None)

    args = p.parse_args()
    if args.cmd == "decide-bq-tier":
        report_dir = Path(args.report_dir)
        prior_dir = Path(args.prior_dir) if args.prior_dir else None
        cls_path = Path(args.classifier_output) if args.classifier_output else None
        if cls_path and not cls_path.exists():
            cls_path = None
        try:
            inputs = build_bq_tier_inputs(
                report_dir=report_dir,
                prior_dir=prior_dir,
                classifier_output_path=cls_path,
            )
            print(decide_bq_tier(inputs))
        except Exception as e:
            print(f"FATAL: decide_bq_tier failed: {e}", file=_sys.stderr)
            _sys.exit(1)
    elif args.cmd == "canonical-events-anchor":
        # Empty / nonexistent prior → empty output, never raise.
        # Mirrors the previous inline `python3 -c "... or ''"` contract.
        if not args.prior_events:
            print("")
        else:
            prior_events = Path(args.prior_events)
            if not prior_events.exists():
                print("")
            else:
                anchor = read_prior_events_run_date(prior_events) or ""
                print(anchor)
    elif args.cmd == "decide-events-reuse":
        from scripts.delta.probe_inputs import build_events_reuse_inputs
        report_dir = Path(args.report_dir)
        prior_thesis_dir = Path(args.prior_thesis_dir) if args.prior_thesis_dir else None
        cls_path = Path(args.classifier_output) if args.classifier_output else None
        if cls_path and not cls_path.exists():
            cls_path = None
        try:
            inputs, anchor = build_events_reuse_inputs(
                report_dir=report_dir,
                prior_thesis_dir=prior_thesis_dir,
                classifier_output_path=cls_path,
            )
            d = decide_events_reuse(inputs)
            anchor_str = anchor or ""
            print(f"{d.decision}|{','.join(d.gates_passed)}|"
                  f"{','.join(d.gates_failed)}|"
                  f"{d.override_reason or ''}|{anchor_str}")
        except Exception as e:
            print(f"FATAL: decide_events_reuse failed: {e}", file=_sys.stderr)
            _sys.exit(1)


if __name__ == "__main__":
    _cli()
