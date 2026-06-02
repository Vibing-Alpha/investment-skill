"""ADR detection, instrument profile, and growth-stock mode.

Migrated from v6.5 pipeline/adr.py (detect functions) + pipeline/adr_detect.py.

Cross-platform fixes applied:
- pathlib.Path instead of hardcoded '/' separators
- encoding="utf-8" on all file I/O
- adr_detect.py subprocess converted to direct function call (detect_adr_market_data)
"""

import math
import re
import sys
from collections.abc import Mapping
from typing import Dict, Optional

from scripts.cli_utils import read_json, write_output
from scripts.schemas.adr_profile import AdrProfile
# fresh-loop2 ISS-026: EPS_*_THRESHOLD imports were dead (zero usage —
# divergence checks use in-place literals). Drop to keep the single
# point of truth honest.
from scripts.sources.adapter_result import (
    AdapterResult,
    ErrorCode,
    adapter_error_from_exception,
)
from scripts.sources.yfinance_guard import yfinance_call
from scripts.sources.common import sanitize_dict_numerics, emit_with_numeric_coerce
from scripts.schemas.quarter_window import (
    aligned_pair,
    InsufficientQuartersError,
    row_matches_period,
)


# ISS-220 SF-D (Loop33 cycle 1): ADR `info.get(...)` returns potentially
# string-numeric drift (`marketCap="123"`). Pre-fix the envelope only ran
# `sanitize_dict_numerics` (NaN/Inf/bool guard), so a string `market_cap`
# slipped through and crashed downstream `_yf_mcap > 0` comparison in
# fetch.py with TypeError, losing ADR fallback. Route via the canonical
# `emit_with_numeric_coerce` boundary helper (Pattern P3 sym-ext: ADR is
# the 8th P3 emit site).
_ADR_NUMERIC_FIELDS = frozenset({
    "market_cap", "shares_outstanding",
    "implied_shares_outstanding", "float_shares",
})


from scripts.adr import safe_float as _sf

_PREFIX = "adr.detect"


# ---------------------------------------------------------------------------
# detect_adr
# ---------------------------------------------------------------------------

def detect_adr(company_facts: Dict) -> Dict:
    """Detect ADR status via multi-signal heuristic.

    Signature preserved: input is company_facts dict (matching schema in
    fetch.py output). Return value extended with ``confidence`` and
    ``detection_reasons`` — existing fields (``is_adr``, ``category``)
    retained so old consumers keep working.

    Signals:
    - high: category contains "ADR" / explicit is_adr flag / files 20-F or 6-K
    - low:  foreign domicile (non-US country)

    Any high signal → is_adr=True, confidence="high".
    Only low signals → is_adr=True, confidence="low" (fail-conservative
    for downstream safety: prefer to apply ADR correction than skip it).
    No signals → is_adr=False, confidence="none".
    """
    if not isinstance(company_facts, dict):
        return {
            "is_adr": False,
            "category": None,
            "confidence": "none",
            "detection_reasons": [],
        }

    category = str(company_facts.get("category", "") or "")
    reasons = []
    high_signals = 0
    low_signals = 0

    if "ADR" in category.upper():
        reasons.append("category_contains_ADR")
        high_signals += 1

    explicit_flag = company_facts.get("is_adr")
    if explicit_flag is True or str(explicit_flag).lower().strip() == "true":
        reasons.append("explicit_is_adr_flag")
        high_signals += 1

    filings = company_facts.get("latest_filings", []) or []
    if any(
        str(f.get("form", "")).upper().replace("-", "").startswith(("20F", "6K"))
        for f in filings if isinstance(f, dict)
    ):
        reasons.append("files_20f_or_6k")
        high_signals += 1

    country = str(company_facts.get("country", "") or "").upper().strip()
    us_or_unknown = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA", ""}
    if country and country not in us_or_unknown:
        reasons.append("foreign_domicile")
        low_signals += 1

    if high_signals > 0:
        is_adr = True
        confidence = "high"
    elif low_signals > 0:
        is_adr = True  # conservatively mark as ADR for downstream safety
        confidence = "low"
    else:
        is_adr = False
        confidence = "none"

    return {
        "is_adr": is_adr,
        "category": category,
        "confidence": confidence,
        "detection_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# build_instrument_profile
# ---------------------------------------------------------------------------

def build_instrument_profile(
    ticker: str,
    company_facts: Dict,
    profile: AdrProfile,
    gics_sector: str,
) -> Dict:
    """Build a lightweight instrument profile dict.

    Returns:
        {'ticker': str, 'asset_type': 'stock', 'is_adr': bool,
         'gics_sector': str, 'category': str, 'valuation_context': str}
    """
    is_adr = profile.is_adr
    category = company_facts.get("category", "")
    asset_type = "stock"  # ADRs are stocks; distinguished by is_adr=True

    if is_adr:
        valuation_context = (
            "ADR: use company_facts.market_cap for ratio metrics; "
            "metrics_snapshot.market_cap may be inflated by ordinary share count."
        )
    else:
        valuation_context = "Standard: metrics_snapshot ratios usable directly."

    return {
        "ticker": ticker,
        "asset_type": asset_type,
        "is_adr": is_adr,
        "gics_sector": gics_sector,
        "category": category,
        "valuation_context": valuation_context,
    }


# ---------------------------------------------------------------------------
# detect_adr_market_data -- converted from adr_detect.py subprocess
# ---------------------------------------------------------------------------

def detect_adr_market_data(ticker: str) -> AdapterResult:
    """Detect ADR market cap and shares outstanding via yfinance.

    Migrated from v6.5 adr_detect.py which ran as a subprocess with a
    hardcoded python path.  Now a direct importable function.

    Requires yfinance to be installed.

    Returns:
        AdapterResult.passed(data={'market_cap': int|None, 'shares_outstanding':
        int|None, 'implied_shares_outstanding': int|None, 'float_shares': int|None})
        on success; AdapterResult.failed on exception.
    """
    # ISS-128 (Loop9 cycle 1): validate ticker BEFORE handing it to
    # yfinance. yfinance interpolates the ticker into URL paths
    # without percent-encoding (`.../v8/finance/chart/{ticker}`), so
    # injection-style values like `"../../etc/passwd?x=y"` traverse
    # path/query into the third-party HTTP stack — bypassing this
    # project's `http_get` quoting, SSRF policy, and redaction. Reject
    # at the boundary; map InvalidTickerError to a FAILED envelope.
    from scripts.sources.yfinance_guard import (
        validate_yfinance_ticker, InvalidTickerError,
    )
    # ISS-172 (Loop21 cycle 1 fresh-session-8): canonical source must
    # match ADAPTER_ENTRYPOINTS module stem. Pre-fix the validator-
    # rejection path used `adr_detect.` while the exception path
    # (L226) used `adr.detect.` — two different strings for the same
    # adapter.
    # ISS-220 4.25 (Loop36 cycle 1): align source with
    # `ADAPTER_ENTRYPOINTS = ("detect", "detect_adr_market_data")`
    # registry (audit_fail_open uses path.stem = "detect" for
    # discovery). Pre-fix the source emitted "adr.detect..."
    # while registry stem was "detect" — audit/correlation tools
    # had two identities for the same adapter. Use the stem-aligned
    # form here.
    src = "detect.detect_adr_market_data"
    try:
        ticker = validate_yfinance_ticker(ticker)
    except InvalidTickerError as e:
        return AdapterResult.failed(
            code=ErrorCode.SHAPE_MISMATCH,
            detail=str(e)[:400],
            source=src,
            retryable=False,
        )
    try:
        import yfinance as yf

        # yfinance Ticker() is lazy; HTTP fires on .info property access.
        # Wrap the full chain so rate-limit retry/translation actually applies.
        info = yfinance_call(lambda: yf.Ticker(ticker).info)
        result = {
            "market_cap": info.get("marketCap"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "implied_shares_outstanding": info.get("impliedSharesOutstanding"),
            "float_shares": info.get("floatShares"),
        }
        # ISS-118 (Loop8 cycle 2) + ISS-220 SF-D (Loop33 cycle 1): route
        # via canonical emit_with_numeric_coerce boundary. NaN/Inf/bool
        # guard (sanitize) + string-drift coercion at known numeric fields
        # (`_ADR_NUMERIC_FIELDS`). Pre-Loop33 only the NaN/Inf/bool half
        # ran; string `marketCap="123"` reached fetch.py and crashed
        # `_yf_mcap > 0` comparison.
        return AdapterResult.passed(
            data=emit_with_numeric_coerce(
                result,
                numeric_fields=_ADR_NUMERIC_FIELDS,
                coerce_bool=True,
            ),
            meta={"source_hint": "yfinance_adr"},
        )
    except Exception as e:
        # ISS-021: route through canonical DL1→DL2 mapper so yfinance
        # rate-limit / call / transport errors get their proper ErrorCode
        # (RATE_LIMIT, UPSTREAM_ERROR, HTTP_TRANSPORT) and `retryable` flag,
        # instead of the blanket INTERNAL_ERROR/non-retryable.
        # ISS-079 (Loop6): re-wrap exception with `_yfinance_safe_msg`-
        # sanitized message body so envelope.detail goes through the
        # full sanitizer (env API keys + auth/cookie/token regex +
        # home-path strip) — same protection the yfinance fallback path
        # has. Preserve exception class for row dispatch (Yf*Error →
        # RATE_LIMIT/UPSTREAM_ERROR). Defensive type() reconstruction
        # falls back to RuntimeError for custom-__init__ exceptions.
        from scripts.sources.yahoo_finance import _yfinance_safe_msg
        sanitized_msg = _yfinance_safe_msg(e)
        try:
            sanitized_exc = type(e)(sanitized_msg)
        except (TypeError, Exception):
            # Exception class with custom __init__ (e.g. RetryExhaustedError
            # takes 4 args) — fall through to RuntimeError. Loses the
            # row-dispatch precision but preserves message safety.
            sanitized_exc = RuntimeError(sanitized_msg)
        # ISS-172 (Loop21): use canonical `src` instead of duplicating
        # the source string here.
        return adapter_error_from_exception(
            sanitized_exc, source=src,
        )


# ---------------------------------------------------------------------------
# detect_growth_stock_mode
# ---------------------------------------------------------------------------

_FISCAL_Q_RE = re.compile(r"^(\d{4})-Q([1-4])$")


def _parse_fiscal_quarter(fp: object) -> Optional[tuple]:
    """Parse an FDS ``fiscal_period`` like ``"2026-Q3"`` into ``(fy, q)``.

    Returns None for annual / malformed / non-string values.
    """
    if not isinstance(fp, str):
        return None
    m = _FISCAL_Q_RE.match(fp.strip())
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2))


def _discrete_quarter_sbc(latest_cf: Mapping, cf_rows, raw_sbc: float) -> float:
    """De-cumulate a YTD share-based-comp figure to the discrete quarter.

    US-GAAP 10-Q cash-flow statements report SBC as a YTD-cumulative figure
    (there is no discrete-quarter cash-flow statement in a 10-Q), while the
    income statement reports the discrete quarter. Dividing a 9-month YTD SBC
    by a single-quarter revenue overstates the ratio ~Nx (SNOW Q3 FY26:
    1.196B / 1.213B = 0.986 vs the true ~0.34) and can falsely trip
    high_sbc_ratio near the 0.15 threshold.

    De-cumulation applies ONLY to YTD-cumulative sources. The yfinance
    fallback (`quarterly_cashflow`) already reports DISCRETE per-quarter
    cash flows tagged ``data_source == "yfinance"`` — de-cumulating those
    would wrongly subtract a real prior quarter and UNDERSTATE the ratio
    (a false negative). So yfinance rows short-circuit to the raw value.
    FDS 10-Q rows (no ``data_source == "yfinance"`` tag) are YTD and get
    de-cumulated: when the latest matched row is fiscal quarter N>1 and the
    immediately-prior same-FY quarter (N-1) is present in the SAME currency
    with a strictly smaller |SBC| (confirming accumulation), the discrete
    quarter is ``latest_ytd - prior_ytd``.

    Returns the raw value unchanged for: yfinance-sourced rows, Q1
    (YTD == discrete), annual/malformed fiscal_period, a missing or
    currency-mismatched prior row, or a non-increasing pair (the magnitude
    guard — a secondary backstop for any unknown discrete source). Known
    limitation: when the immediate prior quarter is absent from a YTD source
    (a gap right before the latest quarter), the raw YTD is used and the
    overstatement persists for that one quarter — no worse than pre-fix,
    and rare for the Jan-FYE cohort whose only gap is the unfetched Q4.
    """
    # yfinance reports discrete quarters — never de-cumulate (would understate).
    if latest_cf.get("data_source") == "yfinance":
        return raw_sbc
    parsed = _parse_fiscal_quarter(latest_cf.get("fiscal_period"))
    if parsed is None:
        return raw_sbc
    fy, q = parsed
    if q <= 1:
        return raw_sbc  # Q1 YTD == discrete quarter
    target = (fy, q - 1)
    latest_cur = str(latest_cf.get("currency") or "").strip().upper()
    prior_sbc = None
    for row in cf_rows:
        if row is latest_cf or not isinstance(row, Mapping):
            continue
        # Never subtract a DISCRETE (yfinance) prior from a YTD (FDS) latest.
        # Today cash_flows is single-source (yfinance fills only when FDS is
        # empty), so this never excludes anything — it makes that invariant
        # explicit and robust if a future path ever mixes sources.
        if row.get("data_source") == "yfinance":
            continue
        # Normalized fiscal-quarter compare (parity with latest parsing —
        # tolerate whitespace/format variance that a raw string == would miss).
        if _parse_fiscal_quarter(row.get("fiscal_period")) != target:
            continue
        # Don't subtract across a currency mismatch (mixed-currency ADR rows).
        row_cur = str(row.get("currency") or "").strip().upper()
        if latest_cur and row_cur and row_cur != latest_cur:
            continue
        prior_sbc = _sf(row.get("share_based_compensation"))
        break
    if prior_sbc is None:
        return raw_sbc  # no prior YTD row to subtract — degrade to raw
    # Confirm accumulation: a YTD figure strictly exceeds the prior YTD in
    # magnitude. If not (anomalous data / unknown discrete source), keep the
    # raw value rather than risk a spurious difference.
    if abs(raw_sbc) <= abs(prior_sbc):
        return raw_sbc
    return raw_sbc - prior_sbc


def detect_growth_stock_mode(metrics_data: Dict, financials_data: Dict, *, ticker: str) -> Dict:
    """Detect if the stock exhibits growth stock characteristics requiring special analysis.

    Triggers (from growth-stock-analysis-ref.md):
    - Negative net income (last 4 quarters combined < 0): weight 1.0
    - High gross margin (> 70%): weight 1.0
    - High SBC ratio (SBC/Revenue > 15%): weight 1.0
    - High cash ratio (cash/total_assets > 40%): weight 0.5

    Score >= 2.0 enables growth stock mode.

    Returns dict with:
    - auto_detected: bool
    - triggers: dict of each condition
    - score: float
    - enabled: bool
    - override: null (can be overridden by command line)
    """
    result = {
        "auto_detected": False,
        "triggers": {
            "negative_net_income": False,
            "high_gross_margin": False,
            "high_sbc_ratio": False,
            "high_cash_ratio": False,
        },
        "trigger_values": {},
        "score": 0.0,
        "enabled": False,
        "override": None,
        # ISS-198 (Loop28 cycle 1 fresh-session-15): explicit status
        # field so consumers (fetch.py orchestrator) can distinguish
        # "computed clean" from "exception swallowed during compute".
        # Pre-fix the broad except at L387 stored `result["error"]=str(e)`
        # but left status implicit, and fetch.py:2402 wrote the dict
        # into category_statuses without a downgrade. Now: status is
        # always present and consumers can branch on it.
        "status": "PASSED",
        "error": None,
    }

    try:
        # 1. Negative net income (TTM < 0)
        income_statements = [
            s for s in financials_data.get("income_statements", [])
            if isinstance(s, dict)
        ]
        is_annual = (
            # Post-impl ISS-042 (cycle 13): use row_matches_period — case-
            # insensitive annual detection (parallel to adr/correct.py
            # parallel site).
            row_matches_period(income_statements[0], "annual")
            if income_statements
            else False
        )
        # Filter via row_matches_period (post-impl ISS-039 structural fix):
        # delegate period semantics to the schema-layer helper. Closes the
        # 3-site producer-consumer-vocab-drift pattern.
        _period_target = "annual" if is_annual else "quarterly"
        income_statements = [
            s for s in income_statements
            if row_matches_period(s, _period_target)
        ]
        # fresh-loop2 cycle 3 C3C-MED-2: sort newest-first BEFORE the
        # [:ttm_n] / [0] reads. Pre-fix relied on producer ordering — if
        # any upstream producer returned oldest-first or unstable order,
        # the TTM negative-NI trigger would pick a stale 4-quarter window
        # (or the cash-ratio gate at L495 would read an old balance row).
        # Parity with `_sort_newest` in adr/correct.py.
        income_statements.sort(
            key=lambda s: s.get("report_period") or "", reverse=True,
        )
        ttm_n = 1 if is_annual else 4
        if len(income_statements) >= ttm_n:
            # _sf already coerces None→0; the nested default 0 is
            # belt-and-suspenders for `.get("net_income")` returning None.
            # TTM sum treats a missing quarter as 0 contribution (the
            # alternative — skipping the sum entirely — masks partial-
            # data tickers from the negative_net_income trigger which
            # would be worse for safety). fail-open-ok: per-quarter
            # missing income contributes 0; sign-check still fires
            # correctly on the present quarters.
            ttm_net_income = sum(
                _sf(stmt.get("net_income", 0))  # fail-open-ok: see comment above
                for stmt in income_statements[:ttm_n]  # fail-open-ok: per-quarter missing income contributes 0; sign-check still fires correctly on the present quarters (DL4 invariant 7 per-signal degrade carve-out, spec §3.2 line 1088)
            )
            result["trigger_values"]["ttm_net_income"] = ttm_net_income
            if ttm_net_income < 0:
                result["triggers"]["negative_net_income"] = True
                result["score"] += 1.0

        # 2. High gross margin (> 70%)
        # ISS-197 (Loop28 cycle 1 fresh-session-15): explicitly nullify
        # bool. Pre-fix `not isinstance(_, bool)` skipped the float()
        # conversion for bool inputs but DID NOT also nullify them, so
        # `gross_margin = True` survived to `True > 0.70 → True` (bool
        # is int subclass) and triggered high_gross_margin spuriously.
        # Nullify explicitly so the existing not-None gate fires.
        gross_margin = metrics_data.get("gross_margin")
        if isinstance(gross_margin, bool):
            gross_margin = None
        elif gross_margin is not None:
            try:
                gross_margin = float(gross_margin)
            except (TypeError, ValueError):
                gross_margin = None
            else:
                # ISS-220 4.8 (Loop33 cycle 1): reject Inf/-Inf/NaN. Pre-fix
                # `float("inf") > 1.0` was True → wrote `Infinity` JSON token
                # (non-standard) into trigger_values + spuriously set
                # high_gross_margin=True. cli_utils.write_output uses default
                # `allow_nan=True` so Infinity persisted to disk.
                if not math.isfinite(gross_margin):
                    gross_margin = None
        if gross_margin is not None:
            # Normalize: values > 1 are likely percentages (75 -> 0.75)
            if gross_margin > 1.0:
                gross_margin = gross_margin / 100.0
            result["trigger_values"]["gross_margin"] = gross_margin
            if gross_margin > 0.70:
                result["triggers"]["high_gross_margin"] = True
                result["score"] += 1.0

        # 3. High SBC ratio (SBC/Revenue > 15%)
        # DL4 §3.2 Fix F + plan §2 Decision 1 per-signal degrade.
        # SBC/Revenue requires PRECISE income↔cash_flow quarter pairing
        # (mismatched quarters distort the ratio), but only consumes
        # income.total_revenue + cash_flow.share_based_compensation —
        # balance_sheet data is never read. The DL4 §3.2 Fix F migration
        # originally routed SBC through `aligned_quarters` (3-family
        # intersection), which silently dropped SBC triggers for
        # sparse-balance ADRs even when income + cash_flow were precisely
        # paired. The over-coupling was reflagged 4 times by zero-context
        # Codex review (rounds 1 / 4 / 6 / 9) → loop-protocol §pattern-
        # decay §3 mandates helper-layer structural fix.
        #
        # Resolution (post-impl ISS-062): replace aligned_quarters with
        # the new 2-family `aligned_pair(income, cash_flow, ticker=...)`
        # helper. It applies the same invariant-9 metadata checks
        # (fiscal_period + currency agreement) without coupling to the
        # balance family. Both annual and quarterly paths unify on the
        # same call shape; the only mode-dependent step is the cash_flow
        # period pre-filter (annual lenient via accept_missing=True,
        # quarterly strict via _is_quarterly_vocab) — parallel to the
        # income_statements filter at line ~333.
        #
        # ISS-220 Loop37 Logic-1: filter cash_flows / balance_sheets to
        # dict rows up front; a non-dict first row would AttributeError
        # on `.get(...)` and the broad except below would discard
        # already-computed negative-income / gross-margin signals.
        cash_flows = [
            cf for cf in financials_data.get("cash_flows", [])
            if isinstance(cf, dict)
        ]
        balance_sheets = [
            bs for bs in financials_data.get("balance_sheets", [])
            if isinstance(bs, dict)
        ]

        if is_annual:
            # accept_missing=True: provider often omits explicit
            # period="annual" tag on cf rows (ISS-044 lenient semantic).
            # ISS-052 quarterly-evidence guard inside row_matches_period
            # still rejects rows whose fiscal_period proves quarterly.
            sbc_cf_rows = [
                cf for cf in cash_flows
                if row_matches_period(cf, "annual", accept_missing=True)
            ]
        else:
            sbc_cf_rows = [
                cf for cf in cash_flows
                if row_matches_period(cf, "quarterly")
            ]

        sbc_pair: Optional[tuple[Mapping, Mapping]] = None
        if not income_statements:
            result["trigger_values"]["sbc_ratio_skipped"] = (
                "no period-matched income_statement row available"
            )
        elif not sbc_cf_rows:
            result["trigger_values"]["sbc_ratio_skipped"] = (
                f"no {'annual' if is_annual else 'quarterly'} "
                f"cash_flow row available"
            )
        else:
            try:
                sbc_pair = aligned_pair(
                    income_statements, sbc_cf_rows, ticker=ticker,
                )
            except InsufficientQuartersError as e:
                result["trigger_values"]["sbc_ratio_skipped"] = (
                    f"aligned_pair: failure_kind={e.failure_kind} "
                    f"available={e.available} detail={e!s}"
                )
            else:
                if sbc_pair is None:
                    result["trigger_values"]["sbc_ratio_skipped"] = (
                        "aligned_pair: no shared report_period between "
                        "income_statements and cash_flows"
                    )

        if sbc_pair is not None:
            latest_is, latest_cf = sbc_pair
            sbc = _sf(latest_cf.get("share_based_compensation"))
            # SNOW 2026-05-28: 10-Q cash-flow SBC is YTD-cumulative while the
            # income revenue below is discrete-quarter. De-cumulate to the
            # discrete quarter so the ratio is window-consistent (else a
            # 9-month YTD SBC / single-quarter revenue overstates ~Nx and can
            # falsely trip high_sbc_ratio). Quarterly path only; no-op for
            # Q1 / annual / missing-prior / already-discrete rows.
            if not is_annual:
                sbc = _discrete_quarter_sbc(latest_cf, sbc_cf_rows, sbc)
            # ISS-220 4.34 (Loop38 cycle 1, iter7): two-step `is not
            # None` selection. Pre-fix `_sf(a) or _sf(b)` treated a
            # legitimate `total_revenue=0` as falsy and silently
            # fell through to `revenue` field — false trigger of
            # the SBC-ratio path on companies that legitimately
            # report zero revenue. Reviewer-required two-step form
            # (raw selection, then coerce) so the call-site shape
            # is unmistakable.
            tr = latest_is.get("total_revenue")
            raw_rev = tr if tr is not None else latest_is.get("revenue")
            revenue = _sf(raw_rev)

            if revenue and revenue > 0:
                sbc_ratio = abs(sbc) / revenue  # SBC is often negative in cash flow
                result["trigger_values"]["sbc_ratio"] = round(sbc_ratio, 4)
                result["trigger_values"]["sbc_amount"] = sbc
                result["trigger_values"]["revenue"] = revenue
                if sbc_ratio > 0.15:
                    result["triggers"]["high_sbc_ratio"] = True
                    result["score"] += 1.0

        # 4. High cash ratio (cash/total_assets > 40%)
        # Reuse the dict-filtered balance_sheets from the SBC block above
        # (post-impl ISS-007): re-reading the raw list and isinstance-checking
        # only `balance_sheets[0]` would yield latest_bs={} when the FIRST
        # row is non-dict, silently hiding valid later rows.
        # fresh-loop2 cycle 3 C3C-MED-2 part 2: sort balance_sheets newest-
        # first too. Pre-fix the `[0]` read trusted producer ordering.
        if balance_sheets:
            balance_sheets_sorted = sorted(
                balance_sheets,
                key=lambda s: s.get("report_period") or "",
                reverse=True,
            )
            latest_bs = balance_sheets_sorted[0]
            cash = _sf(latest_bs.get("cash_and_equivalents")) + _sf(latest_bs.get("current_investments"))
            total_assets = _sf(latest_bs.get("total_assets"))

            if total_assets > 0:
                cash_ratio = cash / total_assets
                result["trigger_values"]["cash_ratio"] = round(cash_ratio, 4)
                result["trigger_values"]["cash_and_investments"] = cash
                result["trigger_values"]["total_assets"] = total_assets
                if cash_ratio > 0.40:
                    result["triggers"]["high_cash_ratio"] = True
                    result["score"] += 0.5

        # Determine if enabled
        result["auto_detected"] = result["score"] >= 2.0
        result["enabled"] = result["auto_detected"]  # Can be overridden later

    except Exception as e:
        # ISS-198 (Loop28): also surface status=FAILED so consumers can
        # distinguish exception-during-compute from clean PASSED with no
        # triggers. Pre-fix `result["error"]=str(e)` left status field
        # absent; fetch.py treated the dict as success.
        result["status"] = "FAILED"
        result["error"] = str(e)
        result["error_type"] = type(e).__name__

    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _parse_args():
    """Parse CLI arguments for ADR detect functions."""
    import argparse
    parser = argparse.ArgumentParser(
        description="ADR detection, instrument profile, and growth-stock mode."
    )
    sub = parser.add_subparsers(dest="command")

    # detect-adr
    # fresh-loop2 cycle 3 C3C-MED-1: add `--ticker` for CLI uniformity
    # with the other subcommands. detect_adr() itself doesn't read
    # ticker (heuristic classifier on company_facts only) but the CLI
    # output now carries it so logs/audits can identify which ticker
    # the detection was for.
    da = sub.add_parser("detect-adr", help="Detect whether a company is an ADR.")
    da.add_argument("--ticker", required=True, help="Ticker symbol (echoed in output)")
    da.add_argument("--facts-json", required=True, help="Path to JSON file with company_facts dict")
    da.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # instrument-profile
    ip = sub.add_parser("instrument-profile", help="Build lightweight instrument profile.")
    ip.add_argument("--ticker", required=True, help="Ticker symbol")
    ip.add_argument("--facts-json", required=True, help="Path to JSON file with company_facts dict")
    ip.add_argument("--adr-profile", required=True, type=str, help="Path to data/adr_profile.json")
    ip.add_argument("--gics-sector", required=True, help="GICS sector string")
    ip.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # growth-stock-mode
    gs = sub.add_parser("growth-stock-mode", help="Detect growth stock characteristics.")
    gs.add_argument("--ticker", required=True, help="Ticker symbol")
    gs.add_argument("--metrics-json", required=True, help="Path to JSON file with metrics_data dict")
    gs.add_argument("--financials-json", required=True, help="Path to JSON file with financials_data dict")
    gs.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # detect-market-data (replaces adr_detect.py subprocess)
    # fresh-loop2 cycle 3 C3C-MED-1: keep positional for backward
    # compatibility, but ALSO accept `--ticker`. Either form works;
    # the call site resolves to whichever is provided. CLI scripts
    # composing detect-market-data should prefer `--ticker` for
    # consistency with all other ADR subcommands.
    dm = sub.add_parser("detect-market-data", help="Detect ADR market cap/shares via yfinance.")
    dm.add_argument("ticker", nargs="?", help="Ticker symbol (positional — legacy form)")
    dm.add_argument("--ticker", dest="ticker_opt", help="Ticker symbol (preferred — CLI-uniform)")
    dm.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    return parser.parse_args()




def _main():
    """CLI main: dispatch to ADR detect functions based on subcommand."""
    args = _parse_args()

    if not args.command:
        print("adr.detect: no subcommand specified. Use --help for usage.", file=sys.stderr)
        sys.exit(1)

    if args.command == "detect-adr":
        facts = read_json(args.facts_json, "--facts-json", _PREFIX)
        if not isinstance(facts, dict):
            print(f"{_PREFIX}: --facts-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        result = detect_adr(facts)
        # fresh-loop2 cycle 5 M5 (C-MED-2): echo ticker in output as
        # promised by the cycle-3 `--ticker required=True` arg docstring.
        # Pre-fix the CLI argument was accepted but discarded — operator
        # logs and downstream consumers couldn't tie the detection
        # result back to the input ticker.
        result["ticker"] = args.ticker

    elif args.command == "instrument-profile":
        from pathlib import Path
        from scripts.schemas.adr_profile import load_adr_profile
        facts = read_json(args.facts_json, "--facts-json", _PREFIX)
        if not isinstance(facts, dict):
            print(f"{_PREFIX}: --facts-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        try:
            profile = load_adr_profile(Path(args.adr_profile), expected_ticker=args.ticker)
        except ValueError as e:
            print(f"{_PREFIX}: --adr-profile load failed: {e}", file=sys.stderr)
            sys.exit(1)
        result = build_instrument_profile(
            ticker=args.ticker,
            company_facts=facts,
            profile=profile,
            gics_sector=args.gics_sector,
        )

    elif args.command == "growth-stock-mode":
        metrics = read_json(args.metrics_json, "--metrics-json", _PREFIX)
        financials = read_json(args.financials_json, "--financials-json", _PREFIX)
        if not isinstance(metrics, dict):
            print(f"{_PREFIX}: --metrics-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        if not isinstance(financials, dict):
            print(f"{_PREFIX}: --financials-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        # Defense-in-depth: the production path runs this detector on
        # currency-RECONCILED financials (fetch._growth_stock_mode_reconciled).
        # This standalone CLI does NOT reconcile, so on a saved foreign-ADR
        # 02_financial_data.json with a partial currency mix the cash_ratio /
        # sbc_ratio would be currency-mixed garbage (the MRAAY bug the wrapper
        # was built to fix). Warn loudly so a CLI user isn't silently misled.
        from scripts.schemas.currency_consistency import detect_mixed_currency
        if detect_mixed_currency(financials.get("income_statements", []))["status"] == "mixed":
            print(
                f"{_PREFIX}: [WARN] input financials are currency-MIXED "
                f"(foreign-ADR partial conversion); cash_ratio / sbc_ratio may be "
                f"dimensionally invalid. Production uses fetch's reconciled path.",
                file=sys.stderr,
            )
        result = detect_growth_stock_mode(metrics, financials, ticker=args.ticker)

    elif args.command == "detect-market-data":
        # fresh-loop2 cycle 3 C3C-MED-1: resolve --ticker preferred form
        # over positional. Both forms accept; neither set → fail-close
        # with explicit error.
        ticker = args.ticker_opt or args.ticker
        if not ticker:
            print(
                "adr.detect: detect-market-data requires --ticker "
                "(or positional ticker for legacy compatibility)",
                file=sys.stderr,
            )
            sys.exit(1)
        result_envelope = detect_adr_market_data(ticker)
        if not result_envelope.ok:
            print(
                f"adr.detect: {result_envelope.error.detail}",
                file=sys.stderr,
            )
            sys.exit(1)
        result = result_envelope.data

    else:
        print(f"adr.detect: unknown subcommand '{args.command}'", file=sys.stderr)
        sys.exit(1)

    # Error-key guard: internal functions may return {"error":...} or {"message":...}
    # on invalid input. Surface these as CLI failures rather than silent success.
    # detect-market-data now short-circuits via the .ok check above; this guard
    # applies only to other subcommands (detect-adr, detect-growth-stock-mode) which
    # still return dict.
    if isinstance(result, dict):
        # Post-impl ISS-009 (fresh-loop1): the prior guard had a partial-
        # success carve-out — `not any(result.get(k) for k in (score, ...))`
        # let a FAILED result through to exit 0 if any signal had fired
        # before the exception. E.g. growth-stock-mode increments `score`
        # incrementally as gross_margin / cash_ratio thresholds hit; if a
        # later statement raises and the function sets
        # `status="FAILED"` / `error_type=...`, the partial score >0
        # short-circuited the err-guard so the FAILED dict was written
        # with exit 0. Now: explicit failure markers ALWAYS exit 1,
        # regardless of how many partial-success keys exist.
        is_explicit_failure = (
            (isinstance(result.get("status"), str)
             and result["status"].upper() in {"FAILED", "ERROR"})
            or result.get("error_type")
        )
        err = result.get("error") or result.get("message")
        if is_explicit_failure:
            msg = err or result.get("status") or result.get("error_type")
            print(f"adr.detect: {msg}", file=sys.stderr)
            sys.exit(1)
        # is_adr is a valid success signal for detect-adr subcommand
        if err and not any(result.get(k) for k in ("score", "auto_detected", "correction",
                                                     "has_mismatch", "is_adr")):
            print(f"adr.detect: {err}", file=sys.stderr)
            sys.exit(1)

    write_output(result, args.output)


if __name__ == "__main__":
    _main()
