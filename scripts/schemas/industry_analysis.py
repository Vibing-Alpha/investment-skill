"""Typed contract for `industry_analysis.json` written by the
research-industry skill.

This artifact is the bridge between industry-level research (WebSearch-
driven) and per-ticker analysis (`/score-business`). Consumers read
`candidate_tickers[*].ticker` to drive downstream BQ scoring.

Public API:
    validate_industry_analysis(data: Mapping) -> IndustryAnalysis  # in-memory
    load_industry_analysis(path: str | Path) -> IndustryAnalysis   # I/O + validate

Producer (the skill itself + a thin assemble helper) calls validate_;
consumers (/score-business invocation orchestration, /portfolio sector
tilt logic) call load_.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from scripts.schemas.errors import SchemaError
from scripts.schemas.source_tag import SOURCE_TAG_RE


_ARTIFACT = "industry_analysis"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_LIFECYCLE = frozenset({"emerging", "growth", "mature", "decline"})
_POSITION = frozenset({"leader", "challenger", "niche", "disruptor"})
_REGIME = frozenset({"tailwind", "neutral", "headwind"})
_RESEARCH_MODE = frozenset({"full", "partial", "no_op"})

_MAX_CANDIDATES = 12  # selection budget; downstream /score-business is per-ticker
_MIN_CANDIDATES = 1   # at least one candidate, else why publish?

_PRIORITY_MIN = 1
_PRIORITY_MAX = 3  # 1 = top pick, 2 = strong, 3 = watchlist


__all__ = [
    "IndustryAnalysis",
    "IndustryMeta",
    "IndustryFraming",
    "CandidateTicker",
    "SectorSignal",
    "validate_industry_analysis",
    "load_industry_analysis",
]


@dataclass(frozen=True)
class IndustryMeta:
    """Identity + provenance for this run."""
    industry_name: str          # human label, e.g. "AI Chips"
    slug: str                   # filesystem-safe, e.g. "ai-chips"
    analysis_date: str          # YYYY-MM-DD, ET trading day
    generated_at: str           # ISO-8601 with tz
    research_mode: str          # full | partial | no_op
    prior_source_date: str | None = None  # for partial/no_op, the reused-from date


@dataclass(frozen=True)
class IndustryFraming:
    """Industry-level structural facts.

    All numeric fields must carry source tags per .claude/rules/anti-
    hallucination.md (e.g. `tam_2025_usd_b_source = "[WebSearch: IDC 2026
    AI Semiconductor Report]"`).
    """
    one_line_thesis: str        # ≤200 chars, the elevator pitch
    lifecycle: str              # _LIFECYCLE enum
    tam_usd_b: float | None     # current-year TAM in USD billions
    tam_source: str | None      # [WebSearch: ...] or [API: ...]
    cagr_5y_pct: float | None   # 5-year forward CAGR in percent (not decimal)
    cagr_source: str | None
    key_drivers: tuple[str, ...]  # 3-6 bullet points, each ≤120 chars + tagged


@dataclass(frozen=True)
class CandidateTicker:
    """One stock-picking candidate within the industry.

    `priority` drives the order /score-business should be invoked for
    follow-up BQ scoring.
    """
    ticker: str                 # US-listed, matches _TICKER_RE
    company_name: str           # display
    market_position: str        # _POSITION enum
    rationale: str              # one-line stock-picking thesis, ≤200 chars
    priority: int               # 1 (top) to 3 (watchlist)
    revenue_exposure_pct: float | None = None  # % of revenue from this industry
    exposure_source: str | None = None


@dataclass(frozen=True)
class SectorSignal:
    """Sector-ETF-level momentum signal.

    Derived from scripts.macro output (sector ETF price snapshots).
    """
    etf_symbol: str             # SOXX, XLK, XLV, etc.
    etf_name: str | None        # display
    trend_5d_pct: float | None
    trend_20d_pct: float | None
    trend_60d_pct: float | None
    regime: str                 # _REGIME enum
    regime_rationale: str       # one-line explanation of the regime call


@dataclass(frozen=True)
class IndustryAnalysis:
    meta: IndustryMeta
    framing: IndustryFraming
    candidate_tickers: tuple[CandidateTicker, ...]
    sector_signal: SectorSignal
    risks: tuple[str, ...]      # 2-5 bullets, each tagged
    catalysts: tuple[str, ...]  # 2-5 bullets, each tagged with date if scheduled


def _fail(field: str, message: str) -> None:
    """Raise SchemaError with the canonical (artifact, field, message) signature."""
    raise SchemaError(_ARTIFACT, field, message)


def _require(cond: bool, field: str, message: str) -> None:
    if not cond:
        _fail(field, message)


def _require_str(d: Mapping[str, Any], key: str, *, path: str) -> str:
    v = d.get(key)
    _require(isinstance(v, str) and v.strip(),
             f"{path}.{key}", "must be non-empty str")
    return v  # type: ignore[return-value]


def _opt_str(d: Mapping[str, Any], key: str, *, path: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    _require(isinstance(v, str), f"{path}.{key}", "must be str or null")
    return v  # type: ignore[return-value]


def _opt_float(d: Mapping[str, Any], key: str, *, path: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    _require(isinstance(v, (int, float)) and not isinstance(v, bool),
             f"{path}.{key}", "must be number or null")
    return float(v)


def _validate_meta(d: Mapping[str, Any]) -> IndustryMeta:
    path = "meta"
    name = _require_str(d, "industry_name", path=path)
    slug = _require_str(d, "slug", path=path)
    _require(_SLUG_RE.match(slug) is not None,
             "meta.slug", f"{slug!r} must match {_SLUG_RE.pattern}")
    analysis_date = _require_str(d, "analysis_date", path=path)
    _require(_DATE_RE.match(analysis_date) is not None,
             "meta.analysis_date", f"{analysis_date!r} must be YYYY-MM-DD")
    generated_at = _require_str(d, "generated_at", path=path)
    _require(_ISO_TS_RE.match(generated_at) is not None,
             "meta.generated_at", f"{generated_at!r} must be ISO-8601 with tz")
    research_mode = _require_str(d, "research_mode", path=path)
    _require(research_mode in _RESEARCH_MODE,
             "meta.research_mode", f"{research_mode!r} not in {sorted(_RESEARCH_MODE)}")
    prior = _opt_str(d, "prior_source_date", path=path)
    if prior is not None:
        _require(_DATE_RE.match(prior) is not None,
                 "meta.prior_source_date", f"{prior!r} must be YYYY-MM-DD")
    return IndustryMeta(
        industry_name=name, slug=slug, analysis_date=analysis_date,
        generated_at=generated_at, research_mode=research_mode,
        prior_source_date=prior,
    )


def _validate_framing(d: Mapping[str, Any]) -> IndustryFraming:
    path = "framing"
    thesis = _require_str(d, "one_line_thesis", path=path)
    _require(len(thesis) <= 200, "framing.one_line_thesis", "must be ≤200 chars")
    lifecycle = _require_str(d, "lifecycle", path=path)
    _require(lifecycle in _LIFECYCLE,
             "framing.lifecycle", f"{lifecycle!r} not in {sorted(_LIFECYCLE)}")
    tam = _opt_float(d, "tam_usd_b", path=path)
    tam_src = _opt_str(d, "tam_source", path=path)
    _require((tam is None) == (tam_src is None),
             "framing.tam_usd_b and tam_source", "must be both set or both null")
    # F12 (codex review cycle 2): companion *_source fields require source-
    # tag grammar `[KIND: descriptor]`. Pre-fix, only paired-presence was
    # checked; a non-tagged "IDC report" string passed validation, weakening
    # anti-hallucination contract.
    if tam_src is not None:
        _require(SOURCE_TAG_RE.search(tam_src) is not None,
                 "framing.tam_source",
                 f"{tam_src!r} missing canonical source tag "
                 f"[API|WebSearch|Filing|Calc: <descriptor>]")
    cagr = _opt_float(d, "cagr_5y_pct", path=path)
    cagr_src = _opt_str(d, "cagr_source", path=path)
    _require((cagr is None) == (cagr_src is None),
             "framing.cagr_5y_pct and cagr_source", "must be both set or both null")
    if cagr_src is not None:
        _require(SOURCE_TAG_RE.search(cagr_src) is not None,
                 "framing.cagr_source",
                 f"{cagr_src!r} missing canonical source tag "
                 f"[API|WebSearch|Filing|Calc: <descriptor>]")
    drivers = d.get("key_drivers", ())
    _require(isinstance(drivers, (list, tuple)) and 3 <= len(drivers) <= 6,
             "framing.key_drivers", "must be a list of 3-6 strings")
    for i, drv in enumerate(drivers):
        _require(isinstance(drv, str) and 0 < len(drv) <= 200,
                 f"framing.key_drivers[{i}]", "must be non-empty str ≤200 chars")
    return IndustryFraming(
        one_line_thesis=thesis, lifecycle=lifecycle,
        tam_usd_b=tam, tam_source=tam_src,
        cagr_5y_pct=cagr, cagr_source=cagr_src,
        key_drivers=tuple(drivers),
    )


def _validate_candidate(d: Mapping[str, Any], idx: int) -> CandidateTicker:
    path = f"candidate_tickers[{idx}]"
    ticker = _require_str(d, "ticker", path=path)
    _require(_TICKER_RE.match(ticker) is not None,
             f"{path}.ticker", f"{ticker!r} must match {_TICKER_RE.pattern}")
    name = _require_str(d, "company_name", path=path)
    position = _require_str(d, "market_position", path=path)
    _require(position in _POSITION,
             f"{path}.market_position", f"{position!r} not in {sorted(_POSITION)}")
    rationale = _require_str(d, "rationale", path=path)
    _require(len(rationale) <= 200, f"{path}.rationale", "must be ≤200 chars")
    priority = d.get("priority")
    _require(isinstance(priority, int) and not isinstance(priority, bool)
             and _PRIORITY_MIN <= priority <= _PRIORITY_MAX,
             f"{path}.priority", f"must be int in [{_PRIORITY_MIN}, {_PRIORITY_MAX}]")
    exposure = _opt_float(d, "revenue_exposure_pct", path=path)
    if exposure is not None:
        _require(0.0 <= exposure <= 100.0,
                 f"{path}.revenue_exposure_pct", "must be in [0, 100]")
    exposure_src = _opt_str(d, "exposure_source", path=path)
    _require((exposure is None) == (exposure_src is None),
             f"{path}.revenue_exposure_pct and exposure_source",
             "must be both set or both null")
    # F12 (codex review cycle 2): same grammar check for exposure_source.
    if exposure_src is not None:
        _require(SOURCE_TAG_RE.search(exposure_src) is not None,
                 f"{path}.exposure_source",
                 f"{exposure_src!r} missing canonical source tag "
                 f"[API|WebSearch|Filing|Calc: <descriptor>]")
    return CandidateTicker(
        ticker=ticker, company_name=name, market_position=position,
        rationale=rationale, priority=priority,  # type: ignore[arg-type]
        revenue_exposure_pct=exposure, exposure_source=exposure_src,
    )


def _validate_sector_signal(d: Mapping[str, Any]) -> SectorSignal:
    path = "sector_signal"
    etf = _require_str(d, "etf_symbol", path=path)
    _require(_TICKER_RE.match(etf) is not None,
             "sector_signal.etf_symbol", f"{etf!r} must match {_TICKER_RE.pattern}")
    etf_name = _opt_str(d, "etf_name", path=path)
    t5 = _opt_float(d, "trend_5d_pct", path=path)
    t20 = _opt_float(d, "trend_20d_pct", path=path)
    t60 = _opt_float(d, "trend_60d_pct", path=path)
    regime = _require_str(d, "regime", path=path)
    _require(regime in _REGIME,
             "sector_signal.regime", f"{regime!r} not in {sorted(_REGIME)}")
    rationale = _require_str(d, "regime_rationale", path=path)
    _require(len(rationale) <= 200,
             "sector_signal.regime_rationale", "must be ≤200 chars")
    return SectorSignal(
        etf_symbol=etf, etf_name=etf_name,
        trend_5d_pct=t5, trend_20d_pct=t20, trend_60d_pct=t60,
        regime=regime, regime_rationale=rationale,
    )


def validate_industry_analysis(data: Mapping[str, Any]) -> IndustryAnalysis:
    """In-memory validation. Raises SchemaError on any contract violation."""
    _require(isinstance(data, Mapping), "<root>", "top-level must be mapping")

    meta = _validate_meta(data.get("meta", {}))
    framing = _validate_framing(data.get("framing", {}))

    raw_candidates = data.get("candidate_tickers", ())
    _require(isinstance(raw_candidates, (list, tuple))
             and _MIN_CANDIDATES <= len(raw_candidates) <= _MAX_CANDIDATES,
             "candidate_tickers",
             f"must be a list of {_MIN_CANDIDATES}-{_MAX_CANDIDATES} entries")
    candidates = tuple(_validate_candidate(c, i) for i, c in enumerate(raw_candidates))

    # Dedupe check: same ticker twice = consumer ambiguity bug.
    seen: set[str] = set()
    for c in candidates:
        _require(c.ticker not in seen,
                 "candidate_tickers", f"contains duplicate ticker {c.ticker!r}")
        seen.add(c.ticker)

    sector_signal = _validate_sector_signal(data.get("sector_signal", {}))

    risks = data.get("risks", ())
    _require(isinstance(risks, (list, tuple)) and 2 <= len(risks) <= 5,
             "risks", "must be a list of 2-5 strings")
    for i, r in enumerate(risks):
        _require(isinstance(r, str) and 0 < len(r) <= 200,
                 f"risks[{i}]", "must be non-empty str ≤200 chars")

    catalysts = data.get("catalysts", ())
    _require(isinstance(catalysts, (list, tuple)) and 2 <= len(catalysts) <= 5,
             "catalysts", "must be a list of 2-5 strings")
    for i, c in enumerate(catalysts):
        _require(isinstance(c, str) and 0 < len(c) <= 200,
                 f"catalysts[{i}]", "must be non-empty str ≤200 chars")

    # Source-tag enforcement: every string field that carries a claim
    # must include a [API: ...] / [WebSearch: ...] / [Calc: ...] / [Filing: ...]
    # tag, EXCEPT the fields where we explicitly carry a separate _source
    # companion field (tam_source / cagr_source / exposure_source).
    # Per-driver, per-risk, per-catalyst, and per-candidate-rationale must be tagged.
    untagged: list[str] = []
    for i, drv in enumerate(framing.key_drivers):
        if SOURCE_TAG_RE.search(drv) is None:
            untagged.append(f"framing.key_drivers[{i}]")
    for i, c in enumerate(candidates):
        if SOURCE_TAG_RE.search(c.rationale) is None:
            untagged.append(f"candidate_tickers[{i}].rationale")
    for i, r in enumerate(risks):
        if SOURCE_TAG_RE.search(r) is None:
            untagged.append(f"risks[{i}]")
    for i, c in enumerate(catalysts):
        if SOURCE_TAG_RE.search(c) is None:
            untagged.append(f"catalysts[{i}]")
    _require(not untagged,
             "source tags",
             f"missing in: {', '.join(untagged)} "
             "(every claim needs [API:...] / [WebSearch:...] / [Calc:...] / [Filing:...])")

    return IndustryAnalysis(
        meta=meta, framing=framing,
        candidate_tickers=candidates,
        sector_signal=sector_signal,
        risks=tuple(risks),
        catalysts=tuple(catalysts),
    )


def load_industry_analysis(path: str | Path) -> IndustryAnalysis:
    """I/O wrapper: read JSON from disk then validate."""
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise SchemaError(_ARTIFACT, str(p), f"failed to load: {e}") from e
    return validate_industry_analysis(data)
