"""P.ANALYSIS_READINESS — is there enough run-day evidence to write a thesis?

Evaluated after eligibility and before any model call. A fund that is
eligible but unreadable gets a refusal artifact, not a thesis written from
holes: the point of the gate is that nothing downstream has to wonder whether
a number it is reading was actually measured.

Nine legs, evaluated in order, and only the FIRST failure is reported. A fund
missing its price is also missing its indicators and its relative strength;
listing eight reasons would bury the one the user can act on.

Two legs exist because of a measured incident each:

- BENCHMARKS reads the BENCHMARK's chart status, not just a null relative
  strength. One 429 on SPY nulled `rs_vs_spy_3m` for every fund in a run
  while each fund's own chart status was PASSED — the refusal named the fund
  instead of the outage.
- INDICATOR_HISTORY is separate from INDICATOR_BLOCK so a young fund is told
  it is young. `too_young_for_indicators` means wait; `indicators_unavailable`
  means retry.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from scripts.etf.constants import REQUIRED_SNAPSHOT_FIELDS

# C.PRICE_MAX_AGE_SESSIONS / C.RATES_MAX_AGE_DAYS
PRICE_MAX_AGE_SESSIONS = 1
RATES_MAX_AGE_DAYS = 7

READINESS_LEG_ORDER = (
    "D.READINESS.PRICE",
    "D.READINESS.STRUCTURE",
    "D.READINESS.INDICATOR_HISTORY",
    "D.READINESS.INDICATOR_BLOCK",
    "D.READINESS.BENCHMARKS",
    "D.READINESS.INDICATORS",
    "D.READINESS.RATES_AVAILABLE",
    "D.READINESS.RATES_VINTAGE",
    "D.READINESS.TREASURY",
)

_BENCHMARKS = ("SPY", "QQQ")
_RS_KEYS = ("rs_vs_spy_3m", "rs_vs_qqq_3m")
_RATE_FIELDS = ("fed_funds", "us_10y", "us_5y", "spread_10y_5y")

# The indicator leaves, with the `ticker_indicators[T].` prefix stripped —
# the rates members of the same tuple are checked by the rates legs.
_INDICATOR_MEMBERS = tuple(
    f[len("ticker_indicators[T]."):]
    for f in REQUIRED_SNAPSHOT_FIELDS if f.startswith("ticker_indicators[T].")
)


@dataclass(frozen=True)
class ReadinessResult:
    readiness: str                    # V.ANALYSIS_READINESS
    reasons: tuple[str, ...]
    failed_leg: Optional[str]


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _get(mapping, *keys, default=None):
    cursor: Any = mapping
    for k in keys:
        if not isinstance(cursor, dict):
            return default
        cursor = cursor.get(k, default if k == keys[-1] else None)
    return cursor


def _member(block: Mapping, dotted: str):
    """Resolve `macd.macd_line` inside an indicator block. Returns the
    sentinel `_ABSENT` when any segment is missing, so an absent member is
    distinguishable from a present null."""
    cursor: Any = block
    for part in dotted.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return _ABSENT
        cursor = cursor[part]
    return cursor


_ABSENT = object()


def analysis_readiness(snapshot: Mapping[str, Any], *, ticker: str,
                       authoring_date: datetime.date) -> ReadinessResult:
    from scripts.delta.calendar import trading_days_between
    from scripts.schemas.strategy import parse_canonical_iso_date

    def refuse(leg: str, reason: str) -> ReadinessResult:
        return ReadinessResult("unavailable", (reason,), leg)

    # --- D.READINESS.PRICE -------------------------------------------------
    price_status = _get(snapshot, "chart_statuses", "ticker_prices", ticker)
    status = price_status.get("status") if isinstance(price_status, dict) else None
    price = _get(snapshot, "ticker_prices", ticker)
    price_as_of = (price_status.get("price_as_of")
                   if isinstance(price_status, dict) else None)
    as_of = parse_canonical_iso_date(price_as_of)
    # The conflict flag lives INSIDE the per-ticker status block, beside
    # `price_as_of` — NOT in a top-level `price_conflict_same_ts` map. The
    # design document named a top-level map that no producer emits; reading it
    # there returns None on every fund, and an absent-means-unanswered guard
    # then refuses every fund on every run. `scripts/macro.py` guarantees the
    # key is present even on its failure paths (`_NO_PRICE_PROVENANCE`), so
    # absence here really is a broken snapshot.
    conflict = (price_status.get("price_conflict_same_ts")
                if isinstance(price_status, dict) else None)
    if (status != "PASSED" or not _finite(price) or price <= 0
            or as_of is None
            or trading_days_between(as_of, authoring_date) > PRICE_MAX_AGE_SESSIONS
            or conflict is not False):
        # `conflict is not False`, not `if conflict`: an absent flag is an
        # unanswered question, and two prices carrying one timestamp means
        # one of them is wrong with nothing saying which.
        return refuse("D.READINESS.PRICE", "price_unavailable")

    # --- D.READINESS.STRUCTURE --------------------------------------------
    structure = _get(snapshot, "ticker_price_structure", ticker)
    if not isinstance(structure, dict):
        return refuse("D.READINESS.STRUCTURE", "structure_unavailable")
    if structure.get("anchor_session_covered") is not True:
        return refuse("D.READINESS.STRUCTURE", "structure_unavailable")
    if not (structure.get("lookback_complete") is True
            or structure.get("inception_proven") is True):
        return refuse("D.READINESS.STRUCTURE", "structure_unavailable")

    # --- D.READINESS.INDICATOR_HISTORY ------------------------------------
    ind_status = _get(snapshot, "ticker_indicator_status", ticker)
    ind_state = ind_status.get("status") if isinstance(ind_status, dict) else None
    if ind_state == "TOO_YOUNG":
        # Proven inception explains WHY the history is short; it does not
        # supply the closes MACD and RSI need. A 67-bar fund computed a
        # 3-month relative strength of -71.38 off a near-inception seed price.
        return refuse("D.READINESS.INDICATOR_HISTORY", "too_young_for_indicators")

    # --- D.READINESS.INDICATOR_BLOCK --------------------------------------
    block = _get(snapshot, "ticker_indicators", ticker)
    if ind_state != "PASSED" or not isinstance(block, dict):
        return refuse("D.READINESS.INDICATOR_BLOCK", "indicators_unavailable")

    # --- D.READINESS.BENCHMARKS -------------------------------------------
    for symbol in _BENCHMARKS:
        st = _get(snapshot, "chart_statuses", "market", symbol)
        if not isinstance(st, dict) or st.get("status") != "PASSED":
            return refuse("D.READINESS.BENCHMARKS", "benchmark_unavailable")
    for key in _RS_KEYS:
        value = _member(block, key)
        if value is _ABSENT or value is None or value == "insufficient_data":
            return refuse("D.READINESS.BENCHMARKS", "benchmark_unavailable")

    # --- D.READINESS.INDICATORS -------------------------------------------
    for dotted in _INDICATOR_MEMBERS:
        value = _member(block, dotted)
        if value is _ABSENT or value is None or value == "insufficient_data":
            return refuse("D.READINESS.INDICATORS", "indicators_unavailable")

    # --- D.READINESS.RATES_AVAILABLE --------------------------------------
    rates_status = snapshot.get("rates_status")
    rates_state = (rates_status.get("status")
                   if isinstance(rates_status, dict) else None)
    if rates_state not in ("PASSED", "PARTIAL"):
        return refuse("D.READINESS.RATES_AVAILABLE", "rates_unavailable")
    rates = snapshot.get("rates")
    if not isinstance(rates, dict):
        return refuse("D.READINESS.RATES_AVAILABLE", "rates_unavailable")
    for name in _RATE_FIELDS:
        if not _finite(rates.get(name)):
            return refuse("D.READINESS.RATES_AVAILABLE", "rates_unavailable")

    # --- D.READINESS.RATES_VINTAGE ----------------------------------------
    # Only the PARTIAL (disk-cache) variant carries a vintage; the live
    # PASSED variant emits none, so requiring one unconditionally would
    # refuse every healthy run.
    if rates_state == "PARTIAL":
        rates_as_of = parse_canonical_iso_date(rates_status.get("as_of_date"))
        if rates_as_of is None:
            return refuse("D.READINESS.RATES_VINTAGE", "rates_stale")
        if (authoring_date - rates_as_of).days > RATES_MAX_AGE_DAYS:
            return refuse("D.READINESS.RATES_VINTAGE", "rates_stale")

    # --- D.READINESS.TREASURY ---------------------------------------------
    treasury = _get(snapshot, "chart_statuses", "treasury")
    if not isinstance(treasury, dict) or not treasury:
        return refuse("D.READINESS.TREASURY", "treasury_unavailable")
    for leg, st in treasury.items():
        if not isinstance(st, dict) or st.get("status") != "PASSED":
            return refuse("D.READINESS.TREASURY", "treasury_unavailable")

    return ReadinessResult("ready", (), None)
