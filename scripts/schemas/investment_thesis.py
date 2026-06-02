"""Typed contract for `investment_thesis.json` written by the
investment-thesis SKILL (LLM agent via Bash).

Public API:
    validate_investment_thesis(data) -> InvestmentThesis
    load_investment_thesis(path) -> InvestmentThesis

CLI (as a thin ~30-line __main__ wrapper for SKILL.md Bash
fail-closed guard):
    python3 -m scripts.schemas.investment_thesis <path>
    Exit 0 on pass, 1 on any runtime failure (distinguish via stderr
    prefix "SchemaError:" vs "IOError:"). Exit 2 reserved for argparse
    usage errors.

Unit note: expected_return and max_downside are PERCENT units (per
prompts/evaluate-thesis.md:246-248), not decimal fractions.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from scripts.schemas.currency_conversion import CurrencyConversion
from scripts.schemas.dl3c_dispatch import Dl3cMode, dispatch_dl3c_mode
from scripts.schemas.errors import SchemaError
from scripts.schemas.source_tag import check_source_tag, validate_source_tags


_ARTIFACT = "investment_thesis"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)

_CONVICTION_ENUM = frozenset({"high", "medium", "low"})
_DOMINANT_ENUM = frozenset({"valuation", "technical", "events", "mixed"})
_ALIGNMENT_ENUM = frozenset({"strong", "partial", "conflicting"})

# unit-ok: percent range — load-bearing artifact contract per evaluate-thesis.md:246-248
_ER_MIN, _ER_MAX = -100.0, 1000.0
# unit-ok: percent range — load-bearing artifact contract per evaluate-thesis.md:246-248
_MD_MIN, _MD_MAX = -100.0, 0.0


__all__ = [
    "InvestmentThesis",
    "ThesisMeta",
    "validate_investment_thesis",
    "load_investment_thesis",
]


@dataclass(frozen=True)
class ThesisMeta:
    ticker: str
    analysis_date: str
    generated_at: str
    # Agent-authored (NOT orchestrator-stamped — stamp_thesis_meta preserves it):
    # the price ER/CE/max_downside were computed against. Exposed so portfolio_log
    # can record it (thesis_price) and a reader can detect ER/CE staleness when the
    # decision-time price has drifted. Optional — absent on legacy artifacts where
    # the agent did not emit it (e.g. early 2026-05-24 AMD/ASTS runs) → None.
    current_price: float | None = None


@dataclass(frozen=True)
class InvestmentThesis:
    meta: ThesisMeta
    # ER / CE are None when valuation is genuinely not-computable (no per-share
    # fair value — the ADR-ratio-unknown / un-anchorable cohort: TTDKY/MRAAY/
    # ASX). Represented as null, NOT a fabricated 0.0 (producer-consumer.md #4:
    # unknown is a failure, not zero). max_downside stays required — a
    # technical-structure proxy is a real value even when valuation is
    # un-anchorable.
    expected_return: float | None    # percent (1.04 = 1.04%); None = not computable
    max_downside: float       # percent (always <= 0)
    capital_efficiency: float | None # dimensionless ratio; neg for bearish; None = not computable
    thesis_conviction: str    # enum {high, medium, low}
    thesis_statement: str
    conditions_entry_attractive_if: tuple[str, ...]
    conditions_thesis_invalid_if: tuple[str, ...]
    signal_dominant: str
    signal_alignment: str
    # DL3c lineage — propagated from bq_analysis.currency_conversion via the
    # evaluate-thesis prompt. Closes the cert chain break previously found
    # in audit 2026-05-22: ADR/foreign-issuer thesis artifacts dropped the
    # FX-conversion provenance, so portfolio + downstream consumers couldn't
    # see that valuation multiples were USD-converted from a non-USD source.
    #
    # Default None for backward compat with pre-2026-05 thesis artifacts.
    # The agent MUST copy bq_analysis.currency_conversion verbatim (do not
    # synthesize a new cert — basis/source/window must trace to the
    # producer-side certificate emitted by extract_fcf + historical_multiples).
    currency_conversion: CurrencyConversion | None = None
    dl3c_mode: Dl3cMode = "legacy_pre_dl3c"


def _require(data: Mapping[str, Any], path: str, key: str) -> Any:
    full = f"{path}.{key}" if path else key
    if key not in data:
        raise SchemaError(_ARTIFACT, full, "missing required field")
    return data[key]


def _require_str(data: Mapping[str, Any], path: str, key: str,
                 *, enum: frozenset[str] | None = None,
                 allow_empty: bool = False) -> str:
    v = _require(data, path, key)
    full = f"{path}.{key}" if path else key
    if not isinstance(v, str):
        raise SchemaError(_ARTIFACT, full,
                          f"expected str, got {type(v).__name__}")
    if not allow_empty and not v:
        raise SchemaError(_ARTIFACT, full, "must be non-empty")
    if enum is not None and v not in enum:
        raise SchemaError(_ARTIFACT, full,
                          f"value {v!r} not in {sorted(enum)}")
    return v


def _require_float(data: Mapping[str, Any], path: str, key: str,
                   lo: float, hi: float) -> float:
    v = _require(data, path, key)
    full = f"{path}.{key}" if path else key
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SchemaError(_ARTIFACT, full,
                          f"expected number, got {type(v).__name__}")
    f = float(v)
    if not (lo <= f <= hi):
        raise SchemaError(_ARTIFACT, full,
                          f"value {f} not in [{lo}, {hi}]")
    return f


def _optional_bounded_float(data: Mapping[str, Any], path: str, key: str,
                            lo: float, hi: float) -> float | None:
    """Like _require_float, but the (still-required) key's VALUE may be null.

    Represents a genuinely not-computable metric (expected_return /
    capital_efficiency when valuation has no per-share fair value — the
    ADR-ratio-unknown / un-anchorable cohort like TTDKY/MRAAY/ASX). Per
    rules/producer-consumer.md #4, unknown must be null, NOT a fabricated 0.0;
    the portfolio_log review consumer already renders null ER/CE as "—". The
    key must still be PRESENT (explicit not-computable, not silent omission),
    and a non-null value must still be a finite number in [lo, hi] — the
    null-allowance does not weaken the type/range gate.
    """
    v = _require(data, path, key)
    if v is None:
        return None
    full = f"{path}.{key}" if path else key
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SchemaError(_ARTIFACT, full,
                          f"expected number or null, got {type(v).__name__}")
    f = float(v)
    if not (lo <= f <= hi):
        raise SchemaError(_ARTIFACT, full,
                          f"value {f} not in [{lo}, {hi}]")
    return f


def _require_non_empty_str_list(
    parent: Mapping[str, Any], parent_path: str, key: str,
) -> tuple[str, ...]:
    v = _require(parent, parent_path, key)
    full = f"{parent_path}.{key}"
    if v is None:
        raise SchemaError(_ARTIFACT, full, "must not be null")
    if not isinstance(v, list):
        raise SchemaError(_ARTIFACT, full,
                          f"expected list, got {type(v).__name__}")
    if not v:
        raise SchemaError(_ARTIFACT, full, "must be non-empty")
    out: list[str] = []
    for i, item in enumerate(v):
        if not isinstance(item, str) or not item.strip():
            raise SchemaError(_ARTIFACT, f"{full}[{i}]",
                              "must be non-empty str")
        out.append(item)
    return tuple(out)


def _validate_meta(raw: Any) -> ThesisMeta:
    if not isinstance(raw, Mapping):
        raise SchemaError(_ARTIFACT, "meta", "must be a dict")
    ticker = _require_str(raw, "meta", "ticker")
    analysis_date = _require_str(raw, "meta", "analysis_date")
    if not _DATE_RE.match(analysis_date):
        raise SchemaError(_ARTIFACT, "meta.analysis_date",
                          f"not ISO YYYY-MM-DD: {analysis_date!r}")
    generated_at = _require_str(raw, "meta", "generated_at")
    if not _ISO_TS_RE.match(generated_at):
        raise SchemaError(_ARTIFACT, "meta.generated_at",
                          f"not ISO timestamp with TZ: {generated_at!r}")
    # current_price is optional (the KEY may be absent, unlike ER/CE whose
    # keys must be present-but-may-be-null). Absent or null → None. A present
    # value must be a positive finite number — a price is never <= 0.
    cp_raw = raw.get("current_price")
    current_price: float | None = None
    if cp_raw is not None:
        if isinstance(cp_raw, bool) or not isinstance(cp_raw, (int, float)):
            raise SchemaError(_ARTIFACT, "meta.current_price",
                              f"expected number or null, got {type(cp_raw).__name__}")
        cp = float(cp_raw)
        if not math.isfinite(cp) or cp <= 0:
            raise SchemaError(_ARTIFACT, "meta.current_price",
                              f"must be a positive finite price, got {cp}")
        current_price = cp
    return ThesisMeta(ticker=ticker, analysis_date=analysis_date,
                      current_price=current_price,
                      generated_at=generated_at)


def validate_investment_thesis(data: Mapping[str, Any]) -> InvestmentThesis:
    if not isinstance(data, Mapping):
        raise SchemaError(_ARTIFACT, "<root>",
                          f"expected dict, got {type(data).__name__}")

    meta = _validate_meta(_require(data, "", "meta"))
    # ER / CE may be null (not-computable: un-anchorable valuation). max_downside
    # stays required — a technical-structure proxy is a real value regardless.
    er = _optional_bounded_float(data, "", "expected_return", _ER_MIN, _ER_MAX)
    md = _require_float(data, "", "max_downside", _MD_MIN, _MD_MAX)
    # Strictly negative: a 0% floor is not a valid downside, and CE = ER/|MD| is
    # computed downstream (scripts.thesis.compute_thesis_ce), so md==0 would be a
    # divide-by-zero. The prompt instructs a technical-structure floor below the
    # current price (always negative). Verified backward-compatible: 0/20 live
    # artifacts have max_downside==0.
    if md == 0:
        raise SchemaError(_ARTIFACT, "max_downside",
                          "must be strictly negative (a 0% floor is not a valid "
                          "downside; CE = ER/|max_downside| would divide by zero)")
    ce_v = _require(data, "", "capital_efficiency")
    if ce_v is None:
        ce: float | None = None
    elif isinstance(ce_v, bool) or not isinstance(ce_v, (int, float)):
        raise SchemaError(_ARTIFACT, "capital_efficiency",
                          f"expected number or null, got {type(ce_v).__name__}")
    else:
        ce = float(ce_v)
        # Note: CE = ER / |max_downside|. Bearish thesis (ER < 0) produces
        # negative CE — legitimate per prompts/evaluate-thesis.md:248 and
        # observed in 8/11 live artifacts. Validate only finiteness.
        if not math.isfinite(ce):
            raise SchemaError(_ARTIFACT, "capital_efficiency",
                              f"value {ce} must be finite")

    # Cross-field invariant: ER and CE travel together. CE = ER/|max_downside|,
    # so CE is not-computable exactly when ER is. The evaluate-thesis prompt
    # states this ("they travel together"); enforce it here so the schema — not
    # just the prompt — is the contract boundary (parity with max_downside being
    # always-required). Verified backward-compatible: 0/20 live artifacts violate.
    if (er is None) != (ce is None):
        raise SchemaError(
            _ARTIFACT, "capital_efficiency",
            "expected_return and capital_efficiency must both be null or both "
            "be numbers (CE = ER/|max_downside|, so CE is not-computable exactly "
            f"when ER is); got expected_return={er!r}, capital_efficiency={ce!r}")

    thesis_raw = _require(data, "", "thesis")
    if not isinstance(thesis_raw, Mapping):
        raise SchemaError(_ARTIFACT, "thesis", "must be a dict")
    conviction = _require_str(thesis_raw, "thesis", "conviction",
                              enum=_CONVICTION_ENUM)
    statement = _require_str(thesis_raw, "thesis", "statement")

    conditions_raw = _require(data, "", "conditions")
    if not isinstance(conditions_raw, Mapping):
        raise SchemaError(_ARTIFACT, "conditions", "must be a dict")
    entry = _require_non_empty_str_list(
        conditions_raw, "conditions", "entry_attractive_if",
    )
    invalid = _require_non_empty_str_list(
        conditions_raw, "conditions", "thesis_invalid_if",
    )

    sig_raw = _require(data, "", "signal_assessment")
    if not isinstance(sig_raw, Mapping):
        raise SchemaError(_ARTIFACT, "signal_assessment", "must be a dict")
    dominant = _require_str(sig_raw, "signal_assessment", "dominant_signal",
                            enum=_DOMINANT_ENUM)
    alignment = _require_str(sig_raw, "signal_assessment", "signal_alignment",
                             enum=_ALIGNMENT_ENUM)

    # Source-tag canonical check over entire payload EXCEPT the DL3c
    # `currency_conversion` subtree: cert `source` fields carry producer
    # identifier strings ("yfinance:JPY=X", "usd_native") whose grammar is
    # enforced by load_currency_conversion / load_fx_window, NOT the
    # analysis-citation [KIND: descriptor] form. Mirrors the carve-out in
    # bq_analysis._validate (DL3c §3.7.4).
    payload_for_source_tags = {
        k: v for k, v in data.items() if k != "currency_conversion"
    }
    validate_source_tags(payload_for_source_tags, artifact=_ARTIFACT)

    # Additionally: calculation_audit uses *_source suffix convention for
    # structured provenance fields (er_source, md_source, etc.). Walk
    # calculation_audit ONLY — scoping avoids false-positives on
    # free-form fields like source_summary.* or meta.current_price_source.
    audit_raw = data.get("calculation_audit")
    if audit_raw is not None:
        if not isinstance(audit_raw, Mapping):
            raise SchemaError(
                _ARTIFACT, "calculation_audit",
                f"expected dict, got {type(audit_raw).__name__}",
            )
        for k, v in audit_raw.items():
            if not (isinstance(k, str) and k.endswith("_source")):
                continue
            path = f"calculation_audit.{k}"
            if not isinstance(v, str):
                raise SchemaError(
                    _ARTIFACT, path,
                    f"source-tag value must be str, got {type(v).__name__}",
                )
            check_source_tag(v, artifact=_ARTIFACT, path=path)

    # Dispatch DL3c mode + validate cert (mirrors bq_analysis loader).
    # Errors from dispatch_dl3c_mode (illegal _dl3c_version, cert without
    # version, cert with basis=usd_native shape) propagate as SchemaError.
    dl3c_mode, currency_conversion = dispatch_dl3c_mode(
        dict(data), artifact=_ARTIFACT,
    )

    return InvestmentThesis(
        meta=meta,
        expected_return=er,
        max_downside=md,
        capital_efficiency=ce,
        thesis_conviction=conviction,
        thesis_statement=statement,
        conditions_entry_attractive_if=entry,
        conditions_thesis_invalid_if=invalid,
        signal_dominant=dominant,
        signal_alignment=alignment,
        currency_conversion=currency_conversion,
        dl3c_mode=dl3c_mode,
    )


def load_investment_thesis(path: str | Path) -> InvestmentThesis:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return validate_investment_thesis(data)


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.schemas.investment_thesis",
        description="Validate investment_thesis.json against schema contract.",
    )
    parser.add_argument("path", help="Path to investment_thesis.json")
    ns = parser.parse_args()
    try:
        load_investment_thesis(ns.path)
    except SchemaError as exc:
        print(f"SchemaError: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"IOError: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
