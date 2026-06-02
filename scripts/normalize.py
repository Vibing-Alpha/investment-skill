"""Data normalization, validation, and field-mapping utilities.

Migrated from v6.5 pipeline/normalize.py.

Cross-platform fixes applied:
- pathlib.Path instead of hardcoded '/' separators
- encoding="utf-8" on all file I/O
"""

import math
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from scripts.cli_utils import read_json, write_output
from scripts.constants import (
    CRITICAL_PRICE_AGE_DAYS,
    EPS_PE_PRICE_DEVIATION_THRESHOLD,
    EPS_TTM_DEVIATION_THRESHOLD,
    MAX_52WEEK_DEVIATION,
    MAX_FINANCIAL_AGE_DAYS,
    MAX_PRICE_AGE_DAYS,
    WARNING_PRICE_AGE_DAYS,
)
from scripts.schemas.adr_profile import AdrProfile
# Canonical fiscal-quarter consecutiveness primitive (incl. Q4→next-Y Q1
# rollover) + the YYYY-QN parser — reused here so the EPS TTM window agrees
# with the DL4 quarter-window definition rather than diverging (one
# implementation, per rules/producer-consumer.md #3).
from scripts.schemas.quarter_window import (
    _FISCAL_PERIOD_RE,
    _validate_consecutive,
)

_PREFIX = "normalize"


# ---------------------------------------------------------------------------
# g -- NaN-safe getter (extracted from yfinance_fill_financial_data)
# ---------------------------------------------------------------------------

def g(obj: Dict, key: str, default: Any = None) -> Any:
    """Get value from a dict, returning *default* when the key is missing or NaN.

    The original version operated on a pandas Series; this generalisation
    works on both plain dicts and pandas Series objects.
    """
    if obj is None:
        return default
    try:
        val = obj[key]
    except (KeyError, IndexError, TypeError):
        return default

    # NaN check: float('nan') != float('nan')
    if isinstance(val, float) and math.isnan(val):
        return default

    return val


# ---------------------------------------------------------------------------
# normalize_yfinance_to_api
# ---------------------------------------------------------------------------

def normalize_yfinance_to_api() -> Dict[str, str]:
    """Return yfinance -> Financial Datasets API field-name mapping table.

    Covers the income-statement fields used by yfinance_fill_financial_data
    in comprehensive_fetch.py.
    """
    return {
        # Revenue
        "Total Revenue": "revenue",
        "Cost Of Revenue": "cost_of_revenue",
        "Gross Profit": "gross_profit",
        # Operating
        "Operating Expense": "operating_expense",
        "Selling General And Administration": "selling_general_and_administrative_expenses",
        "Research And Development": "research_and_development",
        "Operating Income": "operating_income",
        # Non-operating
        "Interest Expense": "interest_expense",
        "EBIT": "ebit",
        "Tax Provision": "income_tax_expense",
        # Net income
        "Net Income": "net_income",
        "Net Income Common Stockholders": "net_income_common_stock",
        "Net Income From Continuing And Discontinued Operation": "net_income_discontinued_operations",
        "Minority Interests": "net_income_non_controlling_interests",
        "Preferred Stock Dividends": "preferred_dividends_impact",
        # Shares
        "Basic Average Shares": "weighted_average_shares",
        "Diluted Average Shares": "weighted_average_shares_diluted",
        "Basic EPS": "earnings_per_share",
        "Diluted EPS": "earnings_per_share_diluted",
        # Balance sheet
        "Total Assets": "total_assets",
        "Total Liabilities Net Minority Interest": "total_liabilities",
        "Stockholders Equity": "shareholders_equity",
        "Total Debt": "total_debt",
        "Cash And Cash Equivalents": "cash_and_equivalents",
        "Current Investments": "current_investments",
        "Net PPE": "property_plant_and_equipment",
        "Goodwill And Other Intangible Assets": "goodwill_and_intangible_assets",
        # Cash flow
        "Operating Cash Flow": "net_cash_flow_from_operations",
        "Capital Expenditure": "capital_expenditure",
        "Free Cash Flow": "free_cash_flow",
        "Depreciation And Amortization": "depreciation_and_amortization",
        "Stock Based Compensation": "share_based_compensation",
    }


# ---------------------------------------------------------------------------
# check_currency_contamination
# ---------------------------------------------------------------------------

def check_currency_contamination(statements: List[Dict]) -> bool:
    """Return True if non-USD financials are present (EV/P-S should skip).

    Checks ALL statements for mixed or non-USD currency.
    Returns False when the list is empty (no data to contaminate).
    """
    if not statements:
        return False

    def _is_non_usd(stmt):
        cur = stmt.get("currency") if isinstance(stmt, dict) else None
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            return True  # Missing/blank currency = unknown = unsafe for ratio use
        return str(cur).strip() != "USD"

    return any(_is_non_usd(stmt) for stmt in statements)


# ---------------------------------------------------------------------------
# enforce_period_consistency
# ---------------------------------------------------------------------------

def enforce_period_consistency(statements: List[Dict]) -> List[Dict]:
    """Quarterly-to-annual fallback logic.

    If all statements are quarterly, return as-is.
    If all are annual, return as-is.
    If a mix is detected, filter to keep only the dominant period type.
    Returns the (possibly filtered) list.
    """
    if not statements:
        return statements

    # Filter non-dict entries
    statements = [s for s in statements if isinstance(s, dict)]
    if not statements:
        return statements

    # Only count explicit period values for dominance -- missing period excluded
    periods = [s.get("period") for s in statements]
    quarterly_count = sum(1 for p in periods if p == "quarterly")
    annual_count = sum(1 for p in periods if p == "annual")

    # Pure quarterly or pure annual: pass through
    if quarterly_count == len(periods) or annual_count == len(periods):
        return statements

    # Mixed: keep the dominant type + tag unknown-period rows
    dominant = "quarterly" if quarterly_count >= annual_count else "annual"
    result = []
    for s in statements:
        p = s.get("period")
        if p == dominant:
            result.append(s)
        elif p is None:
            # Keep but mark as unknown so downstream can treat with caution
            tagged = dict(s)
            tagged["_period_inferred"] = dominant
            result.append(tagged)
        # else: discard non-dominant explicit period
    return result


# ---------------------------------------------------------------------------
# validate_price_freshness
# ---------------------------------------------------------------------------

def validate_price_freshness(
    price_data: Dict, system_date: datetime
) -> Tuple[str, int, str]:
    """Validate price data freshness."""
    if not isinstance(price_data, dict):
        return "FAILED", -1, "Invalid price_data (not a dict)"
    price_time = price_data.get("time")
    if not price_time:
        return "FAILED", -1, "No timestamp in price data"

    try:
        if price_time.endswith("Z"):
            price_dt = datetime.fromisoformat(price_time.replace("Z", "+00:00"))
        else:
            price_dt = datetime.fromisoformat(price_time)

        system_dt = (
            system_date.replace(tzinfo=timezone.utc)
            if system_date.tzinfo is None
            else system_date.astimezone(timezone.utc)
        )
        if price_dt.tzinfo is None:
            price_dt = price_dt.replace(tzinfo=timezone.utc)

        # Compare dates (not datetimes) for freshness to avoid same-day
        # market-hours timestamps being flagged as "future"
        diff_days = (system_dt.date() - price_dt.date()).days

        # Future date = bad data feed (allow same-day regardless of time)
        if diff_days < 0:
            return (
                "CIRCUIT_BREAKER",
                diff_days,
                f"Price timestamp ({price_dt.isoformat()}) is in the future "
                f"relative to system date ({system_dt.isoformat()})",
            )

        price_year = price_dt.year
        system_year = system_dt.year
        year_diff = abs(system_year - price_year)

        # Only trigger circuit breaker if the year gap represents truly stale data
        # (not just a year boundary like Dec 31 -> Jan 1 which is only 1 day)
        if year_diff >= 1 and diff_days > 7:
            return (
                "CIRCUIT_BREAKER",
                diff_days,
                f"CRITICAL: Price year ({price_year}) differs from system year "
                f"({system_year}) by {year_diff} year(s)",
            )

        # Staleness is measured in TRADING sessions that have closed since
        # the price's date — NOT raw calendar days — so a Friday close
        # consumed on the weekend (or the day after a market holiday) reads
        # as 0 sessions stale instead of being flagged WARNING on every
        # weekend/holiday run. The future-date and year-gap guards above
        # intentionally stay on the calendar delta: they detect corrupt
        # feeds, not staleness.
        from scripts.delta.calendar import (
            last_closed_trading_day,
            trading_days_between,
        )
        stale = trading_days_between(
            price_dt.date(), last_closed_trading_day(system_dt)
        )

        # HIGH-20: tightened thresholds, now counted in trading sessions.
        # <=1 session: PASSED, 2..7: WARNING, >7: FAILED. The extreme-age
        # CIRCUIT_BREAKER path is retained above via the year-gap guard for
        # fetch.py's consumer semantics.
        if stale > CRITICAL_PRICE_AGE_DAYS:
            return (
                "CIRCUIT_BREAKER",
                stale,
                f"Price data is {stale} trading days old (> {CRITICAL_PRICE_AGE_DAYS})",
            )

        if stale > WARNING_PRICE_AGE_DAYS:
            return (
                "FAILED",
                stale,
                f"Price data is {stale} trading days old (> {WARNING_PRICE_AGE_DAYS})",
            )

        if stale > MAX_PRICE_AGE_DAYS:
            return (
                "WARNING",
                stale,
                f"Price data is {stale} trading days old (> {MAX_PRICE_AGE_DAYS})",
            )

        return "PASSED", stale, f"Price data is {stale} trading days old"

    except Exception as e:
        return "FAILED", -1, f"Error parsing timestamp: {e}"


# ---------------------------------------------------------------------------
# validate_price_range
# ---------------------------------------------------------------------------

def validate_price_range(price_data: Dict) -> Tuple[str, str]:
    """Check if current price is within reasonable range of 52-week high/low."""
    price = price_data.get("price")
    high_52 = price_data.get("week_52_high")
    low_52 = price_data.get("week_52_low")

    # Reject booleans masquerading as numbers
    if isinstance(price, bool) or isinstance(high_52, bool) or isinstance(low_52, bool):
        return "SKIPPED", "Boolean value in price/52-week data"
    if price is None or high_52 is None or low_52 is None:
        return "SKIPPED", "Missing price or 52-week range data"
    try:
        price = float(price)
        high_52 = float(high_52)
        low_52 = float(low_52)
    except (TypeError, ValueError):
        return "SKIPPED", "Non-numeric price or 52-week range data"
    if not all(math.isfinite(v) for v in (price, high_52, low_52)):
        return "SKIPPED", "Non-finite price or 52-week range data"
    if price <= 0 or high_52 <= 0 or low_52 <= 0:
        return "WARNING", "Non-positive price or 52-week range data"
    if high_52 < low_52:
        return "WARNING", "52-week high below 52-week low (inverted range data)"

    if price > high_52 * (1 + MAX_52WEEK_DEVIATION):
        return (
            "WARNING",
            f"Price ${price:.2f} is >{MAX_52WEEK_DEVIATION * 100:.0f}% "
            f"above 52-week high ${high_52:.2f}",
        )

    if price < low_52 * (1 - MAX_52WEEK_DEVIATION):
        return (
            "WARNING",
            f"Price ${price:.2f} is >{MAX_52WEEK_DEVIATION * 100:.0f}% "
            f"below 52-week low ${low_52:.2f}",
        )

    return (
        "PASSED",
        f"Price ${price:.2f} within 52-week range [${low_52:.2f}, ${high_52:.2f}]",
    )


# ---------------------------------------------------------------------------
# validate_financial_freshness
# ---------------------------------------------------------------------------

def validate_financial_freshness(
    financials_data: Dict, system_date: datetime
) -> Dict:
    """Validate financial statement freshness (v6.6).

    Framework requirement: Latest quarter report_period should be <=120 days
    from system date.
    """
    result = {
        "status": "SKIPPED",
        "latest_report_period": None,
        "days_old": None,
        "message": "",
    }

    if not isinstance(system_date, datetime):
        result["message"] = "Invalid system_date type (expected datetime)"
        return result
    if not isinstance(financials_data, dict):
        result["message"] = "Invalid financials_data (not a dict)"
        return result

    income_statements = financials_data.get("income_statements", [])
    if not income_statements:
        result["message"] = "No income statements available"
        return result

    # Pick the newest statement by PARSEABLE report_period date
    def _parse_rp(s):
        try:
            return datetime.strptime(s.get("report_period", ""), "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    valid_stmts = [
        s for s in income_statements
        if isinstance(s, dict) and _parse_rp(s) is not None
    ]
    if valid_stmts:
        # Prefer the newest statement that actually carries an income-statement
        # BODY (revenue or net_income present). yfinance emits a "current
        # period" husk for a just-ended fiscal year holding only EPS + share
        # counts (revenue/net_income still null); picking it by date alone
        # reports false freshness while the freshest scored-on data (prior FY
        # body) is much older. revenue == 0 is a real body (pre-revenue filer),
        # so test presence, not truthiness. NaN/Inf are "not a number" — treat
        # them as absent like None (production paths coerce non-finite->None at
        # the source, but the standalone normalize CLI can be fed raw JSON whose
        # `NaN` token parses to float('nan'), which `is not None` would wrongly
        # accept). Fall back to date-only when no row has a body, preserving
        # prior behavior for degenerate data.
        def _present(v) -> bool:
            if v is None:
                return False
            if isinstance(v, float) and not math.isfinite(v):
                return False
            return True

        def _has_income_body(s: dict) -> bool:
            return _present(s.get("revenue")) or _present(s.get("net_income"))

        body_stmts = [s for s in valid_stmts if _has_income_body(s)]
        pool = body_stmts if body_stmts else valid_stmts
        latest_stmt = max(pool, key=lambda s: _parse_rp(s))
    else:
        # Guard against non-dict rows in income_statements
        first = income_statements[0] if income_statements else None
        if not isinstance(first, dict):
            result["message"] = "No valid income statements (first row is not a dict)"
            return result
        latest_stmt = first
    report_period = latest_stmt.get("report_period")

    if not report_period:
        result["message"] = "No report_period in latest income statement"
        return result

    result["latest_report_period"] = report_period

    try:
        report_dt = datetime.strptime(report_period, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        if system_date.tzinfo is None:
            system_date = system_date.replace(tzinfo=timezone.utc)
        else:
            system_date = system_date.astimezone(timezone.utc)
        days_old = (system_date - report_dt).days
        result["days_old"] = days_old

        if days_old > MAX_FINANCIAL_AGE_DAYS:
            result["status"] = "WARNING"
            result["message"] = (
                f"Financial data is {days_old} days old "
                f"(>{MAX_FINANCIAL_AGE_DAYS} days threshold)"
            )
        elif days_old < 0:
            result["status"] = "WARNING"
            result["message"] = (
                f"Report period ({report_period}) is in the future"
            )
        else:
            result["status"] = "PASSED"
            result["message"] = (
                f"Financial data is {days_old} days old "
                f"(within {MAX_FINANCIAL_AGE_DAYS} days)"
            )

    except ValueError as e:
        result["status"] = "WARNING"
        result["message"] = (
            f"Could not parse report_period '{report_period}': {e}"
        )

    return result


# ---------------------------------------------------------------------------
# validate_fiscal_period_format
# ---------------------------------------------------------------------------

def validate_fiscal_period_format(financials_data: Dict) -> Dict:
    """Validate fiscal_period format (v6.6).

    Accepted formats: "YYYY-QN" (e.g., "2025-Q4"), bare quarter ("Q1"-"Q4"),
    or annual ("FY").  Bare and annual formats are accepted for backward
    compatibility with API sources that omit the year prefix.
    """
    result = {
        "status": "SKIPPED",
        "checked_count": 0,
        "valid_count": 0,
        "invalid_periods": [],
        "message": "",
    }

    fiscal_period_pattern = re.compile(
        r"^(Q[1-4]|FY|\d{4}-Q[1-4])$"
    )  # "Q4", "FY", or "2025-Q4"

    income_statements = financials_data.get("income_statements", [])
    if not income_statements:
        result["message"] = "No income statements to validate"
        return result

    for stmt in income_statements:
        if not isinstance(stmt, dict):
            continue
        fiscal_period = stmt.get("fiscal_period") or ""
        result["checked_count"] += 1

        if fiscal_period_pattern.match(fiscal_period):
            result["valid_count"] += 1
        else:
            result["invalid_periods"].append(
                {
                    "report_period": stmt.get("report_period"),
                    "fiscal_period": fiscal_period,
                }
            )

    if result["checked_count"] == 0:
        result["message"] = "No fiscal_period fields found"
    elif result["valid_count"] == result["checked_count"]:
        result["status"] = "PASSED"
        result["message"] = (
            f"All {result['checked_count']} fiscal_period values are valid"
        )
    else:
        result["status"] = "WARNING"
        invalid_count = len(result["invalid_periods"])
        result["message"] = (
            f"{invalid_count}/{result['checked_count']} "
            f"fiscal_period values have non-standard format"
        )

    return result


# ---------------------------------------------------------------------------
# _filter_quarterly_stmts -- shared quarterly filtering for EPS validation
# ---------------------------------------------------------------------------

# Periods treated as quarterly: explicit "quarterly", short "q", or blank/absent
_QUARTERLY_PERIODS = frozenset({"quarterly", "q", ""})


def _filter_quarterly_stmts(statements: List[Dict]) -> List[Dict]:
    """Filter and deduplicate quarterly income statements for EPS validation.

    - Keeps only dict rows with period in {"quarterly", "q", ""} (blank = unknown,
      treated as quarterly for backward compatibility with API sources).
    - Sorts newest-first by report_period.
    - Deduplicates by report_period (keeps first occurrence per period).
      Blank report_period rows are kept but capped at 1 to avoid double-counting.

    Returns the filtered, sorted, deduplicated list.
    """
    filtered = sorted(
        [s for s in statements
         if isinstance(s, dict)
         and str(s.get("period", "")).lower().strip() in _QUARTERLY_PERIODS],  # fail-open-ok: blank period IS kept (treated as quarterly — sources often omit it); annual contamination is blocked downstream by _year_quarter parse + consecutiveness gate (TTM) and the _year_quarter gate on Check 1's latest row
        key=lambda s: s.get("report_period", ""),
        reverse=True,
    )

    seen_periods: set = set()
    deduped: List[Dict] = []
    blank_count = 0
    for qs in filtered:
        rp = qs.get("report_period", "")
        if not rp:
            if blank_count == 0:
                deduped.append(qs)
                blank_count += 1
            # skip additional blank-period duplicates
        elif rp not in seen_periods:
            seen_periods.add(rp)
            deduped.append(qs)
    return deduped


# ---------------------------------------------------------------------------
# TTM EPS from consecutive quarters (for the P/E sanity check)
# ---------------------------------------------------------------------------

def _year_quarter(stmt: Dict) -> Optional[Tuple[int, int]]:
    """Parse a statement's (fiscal_year, quarter) from either the canonical
    `fiscal_period = "YYYY-QN"` form or the legacy `fiscal_period = "QN"` +
    separate `fiscal_year` form. Returns None when neither is parseable
    (fail-closed — an unparseable period can't be proven consecutive)."""
    fp = str(stmt.get("fiscal_period", "")).strip()  # fail-open-ok: ""/missing/annual-shaped → both regexes miss → returns None; callers (TTM sum + Check 1 latest-row gate) fail-close to SKIP, so a non-quarter row can never enter a TTM or be mistaken for the latest quarter
    m = _FISCAL_PERIOD_RE.fullmatch(fp)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m2 = re.fullmatch(r"[Qq]([1-4])", fp)
    if m2:
        fy = stmt.get("fiscal_year")
        if isinstance(fy, (int, float)) and not isinstance(fy, bool):
            return (int(fy), int(m2.group(1)))
    return None


def _compute_ttm_eps(quarterly_stmts: List[Dict]) -> Optional[float]:
    """Sum diluted EPS (basic fallback) over the 4 most-recent CONSECUTIVE
    quarters of `quarterly_stmts` (newest-first input). Returns None when there
    are fewer than 4 quarters, any fiscal period is unparseable, the window is
    non-consecutive (e.g. a missing fiscal Q4 — the standard 'no standalone Q4
    10-Q' gap), or any EPS value is missing. Fail-closed so a gapped sum is
    never mistaken for a true TTM (parallels the DL4 aligned-window gate)."""
    if len(quarterly_stmts) < 4:
        return None
    recent = quarterly_stmts[:4]  # fail-open-ok: newest-first (dedup'd); per-row _year_quarter + consecutiveness gate below reject any non-quarter (annual) row before summing
    yq: List[Tuple[int, int]] = []
    diluted_vals: List[Optional[float]] = []
    basic_vals: List[Optional[float]] = []
    for stmt in recent:
        t = _year_quarter(stmt)
        if t is None:
            return None  # non-quarter (e.g. annual / unparseable) row → not a TTM
        yq.append(t)
        diluted_vals.append(_safe_float_module(stmt.get("earnings_per_share_diluted")))
        basic_vals.append(_safe_float_module(stmt.get("earnings_per_share")))
    # _validate_consecutive expects oldest→newest; quarterly_stmts is newest-first.
    if _validate_consecutive(list(reversed(yq))) is not None:
        return None  # gapped window
    # Consistent basis only — never mix diluted and basic across the 4 quarters
    # (a single basic-fallback row would bias the TTM vs a diluted pe_ratio).
    if all(v is not None for v in diluted_vals):
        return sum(diluted_vals)
    if all(v is not None for v in basic_vals):
        return sum(basic_vals)
    return None


def _safe_float_module(v):
    """Module-level _safe_float (the validator has a local clone; this one is
    used by the TTM helpers above which run outside that closure)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# validate_eps_consistency
# ---------------------------------------------------------------------------

def validate_eps_consistency(
    metrics_data: Dict,
    financials_data: Dict,
    price_data: Dict,
    profile: Optional[AdrProfile] = None,
) -> Dict:
    """Cross-validate EPS data consistency.

    On the FDS path `metrics_data.earnings_per_share` is the MOST-RECENT QUARTER
    (MRQ): it is fetched `/financial-metrics?period=quarterly&limit=1`, NOT a TTM
    figure. The checks are framed accordingly (a prior version wrongly compared
    it against a 4-quarter sum and flagged a 'TTM deviation' for nearly every
    stock — see test_eps_consistency_snapshot_mrq_not_flagged_as_ttm). On the
    yfinance fallback the same field maps to `trailingEps` = TTM, so Check 1 is
    SKIPPED there (see the data_source guard below) rather than mis-fired:

    1. Snapshot EPS ~ latest-quarter statement EPS (apples-to-apples MRQ;
       deviation <=2%). Detects a SAME-quarter mis-scaled snapshot (e.g. an
       ADR-ratio / scaling error). SKIPPED when the snapshot is for a DIFFERENT
       fiscal quarter than income_statements[0] — the FDS metrics endpoint can
       lag the statements endpoint by a quarter, or the snapshot's quarter may
       be absent (fiscal-Q4 gap); a cross-quarter deviation is meaningless, so a
       stale/wrong-quarter snapshot is skipped, not flagged. The quarter is keyed
       on the normalized fiscal label (_year_quarter), not the raw report_period
       date (52/53-week issuers alias the same quarter to different dates across
       endpoints). Also SKIPPED when metrics.data_source is the yfinance fallback
       (trailingEps = TTM basis).
    2. P/E x TTM EPS ~ Current Price (deviation <=1%), where TTM EPS is summed
       over 4 CONSECUTIVE quarters; SKIP when a clean TTM is unavailable
       (gapped window / <4 quarters) rather than multiply by a non-TTM EPS.
    3. Diluted shares >= Basic shares (must be true).

    For ADR stocks with large P/E×EPS-vs-price deviation (>50%), uses
    "ADR_CHECK" instead of "WARNING" (per-ADR vs per-ordinary denominator).
    """
    is_adr = profile.is_adr if profile is not None else False
    result = {
        "status": "PASSED",
        "checks": {
            "snapshot_eps_vs_latest_quarter": {
                "status": "SKIPPED",
                "deviation": None,
                "message": "",
            },
            "pe_eps_vs_price": {
                "status": "SKIPPED",
                "deviation": None,
                "message": "",
            },
            "diluted_vs_basic_shares": {
                "status": "SKIPPED",
                "message": "",
            },
        },
        "eps_data_summary": {},
        "warnings": [],
        "errors": [],
    }

    income_statements = financials_data.get("income_statements", [])
    quarterly_stmts = _filter_quarterly_stmts(income_statements)

    # ========================================
    # Check 1: snapshot EPS (MRQ) vs latest-quarter statement EPS
    # ========================================
    def _safe_float(v):
        if v is None or isinstance(v, bool):
            return None
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    # metrics_snapshot is /financial-metrics?period=quarterly&limit=1, so on the
    # FDS path its earnings_per_share is the MOST-RECENT QUARTER (MRQ), NOT TTM.
    # Compare it to the latest quarter's income-statement EPS (apples-to-apples)
    # to catch a stale / wrong-quarter / mis-scaled snapshot. A prior version
    # compared it to a 4-quarter sum and flagged a bogus 'TTM deviation' for
    # nearly every stock (worst for ramping/cyclical names like MU).
    snapshot_eps = _safe_float(metrics_data.get("earnings_per_share"))

    # The MRQ premise above holds ONLY for the FDS path. On the yfinance
    # fallback, metrics_snapshot.earnings_per_share maps to yfinance
    # `trailingEps` = TTM (scripts/sources/yahoo_finance.py), so a TTM-snapshot-
    # vs-single-quarter comparison manufactures a spurious ~Nx deviation
    # (compounded by quote-vs-statement currency mismatch on non-USD filers —
    # e.g. SIVEF's USD trailingEps vs SEK statements -> bogus 350% WARNING).
    # Fail-closed: SKIP rather than WARN when the snapshot basis is not MRQ.
    # data_source is absent on the FDS path, "yfinance" on the fallback path.
    _snapshot_source = str(metrics_data.get("data_source") or "").strip().lower()
    _snapshot_eps_is_mrq = not _snapshot_source.startswith("yfinance")

    # The newest row must be a genuine, parseable QUARTER — guards against a
    # blank-period annual row (kept by _filter_quarterly_stmts) being compared
    # to the MRQ snapshot and producing a false deviation.
    _latest = quarterly_stmts[0] if quarterly_stmts else None
    if not _snapshot_eps_is_mrq:
        # Status stays SKIPPED (the dict default) — only annotate the reason,
        # matching the other skip branches' style.
        result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
            "Snapshot EPS sourced from yfinance (`trailingEps` = TTM), not the "
            "MRQ basis this check requires; a TTM-vs-single-quarter comparison "
            "would be basis-mismatched (and cross-currency for non-USD filers). "
            "Skipped per the snapshot-MRQ precondition."
        )
    elif snapshot_eps is not None and _latest is not None and _year_quarter(_latest) is not None:
        latest = _latest  # newest-first (deduped by _filter_quarterly_stmts)
        # snapshot earnings_per_share is BASIC; match basis, fall back diluted.
        latest_q_eps = _safe_float(latest.get("earnings_per_share"))
        eps_basis = "basic"
        if latest_q_eps is None:
            latest_q_eps = _safe_float(latest.get("earnings_per_share_diluted"))
            eps_basis = "diluted"
        latest_period = (latest.get("report_period")
                         or latest.get("fiscal_period") or "latest quarter")

        # Period-match guard. metrics_snapshot is /financial-metrics?period=
        # quarterly&limit=1, which can LAG /financials/income-statements by a
        # quarter — or the snapshot's quarter may be absent from the statements
        # (the fiscal-Q4 reporting gap seen on VPG/MU). When the snapshot is for
        # a DIFFERENT fiscal quarter than income_statements[0], the EPS comparison
        # is cross-period: the deviation is meaningless (and numerically unstable
        # near a breakeven bottom line, e.g. VPG -0.14 vs -0.02 -> 600%). SKIP
        # rather than fire a spurious 'inconsistent' WARNING. This is period-
        # SCOPED, not a blanket disable: when the quarters match (the common
        # case) the comparison runs unchanged, preserving Check 1's genuine
        # same-quarter mis-scale detector (e.g. ADR-ratio errors).
        #
        # Key on the NORMALIZED fiscal quarter via _year_quarter (the canonical
        # primitive already used by the latest-row gate above and the TTM sum) —
        # NOT raw report_period dates. A 52/53-week (4-4-5) issuer can express the
        # SAME fiscal quarter with different report_period dates across the two
        # FDS endpoints (metrics calendar month-end vs statement week-ending —
        # VPG statements use 2026-04-04 / 2025-09-27 while its snapshot used the
        # calendar 2025-12-31); keying on the date would falsely SKIP and silently
        # suppress the same-quarter mis-scale WARNING. report_period equality is a
        # fallback only when a fiscal label is unparseable on either side.
        # Snapshots with no comparable period at all keep the MRQ premise (run
        # the comparison — the legacy/no-period fixtures still hold).
        _snap_yq = _year_quarter(metrics_data)
        _latest_yq = _year_quarter(latest)
        _snap_rp = str(metrics_data.get("report_period") or "").strip()
        _latest_rp = str(latest.get("report_period") or "").strip()
        if _snap_yq is not None and _latest_yq is not None:
            _period_mismatch = _snap_yq != _latest_yq
        elif _snap_rp and _latest_rp:
            _period_mismatch = _snap_rp != _latest_rp
        else:
            _period_mismatch = False  # no comparable period -> keep MRQ premise

        if _period_mismatch:
            # status stays SKIPPED (the dict default), matching other skip branches
            _snap_label = (str(metrics_data.get("fiscal_period") or "").strip()
                           or _snap_rp or "?")
            _latest_label = (str(latest.get("fiscal_period") or "").strip()
                             or _latest_rp or "?")
            result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
                f"Snapshot EPS period ({_snap_label}) is not the latest "
                f"statement quarter ({_latest_label}) — the FDS metrics "
                f"endpoint lags income-statements, or that quarter is missing from "
                f"the statements (fiscal-Q4 gap); cross-period EPS comparison "
                f"skipped to avoid a spurious deviation."
            )
        elif latest_q_eps is not None and latest_q_eps != 0:
            deviation = abs(snapshot_eps - latest_q_eps) / abs(latest_q_eps)
            result["checks"]["snapshot_eps_vs_latest_quarter"]["deviation"] = round(
                deviation * 100, 2
            )
            result["eps_data_summary"]["snapshot_eps"] = snapshot_eps
            result["eps_data_summary"]["latest_quarter_eps"] = latest_q_eps
            result["eps_data_summary"]["latest_quarter_period"] = latest_period
            _msg = (
                f"Snapshot EPS ${snapshot_eps:.2f} vs latest quarter "
                f"({latest_period}, {eps_basis}) ${latest_q_eps:.2f} "
                f"(deviation: {deviation * 100:.1f}%)"
            )
            if deviation <= EPS_TTM_DEVIATION_THRESHOLD:
                result["checks"]["snapshot_eps_vs_latest_quarter"]["status"] = "PASSED"
                result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = _msg
            else:
                result["checks"]["snapshot_eps_vs_latest_quarter"]["status"] = "WARNING"
                result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
                    _msg + f" > {EPS_TTM_DEVIATION_THRESHOLD * 100}% — snapshot EPS "
                    f"inconsistent with the latest reported quarter"
                )
                result["warnings"].append(
                    f"Snapshot EPS deviates {deviation * 100:.1f}% from the latest "
                    f"quarter's reported EPS"
                )
        elif latest_q_eps == 0:
            result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
                "Latest quarter EPS is zero, cannot calculate deviation"
            )
        else:
            result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
                "Latest quarter EPS not available in income statements"
            )
    else:
        if snapshot_eps is None:
            result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
                "Snapshot EPS not available from metrics"
            )
        elif not quarterly_stmts:
            result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
                "No quarterly income statements available"
            )
        else:
            result["checks"]["snapshot_eps_vs_latest_quarter"]["message"] = (
                "Latest income-statement row is not a parseable quarter "
                "(e.g. annual / blank fiscal_period) — snapshot/quarter "
                "comparison skipped"
            )

    # ========================================
    # Check 2: P/E x EPS ~ Price
    # ========================================
    # DL3a §invariant 2 currency guard (impl-loop2 first-principles audit):
    # P/E × EPS gives an implied price in STATEMENT currency, compared
    # against current_price in QUOTE currency. For ADRs / foreign equities
    # where statement_currency != quote_currency, the comparison is
    # mathematically invalid. Probe statement currency first; if non-USD,
    # mark Check 2 as SKIPPED with reason — Check 1 (snapshot vs latest quarter)
    # remains valid in any single currency and runs unaffected.
    _stmt_currency_raw = None
    if income_statements:
        _stmt0 = income_statements[0]
        if isinstance(_stmt0, dict):
            _stmt_currency_raw = _stmt0.get("currency")
    _stmt_currency = (
        str(_stmt_currency_raw).strip().upper()
        if isinstance(_stmt_currency_raw, str) else None
    )
    pe_ratio = _safe_float(metrics_data.get("price_to_earnings_ratio"))
    current_price = _safe_float(price_data.get("price"))
    # The snapshot pe_ratio is TTM-based, so validate it against a TTM EPS we
    # compute from 4 CONSECUTIVE quarters — NOT the snapshot's MRQ EPS (which
    # would make pe×eps systematically miss the price). None when a clean TTM
    # is unavailable (gapped window / <4 quarters) -> SKIP rather than warn.
    ttm_eps = _compute_ttm_eps(quarterly_stmts)

    if _stmt_currency != "USD":
        # Fail-close currency guard. Use existing "SKIPPED" status so the
        # overall-status consolidation downstream treats this as PARTIAL,
        # not WARNING (a non-USD statement is a producer state, not an
        # arithmetic deviation).
        result["checks"]["pe_eps_vs_price"]["status"] = "SKIPPED"
        result["checks"]["pe_eps_vs_price"]["message"] = (
            f"statement currency={_stmt_currency_raw!r} (normalized={_stmt_currency!r}); "
            f"P/E × EPS gives implied price in statement currency, "
            f"comparison to USD-denominated current_price is invalid. "
            f"Fail-closed per DL3a §invariant 2."
        )
    elif ttm_eps is None:
        # No clean TTM (gapped window — e.g. a missing fiscal Q4 — or <4
        # quarters). Multiplying pe_ratio by a non-TTM EPS would manufacture a
        # spurious price deviation, so skip rather than warn.
        result["checks"]["pe_eps_vs_price"]["status"] = "SKIPPED"
        result["checks"]["pe_eps_vs_price"]["message"] = (
            "TTM EPS unavailable (need 4 consecutive quarters; window is "
            "gapped or short) — P/E sanity check skipped rather than computed "
            "off a non-TTM EPS."
        )
    elif pe_ratio is not None and current_price is not None:
        calculated_price = pe_ratio * ttm_eps

        if current_price > 0:
            deviation = abs(calculated_price - current_price) / current_price
            result["checks"]["pe_eps_vs_price"]["deviation"] = round(
                deviation * 100, 2
            )
            result["eps_data_summary"]["ttm_eps"] = round(ttm_eps, 4)
            result["eps_data_summary"]["pe_ratio"] = pe_ratio
            result["eps_data_summary"]["calculated_price"] = round(
                calculated_price, 2
            )
            result["eps_data_summary"]["actual_price"] = current_price

            if deviation <= EPS_PE_PRICE_DEVIATION_THRESHOLD:
                result["checks"]["pe_eps_vs_price"]["status"] = "PASSED"
                result["checks"]["pe_eps_vs_price"]["message"] = (
                    f"P/E*EPS=${calculated_price:.2f} vs Price=${current_price:.2f} "
                    f"(deviation: {deviation * 100:.2f}%)"
                )
            elif is_adr and deviation > 0.50:
                result["checks"]["pe_eps_vs_price"]["status"] = "ADR_CHECK"
                result["checks"]["pe_eps_vs_price"]["message"] = (
                    f"P/E*EPS=${calculated_price:.2f} vs Price=${current_price:.2f} "
                    f"(deviation: {deviation * 100:.2f}% — ADR denominator mismatch expected)"
                )
            else:
                result["checks"]["pe_eps_vs_price"]["status"] = "WARNING"
                result["checks"]["pe_eps_vs_price"]["message"] = (
                    f"P/E*EPS=${calculated_price:.2f} vs Price=${current_price:.2f} "
                    f"(deviation: {deviation * 100:.2f}% > "
                    f"{EPS_PE_PRICE_DEVIATION_THRESHOLD * 100}%)"
                )
                result["warnings"].append(
                    f"P/E*EPS price deviation "
                    f"({deviation * 100:.2f}%) exceeds threshold"
                )
    else:
        # ttm_eps is non-None here (None is handled by the SKIP branch above).
        missing = []
        if pe_ratio is None:
            missing.append("P/E ratio")
        if current_price is None:
            missing.append("current price")
        result["checks"]["pe_eps_vs_price"]["message"] = (
            f"Missing data: {', '.join(missing)}"
        )

    # ========================================
    # Check 3: Diluted shares > Basic shares
    # ========================================
    # Use quarterly_stmts (already newest-first) or fallback to sorted income_statements
    shares_source = quarterly_stmts if quarterly_stmts else sorted(
        [s for s in income_statements if isinstance(s, dict)],
        key=lambda s: s.get("report_period", ""), reverse=True,
    )
    if shares_source:
        latest_stmt = shares_source[0]
        basic_shares = _safe_float(latest_stmt.get("weighted_average_shares"))
        diluted_shares = _safe_float(latest_stmt.get("weighted_average_shares_diluted"))

        if basic_shares is not None and diluted_shares is not None:
            result["eps_data_summary"]["basic_shares"] = basic_shares
            result["eps_data_summary"]["diluted_shares"] = diluted_shares

            if diluted_shares >= basic_shares:
                result["checks"]["diluted_vs_basic_shares"]["status"] = "PASSED"
                result["checks"]["diluted_vs_basic_shares"]["message"] = (
                    f"Diluted ({diluted_shares:,}) >= Basic ({basic_shares:,})"
                )
            else:
                result["checks"]["diluted_vs_basic_shares"]["status"] = "WARNING"
                result["checks"]["diluted_vs_basic_shares"]["message"] = (
                    f"Diluted ({diluted_shares:,}) < Basic ({basic_shares:,}) "
                    f"- data anomaly"
                )
                result["warnings"].append(
                    "Diluted shares less than basic shares - potential data issue"
                )
        else:
            result["checks"]["diluted_vs_basic_shares"]["message"] = (
                "Share count data not available"
            )

    # ========================================
    # Determine overall status
    # ========================================
    check_statuses = [c["status"] for c in result["checks"].values()]

    if "WARNING" in check_statuses:
        result["status"] = "WARNING"
    elif all(
        s in ("PASSED", "SKIPPED", "ADR_CHECK") for s in check_statuses
    ):
        # SKIPPED checks downgrade overall to PARTIAL (missing data != passing)
        has_skipped = "SKIPPED" in check_statuses
        if "ADR_CHECK" in check_statuses:
            result["status"] = "ADR_CHECK"
        elif has_skipped:
            result["status"] = "PARTIAL"
        else:
            result["status"] = "PASSED"
    else:
        result["status"] = "PARTIAL"

    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _parse_args():
    """Parse CLI arguments for normalize/validation functions."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Data normalization and validation utilities (price/financial freshness, EPS consistency, etc.)."
    )
    sub = parser.add_subparsers(dest="command")

    # price-freshness
    pf = sub.add_parser("price-freshness", help="Validate price data freshness.")
    pf.add_argument("--price-json", required=True, help="Path to JSON file with price_data dict")
    pf.add_argument("--system-date", required=True, help="System date as YYYY-MM-DD")
    pf.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # price-range
    pr = sub.add_parser("price-range", help="Check price within 52-week range.")
    pr.add_argument("--price-json", required=True, help="Path to JSON file with price_data dict")
    pr.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # financial-freshness
    ff = sub.add_parser("financial-freshness", help="Validate financial statement freshness.")
    ff.add_argument("--financials-json", required=True, help="Path to JSON file with financials_data dict")
    ff.add_argument("--system-date", required=True, help="System date as YYYY-MM-DD")
    ff.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # fiscal-period-format
    fp = sub.add_parser("fiscal-period-format", help="Validate fiscal_period format in statements.")
    fp.add_argument("--financials-json", required=True, help="Path to JSON file with financials_data dict")
    fp.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # eps-consistency
    ec = sub.add_parser("eps-consistency", help="Cross-validate EPS data consistency.")
    ec.add_argument("--ticker", required=True, help="Ticker symbol; loader verifies it matches the adr_profile.")
    ec.add_argument("--adr-profile", required=True, type=str, help="Path to data/adr_profile.json")
    ec.add_argument("--metrics-json", required=True, help="Path to JSON file with metrics_data dict")
    ec.add_argument("--financials-json", required=True, help="Path to JSON file with financials_data dict")
    ec.add_argument("--price-json", required=True, help="Path to JSON file with price_data dict")
    ec.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # currency-contamination
    cc = sub.add_parser("currency-contamination", help="Check for non-USD financial statements.")
    cc.add_argument("--statements-json", required=True, help="Path to JSON file with list of statement dicts")
    cc.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # period-consistency
    pc = sub.add_parser("period-consistency", help="Enforce quarterly/annual period consistency.")
    pc.add_argument("--statements-json", required=True, help="Path to JSON file with list of statement dicts")
    pc.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    return parser.parse_args()




def _parse_system_date(date_str):
    """Parse YYYY-MM-DD to datetime. Exit with stderr on failure."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        print(f"normalize: invalid --system-date '{date_str}': {exc}", file=sys.stderr)
        sys.exit(1)


def _main():
    """CLI main: dispatch to normalize/validation functions based on subcommand."""
    args = _parse_args()

    if not args.command:
        print("normalize: no subcommand specified. Use --help for usage.", file=sys.stderr)
        sys.exit(1)

    if args.command == "price-freshness":
        price = read_json(args.price_json, "--price-json", _PREFIX)
        if not isinstance(price, dict):
            print(f"{_PREFIX}: --price-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        system_date = _parse_system_date(args.system_date)
        status, diff_days, message = validate_price_freshness(price, system_date)
        result = {"status": status, "diff_days": diff_days, "message": message}

    elif args.command == "price-range":
        price = read_json(args.price_json, "--price-json", _PREFIX)
        if not isinstance(price, dict):
            print(f"{_PREFIX}: --price-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        status, message = validate_price_range(price)
        result = {"status": status, "message": message}

    elif args.command == "financial-freshness":
        financials = read_json(args.financials_json, "--financials-json", _PREFIX)
        if not isinstance(financials, dict):
            print(f"{_PREFIX}: --financials-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        system_date = _parse_system_date(args.system_date)
        result = validate_financial_freshness(financials, system_date)

    elif args.command == "fiscal-period-format":
        financials = read_json(args.financials_json, "--financials-json", _PREFIX)
        if not isinstance(financials, dict):
            print(f"{_PREFIX}: --financials-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        result = validate_fiscal_period_format(financials)

    elif args.command == "eps-consistency":
        from pathlib import Path
        from scripts.schemas.adr_profile import load_adr_profile
        try:
            profile = load_adr_profile(Path(args.adr_profile), expected_ticker=args.ticker)
        except ValueError as e:
            print(f"{_PREFIX}: --adr-profile load failed: {e}", file=sys.stderr)
            sys.exit(1)
        metrics = read_json(args.metrics_json, "--metrics-json", _PREFIX)
        financials = read_json(args.financials_json, "--financials-json", _PREFIX)
        price = read_json(args.price_json, "--price-json", _PREFIX)
        if not isinstance(metrics, dict):
            print(f"{_PREFIX}: --metrics-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        if not isinstance(financials, dict):
            print(f"{_PREFIX}: --financials-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        if not isinstance(price, dict):
            print(f"{_PREFIX}: --price-json must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        result = validate_eps_consistency(metrics, financials, price, profile=profile)

    elif args.command == "currency-contamination":
        statements = read_json(args.statements_json, "--statements-json", _PREFIX)
        if not isinstance(statements, list):
            print(f"{_PREFIX}: --statements-json must contain a JSON array", file=sys.stderr)
            sys.exit(1)
        contaminated = check_currency_contamination(statements)
        result = {"contaminated": contaminated}

    elif args.command == "period-consistency":
        statements = read_json(args.statements_json, "--statements-json", _PREFIX)
        if not isinstance(statements, list):
            print(f"{_PREFIX}: --statements-json must contain a JSON array", file=sys.stderr)
            sys.exit(1)
        filtered = enforce_period_consistency(statements)
        result = {"statements": filtered, "count": len(filtered)}

    else:
        print(f"{_PREFIX}: unknown subcommand '{args.command}'", file=sys.stderr)
        sys.exit(1)

    write_output(result, args.output)


if __name__ == "__main__":
    _main()
