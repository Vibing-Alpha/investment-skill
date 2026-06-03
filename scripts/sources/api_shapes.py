"""DL2 runtime schema validator.

Declarative shape-spec DSL + validate_api_shape walker. Used by adapter
entry points to gate raw upstream JSON at boundary. Stdlib-only; no
pydantic / jsonschema (CLAUDE.md hard constraint).

See docs/superpowers/specs/2026-04-25-dl2-adapter-envelope-design.md
§Shape-spec DSL for the contract.

Validation-placement convention (v24):
- Single-call adapters validate the RAW upstream response pre-transform.
  Shape top-level key mirrors the raw response key (e.g., FD_ANALYST_SHAPE
  uses `analyst_estimates` because `fetch_analyst_estimates` reads
  `response.get("analyst_estimates", [])` before renaming).
- Aggregator adapters (one function making multiple HTTP calls merged
  into a single dict) validate the AGGREGATED post-transform dict; no
  single "raw" exists. Only FD_FINANCIALS_SHAPE follows this pattern
  (`fetch_financial_statements` makes 3 separate calls and assembles
  `{"income_statements": [...], "balance_sheets": [...], "cash_flows":
  [...]}`).
- Per-item required keys should pin ONLY fields that the adapter or its
  downstream consumers actually dereference. FD response items vary
  endpoint-by-endpoint: insider/segmented items carry `ticker`, but
  analyst/institutional items do NOT. Do not assume uniformity.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from scripts.schemas.errors import SchemaError


class ShapeError(SchemaError):
    """Raised by ValidationResult.require() when a validation fails.
    Subclass of SchemaError (which subclasses ValueError). Existing
    `except ValueError` / `except SchemaError` callers continue to
    catch it; new code can catch the narrower type."""


@dataclass(frozen=True)
class Optional_:
    """Field modifier: if present-and-None or absent, validation passes;
    if present, inner spec runs. Declared as @dataclass(frozen=True) for
    AST/type-check distinguishability from bare types."""
    inner: Any    # ShapeSpec; typed as Any to avoid recursive TypeAlias stdlib limitation


# ISS-220 SF-C (Loop32 cycle 2): YYYY-MM-DD validation primitive.
# Sentinel class (not instantiated) — pass `Date_yyyy_mm_dd` directly
# as the schema value where a date-typed string is expected.
import re as _re
_YYYY_MM_DD_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_yyyy_mm_dd(s: Any) -> bool:
    """Return True when *s* is a non-empty `YYYY-MM-DD` string with a
    valid calendar date.

    Used by:
    - `Date_yyyy_mm_dd` shape primitive (api_shapes._walk)
    - `_check_date_array` validator (SEC submissions reportDate / filingDate)
    - `fmp._is_valid_yyyy_mm_dd` (re-export for fmp adapter)
    - call-site guards in fetch.py before `int(report_date[:4])` slicing

    Rejects: non-str, empty str, malformed (`"unknown"`, `"2024-xx"`,
    `"2024/01/02"`, dates with non-numeric digits).

    ISS-220 SF-F (Loop34 cycle 1): regex pass alone is insufficient —
    `2024-99-99` matched the pre-fix `^\\d{4}-\\d{2}-\\d{2}$` regex
    but is calendar-impossible (month 99). Downstream `int(report_date
    [5:7])` produced `Q33` and entered fallback queries. Now `strptime`
    enforces calendar validity (rejects month > 12, day > month-length,
    Feb 29 on non-leap years). Year range: any 4-digit year accepted
    (0001-9999); we don't constrain to 1900-2100 because SEC filings
    historically include early 1900s dates and clamping risks legitimate
    rejection.
    """
    if not isinstance(s, str):
        return False
    if not _YYYY_MM_DD_RE.match(s):
        return False
    # Calendar validation
    from datetime import datetime as _dt
    try:
        _dt.strptime(s, "%Y-%m-%d")
    except ValueError:
        return False
    return True


class Date_yyyy_mm_dd:
    """Shape DSL sentinel class: the value at this position must be a
    non-empty `YYYY-MM-DD` string. Use as schema field type, not as
    instance.

        FOO_SHAPE = {
            "filing_date": Date_yyyy_mm_dd,
            "report_date": Optional_(Date_yyyy_mm_dd),  # nullable
            ...
        }

    Why a sentinel class and not a Callable validator: the walker
    handles bare-type branches at line 82 (`isinstance(schema, type)`)
    BEFORE the Callable branch. `Date_yyyy_mm_dd` IS a type (every
    class is), so without an explicit `schema is Date_yyyy_mm_dd`
    check inserted before the bare-type branch, the walker would do
    `isinstance(raw, Date_yyyy_mm_dd)` which fails because the
    primitive is the type itself, not an instance. The walker change
    is a 5-line insertion at the top of `_walk`.
    """
    @staticmethod
    def validate(value: Any, path: str) -> list[str]:
        # ISS-220 SF-F (Loop34 cycle 1): route through _is_valid_yyyy_mm_dd
        # so calendar validation (datetime.strptime) applies here too.
        # Pre-fix this method had its own regex check that accepted
        # `2024-99-99`. Distinct error messages per failure mode kept
        # for diagnostic clarity (None vs non-str vs format vs calendar).
        if value is None:
            return [f"{path}: expected YYYY-MM-DD str, got None"]
        if not isinstance(value, str):
            return [f"{path}: expected YYYY-MM-DD str, got {type(value).__name__}"]
        if not _YYYY_MM_DD_RE.match(value):
            return [f"{path}: expected YYYY-MM-DD format, got {value!r}"]
        if not _is_valid_yyyy_mm_dd(value):
            # regex passed but calendar invalid (e.g. 2024-99-99)
            return [f"{path}: expected valid calendar date YYYY-MM-DD, got {value!r}"]
        return []


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()

    def require(self) -> None:
        """Raise ShapeError if not ok. Most adapters use the non-raising
        branch + AdapterResult.failed_from_shape; raise is for tests +
        probes."""
        if not self.ok:
            raise ShapeError(
                "api_shape",
                "(multiple)",
                "; ".join(self.errors),
            )


def validate_api_shape(raw: Any, schema: Any) -> ValidationResult:
    """Recursive structural validation. Accumulates all errors (no
    short-circuit). Returns ValidationResult."""
    errors = _walk(raw, schema, path="$")
    return ValidationResult(ok=not errors, errors=tuple(errors))


def _walk(raw: Any, schema: Any, *, path: str) -> list[str]:
    # Optional_ wrapper: None or absent is OK; otherwise recurse into inner
    if isinstance(schema, Optional_):
        if raw is None:
            return []
        return _walk(raw, schema.inner, path=path)

    # ISS-220 SF-C (Loop32 cycle 2): Date_yyyy_mm_dd sentinel class.
    # MUST come BEFORE the bare-type branch — `Date_yyyy_mm_dd` IS a
    # type (every class is), so `isinstance(schema, type)` would match
    # and try `isinstance(raw, Date_yyyy_mm_dd)` which fails (the
    # primitive is the type itself, not an instance). Identity check
    # routes to the validator method.
    if schema is Date_yyyy_mm_dd:
        return Date_yyyy_mm_dd.validate(raw, path)

    # Bare type check
    if isinstance(schema, type):
        # ISS-034 (Loop2): bool is a subclass of int — `[int]` schema would
        # otherwise accept `[True, False]` as a valid timestamp list,
        # producing a 1970-epoch PASSED envelope downstream. Reject bool
        # specifically when the schema is `int`. Same logic for float
        # (bool also subclasses int but not float; harmless but explicit).
        if schema is int and isinstance(raw, bool):
            return [f"{path}: expected int, got bool"]
        if not isinstance(raw, schema):
            return [f"{path}: expected {schema.__name__}, got {type(raw).__name__}"]
        return []

    # List-of-spec (single-element list)
    if isinstance(schema, list):
        if len(schema) != 1:
            return [f"{path}: list-schema must have exactly 1 element spec, got {len(schema)}"]
        if not isinstance(raw, list):
            return [f"{path}: expected list, got {type(raw).__name__}"]
        errors: list[str] = []
        for i, item in enumerate(raw):
            errors.extend(_walk(item, schema[0], path=f"{path}[{i}]"))
        return errors

    # Dict schema
    if isinstance(schema, dict):
        if not isinstance(raw, dict):
            return [f"{path}: expected dict, got {type(raw).__name__}"]
        errors = []
        for k, spec in schema.items():
            is_opt = isinstance(spec, Optional_)
            if k not in raw:
                if not is_opt:
                    errors.append(f"{path}.{k}: missing required key")
                continue
            errors.extend(_walk(raw[k], spec, path=f"{path}.{k}"))
        return errors

    # Callable validator — returns list of error strings (empty = ok).
    # v24 hardening (T3 review I-1 + I-2) + ISS-029 (Cycle 4):
    # - None return → CONTRACT VIOLATION (pre-Cycle-4 treated as success;
    #   that defensive coercion let a forgotten `return [...]` silent-pass
    #   the validator. Now strict: validators MUST always return list[str].)
    # - str return → contract violation (raw `list("foo")` would have
    #   silently char-split into per-character errors).
    # - List of error strings gets path-prefixed so deeply-nested callable
    #   errors (e.g. _is_number_or_null at $.indicators.quote[0].close[5])
    #   carry the location instead of opaque "expected number or null".
    if callable(schema):
        result = schema(raw)
        if result is None:
            return [
                f"{path}: validator returned None (expected list[str]); "
                f"forgotten `return [...]`?"
            ]
        if isinstance(result, str):
            return [f"{path}: validator returned str (expected list[str]): {result!r}"]
        # ISS-033 (Loop2): enforce list[str] strictly. Pre-fix accepted any
        # iterable (tuple/dict/set), and the path-prefix loop would format
        # arbitrary types via f"{msg}". A validator that returns
        # `[ValidationResult(...)]` or `{"err": "x"}` would silently
        # produce malformed errors. Now: require list of str.
        if not isinstance(result, list):
            return [
                f"{path}: validator returned {type(result).__name__} "
                f"(expected list[str])"
            ]
        if not all(isinstance(msg, str) for msg in result):
            return [
                f"{path}: validator returned non-str entry "
                f"(expected list[str])"
            ]
        return [f"{path}: {msg}" for msg in result]

    return [f"{path}: unknown schema type {type(schema).__name__}"]


# ---------------------------------------------------------------------------
# Shape constants per adapter entry
# ---------------------------------------------------------------------------

def _is_number_or_null(v: Any) -> list[str]:
    """Accept None or finite int/float. Reject NaN/Infinity (would
    pass `isinstance(v, float)` but break downstream arithmetic and
    JSON serialization). Reject bool (subclass of int but not a
    valid numeric value for prices/multiples).

    ISS-004: pre-fix accepted `float('nan')` / `float('inf')` / `True`
    silently — a NaN price could land in PASSED envelopes.
    """
    if v is None:
        return []
    if isinstance(v, bool):
        return [f"expected number or null, got bool"]
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return [f"expected finite number or null, got {v!r}"]
        return []
    return [f"expected number or null, got {type(v).__name__}"]


def NonEmptyList(inner_spec):
    """ISS-067 (Loop5): factory for "non-empty list" shape constraint.
    DSL's plain `[spec]` is vacuous-pass on empty list; chart APIs that
    must have at least one bar (timestamp, quote, close) need explicit
    non-empty.

    Returns a callable validator that:
    - rejects non-list values
    - rejects empty list
    - validates each element against inner_spec via _walk
    """
    def _validate(v):
        if not isinstance(v, list):
            return [f"expected non-empty list, got {type(v).__name__}"]
        if not v:
            return ["expected non-empty list, got empty list"]
        errs = []
        for i, item in enumerate(v):
            errs.extend(_walk(item, inner_spec, path=f"[{i}]"))
        return errs
    return _validate


def _is_positive_int(v: Any) -> list[str]:
    """Accept positive int (>0). Reject bool (int subclass) and float.

    ISS-023: `regularMarketTime` validator. Pre-fix shape only typed it
    as plain int/Optional but didn't validate value or reject bool.
    `True <= 0` is False → True passes the producer guard, becoming
    epoch=1 → 1970-01-01T00:00:01Z written to PASSED envelope.
    """
    if isinstance(v, bool):
        return [f"expected positive int, got bool"]
    if not isinstance(v, int):
        return [f"expected positive int, got {type(v).__name__}"]
    if v <= 0:
        return [f"expected positive int, got {v}"]
    return []


# --- Financial Datasets family (13) ---

FD_PRICE_SHAPE = {
    # fetch_price_data unwraps results[0] from fetch_yahoo_quote; same shape.
    # ISS-023: regularMarketTime now uses _is_positive_int validator
    # (rejects bool, requires >0) so True/NaN/0 don't produce 1970 epoch.
    # ISS-104 (Loop8): aligned with YAHOO_CHART_SHAPE — timestamp / quote /
    # close all NonEmptyList. Pre-fix prefetched_chart route validated
    # vacuously-pass on empty list; wrapper route used NonEmptyList.
    # Route-dependent acceptance was a real divergence.
    "meta": {"symbol": str,
             "regularMarketPrice": Optional_(_is_number_or_null),
             "regularMarketTime": Optional_(_is_positive_int)},
    "timestamp": NonEmptyList(int),
    "indicators": {"quote": NonEmptyList(
        {"close": NonEmptyList(Optional_(_is_number_or_null))},
    )},
}

FD_METRICS_SHAPE = {
    "financial_metrics": [
        {
            "ticker": str,
            "period": str,
            # Numeric fields on period rows are nullable upstream.
            "price_to_earnings_ratio": Optional_(_is_number_or_null),
        },
    ],
}

FD_FINANCIALS_SHAPE = {
    "income_statements": [
        {
            "report_period": str,
            "revenue": Optional_(_is_number_or_null),
            "net_income": Optional_(_is_number_or_null),
        },
    ],
    "balance_sheets": [{"report_period": str}],
    "cash_flows": [{"report_period": str}],
}

# v24 correction: `country_iso` is phantom — the real `fetch_company_data`
# reads `facts.get("country", ...)` (financial_datasets.py:197). Even as
# Optional_, the wrong key name rots downstream consumer expectations.
FD_COMPANY_SHAPE = {
    "company_facts": {
        "ticker": str,
        "name": Optional_(str),
        "country": Optional_(str),
        # ISS-174 (Loop22 cycle 1 fresh-session-9): fetch_company_data
        # dereferences `category.upper()` and `exchange.upper()` (lines
        # ~755-767). Pre-fix shape only validated 3 fields, so a drifted
        # `category: ["ADR"]` (list) raised AttributeError →
        # INTERNAL_ERROR instead of SHAPE_MISMATCH. Add Optional_(str)
        # for every field the producer either dereferences as a string
        # OR emits into envelope.data as-is.
        "category": Optional_(str),
        "exchange": Optional_(str),
        "city": Optional_(str),
        "state": Optional_(str),
        "sector": Optional_(str),
        "industry": Optional_(str),
        "website_url": Optional_(str),
        "description": Optional_(str),
        "sic_code": Optional_(str),
        "sic_description": Optional_(str),
        # ISS-194 (Loop27 cycle 1 fresh-session-14): is_adr REMOVED.
        # The adapter computes is_adr from `category` ("ADR" substring
        # check at financial_datasets.py:816) — it never reads
        # `facts.is_adr`. Pre-fix Optional_(_is_number_or_null) here
        # rejected bool, so any provider that started emitting
        # `is_adr: true` (legitimate boolean) would fail the whole
        # company adapter as SHAPE_MISMATCH. Remove unused field
        # validation; the adapter's is_adr is derived, not consumed.
        "company_type": Optional_(str),
        # ISS-220 Loop37 Arch-3: cik written into PASSED data
        # (financial_datasets.py:774) but absent from shape. Optional_
        # because some providers omit it; type-check rejects list/dict
        # drift that would slip through JSON-safety only.
        "cik": Optional_(str),
    },
}

# v24 correction: removed per-item `"ticker": str`. Real items from
# `response.get("news", [])` carry only `title/url/source/date/sentiment/
# summary/text`; `fetch_news_data` (financial_datasets.py:345-356) does
# NOT dereference `ticker` per item. Original shape would SHAPE_MISMATCH
# every valid news response.
FD_NEWS_SHAPE = {
    # ISS-175 (Loop22 cycle 1 fresh-session-9): fetch_news_data emits
    # url/source/sentiment/summary directly from each item. Pre-fix
    # shape only validated title+date, so `url: 123` (int) and
    # `summary: {...}` (dict) and `source: ["a","b"]` (list) all
    # passed PASSED with malformed values in articles. Validate every
    # emitted field; nullable upstream → Optional_(str).
    "news": [{
        "title": str,
        "date": str,
        "url": Optional_(str),
        "source": Optional_(str),
        "summary": Optional_(str),
        "sentiment": Optional_(str),
        # ISS-182 (Loop24 cycle 1 fresh-session-11): fetch_news_data
        # falls back to `text[:500]` when summary is empty (financial
        # _datasets.py L1114). Non-string `text` would TypeError on
        # slice. Add Optional_(str) — completes the news shape.
        "text": Optional_(str),
    }],
}

# 2026-06 endpoint migration: FD retired `/financials/segmented-revenues`
# (now HTTP 404) and moved the data to `/financials/segments`, renaming the
# top-level response key `segmented_revenues` → `segmented_financials`. The
# per-period rows STILL carry `ticker` + `report_period` at top level (the
# numeric breakdowns moved into a nested `income_statement.<stmt>.<dim>[]`
# block), so this loose per-item shape is unchanged across the migration.
FD_SEGMENTED_SHAPE = {
    "segmented_financials": [{"ticker": str, "report_period": str}],
}

FD_INSIDER_SHAPE = {
    "insider_trades": [{"ticker": str, "filing_date": str}],
}

# v20 correction (Codex round 15 H2): FD_ANALYST_SHAPE previously
# said `consensus_estimates`/`revisions` — but real API response at
# `fetch_analyst_estimates` (financial_datasets.py:468) reads
# `response.get("analyst_estimates", [])`. validate_api_shape would
# have false-rejected every valid response as SHAPE_MISMATCH.
# v24 correction: removed per-item `"ticker": str`. Real analyst items
# (per 06_analyst_estimates.json fixture) carry only
# `earnings_per_share/fiscal_period/period/revenue`; no `ticker` at the
# item level. Original v20 shape still SHAPE_MISMATCHed every response.
FD_ANALYST_SHAPE = {
    "analyst_estimates": [
        {
            "period": Optional_(str),
            # ISS-220 Loop39 Logic-1: fiscal_period is also emitted in
            # PASSED data (financial_datasets.py:fetch_analyst_estimates).
            # Pre-fix only `period` was shape-validated; a drifted
            # `fiscal_period: ["Q1", "Q2"]` (list) or `{"q": 1}` (dict)
            # passed JSON-safety and persisted in PASSED envelope.
            "fiscal_period": Optional_(str),
        },
    ],
}

# v20 correction (Codex round 15 H2): FD_EARNINGS_SHAPE previously
# said `earnings_calendar` — but real API at `fetch_earnings_snapshot`
# reads `response.get("earnings")`.
# v20+Task13 correction (2026-05 shape regression): API silently changed
# from returning a single dict to returning a list of row dicts (each row
# carries `currency`, `actual_eps`, etc.). `fetch_earnings_snapshot` now
# normalises this by extracting rows[0] and emitting it as a dict for
# backward compat with all downstream dict-handling consumers.
FD_EARNINGS_SHAPE = {
    "earnings": Optional_(list),
}

# v24 correction: removed per-item `"ticker": str` (same pattern as
# FD_NEWS_SHAPE — unverified per-item ticker presence, defensive drop).
FD_PRESS_SHAPE = {
    # ISS-179 (Loop23 cycle 1 fresh-session-10): fetch_earnings_press_
    # releases emits the whole row unchanged. Pre-fix shape only validated
    # title+date, so url/source/summary type drift slipped through into
    # PASSED envelope. Mirrors ISS-175 (Loop22) FD_NEWS_SHAPE fix.
    "press_releases": [{
        "title": str,
        "date": str,
        "url": Optional_(str),
        "source": Optional_(str),
        "summary": Optional_(str),
    }],
}

# v24 correction: removed per-item `"ticker": str`. Real holdings items
# (per 08_institutional.json fixture) carry only
# `investor/market_value/price/report_period/security_type/shares`;
# no `ticker` at the item level. Original shape would SHAPE_MISMATCH.
FD_INST_SHAPE = {
    # ISS-181 (Loop24 cycle 1 fresh-session-11): fetch_institutional_
    # ownership emits the whole row via _emit_with_numeric_coerce —
    # numeric fields (shares/market_value/price) are coerced, but
    # str fields (investor/security_type) were unvalidated. A
    # `{"investor": ["bad"]}` drift slipped through PASSED with the
    # list-valued investor in holdings. Validate emitted str fields.
    "institutional_ownership": [{
        "report_period": str,
        "investor": Optional_(str),
        "security_type": Optional_(str),
    }],
}

# Shape validates the RAW upstream response pre-transform (per v24
# module docstring convention). scripts/sources/financial_datasets.py:566
# does `rates = response.get("interest_rates", [])` so the raw top-level
# key is `interest_rates` (the `rates` key is the POST-transform name
# used in the adapter's return dict, which consumers see at
# macro.py:108).
# v2 correction (Codex Cycle 1 Phase 1 H3): the pre-v2 inner shape
# `{"federal_funds_rate": ..., ...}` did not match — corrected to the
# list-of-banks form.
# v24 correction: outer key changed from `rates` (post-transform) to
# `interest_rates` (raw) to align with the single-call pre-transform
# validation convention. Sibling FD_RATES_HIST_SHAPE already used
# `interest_rates` — this closes the inconsistency.
FD_RATES_SNAPSHOT_SHAPE = {
    "interest_rates": [
        {
            "bank": str,
            "rate": Optional_(_is_number_or_null),
            "rate_pct": Optional_(_is_number_or_null),
            "as_of_date": Optional_(str),
        },
    ],
}

FD_RATES_HIST_SHAPE = {
    # Historical endpoint shape per financial_datasets.py:578
    # (`fetch_interest_rates_historical`). List-of-observations form.
    "interest_rates": [
        {
            "date": str,
            "rate_pct": Optional_(_is_number_or_null),
        },
    ],
}


# --- Yahoo Finance (v6.1 correction — unwrapped shape post results[0]) ---

YAHOO_CHART_SHAPE = {
    # fetch_yahoo_quote returns results[0] at yahoo_finance.py:197 —
    # the raw {"chart": {"result": [...]}} envelope is stripped before
    # the validator runs. Shape describes the inner form only.
    # ISS-023: regularMarketTime — see FD_PRICE_SHAPE comment.
    # ISS-067 (Loop5): timestamp / indicators.quote / quote[0].close MUST
    # be non-empty — caller (fetch_yahoo_quote_result wrapper, then
    # macro.py / financial_datasets.fetch_price_data) reads quote[0] and
    # close[-1] / open[-1] / close[-2] which IndexError on empty.
    "meta": {"symbol": str,
             "regularMarketPrice": Optional_(_is_number_or_null),
             "regularMarketTime": Optional_(_is_positive_int)},
    "timestamp": NonEmptyList(int),
    "indicators": {"quote": NonEmptyList(
        {"close": NonEmptyList(Optional_(_is_number_or_null))},
    )},
}


def YAHOO_HISTORICAL_OHLCV_SHAPE(chart: Any) -> list[str]:
    """ISS-142 (Loop14 cycle 1 fresh-session): stricter shape for
    `fetch_historical_prices` (yahoo_finance.py:329) which builds OHLCV
    bars by ``zip(timestamps, opens, highs, lows, closes, volumes)``.
    Plain `zip()` silently truncates mismatched arrays — pre-fix a 3
    timestamp / 1 close response returned PASSED with daily_count=1,
    and a string close like ``"bad"`` was persisted verbatim.

    YAHOO_CHART_SHAPE only enforces non-empty timestamp + non-empty
    close (because macro / fetch_price_data consumers don't always
    need full OHLCV). This validator is the OHLCV-strict variant for
    consumers that DO build bars: requires open/high/low/close/volume
    arrays present, parallel-length to timestamp, and numeric (not
    bool / not string) when not None.

    Returns: error string list (empty = ok). Compatible with
    `validate_api_shape(chart, YAHOO_HISTORICAL_OHLCV_SHAPE)` —
    the DSL accepts callables as schema.
    """
    if not isinstance(chart, dict):
        return [f"$: expected dict, got {type(chart).__name__}"]
    timestamps = chart.get("timestamp")
    if not isinstance(timestamps, list) or not timestamps:
        return ["$.timestamp: expected non-empty list"]
    # ISS-151 (Loop16 cycle 1 fresh-session-3): validate each timestamp
    # is a positive int (reject bool / str / float / non-positive). The
    # parser below calls `datetime.fromtimestamp(ts, ...)` (yahoo_finance
    # .py:416 daily, :478 weekly), which raises TypeError on str/bool
    # and OSError on out-of-range. Catching at the shape boundary
    # surfaces SHAPE_MISMATCH instead of the parse path's PARSE_ERROR.
    for i, ts in enumerate(timestamps):
        # bool is int subclass — reject explicitly so True/False epochs
        # don't slip through (mirrors regularMarketTime _is_positive_int).
        if isinstance(ts, bool):
            return [f"$.timestamp[{i}]: expected positive int, got bool"]
        if not isinstance(ts, int):
            return [
                f"$.timestamp[{i}]: expected positive int, "
                f"got {type(ts).__name__}"
            ]
        if ts <= 0:
            return [f"$.timestamp[{i}]: expected positive int, got {ts}"]
    n = len(timestamps)
    indicators = chart.get("indicators")
    if not isinstance(indicators, dict):
        return ["$.indicators: expected dict"]
    quote_list = indicators.get("quote")
    if not isinstance(quote_list, list) or not quote_list:
        return ["$.indicators.quote: expected non-empty list"]
    quote = quote_list[0]
    if not isinstance(quote, dict):
        return ["$.indicators.quote[0]: expected dict"]
    # Late import — avoid circular dep with common.py at module top.
    from scripts.sources.common import is_bool_like
    import math as _math
    errs: list[str] = []
    for field in ("open", "high", "low", "close", "volume"):
        arr = quote.get(field)
        if arr is None:
            errs.append(f"$.indicators.quote[0].{field}: missing array")
            continue
        if not isinstance(arr, list):
            errs.append(
                f"$.indicators.quote[0].{field}: expected list, "
                f"got {type(arr).__name__}"
            )
            continue
        if len(arr) != n:
            errs.append(
                f"$.indicators.quote[0].{field}: length {len(arr)} "
                f"!= timestamp length {n} (zip() would silently truncate)"
            )
            continue
        for i, v in enumerate(arr):
            if v is None:
                continue  # Yahoo emits None for incomplete bars
            if is_bool_like(v):
                errs.append(
                    f"$.indicators.quote[0].{field}[{i}]: expected "
                    f"numeric, got bool (True/False or numpy.bool_)"
                )
                break  # one error per field is enough
            if not isinstance(v, (int, float)):
                errs.append(
                    f"$.indicators.quote[0].{field}[{i}]: expected "
                    f"numeric, got {type(v).__name__}"
                )
                break
            # ISS-155 (Loop17 cycle 1 fresh-session-4): reject NaN/+Inf/
            # -Inf at the shape boundary. Pre-fix, isinstance(NaN, float)
            # is True so the loop accepted the bar, then fetch_historical
            # _prices counted it as a daily/weekly bar, then the post-emit
            # _sanitize_dict_numerics erased the close to None — leaving
            # a counted bar with close=None. PASSED envelope with phantom
            # bar count is worse than SHAPE_MISMATCH at boundary.
            if not _math.isfinite(v):
                errs.append(
                    f"$.indicators.quote[0].{field}[{i}]: expected "
                    f"finite numeric, got {v!r}"
                )
                break
    # ISS-157 (Loop18 cycle 1 fresh-session-5): adjclose array is
    # OPTIONAL (Yahoo emits it for split-adjusted prices), but IF
    # present, must follow the same parallel-length + finite-numeric
    # contract as the OHLCV arrays. Pre-fix `adj_closes[idx]` was
    # consumed at yahoo_finance.py:410/472 with no shape validation,
    # so a drifted `adjclose: ["bad"]` produced PASSED with the
    # literal "bad" persisted into `result["daily"][N]["adjclose"]`.
    adjclose_block = indicators.get("adjclose")
    if adjclose_block is not None:
        if not isinstance(adjclose_block, list) or not adjclose_block:
            errs.append("$.indicators.adjclose: expected non-empty list when present")
        else:
            adj0 = adjclose_block[0]
            if not isinstance(adj0, dict):
                errs.append("$.indicators.adjclose[0]: expected dict")
            else:
                adjarr = adj0.get("adjclose")
                if adjarr is None:
                    # Tolerate missing inner adjclose key — treat as
                    # "no adjclose data this response" (legitimate path).
                    pass
                elif not isinstance(adjarr, list):
                    errs.append(
                        f"$.indicators.adjclose[0].adjclose: expected "
                        f"list, got {type(adjarr).__name__}"
                    )
                elif len(adjarr) != n:
                    errs.append(
                        f"$.indicators.adjclose[0].adjclose: length "
                        f"{len(adjarr)} != timestamp length {n}"
                    )
                else:
                    for i, v in enumerate(adjarr):
                        if v is None:
                            continue
                        if is_bool_like(v):
                            errs.append(
                                f"$.indicators.adjclose[0].adjclose[{i}]: "
                                f"expected numeric, got bool"
                            )
                            break
                        if not isinstance(v, (int, float)):
                            errs.append(
                                f"$.indicators.adjclose[0].adjclose[{i}]: "
                                f"expected numeric, got {type(v).__name__}"
                            )
                            break
                        if not _math.isfinite(v):
                            errs.append(
                                f"$.indicators.adjclose[0].adjclose[{i}]: "
                                f"expected finite numeric, got {v!r}"
                            )
                            break
    return errs


# --- SEC EDGAR ---

def _sec_submissions_recent_validator(recent: Any) -> list[str]:
    """Custom validator for SEC submissions `filings.recent` block.

    Requires accessionNumber/form/filingDate/primaryDocument to all be
    str-lists of equal length — sec_edgar.py:825-837 dereferences them
    via parallel index access (`accessions[i]`, `primary_docs[i]`).
    Validating length parity here surfaces upstream drift as
    SHAPE_MISMATCH at the boundary instead of letting an IndexError
    inside the consumer become INTERNAL_ERROR.

    ISS-018 fix: pre-fix shape declared accessionNumber/form/filingDate
    only (missing `primaryDocument`) and didn't enforce parallel length.
    """
    if not isinstance(recent, dict):
        return [f"expected dict, got {type(recent).__name__}"]
    required_lists = ("accessionNumber", "form", "filingDate", "primaryDocument")
    errs: list[str] = []
    lengths = {}
    for key in required_lists:
        if key not in recent:
            errs.append(f"missing required key: {key}")
            continue
        val = recent[key]
        if not isinstance(val, list):
            errs.append(f"{key}: expected list, got {type(val).__name__}")
            continue
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errs.append(f"{key}[{i}]: expected str, got {type(item).__name__}")
                break
        lengths[key] = len(val)
    if not errs and len(set(lengths.values())) > 1:
        errs.append(f"parallel-array length mismatch: {lengths}")
    # ISS-160 (Loop18 cycle 1 fresh-session-5): reportDate is OPTIONAL
    # in the SEC submissions shape (some filings legitimately lack it,
    # consumer at sec_edgar.py:1028-1031 already handles missing-via-
    # length-check + missing_report_date sentinel). But IF present, it
    # MUST be list[str] — pre-fix a drifted upstream `"reportDate":
    # "2024-09-28"` (single string instead of list) was indexed by
    # `report_dates[i]` returning the i-th CHARACTER ("2"), then
    # treated as a valid 1-char report date and emitted in PASSED
    # envelope. Validate type if present; tolerate missing entirely.
    # ISS-208 (Loop30 cycle 1 fresh-session-17): pull date validation
    # into a shared helper so reportDate and filingDate stay symmetric.
    # Pre-fix only reportDate was format-validated (ISS-188); filingDate
    # was list/str-checked but not format-checked, so a malformed
    # filingDate slipped past the validator and entered PASSED data.
    # ISS-220 SF-C (Loop32 cycle 2): use module-level
    # `_is_valid_yyyy_mm_dd` helper (single regex compile shared with
    # Date_yyyy_mm_dd shape primitive + fmp adapter).
    def _check_date_array(field_name: str) -> None:
        if field_name not in recent:
            return
        val = recent[field_name]
        if not isinstance(val, list):
            errs.append(
                f"{field_name}: expected list[str] when present, "
                f"got {type(val).__name__}"
            )
            return
        for i, item in enumerate(val):
            # Empty-string / None entries are tolerated (consumer
            # treats those as missing for that filing).
            if item is None or item == "":
                continue
            if not isinstance(item, str):
                errs.append(
                    f"{field_name}[{i}]: expected str / None / '', "
                    f"got {type(item).__name__}"
                )
                break
            if not _is_valid_yyyy_mm_dd(item):
                errs.append(
                    f"{field_name}[{i}]: expected YYYY-MM-DD, "
                    f"got {item!r}"
                )
                break

    _check_date_array("reportDate")
    _check_date_array("filingDate")
    return errs


SEC_SUBMISSIONS_SHAPE = {
    "cik": str,
    "name": str,
    "filings": {"recent": _sec_submissions_recent_validator},
}

# v3 correction (Codex Cycle 1 Phase 4 fresh-challenge MED-6):
# SEC `company_tickers.json` top-level shape is
# `{"0": {"ticker": "AAPL", "cik_str": 320193, "title": "..."},
#  "1": {...}, ...}` — a dict whose VALUES are the per-ticker
# leaves. Calling a leaf validator on the raw top-level dict
# reports "missing required keys" for every call (the required
# keys exist in VALUES, not at top level). Two shape constants:
#   - `_SEC_TICKER_LEAF_SHAPE`: validator for a single per-ticker
#     entry (applied by caller to `dict.values()` iteration).
#   - `SEC_TICKER_MAP_SHAPE`: declarative outer spec that pins
#     top-level = dict whose values each match the leaf shape.
# The adapter at sec_edgar.py iterates `raw.values()` and passes
# each value through `validate_api_shape(v, _SEC_TICKER_LEAF_SHAPE)`.


def _SEC_TICKER_LEAF_SHAPE(v: Any) -> list[str]:
    """Leaf validator for a single ticker entry.

    ISS-147 (Loop15 cycle 1 fresh-session-2): pre-fix only checked key
    existence. `{"ticker": 123, "cik_str": ["bad"], "title": None}`
    passed shape validation; downstream `_resolve_cik` then crashed at
    `entry.get("ticker", "").upper()` (AttributeError on int) or
    `str(entry["cik_str"]).zfill(10)` produced "['bad']" — INTERNAL_ERROR
    instead of SHAPE_MISMATCH. Now type-validate the 3 required fields:
      - ticker: str, non-empty after strip
      - cik_str: int, OR str of digits (SEC emits int but 5+-year-old
        company_tickers responses sometimes used str; tolerate both)
      - title: str, non-empty after strip
    """
    if not isinstance(v, dict):
        return [f"expected dict, got {type(v).__name__}"]
    errs: list[str] = []
    # ticker
    if "ticker" not in v:
        errs.append("missing required key: ticker")
    else:
        t = v["ticker"]
        if not isinstance(t, str):
            errs.append(f"ticker: expected str, got {type(t).__name__}")
        elif not t.strip():
            errs.append("ticker: expected non-empty string")
    # cik_str
    if "cik_str" not in v:
        errs.append("missing required key: cik_str")
    else:
        c = v["cik_str"]
        if isinstance(c, bool):
            errs.append("cik_str: expected int or digit-str, got bool")
        elif isinstance(c, int):
            if c < 0:
                errs.append(f"cik_str: expected non-negative int, got {c}")
        elif isinstance(c, str):
            if not c.strip().isdigit():
                errs.append(
                    f"cik_str: expected digit-only string, got {c!r}"
                )
        else:
            errs.append(
                f"cik_str: expected int or digit-str, got {type(c).__name__}"
            )
    # title
    if "title" not in v:
        errs.append("missing required key: title")
    else:
        ti = v["title"]
        if not isinstance(ti, str):
            errs.append(f"title: expected str, got {type(ti).__name__}")
        elif not ti.strip():
            errs.append("title: expected non-empty string")
    return errs


def SEC_TICKER_MAP_SHAPE(raw: Any) -> list[str]:
    """Top-level validator: raw must be a dict whose every value
    satisfies the leaf shape. Key format (SEC emits string indices
    '0','1',...) is not constrained — the adapter cares only about
    the values."""
    if not isinstance(raw, dict):
        return [f"expected top-level dict, got {type(raw).__name__}"]
    errs: list[str] = []
    for k, v in raw.items():
        leaf_errs = _SEC_TICKER_LEAF_SHAPE(v)
        errs.extend(f"value[{k!r}]: {e}" for e in leaf_errs)
    return errs


# --- FMP ---

# --- FD `/filings/items` (consumed by sec_edgar.fetch_filing_items_from_api) ---

# ISS-084 (Loop6 backlog): consumer reads `api_item.get("number")` /
# `api_item.get("text")` / `api_item.get("name")` and dereferences each.
# Without shape validation, `{"items": null}` or `{"items": [non-dict]}`
# crashed at the .get() call → INTERNAL_ERROR. Now: enforce dict + list
# of dict items at the boundary.
FD_FILINGS_ITEMS_SHAPE = {
    "items": [
        {
            "number": Optional_(str),
            "text": Optional_(str),
            "name": Optional_(str),
        },
    ],
}


# ---------------------------------------------------------------------------
# FMP financial-data fallback shapes (2026-05-29 dual-API integration)
#
# FMP statement / estimate / earnings endpoints return a TOP-LEVEL list of
# row dicts (no wrapper key), like FMP_FILING_LIST_SHAPE. These shapes are
# deliberately minimal: their job is to reject the non-list error payload
# (`{"Error Message": "..."}`) and list-of-non-dict drift at the adapter
# boundary. Per-field numeric drift is handled downstream by
# `emit_with_numeric_coerce`, and missing optional fields are read via
# `.get()` in the converters, so only the keys the converter REQUIRES to
# build report_period / fiscal_period are validated as `str`.
# ---------------------------------------------------------------------------
FMP_STATEMENT_SHAPE = [
    {
        # `date` → report_period; `period` ("Q1".."Q4") + `calendarYear` →
        # fiscal_period. `date` is the load-bearing field; `period` is
        # Optional_ because the quarterly filter skips non-Q rows defensively.
        "date": str,
        "period": Optional_(str),
    },
]

FMP_ANALYST_EST_SHAPE = [
    {
        "date": str,
    },
]

FMP_EARN_SURPRISE_SHAPE = [
    {
        "date": str,
    },
]


FMP_FILING_LIST_SHAPE = [
    {
        "symbol": str,
        "type": str,
        "fillingDate": Optional_(str),
        "finalLink": Optional_(str),
        # ISS-178 (Loop23 cycle 1 fresh-session-10): `link` is consumed
        # by convert_fmp_to_filing_metadata via re.search and by
        # _fetch_filing_date_impl via .replace(); pre-fix shape didn't
        # validate it, so a non-string `link` (e.g. None / int / list)
        # passed PASSED then crashed downstream as INTERNAL_ERROR.
        # Optional_(str) tolerates absence + None (consumers already
        # `or ""` per ISS-159/162) but rejects type drift at the
        # boundary where it should be SHAPE_MISMATCH.
        "link": Optional_(str),
    },
]


__all__ = [
    "Optional_",
    "NonEmptyList",
    "ValidationResult",
    "ShapeError",
    "validate_api_shape",
    "FD_PRICE_SHAPE", "FD_METRICS_SHAPE", "FD_FINANCIALS_SHAPE",
    "FD_COMPANY_SHAPE", "FD_NEWS_SHAPE", "FD_SEGMENTED_SHAPE",
    "FD_INSIDER_SHAPE", "FD_ANALYST_SHAPE", "FD_EARNINGS_SHAPE",
    "FD_PRESS_SHAPE", "FD_INST_SHAPE",
    "FD_RATES_SNAPSHOT_SHAPE", "FD_RATES_HIST_SHAPE",
    "YAHOO_CHART_SHAPE",
    "YAHOO_HISTORICAL_OHLCV_SHAPE",
    "SEC_SUBMISSIONS_SHAPE", "SEC_TICKER_MAP_SHAPE",
    "FMP_FILING_LIST_SHAPE",
    "FMP_STATEMENT_SHAPE", "FMP_ANALYST_EST_SHAPE", "FMP_EARN_SURPRISE_SHAPE",
    "FD_FILINGS_ITEMS_SHAPE",
]
