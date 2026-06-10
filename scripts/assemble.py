"""Assemble bq_analysis.json from score files + synthesis.json + validation data.

Handles the mechanical merging that was previously done by the synthesis agent,
reducing agent output from ~50KB to ~10KB and cutting synthesis time by ~70%.

Usage:
    python3 -m scripts.assemble --report-dir reports/AAPL/20260406
"""

import argparse
import datetime
import json
import math
import sys
from datetime import timezone
from pathlib import Path

from scripts.cli_utils import emit_dl3c_root_marker, read_json, write_output
from scripts.constants import Status
from scripts.delta.calendar import today_et, last_closed_trading_day

PREFIX = "assemble"
OUTPUT_VERSION = "8.0"

# Fields in score files that are redundant in the dimensions section
STRIP_KEYS = {"dimension", "ticker", "data_freshness", "scoring_calculation"}

DEFAULT_WEIGHTS = {"fundamental": 0.35, "forward": 0.35, "industry": 0.30}
DIMENSIONS = list(DEFAULT_WEIGHTS.keys())

# Which dimension score files are FRESH (agent-rerun) per delta tier —
# mirrors the orchestrator's AGENTS_RUN map in
# .claude/skills/score-business/SKILL.md (full: fundamental,forward,
# industry; partial: forward,industry; no_op: none). Fresh dims are
# strict-gated for WebSearch source binding; reused dims stay lenient.
WEBSEARCH_FRESH_DIMS_BY_TIER = {
    "full": ("fundamental", "forward", "industry"),
    "partial": ("forward", "industry"),
    "no_op": (),
}

# DL3c §3.7.4: scoped set of DL3c-gated artifacts whose `dl3c_mode` must be
# consistent across an assemble run. `peer_multiples.json` is NOT in this
# scope — it's always USD (yfinance, USD-normalized; not a cert consumer
# per §3.7.2) and including it would block every converted-ticker run.
DL3C_GATED_ARTIFACTS = ("fcf_inputs", "historical_multiples", "adr_correction")


def build_meta(
    ticker,
    validation,
    freshness_interpretation,
    analysis_date,
    tier_context,
):
    """Build the meta section.

    tier_context is a dict loaded from --tier-context-json:
    {
      "tier_this_run": "full" | "partial" | "no_op",
      "component_provenance": {
        "dimensions.fundamental": {"source_date": "...", "reason": "..."},
        ...
      }
    }
    """
    if analysis_date is None:
        # ET trading day, NOT UTC date (spec §11). Matters at UTC-midnight
        # boundaries where UTC and ET days differ.
        analysis_date = today_et().isoformat()

    financials = validation.get("categories", {}).get("financials", {})
    latest_period = financials.get("latest_period")

    freshness_note = compute_freshness_note(validation)
    if freshness_interpretation:
        freshness_note = (
            f"{freshness_note}. {freshness_interpretation}"
            if freshness_note else freshness_interpretation
        )

    return {
        "ticker": ticker,
        "analysis_date": analysis_date,
        "generated_at": datetime.datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "market_asof_date": last_closed_trading_day().isoformat(),
        "data_freshness": latest_period,
        "freshness_note": freshness_note,
        "output_version": OUTPUT_VERSION,
        "tier_this_run": tier_context["tier_this_run"],
        "component_provenance": tier_context["component_provenance"],
    }


def compute_freshness_note(validation):
    """Build a freshness note from validation data, or return None if fresh.

    Defensive against upstream None/non-dict values: `validation.get(x, {})`
    returns the literal None if x IS present and explicitly None, which
    breaks subsequent .get() chains with AttributeError. Use `or {}` to
    coerce None to empty dict at each level.
    """
    if not isinstance(validation, dict):
        return None
    parts = []

    # Check financial data age — chain defensively through possible None/wrong-type
    categories = validation.get("categories") or {}
    if not isinstance(categories, dict):
        return None
    eps_val = categories.get("eps_validation") or {}
    if not isinstance(eps_val, dict):
        return None
    fin_freshness = eps_val.get("financial_freshness") or {}
    if not isinstance(fin_freshness, dict):
        fin_freshness = {}
    days_old = fin_freshness.get("days_old")
    latest_period = fin_freshness.get("latest_report_period")

    if isinstance(days_old, (int, float)) and days_old > 30:
        parts.append(
            f"Financial data is {days_old} days old "
            f"(latest period: {latest_period})"
        )

    # Check for circuit breakers — reuse the already-sanitized `categories`
    circuit_breakers = []
    for cat_name, cat_data in categories.items():
        if isinstance(cat_data, dict) and cat_data.get("status") == Status.CIRCUIT_BREAKER:
            circuit_breakers.append(cat_name)
    if circuit_breakers:
        parts.append(f"CIRCUIT BREAKER in: {', '.join(circuit_breakers)}")

    # Check EPS warnings
    eps_consistency = eps_val.get("eps_consistency", {})
    warnings = eps_consistency.get("warnings", [])
    if warnings:
        parts.append(
            "EPS validation WARNING: " + "; ".join(warnings)
        )

    return ". ".join(parts) if parts else None


def build_scores(score_files, weights):
    """Compute weighted BQ score from dimension scores.

    Fail-closed on empty input or fully-mismatched dimension names — main()
    guards with a ≥2 dimensions gate but defensive callers may pass junk.
    Returning ZeroDivisionError here would crash the whole assemble pipeline.
    """
    scores = {}
    for dim_name, weight in weights.items():
        if dim_name in score_files:
            scores[dim_name] = score_files[dim_name]["overall"]
        else:
            print(
                f"{PREFIX}: WARNING — missing dimension '{dim_name}', "
                f"excluded from weighted average",
                file=sys.stderr,
            )

    total_weight = sum(weights[d] for d in scores)
    if total_weight == 0:
        # No matching dimensions between score_files and weights — fail-closed.
        # Caller should have caught this via the main() ≥2 dim gate but this
        # is the defensive second line.
        return {"overall": None, "weights": {}}

    overall = sum(scores[d] * weights[d] for d in scores) / total_weight

    # Post-condition: a finite weighted average. NaN/Inf can sneak in via a
    # score_file with `overall: NaN` (e.g. an upstream agent failure that
    # serialized a poisoned float). Fail-closed mirrors the empty-input
    # branch above — do NOT raise (this helper runs inside the assemble
    # pipeline and a raise would crash the whole run; the caller already
    # has stricter file-level validation downstream via load_bq_analysis).
    # codex review 2026-05-22: DL3c emit_with_numeric_coerce protects the
    # FD adapter boundary but not the downstream weighted-avg sink here.
    if not math.isfinite(overall):
        print(
            f"{PREFIX}: WARNING — weighted overall is non-finite "
            f"({overall!r}); a score_file likely carries NaN/Inf. "
            f"Failing closed with overall=None.",
            file=sys.stderr,
        )
        return {"overall": None, "weights": {}}

    adjusted_weights = (
        {d: round(weights[d] / total_weight, 4) for d in scores}
        if len(scores) < len(weights)
        else dict(weights)
    )

    return {
        "overall": round(overall, 1),
        **scores,
        "weights": adjusted_weights,
    }


def build_dimensions(score_files):
    """Copy score files into dimensions, stripping redundant keys."""
    dimensions = {}
    for dim_name, data in score_files.items():
        dimensions[dim_name] = {
            k: v for k, v in data.items() if k not in STRIP_KEYS
        }
    return dimensions


def parse_weights(weights_str):
    """Parse weights from comma-separated string like '0.35,0.35,0.30'.

    Each weight must be finite (no NaN/Inf) and non-negative — anything
    else poisons the weighted average in build_scores. codex review
    2026-05-22 catch: DL3c emit_with_numeric_coerce protects the FD
    adapter boundary but not this CLI-arg entry point.
    """
    raw_parts = [x.strip() for x in weights_str.split(",")]
    parts = []
    for raw in raw_parts:
        try:
            val = float(raw)
        except ValueError:
            print(
                f"{PREFIX}: --weights value {raw!r} is not a number",
                file=sys.stderr,
            )
            sys.exit(1)
        if not math.isfinite(val):
            print(
                f"{PREFIX}: --weights value {raw!r} is not finite "
                f"(parsed as {val!r}); NaN/Inf weights poison the "
                f"weighted average",
                file=sys.stderr,
            )
            sys.exit(1)
        if val < 0:
            print(
                f"{PREFIX}: --weights value {raw!r} is negative "
                f"({val!r}); weights must be non-negative",
                file=sys.stderr,
            )
            sys.exit(1)
        parts.append(val)
    if len(parts) != 3:
        print(
            f"{PREFIX}: --weights requires 3 comma-separated values, got {len(parts)}",
            file=sys.stderr,
        )
        sys.exit(1)
    return dict(zip(DIMENSIONS, parts))


def _load_strategy_weights(strategy_path=None):
    """Resolve dimension_weights from strategy.yaml.scoring.dimension_weights.

    Precedence (resolved by caller):
        --weights CLI > strategy.yaml > DEFAULT_WEIGHTS

    This helper returns the strategy.yaml value if:
      - file exists and parses as a mapping
      - scoring.dimension_weights is a dict keyed EXACTLY by the three
        dimensions (no missing / extra keys)
      - values are numeric, non-negative, and sum to ~1.0

    Otherwise returns a copy of DEFAULT_WEIGHTS. Any I/O or parse error
    falls back to defaults (fail-open on this optional override —
    corrupt user config should not block the BQ pipeline).
    """
    try:
        import yaml
    except ImportError:
        return dict(DEFAULT_WEIGHTS)
    # Try cwd first (tests inject strategy.yaml via tmp_path + cwd override);
    # fall back to __file__-anchored. Pure cwd broke when running from
    # /tmp; pure __file__ broke test-injection. Both matter.  # noqa: audit-fail-open
    if strategy_path:
        p = Path(strategy_path)
    else:
        cwd_candidate = Path("strategy.yaml")  # fail-open-ok: cwd-first for test-injection + CLI ergonomics
        root_candidate = Path(__file__).resolve().parent.parent / "strategy.yaml"
        p = cwd_candidate if cwd_candidate.exists() else root_candidate
    if not p.exists():
        # No user intent to override — silent default.
        return dict(DEFAULT_WEIGHTS)
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(
            f"{PREFIX}: strategy.yaml could not be parsed ({type(e).__name__}: {e}); "
            f"falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    scoring = data.get("scoring")
    if scoring is None:
        # No scoring section — no override intent, silent default.
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(scoring, dict):
        print(
            f"{PREFIX}: strategy.yaml.scoring is not a mapping "
            f"(got {type(scoring).__name__}); falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    if "dimension_weights" not in scoring:
        # scoring section exists but no dimension_weights key — questionable
        # but could just mean user set other scoring knobs. Log to be audible.
        print(
            f"{PREFIX}: strategy.yaml.scoring exists but lacks 'dimension_weights'; "
            f"falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    w = scoring.get("dimension_weights")
    if not isinstance(w, dict):
        print(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights is not a mapping "
            f"(got {type(w).__name__}); falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    if set(w.keys()) != set(DEFAULT_WEIGHTS):
        print(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights invalid "
            f"(keys={sorted(w.keys())} must match {sorted(DEFAULT_WEIGHTS)}); "
            f"falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in w.values()):
        print(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights invalid "
            f"(values={dict(w)} must be numeric in [0,1]); "
            f"falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    if abs(sum(w.values()) - 1.0) >= 0.01:
        print(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights invalid "
            f"(sum={sum(w.values()):.4f} must be ~1.0); "
            f"falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    return {k: float(w[k]) for k in DEFAULT_WEIGHTS}


def _cert_to_dict(cert):
    """Serialize a CurrencyConversion dataclass back to the §3.1.2 JSON shape.

    Mirrors the producer-side cert builder so the propagated block written
    into bq_analysis.json round-trips through load_currency_conversion.
    """
    window_rows = []
    if cert.window is not None:
        for row in cert.window.rows:
            row_out = {
                "currency": row.currency,
                "date": row.date,
                "fx_rate_usd_per_local": row.fx_rate_usd_per_local,
                "source": row.source,
            }
            if row.bar_date is not None:
                row_out["bar_date"] = row.bar_date
            if row.lag_days is not None:
                row_out["lag_days"] = row.lag_days
            window_rows.append(row_out)
    return {
        "basis": cert.basis,
        "source_currency": cert.source_currency,
        "fx_source": cert.fx_source,
        "window": window_rows,
    }


def _load_dl3c_gated_artifacts(report_dir):
    """DL3c §3.7.4: load gated artifacts via typed loaders.

    Returns (loaded_modes, propagated_cert, converted_cert_dicts):
      - loaded_modes: dict[str, str] mapping artifact name (matching
        DL3C_GATED_ARTIFACTS) → dl3c_mode literal. Only artifacts that
        loaded successfully are present; missing files and SchemaError
        loads are excluded (a warning is emitted on stderr for the latter).
      - propagated_cert: dict serialized cert from the FIRST artifact whose
        mode is "post_dl3c_usd_converted", or None if no converted artifact
        was loaded. The mixed-mode raise + cert-divergence raise downstream
        ensure that, when multiple converted artifacts exist, they all
        emit the same cert (so the "first one wins" semantic is safe).
      - converted_cert_dicts: dict[name → serialized cert] for every
        converted gated artifact. Used by _check_mixed_dl3c_modes (post-
        impl loop-1 H4) to detect cert divergence across artifacts.
    """
    # Inline imports keep the typed-loader dependency local to the DL3c
    # propagation step — mirrors the inline `from scripts.schemas.bq_analysis
    # import load_bq_analysis` convention at the bottom of main().
    from scripts.schemas.adr_correction import load_adr_correction
    from scripts.schemas.errors import SchemaError
    from scripts.schemas.fcf_inputs import load_fcf_inputs
    from scripts.schemas.historical_multiples import load_historical_multiples

    artifact_paths = {
        "fcf_inputs": report_dir / "data" / "fcf_inputs.json",
        "historical_multiples": report_dir / "data" / "historical_multiples.json",
        "adr_correction": report_dir / "data" / "adr_correction.json",
    }
    loaders = {
        "fcf_inputs": load_fcf_inputs,
        "historical_multiples": load_historical_multiples,
        "adr_correction": load_adr_correction,
    }

    loaded_modes = {}
    # post-impl loop-1 H4 fix: collect ALL converted certs (not just the
    # first). _check_mixed_dl3c_modes now also verifies that every
    # converted artifact agrees on (source_currency, fx_source, window).
    # Pre-fix `propagated_cert = first_converted_doc_cert` silently dropped
    # later artifacts' certs even when they disagreed — a partial-FX
    # state with diverging FX windows would produce a bq_analysis.json
    # carrying one artifact's view as if it were canonical.
    converted_cert_dicts: dict[str, dict] = {}
    for name in DL3C_GATED_ARTIFACTS:
        path = artifact_paths[name]
        if not path.exists():
            # Missing-file tolerance preserved — some tickers legitimately
            # skip a gated artifact (USD-only tiers don't produce
            # adr_correction.json, smoke tiers may skip historical_multiples).
            continue
        try:
            doc = loaders[name](path)
        except (SchemaError, ValueError) as exc:
            # post-impl loop-2/3 ISS-023 fix: an EXISTING gated artifact
            # that fails typed-load is a partial-write or schema-drift
            # signal — fail-close. Pre-fix this was `log + continue`
            # with the rationale "upstream producer should have
            # fail-closed already". But the FX-failure-path producer
            # envelope emits a valid-shape artifact with no cert + an
            # explicit fx_failure_reason (loop-2 ISS-021 added
            # post_dl3c_failed_fx mode for that case). If a loader
            # raises here, it means we hit a state OUTSIDE that
            # well-formed-failure envelope — a malformed cert, a
            # bad _dl3c_version, a basis=usd_native with cert
            # present, etc. Those are partial-migration signals that
            # MUST surface to the operator rather than getting
            # silently dropped from mixed-mode/cert-divergence checks.
            # Re-flagged by codex fresh-session in 3 consecutive loops
            # (loop-1 cycle-1, loop-2 cycle-1, loop-3 final challenge).
            print(
                f"{PREFIX}: FATAL — failed to load {name}.json for DL3c "
                f"dispatch: {exc}; the artifact exists on disk but its "
                f"DL3c-relevant subset is malformed. Investigate the "
                f"producer pipeline (likely a partial write, schema "
                f"drift, or hand-edit). To bypass for a one-off run, "
                f"delete the artifact (file-missing is the tolerated "
                f"state, not file-malformed).",
                file=sys.stderr,
            )
            sys.exit(1)
        # Frozen-anchor exclusion: fetch.py:write_adr_anchor writes a
        # classification anchor (no cert, not a DL3c artifact) to the
        # adr_correction.json path. It is NOT part of the FX-conversion mode
        # set — counting it as `legacy_pre_dl3c` would make it a phantom
        # non-converted artifact that spuriously trips _check_mixed_dl3c_modes
        # when fcf_inputs/historical_multiples ARE converted (non-USD ADR
        # re-assembled after a thesis run). Treat it as an absent gated
        # artifact (same as the missing-file path above).
        if getattr(doc, "is_frozen_anchor", False):
            continue
        loaded_modes[name] = doc.dl3c_mode
        if doc.dl3c_mode == "post_dl3c_usd_converted":
            converted_cert_dicts[name] = _cert_to_dict(doc.currency_conversion)
    propagated_cert = (
        next(iter(converted_cert_dicts.values()))
        if converted_cert_dicts else None
    )
    return loaded_modes, propagated_cert, converted_cert_dicts


def _check_mixed_dl3c_modes(loaded_modes, converted_cert_dicts=None):
    """DL3c §3.7.4 / cycle-15 F-15-7: raise if gated artifacts disagree.

    Two independent fail-close cases:

    1. **Cross-mode contamination** — at least one converted artifact
       coexists with another artifact in a NON-converted mode
       (post_dl3c_usd_native OR legacy_pre_dl3c). Indicates partial FX
       success or stale legacy artifact mixed with a new converted artifact;
       operator must investigate.

       post-impl loop-1 H3 fix: pre-fix the check only compared
       `post_dl3c_usd_converted` vs `post_dl3c_usd_native`, missing the
       legacy_pre_dl3c case where a converted artifact silently coexists
       with an artifact predating DL3c. Now any non-converted mode
       (native OR legacy) triggers the fail-close when ANY converted
       artifact exists.

    2. **Diverging converted certs** — multiple converted artifacts present
       but their certificates differ on (basis, source_currency, fx_source,
       window). Indicates two FX windows fetched at different times or
       against different currencies; bq_analysis.json must not silently
       propagate one as canonical (post-impl loop-1 H4 fix).
    """
    converted = {n for n, m in loaded_modes.items() if m == "post_dl3c_usd_converted"}
    # post-impl loop-1 cycle-2 ISS-021: `post_dl3c_failed_fx` also counts
    # as non-converted; mixing it with `post_dl3c_usd_converted` is a
    # genuine partial-FX state (one ticker artifact converted while
    # another's FX fetch failed). Pre-fix `failed_fx` was relabeled
    # `usd_native` and slipped through unchecked.
    non_converted = {
        n for n, m in loaded_modes.items()
        if m in (
            "post_dl3c_usd_native", "legacy_pre_dl3c", "post_dl3c_failed_fx",
        )
    }
    failed_fx = {n for n, m in loaded_modes.items() if m == "post_dl3c_failed_fx"}

    # post-impl loop-1 cycle-3 HIGH-1: a fully-failed-FX file set (every
    # gated artifact is post_dl3c_failed_fx) has no converted artifact
    # to trigger the `converted AND non_converted` branch below, so it
    # would slip through the gate and produce a cert-free bq_analysis.json
    # indistinguishable from a true USD-native run. failed_fx ANYWHERE is
    # a non-USD ticker whose FX fetch broke — fail-close so the operator
    # surfaces the issue rather than silently downstream-consuming
    # pre-conversion local-currency data.
    if failed_fx:
        print(
            f"{PREFIX}: FATAL — FX conversion failed for gated artifacts: "
            f"{sorted(failed_fx)}; the underlying data is still in local "
            f"currency. operator must retry FX fetch or resolve the source "
            f"data issue (e.g., add the currency to SUPPORTED_FX_CURRENCIES, "
            f"check yfinance availability)",
            file=sys.stderr,
        )
        sys.exit(1)

    if converted and non_converted:
        print(
            f"{PREFIX}: FATAL — mixed DL3c modes across gated artifacts: "
            f"converted={sorted(converted)}, "
            f"non_converted={sorted(non_converted)}; "
            f"operator must investigate inconsistent FX state",
            file=sys.stderr,
        )
        sys.exit(1)

    # H4: cert-divergence check across multiple converted artifacts.
    if converted_cert_dicts and len(converted_cert_dicts) >= 2:
        baseline_name = next(iter(converted_cert_dicts))
        baseline = converted_cert_dicts[baseline_name]
        for name, cert in converted_cert_dicts.items():
            if cert != baseline:
                print(
                    f"{PREFIX}: FATAL — converted DL3c certs disagree across "
                    f"gated artifacts: {baseline_name}.cert != {name}.cert. "
                    f"Likely a partial FX-state migration (one artifact "
                    f"reran against a different FX window). operator must "
                    f"reconcile or rerun all gated producers in one pass.",
                    file=sys.stderr,
                )
                sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Assemble bq_analysis.json from score + synthesis files"
    )
    parser.add_argument(
        "--report-dir",
        required=True,
        help="Report directory (e.g. reports/AAPL/20260406)",
    )
    parser.add_argument(
        "--weights",
        default=None,
        help="Dimension weights as comma-separated values (default: 0.35,0.35,0.30)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Analysis date override (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--tier-context-json",
        required=True,
        help="Path to transient JSON with tier_this_run + component_provenance.",
    )
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    # Weight precedence: CLI --weights > strategy.yaml > DEFAULT_WEIGHTS
    if args.weights:
        weights = parse_weights(args.weights)
    else:
        weights = _load_strategy_weights()

    # Read inputs
    score_files = {}
    for dim_name in DIMENSIONS:
        score_path = report_dir / "scores" / f"{dim_name}.json"
        if score_path.exists():
            score_files[dim_name] = read_json(
                str(score_path), f"scores/{dim_name}.json", PREFIX
            )
        else:
            print(
                f"{PREFIX}: WARNING — {score_path} not found, skipping",
                file=sys.stderr,
            )

    if not score_files:
        print(f"{PREFIX}: no score files found, cannot assemble", file=sys.stderr)
        sys.exit(1)

    # HIGH-19: fail closed on <2 dimensions. A valid BQ verdict requires
    # at least 2 of the 3 dimensions per prompts/score-synthesize.md.
    # The prior behavior (single-dim → warn + emit full verdict) produced
    # overall scores that looked authoritative but came from one pillar.
    if len(score_files) < 2:
        available = ", ".join(sorted(score_files)) or "(none)"
        print(
            f"{PREFIX}: only {len(score_files)}/3 dimensions available "
            f"({available}) — insufficient for a BQ verdict (per "
            f"prompts/score-synthesize.md a valid BQ requires \u22652 "
            f"dimensions). Provide more score files or re-run the skill.",
            file=sys.stderr,
        )
        sys.exit(1)

    synthesis_path = report_dir / "synthesis.json"
    synthesis = read_json(str(synthesis_path), "synthesis.json", PREFIX)

    validation_path = report_dir / "data" / "00_validation.json"
    validation = read_json(str(validation_path), "00_validation.json", PREFIX)

    # Use project's standard read_json (fail-closed with stderr + exit 1
    # on missing/malformed). Pre-existence check is redundant — read_json
    # reports missing-file clearly. Note: exit code is 1 not 2; that's the
    # established convention across all scripts (see cli_utils.py).
    tier_context = read_json(
        args.tier_context_json, "--tier-context-json", PREFIX
    )

    # Validate cross-check between 00_validation.tier_decided and tier_context.
    # Spec §6.2: missing or null in EITHER source aborts; "probe" is non-terminal.
    vtier = validation.get("tier_decided")
    ctier = tier_context.get("tier_this_run")
    if vtier is None or ctier is None:
        print(
            f"{PREFIX}: FATAL — tier is missing/null: "
            f"00_validation.tier_decided={vtier!r}, --tier-context-json={ctier!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if vtier == "probe" or ctier == "probe":
        print(
            f"{PREFIX}: FATAL — assembler received non-terminal tier 'probe'; "
            f"orchestrator must upgrade scope before calling assembler",
            file=sys.stderr,
        )
        sys.exit(1)
    if vtier != ctier:
        print(
            f"{PREFIX}: FATAL — tier mismatch: "
            f"00_validation.tier_decided={vtier!r} vs --tier-context-json={ctier!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # WebSearch source-binding gate (Plan B Task 6). The dims FRESH this
    # run were produced by agents under the post-binding prompt contract:
    # every WebSearch citation must bind outlet + url + access-date
    # ([WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]). Reused
    # (prior-run, possibly legacy) dims are NOT gated — incremental
    # partial/no_op runs over pre-binding priors keep working. The
    # fresh-dim set per tier mirrors the orchestrator's AGENTS_RUN map
    # (full: 3 dims; partial: forward+industry; no_op: none).
    from scripts.schemas import SchemaError as _SchemaError
    from scripts.schemas.source_tag import validate_source_tags
    for dim_name in WEBSEARCH_FRESH_DIMS_BY_TIER.get(ctier, ()):
        if dim_name not in score_files:
            continue
        try:
            validate_source_tags(
                score_files[dim_name],
                artifact=f"scores/{dim_name}",
                strict_websearch=True,
            )
        except _SchemaError as exc:
            print(
                f"{PREFIX}: FATAL — WebSearch source-binding violation in "
                f"scores/{dim_name}.json (fresh this run, tier={ctier}): "
                f"{exc}. Every [WebSearch:] citation in a fresh dimension "
                f"must be [WebSearch: <outlet>, <url>, accessed "
                f"<YYYY-MM-DD>] backed by a real search.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Determine ticker (from validation or first score file)
    ticker = validation.get("ticker")
    if not ticker:
        first_score = next(iter(score_files.values()))
        ticker = first_score.get("ticker", "UNKNOWN")

    # Extract fields from synthesis that are handled by the script
    freshness_interpretation = synthesis.pop("freshness_interpretation", None)
    synthesis.pop("business_quality", None)  # canonical value is scores.overall

    # DL3c §3.7.4: load gated artifacts, capture propagation cert, enforce
    # mixed-mode consistency BEFORE writing output. Missing artifacts are
    # tolerated (USD-only tickers typically lack adr_correction.json; some
    # tiers skip historical_multiples).
    loaded_modes, propagated_cert, converted_cert_dicts = (
        _load_dl3c_gated_artifacts(report_dir)
    )
    _check_mixed_dl3c_modes(loaded_modes, converted_cert_dicts)

    # Assemble
    result = {
        "meta": build_meta(ticker, validation, freshness_interpretation, args.date, tier_context),
        "scores": build_scores(score_files, weights),
        "synthesis": synthesis,
        "dimensions": build_dimensions(score_files),
    }

    # DL3c §3.7.4: propagate cert if any gated artifact was usd_converted.
    # Mixed-mode raise above guarantees the captured cert is representative
    # of all converted artifacts in this run.
    if propagated_cert is not None:
        result["currency_conversion"] = propagated_cert

    # Propagate the deterministic mixed-currency marker. fetch.py persists
    # `currency_consistency` onto 02_financial_data.json ONLY on the mixed
    # path (USD-native financials carry no block), so its mere presence is the
    # signal: field-level USD/native mixing that the self-repair did or did not
    # resolve. Recording it here makes the corruption a machine-readable fact in
    # the canonical artifact instead of leaving it to whatever the synthesis LLM
    # happened to write in prose (the prompts/score-*.md currency guards are the
    # SOFT layer; this is the deterministic HARD record consumed by /portfolio
    # and audits). Distinct from the DL3c `currency_conversion` cert above, which
    # records a CLEAN non-USD → USD conversion. This read is additive metadata,
    # so it tolerates a missing/malformed financials file rather than aborting a
    # core assemble that only needs scores + synthesis + validation.
    fin_path = report_dir / "data" / "02_financial_data.json"
    currency_consistency = None
    if fin_path.exists():
        try:
            with fin_path.open("r", encoding="utf-8") as f:
                fin_data = json.load(f)
            if isinstance(fin_data, dict):
                currency_consistency = fin_data.get("currency_consistency")
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"{PREFIX}: WARNING — could not read currency_consistency from "
                f"02_financial_data.json: {e}",
                file=sys.stderr,
            )
    if isinstance(currency_consistency, dict) and currency_consistency.get("status"):
        result["currency_consistency"] = currency_consistency
        note = f"financials currency_consistency: {currency_consistency['status']}"
        existing = result["meta"].get("freshness_note")
        result["meta"]["freshness_note"] = (
            f"{existing}. {note}" if existing else note
        )

    # WebSearch binding marker — FULL-tier only: all three dims (and the
    # synthesis derived from them) are fresh under the post-binding
    # contract, so the artifact as a whole is strict-validatable at every
    # future load. Partial/no_op artifacts embed reused (possibly
    # pre-binding) dims and stay unmarked → legacy-lenient. The staging
    # load below therefore strict-validates full-tier output fail-closed.
    if ctier == "full":
        from scripts.schemas.source_tag import stamp_websearch_binding
        result = stamp_websearch_binding(result)

    # DL3c marker — emit on EVERY assemble run (post-DL3c). Idempotent;
    # places `_dl3c_version: 1` as the FIRST root key per PEP 468.
    result = emit_dl3c_root_marker(result)

    # Write to a staging path, validate it, then atomically promote to the
    # canonical path — so the contract-validation failure mode honors the
    # same invariant the pre-write failures already enforce: a failed
    # assemble must NOT leave a bq_analysis.json at the canonical path (cf.
    # test_single_dimension_fails_closed / DL3c failed_fx), and a failed
    # re-run must not clobber a prior-good artifact. Validating the
    # serialized staging file (not the in-memory dict) still catches JSON
    # round-trip bugs (encoding, float precision); the promote is a rename,
    # so the validated bytes are exactly what lands at the canonical path.
    output_path = report_dir / "bq_analysis.json"
    staging_path = report_dir / ".bq_analysis.staging.json"
    write_output(result, str(staging_path))

    # Produce-time contract validation (fail-closed). Inline import matches
    # existing macro/strategy loader convention.
    from scripts.schemas.bq_analysis import load_bq_analysis
    from scripts.schemas import SchemaError
    try:
        load_bq_analysis(str(staging_path))
    except SchemaError as exc:
        staging_path.unlink(missing_ok=True)
        print(f"{PREFIX}: fatal: contract validation failed on "
              f"{output_path}: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Unexpected validator failure (not a contract SchemaError): keep
        # the error loud by re-raising, but don't leak the staging file.
        # Mirrors write_output's own temp-cleanup-then-raise pattern.
        staging_path.unlink(missing_ok=True)
        raise

    staging_path.replace(output_path)
    print(f"{PREFIX}: wrote {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
