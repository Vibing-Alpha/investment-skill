"""Typed contract for `bq_analysis.json` written by scripts.assemble.

Consumer-driven required fields — see
docs/superpowers/specs/2026-04-22-llm-contract-minimum-design.md
for the contract + rationale.

Public API:
    validate_bq_analysis(data: Mapping) -> BqAnalysis   # pure in-memory
    load_bq_analysis(path: str | Path) -> BqAnalysis    # I/O + validate

Producers call validate_bq_analysis (hold dict in memory);
consumers call load_bq_analysis (read from disk).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from scripts.schemas.currency_conversion import CurrencyConversion
from scripts.schemas.dl3c_dispatch import Dl3cMode, dispatch_dl3c_mode
from scripts.schemas.errors import SchemaError
from scripts.schemas.source_tag import validate_source_tags


_ARTIFACT = "bq_analysis"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
_WEIGHTS_TOL = 0.01  # matches scripts/assemble.py:274 producer tolerance

_REQUIRED_WEIGHT_KEYS = frozenset({"fundamental", "forward", "industry"})

_SCORE_MIN = 0.0   # unit-ok: BQ scores are 0-10 rubric, not percent/decimal
_SCORE_MAX = 10.0  # unit-ok: BQ scores are 0-10 rubric, not percent/decimal


__all__ = [
    "BqAnalysis",
    "BqMeta",
    "BqScores",
    "validate_bq_analysis",
    "load_bq_analysis",
]


@dataclass(frozen=True)
class BqMeta:
    ticker: str
    analysis_date: str
    generated_at: str


@dataclass(frozen=True)
class BqScores:
    overall: float
    fundamental: float
    forward: float
    industry: float
    weights: dict[str, float]


@dataclass(frozen=True)
class BqAnalysis:
    meta: BqMeta
    scores: BqScores
    # synthesis: required to be present, inner contents loose
    synthesis: Mapping[str, Any] = field(default_factory=dict)
    # Flat convenience for path dimensions.industry.peer_tickers.
    # Semantic: empty tuple () unambiguously means "dimensions.industry
    # dim absent" (partial-dim assembly path). If industry IS present,
    # validator enforces a non-empty list; a present-but-empty tuple
    # cannot occur at load time. Consumers: check truthiness (`if doc.
    # dimensions_industry_peer_tickers:`) to distinguish.
    dimensions_industry_peer_tickers: tuple[str, ...] = ()
    # post-impl loop-1 H5: DL3c dispatch surfaces at the bq_analysis layer.
    # Pre-fix `validate_bq_analysis` excluded `currency_conversion` from
    # source-tag walks but never validated the cert itself, so a
    # malformed-shape cert (e.g., basis="usd_xyz" / missing window /
    # disagreeing inner currency) propagated through assemble unchecked.
    # Now we route the same `dispatch_dl3c_mode` the producer loaders use:
    # legacy_pre_dl3c / post_dl3c_usd_native synthesize a usd_native cert;
    # post_dl3c_usd_converted loads + validates the embedded cert.
    currency_conversion: CurrencyConversion | None = None
    dl3c_mode: Dl3cMode = "legacy_pre_dl3c"
    # Deterministic mixed-currency detector marker, propagated from
    # 02_financial_data.json by assemble.py ONLY when a foreign-ADR feed
    # returned a field-level USD/native mix (status "mixed_unrepairable" /
    # "repaired"). Absent ⇒ USD-native (fetch persists it only on the mixed
    # path). Inner contents are consumer-loose (mirrors `synthesis`): the
    # detector owns the shape. Typed here — not just raw-JSON — so loader-based
    # consumers (e.g. scripts/portfolio_log.py) can gate on corrupted financials
    # instead of trusting synthesis prose.
    currency_consistency: Mapping[str, Any] | None = None


def _require(data: Mapping[str, Any], path: str, key: str) -> Any:
    if key not in data:
        raise SchemaError(_ARTIFACT, f"{path}.{key}" if path else key,
                          "missing required field")
    return data[key]


def _require_str(data: Mapping[str, Any], path: str, key: str,
                 *, allow_empty: bool = False) -> str:
    v = _require(data, path, key)
    full = f"{path}.{key}" if path else key
    if not isinstance(v, str):
        raise SchemaError(_ARTIFACT, full, f"expected str, got {type(v).__name__}")
    if not allow_empty and not v:
        raise SchemaError(_ARTIFACT, full, "must be non-empty")
    return v


def _require_float_in_range(
    data: Mapping[str, Any], path: str, key: str,
    lo: float, hi: float,
) -> float:
    v = _require(data, path, key)
    full = f"{path}.{key}" if path else key
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise SchemaError(_ARTIFACT, full,
                          f"expected number, got {type(v).__name__}")
    f = float(v)
    if not (lo <= f <= hi):
        raise SchemaError(_ARTIFACT, full,
                          f"value {f} not in [{lo}, {hi}]")
    return f


def _validate_meta(raw: Any) -> BqMeta:
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
    return BqMeta(ticker=ticker, analysis_date=analysis_date,
                  generated_at=generated_at)


def _validate_scores(raw: Any) -> BqScores:
    if not isinstance(raw, Mapping):
        raise SchemaError(_ARTIFACT, "scores", "must be a dict")
    overall = _require_float_in_range(raw, "scores", "overall",
                                      _SCORE_MIN, _SCORE_MAX)
    fundamental = _require_float_in_range(raw, "scores", "fundamental",
                                          _SCORE_MIN, _SCORE_MAX)
    forward = _require_float_in_range(raw, "scores", "forward",
                                      _SCORE_MIN, _SCORE_MAX)
    industry = _require_float_in_range(raw, "scores", "industry",
                                       _SCORE_MIN, _SCORE_MAX)
    weights_raw = _require(raw, "scores", "weights")
    if not isinstance(weights_raw, Mapping):
        raise SchemaError(_ARTIFACT, "scores.weights", "must be a dict")
    actual_keys = set(weights_raw.keys())
    missing = _REQUIRED_WEIGHT_KEYS - actual_keys
    if missing:
        raise SchemaError(_ARTIFACT, "scores.weights",
                          f"missing keys: {sorted(missing)}")
    extra = actual_keys - _REQUIRED_WEIGHT_KEYS
    if extra:
        raise SchemaError(_ARTIFACT, "scores.weights",
                          f"unexpected keys: {sorted(extra)}")
    weights: dict[str, float] = {}
    for k in _REQUIRED_WEIGHT_KEYS:
        v = weights_raw[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise SchemaError(_ARTIFACT, f"scores.weights.{k}",
                              f"expected number, got {type(v).__name__}")
        fv = float(v)
        if not (0.0 <= fv <= 1.0):
            raise SchemaError(_ARTIFACT, f"scores.weights.{k}",
                              f"value {fv} not in [0.0, 1.0]")
        weights[k] = fv
    total = sum(weights.values())
    if abs(total - 1.0) > _WEIGHTS_TOL:
        raise SchemaError(_ARTIFACT, "scores.weights",
                          f"sum {total:.6f} deviates from 1.0 by more "
                          f"than tolerance {_WEIGHTS_TOL}")
    return BqScores(overall=overall, fundamental=fundamental,
                    forward=forward, industry=industry, weights=weights)


def _validate_peer_tickers(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise SchemaError(
            _ARTIFACT, "dimensions.industry.peer_tickers",
            f"expected list, got {type(raw).__name__}",
        )
    if not raw:
        raise SchemaError(_ARTIFACT, "dimensions.industry.peer_tickers",
                          "must be non-empty")
    out: list[str] = []
    for i, t in enumerate(raw):
        path = f"dimensions.industry.peer_tickers[{i}]"
        if not isinstance(t, str):
            raise SchemaError(_ARTIFACT, path,
                              f"expected str, got {type(t).__name__}")
        if not _TICKER_RE.match(t):
            raise SchemaError(_ARTIFACT, path,
                              f"ticker {t!r} does not match {_TICKER_RE.pattern}")
        out.append(t)
    return tuple(out)


def validate_bq_analysis(data: Mapping[str, Any]) -> BqAnalysis:
    if not isinstance(data, Mapping):
        raise SchemaError(_ARTIFACT, "<root>",
                          f"expected dict, got {type(data).__name__}")
    meta_raw = _require(data, "", "meta")
    scores_raw = _require(data, "", "scores")
    synthesis_raw = _require(data, "", "synthesis")
    dimensions_raw = _require(data, "", "dimensions")

    meta = _validate_meta(meta_raw)
    scores = _validate_scores(scores_raw)
    if not isinstance(synthesis_raw, Mapping):
        raise SchemaError(_ARTIFACT, "synthesis",
                          f"expected dict, got {type(synthesis_raw).__name__}")
    if not isinstance(dimensions_raw, Mapping):
        raise SchemaError(_ARTIFACT, "dimensions",
                          f"expected dict, got {type(dimensions_raw).__name__}")

    # Industry dimension is optional — assemble.py's main() has a >=2-dim
    # gate that allows fundamental+forward without industry. Peer_tickers
    # is required ONLY when the industry dim is present.
    peer_tickers: tuple[str, ...] = ()
    if "industry" in dimensions_raw:
        industry = dimensions_raw["industry"]
        if not isinstance(industry, Mapping):
            raise SchemaError(_ARTIFACT, "dimensions.industry",
                              f"expected dict, got {type(industry).__name__}")
        peer_tickers_raw = _require(industry, "dimensions.industry", "peer_tickers")
        peer_tickers = _validate_peer_tickers(peer_tickers_raw)

    # Source-tag validation walks the loaded payload. EXCEPT the DL3c
    # `currency_conversion` subtree: cert `source` fields carry producer
    # identifier strings ("yfinance:JPY=X", "usd_native") whose grammar is
    # enforced by load_currency_conversion / load_fx_window, NOT the
    # analysis-citation `[KIND: descriptor]` form. Walking it would
    # spuriously reject every cert-bearing artifact (DL3c §3.7.4).
    payload_for_source_tags = {
        k: v for k, v in data.items() if k != "currency_conversion"
    }
    validate_source_tags(payload_for_source_tags, artifact=_ARTIFACT)

    # post-impl loop-1 H5: dispatch DL3c mode + validate cert. Errors from
    # dispatch_dl3c_mode (illegal _dl3c_version, cert without version,
    # cert with basis=usd_native) propagate as SchemaError naturally.
    dl3c_mode, currency_conversion = dispatch_dl3c_mode(
        dict(data), artifact=_ARTIFACT,
    )

    # Consumer-loose: keep the detector marker as-is when it's a dict, else
    # None. The detector (scripts/schemas/currency_consistency.py) owns the
    # inner shape; we do not re-validate it here.
    cc_raw = data.get("currency_consistency")
    currency_consistency = cc_raw if isinstance(cc_raw, Mapping) else None

    return BqAnalysis(
        meta=meta,
        scores=scores,
        synthesis=synthesis_raw,
        dimensions_industry_peer_tickers=peer_tickers,
        currency_conversion=currency_conversion,
        dl3c_mode=dl3c_mode,
        currency_consistency=currency_consistency,
    )


def load_bq_analysis(path: str | Path) -> BqAnalysis:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return validate_bq_analysis(data)
