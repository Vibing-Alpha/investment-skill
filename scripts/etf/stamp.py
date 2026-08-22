"""PR.STAMP — write `A.ETF_THESIS`, deciding which variant it is.

Three outcomes, and the model is only invoked for one of them:

    ineligible            the instrument was refused before any market
                          evidence was consulted. No snapshot is bound.
    analysis_unavailable  eligible, but this run's evidence was not enough.
                          The snapshot IS bound — the refusal is a claim
                          about a specific snapshot, and must stay re-checkable.
    thesis                eligible and ready. The model wrote it; every
                          number in it is bound to a hashed artifact.

`analysis_readiness` and `entry_eligibility` are stamped by this producer,
never by the model. A model that could write its own readiness could write
itself past the gate that exists to stop it.

Write order matters and is not decorative: stage both files, validate the
STAGED bundle, then promote. A validation failure after promotion would
leave an invalid artifact where consumers look. Promotion is per-file atomic
via `cli_utils`; it is NOT transactional across the pair, and this module
does not claim otherwise.

CLI:
    python3 -m scripts.etf.stamp --ticker T --profile PATH \\
        --market-snapshot PATH --state PATH --prior-thesis PATH_OR_NONE \\
        --authoring-date DATE --output-json PATH --output-markdown PATH \\
        [--model-json PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from scripts.cli_utils import normalize_ticker, write_output, write_text_atomic
from scripts.etf.readiness import analysis_readiness
from scripts.schemas.etf_thesis import (
    load_etf_bundle,
    load_etf_thesis,
    sha256_bytes,
    validate_etf_thesis,
)

_PREFIX = "etf.stamp"

# Fields the model owns. Anything else it sends is dropped rather than merged
# — a model cannot widen its own writable surface by inventing a key.
_MODEL_FIELDS = ("merit_recommendation", "merit_evidence", "kind",
                 "technical_timing", "environment", "entry_conditions",
                 "invalidation_conditions", "narrative")


def _held(state: Mapping[str, Any], ticker: str) -> bool:
    holdings = state.get("holdings") if isinstance(state, dict) else None
    if not isinstance(holdings, dict):
        return False
    for key in holdings:
        try:
            if normalize_ticker(key) == ticker:
                return True
        except (ValueError, TypeError):
            continue
    return False


def build_held_exit_context(prior_path, prior_bytes: bytes) -> Optional[dict]:
    """Carry a held position's exit conditions across a refusing run.

    The position exists whether or not this run could analyse the fund, so
    dropping its invalidation conditions would leave a holding with no
    documented exit — the one state a refusal must not create.
    """
    try:
        prior = validate_etf_thesis(json.loads(prior_bytes.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"{_PREFIX}: prior thesis {prior_path} unusable "
              f"({exc}); no held_exit_context carried", file=sys.stderr)
        return None
    if prior.variant != "thesis":
        # A prior refusal has no conditions of its own to carry. If it carried
        # a held_exit_context, that context is still the newest one there is.
        return prior.raw.get("held_exit_context")
    return {
        "invalidation_conditions": [dict(c) for c in prior.invalidation_conditions],
        "conditions_authored_at": f"{prior.as_of}T00:00:00Z",
        "source_thesis_sha256": sha256_bytes(prior_bytes),
    }


def build_thesis_document(*, ticker: str, authoring_date: str,
                          profile_bytes: bytes, market_bytes: bytes,
                          state: Mapping[str, Any],
                          prior_path: Optional[str],
                          prior_bytes: Optional[bytes],
                          model_output: Optional[Mapping[str, Any]]) -> dict:
    """Assemble the artifact. Pure given its inputs, so every branch is
    testable without a filesystem or a model."""
    from scripts.schemas.etf_profile import validate_etf_profile

    profile_doc = json.loads(profile_bytes.decode("utf-8"))
    profile = validate_etf_profile(profile_doc)
    market_doc = json.loads(market_bytes.decode("utf-8"))

    import datetime
    run_date = datetime.date.fromisoformat(authoring_date)
    readiness = analysis_readiness(market_doc, ticker=ticker,
                                   authoring_date=run_date)

    doc: dict[str, Any] = {
        "ticker": ticker,
        "as_of": authoring_date,
        "entry_eligibility": profile.entry_eligibility,
        "entry_reasons": list(profile.entry_reasons),
        "profile_sha256": sha256_bytes(profile_bytes),
        "meta": {"producer": "scripts.etf.stamp",
                 "authoring_date": authoring_date},
    }

    if profile.entry_eligibility != "pass":
        # Refused before any market evidence was consulted, so no snapshot is
        # bound: citing one would claim a market reading that never informed
        # the decision.
        doc["refusal_kind"] = "ineligible"
        doc["analysis_readiness"] = "unavailable"
        doc["analysis_reasons"] = ["not_evaluated_instrument_refused"]
    elif readiness.readiness != "ready":
        doc["refusal_kind"] = "analysis_unavailable"
        doc["market_snapshot_sha256"] = sha256_bytes(market_bytes)
        doc["analysis_readiness"] = "unavailable"
        doc["analysis_reasons"] = list(readiness.reasons)
    else:
        if not isinstance(model_output, dict):
            raise ValueError(
                "eligible and ready, but no evaluate-etf output was supplied; "
                "refusing to stamp a thesis with no argument in it")
        doc["market_snapshot_sha256"] = sha256_bytes(market_bytes)
        doc["analysis_readiness"] = "ready"
        doc["analysis_reasons"] = []
        for key in _MODEL_FIELDS:
            if key in model_output:
                doc[key] = model_output[key]

    if "refusal_kind" in doc and prior_bytes is not None and _held(state, ticker):
        context = build_held_exit_context(prior_path, prior_bytes)
        if context is not None:
            doc["held_exit_context"] = context

    return doc


# Fields whose stored form is a FRACTION but whose readable form is a
# percentage. `max_holding_weight = 0.0868534` makes a reader do the
# conversion; `8.69%` does not, and the decision is about concentration.
_FRACTION_FIELDS = frozenset({
    "max_holding_weight", "stock_frac", "cash_frac", "non_equity_frac",
    "allocation_sum_frac",
})
# Fields large enough that the digits past the decimal are noise.
_MONEY_FIELDS = frozenset({"avg_dollar_volume_usd", "aum_usd"})


def _no_false_zero(value: float, suffix: str) -> str:
    """Round for reading, but never round a nonzero value down to `0`.

    A displayed `0.00%` reads as "none", and "none" is a different fact from
    "very small" — EWSC was measured returning a holding row at 0.000057.
    Widen the precision until a nonzero value survives it.
    """
    if value == 0:
        return f"0{suffix}"
    for places in (2, 4, 6):
        text = f"{value:.{places}f}"
        if float(text) != 0:
            return f"{text.rstrip('0').rstrip('.')}{suffix}"
    return f"{value:.2e}{suffix}"


def _display(field_path: str, value):
    """The human form of a bound number.

    ONLY the Markdown is rounded. The JSON keeps full precision, because that
    is what `load_etf_bundle` compares against the artifact — rounding at the
    binding would turn every cited number into a mismatch.
    """
    leaf = field_path.rsplit(".", 1)[-1]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if leaf in _FRACTION_FIELDS:
        return _no_false_zero(value * 100, "%")
    if leaf in _MONEY_FIELDS:
        for unit, scale in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if abs(value) >= scale:
                return f"${value / scale:.2f}{unit}"
        return f"${value:,.2f}"
    if leaf.endswith("_pct"):
        return _no_false_zero(value, "%")
    # Everything else keeps two decimals unless it is already shorter.
    return _no_false_zero(value, "")


def _evidence_lines(heading: str, refs) -> list:
    """Render the numbers a section rests on.

    Without these the summary says "timing: neutral" and nothing else, which
    tells a reader the verdict but not whether to believe it — and the
    verdict is the part they could have guessed."""
    if not refs:
        return []
    out = [f"### {heading}", ""]
    for ref in refs:
        leaf = ref["field_path"].rsplit(".", 1)[-1]
        formula = ref.get("formula")
        note = f" — {formula}" if formula else ""
        out.append(f"- `{leaf}` = {_display(ref['field_path'], ref['value'])}{note}")
    out.append("")
    return out


def render_markdown(doc: Mapping[str, Any]) -> str:
    """The human-readable companion. Deliberately thin: it renders what the
    JSON already says and adds no judgement of its own."""
    lines = [f"# {doc['ticker']} — ETF thesis ({doc['as_of']})", ""]
    if "refusal_kind" in doc:
        lines += [f"**No thesis written.** ({doc['refusal_kind']})", ""]
        if doc["entry_reasons"]:
            lines.append("Eligibility: " + ", ".join(doc["entry_reasons"]))
        if doc["analysis_reasons"]:
            lines.append("Analysis: " + ", ".join(doc["analysis_reasons"]))
        held = doc.get("held_exit_context")
        if held:
            lines += ["", "## Exit conditions still in force",
                      f"_Authored {held['conditions_authored_at']}._", ""]
            for c in held["invalidation_conditions"]:
                lines.append(f"- **{c['id']}** {c['statement']}")
    else:
        lines += [f"**Merit:** {doc['merit_recommendation']}  ",
                  f"**Kind:** {doc['kind']}  ",
                  f"**Technical timing:** {doc['technical_timing']['assessment']}",
                  ""]
        lines += _evidence_lines("What the merit rests on", doc["merit_evidence"])
        lines += _evidence_lines("Timing readings",
                                 doc["technical_timing"]["evidence"])
        lines += ["## Environment", "", doc["environment"]["assessment"], ""]
        lines += _evidence_lines("Rates", doc["environment"]["evidence"])
        lines += ["## Entry conditions", ""]
        for c in doc["entry_conditions"]:
            lines.append(f"- **{c['id']}** {c['statement']}")
        lines += ["", "## Invalidation conditions", ""]
        for c in doc["invalidation_conditions"]:
            lines.append(f"- **{c['id']}** {c['statement']}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_bytes(path: str, label: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        print(f"{_PREFIX}: cannot read {label} {path}: {exc}", file=sys.stderr)
        raise SystemExit(1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.etf.stamp")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--market-snapshot", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--prior-thesis", required=True,
                    help="path to the prior etf_thesis.json, or the literal "
                         "`none`. Required, not optional: an omitted path and "
                         "a deliberate absence must not look alike.")
    ap.add_argument("--authoring-date", required=True)
    ap.add_argument("--output-json", required=True)
    ap.add_argument("--output-markdown", required=True)
    ap.add_argument("--model-json",
                    help="evaluate-etf output; required when the fund is "
                         "eligible and ready")
    args = ap.parse_args(argv)

    try:
        ticker = normalize_ticker(args.ticker)
    except ValueError as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return 2

    from scripts.schemas.strategy import parse_canonical_iso_date
    if parse_canonical_iso_date(args.authoring_date) is None:
        print(f"{_PREFIX}: --authoring-date must be YYYY-MM-DD",
              file=sys.stderr)
        return 2

    profile_bytes = _read_bytes(args.profile, "--profile")
    market_bytes = _read_bytes(args.market_snapshot, "--market-snapshot")

    import yaml
    try:
        with open(args.state, encoding="utf-8") as fh:
            state = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"{_PREFIX}: cannot read --state {args.state}: {exc}",
              file=sys.stderr)
        return 1

    prior_bytes = None
    prior_path = None
    if args.prior_thesis.lower() != "none":
        prior_path = args.prior_thesis
        prior_bytes = _read_bytes(prior_path, "--prior-thesis")

    model_output = None
    if args.model_json:
        try:
            model_output = json.loads(
                Path(args.model_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{_PREFIX}: cannot read --model-json: {exc}",
                  file=sys.stderr)
            return 1

    try:
        doc = build_thesis_document(
            ticker=ticker, authoring_date=args.authoring_date,
            profile_bytes=profile_bytes, market_bytes=market_bytes,
            state=state, prior_path=prior_path, prior_bytes=prior_bytes,
            model_output=model_output)
    except ValueError as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return 1

    # Stage, validate the STAGED bundle, then promote. Validating after
    # promotion would leave an invalid artifact where consumers look.
    staged_json = Path(args.output_json).with_suffix(".staging.json")
    staged_md = Path(args.output_markdown).with_suffix(".staging.md")
    markdown = render_markdown(doc)
    try:
        write_output(doc, str(staged_json))
        write_text_atomic(markdown, str(staged_md))
        load_etf_bundle(staged_json, args.profile, args.market_snapshot,
                        expected_ticker=ticker)
    except (OSError, ValueError) as exc:
        print(f"{_PREFIX}: staged artifact failed validation, nothing "
              f"promoted: {exc}", file=sys.stderr)
        for stale in (staged_json, staged_md):
            try:
                stale.unlink()
            except OSError:
                pass
        return 1

    # Per-file atomic replacement. NOT pair-atomic: a kill between the two
    # replaces can leave one file a generation behind, and saying otherwise
    # would be a promise this code cannot keep.
    try:
        write_output(doc, args.output_json)
        write_text_atomic(markdown, args.output_markdown)
    except OSError as exc:
        print(f"{_PREFIX}: promotion failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for stale in (staged_json, staged_md):
            try:
                stale.unlink()
            except OSError:
                pass

    print(Path(args.output_json).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
