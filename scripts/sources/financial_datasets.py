"""Financial Datasets API adapter.

Provides fetch functions for all Financial Datasets API endpoints used by
comprehensive_fetch.py. Each function delegates HTTP via
``sources.common.make_request``.

Constants are imported from ``scripts.constants``.
"""

import json
import os
import ssl
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Tuple

from scripts.constants import BASE_URL, YAHOO_BASE_URL
from .common import (
    is_bool_like, is_us_country, make_request, safe_urlopen,
    sanitize_dict_numerics,
    safe_num, coerce_known_numeric_fields, emit_with_numeric_coerce,
    normalize_currency, HttpStatusError,
)
from scripts.sources.adapter_result import (
    AdapterResult,
    AdapterError,
    ErrorCode,
    adapter_error_from_exception,
)
from scripts.sources.api_shapes import (
    validate_api_shape,
    FD_PRICE_SHAPE,
    FD_METRICS_SHAPE,
    FD_FINANCIALS_SHAPE,
    FD_COMPANY_SHAPE,
    FD_NEWS_SHAPE,
    FD_SEGMENTED_SHAPE,
    FD_INSIDER_SHAPE,
    FD_ANALYST_SHAPE,
    FD_EARNINGS_SHAPE,
    FD_PRESS_SHAPE,
    FD_INST_SHAPE,
    FD_RATES_SNAPSHOT_SHAPE,
    FD_RATES_HIST_SHAPE,
)


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------

def _make_request(url: str) -> Dict:
    """Delegate to sources.common.make_request."""
    return make_request(url)


# ---------------------------------------------------------------------------
# Numeric finite/bool guard (module-level, used across emit sites)
# ---------------------------------------------------------------------------
# ISS-075/085 (Loop3/6): coerce non-finite (NaN/Inf), bool, non-numeric
# upstream values to None before they land in PASSED envelope.data.
# Used by price (price/market_cap/week_52_*/volume/day_*),
# company (market_cap), analyst (EPS/revenue), earnings, institutional
# (shares/price/market_value).

# ISS-220 SF-D (Loop33 cycle 1): _safe_num promoted to common.safe_num.
# Imported at module top.


# ISS-158 (Loop18 cycle 1 fresh-session-5): known numeric fields per
# response category. _sanitize_dict_numerics is generic and leaves
# strings unchanged; pre-fix `metrics["market_cap"] = "bad"` from
# upstream drift slipped through PASSED. This per-category allowlist
# lets fetch_metrics_data and fetch_analyst_estimates coerce non-
# numeric values at known numeric fields to None before emit.
_METRICS_NUMERIC_FIELDS = frozenset({
    "price_to_earnings_ratio", "price_to_book_ratio",
    "price_to_sales_ratio", "enterprise_value_to_ebitda_ratio",
    "enterprise_value_to_revenue_ratio", "enterprise_value",
    "market_cap", "free_cash_flow", "free_cash_flow_per_share",
    "free_cash_flow_yield", "earnings_per_share", "book_value_per_share",
    "current_ratio", "quick_ratio", "debt_to_equity", "gross_margin",
    "operating_margin", "net_margin", "return_on_equity",
    "return_on_assets", "revenue_growth", "earnings_growth",
    "peg_ratio", "payout_ratio",
})

_ANALYST_NUMERIC_FIELDS = frozenset({
    "earnings_per_share", "revenue", "ebitda", "ebit",
    "operating_income", "net_income", "free_cash_flow",
    "earnings_per_share_high", "earnings_per_share_low",
    "earnings_per_share_mean", "earnings_per_share_median",
    "revenue_high", "revenue_low", "revenue_mean", "revenue_median",
})

# ISS-163 (Loop19 cycle 1 fresh-session-6): institutional holding rows
# carry numeric fields (shares / market_value / price) that the existing
# `_sanitize_dict_numerics` only protects against NaN/Inf/bool. Pre-fix
# a drifted upstream `"shares": "not-a-number"` survived as a string in
# the PASSED envelope.
_INSTITUTIONAL_NUMERIC_FIELDS = frozenset({
    "shares", "market_value", "price",
})

# ISS-184 (Loop25 cycle 1 fresh-session-12): insider trades carry
# numeric fields that _sanitize_dict_numerics doesn't coerce strings on.
# `transaction_shares: "bad"` and `transaction_price: "bad"` slipped
# through PASSED pre-fix.
_INSIDER_NUMERIC_FIELDS = frozenset({
    "transaction_shares", "transaction_price", "transaction_value",
    "shares_owned_before_transaction", "shares_owned_after_transaction",
})

# ISS-185 (Loop25 cycle 1 fresh-session-12): segmented revenue rows
# have a `revenue` field per segment; non-numeric drift slipped through.
_SEGMENTED_NUMERIC_FIELDS = frozenset({
    "revenue", "amount", "value",
})

# Codex review 2026-06: only these `income_statement.revenue.<dim>` breakdowns
# are emitted into the `segmented_revenues` artifact — they are the axes the
# score-fundamental agent consumes (product / geography / business segment).
# An unknown dimension (e.g. a "total" rollup) is dropped so it cannot make
# the adapter return a hollow PASSED with no consumable revenue mix (which
# would suppress fetch.py's filing-notes fallback).
_SEGMENTED_REVENUE_DIMENSIONS = frozenset({"product", "geography", "segment"})

# ISS-166 (Loop20 cycle 1 fresh-session-7): earnings snapshot returns
# actual/estimate/surprise numerics. Pre-fix _sanitize_dict_numerics
# left strings unchanged; `{"earnings": {"actual_eps": "not-a-number"}}`
# slipped through PASSED.
_EARNINGS_NUMERIC_FIELDS = frozenset({
    "actual_eps", "estimated_eps", "surprise_eps", "surprise_pct",
    "actual_revenue", "estimated_revenue", "surprise_revenue",
    "eps_actual", "eps_estimated", "eps_surprise",
    "revenue_actual", "revenue_estimated", "revenue_surprise",
})

# ISS-168 (Loop21 cycle 1 fresh-session-8): financial statement rows
# (income / balance / cash flow) carry many numeric fields beyond the
# 2 explicitly listed in FD_FINANCIALS_SHAPE. Pre-fix `total_assets:
# "bad"` and `net_cash_flow_from_operations: "bad"` slipped through
# PASSED. Per-row coercion at emit boundary.
_FINANCIALS_NUMERIC_FIELDS = frozenset({
    # income statement
    "revenue", "cost_of_revenue", "gross_profit",
    "operating_expense", "selling_general_and_administrative_expenses",
    "research_and_development", "operating_income", "interest_expense",
    "ebit", "ebitda", "income_tax_expense", "net_income",
    # net_income_common_stock / consolidated_income are emitted by BOTH the
    # FDS income path and the FMP fallback converter; without them here a
    # drifted string in either slot would survive while sibling `net_income`
    # (same source) coerces — an internal inconsistency the
    # currency_consistency repair reads. (codex 2026-05-29)
    "net_income_common_stock", "consolidated_income",
    "earnings_per_share", "earnings_per_share_diluted",
    "weighted_average_shares", "weighted_average_shares_diluted",
    # balance sheet
    "total_assets", "current_assets", "cash_and_equivalents",
    "inventory", "current_investments", "trade_and_non_trade_receivables",
    "non_current_assets", "property_plant_and_equipment", "goodwill_and_intangible_assets",
    "investments", "non_current_investments", "outstanding_shares",
    "tax_assets", "total_liabilities", "current_liabilities",
    "current_debt", "trade_and_non_trade_payables", "deferred_revenue",
    "deposit_liabilities", "non_current_liabilities", "non_current_debt",
    "tax_liabilities", "shareholders_equity", "retained_earnings",
    "accumulated_other_comprehensive_income", "total_debt",
    # cash flow statement
    "net_cash_flow_from_operations", "depreciation_and_amortization",
    "share_based_compensation", "net_cash_flow_from_investing",
    "capital_expenditure", "business_acquisitions_and_disposals",
    "investment_acquisitions_and_disposals",
    "net_cash_flow_from_financing", "issuance_or_repayment_of_debt_securities",
    "issuance_or_purchase_of_equity_shares", "dividends_and_other_cash_distributions",
    "change_in_cash_and_equivalents", "effect_of_exchange_rate_changes",
})


def _normalize_financials_row_currencies(financials_data: dict) -> dict:
    """Copy-on-write: normalize per-row currency in income/balance/cashflow lists.

    DL3a §2 invariant 3 — producer-emit-boundary currency normalization
    (impl-loop2 F2 fix for ISS-019 defense-in-depth). Pre-fix, the FD
    adapter emitted per-row currency raw; only fetch.py's downstream
    `_reconcile_financials_currency` normalized before save_json. Any
    direct consumer of `fetch_financial_statements` (e.g. test harness,
    snapshot replay, new module bypassing fetch.py) received unnormalized
    currency. This helper closes the producer contract.

    Returns a new dict; original is not mutated. Rows are copied with
    `currency` replaced by `normalize_currency(row.get("currency"))`
    (which collapses lowercase / padded / unsupported ISO to None).

    fetch.py:1401 `_reconcile_financials_currency` becomes belt-and-
    suspenders for the normalization step (no-op for already-normalized
    values) and remains load-bearing for the cross-row consistency
    check (raises on disagreement).
    """
    if not isinstance(financials_data, dict):
        return financials_data
    out = dict(financials_data)
    for key in ("income_statements", "balance_sheets", "cash_flows"):
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = [
                {**row, "currency": normalize_currency(row.get("currency"))}
                if isinstance(row, dict) else row
                for row in rows
            ]
    return out


# ISS-220 SF-D (Loop33 cycle 1): _coerce_known_numeric_fields and
# _emit_with_numeric_coerce promoted to common.coerce_known_numeric_fields
# and common.emit_with_numeric_coerce respectively. Imported at module top.
# ADR (adr/detect.py) is the 8th P3 emit-boundary site that motivated the
# promotion (was passing string numerics through PASSED data via
# sanitize_dict_numerics-only).


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def fetch_price_data(
    ticker: str,
    prefetched_chart: Dict = None,
) -> AdapterResult:
    """Fetch price snapshot from Yahoo Finance v8 chart API.

    Args:
        prefetched_chart: Optional pre-fetched chart result dict (e.g. from
            a 6mo fetch_yahoo_quote call).  When provided, skips the
            redundant 5d API call.
    """
    src = "financial_datasets.fetch_price_data"
    # ISS-086 (Loop6 backlog): when no prefetched_chart, route through
    # the DL2-aware wrapper `fetch_yahoo_quote_result` so the shape
    # validation happens at the wrapper boundary (consistent with
    # macro.py and any future DL2 caller). Pre-fix called raw
    # `fetch_yahoo_quote` then re-validated locally — duplicated logic
    # that could drift from the wrapper. With prefetched_chart, still
    # validate locally since the cache may be from any source.
    from .yahoo_finance import fetch_yahoo_quote_result

    try:
        if prefetched_chart is not None:
            result = prefetched_chart
            # Local shape validation — prefetched chart could be stale
            # or from a non-DL2 path.
            v = validate_api_shape(result, FD_PRICE_SHAPE)
            if not v.ok:
                return AdapterResult.failed_from_shape(v, source=src)
        else:
            wrapped = fetch_yahoo_quote_result(
                ticker, range_param="5d", interval="1d",
            )
            if not wrapped.ok:
                # Wrapper already produced a structured envelope — re-emit
                # with this entrypoint's source preserved.
                return AdapterResult.failed(
                    code=wrapped.error.code,
                    detail=wrapped.error.detail,
                    source=src,
                    retryable=wrapped.error.retryable,
                    upstream_status=wrapped.error.upstream_status,
                    cause=wrapped.error.cause,
                    shape_errors=wrapped.error.shape_errors,
                )
            result = wrapped.data
        meta = result.get("meta", {})

        price = meta.get("regularMarketPrice")
        if price is None:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="regularMarketPrice missing",
                source=src,
            )

        # Guard epoch-zero: missing/invalid regularMarketTime → 1970 timestamp
        market_time = meta.get("regularMarketTime")
        if market_time is None or market_time <= 0:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="regularMarketTime missing or zero",
                source=src,
            )

        # ISS-052 (Loop3): empty `indicators.quote=[]` (key present but
        # zero-element list) passes the FD_PRICE_SHAPE list-of-dict check
        # (vacuous on empty list) but `quote_list[0]` would raise
        # IndexError → INTERNAL_ERROR instead of SHAPE_MISMATCH. Now: explicit
        # guard surfaces empty quote as SHAPE_MISMATCH at the producer
        # boundary.
        timestamps = result.get("timestamp", [])
        quote_list = result.get("indicators", {}).get("quote", [])
        if not quote_list:
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=(
                    "indicators.quote is empty list — chart response "
                    "shape requires at least one quote block"
                ),
                source=src,
                retryable=False,
            )
        quotes = quote_list[0] if isinstance(quote_list[0], dict) else {}
        opens = quotes.get("open", [])
        closes = quotes.get("close", [])
        # ISS-220 4.19 (Loop34 cycle 1): YAHOO_CHART_SHAPE doesn't
        # enforce list-type on `open` / `close` inside `quote[0]`,
        # so a drifted Yahoo response with `open={"foo": "bar"}`
        # would AttributeError on `opens[-1]` → mapper Row 9
        # PARSE_ERROR. Surface as SHAPE_MISMATCH instead. Use the
        # adapter-local ShapeError class (matches Yahoo / FD shape
        # boundary convention).
        if not isinstance(opens, list):
            from scripts.sources.api_shapes import ShapeError as _ShapeError
            raise _ShapeError(
                "yahoo_chart_quote",
                "indicators.quote[0].open",
                f"expected list, got {type(opens).__name__}",
            )
        if not isinstance(closes, list):
            from scripts.sources.api_shapes import ShapeError as _ShapeError
            raise _ShapeError(
                "yahoo_chart_quote",
                "indicators.quote[0].close",
                f"expected list, got {type(closes).__name__}",
            )

        day_open = opens[-1] if opens else None
        previous_close = closes[-2] if len(closes) >= 2 else None

        # §3.2 row 1 (DL3a Task 9): capture currency from Yahoo chart meta.
        # meta is already extracted above (line ~223); guard None from
        # prefetched_chart paths where the caller might pass meta={}.
        currency = normalize_currency(meta.get("currency"))

        # ISS-075 (Loop5) + ISS-085 (Loop6 backlog): _safe_num is now
        # module-level and reused by company/analyst/earnings/institutional
        # producers. See definition near top of this file.
        return AdapterResult.passed(
            data={
                "ticker": ticker,
                "price": safe_num(price),
                "time": datetime.fromtimestamp(
                    market_time, tz=timezone.utc,
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "market_cap": safe_num(meta.get("marketCap")),
                "week_52_high": safe_num(meta.get("fiftyTwoWeekHigh")),
                "week_52_low": safe_num(meta.get("fiftyTwoWeekLow")),
                "volume": safe_num(meta.get("regularMarketVolume")),
                "day_high": safe_num(meta.get("regularMarketDayHigh")),
                "day_low": safe_num(meta.get("regularMarketDayLow")),
                "day_open": safe_num(day_open),
                "previous_close": safe_num(previous_close),
                "currency": currency,
            },
            meta={"source_hint": "fd_price"},
        )
    except Exception as e:
        print(f"[ERROR] Yahoo price fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Financial metrics
# ---------------------------------------------------------------------------

def fetch_metrics_data(ticker: str) -> AdapterResult:
    """Fetch financial metrics via /financial-metrics?period=quarterly&limit=1."""
    src = "financial_datasets.fetch_metrics_data"
    try:
        # ISS-027 (Cycle 4 backlog): use urllib.parse.urlencode for ALL
        # query params so future drift in caller-supplied period/limit
        # values can't inject query semantics. Defense-in-depth even
        # though current callers pass internal constants only.
        url = f"{BASE_URL}/financial-metrics?" + urllib.parse.urlencode({
            "ticker": ticker, "period": "quarterly", "limit": 1,
        })
        response = _make_request(url)
        v = validate_api_shape(response, FD_METRICS_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        metrics_list = response.get("financial_metrics", [])
        if not metrics_list:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty financial_metrics list",
                source=src,
            )
        # ISS-105 + ISS-158: sanitize NaN/Inf/bool + coerce string
        # drift in known numeric fields. Single helper chains both
        # (post-Loop22 structural — see _emit_with_numeric_coerce).
        return AdapterResult.passed(
            data=emit_with_numeric_coerce(
                metrics_list[0], numeric_fields=_METRICS_NUMERIC_FIELDS,
            ),
            meta={"source_hint": "fd_metrics"},
        )
    except Exception as e:
        print(f"[ERROR] Metrics fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Financial statements
# ---------------------------------------------------------------------------

def fetch_financial_statements(ticker: str) -> AdapterResult:
    """Fetch income, balance sheet, and cash flow statements."""
    result = {
        "income_statements": [],
        "balance_sheets": [],
        "cash_flows": [],
    }

    # ISS-027: switched to urllib.parse.urlencode below (3 sub-fetches).
    # `safe_ticker` no longer needed separately — urlencode handles it.
    #
    # 2026-06 FDS Q4-burial regression (this fix): FDS's `period=quarterly`
    # feed now returns the *standalone fiscal Q4* row ONLY when `limit` is large
    # enough to reach it — at `limit=8` the most-recent 8 rows are the recent
    # Q1/Q2/Q3 quarters and EVERY company's fiscal Q4 is omitted (verified live
    # on 26 tickers, Dec-FYE *and* non-Dec-FYE alike: MU/AMD/AAPL/NVDA/… all
    # returned a Q3→next-year-Q1 gap at limit=8). A missing Q4 makes the trailing
    # window non-consecutive, so the canonical DL4 gate
    # (`quarter_window.aligned_quarters`) raised `non_consecutive` for 100% of
    # tickers — silently routing the ENTIRE financials category to the FMP
    # fallback (and failing outright where FMP doesn't cover the name, e.g. small
    # caps / ADRs). At `limit>=12` FDS interleaves the real Q4 rows back in
    # (revenue/net_income confirmed genuine), so the FDS-direct path passes the
    # gate again. 16 = the proven threshold (12) + one fiscal-year of margin
    # against the threshold drifting. It is ALSO the floor `historical_multiples`
    # needs: its 2Y lens caps to the most-recent 8 trailing-4Q windows
    # (historical_multiples.py:476), which requires >=11 consecutive raw quarters;
    # at the old limit=8 it could only ever form 5 windows, silently
    # under-sampling its own "~2 years" contract. So this fix INTENTIONALLY
    # changes the historical-multiple summary bands (min/median/max/data_points)
    # for new runs from a 5-window to the contract-correct 8-window 2Y sample —
    # the latest trailing TTM (extract_fcf's strict latest-4) is unaffected.
    # Same CLASS of silent FDS API drift as the /financials/segments migration
    # (commit b2a3b323). Regression-locked by
    # tests/test_sources_envelope_contract.py::
    #   test_fetch_financials_limit_captures_fiscal_q4.
    _common_params = {"ticker": ticker, "period": "quarterly", "limit": 16}

    # Per-endpoint missing-key guard — ISS-005 fix.
    # Pre-fix: `response.get("income_statements", [])` masked upstream schema
    # drift (e.g. key renamed) as a vacuously-empty list, hiding SHAPE_MISMATCH.
    # Post-fix: track schema_drift per endpoint; aggregator surfaces
    # SHAPE_MISMATCH explicitly so consumers don't conflate "data missing for
    # ticker" with "upstream schema changed".
    # Per-endpoint outcomes:
    #   schema_drift=True  → upstream response was non-dict OR missing the
    #                       expected key. Surface as SHAPE_MISMATCH.
    #   key present, value present (list/None) → assign verbatim; aggregator
    #                       validate_api_shape catches None or non-list.
    schema_drift = {"income": False, "balance": False, "cashflow": False}
    # ISS-041 (Loop2 backlog): track per-endpoint transport/HTTP exception
    # so the aggregator can surface the highest-severity real cause
    # (HTTP_TRANSPORT / RATE_LIMIT / etc) instead of conflating to NOT_FOUND.
    transport_errors: list[Exception] = []

    # Income statements
    try:
        url = (
            f"{BASE_URL}/financials/income-statements?"
            + urllib.parse.urlencode(_common_params)
        )
        response = _make_request(url)
        if not isinstance(response, dict):
            print(f"[ERROR] Income response not dict: {type(response).__name__}", file=sys.stderr)
            schema_drift["income"] = True
        elif "income_statements" not in response:
            print(f"[ERROR] Income response missing 'income_statements' key (schema drift): keys={list(response.keys())[:5]}", file=sys.stderr)
            schema_drift["income"] = True
        else:
            # Drop `or []` — let aggregator validate catch None / non-list as
            # SHAPE_MISMATCH instead of silently coercing to vacuous-empty.
            result["income_statements"] = response["income_statements"]
    except Exception as e:
        print(f"[ERROR] Income statement fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        transport_errors.append(e)

    # Balance sheets
    try:
        url = (
            f"{BASE_URL}/financials/balance-sheets?"
            + urllib.parse.urlencode(_common_params)
        )
        response = _make_request(url)
        if not isinstance(response, dict):
            print(f"[ERROR] Balance response not dict: {type(response).__name__}", file=sys.stderr)
            schema_drift["balance"] = True
        elif "balance_sheets" not in response:
            print(f"[ERROR] Balance response missing 'balance_sheets' key (schema drift): keys={list(response.keys())[:5]}", file=sys.stderr)
            schema_drift["balance"] = True
        else:
            result["balance_sheets"] = response["balance_sheets"]
    except Exception as e:
        print(f"[ERROR] Balance sheet fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        transport_errors.append(e)

    # Cash flow statements
    try:
        url = (
            f"{BASE_URL}/financials/cash-flow-statements?"
            + urllib.parse.urlencode(_common_params)
        )
        response = _make_request(url)
        if not isinstance(response, dict):
            print(f"[ERROR] Cash flow response not dict: {type(response).__name__}", file=sys.stderr)
            schema_drift["cashflow"] = True
        elif "cash_flow_statements" not in response:
            print(f"[ERROR] Cash flow response missing 'cash_flow_statements' key (schema drift): keys={list(response.keys())[:5]}", file=sys.stderr)
            schema_drift["cashflow"] = True
        else:
            result["cash_flows"] = response["cash_flow_statements"]
    except Exception as e:
        print(f"[ERROR] Cash flow fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        transport_errors.append(e)

    src = "financial_datasets.fetch_financial_statements"

    # ISS-041 (Loop2 backlog): if any sub-fetch raised a transport/HTTP
    # exception, surface that as the cause — pick the highest-severity
    # exception via canonical mapping. Pre-fix: stderr-log + collapse to
    # NOT_FOUND on emptiness, hiding rate-limit / 5xx / SSRF causes.
    if transport_errors:
        # ISS-050 (Loop3): normalize slot values to [] BEFORE early-return
        # via transport branch. Pre-fix, mixed scenarios (e.g. balance
        # transport-errored + income returned `None` value) early-returned
        # with `data["income_statements"] = None` still leaking through.
        # Consumer's `len(_inc)` then crashed with TypeError. The transport
        # branch is the early-return path, so normalize at its boundary
        # only — don't mask SHAPE_MISMATCH signals on the non-transport
        # path (which has its own per-slot recovery via ISS-040 fix).
        for _slot_key in ("income_statements", "balance_sheets", "cash_flows"):
            if not isinstance(result[_slot_key], list):
                result[_slot_key] = []
        # ISS-074 (Loop5): use centralized severity_of_exception so the
        # ranking matches fetch.py filing pipeline + yahoo historical.
        # Pre-fix had its own _severity that lumped 400/500 HttpStatusError
        # at the same rank as HttpTransportError, losing UPSTREAM_ERROR
        # vs HTTP_STATUS distinction.
        from scripts.sources.adapter_result import severity_of_exception
        chosen = min(transport_errors, key=severity_of_exception)
        # If at least one slot delivered data, return PARTIAL; else FAILED.
        any_data = any(result[k] for k in ("income_statements", "balance_sheets", "cash_flows"))
        envelope = adapter_error_from_exception(chosen, source=src)
        # ISS-192 (Loop27 cycle 1 fresh-session-14): validate retained
        # slots BEFORE building the PARTIAL envelope. Pre-fix the
        # transport-error branch returned `sanitized_transport_data`
        # without per-slot shape validation. A successful income
        # response missing required `report_period` plus balance/cash
        # transport errors → PARTIAL http_transport with the malformed
        # income row in data. The aggregate shape check at L696 only
        # runs in the no-transport-error path, so transport errors
        # leak unvalidated data. Now: per-slot validate each retained
        # list-valued slot; clear malformed slots to [] so PARTIAL
        # data is consumer-safe.
        from scripts.sources.api_shapes import (
            FD_FINANCIALS_SHAPE as _FD_FIN_SHAPE,
        )
        for _slot_key in ("income_statements", "balance_sheets", "cash_flows"):
            _slot_data = result.get(_slot_key)
            if not _slot_data:
                continue
            # Build a single-slot wrapper to reuse FD_FINANCIALS_SHAPE
            # row schemas without aggregating cross-slot drift.
            _slot_wrapper = {_slot_key: _slot_data}
            _slot_schema = {_slot_key: _FD_FIN_SHAPE[_slot_key]}
            _slot_v = validate_api_shape(_slot_wrapper, _slot_schema)
            if not _slot_v.ok:
                # Clear malformed slot; consumer sees [] instead of
                # silently-broken row data with missing report_period.
                result[_slot_key] = []
        # Recompute any_data after potential slot clearing.
        any_data = any(result[k] for k in ("income_statements", "balance_sheets", "cash_flows"))
        # ISS-115 (Loop8 cycle 2): sanitize before transport-PARTIAL /
        # transport-FAILED branches too. Pre-fix only the canonical
        # PASSED/PARTIAL paths ran through `_sanitize_dict_numerics`;
        # transport-error branches passed raw `result` whose
        # successfully-fetched slot data could carry NaN/Inf — and
        # ISS-106's JSON-safety guard now hard-errors at envelope
        # construction on those values. Sanitize first so the partial
        # data we DID fetch survives without crashing the envelope.
        # ISS-214 (Loop31 cycle 1 fresh-session-18): elevate to the
        # full emit boundary helper. Pre-fix this branch ran only
        # NaN/Inf/bool sanitize; the PASSED path additionally coerces
        # known numeric fields via `_emit_with_numeric_coerce` so a
        # PARTIAL envelope with `total_assets="bad"` survived where
        # PASSED would have coerced it to None — silent contract drift
        # between PASSED and PARTIAL.
        sanitized_transport_data = emit_with_numeric_coerce(
            _normalize_financials_row_currencies(result),
            numeric_fields=_FINANCIALS_NUMERIC_FIELDS,
        )
        if any_data:
            return AdapterResult.partial(
                data=sanitized_transport_data,
                error=envelope.error,
                meta={"source_hint": "fd_financials",
                      "transport_error_count": len(transport_errors)},
            )
        # All three failed (or some failed + rest empty): re-emit envelope
        # with this entrypoint's source preserved, plus residual data dict.
        return AdapterResult.failed(
            code=envelope.error.code,
            detail=envelope.error.detail,
            source=src,
            retryable=envelope.error.retryable,
            upstream_status=envelope.error.upstream_status,
            cause=envelope.error.cause,
            data=sanitized_transport_data,
            meta={"source_hint": "fd_financials",
                  "transport_error_count": len(transport_errors)},
        )

    # ISS-005 strengthen: surface schema drift as SHAPE_MISMATCH explicitly,
    # not as NOT_FOUND. If any sub-fetch saw a missing/wrong-type response,
    # the envelope reflects shape mismatch — distinguishing real drift from
    # "no data for ticker".
    drifted = [k for k, v in schema_drift.items() if v]
    if drifted:
        # ISS-096 (Loop7): per-slot normalize BEFORE early-return on drift.
        # Pre-fix scenario: income missing key (drift) + balance returns
        # `"bad"` (non-list, key present) → drift branch returns PARTIAL
        # at this point with malformed `balance_sheets="bad"` still in
        # data, since the per-slot recovery at the next `validate_api_shape`
        # block runs only on the no-drift path. Now: normalize any non-list
        # slot to [] here too so PARTIAL data is consumer-safe.
        for _slot_key in ("income_statements", "balance_sheets", "cash_flows"):
            if not isinstance(result[_slot_key], list):
                result[_slot_key] = []
        # If ALL three drifted, FAILED. If some drifted but others delivered
        # data, PARTIAL — preserves whatever data we managed to fetch.
        any_data = any(result[k] for k in ("income_statements", "balance_sheets", "cash_flows"))
        detail = f"schema drift on endpoints: {drifted}"
        # ISS-115 (Loop8 cycle 2): sanitize drift-branch data too.
        # ISS-214 (Loop31): full emit-boundary symmetry with PASSED path.
        sanitized_drift_data = emit_with_numeric_coerce(
            _normalize_financials_row_currencies(result),
            numeric_fields=_FINANCIALS_NUMERIC_FIELDS,
        )
        if any_data:
            return AdapterResult.partial(
                data=sanitized_drift_data,
                error=AdapterError(
                    code=ErrorCode.SHAPE_MISMATCH,
                    detail=detail,
                    source=src,
                    retryable=False,
                ),
                meta={"source_hint": "fd_financials"},
            )
        return AdapterResult.failed(
            code=ErrorCode.SHAPE_MISMATCH,
            detail=detail,
            source=src,
            retryable=False,
            data=sanitized_drift_data,
        )

    # Shape validation against canonical FD_FINANCIALS_SHAPE — catches
    # API drift (renamed report_period field, missing revenue/net_income,
    # null/non-list values per ISS-005 fix dropping `or []`) at the boundary.
    # Spec §Adapter migration row 3.
    #
    # ISS-040 (Loop2): when one sub-fetch's value is malformed (e.g.
    # `cash_flow_statements: None`) but the other two delivered valid data,
    # `validate_api_shape` reports SHAPE_MISMATCH for the whole aggregate —
    # `failed_from_shape` then returns FAILED with no data, throwing away
    # the income/balance lists we DID fetch successfully. Better: clear
    # the bad slot back to [] so the populated slots can still emit as
    # PARTIAL+SHAPE_MISMATCH (preserving what we have).
    v = validate_api_shape(result, FD_FINANCIALS_SHAPE)
    if not v.ok:
        # Identify which sub-fetch slot(s) caused the failure by re-validating
        # each slot in isolation. Slot keys map: income_statements/balance_sheets
        # /cash_flows. Map back to the FD_FINANCIALS_SHAPE per-list spec.
        per_slot_drift = []
        single_slot_shapes = {
            "income_statements": FD_FINANCIALS_SHAPE["income_statements"],
            "balance_sheets": FD_FINANCIALS_SHAPE["balance_sheets"],
            "cash_flows": FD_FINANCIALS_SHAPE["cash_flows"],
        }
        for slot_key, slot_shape in single_slot_shapes.items():
            slot_v = validate_api_shape(result[slot_key], slot_shape)
            if not slot_v.ok:
                per_slot_drift.append(slot_key)
                # Coerce bad slot to empty list so other slots survive
                result[slot_key] = []
        if any(result[k] for k in single_slot_shapes):
            # Some slots still have valid data → PARTIAL with SHAPE_MISMATCH
            shape_errors = tuple(v.errors)
            # ISS-115 (Loop8 cycle 2): sanitize per-slot drift data.
            # ISS-214 (Loop31): full emit-boundary symmetry with PASSED path.
            sanitized_slot_data = emit_with_numeric_coerce(
                _normalize_financials_row_currencies(result),
                numeric_fields=_FINANCIALS_NUMERIC_FIELDS,
            )
            return AdapterResult.partial(
                data=sanitized_slot_data,
                error=AdapterError(
                    code=ErrorCode.SHAPE_MISMATCH,
                    detail=(
                        f"shape drift on slots {per_slot_drift}: "
                        f"{shape_errors[0]}"
                    ),
                    source=src,
                    retryable=False,
                    shape_errors=shape_errors,
                ),
                meta={"source_hint": "fd_financials"},
            )
        return AdapterResult.failed_from_shape(v, source=src)

    # Determine status
    if (
        result["income_statements"]
        and result["balance_sheets"]
        and result["cash_flows"]
    ):
        status = "PASSED"
    elif (
        result["income_statements"]
        or result["balance_sheets"]
        or result["cash_flows"]
    ):
        status = "PARTIAL"
    else:
        status = "FAILED"

    # ISS-105/168: bool drift + numeric string drift coerced via
    # _emit_with_numeric_coerce. Helper auto-recurses one level into
    # list-valued keys, so income_statements/balance_sheets/cash_flows
    # get per-row coercion in a single call.
    sanitized_data = emit_with_numeric_coerce(
        _normalize_financials_row_currencies(result),
        numeric_fields=_FINANCIALS_NUMERIC_FIELDS,
    )
    if status == "PASSED":
        return AdapterResult.passed(
            data=sanitized_data,
            meta={"source_hint": "fd_financials"},
        )
    elif status == "PARTIAL":
        return AdapterResult.partial(
            data=sanitized_data,
            error=AdapterError(
                code=ErrorCode.NOT_FOUND,
                detail="one or more financial statement types missing",
                source=src,
                retryable=False,
            ),
            meta={"source_hint": "fd_financials"},
        )
    else:
        return AdapterResult.failed(
            code=ErrorCode.NOT_FOUND,
            detail="all financial statement fetches returned empty",
            source=src,
            data=sanitized_data,
        )


# ---------------------------------------------------------------------------
# Company data
# ---------------------------------------------------------------------------

def fetch_company_data(ticker: str) -> AdapterResult:
    """Fetch company facts with ADR/foreign company detection."""
    src = "financial_datasets.fetch_company_data"
    try:
        safe_ticker = urllib.parse.quote(ticker, safe='')
        url = f"{BASE_URL}/company/facts?ticker={safe_ticker}"
        response = _make_request(url)
        v = validate_api_shape(response, FD_COMPANY_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        facts = response.get("company_facts", {})

        if not facts:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty company_facts",
                source=src,
            )

        category = facts.get("category", "")
        country = facts.get("country", "")
        city = facts.get("city", "")
        state = facts.get("state", "")
        exchange = facts.get("exchange", "")
        sic_code = facts.get("sic_code")
        sic_description = facts.get("sic_description")

        # ADR/Foreign detection
        is_adr = "ADR" in category.upper() if category else False
        # Fail-closed: missing country is treated as unknown (not assumed US)
        is_foreign = not is_us_country(country) if country else None
        is_otc = "OTC" in exchange.upper() if exchange else False

        # Determine filing type hint
        if is_adr or is_foreign:
            filing_type_hint = "20-F"
            requires_20f = True
        elif is_foreign is None:
            # Unknown country — default to 10-K but flag uncertainty
            filing_type_hint = "10-K"
            requires_20f = False
        else:
            filing_type_hint = "10-K"
            requires_20f = False

        # Detection source tracking
        detection_source = []
        if is_adr:
            detection_source.append("category_contains_ADR")
        if is_foreign:
            detection_source.append(f"country={country}")
        elif is_foreign is None:
            detection_source.append("country_unknown")

        # ISS-220 4.23 (Loop35 cycle 1): `facts.get("city", "")` returns
        # the default ONLY when the key is absent. When the key is
        # present with value None, .get() returns None — the f-string
        # then stringifies it to literal "None" → output "None, None, US".
        # Skip empty / None / whitespace-only segments and join the rest.
        location = ", ".join(
            str(x).strip() for x in (city, state, country) if x
        )

        company_type = {
            "is_foreign": is_foreign,
            "is_adr": is_adr,
            "is_otc": is_otc,
            "home_country": country,
            "exchange": exchange,
            "filing_type_hint": filing_type_hint,
            "requires_20f": requires_20f,
            "currency_warning": False,
            "detection_source": detection_source,
        }

        # ISS-085 (Loop6 backlog): finite/bool guard on numeric emit
        # field. employees / market_cap upstream-malformed → coerce None
        # rather than land NaN/Inf/bool in PASSED envelope.
        return AdapterResult.passed(
            data={
                "name": facts.get("name"),
                "ticker": facts.get("ticker"),
                "cik": facts.get("cik"),
                "sector": facts.get("sector"),
                "industry": facts.get("industry"),
                "employees": safe_num(facts.get("employees")),
                "website_url": facts.get("website_url"),
                "description": facts.get("description"),
                "location": location,
                "exchange": exchange,
                "market_cap": safe_num(facts.get("market_cap")),
                "country": country,
                "city": city,
                "state": state,
                "sic_code": str(sic_code) if sic_code else None,
                "sic_description": sic_description,
                "category": category,
                "is_adr": is_adr,
                "company_type": company_type,
            },
            meta={"source_hint": "fd_company"},
        )
    except Exception as e:
        print(f"[ERROR] Company data fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def _fetch_news_finnhub(ticker: str, limit: int = 10) -> Tuple[list, str]:
    """Finnhub fallback for news when Financial Datasets API returns 0.

    Returns (articles, status) tuple where status is one of:
      "ok"            — Finnhub returned (possibly-empty) article list
      "no_api_key"    — FINNHUB_API_KEY missing; fallback unavailable
      "fallback_error" — Finnhub call raised an exception

    ISS-043 (Loop2 backlog): pre-fix returned bare `list`, conflating
    success-empty with no-api-key with exception-failure. Caller couldn't
    distinguish "Finnhub agrees there's no news" from "we never reached
    Finnhub". Status string lets the caller emit a meaningful envelope.
    """
    import os
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return [], "no_api_key"
    try:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        # Normalize ticker for Finnhub (strip share class dots)
        symbol = ticker.replace(".", "")
        params = urllib.parse.urlencode({
            "symbol": symbol, "from": start, "to": end, "token": api_key,
        })
        url = f"https://finnhub.io/api/v1/company-news?{params}"
        # ISS-177 (Loop23 cycle 1 fresh-session-10): switch from
        # `safe_urlopen + DEFAULT_POLICY` to `safe_http_get_json +
        # FINNHUB_POLICY`. FINNHUB_POLICY pins finnhub.io + HTTPS-only
        # so cross-origin redirects can't follow. Bonus: status check
        # + JSON parse + typed-exception propagation come for free —
        # the helper raises HttpStatusError on 4xx/5xx, which we map
        # back to the legacy status strings the caller expects.
        from scripts.sources.common import (
            FINNHUB_POLICY, safe_http_get_json, HttpStatusError,
            RetryExhaustedError,
        )
        try:
            data = safe_http_get_json(url, policy=FINNHUB_POLICY)
        except HttpStatusError as e:
            # ISS-165 contract preserved: caller at fetch_news_data
            # branches on these specific strings.
            # ISS-220 4.17 (Loop34 cycle 1): include 402 (Payment
            # Required) — Finnhub's quota-exhaustion code. Pre-fix it
            # fell through to "fallback_error" → UPSTREAM_ERROR,
            # hiding the actionable "billing/quota" cause from operators.
            # ErrorCode taxonomy treats 401/402/403 uniformly as
            # UNAUTHORIZED.
            if e.status in (401, 402, 403):
                return [], "unauthorized"
            if e.status == 429:
                return [], "rate_limited"
            return [], "fallback_error"
        except RetryExhaustedError as e:  # retry-exhausted-classification-ok: legacy string-status contract for fetch_news_data caller; out-of-AdapterResult contract by design (see CLAUDE.md adapter authoring contract §exception)
            # ISS-202 (Loop29 cycle 1 fresh-session-16): FINNHUB_POLICY
            # inherits the default retry_on set including 429, so a
            # sustained 429 wave raises RetryExhaustedError(status=429),
            # NOT HttpStatusError. Pre-fix that fell through to
            # `fallback_error`, which fetch_news_data mapped to
            # UPSTREAM_ERROR — operators saw "Finnhub fallback errored"
            # for what was actually rate limiting. Map exhausted-429
            # back to "rate_limited" so the rate-limit signal survives.
            #
            # ISS-220 SF-B (Loop32 cycle 2): _fetch_news_finnhub returns
            # `(list, str)` with legacy string-vocab statuses
            # ("rate_limited" / "unauthorized" / "fallback_error" /
            # "no_results") — NOT AdapterResult. fetch_news_data caller
            # pattern-matches on these strings (see ISS-024 / Cycle 4).
            # The hand-rolled classification stays here intentionally;
            # routing through adapter_error_from_exception would emit
            # AdapterResult (wrong return type for this private helper)
            # and break the caller's string dispatch. Pattern V audit
            # skips this site via the trailing
            # `# retry-exhausted-classification-ok` comment.
            if getattr(e, "status", None) == 429:
                return [], "rate_limited"
            return [], "fallback_error"
        # Defense-in-depth: a Finnhub 200 with non-list body (drift)
        # would TypeError at `data[:limit]`. Treat as fallback_error.
        if not isinstance(data, list):
            return [], "fallback_error"
        articles = []
        import math as _math
        for n in data[:limit]:
            # ISS-220 4.6 (Loop32 cycle 2): guard non-dict rows. Pre-fix
            # `n.get(...)` raised AttributeError on a mixed list (e.g.
            # `[{...}, "string"]`) and the outer `except Exception` jumped
            # the loop, discarding all valid articles already appended.
            # Skip malformed rows individually so partial-result
            # preservation holds.
            if not isinstance(n, dict):
                continue
            ts = n.get("datetime")
            # ISS-171 (Loop21 cycle 1 fresh-session-8): bool is int subclass —
            # `datetime.fromtimestamp(True, ...)` produces 1970-01-01T00:00:01.
            # Also reject non-finite floats and non-positive epochs.
            # Mirrors ISS-139/151 (bool / int subclass surprise) +
            # numpy-bool guard via is_bool_like.
            if (
                ts is None
                or is_bool_like(ts)
                or not isinstance(ts, (int, float))
                or (isinstance(ts, float) and not _math.isfinite(ts))
                or ts <= 0
            ):
                pub = None
            else:
                try:
                    pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except (OSError, OverflowError, ValueError):
                    pub = None
            # ISS-186 (Loop25 cycle 1 fresh-session-12): validate string
            # types per article. Pre-fix `headline: ["bad"]` (list) or
            # `url: 123` (int) slipped through PASSED then `.get(k, "")`
            # left them as-is; downstream consumers expecting str
            # crashed or rendered garbage. Coerce non-string to "" so
            # the article is dropped at the next layer (no title /
            # no url) instead of leaking through.
            def _safe_str(v):
                return v if isinstance(v, str) else ""
            articles.append({
                "title": _safe_str(n.get("headline")),
                "url": _safe_str(n.get("url")),
                "source": f"Finnhub:{_safe_str(n.get('source')) or 'Unknown'}",
                "published_at": pub,
                "sentiment": None,
                "summary": _safe_str(n.get("summary"))[:500],
            })
        return articles, "ok"
    except Exception as e:
        err = str(e)
        if api_key:
            # ISS-114 (Loop8 cycle 2): redact url-encoded variants too.
            # `urllib.parse.urlencode({"token": api_key})` quotes special
            # chars (e.g. `+/=` in base64-style keys → `%2B%2F%3D`), so
            # a 429 / 5xx exception that stringifies the URL leaks the
            # encoded form. Sibling fmp.py uses `_fmp_redact_variants` for
            # this; route Finnhub through the canonical `_scrub_detail`
            # helper which now expands raw + quote + quote_plus.
            from scripts.sources.adapter_result import _scrub_detail
            err = _scrub_detail(err, (api_key,))
        print(f"[WARN] Finnhub news fallback failed: {err}", file=sys.stderr)
        return [], "fallback_error"


def fetch_news_data(ticker: str, limit: int = 10) -> AdapterResult:
    """Fetch recent news with complete URLs. Falls back to Finnhub if primary returns 0."""
    src = "financial_datasets.fetch_news_data"
    try:
        # ISS-027: urlencode for defense-in-depth.
        url = f"{BASE_URL}/news?" + urllib.parse.urlencode({
            "ticker": ticker, "limit": limit,
        })
        try:
            response = _make_request(url)
        except HttpStatusError as e:
            # The FDS news endpoint returns HTTP 404 for tickers it does
            # NOT cover (mid-caps like VSH), not a 200-with-empty-list.
            # Pre-fix that 404 was caught by the outer `except Exception`
            # and returned NOT_FOUND immediately, bypassing the Finnhub
            # fallback below (which only fired on a real-empty primary).
            # Route a 404 into the empty-response path so the existing
            # fallback runs; let every other status (401/429/5xx)
            # propagate unchanged to the canonical exception mapper.
            if e.status == 404:
                response = {"news": []}
            else:
                raise
        # ISS-010: missing-key guard. `response.get("news", [])` masks
        # schema drift as empty → silently falls back to Finnhub. Now an
        # absent top-level key surfaces as SHAPE_MISMATCH explicitly.
        if not isinstance(response, dict):
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=f"news response not dict: {type(response).__name__}",
                source=src,
                retryable=False,
            )
        if "news" not in response:
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=f"news response missing 'news' key (schema drift): keys={list(response.keys())[:5]}",
                source=src,
                retryable=False,
            )
        # ISS-042 (Loop2): drop the `or []` so `{"news": None}` (key
        # present, value malformed) surfaces as SHAPE_MISMATCH instead of
        # silently falling back to Finnhub. `or []` masks the drift,
        # making FD null-value drift indistinguishable from real empty.
        news_items_raw = response["news"]
        if news_items_raw is None or not isinstance(news_items_raw, list):
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=(
                    f"news['news'] is {type(news_items_raw).__name__}, "
                    f"expected list (schema drift)"
                ),
                source=src,
                retryable=False,
            )
        news_items = news_items_raw

        if not news_items:
            # ISS-043 (Loop2 backlog): _fetch_news_finnhub now returns
            # (articles, status). Distinguish:
            #   "ok" + non-empty → Finnhub delivered (Finnhub PASSED)
            #   "ok" + empty     → Finnhub agrees zero news (real empty)
            #   "no_api_key"     → fallback unavailable; treat as primary empty
            #   "fallback_error" → Finnhub errored; surface in meta
            finnhub_articles, finnhub_status = _fetch_news_finnhub(ticker, limit)
            if finnhub_articles:
                latest_date = finnhub_articles[0].get("published_at")
                print(
                    f"    Finnhub fallback: {len(finnhub_articles)} articles",
                    file=sys.stderr,
                )
                return AdapterResult.passed(
                    data={
                        "articles": finnhub_articles,
                        "count": len(finnhub_articles),
                        "latest_date": latest_date,
                        "source": "finnhub_fallback",
                    },
                    meta={"source_hint": "fd_news_finnhub_fallback",
                          "finnhub_status": finnhub_status},
                )
            # ISS-043: surface fallback failure mode in error.detail + meta
            # so consumers can tell empty-real from fallback-broken.
            # ISS-165 (Loop20 cycle 1 fresh-session-7): _fetch_news_finnhub
            # now returns specific status strings for HTTP 401/403/429
            # so we can preserve the upstream cause classification
            # instead of collapsing all non-PASSED Finnhub outcomes
            # to UPSTREAM_ERROR.
            if finnhub_status == "unauthorized":
                return AdapterResult.failed(
                    code=ErrorCode.UNAUTHORIZED,
                    detail=(
                        "primary news endpoint returned 0 articles; "
                        "Finnhub fallback rejected with 401/403"
                    ),
                    source=src,
                    retryable=False,
                    data={"articles": [], "count": 0},
                    meta={"finnhub_status": finnhub_status},
                )
            if finnhub_status == "rate_limited":
                return AdapterResult.failed(
                    code=ErrorCode.RATE_LIMIT,
                    detail=(
                        "primary news endpoint returned 0 articles; "
                        "Finnhub fallback rate-limited (429)"
                    ),
                    source=src,
                    retryable=True,
                    data={"articles": [], "count": 0},
                    meta={"finnhub_status": finnhub_status},
                )
            if finnhub_status == "fallback_error":
                return AdapterResult.failed(
                    code=ErrorCode.UPSTREAM_ERROR,
                    detail=(
                        "primary news endpoint returned 0 articles; "
                        "Finnhub fallback errored — see stderr for cause"
                    ),
                    source=src,
                    retryable=True,
                    data={"articles": [], "count": 0},
                    meta={"finnhub_status": finnhub_status},
                )
            # finnhub_status in ("no_api_key", "ok") with empty articles —
            # both mean "we agree there's no news for this ticker right now".
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=(
                    f"empty news response; Finnhub fallback={finnhub_status}"
                ),
                source=src,
                data={"articles": [], "count": 0},
                meta={"finnhub_status": finnhub_status},
            )

        v = validate_api_shape(response, FD_NEWS_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)

        articles = []
        for item in news_items:
            # ISS-193 (Loop27 cycle 1 fresh-session-14): normalize
            # `text` to str BEFORE slice. FD_NEWS_SHAPE permits
            # text=None (Optional_(str)), so `item.get("text", "")`
            # returns None when key present with None value, then
            # `None[:500]` raises TypeError → INTERNAL_ERROR. The
            # original `summary or text[:500]` short-circuits when
            # summary is truthy, but when summary is empty/None we
            # hit the text branch and crash. Coerce non-str → "".
            _text = item.get("text")
            _text_str = _text if isinstance(_text, str) else ""
            _summary = item.get("summary")
            _summary_str = _summary if isinstance(_summary, str) else ""
            articles.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "source": item.get("source"),
                "published_at": (
                    item.get("date") or item.get("published_at")
                ),
                "sentiment": item.get("sentiment"),
                "summary": _summary_str or _text_str[:500],
            })

        latest_date = articles[0].get("published_at") if articles else None

        return AdapterResult.passed(
            data={
                "articles": articles,
                "count": len(articles),
                "latest_date": latest_date,
            },
            meta={"source_hint": "fd_news"},
        )
    except Exception as e:
        print(f"[ERROR] News fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Segmented revenues
# ---------------------------------------------------------------------------

def fetch_segmented_revenues(
    ticker: str, limit: int = 5,
) -> AdapterResult:
    """Fetch segmented REVENUE data (by product / geography / business segment).

    2026-06 endpoint migration: `/financials/segmented-revenues` was retired
    (now HTTP 404) and the data moved to `/financials/segments`, which returns
    a nested per-period shape:

        {"segmented_financials": [{
            "ticker", "report_period", "fiscal_period", "period", "currency",
            "income_statement": {
                "revenue":          {"product"|"geography"|"segment": [{label,value}]},
                "operating_income": {"segment": [{label,value}]}}}]}

    We flatten the `income_statement.revenue.<dim>` block to one denormalized
    row per (period, dimension, label) so the numeric `value` sits at row top
    level — exactly the list-of-rows shape `emit_with_numeric_coerce` coerces —
    and the downstream score-fundamental agent filters by `dimension`
    (product/geography/segment) to compute a revenue mix. `currency` is carried
    per row (NOT assumed USD) for non-USD ADRs.

    We deliberately emit ONLY the `revenue` statement, NOT `operating_income`:
    the artifact key (`segmented_revenues`) and the sole consumer
    (prompts/score-fundamental.md — revenue mix) are revenue-only, and the
    `operating_income.segment` rows share labels with `revenue.segment`
    (e.g. "Americas" appears in both with different values), which would let a
    revenue-mix calculation double-count or mix profit into a revenue %.
    (Codex review 2026-06, producer-consumer finding.) No consumer reads
    operating-income-by-segment today; add a separate artifact key if one does.
    """
    src = "financial_datasets.fetch_segmented_revenues"
    try:
        # ISS-027: urlencode all params.
        url = f"{BASE_URL}/financials/segments?" + urllib.parse.urlencode({
            "ticker": ticker, "period": "annual", "limit": limit,
        })
        response = _make_request(url)
        v = validate_api_shape(response, FD_SEGMENTED_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        periods = response.get("segmented_financials", [])

        # Flatten nested income_statement.revenue.<dim>[].{label,value} into
        # denormalized rows. Skip unlabeled rows fail-closed (rules/
        # producer-consumer.md #4 — never fabricate a segment label).
        segments = []
        for block in periods:
            if not isinstance(block, dict):
                continue
            base = {
                "ticker": block.get("ticker"),
                "report_period": block.get("report_period"),
                "fiscal_period": block.get("fiscal_period"),
                "period": block.get("period"),
                # Normalize per repo currency convention (DL3a §2) — the only
                # currency signal on these rows for non-USD ADRs; raw passthrough
                # would let " jpy "/case drift into the artifact.
                "currency": normalize_currency(block.get("currency")),
            }
            income = block.get("income_statement") or {}
            if not isinstance(income, dict):
                continue
            revenue = income.get("revenue") or {}
            if not isinstance(revenue, dict):
                continue
            for dimension, items in revenue.items():
                # Only emit consumer-known dimensions (see
                # _SEGMENTED_REVENUE_DIMENSIONS) so an unknown rollup can't
                # produce a hollow PASSED with no usable mix.
                if dimension not in _SEGMENTED_REVENUE_DIMENSIONS:
                    continue
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    label = item.get("label")
                    if not isinstance(label, str) or not label.strip():
                        continue
                    segments.append({
                        **base,
                        "dimension": dimension,
                        "label": label,
                        "value": item.get("value"),
                    })

        # ISS-105 + ISS-185: per-row numeric `value` field. The denormalized
        # row shape means emit_with_numeric_coerce's list-of-rows branch
        # handles bool/NaN/Inf (deep) + string-drift coercion at `value`.
        segments = emit_with_numeric_coerce(
            segments, numeric_fields=_SEGMENTED_NUMERIC_FIELDS,
        )

        # Fail closed when NO row carries a usable POSITIVE numeric value
        # (rules/producer-consumer.md #4): a labels-only / all-coerced-to-None
        # / all-zero / all-negative feed cannot yield a revenue mix (zero
        # denominator / nonsensical share). Revenue segments are definitionally
        # non-negative, so we require at least one strictly-positive value.
        # Returning NOT_FOUND (not a hollow PASSED) lets fetch.py promote to the
        # filing-revenue-notes fallback, which only triggers on status ==
        # FAILED. (Codex review 2026-06, silent-pass finding.) bool is already
        # coerced to None by emit_with_numeric_coerce above; the isinstance(bool)
        # guard is belt-and-suspenders.
        if not any(
            isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
            for v in (r.get("value") for r in segments)
        ):
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="no usable (positive) revenue segment values in segmented_financials",
                source=src,
                data={"segments": [], "periods": 0},
            )

        return AdapterResult.passed(
            data={
                "segments": segments,
                "periods": len(periods),
            },
            meta={"source_hint": "fd_segmented"},
        )
    except Exception as e:
        print(
            f"[ERROR] Segmented revenues fetch failed: {type(e).__name__}: {e}", file=sys.stderr,
        )
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Insider trades
# ---------------------------------------------------------------------------

def fetch_insider_data(ticker: str, limit: int = 50) -> AdapterResult:
    """Fetch insider trading data."""
    src = "financial_datasets.fetch_insider_data"
    try:
        # ISS-027: urlencode all params.
        url = f"{BASE_URL}/insider-trades?" + urllib.parse.urlencode({
            "ticker": ticker, "limit": limit,
        })
        response = _make_request(url)
        v = validate_api_shape(response, FD_INSIDER_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        trades = response.get("insider_trades", [])

        if not trades:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty insider_trades list",
                source=src,
                data={"trades": [], "count": 0, "summary": {}},
            )

        def _ts(t):
            # ISS-053 (Loop3 backlog): reject NaN/Infinity. `float("Infinity")`
            # would parse and produce sum() = inf in totals, leaking
            # non-finite numbers into JSON output (downstream
            # serializers may emit invalid JSON).
            import math
            v = t.get("transaction_shares")
            # ISS-141 (Loop12 cycle 1): is_bool_like covers numpy.bool_.
            # pre-fix only Python bool was rejected; numpy bool would slip
            # through to `float(v)` and silently become a 1-share trade.
            # If L126 sanitizer ever stops fail-loud-rejecting bools, this
            # site becomes the silent-corruption surface.
            if v is None or is_bool_like(v):
                return 0
            try:
                f = float(v)
            except (TypeError, ValueError):
                return 0
            if not math.isfinite(f):
                return 0
            return f

        # ISS-220 4.27 (Loop36 cycle 1): P3 sym-ext gap.
        # Pre-fix ordered: raw → summary (via _ts) → sanitize → emit.
        # Summary used `_ts(t)` which has its own coerce, but other
        # numeric fields per-row (transaction_value, transaction_price,
        # shares_owned_*) were sanitized AFTER summary computation —
        # so the emit row's values for those fields differ from what
        # any consumer-side audit re-computing the summary from
        # sanitized_trades would see (string drift `"500"` survives
        # `_ts` for transaction_shares but is None'd for
        # transaction_value in sanitize). Sanitize FIRST, derive
        # summary from sanitized_trades for full consistency.
        # ISS-116 (Loop8 cycle 2) + ISS-184 (Loop25 cycle 1): individual
        # trade items have many numeric fields (transaction_price /
        # transaction_value / shares_owned_*) that aren't validated by
        # FD_INSIDER_SHAPE. emit_with_numeric_coerce handles bool/
        # NaN/Inf + string-drift coercion across the list-of-rows shape.
        sanitized_trades = emit_with_numeric_coerce(
            trades, numeric_fields=_INSIDER_NUMERIC_FIELDS,
        )

        # Trade DIRECTION comes from transaction_type, NOT the sign of
        # transaction_shares. FD returns transaction_shares as a positive
        # MAGNITUDE for sales (143/151 stored open-market sales are
        # positive), so the old `_ts(t) > 0 => buy` rule inverted the net
        # insider signal (MU 20260522: 50 open-market sales scored as 50
        # buys, net_shares +48090). See regression test
        # test_insider_direction_from_transaction_type_not_share_sign.
        # Anchor on the "open market" prefix so the "exercise-price"
        # substring in "Tax or exercise-price share withholding" cannot
        # trap a disposal into a buy. Routine comp (grants, option
        # exercises, tax withholding, gifts) is not a discretionary
        # sentiment signal -> bucketed as "other", never silently counted
        # as a buy (rules/producer-consumer.md #4).
        def _direction(t):
            tt = (t.get("transaction_type") or "").strip().lower()
            if "open market" in tt:
                if "purchase" in tt or "bought" in tt or "buy" in tt:
                    return "buy"
                if "sale" in tt or "sold" in tt or "sell" in tt:
                    return "sell"
            return "other"

        buys = [t for t in sanitized_trades if _direction(t) == "buy"]
        sells = [t for t in sanitized_trades if _direction(t) == "sell"]
        other_count = sum(1 for t in sanitized_trades if _direction(t) == "other")

        # Magnitude only — transaction_shares sign is unreliable; abs()
        # also neutralizes the negatively-signed minority of sales.
        total_bought = sum(abs(_ts(t)) for t in buys)
        total_sold = sum(abs(_ts(t)) for t in sells)

        return AdapterResult.passed(
            data={
                "trades": sanitized_trades,
                "count": len(sanitized_trades),
                "summary": {
                    "buy_count": len(buys),
                    "sell_count": len(sells),
                    "total_shares_bought": total_bought,
                    "total_shares_sold": total_sold,
                    "net_shares": total_bought - total_sold,
                    "other_count": other_count,
                    "direction_basis": "open_market_transaction_type",
                },
            },
            meta={"source_hint": "fd_insider"},
        )
    except Exception as e:
        print(f"[ERROR] Insider trades fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Analyst estimates
# ---------------------------------------------------------------------------

def fetch_analyst_estimates(
    ticker: str, period: str = "quarterly", limit: int = 10,
) -> AdapterResult:
    """Fetch analyst consensus estimates (EPS + revenue)."""
    src = "financial_datasets.fetch_analyst_estimates"
    try:
        # ISS-027: urlencode — period is caller-supplied str (could in theory
        # come from less-trusted input), so urlencode is genuine defense.
        url = f"{BASE_URL}/analyst-estimates?" + urllib.parse.urlencode({
            "ticker": ticker, "period": period, "limit": limit,
        })
        response = _make_request(url)
        v = validate_api_shape(response, FD_ANALYST_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        estimates = response.get("analyst_estimates", [])
        if not estimates:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty analyst_estimates list",
                source=src,
                data={"estimates": [], "count": 0},
            )
        # ISS-085/095/158: bool drift + numeric string drift coerced
        # via _emit_with_numeric_coerce (handles list-of-rows case).
        return AdapterResult.passed(
            data={
                "estimates": emit_with_numeric_coerce(
                    estimates, numeric_fields=_ANALYST_NUMERIC_FIELDS,
                ),
                "count": len(estimates),
                "period": period,
            },
            meta={"source_hint": "fd_analyst"},
        )
    except Exception as e:
        print(
            f"[ERROR] Analyst estimates fetch failed: {type(e).__name__}: {e}", file=sys.stderr,
        )
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Earnings
# ---------------------------------------------------------------------------

def fetch_earnings_snapshot(ticker: str) -> AdapterResult:
    """Fetch latest earnings snapshot (actual vs estimated, surprise)."""
    src = "financial_datasets.fetch_earnings_snapshot"
    try:
        safe_ticker = urllib.parse.quote(ticker, safe='')
        url = f"{BASE_URL}/earnings?ticker={safe_ticker}"
        response = _make_request(url)
        v = validate_api_shape(response, FD_EARNINGS_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        earnings = response.get("earnings")
        if not earnings:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="earnings field missing or empty",
                source=src,
            )
        # 2026-05 shape regression: API changed from a single dict to a
        # list of row dicts (FD_EARNINGS_SHAPE updated to Optional_(list)).
        # Extract rows[0] so all downstream dict-handling consumers are
        # unaffected.  Guard against corrupt non-dict elements before [0].
        if isinstance(earnings, list):
            earnings = earnings[0] if (earnings and isinstance(earnings[0], dict)) else None
        if not earnings:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="earnings list was empty or first element was not a dict",
                source=src,
            )
        # DL3a §2 invariant 3 — pre-emit currency normalization. emit_with_
        # numeric_coerce only sanitizes numerics; the `currency` field would
        # otherwise pass through raw (lowercase / padded / unsupported ISO).
        # Copy-on-write so we don't mutate the input dict shared with caller.
        earnings = {
            **earnings,
            "currency": normalize_currency(earnings.get("currency")) or "UNKNOWN",
        }
        # ISS-085/095/166: bool drift + numeric string drift coerced
        # via _emit_with_numeric_coerce (single-dict case).
        return AdapterResult.passed(
            data=emit_with_numeric_coerce(
                earnings, numeric_fields=_EARNINGS_NUMERIC_FIELDS,
            ),
            meta={"source_hint": "fd_earnings"},
        )
    except Exception as e:
        print(
            f"[ERROR] Earnings snapshot fetch failed: {type(e).__name__}: {e}", file=sys.stderr,
        )
        return adapter_error_from_exception(e, source=src)


def fetch_earnings_press_releases(ticker: str) -> AdapterResult:
    """Fetch earnings press releases.  Known to return 400 for some tickers."""
    src = "financial_datasets.fetch_earnings_press_releases"
    try:
        safe_ticker = urllib.parse.quote(ticker, safe='')
        url = f"{BASE_URL}/earnings/press-releases?ticker={safe_ticker}"
        response = _make_request(url)
        v = validate_api_shape(response, FD_PRESS_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        releases = response.get("press_releases", [])
        if not releases:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty press_releases list",
                source=src,
                data={"press_releases": [], "count": 0},
            )
        return AdapterResult.passed(
            data={
                "press_releases": releases,
                "count": len(releases),
            },
            meta={"source_hint": "fd_press"},
        )
    except Exception as e:
        print(
            f"[WARNING] Earnings press releases fetch failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Institutional ownership
# ---------------------------------------------------------------------------

def fetch_institutional_ownership(
    ticker: str, limit: int = 20,
) -> AdapterResult:
    """Fetch institutional ownership (13F holdings)."""
    src = "financial_datasets.fetch_institutional_ownership"
    try:
        # ISS-027: urlencode for defense-in-depth.
        url = f"{BASE_URL}/institutional-ownership?" + urllib.parse.urlencode({
            "ticker": ticker, "limit": limit,
        })
        response = _make_request(url)
        v = validate_api_shape(response, FD_INST_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        holdings = response.get("institutional_ownership", [])
        if not holdings:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty institutional_ownership list",
                source=src,
                data={"holdings": [], "count": 0},
            )
        # ISS-085/095/163: bool drift + numeric string drift coerced
        # via _emit_with_numeric_coerce (list-of-rows case).
        return AdapterResult.passed(
            data={
                "holdings": emit_with_numeric_coerce(
                    holdings, numeric_fields=_INSTITUTIONAL_NUMERIC_FIELDS,
                ),
                "count": len(holdings),
                "ticker": response.get("ticker", ticker),
            },
            meta={"source_hint": "fd_institutional"},
        )
    except Exception as e:
        print(
            f"[ERROR] Institutional ownership fetch failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# Macro interest rates
# ---------------------------------------------------------------------------

def fetch_interest_rates_snapshot() -> AdapterResult:
    """Fetch current interest rates from all major central banks."""
    src = "financial_datasets.fetch_interest_rates_snapshot"
    try:
        url = f"{BASE_URL}/macro/interest-rates/snapshot"
        response = _make_request(url)
        v = validate_api_shape(response, FD_RATES_SNAPSHOT_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        rates = response.get("interest_rates", [])
        if not rates:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty interest_rates list",
                source=src,
                data={"rates": []},
            )
        sanitized_rates = [
            sanitize_dict_numerics(r, coerce_bool=True) for r in rates
        ]
        return AdapterResult.passed(
            data={"rates": sanitized_rates},
            meta={"source_hint": "fd_rates_snapshot"},
        )
    except Exception as e:
        print(
            f"[ERROR] Interest rates snapshot fetch failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return adapter_error_from_exception(e, source=src)


def fetch_interest_rates_historical(
    bank: str = "FED", start_date: str = None,
) -> AdapterResult:
    """Fetch historical interest rates for a central bank."""
    src = "financial_datasets.fetch_interest_rates_historical"
    try:
        # ISS-020: URL-encode `bank` and `start_date` query params. Pre-fix
        # f-string interpolation allowed `bank="FED&debug=1"` to inject
        # extra params. Sibling FD adapters all url-encode their params;
        # this brings the historical-rates path in line.
        params = {"bank": bank}
        if start_date:
            params["start_date"] = start_date
        url = f"{BASE_URL}/macro/interest-rates?{urllib.parse.urlencode(params)}"
        response = _make_request(url)
        v = validate_api_shape(response, FD_RATES_HIST_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        rates = response.get("interest_rates", [])
        if not rates:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail="empty interest_rates list",
                source=src,
                data={"rates": [], "bank": bank},
            )
        sanitized_rates = [
            sanitize_dict_numerics(r, coerce_bool=True) for r in rates
        ]
        return AdapterResult.passed(
            data={
                "rates": sanitized_rates,
                "bank": bank,
                "count": len(sanitized_rates),
            },
            meta={"source_hint": "fd_rates_historical"},
        )
    except Exception as e:
        print(
            f"[ERROR] Interest rates historical fetch failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return adapter_error_from_exception(e, source=src)
