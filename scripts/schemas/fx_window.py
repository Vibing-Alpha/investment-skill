"""Typed contract for the `fx_window` array embedded in
`currency_conversion` certificates.

Validates: per-row currency match across the window, dates strictly
ascending, rates positive finite, source string non-empty.

Consumers: extract_fcf, historical_multiples, adr/correct (after they
call fx_rates.get_fx_window and embed the result).

Mirror `quarter_window.py` discipline: every check raises
SchemaError(ValueError); dataclasses are frozen; no defaults that hide
errors.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from scripts.fx_constants import SUPPORTED_FX_CURRENCIES
from scripts.schemas.errors import SchemaError

_ARTIFACT = "fx_window"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_CCY_RE = re.compile(r"^[A-Z]{3}$")


def _is_valid_iso_date(value: object) -> bool:
    """Shape (regex) + real-calendar-date (date.fromisoformat) parity check.

    Mirrors `quarter_window._is_valid_report_period` — regex-only matches
    accept malformed values like "2024-13-99" / "2024-02-30" which
    `date.fromisoformat` rejects.
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class FxRateRow:
    currency: str                       # ISO 4217, validated against SUPPORTED_FX_CURRENCIES
    date: str                           # YYYY-MM-DD, strictly increasing across window
    fx_rate_usd_per_local: float        # > 0 and finite
    source: str                         # "yfinance:<CCY>=X" or "usd_native"
    bar_date: Optional[str] = None      # may be omitted for usd_native
    lag_days: Optional[int] = None      # may be omitted for usd_native


@dataclass(frozen=True)
class FxWindow:
    currency: str                       # single value, must match every row
    source: str                         # single source for the window
    rows: tuple[FxRateRow, ...]         # at least 1; aligned ascending by date


def _validate_row(raw: object, idx: int) -> FxRateRow:
    """Build + validate one FxRateRow from a raw dict."""
    field_prefix = f"rows[{idx}]"
    if not isinstance(raw, dict):
        raise SchemaError(_ARTIFACT, field_prefix,
                          f"must be dict, got {type(raw).__name__}")

    # currency
    currency = raw.get("currency")
    if not isinstance(currency, str) or not _ISO_CCY_RE.fullmatch(currency):
        raise SchemaError(_ARTIFACT, f"{field_prefix}.currency",
                          f"must be uppercase ISO-4217 3-letter code, "
                          f"got {currency!r}")
    if currency not in SUPPORTED_FX_CURRENCIES:
        raise SchemaError(_ARTIFACT, f"{field_prefix}.currency",
                          f"unsupported currency {currency!r}; "
                          f"closed vocab is {sorted(SUPPORTED_FX_CURRENCIES)}")

    # date
    row_date = raw.get("date")
    if not _is_valid_iso_date(row_date):
        raise SchemaError(_ARTIFACT, f"{field_prefix}.date",
                          f"must be a valid YYYY-MM-DD calendar date, "
                          f"got {row_date!r}")

    # fx_rate_usd_per_local
    rate = raw.get("fx_rate_usd_per_local")
    # Reject bool explicitly — bool is a subclass of int but is not a
    # meaningful rate value; mirror fx_constants/numeric-coercion discipline.
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise SchemaError(_ARTIFACT, f"{field_prefix}.fx_rate_usd_per_local",
                          f"must be finite positive number, "
                          f"got {type(rate).__name__}={rate!r}")
    rate_f = float(rate)
    if not math.isfinite(rate_f):
        raise SchemaError(_ARTIFACT, f"{field_prefix}.fx_rate_usd_per_local",
                          f"must be finite (NaN/Inf rejected), got {rate!r}")
    if rate_f <= 0.0:
        raise SchemaError(_ARTIFACT, f"{field_prefix}.fx_rate_usd_per_local",
                          f"must be > 0, got {rate_f!r}")

    # source
    source = raw.get("source")
    if not isinstance(source, str) or not source:
        raise SchemaError(_ARTIFACT, f"{field_prefix}.source",
                          f"must be non-empty str, got {source!r}")

    # bar_date (optional)
    bar_date = raw.get("bar_date")
    if bar_date is not None and not _is_valid_iso_date(bar_date):
        raise SchemaError(_ARTIFACT, f"{field_prefix}.bar_date",
                          f"must be a valid YYYY-MM-DD calendar date or "
                          f"omitted, got {bar_date!r}")

    # lag_days (optional)
    lag_days = raw.get("lag_days")
    if lag_days is not None:
        if isinstance(lag_days, bool) or not isinstance(lag_days, int):
            raise SchemaError(_ARTIFACT, f"{field_prefix}.lag_days",
                              f"must be int or omitted, "
                              f"got {type(lag_days).__name__}={lag_days!r}")

    return FxRateRow(
        currency=currency,
        date=row_date,
        fx_rate_usd_per_local=rate_f,
        source=source,
        bar_date=bar_date,
        lag_days=lag_days,
    )


def load_fx_window(data: dict | list) -> FxWindow:
    """Validate + freeze.

    Accepts BOTH shapes:
      - bare list of row dicts (the embedded `window[]` array shape) —
        currency + source are inferred from the first row and required
        to match all rows.
      - wrapper dict with `rows` (or `window`) key + sibling `currency`
        and `source` keys.
    """
    if isinstance(data, list):
        raw_rows = data
        declared_currency: Optional[str] = None
        declared_source: Optional[str] = None
    elif isinstance(data, dict):
        # Prefer `rows`; tolerate `window` (mirrors §3.1.2 cert shape).
        if "rows" in data:
            raw_rows = data["rows"]
        elif "window" in data:
            raw_rows = data["window"]
        else:
            raise SchemaError(_ARTIFACT, "rows",
                              "wrapper dict must include 'rows' key "
                              "(list of row dicts)")
        if not isinstance(raw_rows, list):
            raise SchemaError(_ARTIFACT, "rows",
                              f"must be list, got {type(raw_rows).__name__}")
        declared_currency = data.get("currency")
        declared_source = data.get("source")
    else:
        raise SchemaError(_ARTIFACT, "data",
                          f"must be list or dict, got {type(data).__name__}")

    if not raw_rows:
        raise SchemaError(_ARTIFACT, "rows",
                          "must contain at least 1 row")

    rows: list[FxRateRow] = [_validate_row(r, i) for i, r in enumerate(raw_rows)]

    # Cross-row checks
    currencies = {r.currency for r in rows}
    if len(currencies) > 1:
        raise SchemaError(_ARTIFACT, "currency",
                          f"mixed_currency_window: rows carry "
                          f"{sorted(currencies)}; all rows must share one "
                          f"currency")
    sources = {r.source for r in rows}
    if len(sources) > 1:
        raise SchemaError(_ARTIFACT, "source",
                          f"mixed source within window: {sorted(sources)}; "
                          f"all rows must share one source")

    inferred_currency = next(iter(currencies))
    inferred_source = next(iter(sources))

    # If wrapper dict declared values, they must agree with the rows.
    if declared_currency is not None and declared_currency != inferred_currency:
        raise SchemaError(_ARTIFACT, "currency",
                          f"wrapper currency {declared_currency!r} does not "
                          f"match row currency {inferred_currency!r}")
    if declared_source is not None and declared_source != inferred_source:
        raise SchemaError(_ARTIFACT, "source",
                          f"wrapper source {declared_source!r} does not "
                          f"match row source {inferred_source!r}")

    # Strictly ascending dates
    for i in range(1, len(rows)):
        prev = rows[i - 1].date
        cur = rows[i].date
        if cur <= prev:
            raise SchemaError(
                _ARTIFACT, f"rows[{i}].date",
                f"dates must be strictly ascending; row {i - 1}={prev!r} "
                f"row {i}={cur!r}",
            )

    return FxWindow(
        currency=inferred_currency,
        source=inferred_source,
        rows=tuple(rows),
    )
