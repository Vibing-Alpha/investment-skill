"""Comprehensive data fetch orchestrator for v7.

CLI entrypoint that fetches all data categories (A-K + macro), runs
validations, detects ADR/growth-stock modes, and writes output files.

Migrated from v6.5 pipeline/fetch.py with cross-platform fixes:
- pathlib.Path for all file operations
- explicit encoding="utf-8" on all I/O
- ensure_ascii=False on JSON writes
- no hardcoded venv paths (uses direct function imports)
- no subprocess calls (ADR detection is a direct function call)

CLI:
    python3 scripts/fetch.py -t AAPL -o /tmp/out [--system-date 2026-03-22] [-v]
"""

import argparse
import json
import math
import os
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Tuple

# ---------------------------------------------------------------------------
# Ensure the project root (parent of scripts/) is on sys.path so that
# ``python3 scripts/fetch.py`` works standalone.
# ---------------------------------------------------------------------------
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scripts.constants import (
    BASE_URL,
    CATEGORIES,
    KNOWN_ADR_CLASSIFICATIONS,
    MIN_FILING_ITEM_CHARS,
    REQUIRED_10K_ITEMS,
    REQUIRED_10Q_ITEMS,
)
from scripts.cli_utils import write_output as _cli_write_output
from scripts.cli_utils import write_text_atomic as _cli_write_text_atomic
from scripts.sources.yfinance_guard import yfinance_call
from scripts.sources.yahoo_finance import YfinanceFallbackOutcome
from scripts.sources.adapter_result import (
    AdapterResult,
    AdapterError,
    ErrorCode,
    adapter_error_from_exception,
)
from scripts.sources.api_shapes import _is_valid_yyyy_mm_dd


# ---------------------------------------------------------------------------
# Allowlist for --categories CLI arg (DL7 #2 — fail-open-silent-category-gating).
# ---------------------------------------------------------------------------
# Each value is a prefix string that `_should_fetch(cat_prefix)` (defined in
# main()) dispatches on. Unknown values used to silently fetch nothing,
# producing an empty report with no error indication. The argparse handler
# rejects any unknown value with exit code 2 (configuration error) BEFORE
# any category-gated work begins.
#
# Source of truth: every literal passed to `_should_fetch(...)` in this file.
# When adding a new dispatch site, add the prefix here in the same commit.
#
# Note: `00_validation` is ALWAYS written (not gated) and `adr_profile` /
# `indicators` are downstream artifacts produced outside `--categories`
# control, so they are intentionally NOT in this set.
KNOWN_CATEGORIES: frozenset = frozenset({
    "01_price_data",
    "02_financial_data",
    "03_company_news",
    "04_insider_data",
    "05_filing",           # bare prefix (used by the filing-family any() gate)
    "05_filing_summary",
    "05_filing_content",
    "06_analyst_estimates",
    "07_earnings",
    "08_institutional",
    "09_macro_rates",
})


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the comprehensive data fetcher."""
    parser = argparse.ArgumentParser(
        description="Comprehensive Data Fetcher (v7 modular pipeline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ticker", "-t", required=True, help="Stock ticker symbol"
    )
    parser.add_argument(
        "--output-dir", "-o", required=True, help="Output directory"
    )
    parser.add_argument(
        "--system-date",
        help="Override system date (YYYY-MM-DD format)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )
    parser.add_argument(
        "--categories",
        default=None,
        help=(
            "Comma-separated category prefixes to fetch (e.g. "
            "'01_price_data,03_company_news'). Default: all."
        ),
    )
    parser.add_argument(
        "--news-limit",
        type=int,
        default=10,
        help=(
            "Per-request news article limit. Default 10 for compatibility "
            "with pre-delta full runs. Probe-mode callers should pass 100."
        ),
    )
    parser.add_argument(
        "--tier-decided",
        choices=["probe", "full", "partial", "no_op"],
        default=None,
        help=(
            "The tier this fetch supports. 'probe' is for the first phase "
            "before classifier runs (fetch-only, no commit semantics); "
            "'full'/'partial'/'no_op' are terminal values written to "
            "00_validation.json for the assembler."
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# save_json / save_text
# ---------------------------------------------------------------------------

def save_json(data: Dict, path: Path) -> None:
    """Atomic JSON write via cli_utils.write_output (temp + os.replace).

    Previously non-atomic; a crash between writing 00_validation.json and
    later 02_*/05_* files could leave downstream reading a 'success' flag
    while business data was half-written.
    """
    _cli_write_output(data, str(path))


def save_text(content: str, path: Path) -> None:
    """Atomic text write via cli_utils.write_text_atomic (temp + os.replace)."""
    _cli_write_text_atomic(content, str(path))


# ---------------------------------------------------------------------------
# ADR classification helpers
# ---------------------------------------------------------------------------


def classify_ticker(ticker: str) -> Dict:
    """Classify a ticker's ADR status using the static fallback table.

    The classification table lives in scripts.constants.KNOWN_ADR_CLASSIFICATIONS.
    Unknown tickers default to domestic (fail-safe).
    """
    ticker = ticker.upper()
    if ticker in KNOWN_ADR_CLASSIFICATIONS:
        return dict(KNOWN_ADR_CLASSIFICATIONS[ticker])
    return {
        "filing_type": "10-K",
        "needs_ratio_correction": False,
        "data_quality_tier": "domestic",
    }


def write_adr_anchor(classification: Dict, output_path: Path) -> None:
    """Write the ADR classification + corrected values to a JSON anchor file.

    ISS-211 (Loop30 cycle 1 fresh-session-17): route through `save_json`
    (atomic temp+os.replace) instead of plain `open(..., "w")` to match
    the rest of fetch.py's output writes. Pre-fix a crash between open
    and json.dump could leave `adr_correction.json` truncated, and
    downstream consumers would read a half-written JSON.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(classification, output_path)


def _latest_shares_outstanding(financials_data: Dict) -> Optional[float]:
    """Most-current diluted share count for market-cap reconciliation.

    Prefers the latest balance sheet's point-in-time `outstanding_shares`
    (statement arrays are NEWEST-first → index [0]); falls back to the
    income statement's diluted then basic weighted-average shares. Returns
    None when no positive share count is available.
    """
    fin = financials_data or {}
    bs = fin.get("balance_sheets") or []
    if bs:
        for key in ("outstanding_shares", "shares_outstanding",
                    "common_shares_outstanding"):
            v = bs[0].get(key)
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and v > 0):
                return float(v)
    inc = fin.get("income_statements") or []
    if inc:
        for key in ("weighted_average_shares_diluted",
                    "weighted_average_shares"):
            v = inc[0].get(key)
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and v > 0):
                return float(v)
    return None


def _resolve_is_adr(profile: object, fallback_is_adr: bool) -> bool:
    """Authoritative ADR flag — the adr_profile detector overrides company_data.

    `company_data.is_adr` (from yfinance) can be False for a known ADR
    (e.g. MRAAY/Murata), which would skip ADR-gated per-share corrections and
    write a wrong `validation.is_adr`. The adr_profile (known-ADR table +
    domicile/20-F signals, produced by `resolve_adr_profile`) is the authority.
    Falls back to the company-derived flag when no profile is available
    (subset fetch) or the profile lacks the attribute.
    """
    if profile is not None:
        val = getattr(profile, "is_adr", None)
        if val is not None:
            return bool(val)
    return bool(fallback_is_adr)


def _reconcile_anchor_with_profile(
    classification: Dict, profile: object, *, fallback_is_adr: bool = False,
) -> Dict:
    """Honor the data-driven adr_profile when writing the classify_ticker anchor.

    `classify_ticker` is a static KNOWN_ADR_CLASSIFICATIONS lookup with a
    `domestic` fallback for unknown tickers. The richer `adr_profile` detector
    (domicile / 20-F / category signals) catches foreign ADRs the table does
    NOT list — e.g. SIVEF (Sivers Semiconductors AB), a SEK-reporting OTC
    issuer with no SEC filings. Without reconciliation the anchor reports such
    a foreign ADR as `domestic` / `10-K`, contradicting the profile and the
    non-USD statements (misleading downstream metadata).

    The ADR truth is resolved via `_resolve_is_adr(profile, fallback_is_adr)`:
    pass the caller's already-resolved `is_adr` as `fallback_is_adr` so the
    subset-fetch path — where `profile is None` but `is_adr` was upgraded from
    a prior persisted `adr_profile.json` — is still honored (codex review).

    When the resolved truth is authoritative-ADR but the static table fell
    back to `domestic`, upgrade the anchor to an honest foreign-but-unverified
    tier. A ticker already carrying a curated foreign tier (`pure_adr` /
    `sec_foreign`) is trusted and left untouched, as is a genuine domestic
    ticker. Mutates and returns `classification`. No DL3c fields are
    introduced, so the anchor is still recognized as the non-DL3c
    classify_ticker artifact.
    """
    if not _resolve_is_adr(profile, fallback_is_adr):
        return classification
    if classification.get("data_quality_tier") != "domestic":
        # Curated foreign tier already present — trust the static table.
        return classification
    # adr_profile detected a foreign ADR the static table doesn't know.
    classification["data_quality_tier"] = "adr_unverified"
    classification["filing_type"] = "20-F"
    classification["needs_ratio_correction"] = False  # unverified ratio — conservative
    classification["classification_source"] = "adr_profile_reconciled"
    return classification


def _repair_financials_currency_marker(financial_output: Dict) -> None:
    """Detect + self-validating repair of mixed-currency statements at the
    02_financial_data.json save boundary.

    The mix is created by the H-block's `compute_adr_valuation_correction`,
    which runs DL3c `apply_fx_conversion` IN PLACE on the shared statements,
    converting only the 12-field master set to USD and leaving the rest native.
    Run AFTER that (here, on the final saved object) so the repair sees the mix
    and converts the remaining native fields to USD. On success the statement
    is fully USD + carries a `currency_consistency` repaired marker; on
    unrepairable it is flagged so extract_fcf / historical_multiples fail-close.
    """
    from scripts.schemas.currency_consistency import (
        detect_mixed_currency,
        repair_mixed_currency,
    )
    if detect_mixed_currency(financial_output.get("income_statements", []))["status"] != "mixed":
        return
    repair = repair_mixed_currency(financial_output)
    if repair["status"] == "repaired":
        print(
            f"    [WARN] mixed-currency statements REPAIRED to USD (implied FX "
            f"{repair['implied_fx_by_period']}, "
            f"{len(repair.get('converted_fields', []))} fields). The ADR valuation "
            f"correction converts only the 12-field master set in place; the "
            f"remaining native fields are reconciled here.",
            file=sys.stderr,
        )
    else:
        financial_output["currency_consistency"] = {
            "status": "mixed_unrepairable",
            "detector": detect_mixed_currency(financial_output.get("income_statements", [])),
            "violations": repair.get("violations", []),
            "reason": repair.get("reason"),
            # Self-document the partial-conversion trap. The rows are left in the
            # post-H-block mix (12-field master set in USD, rest native) but each
            # row STILL carries currency:"USD" — load-bearing for the valuation/FX
            # producers, so the tag must stay "USD". Those producers do NOT read
            # this marker, and none double-converts: extract_fcf +
            # historical_multiples independently fail-close by RE-RUNNING
            # detect_mixed_currency() on the income rows (status "mixed"), while
            # adr.correct gates on the row currency tag. The gap THIS warning
            # closes is the non-fail-close consumers: the score-business LLM
            # scoring agents read raw rows directly, have no detector re-run, and
            # would trust the USD tag and compute currency-mixed ratios. The score
            # prompts gate on this warning / the marker status.
            "partial_conversion_warning": (
                "Statement rows are tagged currency:'USD' but only the 12-field "
                "master set was FX-converted; the remaining money fields are "
                "still in the native currency. Any cross-field ratio computed "
                "from these raw rows (gross/operating margin, current ratio, "
                "cash ratio, total_liabilities/equity, etc.) is currency-MIXED "
                "and WRONG. Do not compute ratios from these rows — source them "
                "from company-reported figures (filings/WebSearch), or recompute "
                "on one currency basis via detector.implied_fx. The USD row tag "
                "exists only to stop downstream FX producers double-converting."
            ),
        }
        print(
            f"    [WARN] mixed-currency statements NOT repairable "
            f"({repair.get('reason') or repair.get('violations')}). Downstream "
            f"FCF/multiples producers re-detect the mix and fail-close; raw rows "
            f"stay partially-converted (tagged USD) — ratios from them are mixed.",
            file=sys.stderr,
        )


def _growth_stock_mode_reconciled(
    metrics_data: Dict, financials_data: Dict, *, ticker: str
) -> Dict:
    """Run growth-stock-mode detection on currency-RECONCILED financials.

    The H-block (`compute_adr_valuation_correction`) leaves `financials_data`
    in a per-field currency mix (12-field master set in USD, rest native JPY).
    Growth-stock-mode reads balance-sheet `cash_and_equivalents` +
    `current_investments` + `total_assets`, so a naive detect computes
    cash_ratio from USD cash + native investments over a native total_assets —
    a dimensionally-invalid value (MRAAY 2025-09-30: 0.011 vs the true ~0.20).

    Reconcile the mix to all-USD FIRST (detect-gated, so a no-op on clean /
    consistent statements — non-ADR paths are untouched), THEN detect. The
    reconcile stamps the `currency_consistency` marker on `financials_data` in
    place; the 02_financial_data save boundary carries that marker onto the
    saved object, making the repair call there an idempotent no-op. The mix's
    only money-field consumer between the H-block and the save is this detector
    (`_reconcile_market_cap` reads share counts only), so reconciling here is
    safe and does not perturb other readers.
    """
    from scripts.adr.detect import detect_growth_stock_mode

    _repair_financials_currency_marker(financials_data)
    return detect_growth_stock_mode(metrics_data, financials_data, ticker=ticker)


def _reconcile_market_cap(
    price_data: Dict,
    financials_data: Dict,
    *,
    is_adr: bool = False,
    divergence_threshold: float = 0.25,
) -> Optional[Dict]:
    """Correct a stale provider `market_cap` against price × shares.

    The snapshot's `market_cap` is taken verbatim from the Yahoo chart
    `meta.marketCap`, which can lag the current price for fast-moving stocks
    (VSH ran 3.7x in 6 months while the field stayed at the old ~$14 level,
    understating market cap ~3.3x and silently flipping every market-cap-
    derived multiple — P/S 0.64→2.07, P/B 0.94→3.10). The canonical market
    cap is current price × shares; `price` is freshness-validated upstream,
    so recompute from it when the provider value is missing or diverges
    materially. Mutates `price_data['market_cap']` in place when overriding.

    Returns a reconciliation record (status ∈ consistent|corrected|filled),
    or None when there is not enough data to check (missing price/shares) —
    in which case the provider value is left untouched (fail-safe: never
    fabricate a market cap without both a live price and a share count).

    ADR guard: for ADRs the snapshot price is per-ADR (USD) while the
    financials' share count is ordinary (home-market) shares, so price ×
    shares is a unit mismatch unless the ADR ratio is 1:1. The provider
    market_cap (total-company cap) is the correct concept — leave it
    untouched. (MRAAY/Murata: a $22.50 ADR × 1.83B ordinary shares wrongly
    produced ~$41B vs the provider's ~$82B; real cap ~$47-68B.)
    """
    if is_adr:
        return None
    price = price_data.get("price")
    if (not isinstance(price, (int, float)) or isinstance(price, bool)
            or not math.isfinite(price) or price <= 0):
        return None
    shares = _latest_shares_outstanding(financials_data)
    if shares is None:
        return None
    implied = float(price) * shares
    if not math.isfinite(implied):
        return None
    provider = price_data.get("market_cap")
    if (isinstance(provider, (int, float)) and not isinstance(provider, bool)
            and math.isfinite(provider) and provider > 0):
        divergence = abs(provider / implied - 1.0)
        if divergence <= divergence_threshold:
            return {
                "status": "consistent",
                "provider": provider,
                "implied": implied,
                "divergence_pct": round(divergence * 100, 2),
            }
        price_data["market_cap"] = implied
        return {
            "status": "corrected",
            "from": provider,
            "to": implied,
            "shares": shares,
            "divergence_pct": round(divergence * 100, 2),
            "reason": "provider_market_cap_diverged_from_price_x_shares",
        }
    price_data["market_cap"] = implied
    return {
        "status": "filled",
        "to": implied,
        "shares": shares,
        "reason": "provider_market_cap_missing",
    }


# metrics_snapshot fields whose value is market-cap (or, equivalently, price)
# linear in the NUMERATOR: multiple = market_cap / fundamental. A cap
# correction rescales them by the cap ratio — the denominator (earnings,
# sales, book value, expected growth) is price-independent. PEG = (P/E) /
# growth, so it scales exactly like P/E.
_CAP_LINKED_NUMERATOR_FIELDS = (
    "price_to_earnings_ratio",
    "price_to_sales_ratio",
    "price_to_book_ratio",
    "peg_ratio",
)
# Fields with market_cap in the DENOMINATOR: yield = fundamental / market_cap.
# These scale by the INVERSE of the cap ratio.
_CAP_LINKED_DENOMINATOR_FIELDS = (
    "free_cash_flow_yield",
)
# Fields with enterprise_value in the numerator: multiple = EV / fundamental.
# EV = market_cap + net_debt; net_debt is unaffected by a price/cap
# correction, so EV moves by the SAME absolute delta as the cap and the
# multiple scales by the EV ratio (not the cap ratio).
_EV_LINKED_NUMERATOR_FIELDS = (
    "enterprise_value_to_ebitda_ratio",
    "enterprise_value_to_revenue_ratio",
)


def _is_finite_number(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _propagate_market_cap_to_metrics(
    metrics_snapshot: Dict,
    reconciliation: Optional[Dict],
) -> Optional[Dict]:
    """Propagate a price_data market-cap reconciliation into metrics_snapshot.

    `_reconcile_market_cap` corrects `price_data['market_cap']`, but the raw
    `metrics_snapshot` (02_financial_data.json) still carries the STALE
    provider cap AND every multiple derived from it (P/E, P/S, P/B, PEG,
    EV/EBITDA, EV/Rev, FCF yield). Those multiples are cap-linked — the
    fundamental denominators (earnings, sales, book, EBITDA, revenue, FCF) are
    unaffected by a price/cap correction — so the reconciliation must be
    pushed through here too, or consumers (the `historical_multiples` script
    AND the valuation LLM agent) silently read multiples computed off a lagged
    cap (MU 20260522: P/E stayed 19.4 vs true ~35 after a $467.7B -> $846.9B
    correction).

    Rescales in place: cap-numerator fields x cap_ratio, cap-denominator
    fields / cap_ratio, EV-numerator fields x ev_ratio. Price-INDEPENDENT
    fields (margins, growth, per-share, leverage, turnover, EPS, ROE/ROA) are
    left untouched. Stamps a `market_cap_reconciliation` provenance block
    mirroring the price_data record.

    No-op (returns None) unless the reconciliation actually overrode the cap
    (status corrected|filled). When the snapshot has no usable stale cap to
    ratio against, the cap is still aligned to the reconciled truth but the
    derived multiples are left as-is and flagged (multiples_rescaled=False) —
    never emit a wrong rescale.
    """
    if not isinstance(metrics_snapshot, dict):
        return None
    if not reconciliation or reconciliation.get("status") not in ("corrected", "filled"):
        return None
    reconciled_cap = reconciliation.get("to")
    if not (_is_finite_number(reconciled_cap) and reconciled_cap > 0):
        return None

    stale_cap = metrics_snapshot.get("market_cap")
    record: Dict = {
        "applied": True,
        "reconciled_market_cap": reconciled_cap,
        "stale_market_cap": stale_cap if _is_finite_number(stale_cap) else None,
        "source": "price_data.market_cap_reconciliation",
    }
    # Always align the cap itself to the reconciled truth.
    metrics_snapshot["market_cap"] = reconciled_cap

    if not (_is_finite_number(stale_cap) and stale_cap > 0):
        # No usable old cap to form a ratio -> cannot rescale the derived
        # multiples without fabricating. Flag rather than emit wrong numbers.
        record["multiples_rescaled"] = False
        record["ev_ratio"] = None
        record["fields_rescaled"] = []
        record["note"] = (
            "stale market_cap missing/zero — derived multiples left as-is and "
            "may be inconsistent with the reconciled cap"
        )
        metrics_snapshot["market_cap_reconciliation"] = record
        return record

    cap_ratio = reconciled_cap / stale_cap
    rescaled = []

    for field in _CAP_LINKED_NUMERATOR_FIELDS:
        v = metrics_snapshot.get(field)
        if _is_finite_number(v):
            metrics_snapshot[field] = v * cap_ratio
            rescaled.append(field)
    for field in _CAP_LINKED_DENOMINATOR_FIELDS:
        v = metrics_snapshot.get(field)
        if _is_finite_number(v):
            metrics_snapshot[field] = v / cap_ratio
            rescaled.append(field)

    # EV moves by the same absolute delta as the cap (net debt unchanged),
    # so enterprise_value itself can be aligned for ANY finite old EV —
    # including exactly 0 (where net_debt == -stale_cap, so EV_new = Δcap).
    ev_old = metrics_snapshot.get("enterprise_value")
    ev_ratio = None
    if _is_finite_number(ev_old):
        ev_new = ev_old + (reconciled_cap - stale_cap)
        metrics_snapshot["enterprise_value"] = ev_new
        # EV multiples scale by the EV ratio, but only when the old EV is
        # non-zero — a 0 old EV carries no recoverable EBITDA/revenue base,
        # so leave those multiples flagged (unscaled) rather than fabricate.
        if ev_old != 0:
            ev_ratio = ev_new / ev_old
            for field in _EV_LINKED_NUMERATOR_FIELDS:
                v = metrics_snapshot.get(field)
                if _is_finite_number(v):
                    metrics_snapshot[field] = v * ev_ratio
                    rescaled.append(field)

    record["cap_ratio"] = cap_ratio
    record["ev_ratio"] = ev_ratio
    record["fields_rescaled"] = rescaled
    record["multiples_rescaled"] = True
    metrics_snapshot["market_cap_reconciliation"] = record
    return record


# ---------------------------------------------------------------------------
# _fetch_filing_data_impl -- DI variant of fetch_filing_data
# ---------------------------------------------------------------------------

def _fetch_filing_data_impl(
    ticker: str,
    is_adr: bool = False,
    fmp_api_key: str = "",
    fetch_fmp_metadata_fn: Optional[Callable] = None,
    fetch_filing_date_fn: Optional[Callable] = None,
) -> AdapterResult:
    """Fetch SEC filings with dependency-injected callables.

    This is the DI variant of comprehensive_fetch.fetch_filing_data.
    External dependencies (FMP metadata, filing date resolution) are
    injected as callables rather than referencing module-level globals.

    Args:
        ticker: Stock ticker symbol.
        is_adr: Whether the stock is an ADR.
        fmp_api_key: FMP API key (empty string disables FMP fallback).
        fetch_fmp_metadata_fn: Callable(ticker, filing_type, limit=1,
            fmp_api_key=...) -> AdapterResult (T15).
        fetch_filing_date_fn: Callable(ticker, filing_type, accession_number,
            fmp_api_key=..., fetch_fmp_metadata_fn=...) -> AdapterResult (T15).

    Returns:
        AdapterResult with data={"summary": ..., "content": ...} and
        meta["filing_status_legacy"] preserving the 4-state legacy string
        (PASSED/PARTIAL/INCOMPLETE/FAILED).
    """
    import time

    from scripts.sources.common import make_request
    from scripts.sources.sec_edgar import (
        fetch_filing_from_sec_edgar,
        fetch_filing_items_from_api,
    )

    # Lazy-load default callables
    if fetch_fmp_metadata_fn is None:
        from scripts.sources.fmp import _fetch_filing_metadata_from_fmp_impl
        fetch_fmp_metadata_fn = _fetch_filing_metadata_from_fmp_impl
    if fetch_filing_date_fn is None:
        from scripts.sources.fmp import _fetch_filing_date_impl
        fetch_filing_date_fn = _fetch_filing_date_impl

    # Helper: thin wrapper around FMP metadata with injected API key
    def _fmp_metadata(tkr, ftype, limit=1):
        # v18: fetch_fmp_metadata_fn returns AdapterResult (T15). Unwrap to
        # preserve impl-internal List[Dict] contract for downstream
        # fmp_results[0].get(...) indexing.
        # ISS-065 (Loop4): also track non-OK envelopes via _track_child
        # so the final filing envelope can surface UNAUTHORIZED/RATE_LIMIT
        # causes from FMP. _track_child resolves at call-time (closure)
        # so its forward-declaration is safe.
        result = fetch_fmp_metadata_fn(
            tkr, ftype, limit=limit, fmp_api_key=fmp_api_key,
        )
        _track_child(result)
        return result.data.get("items", []) if result.ok else []

    # Helper: thin wrapper around FMP filing date
    def _fmp_filing_date(tkr, ftype, acc):
        # v18: fetch_filing_date_fn returns AdapterResult (T15). Unwrap to
        # preserve impl-internal str contract.
        # ISS-065 (Loop4): track non-OK envelopes (RATE_LIMIT / NOT_FOUND
        # from method1+method2 collapse) for filing-pipeline severity.
        result = fetch_filing_date_fn(
            tkr, ftype, acc,
            fmp_api_key=fmp_api_key,
            fetch_fmp_metadata_fn=fetch_fmp_metadata_fn,
        )
        _track_child(result)
        return result.data.get("filing_date", "") if result.ok else ""  # fail-open-ok: pre-T15 seam; empty str → caller tries SEC EDGAR fallback

    # Helper: make request via scripts.sources.common
    def _api_get(endpoint_url: str) -> Dict:
        """Direct URL fetch through sources.common.make_request."""
        return make_request(endpoint_url)

    from scripts.sources.fmp import convert_fmp_to_filing_metadata

    summary: Dict = {
        "filings_list": [],
        "latest_10k": None,
        "latest_10q": None,
        "latest_20f": None,
        "latest_6k": None,
        "validation": {
            "10k_items_status": {},
            "10q_items_status": {},
            "missing_items": [],
            "retry_attempts": 0,
        },
    }

    # ISS-051 (Loop3): track child AdapterErrors from sec_edgar / fmp /
    # API fallback calls so the final filing envelope can surface the
    # highest-severity cause (e.g. SSRF_BLOCKED, RATE_LIMIT) instead of
    # collapsing every failure to UPSTREAM_ERROR. Mirrors ISS-041
    # financials transport-error preservation.
    child_errors: list = []

    def _track_child(result):
        """Append child AdapterError to accumulator if this call failed
        with a structured envelope. Returns the result unchanged for
        chaining."""
        if result is not None and not result.ok and result.error is not None:
            child_errors.append(result.error)
        return result

    content_dict: Dict = {}
    max_retries = CATEGORIES["filing"]["retry_count"]

    # ISS-017 fix: URL-encode ticker before query interpolation. Pre-fix,
    # `f"{BASE_URL}/filings?ticker={ticker}&limit=20"` raw-interpolated CLI
    # input. A `--ticker "AAPL&limit=999"` would inject extra query params.
    # Other Financial Datasets adapters (financial_datasets.py) all use
    # urllib.parse.quote(ticker, safe='') — this brings _fetch_filing_data_impl
    # in line.
    safe_ticker = urllib.parse.quote(ticker, safe='')

    # ISS-073 (Loop5): convert raw _api_get exceptions into AdapterError
    # and add to child_errors. Pre-fix, primary FD `/filings` 429 / 401 /
    # SSRF / size were just stderr-logged then masked by later fallback
    # NOT_FOUND. The severity selector at the final return couldn't see
    # them. Now: route through canonical mapper so primary HTTP failures
    # surface as RATE_LIMIT / UNAUTHORIZED / SSRF_BLOCKED in the
    # filing envelope's severity-selected error code.
    def _track_api_exc(exc, source_label):
        """Map a raw FD `_api_get` exception to AdapterError and track."""
        envelope = adapter_error_from_exception(
            exc, source=f"fetch._fetch_filing_data_impl/{source_label}",
        )
        if envelope.error is not None:
            child_errors.append(envelope.error)

    # ISS-083 (Loop6): validate raw `/filings` response shape so a
    # drifted upstream returning `{"filings": ["bad"]}` or non-dict items
    # doesn't AttributeError later when we call `filing.get(...)`.
    def _validate_filings_list(resp):
        """Return (filings_list, error_msg). filings_list is empty if
        shape drift; error_msg is non-empty if drift detected."""
        if not isinstance(resp, dict):
            return [], f"response not dict: {type(resp).__name__}"
        if "filings" not in resp:
            return [], "missing 'filings' key"
        flist = resp["filings"]
        if not isinstance(flist, list):
            return [], f"filings not list: {type(flist).__name__}"
        # Each item must be dict so .get() calls don't crash
        for i, item in enumerate(flist):
            if not isinstance(item, dict):
                return [], f"filings[{i}] not dict: {type(item).__name__}"
        return flist, ""

    # Step 1: Get filings list (for ADR 20-F/6-K fallback scan and filing_summary metadata)
    for attempt in range(max_retries):
        try:
            url = f"{BASE_URL}/filings?ticker={safe_ticker}&limit=20"
            response = _api_get(url)
            flist, drift_msg = _validate_filings_list(response)
            if drift_msg:
                print(
                    f"[ERROR] Filings list shape drift: {drift_msg}",
                    file=sys.stderr,
                )
                # Track as SHAPE_MISMATCH for severity selection
                child_errors.append(AdapterError(
                    code=ErrorCode.SHAPE_MISMATCH,
                    detail=f"filings list shape drift: {drift_msg}",
                    source="fetch._fetch_filing_data_impl/filings_list",
                    retryable=False,
                ))
            else:
                summary["filings_list"] = flist
            if summary["filings_list"]:
                break
        except Exception as e:
            print(
                f"[ERROR] Filings list fetch attempt {attempt + 1}/{max_retries} "
                f"failed: {e}",
                file=sys.stderr,
            )
            _track_api_exc(e, "filings_list")  # ISS-073
            summary["validation"]["retry_attempts"] += 1
            if attempt < max_retries - 1:
                time.sleep(1)

    # Step 2: Dedicated server-side filtered calls for 10-K and 10-Q
    latest_10k = None
    latest_10q = None

    for ftype_label, ftype_param in [("10-K", "10-K"), ("10-Q", "10-Q")]:
        try:
            url = f"{BASE_URL}/filings?ticker={safe_ticker}&filing_type={ftype_param}&limit=1"
            resp = _api_get(url)
            # ISS-083: same shape validation as filings list — guard
            # against `{"filings": ["bad"]}` or non-dict entries that
            # would crash `filings[0].get(...)` below.
            filings, drift_msg = _validate_filings_list(resp)
            if drift_msg:
                print(
                    f"[ERROR] {ftype_label} filtered shape drift: {drift_msg}",
                    file=sys.stderr,
                )
                child_errors.append(AdapterError(
                    code=ErrorCode.SHAPE_MISMATCH,
                    detail=f"{ftype_label} filtered: {drift_msg}",
                    source=f"fetch._fetch_filing_data_impl/filings_{ftype_label}",
                    retryable=False,
                ))
                filings = []
            if filings:
                if ftype_label == "10-K":
                    latest_10k = filings[0]
                else:
                    latest_10q = filings[0]
                print(
                    f"    Found {ftype_label}: report_date={filings[0].get('report_date')}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"[ERROR] {ftype_label} filtered fetch failed: {e}",
                file=sys.stderr,
            )
            _track_api_exc(e, f"filings_{ftype_label}")  # ISS-073

    # FMP fallback for 10-K
    if not latest_10k and fmp_api_key:
        print("    Primary API found no 10-K, trying FMP fallback...", file=sys.stderr)
        fmp_results = _fmp_metadata(ticker, "10-K", limit=1)
        if fmp_results:
            latest_10k = convert_fmp_to_filing_metadata(fmp_results[0])
            print(
                f"    FMP found 10-K: report_date={latest_10k.get('report_date')}",
                file=sys.stderr,
            )

    # FMP fallback for 10-Q
    if not latest_10q and fmp_api_key:
        print("    Primary API found no 10-Q, trying FMP fallback...", file=sys.stderr)
        fmp_results = _fmp_metadata(ticker, "10-Q", limit=1)
        if fmp_results:
            latest_10q = convert_fmp_to_filing_metadata(fmp_results[0])
            print(
                f"    FMP found 10-Q: report_date={latest_10q.get('report_date')}",
                file=sys.stderr,
            )

    # SEC EDGAR submissions fallback (direct CIK lookup)
    # Covers newly listed / spun-off companies not yet in third-party APIs
    if not latest_10k or not latest_10q:
        from scripts.sources.sec_edgar import lookup_filing_via_sec_submissions

        if not latest_10k:
            print(
                "    No 10-K from API/FMP, trying SEC EDGAR submissions...",
                file=sys.stderr,
            )
            sec_10k_result = lookup_filing_via_sec_submissions(ticker, "10-K")
            # ISS-065 (Loop4): track FAILED submissions results so a
            # RATE_LIMIT / UNAUTHORIZED cause from SEC submissions
            # surfaces in the final filing envelope's severity-selected
            # error code instead of being silently dropped.
            _track_child(sec_10k_result)
            # ISS-026: lookup returns PARTIAL when reportDate is missing
            # (4 of 5 fields populated). Read .data unconditionally and let
            # downstream missing_report_date sentinel check drive fail-closed.
            # FAILED still produces empty data via the envelope contract.
            sec_10k = (
                sec_10k_result.data
                if sec_10k_result.status != "FAILED"
                else {}
            )
            if sec_10k:
                latest_10k = sec_10k

        if not latest_10q:
            print(
                "    No 10-Q from API/FMP, trying SEC EDGAR submissions...",
                file=sys.stderr,
            )
            sec_10q_result = lookup_filing_via_sec_submissions(ticker, "10-Q")
            # ISS-065 (Loop4): same as sec_10k_result above — track child.
            _track_child(sec_10q_result)
            # ISS-026: same as sec_10k_result above — PARTIAL preserves
            # missing_report_date sentinel for the downstream fail-closed path.
            sec_10q = (
                sec_10q_result.data
                if sec_10q_result.status != "FAILED"
                else {}
            )
            if sec_10q:
                latest_10q = sec_10q

    # ADR fallback: 20-F / 6-K (extract from already-fetched filings list)
    if not latest_10k and not latest_10q:
        print(
            "    No 10-K/10-Q found, trying 20-F/6-K (ADR fallback)...",
            file=sys.stderr,
        )

        for filing in summary["filings_list"]:
            ftype = filing.get("filing_type", "")
            if ftype == "20-F" and not summary["latest_20f"]:
                summary["latest_20f"] = filing
                print(
                    f"    Found 20-F: report_date={filing.get('report_date')}",
                    file=sys.stderr,
                )
            elif ftype == "6-K" and not summary["latest_6k"]:
                summary["latest_6k"] = filing
                print(
                    f"    Found 6-K: report_date={filing.get('report_date')}",
                    file=sys.stderr,
                )
            if summary["latest_20f"] and summary["latest_6k"]:
                break

        if not summary["latest_20f"] and not summary["latest_6k"]:
            print(
                "[CRITICAL] No 10-K, 10-Q, 20-F, or 6-K filings found",
                file=sys.stderr,
            )
            # ISS-082 (Loop6): early return path now also routes through
            # the same severity-of-child-errors selection used at the
            # main exit (line ~865). Pre-fix this branch unconditionally
            # returned UPSTREAM_ERROR even when child_errors had recorded
            # higher-severity causes (RATE_LIMIT, SSRF_BLOCKED,
            # UNAUTHORIZED) from primary FD failures.
            from scripts.sources.adapter_result import severity_of_error
            _early_data = {"summary": summary, "content": content_dict}
            _early_meta = {"source_hint": "filing_composite",
                           "filing_status_legacy": "FAILED"}
            if child_errors:
                primary = min(child_errors, key=severity_of_error)
                _early_meta["child_error_count"] = len(child_errors)
                # ISS-220 SF-A (Loop32 cycle 2): preserve full child
                # metadata via canonical helper.
                return AdapterResult.failed_from_child(
                    primary,
                    source="fetch._fetch_filing_data_impl",
                    detail=(
                        f"no 10-K/10-Q/20-F/6-K filings found for ticker "
                        f"(ADR fallback exhausted); primary child error: "
                        f"{primary.code.value}: {primary.detail}"
                    ),
                    data=_early_data,
                    meta=_early_meta,
                )
            return AdapterResult.failed(
                code=ErrorCode.UPSTREAM_ERROR,
                detail="no 10-K/10-Q/20-F/6-K filings found for ticker "
                       "(ADR fallback exhausted)",
                source="fetch._fetch_filing_data_impl",
                retryable=True,
                data=_early_data,
                meta=_early_meta,
            )

        # Extract 20-F content and map to 10-K equivalents
        if summary["latest_20f"]:
            filing_20f = summary["latest_20f"]
            report_date = filing_20f.get("report_date", "")  # fail-open-ok: missing_report_date guard follows (P0-c/HIGH-3 parity)
            # Removed silent calendar-year fallback. Previously: when SEC
            # submissions / API / FMP didn't return a report_date, fetch
            # inferred year from datetime.now().year - 1. 20-F is used
            # exclusively by foreign private issuers — exactly the ADRs
            # where fiscal-year != calendar-year is most common (e.g.
            # Nokia/TSMC March/December cross-year splits). Now: fail-closed
            # — skip the 20-F block entirely so summary["latest_10k"] stays
            # unset and the required-item check downstream flags it as missing.
            # Mirrors the 10-K/10-Q blocks (HIGH-3 parity).
            # ISS-220 SF-C (Loop32 cycle 2): asymmetry vs 10-K/10-Q —
            # this guard didn't check `isinstance(report_date, str)` so
            # `len(123)` crashed with TypeError when upstream returned
            # a non-str. AND no YYYY-MM-DD format check, so malformed
            # `"2024-xx"` reached `int(report_date[:4])` later. Use
            # _is_valid_yyyy_mm_dd which does both type and format.
            missing_report_date = (
                not _is_valid_yyyy_mm_dd(report_date)
                or filing_20f.get("status") == "missing_report_date"
            )
            if missing_report_date:
                print(
                    "    [20-F] Skipping: upstream returned no report_date; "
                    "calendar-year guessing disabled (cross-fiscal-year unsafe)",
                    file=sys.stderr,
                )
                summary["validation"]["missing_items"].append(
                    "20-F:missing_report_date"
                )
                summary["latest_20f"] = None
                filing_20f = None

        if summary["latest_20f"]:
            filing_20f = summary["latest_20f"]
            report_date = filing_20f.get("report_date", "")  # fail-open-ok: missing_report_date guard above sets latest_20f=None (P0-c fix)
            year = int(report_date[:4])

            print("    Fetching 20-F items from SEC EDGAR...", file=sys.stderr)
            edgar_result = _track_child(fetch_filing_from_sec_edgar(
                filing_20f.get("url", ""), ticker, "20-F"
            ))
            edgar_items = edgar_result.data.get("items_metadata", {})
            edgar_content = edgar_result.data.get("content_dict", {})

            item_mapping = {
                "item_4": "item_1",
                "item_3": "item_1a",
                "item_5": "item_7",
            }
            content_mapping = {
                "20f_item_4": "10k_item_1",
                "20f_item_3": "10k_item_1a",
                "20f_item_5": "10k_item_7",
                "20f_revenue_notes": "10k_revenue_notes",
            }

            if edgar_items:
                mapped_items = {}
                for old_key, meta in edgar_items.items():
                    new_key = item_mapping.get(old_key, old_key)
                    mapped_items[new_key] = meta

                mapped_content = {}
                for old_key, text in edgar_content.items():
                    new_key = content_mapping.get(old_key, old_key)
                    mapped_content[new_key] = text

                summary["latest_10k"] = {
                    "report_period": report_date,
                    "filing_date": filing_20f.get("filing_date"),
                    "year": year,
                    "url": filing_20f.get("url", ""),
                    "accession_number": filing_20f.get("accession_number", ""),
                    "items": mapped_items,
                    "source": "sec_edgar_20f",
                    "original_filing_type": "20-F",
                }
                content_dict.update(mapped_content)

                for item_key, item_meta in mapped_items.items():
                    if item_key != "revenue_notes":
                        summary["validation"]["10k_items_status"][item_key] = {
                            "chars": item_meta.get("total_chars", 0),
                            "valid": item_meta.get("total_chars", 0)
                            >= MIN_FILING_ITEM_CHARS,
                        }
            else:
                # ISS-102 (Loop8): the FD `/filings/items` endpoint that
                # `fetch_filing_items_from_api` queries only ships 10-K
                # and 10-Q items — calling with `"20-F"` immediately
                # returns NOT_FOUND. The mapping logic below was
                # designed for a hypothetical 20-F-aware API path that
                # never shipped. Keep the call but log the limitation
                # explicitly (still tracked via _track_child so the
                # filing envelope's child severity sees the NOT_FOUND).
                print(
                    "    20-F content extraction failed; API fallback for "
                    "20-F not supported by upstream (calls return "
                    "NOT_FOUND but error gets tracked for severity)...",
                    file=sys.stderr,
                )
                # API fallback for 20-F (no upstream support; tracked NOT_FOUND)
                api_result = _track_child(fetch_filing_items_from_api(
                    ticker, "20-F", year
                ))
                api_items = api_result.data.get("items_metadata", {})
                api_content = api_result.data.get("content_dict", {})
                if api_content:
                    mapped_api_content = {}
                    mapped_api_items = {}
                    for old_key, text in api_content.items():
                        new_key = content_mapping.get(old_key, old_key)
                        mapped_api_content[new_key] = text
                        mapped_api_items[new_key] = {"total_chars": len(text)}
                    content_dict.update(mapped_api_content)
                    # Build filing summary (same structure as direct-EDGAR path)
                    summary["latest_10k"] = {
                        "report_period": report_date,
                        "filing_date": filing_20f.get("filing_date"),
                        "year": year,
                        "url": filing_20f.get("url", ""),
                        "accession_number": filing_20f.get("accession_number", ""),
                        "items": mapped_api_items,
                        "source": "api_fallback_20f",
                        "original_filing_type": "20-F",
                    }
                    for ik, im in mapped_api_items.items():
                        if ik != "revenue_notes":
                            summary["validation"]["10k_items_status"][ik] = {
                                "chars": im.get("total_chars", 0),
                                "valid": im.get("total_chars", 0) >= MIN_FILING_ITEM_CHARS,
                            }
                    print(
                        f"    20-F API fallback: {len(mapped_api_content)} items",
                        file=sys.stderr,
                    )
        else:
            print(
                "    6-K found but no 20-F - metadata only",
                file=sys.stderr,
            )

    # Step 3: Fetch 10-K items from SEC EDGAR
    if latest_10k:
        report_date = latest_10k.get("report_date", "")  # fail-open-ok: missing_report_date guard follows (I2/HIGH-3 fix)
        # Removed silent calendar-year fallback. Previously: when SEC
        # submissions / API / FMP didn't return a report_date, fetch
        # inferred year from datetime.now().year - 1. This broke on any
        # company whose fiscal year != calendar year (TTDKY fiscal-March,
        # Apple fiscal-September, ...). Now: fail-closed — skip the 10-K
        # block entirely so summary["latest_10k"] stays unset and the
        # required-item check downstream flags it as missing.
        # Mirrors the 10-Q block (HIGH-3 parity).
        # ISS-154 (Loop17 cycle 1 fresh-session-4) + ISS-220 SF-C
        # (Loop32 cycle 2): require report_date to be a valid
        # YYYY-MM-DD str. Pre-Loop17 a drifted upstream `report_date=123`
        # crashed at len() (fixed via isinstance guard). Pre-Loop32 a
        # malformed `"2024-xx"` slipped past `len >= 7` and crashed at
        # `int(report_date[:4])` 13 lines down. _is_valid_yyyy_mm_dd
        # does type + format in one call.
        missing_report_date = (
            not _is_valid_yyyy_mm_dd(report_date)
            or latest_10k.get("status") == "missing_report_date"
        )
        if missing_report_date:
            print(
                "    [10-K] Skipping: upstream returned no report_date; "
                "calendar-year guessing disabled (cross-fiscal-year unsafe)",
                file=sys.stderr,
            )
            summary["validation"]["missing_items"].append(
                "10-K:missing_report_date"
            )
            latest_10k = None

    if latest_10k:
        year = int(report_date[:4])
        accession_10k = latest_10k.get("accession_number", "")

        primary_url = latest_10k.get("url", "")
        print("    Fetching 10-K items from SEC EDGAR...", file=sys.stderr)
        edgar_result = _track_child(fetch_filing_from_sec_edgar(
            primary_url, ticker, "10-K"
        ))
        edgar_items = edgar_result.data.get("items_metadata", {})
        edgar_content = edgar_result.data.get("content_dict", {})

        source = "sec_edgar_direct"
        if not edgar_items:
            if fmp_api_key:
                fmp_results = _fmp_metadata(ticker, "10-K", limit=1)
                if fmp_results:
                    fmp_url = fmp_results[0].get("finalLink", "")
                    if fmp_url and fmp_url != primary_url:
                        print(
                            f"    Trying FMP alternative URL: {fmp_url[:80]}...",
                            file=sys.stderr,
                        )
                        edgar_result = _track_child(fetch_filing_from_sec_edgar(
                            fmp_url, ticker, "10-K"
                        ))
                        edgar_items = edgar_result.data.get("items_metadata", {})
                        edgar_content = edgar_result.data.get("content_dict", {})
                        if edgar_items:
                            source = "sec_edgar_via_fmp"

            if not edgar_items:
                print(
                    "    SEC EDGAR extraction failed, trying API /filings/items fallback...",
                    file=sys.stderr,
                )
                api_result = _track_child(fetch_filing_items_from_api(
                    ticker, "10-K", year
                ))
                edgar_items = api_result.data.get("items_metadata", {})
                edgar_content = api_result.data.get("content_dict", {})
                if edgar_items:
                    source = "api_filings_items"

        filing_date = _fmp_filing_date(ticker, "10-K", accession_10k)
        if filing_date:
            print(f"    10-K filing_date: {filing_date}", file=sys.stderr)

        summary["latest_10k"] = {
            "report_period": report_date,
            "filing_date": filing_date or latest_10k.get("filing_date"),
            "year": year,
            "url": latest_10k.get("url", ""),
            "accession_number": accession_10k,
            "items": edgar_items,
            "source": source,
        }
        content_dict.update(edgar_content)

        for item_key, item_meta in edgar_items.items():
            if item_key != "revenue_notes":
                summary["validation"]["10k_items_status"][item_key] = {
                    "chars": item_meta.get("total_chars", 0),
                    "valid": item_meta.get("total_chars", 0)
                    >= MIN_FILING_ITEM_CHARS,
                }

    # Check for missing required 10-K items
    if summary["latest_10k"]:
        for required_item in REQUIRED_10K_ITEMS:
            if required_item not in summary["latest_10k"].get("items", {}):
                summary["validation"]["missing_items"].append(
                    f"10-K:{required_item}"
                )
            elif (
                summary["latest_10k"]["items"][required_item].get(
                    "total_chars", 0
                )
                < MIN_FILING_ITEM_CHARS
            ):
                summary["validation"]["missing_items"].append(
                    f"10-K:{required_item}(insufficient content)"
                )

    # Step 4: Fetch 10-Q items
    if latest_10q:
        report_date = latest_10q.get("report_date", "")  # fail-open-ok: missing_report_date guard follows (Task 0.2/HIGH-3 fix)
        # Removed silent calendar-quarter fallback. Previously: when SEC
        # submissions / API / FMP didn't return a report_date, fetch
        # inferred year/quarter from the current calendar. This broke on
        # any company whose fiscal year != calendar year (TTDKY fiscal-
        # March, Apple fiscal-September, ...). Now: fail-closed — skip
        # the 10-Q block entirely so summary["latest_10q"] stays unset
        # and the required-item check downstream flags it as missing.
        # ISS-154 (Loop17) + ISS-220 SF-C (Loop32 cycle 2): require
        # YYYY-MM-DD via shared helper (type + format in one call).
        missing_report_date = (
            not _is_valid_yyyy_mm_dd(report_date)
            or latest_10q.get("status") == "missing_report_date"
        )
        if missing_report_date:
            print(
                "    [10-Q] Skipping: upstream returned no report_date; "
                "calendar-quarter guessing disabled (cross-fiscal-year unsafe)",
                file=sys.stderr,
            )
            summary["validation"]["missing_items"].append(
                "10-Q:missing_report_date"
            )
            latest_10q = None

    if latest_10q:
        calendar_year = int(report_date[:4])
        calendar_month = int(report_date[5:7])
        calendar_quarter = (calendar_month - 1) // 3 + 1
        accession_10q = latest_10q.get("accession_number", "")

        primary_url = latest_10q.get("url", "")
        print("    Fetching 10-Q items from SEC EDGAR...", file=sys.stderr)
        edgar_result = _track_child(fetch_filing_from_sec_edgar(
            primary_url, ticker, "10-Q"
        ))
        edgar_items = edgar_result.data.get("items_metadata", {})
        edgar_content = edgar_result.data.get("content_dict", {})

        source = "sec_edgar_direct"
        if not edgar_items:
            if fmp_api_key:
                fmp_results = _fmp_metadata(ticker, "10-Q", limit=1)
                if fmp_results:
                    fmp_url = fmp_results[0].get("finalLink", "")
                    if fmp_url and fmp_url != primary_url:
                        print(
                            f"    Trying FMP alternative URL: {fmp_url[:80]}...",
                            file=sys.stderr,
                        )
                        edgar_result = _track_child(fetch_filing_from_sec_edgar(
                            fmp_url, ticker, "10-Q"
                        ))
                        edgar_items = edgar_result.data.get("items_metadata", {})
                        edgar_content = edgar_result.data.get("content_dict", {})
                        if edgar_items:
                            source = "sec_edgar_via_fmp"

            if not edgar_items:
                print(
                    "    SEC EDGAR extraction failed, trying API /filings/items fallback...",
                    file=sys.stderr,
                )
                api_result = _track_child(fetch_filing_items_from_api(
                    ticker, "10-Q", calendar_year, calendar_quarter
                ))
                edgar_items = api_result.data.get("items_metadata", {})
                edgar_content = api_result.data.get("content_dict", {})
                if edgar_items:
                    source = "api_filings_items"

        if edgar_items:
            filing_date = _fmp_filing_date(ticker, "10-Q", accession_10q)
            if filing_date:
                print(f"    10-Q filing_date: {filing_date}", file=sys.stderr)

            summary["latest_10q"] = {
                "report_period": report_date,
                "filing_date": filing_date or latest_10q.get("filing_date"),
                "year": calendar_year,
                "quarter": calendar_quarter,
                "url": latest_10q.get("url", ""),
                "accession_number": accession_10q,
                "items": edgar_items,
                "source": source,
            }
            content_dict.update(edgar_content)

            for item_key, item_meta in edgar_items.items():
                if item_key != "revenue_notes":
                    summary["validation"]["10q_items_status"][item_key] = {
                        "chars": item_meta.get("total_chars", 0),
                        "valid": item_meta.get("total_chars", 0)
                        >= MIN_FILING_ITEM_CHARS,
                    }

    # Check for missing required 10-Q items
    if summary["latest_10q"]:
        for required_item in REQUIRED_10Q_ITEMS:
            if required_item not in summary["latest_10q"].get("items", {}):
                summary["validation"]["missing_items"].append(
                    f"10-Q:{required_item}"
                )
            elif (
                summary["latest_10q"]["items"][required_item].get(
                    "total_chars", 0
                )
                < MIN_FILING_ITEM_CHARS
            ):
                summary["validation"]["missing_items"].append(
                    f"10-Q:{required_item}(insufficient content)"
                )

    # Determine status
    has_valid_10k = (
        summary["latest_10k"]
        and summary["latest_10k"].get("items")
        and all(
            item in summary["latest_10k"]["items"]
            for item in REQUIRED_10K_ITEMS
        )
        and all(
            summary["latest_10k"]["items"][item].get("total_chars", 0)
            >= MIN_FILING_ITEM_CHARS
            for item in REQUIRED_10K_ITEMS
        )
    )

    has_valid_10q = (
        summary["latest_10q"]
        and summary["latest_10q"].get("items")
        and all(
            item in summary["latest_10q"]["items"]
            for item in REQUIRED_10Q_ITEMS
        )
        and all(
            summary["latest_10q"]["items"][item].get("total_chars", 0)
            >= MIN_FILING_ITEM_CHARS
            for item in REQUIRED_10Q_ITEMS
        )
    )

    missing_count = len(summary["validation"]["missing_items"])

    if has_valid_10k and has_valid_10q and missing_count == 0:
        status = "PASSED"
    elif has_valid_10k and has_valid_10q and missing_count <= 1:
        status = "PARTIAL"
    elif has_valid_10k or summary["latest_10k"] or summary["latest_10q"]:
        status = "INCOMPLETE"
    else:
        status = "FAILED"

    print(
        f"    Filing validation: 10-K={has_valid_10k}, 10-Q={has_valid_10q}, "
        f"missing={summary['validation']['missing_items']}",
        file=sys.stderr,
    )

    # Map legacy 4-state to AdapterResult with meta side-channel for
    # INCOMPLETE preservation (envelope natively has 3 states).
    data = {"summary": summary, "content": content_dict}
    meta = {"source_hint": "filing_composite",
            "filing_status_legacy": status}
    src = "fetch._fetch_filing_data_impl"
    if status == "PASSED":
        return AdapterResult.passed(data=data, meta=meta)

    # ISS-051 (Loop3) + ISS-074 (Loop5): pick highest-severity child
    # error using centralized `severity_of_error` so filing/financials/
    # historical rank identically.
    from scripts.sources.adapter_result import severity_of_error
    if child_errors:
        primary = min(child_errors, key=severity_of_error)
        primary_code = primary.code
        primary_detail = (
            f"{status}: missing_count={missing_count}, "
            f"10K_valid={has_valid_10k}, 10Q_valid={has_valid_10q}; "
            f"primary child error: {primary.code.value}: {primary.detail}"
        )
        primary_retryable = primary.retryable
        primary_upstream = primary.upstream_status
    else:
        primary_code = ErrorCode.UPSTREAM_ERROR
        primary_detail = (
            f"{status}: missing_count={missing_count}, "
            f"10K_valid={has_valid_10k}, 10Q_valid={has_valid_10q}"
        )
        primary_retryable = True
        primary_upstream = None

    meta_with_children = dict(meta)
    if child_errors:
        meta_with_children["child_error_count"] = len(child_errors)

    # ISS-220 SF-A (Loop32 cycle 2): preserve full primary child metadata
    # via AdapterError.from_child_fields. Pre-fix the aggregator only
    # copied `code/detail/source/retryable/upstream_status`, dropping
    # `cause` and `shape_errors` — codex loop32 found the gap. When
    # there are no child errors (all-missing case), fall back to the
    # plain-constructor path; from_child_fields requires a primary.
    if child_errors:
        agg_error = AdapterError.from_child_fields(
            primary=primary,
            code=primary_code,
            source=src,
            detail=primary_detail,
            retryable=primary_retryable,
        )
    else:
        agg_error = AdapterError(
            code=primary_code,
            detail=primary_detail,
            source=src,
            retryable=primary_retryable,
            upstream_status=primary_upstream,
        )

    if status in ("PARTIAL", "INCOMPLETE"):
        return AdapterResult.partial(
            data=data,
            error=agg_error,
            meta=meta_with_children,
        )
    # FAILED — re-emit via failed_from_child for full metadata
    # preservation. agg_error already carries all child diagnostic
    # fields (built via from_child_fields above).
    return AdapterResult.failed_from_child(
        agg_error,
        source=src,
        detail=agg_error.detail or "both 10-K and 10-Q missing",
        data=data,
        meta=meta_with_children,
    )


# ---------------------------------------------------------------------------
# Beta fetch — yfinance lookup + price-history fallback
# ---------------------------------------------------------------------------

def _compute_beta_from_returns(
    daily_prices: list,
    market_prices: list,
) -> Optional[float]:
    """Compute beta from daily price returns via covariance/variance.

    Both inputs are lists of dicts with 'close' field, oldest-first.
    Returns None if insufficient data or computation fails.
    """
    if len(daily_prices) < 60 or len(market_prices) < 60:
        return None

    # Build date-indexed return maps
    def _returns(prices):
        out = {}
        for i in range(1, len(prices)):
            prev_close = prices[i - 1].get("close")
            cur_close = prices[i].get("close")
            date = prices[i].get("date") or prices[i].get("time", "")
            date = str(date)[:10]
            # Strict numeric check — truthy-string close (e.g. "N/A") would
            # pass the `and` guard and crash the subtraction with TypeError.
            # Same upstream-contamination pattern as historical_multiples.py:116.
            if (
                isinstance(prev_close, (int, float))
                and not isinstance(prev_close, bool)
                and isinstance(cur_close, (int, float))
                and not isinstance(cur_close, bool)
                and prev_close > 0
                and date
            ):
                out[date] = (cur_close - prev_close) / prev_close
        return out

    stock_ret = _returns(daily_prices)
    mkt_ret = _returns(market_prices)

    # Align on common dates
    common = sorted(set(stock_ret) & set(mkt_ret))
    if len(common) < 40:
        return None

    s = [stock_ret[d] for d in common]
    m = [mkt_ret[d] for d in common]

    n = len(common)
    mean_s = sum(s) / n
    mean_m = sum(m) / n
    cov = sum((s[i] - mean_s) * (m[i] - mean_m) for i in range(n)) / (n - 1)
    var_m = sum((m[i] - mean_m) ** 2 for i in range(n)) / (n - 1)

    if var_m == 0:
        return None

    return round(cov / var_m, 3)


def _fetch_beta(ticker: str, historical_data: dict) -> dict:
    """Fetch equity beta: yfinance first, then compute from price history.

    Returns a dict with value, source, and any warnings.
    """
    result: dict = {"value": None, "source": None, "warnings": []}

    try:
        import yfinance as _yf
    except ImportError:
        result["source"] = "unavailable"
        result["warnings"].append("yfinance not installed — beta unavailable")
        return result

    # ISS-145 (Loop15 cycle 1 fresh-session-2): validate ticker before
    # the yfinance call below. yfinance interpolates the ticker into
    # URL paths; without this guard a malformed CLI / portfolio-config
    # ticker (e.g. "../path", whitespace, percent-encoded chars) reaches
    # yfinance HTTP layer. adr/detect.detect_adr_market_data already
    # follows this pattern; beta path was missed.
    from scripts.sources.yfinance_guard import (
        validate_yfinance_ticker as _validate_yf_ticker,
        InvalidTickerError as _InvalidTickerError,
    )
    try:
        ticker = _validate_yf_ticker(ticker)
    except _InvalidTickerError as _ite:
        result["source"] = "unavailable"
        result["warnings"].append(
            f"yfinance ticker rejected by validator: {_ite}"
        )
        return result

    # ISS-146 (Loop15 cycle 1 fresh-session-2): _yfinance_safe_msg sanitizes
    # yfinance exception text (cookies, crumbs, local cache paths, URLs).
    # Other yfinance call sites already use this; _fetch_beta's two
    # warning paths were leaking raw exception text into 01_price_data.json.
    from scripts.sources.yahoo_finance import _yfinance_safe_msg

    # ISS-180 (Loop24 cycle 1 fresh-session-11): finite-numeric guard.
    # Pre-fix `isinstance(yf_beta, (int, float)) and yf_beta > 0`
    # accepted bool (True > 0 is True; bool is int subclass) coercing
    # to 1.0, AND accepted float('inf') / float('nan') (NaN > 0 is
    # False but NaN bypassed via != comparison drift). Save_json then
    # uses default allow_nan=True so Inf would land as literal
    # "Infinity" in 01_price_data.json. Reject bool + non-finite.
    import math as _math
    from scripts.sources.common import is_bool_like

    def _safe_finite_beta(v):
        if v is None or is_bool_like(v):
            return None
        if not isinstance(v, (int, float)):
            return None
        if not _math.isfinite(v):
            return None
        if v <= 0:
            return None
        return float(v)

    # Attempt 1: yfinance .info.beta
    try:
        info = yfinance_call(lambda: _yf.Ticker(ticker).info)
        yf_beta = info.get("beta") if info else None
        safe = _safe_finite_beta(yf_beta)
        if safe is not None:
            result["value"] = round(safe, 3)
            result["source"] = "yfinance"
            return result
    except Exception as e:
        result["warnings"].append(
            f"yfinance .info.beta lookup failed: {_yfinance_safe_msg(e)}"
        )

    # Attempt 2: compute from daily price returns vs SPY
    daily_prices = historical_data.get("daily", [])
    if daily_prices and len(daily_prices) >= 60:
        try:
            dates = [p.get("date") or p.get("time", "") for p in daily_prices]
            dates = [str(d)[:10] for d in dates if d]
            if dates:
                start = min(dates)
                end = max(dates)
                spy = yfinance_call(lambda: _yf.download(
                    "SPY", start=start, end=end,
                    progress=False, auto_adjust=True,
                ))
                if spy is not None and len(spy) >= 40:
                    # ISS-180 (Loop24): finite-numeric guard on SPY closes
                    # too. Pre-fix `float(close_val)` would coerce bool to
                    # 1.0/0.0 and propagate NaN/Inf through to
                    # _compute_beta_from_returns, which only checked
                    # `isinstance(_, (int, float))` (no isfinite). Reject
                    # bool + non-finite at the conversion boundary; skip
                    # the bar if the close isn't a clean finite numeric.
                    spy_prices = []
                    for idx, row in spy.iterrows():
                        close_val = row.get("Close")
                        if hasattr(close_val, "item"):
                            close_val = close_val.item()
                        if close_val is None or is_bool_like(close_val):
                            continue
                        if not isinstance(close_val, (int, float)):
                            continue
                        if not _math.isfinite(close_val):
                            continue
                        spy_prices.append({
                            "date": str(idx.date()),
                            "close": float(close_val),
                        })
                    computed = _compute_beta_from_returns(daily_prices, spy_prices)
                    # ISS-180: also guard the computed beta against NaN
                    # propagation through the covariance math (zero variance
                    # path returns None, but pathological inputs can still
                    # yield non-finite outputs in edge cases).
                    if (
                        computed is not None
                        and isinstance(computed, (int, float))
                        and not is_bool_like(computed)
                        and _math.isfinite(computed)
                    ):
                        result["value"] = computed
                        result["source"] = "computed_from_daily_returns"
                        n_days = len(daily_prices)
                        if n_days < 250:
                            result["warnings"].append(
                                f"Only {n_days} trading days — beta unreliable "
                                f"(prefer 1Y+ / 250+ days)"
                            )
                        return result
        except Exception as e:
            # ISS-146 (Loop15): same _yfinance_safe_msg sanitization as
            # the .info.beta path above — yfinance download exceptions
            # can also embed cookies/crumbs/cache-paths.
            result["warnings"].append(
                f"Beta computation failed: {_yfinance_safe_msg(e)}"
            )

    result["value"] = None
    result["source"] = "unavailable"
    result["warnings"].append(
        "No beta available — yfinance returned None and insufficient "
        "price history for computation"
    )
    return result


# ---------------------------------------------------------------------------
# DL2 T23: _derive_category_status — single synthesis point
# ---------------------------------------------------------------------------

# v18 widened Literal: filing consolidation passes legacy 4-state strings
# via override_status to preserve INCOMPLETE (which AdapterResult cannot
# natively express). Previous {SKIPPED, CIRCUIT_BREAKER} Literal would be
# a type-narrowing bug for the filing case.
_OverrideStatus = Literal[
    "SKIPPED", "CIRCUIT_BREAKER",
    "PASSED", "PARTIAL", "INCOMPLETE", "FAILED",
]


def _derive_category_status(
    result=None,
    *,
    override_status: Optional[str] = None,
    reason: Optional[str] = None,
    extra_keys: Optional[Dict] = None,
) -> Dict:
    """Single synthesis point for category_statuses[category].

    Handles all 6 status values:
    - PASSED / PARTIAL / FAILED from `AdapterResult.status`
    - SKIPPED / CIRCUIT_BREAKER from fetch.py orchestration overrides
    - INCOMPLETE legacy 4-state passthrough via override_status side-channel

    Argument precedence (override-wins):
    - At least one of `result` or `override_status` is required.
    - When BOTH are non-None, `override_status` wins for the `status`
      field. `result.error` and `result.meta.truncated` are NOT
      propagated in that case — the override-status branch is a
      pure-orchestration write where the caller has already decided the
      status semantics. Callsites that want the AdapterResult's
      error_code/error_detail should pass `override_status=None` (and let
      the result branch run) or `override_status=result.status`
      explicitly. Real callsites in fetch.py (price-freshness,
      filing-consolidation) rely on this override-wins behavior.

    extra_keys: preserve pre-migration per-category custom keys (e.g.,
      daily_count/weekly_count for historical, article_count for news).
      Spread-merged LAST and CANNOT contain "status" key (raises
      ValueError to force explicit override_status path).
    reason: only populated when non-None AND override_status path taken.
    """
    if extra_keys is not None and "status" in extra_keys:
        raise ValueError(
            "_derive_category_status: 'status' in extra_keys is "
            "ambiguous; use override_status= instead"
        )
    # ISS-220 4.31 (Loop38 cycle 1, iter7): runtime allowlist guard.
    # `override_status` is typed as Optional[str] (not Literal) for
    # signature ergonomics, but the documented contract is the
    # _OverrideStatus enum. Pre-fix a typo would silently emit a
    # malformed status value into category_statuses → downstream
    # consumers iterating `non_critical_keys` couldn't recognize the
    # status. Reject at function entry so misuse fails loudly.
    if override_status is not None and override_status not in (
        "SKIPPED", "CIRCUIT_BREAKER",
        "PASSED", "PARTIAL", "INCOMPLETE", "FAILED",
    ):
        raise ValueError(
            f"_derive_category_status: override_status must be one of "
            f"_OverrideStatus literal values; got {override_status!r}"
        )
    if override_status is not None:
        entry: Dict = {"status": override_status}
        if reason is not None:
            entry["reason"] = reason
        if extra_keys:
            entry.update(extra_keys)
        return entry
    if result is None:
        raise ValueError(
            "_derive_category_status: at least one of `result` or "
            "`override_status` required"
        )
    entry = {"status": result.status}
    if result.error is not None:
        entry["error_code"] = result.error.code.value
        entry["error_detail"] = result.error.detail
    truncated = result.meta.get("truncated", False)
    if truncated:
        entry["truncated"] = truncated
    if extra_keys:
        entry.update(extra_keys)
    return entry


# ---------------------------------------------------------------------------
# DL2 T10: merge_fallback_outcome helper
# ---------------------------------------------------------------------------

# v4 correction (Codex Cycle 1 Phase 3d H2): pre-migration yfinance
# fallback wrote category_statuses for only 3 categories —
# `{financials, metrics, company}`. BUT the pre-migration
# `yfinance_summary["fills"]` dict legitimately contains a 4th key
# `"analyst"` (analyst estimates fills), preserved in
# `YfinanceFallbackOutcome.fills` for faithful `run_meta.json`
# serialization via `to_run_meta_dict()`.
#
# Three sets:
#   - _YF_FALLBACK_MERGE_SET: writes category_statuses. 3 keys.
#   - _YF_FALLBACK_EXPECTED_NO_MERGE_SET: expected in fills for
#     run_meta but must NOT mutate category_statuses (currently {analyst}).
#   - Anything else: truly unexpected → fail loudly (producer bug).

_YF_FALLBACK_MERGE_SET = frozenset({"financials", "metrics", "company"})
_YF_FALLBACK_EXPECTED_NO_MERGE_SET = frozenset({"analyst"})


def merge_fallback_outcome(
    category_statuses: dict,
    outcome,
) -> None:
    """Single entry point for applying yfinance fallback fills to
    category_statuses. Replaces the cross-module mutation previously
    in yahoo_finance.py:_run_yfinance_fallback_impl.

    Merges `category_statuses` for exactly
    `{financials, metrics, company}` (pre-migration mutation surface).
    Silently skips expected-but-non-mutating fills (currently
    `{analyst}` — analyst fills appear in run_meta via
    `to_run_meta_dict()` but the pre-migration code path does NOT
    modify category_statuses for analyst).

    Raises AssertionError only for TRULY unknown fill keys — those in
    neither set. Such a key is a producer contract violation.

    Preserves pre-existing keys in each category dict via spread-first
    merge (spec §What does NOT change enumerated fields).
    """
    known = _YF_FALLBACK_MERGE_SET | _YF_FALLBACK_EXPECTED_NO_MERGE_SET
    for category, update in outcome.fills.items():
        if category not in known:
            raise AssertionError(
                f"merge_fallback_outcome: unknown fill category "
                f"{category!r}. Known: "
                f"merge={sorted(_YF_FALLBACK_MERGE_SET)}, "
                f"no-merge={sorted(_YF_FALLBACK_EXPECTED_NO_MERGE_SET)}. "
                f"New categories require updating this contract + "
                f"Slice-7 snapshot (if mutating) in the same commit."
            )
        if category not in _YF_FALLBACK_MERGE_SET:
            continue
        if update.filled and update.category_status is not None:
            existing = category_statuses.get(category, {})
            merged = {
                **existing,
                **update.category_status,
            }
            # 2026-05-29 dual-API (codex): preserve FMP provenance. The FMP
            # fallback runs BEFORE yfinance; if it already filled this
            # category, yfinance may only have patched residual gaps (e.g.
            # a null P/E that re-triggers `metrics_empty`). Don't relabel the
            # primary source — keep `data_source="fmp_fallback"`. Symmetric to
            # the analyst-status guard in _main_impl.
            if existing.get("data_source") == "fmp_fallback":
                merged["data_source"] = "fmp_fallback"
            # ISS-054 (Loop3 backlog): when fallback flips status to
            # PASSED, drop stale error_code/error_detail from the
            # pre-existing failed envelope. Otherwise the merged dict
            # has `status="PASSED"` alongside `error_code="rate_limit"`
            # — contradictory observability state.
            if merged.get("status") == "PASSED":
                merged.pop("error_code", None)
                merged.pop("error_detail", None)
            category_statuses[category] = merged


# ---------------------------------------------------------------------------
# DL3a T10: _reconcile_financials_currency helper
# ---------------------------------------------------------------------------


def _reconcile_financials_currency(financial_output: dict) -> None:
    """§3.2 row 2 reconciliation — covers None AND UNKNOWN per FIX-4.1.

    Normalizes per-row currency fields in place (None / UNKNOWN → stays
    sentinel), then asserts currency consistency across all statement rows.

    Raises SchemaError if:
    - Two known ISO codes disagree (e.g. USD vs JPY in same file)
    - Known ISO co-exists with any non-ISO sentinel (None or UNKNOWN)
    """
    from scripts.sources.common import normalize_currency
    from scripts.schemas.errors import SchemaError

    stmt_lists = [
        financial_output.get("income_statements", []) or [],
        financial_output.get("balance_sheets", []) or [],
        financial_output.get("cash_flows", []) or [],
    ]

    # Normalize each row's currency in place
    for stmt_list in stmt_lists:
        for row in stmt_list:
            if isinstance(row, dict):
                row["currency"] = normalize_currency(row.get("currency"))

    all_observed = {
        row.get("currency")
        for stmt_list in stmt_lists
        for row in stmt_list
        if isinstance(row, dict)
    }
    known_iso = {c for c in all_observed if c is not None and c != "UNKNOWN"}
    if len(known_iso) > 1:
        raise SchemaError(
            "02_financial_data.json", "currency",
            f"row currency disagreement: {sorted(known_iso)}"
        )
    non_iso_sentinels = {c for c in all_observed if c is None or c == "UNKNOWN"}
    if known_iso and non_iso_sentinels:
        raise SchemaError(
            "02_financial_data.json", "currency",
            f"row currency mixed known {sorted(known_iso)} with non-ISO sentinels "
            f"{sorted((s if s is not None else 'None') for s in non_iso_sentinels)} — "
            f"row[0] consumer gate insufficient (loop3 cycle-4 FIX-4.1)"
        )

    # NOTE: mixed-currency detection + repair does NOT run here. The mix is
    # created LATER (the H-block's compute_adr_valuation_correction runs DL3c
    # apply_fx_conversion in place, converting only the 12-field master set to
    # USD). Running detect here would see the still-consistent pre-H-block
    # statement and miss it. The detect+repair therefore runs at the
    # financial_output save boundary — see `_repair_financials_currency_marker`.


# ---------------------------------------------------------------------------
# _main_impl -- core orchestration
# ---------------------------------------------------------------------------

def _main_impl(
    args: argparse.Namespace,
    fetch_filing_data_fn: Optional[Callable] = None,
    run_yfinance_fallback_fn: Optional[Callable] = None,
    fetch_filing_metadata_from_fmp_fn: Optional[Callable] = None,
    run_fmp_fallback_fn: Optional[Callable] = None,
) -> int:
    """Core implementation accepting injected callables.

    Args:
        args: Parsed CLI arguments (from parse_args()).
        fetch_filing_data_fn: Filing data fetcher returning AdapterResult
            with ``data={"summary": ..., "content": ...}`` and
            ``meta["filing_status_legacy"]`` (legacy 4-state). Signature:
            ``(ticker, is_adr=False, ...) -> AdapterResult``. Callers
            consume ``filing_result.data / .meta / .status`` (NOT
            unpacked tuple — pre-DL2 the contract was a 3-tuple, but
            DL2 migration moved this to AdapterResult; ISS-220 Loop40
            Arch-1 docstring sync).
        run_yfinance_fallback_fn: yfinance fallback orchestrator.
            Signature: ``(ticker, financials, metrics, company, analyst)
            -> YfinanceFallbackOutcome``. Pre-DL2 took a 6th
            ``category_statuses`` parameter; DL2 refactor (loop20
            Task 10) removed cross-module mutation — now returns
            structured outcome via ``merge_fallback_outcome``.
        fetch_filing_metadata_from_fmp_fn: FMP metadata fetcher (reserved for
            ``_fetch_filing_data_impl`` wiring).
        run_fmp_fallback_fn: FMP financial-data fallback orchestrator (2026-05-29
            dual-API integration). Signature: ``(ticker, *, financials_data,
            metrics_data, analyst_data, earnings_combined, want_financials,
            want_metrics, want_analyst, want_earnings) -> FmpFallbackOutcome``.
            Runs BEFORE the yfinance fallback so FMP (higher-quality, 8 clean
            quarters incl. standalone fiscal Q4) is preferred for the
            categories FDS starves; None disables it.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    # ------------------------------------------------------------------
    # Lazy imports -- keep module-level clean for testability
    # ------------------------------------------------------------------
    from scripts.sources.financial_datasets import (
        fetch_price_data,
        fetch_metrics_data,
        fetch_financial_statements,
        fetch_company_data,
        fetch_news_data,
        fetch_insider_data,
        fetch_analyst_estimates,
        fetch_earnings_snapshot,
        fetch_earnings_press_releases,
        fetch_institutional_ownership,
        fetch_interest_rates_snapshot,
        fetch_interest_rates_historical,
        fetch_segmented_revenues,
    )
    from scripts.sources.yahoo_finance import fetch_historical_prices
    from scripts.adr.detect import (
        detect_adr_market_data,
    )
    from scripts.adr.correct import (
        compute_adr_valuation_correction,
        compute_adr_eps_check,
    )
    from scripts.adr.profile import resolve_adr_profile
    from scripts.schemas.adr_profile import AdrProfile
    from scripts.schemas.errors import SchemaError
    from scripts.normalize import (
        validate_price_freshness,
        validate_price_range,
        validate_financial_freshness,
        validate_fiscal_period_format,
        validate_eps_consistency,
    )

    # ------------------------------------------------------------------
    # 0. Setup
    # ------------------------------------------------------------------
    ticker = args.ticker.upper()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.system_date:
        try:
            system_date = datetime.strptime(args.system_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            print(
                f"fetch: invalid --system-date format '{args.system_date}' "
                f"(expected YYYY-MM-DD, e.g. 2026-03-22)",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        system_date = datetime.now(timezone.utc)

    system_date_str = system_date.strftime("%Y-%m-%d")

    print(f"{'=' * 60}", file=sys.stderr)
    print(f"Comprehensive Data Fetch (v7): {ticker}", file=sys.stderr)
    print(f"System Date: {system_date_str}", file=sys.stderr)
    print(f"Output Directory: {output_dir}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)

    category_statuses: Dict = {}
    circuit_breaker_triggered = False
    circuit_breaker_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Delta-era: category gating (subset fetch)
    # ------------------------------------------------------------------
    requested_categories = None
    if getattr(args, "categories", None):
        requested_categories = set(
            c.strip() for c in args.categories.split(",") if c.strip()
        )
        # DL7 #2 — fail-open-silent-category-gating: reject unknown values
        # before any category-gated work begins. Empty-after-strip (e.g.
        # `--categories ""`) parses to an empty set and falls through to the
        # "fetch all" path below (requested_categories left as the empty set
        # would mean "fetch nothing" — preserve legacy "fetch all" by resetting
        # to None when the user supplied an effectively-empty arg).
        if not requested_categories:
            requested_categories = None
        else:
            unknown = requested_categories - KNOWN_CATEGORIES
            if unknown:
                print(
                    f"FATAL: unknown categories: {sorted(unknown)}. "
                    f"Known: {sorted(KNOWN_CATEGORIES)}",
                    file=sys.stderr,
                )
                sys.exit(2)

    def _should_fetch(cat_prefix: str) -> bool:
        if requested_categories is None:
            return True
        return cat_prefix in requested_categories

    def _inputs_fetched(statuses: Dict, *needed: str) -> bool:
        """True iff every needed category was actually attempted (not SKIPPED)."""
        for cat in needed:
            st = statuses.get(cat, {}).get("status")
            if st in (None, "SKIPPED"):
                return False
        return True

    # Defaults for variables that may be skipped; tail writes use these when
    # a category is not fetched (and only when the output file is absent).
    price_data: Dict = {}
    price_status = "SKIPPED"
    historical_data: Dict = {}
    historical_status = "SKIPPED"
    metrics_data: Dict = {}
    metrics_status = "SKIPPED"
    financials_data: Dict = {}
    financials_status = "SKIPPED"
    company_data: Dict = {}
    company_status = "SKIPPED"
    is_adr = False
    sector = ""
    is_tech_stock = False
    news_data: Dict = {}
    news_status = "SKIPPED"
    filing_summary: Dict = {}
    filing_content: Dict = {}
    filing_status = "SKIPPED"
    segmented_data: Dict = {}
    segmented_status = "SKIPPED"
    insider_data: Dict = {}
    insider_status = "SKIPPED"
    analyst_data: Dict = {}
    analyst_status = "SKIPPED"
    earnings_data: Dict = {}
    earnings_status = "SKIPPED"
    press_data: Dict = {}
    press_status = "SKIPPED"
    earnings_combined: Dict = {
        "earnings": {},
        "press_releases": [],
        "press_releases_count": 0,
    }
    institutional_data: Dict = {}
    institutional_status = "SKIPPED"
    macro_rates_data: Dict = {
        "current_rates": [],
        "fed_history": [],
        "fed_history_count": 0,
    }
    rates_snap_status = "SKIPPED"
    rates_hist_status = "SKIPPED"

    # ==================================================================
    # Category A2: Historical Prices (fetched FIRST -- 6mo chart contains
    # meta block reused by Category A to avoid a redundant 5d API call)
    # ==================================================================
    _raw_daily_chart = None
    if _should_fetch("01_price_data"):
        print("\n[A2] Fetching Historical Prices...", file=sys.stderr)
        historical_result = fetch_historical_prices(ticker)
        historical_data = historical_result.data.get("result", {})
        _raw_daily_chart = historical_result.data.get("raw_daily_chart", {})
        historical_status = historical_result.status
        category_statuses["historical"] = _derive_category_status(
            historical_result,
            extra_keys={
                "daily_count": historical_data.get("daily_count", 0),
                "weekly_count": historical_data.get("weekly_count", 0),
                "has_sma_20": "sma_20" in historical_data,
                "has_sma_50": "sma_50" in historical_data,
            },
        )
        print(
            f"    Status: {historical_status} "
            f"(Daily: {historical_data.get('daily_count', 0)}, "
            f"Weekly: {historical_data.get('weekly_count', 0)})",
            file=sys.stderr,
        )
    else:
        category_statuses["historical"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[A2] Historical Prices SKIPPED (not in --categories)", file=sys.stderr)

    # ==================================================================
    # Category A: Price Data (CRITICAL) -- reuse 6mo chart when available
    # ==================================================================
    if not _should_fetch("01_price_data"):
        category_statuses["price"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[A] Price Data SKIPPED (not in --categories)", file=sys.stderr)
    else:
        print("\n[A] Fetching Price Data...", file=sys.stderr)
        price_result = fetch_price_data(
            ticker,
            prefetched_chart=_raw_daily_chart if _raw_daily_chart else None,
        )
        price_data = price_result.data

        if price_result.status != "FAILED":
            freshness_status, time_diff, freshness_msg = validate_price_freshness(
                price_data, system_date
            )
            if freshness_status == "CIRCUIT_BREAKER":
                circuit_breaker_triggered = True
                circuit_breaker_reason = freshness_msg
                category_statuses["price"] = _derive_category_status(
                    override_status="CIRCUIT_BREAKER",
                    extra_keys={
                        "time_diff_days": time_diff,
                        "message": freshness_msg,
                    },
                )
            else:
                range_status, range_msg = validate_price_range(price_data)
                # ISS-009 (Cycle 4 backlog): map freshness "WARNING" → 6-state
                # vocabulary "PARTIAL" (data is fine but with caveats; the
                # original "WARNING" string is preserved in extra_keys for
                # observability). "FAILED" stays as-is — that maps to the
                # canonical FAILED state. Pre-fix, raw "WARNING" leaked into
                # category_statuses[*]["status"] which is documented as a
                # 6-value enum (SKIPPED/CIRCUIT_BREAKER/PASSED/PARTIAL/
                # INCOMPLETE/FAILED).
                if freshness_status == "WARNING" and price_result.status != "WARNING":
                    effective_status = "PARTIAL"
                elif freshness_status == "FAILED" and freshness_status != price_result.status:
                    effective_status = "FAILED"
                else:
                    effective_status = None
                category_statuses["price"] = _derive_category_status(
                    price_result,
                    override_status=effective_status,
                    extra_keys={
                        "time_diff_days": time_diff,
                        "freshness_status": freshness_status,
                        "freshness_message": freshness_msg,
                        "range_status": range_status,
                        "range_message": range_msg,
                    },
                )
        else:
            category_statuses["price"] = _derive_category_status(
                price_result,
                extra_keys={"message": "Price fetch failed"},
            )

        print(f"    Status: {category_statuses['price']['status']}", file=sys.stderr)

    # ------------------------------------------------------------------
    # CIRCUIT BREAKER check
    # ------------------------------------------------------------------
    if circuit_breaker_triggered:
        print(f"\n{'=' * 60}", file=sys.stderr)
        print("CIRCUIT BREAKER TRIGGERED!", file=sys.stderr)
        print(f"Reason: {circuit_breaker_reason}", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)

        validation = {
            "status": "CIRCUIT_BREAKER",
            "system_date": system_date_str,
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "is_adr": False,
            "is_tech_stock": False,
            "sector": None,
            "growth_stock_mode": None,
            "yfinance_fallback": None,
            "eps_consistency": None,
            "adr_valuation_correction": None,
            "circuit_breaker_reason": circuit_breaker_reason,
            "tier_decided": args.tier_decided,
            "categories": category_statuses,
        }
        save_json(validation, output_dir / "00_validation.json")
        # Save price data for post-mortem debugging (already fetched before breaker)
        save_json(
            {"snapshot": price_data, "historical": historical_data},
            output_dir / "01_price_data.json",
        )
        print(
            json.dumps(
                {"status": "CIRCUIT_BREAKER", "reason": circuit_breaker_reason}
            )
        )
        return 1

    # ==================================================================
    # Category B: Metrics Snapshot (CRITICAL)
    # ==================================================================
    if _should_fetch("02_financial_data"):
        print("\n[B] Fetching Metrics Snapshot...", file=sys.stderr)
        metrics_result = fetch_metrics_data(ticker)
        metrics_data = metrics_result.data
        category_statuses["metrics"] = _derive_category_status(metrics_result)
        print(f"    Status: {metrics_result.status}", file=sys.stderr)
    else:
        category_statuses["metrics"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[B] Metrics Snapshot SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category C: Financial Statements (CRITICAL)
    # ==================================================================
    if _should_fetch("02_financial_data"):
        print("\n[C] Fetching Financial Statements...", file=sys.stderr)
        financials_result = fetch_financial_statements(ticker)
        financials_data = financials_result.data
        # R15 + Codex review: guard against non-dict data; also fix status
        if not isinstance(financials_data, dict):
            financials_data = {}
        _inc = financials_data.get("income_statements", [])
        _bal = financials_data.get("balance_sheets", [])
        _cf = financials_data.get("cash_flows", [])
        category_statuses["financials"] = _derive_category_status(
            financials_result,
            extra_keys={
                "income_count": len(_inc),
                "balance_count": len(_bal),
                "cashflow_count": len(_cf),
                "latest_period": _inc[0].get("report_period") if _inc else None,
            },
        )
        print(
            f"    Status: {financials_result.status} "
            f"(Income: {len(_inc)}, "
            f"Balance: {len(_bal)}, "
            f"CashFlow: {len(_cf)})",
            file=sys.stderr,
        )
    else:
        category_statuses["financials"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[C] Financial Statements SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category D: Company Data (IMPORTANT)
    # ==================================================================
    # Company metadata travels with 03_company_news.json output file; gate
    # on that category prefix.
    if _should_fetch("03_company_news"):
        print("\n[D] Fetching Company Data...", file=sys.stderr)
        company_result = fetch_company_data(ticker)
        company_data = company_result.data
        is_adr = company_data.get("is_adr", False)
        sector = company_data.get("sector") or ""
        is_tech_stock = any(kw in sector for kw in ["Technology", "Semiconductors"])
        category_statuses["company"] = _derive_category_status(company_result)
        print(f"    Status: {company_result.status}", file=sys.stderr)
        if is_adr:
            print(
                f"    ADR detected: category='{company_data.get('category')}'",
                file=sys.stderr,
            )
        if is_tech_stock:
            print(f"    Tech stock detected: sector='{sector}'", file=sys.stderr)
    else:
        category_statuses["company"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[D] Company Data SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category E: News Data (IMPORTANT)
    # ==================================================================
    if _should_fetch("03_company_news"):
        print("\n[E] Fetching News Data...", file=sys.stderr)
        # Financial Datasets API caps news limit at 10. Clamp user input
        # to avoid HTTP 400 that silently turns into empty news_data and
        # (downstream) classifier_input_healthy=False → fail-open partial.
        _news_limit = min(args.news_limit, 10)
        if _news_limit != args.news_limit:
            print(f"    (clamped --news-limit {args.news_limit} → 10 per FD API cap)",
                  file=sys.stderr)
        news_result = fetch_news_data(ticker, limit=_news_limit)
        if not isinstance(news_result.data, dict):
            news_data = {}
        else:
            news_data = news_result.data
        category_statuses["news"] = _derive_category_status(
            news_result,
            extra_keys={
                "article_count": news_data.get("count", 0),
                "latest_date": news_data.get("latest_date"),
            },
        )
        print(
            f"    Status: {news_result.status} ({news_data.get('count', 0)} articles)",
            file=sys.stderr,
        )
    else:
        category_statuses["news"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[E] News Data SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category F: Filing Data (CRITICAL)
    # ==================================================================
    # 05_filing_* family (summary + content markdown + intelligence).
    _fetch_filing_category = any(
        _should_fetch(p) for p in (
            "05_filing_summary", "05_filing_content", "05_filing",
        )
    ) if requested_categories is not None else True
    if _fetch_filing_category:
        print("\n[F] Fetching Filing Data (CRITICAL)...", file=sys.stderr)
        if fetch_filing_data_fn is None:
            fetch_filing_data_fn = _fetch_filing_data_impl
        filing_result = fetch_filing_data_fn(ticker, is_adr=is_adr)
        filing_summary = filing_result.data.get("summary", {})
        filing_content = filing_result.data.get("content", {})
        # v17: read 4-state legacy status from meta side-channel
        # (envelope natively has 3 states; producer packs INCOMPLETE here).
        filing_status = filing_result.meta.get(
            "filing_status_legacy", filing_result.status,
        )
    else:
        print("\n[F] Filing Data SKIPPED", file=sys.stderr)
        filing_summary, filing_content, filing_status = {}, {}, "SKIPPED"
    if not isinstance(filing_summary, dict):
        filing_summary = {}
    if not isinstance(filing_content, dict):
        filing_content = {}

    # Filter non-string values to prevent len() crash on malformed adapter output
    filing_content = {k: v for k, v in filing_content.items() if isinstance(v, str)}
    total_filing_chars = sum(len(c) for c in filing_content.values())
    items_detail = {k: len(v) for k, v in filing_content.items()}
    # Reconcile filing_summary with what was actually persisted (authoritative)
    if isinstance(filing_summary.get("validation"), dict):
        filing_summary["validation"]["persisted_items"] = sorted(filing_content.keys())
        # Update missing_items to reflect only items missing from PERSISTED content
        configured_required = filing_summary["validation"].get("required_items", [])
        if configured_required:
            actual_missing = [
                item for item in configured_required
                if not any(item.lower() in k.lower() for k in filing_content)
            ]
            filing_summary["validation"]["missing_items"] = actual_missing
    # Prune summary filing-type entries if their content was filtered out
    for ftype_key in ("latest_10k", "latest_10q", "latest_20f", "latest_6k"):
        ftype_data = filing_summary.get(ftype_key)
        if isinstance(ftype_data, dict) and ftype_data.get("items"):
            prefix = ftype_key.replace("latest_", "").replace("-", "")
            has_any = any(k.startswith(prefix) for k in filing_content)
            if not has_any:
                ftype_data["items"] = {}
                ftype_data["_filtered"] = "content removed during sanitization"

    # Derive filing presence from PERSISTED content, not just summary metadata
    has_10k = bool(filing_summary.get("latest_10k")) and any(
        k.startswith("10k") for k in filing_content
    )
    has_10q = bool(filing_summary.get("latest_10q")) and any(
        k.startswith("10q") for k in filing_content
    )
    has_20f = bool(filing_summary.get("latest_20f")) and any(
        k.startswith("20f") for k in filing_content
    )
    has_6k = bool(filing_summary.get("latest_6k")) and any(
        k.startswith("6k") for k in filing_content
    )

    # ISS-097 (Loop7): ADR fallback remaps 20-F content keys to 10-K
    # equivalents (sec_edgar_20f / api_fallback_20f source markers in
    # latest_10k). Persisted prefixes lose the 20-F provenance, so
    # `has_20f` becomes False even though the underlying data WAS 20-F.
    # Recover provenance from `latest_10k.original_filing_type` so
    # observability accurately reports "this was a 20-F filing remapped
    # to 10-K shape for ADR".
    latest_10k_meta = filing_summary.get("latest_10k") or {}
    original_filing_type = latest_10k_meta.get("original_filing_type")
    original_has_20f = (original_filing_type == "20-F") or has_20f
    latest_10q_meta = filing_summary.get("latest_10q") or {}
    original_has_6k = (
        latest_10q_meta.get("original_filing_type") == "6-K"
    ) or has_6k

    if has_10k:
        annual_type = "10-K"
    elif has_20f:
        annual_type = "20-F"
    else:
        annual_type = None

    if has_10q:
        quarterly_type = "10-Q"
    elif has_6k:
        quarterly_type = "6-K"
    else:
        quarterly_type = None

    filing_validation = filing_summary.get("validation", {})
    # R15 T9: recheck filing_status after sanitization -- if all content removed,
    # the original status is stale
    if filing_status == "PASSED" and not has_10k and not has_10q and not has_20f and not has_6k:
        filing_status = "INCOMPLETE"
    if _fetch_filing_category:
        # ISS-019 fix: when override_status equals the result envelope's
        # error-bearing state (FAILED/PARTIAL with error attached), the
        # override-wins helper branch drops error.code / error.detail. For
        # the filing case, the override is just preserving the legacy
        # 4-state — observability needs the producer's error info.
        # Propagate via extra_keys when present.
        filing_extra_keys = {
            "has_10k": has_10k,
            "has_10q": has_10q,
            "has_20f": has_20f,
            "has_6k": has_6k,
            "filing_type_used": {
                "annual": annual_type,
                "quarterly": quarterly_type,
            },
            "filing_provenance": {
                # ISS-097 (Loop7): use original_has_* derived from
                # latest_10k.original_filing_type so 20-F remap is
                # preserved across has_20f-from-prefix collapse.
                "original_has_20f": original_has_20f,
                "original_has_6k": original_has_6k,
                "note": ("20-F/6-K content remapped to 10k/10q keys for "
                         "downstream compat" if (original_has_20f or original_has_6k) else None),
            },
            "total_filing_chars": total_filing_chars,
            "items_detail": items_detail,
            "missing_items": filing_validation.get("missing_items", []),
            "retry_attempts": filing_validation.get("retry_attempts", 0),
        }
        # ISS-019: propagate envelope error info through extra_keys when
        # override path drops it. Real filing FAILED scenarios (transport,
        # SSRF, oversize, schema drift) carry error.code/detail that
        # otherwise vanish from category_statuses["filing"].
        if filing_result.error is not None:
            filing_extra_keys.setdefault(
                "error_code", filing_result.error.code.value,
            )
            filing_extra_keys.setdefault(
                "error_detail", filing_result.error.detail,
            )
        category_statuses["filing"] = _derive_category_status(
            filing_result,
            override_status=filing_status,
            extra_keys=filing_extra_keys,
        )

        print(f"    Status: {filing_status}", file=sys.stderr)
        print(
            f"    10-K: {has_10k}, 10-Q: {has_10q}, 20-F: {has_20f}, 6-K: {has_6k}",
            file=sys.stderr,
        )
        print(f"    Total chars: {total_filing_chars:,}", file=sys.stderr)
        if filing_validation.get("missing_items"):
            print(
                f"    Missing: {filing_validation['missing_items']}",
                file=sys.stderr,
            )
    else:
        # SKIPPED path — no filing_result reference (not bound on this branch)
        category_statuses["filing"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )

    # ==================================================================
    # Category F2: Segmented Revenues
    # ==================================================================
    # Gated on financial data because segmented revenues fold into
    # 02_financial_data.json's 'segmented_revenues' field.
    if _should_fetch("02_financial_data"):
        print("\n[F2] Fetching Segmented Revenues...", file=sys.stderr)
        segmented_result = fetch_segmented_revenues(ticker)
        segmented_data = segmented_result.data if isinstance(segmented_result.data, dict) else {}
        has_10k_revenue_notes = "10k_revenue_notes" in filing_content
        has_10q_revenue_notes = "10q_revenue_notes" in filing_content
        category_statuses["segmented_revenues"] = _derive_category_status(
            segmented_result,
            extra_keys={
                "periods": segmented_data.get("periods", 0),
                "filing_revenue_notes": {
                    "10k_available": has_10k_revenue_notes,
                    "10q_available": has_10q_revenue_notes,
                    "fallback_status": (
                        "AVAILABLE"
                        if (has_10k_revenue_notes or has_10q_revenue_notes)
                        else "UNAVAILABLE"
                    ),
                },
            },
        )
        # Conditional FAILED → PARTIAL promotion (mirror pre-migration)
        if (segmented_result.status == "FAILED"
                and (has_10k_revenue_notes or has_10q_revenue_notes)):
            category_statuses["segmented_revenues"]["status"] = "PARTIAL"
            category_statuses["segmented_revenues"]["source"] = "filing_revenue_notes"
            print(
                "    Status: PARTIAL "
                "(API empty, promoted via Filing Revenue Notes fallback)",
                file=sys.stderr,
            )
        else:
            print(
                f"    Status: {segmented_result.status} "
                f"({segmented_data.get('periods', 0)} periods)",
                file=sys.stderr,
            )
    else:
        category_statuses["segmented_revenues"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[F2] Segmented Revenues SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category G: Insider Data (AUXILIARY)
    # ==================================================================
    if _should_fetch("04_insider_data"):
        print("\n[G] Fetching Insider Data...", file=sys.stderr)
        insider_result = fetch_insider_data(ticker)
        insider_data = insider_result.data if isinstance(insider_result.data, dict) else {}
        category_statuses["insider"] = _derive_category_status(
            insider_result,
            extra_keys={
                "trade_count": insider_data.get("count", 0),
                "summary": insider_data.get("summary", {}),
            },
        )
        print(
            f"    Status: {insider_result.status} ({insider_data.get('count', 0)} trades)",
            file=sys.stderr,
        )
    else:
        category_statuses["insider"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[G] Insider Data SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category I: Analyst Estimates (AUXILIARY)
    # ==================================================================
    if _should_fetch("06_analyst_estimates"):
        print("\n[I] Fetching Analyst Estimates...", file=sys.stderr)
        analyst_result = fetch_analyst_estimates(ticker)
        analyst_data = analyst_result.data if isinstance(analyst_result.data, dict) else {}
        category_statuses["analyst_estimates"] = _derive_category_status(
            analyst_result,
            extra_keys={
                "count": analyst_data.get("count", 0),
            },
        )
        print(
            f"    Status: {analyst_result.status} ({analyst_data.get('count', 0)} periods)",
            file=sys.stderr,
        )
    else:
        category_statuses["analyst_estimates"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[I] Analyst Estimates SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category J: Earnings Data (AUXILIARY)
    # ==================================================================
    if _should_fetch("07_earnings"):
        print("\n[J] Fetching Earnings Data...", file=sys.stderr)
        from scripts.sources.common import normalize_currency as _normalize_currency_j
        earnings_result = fetch_earnings_snapshot(ticker)
        press_result = fetch_earnings_press_releases(ticker)
        earnings_data = earnings_result.data if isinstance(earnings_result.data, dict) else {}
        press_data = press_result.data if isinstance(press_result.data, dict) else {}
        earnings_combined = {
            "earnings": earnings_data,
            "currency": _normalize_currency_j(earnings_data.get("currency")),
            "press_releases": press_data.get("press_releases", []),
            "press_releases_count": press_data.get("count", 0),
        }
        # ISS-011 (Cycle 4 backlog): combined-status matrix.
        # Pre-fix used earnings_result.status as the top-level "earnings"
        # category status, demoting press_releases failure to extra_key
        # only — consumers seeing the top-level PASSED couldn't tell that
        # press_releases had failed. Now combine:
        #   both PASSED → PASSED
        #   one PASSED + one FAILED → PARTIAL
        #   both FAILED → FAILED
        earnings_status = earnings_result.status
        press_status = press_result.status
        if earnings_status == "PASSED" and press_status == "PASSED":
            combined_status = None  # let result envelope drive (PASSED)
        elif earnings_status == "FAILED" and press_status == "FAILED":
            combined_status = "FAILED"
        elif earnings_status in ("PASSED", "PARTIAL") and press_status == "FAILED":
            combined_status = "PARTIAL"
        elif earnings_status == "FAILED" and press_status in ("PASSED", "PARTIAL"):
            combined_status = "PARTIAL"
        else:
            # Either both PARTIAL, or one PASSED one PARTIAL — either way PARTIAL
            combined_status = "PARTIAL"
        # Aggregate error info when there was any non-PASSED component
        earnings_extra = {
            "press_releases_status": press_status,
            "press_releases_count": press_data.get("count", 0),
        }
        if press_result.error is not None:
            earnings_extra["press_releases_error_code"] = press_result.error.code.value
            earnings_extra["press_releases_error_detail"] = press_result.error.detail
        # ISS-220 SF-H (Loop35 cycle 1): promote child error to canonical
        # top-level `error_code`/`error_detail` whenever combined_status
        # is non-PASSED. Pre-fix only earnings (primary) error promoted;
        # press_releases (secondary) error stayed nested under
        # `press_releases_error_code` and generic consumers reading
        # `category_statuses["earnings"]["error_code"]` saw None even
        # though combined was PARTIAL.
        if combined_status is not None:
            # Prefer primary (earnings) error when both present;
            # fall back to secondary (press) when primary succeeded.
            primary_err = earnings_result.error
            secondary_err = press_result.error
            chosen_err = primary_err if primary_err is not None else secondary_err
            if chosen_err is not None:
                earnings_extra["error_code"] = chosen_err.code.value
                earnings_extra["error_detail"] = chosen_err.detail
        category_statuses["earnings"] = _derive_category_status(
            earnings_result,
            override_status=combined_status,
            extra_keys=earnings_extra,
        )
        print(
            f"    Earnings: {earnings_result.status} "
            f"(period={earnings_data.get('report_period', '?')})",
            file=sys.stderr,
        )
        print(
            f"    Press releases: {press_result.status} "
            f"({press_data.get('count', 0)} releases)",
            file=sys.stderr,
        )
    else:
        category_statuses["earnings"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[J] Earnings Data SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category K: Institutional Ownership (AUXILIARY)
    # ==================================================================
    if _should_fetch("08_institutional"):
        print("\n[K] Fetching Institutional Ownership...", file=sys.stderr)
        institutional_result = fetch_institutional_ownership(ticker)
        institutional_data = (
            institutional_result.data
            if isinstance(institutional_result.data, dict)
            else {}
        )
        category_statuses["institutional"] = _derive_category_status(
            institutional_result,
            extra_keys={
                "count": institutional_data.get("count", 0),
            },
        )
        print(
            f"    Status: {institutional_result.status} "
            f"({institutional_data.get('count', 0)} holders)",
            file=sys.stderr,
        )
    else:
        category_statuses["institutional"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[K] Institutional Ownership SKIPPED", file=sys.stderr)

    # ==================================================================
    # Category L: Macro Interest Rates (AUXILIARY)
    # ==================================================================
    if _should_fetch("09_macro_rates"):
        print("\n[L] Fetching Macro Interest Rates...", file=sys.stderr)
        rates_snap_result = fetch_interest_rates_snapshot()
        rates_snapshot = rates_snap_result.data if isinstance(rates_snap_result.data, dict) else {}
        two_years_ago = (system_date - timedelta(days=730)).strftime("%Y-%m-%d")
        rates_hist_result = fetch_interest_rates_historical("FED", two_years_ago)
        rates_historical = (
            rates_hist_result.data if isinstance(rates_hist_result.data, dict) else {}
        )
        macro_rates_data = {
            "current_rates": rates_snapshot.get("rates", []),
            "fed_history": rates_historical.get("rates", []),
            "fed_history_count": rates_historical.get("count", 0),
        }
        # ISS-103 (Loop8): combined-status matrix. Pre-fix top-level
        # "status" tracked only the snapshot result; historical FAILED
        # was demoted to extra_key. Now apply same matrix as earnings
        # (ISS-011): both PASSED → PASSED, mixed → PARTIAL, both FAILED
        # → FAILED. Surface historical error info via extra_keys.
        snap_status = rates_snap_result.status
        hist_status = rates_hist_result.status
        if snap_status == "PASSED" and hist_status == "PASSED":
            macro_combined_status = None  # let envelope drive
        elif snap_status == "FAILED" and hist_status == "FAILED":
            macro_combined_status = "FAILED"
        else:
            macro_combined_status = "PARTIAL"
        macro_extra = {
            "historical_status": hist_status,
            "banks_count": len(rates_snapshot.get("rates", [])),
        }
        if rates_hist_result.error is not None:
            macro_extra["historical_error_code"] = rates_hist_result.error.code.value
            macro_extra["historical_error_detail"] = rates_hist_result.error.detail
        # ISS-220 SF-H (Loop35 cycle 1): same canonical-top-level
        # promotion as earnings combined-status site above. Pre-fix
        # only snapshot (primary) error promoted; historical (secondary)
        # error stayed nested under historical_error_code only.
        if macro_combined_status is not None:
            primary_err = rates_snap_result.error
            secondary_err = rates_hist_result.error
            chosen_err = primary_err if primary_err is not None else secondary_err
            if chosen_err is not None:
                macro_extra["error_code"] = chosen_err.code.value
                macro_extra["error_detail"] = chosen_err.detail
        category_statuses["macro_rates"] = _derive_category_status(
            rates_snap_result,
            override_status=macro_combined_status,
            extra_keys=macro_extra,
        )
        print(
            f"    Snapshot: {rates_snap_result.status} "
            f"({len(rates_snapshot.get('rates', []))} banks)",
            file=sys.stderr,
        )
        print(
            f"    FED history: {rates_hist_result.status} "
            f"({rates_historical.get('count', 0)} data points)",
            file=sys.stderr,
        )
    else:
        category_statuses["macro_rates"] = _derive_category_status(
            override_status="SKIPPED",
            reason="not in --categories",
        )
        print("\n[L] Macro Interest Rates SKIPPED", file=sys.stderr)

    # ==================================================================
    # FMP FINANCIAL-DATA FALLBACK (2026-05-29 dual-API integration)
    # ==================================================================
    # Runs BEFORE the yfinance fallback so FMP — higher-quality, structured,
    # 8 clean quarters INCLUDING the standalone fiscal Q4 that FDS omits for
    # non-Dec-FYE issuers — is preferred for the categories FDS starves
    # (financials / metrics / analyst estimates / earnings). The yfinance
    # pass that follows still fills anything FMP left empty (each per-category
    # check is emptiness-gated), so the two compose without clobbering.
    # See docs/superpowers/plans/2026-05-29-fmp-dual-api-fallback.md.
    fmp_fallback_summary = None
    _fmp_wanted = (
        _inputs_fetched(category_statuses, "financials")
        or _inputs_fetched(category_statuses, "metrics")
        or _inputs_fetched(category_statuses, "analyst_estimates")
        or _inputs_fetched(category_statuses, "earnings")
    )
    if run_fmp_fallback_fn is not None and _fmp_wanted:
        try:
            fmp_outcome = run_fmp_fallback_fn(
                ticker,
                financials_data=financials_data,
                metrics_data=metrics_data,
                analyst_data=analyst_data,
                earnings_combined=earnings_combined,
                want_financials=_inputs_fetched(category_statuses, "financials"),
                want_metrics=_inputs_fetched(category_statuses, "metrics"),
                want_analyst=_inputs_fetched(category_statuses, "analyst_estimates"),
                want_earnings=_inputs_fetched(category_statuses, "earnings"),
                as_of_date=system_date_str,
            )
            financials_data = fmp_outcome.financials_data
            metrics_data = fmp_outcome.metrics_data
            analyst_data = fmp_outcome.analyst_data
            earnings_combined = fmp_outcome.earnings_combined
            # Merge per-category status updates (provenance: fmp_fallback).
            # Producer-consumer rule #1: keys match the category_statuses
            # vocabulary used elsewhere (financials/metrics/analyst_estimates/
            # earnings).
            for _cat, _upd in fmp_outcome.status_updates.items():
                category_statuses[_cat] = _upd
            fmp_fallback_summary = fmp_outcome.to_run_meta_dict()
            for _cat, _fill in fmp_outcome.fills.items():
                if _fill.get("filled"):
                    print(f"    FMP fallback filled {_cat}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — fallback must never sink the run
            # The FMP fetchers already scrub + envelope their own errors
            # (adapter_error_from_exception with _fmp_redact_variants) and the
            # orchestrator never re-raises them, so a raw escape here is a
            # programming error, not a network message. Log ONLY the exception
            # type — never str(e) — so an unscrubbed URL/key can't leak even in
            # that bug path (the api key is closed over in the wrapper and is
            # not in this scope to redact against).
            print(f"[ERROR] FMP fallback crashed: {type(e).__name__}", file=sys.stderr)
            fmp_fallback_summary = {
                "attempted": True, "available": False,
                "reason": "fallback exception",
                "error_type": type(e).__name__, "fills": {},
            }

    # ==================================================================
    # YFINANCE FALLBACK
    # ==================================================================
    # Only run if the categories the fallback REPAIRS were actually
    # fetched. Pre-Loop33 the gate also required `analyst_estimates`,
    # which made subset fetches that legitimately skipped analyst (e.g.
    # `--categories financials,company`) ineligible for fallback even
    # though financials/metrics/company could still be repaired. The
    # analyst path inside `_run_yfinance_fallback_impl` is internally
    # optional (skipped if yf object is unavailable), so dropping it
    # from the gate broadens fallback coverage without fabricating
    # statuses (each per-category fill checks its own input emptiness).
    # ISS-220 4.14 (Loop33 cycle 1).
    _yf_inputs_ready = _inputs_fetched(
        category_statuses, "financials", "metrics", "company",
    )
    if run_yfinance_fallback_fn is not None and _yf_inputs_ready:
        try:
            fallback_outcome = run_yfinance_fallback_fn(
                ticker,
                financials_data,
                metrics_data,
                company_data,
                analyst_data,
            )
            assert isinstance(fallback_outcome, YfinanceFallbackOutcome), (
                f"expected YfinanceFallbackOutcome, got "
                f"{type(fallback_outcome).__name__}"
            )
            merge_fallback_outcome(category_statuses, fallback_outcome)
            financials_data = fallback_outcome.financials_data
            metrics_data = fallback_outcome.metrics_data
            company_data = fallback_outcome.company_data
            analyst_data = fallback_outcome.analyst_data
            yfinance_summary = fallback_outcome.to_run_meta_dict()
            # ISS-220 4.24 (Loop36 cycle 1): sync analyst_estimates
            # category_status to reflect fallback fills. Producer
            # (yahoo_finance.py:1839) sets analyst fallback
            # category_status=None — `merge_fallback_outcome` does
            # NOT write to category_statuses["analyst_estimates"],
            # so it stays at the pre-fallback FAILED/PARTIAL value
            # even though analyst_data has been fresh-filled.
            #
            # Decision rule (R-D1 reviewer):
            #   filled=False → unchanged
            #   filled=True + both sub-fields populated → PASSED
            #   filled=True + only some sub-fields → PARTIAL
            analyst_fill = fallback_outcome.fills.get("analyst")
            # 2026-05-29 dual-API: do NOT clobber an FMP-sourced provenance.
            # The FMP fallback (which runs first) may have already filled the
            # consensus `estimates` array and stamped data_source=fmp_fallback.
            # yfinance only ADDS the supplementary `yfinance_analyst` price
            # targets/recommendations sub-dict — that enrichment must not
            # rewrite the estimates' provenance back to yfinance_fallback.
            _analyst_is_fmp = (
                category_statuses.get("analyst_estimates", {}).get("data_source")
                == "fmp_fallback"
            )
            if analyst_fill is not None and analyst_fill.filled and not _analyst_is_fmp:
                yf_analyst = analyst_data.get("yfinance_analyst", {})
                has_pt = "price_targets" in yf_analyst
                has_rec = "recommendations" in yf_analyst
                if has_pt and has_rec:
                    new_status = "PASSED"
                else:
                    new_status = "PARTIAL"
                category_statuses["analyst_estimates"] = {
                    "status": new_status,
                    "data_source": "yfinance_fallback",
                }
        except Exception as e:
            # ISS-220 4.30 (Loop36 cycle 1): sym-ext gap of 4.12.
            # Outer except path persisted raw `str(e)` to stderr +
            # yfinance_summary["error"]; yfinance exception text
            # may carry cookies/crumbs/cache home paths. Route
            # through `_yfinance_safe_msg` so this fallback-side
            # log path matches the canonical mapper's scrub.
            from scripts.sources.yahoo_finance import _yfinance_safe_msg
            scrubbed = _yfinance_safe_msg(e)
            print(f"[ERROR] yfinance fallback crashed: {scrubbed}", file=sys.stderr)
            yfinance_summary = {
                "attempted": True, "available": False,
                "reason": "fallback exception",
                "error": scrubbed, "fills": {},
            }
    else:
        _skip_reason = (
            "inputs skipped by --categories"
            if not _yf_inputs_ready else "yfinance fallback not wired"
        )
        yfinance_summary = {
            "attempted": False,
            "available": False,
            "reason": _skip_reason,
            "fills": {},
        }

    # Recompute derived flags after yfinance fallback may have filled company data
    raw_is_adr = company_data.get("is_adr", is_adr)
    if isinstance(raw_is_adr, str):  # pattern-x-ok: defensive numpy.bool_/JSON-true drift rebuild per ISS-141/ISS-220 SF-D
        is_adr = raw_is_adr.strip().lower() in ("1", "true", "yes")  # pattern-x-ok: defensive numpy.bool_/JSON-true drift rebuild per ISS-141/ISS-220 SF-D
    else:
        is_adr = bool(raw_is_adr)
    sector = company_data.get("sector") or sector
    is_tech_stock = any(kw in str(sector) for kw in ["Technology", "Semiconductors"])

    # After the post-fallback raw_is_adr defensive rebuild closes. Runs
    # UNCONDITIONALLY regardless of whether yfinance fallback fired. Placed
    # BEFORE H-block ADR consumers (L2400+) read financials_data.
    _reconcile_financials_currency(financials_data)

    # Compute _eps_inputs_ready EARLY so resolver caller-side gate can reference it.
    # Spec §3.5 "Single canonical resolver call".
    #
    # **Spec→code divergence corrected here** (Cycle 2 Phase 4 finding): spec §3.5
    # behavior matrix row 3 claims "company FAILED → _eps_inputs_ready=False
    # (forced — _inputs_fetched flags 'company' as not-fetched)". Verified
    # (`scripts/fetch.py:1513-1519`): `_inputs_fetched` returns True for FAILED
    # status — only None/SKIPPED disqualify. So the spec's matrix is wrong; the
    # raw `_inputs_fetched(...)` predicate would let company-FAILED through.
    # Mitigation: compute a stricter STRICT predicate locally that also
    # disqualifies FAILED for the H-block gate, and use that for both the
    # resolver gate AND consumer gating.
    _eps_inputs_ready_raw = _inputs_fetched(
        category_statuses, "financials", "metrics", "price", "company",
    )
    _company_status = (category_statuses.get("company") or {}).get("status")
    # Strict variant: ALL four inputs are non-FAILED, non-SKIPPED, non-None.
    _eps_inputs_ready = _eps_inputs_ready_raw and all(
        (category_statuses.get(c) or {}).get("status") not in (None, "SKIPPED", "FAILED")
        for c in ("financials", "metrics", "price", "company")
    )

    # Caller-side gate: resolver is called ONLY when 03_company_news was fetched
    # AND company-fetch did not fail (Mode A-only per FIX-006).
    # `category_statuses[cat]` is a dict (see existing fetch.py:2014 etc), NOT an
    # object with a `.status` attribute — codex Cycle 1 C-6 finding. Use dict access.
    #
    # Subset-fetch refresh semantics (spec §8 clarification): when a user runs
    # `fetch --categories 03_company_news` (CLAUDE.md-documented ADR refresh
    # workflow), company_data is present but _eps_inputs_ready=False (other
    # EPS inputs are SKIPPED). In that case the resolver still DERIVES + WRITES
    # adr_profile.json from the fresh company_data — required so the CLI ADR
    # workflow (`adr/correct.py --adr-profile data/adr_profile.json`) reads
    # a current profile. Spec §8 "produced exactly when _eps_inputs_ready=True"
    # means "Mode A only, no Mode B-internal LOAD of a prior file"; it does
    # NOT mean "only run resolver in full-fetch". The `require=` arg controls
    # whether derive failure raises — full-fetch raises, subset-fetch returns
    # None silently.
    profile: Optional[AdrProfile] = None
    if _should_fetch("03_company_news") and _company_status != "FAILED":
        profile = resolve_adr_profile(
            ticker=ticker,
            company_data=company_data,
            output_dir=output_dir,
            as_of_date=system_date_str,
            require=_eps_inputs_ready,
        )

    # Belt-and-suspenders: catches future resolver bugs. With the strict
    # _eps_inputs_ready predicate, company-FAILED case correctly evaluates
    # _eps_inputs_ready=False, so this guard does NOT fire on that case — it
    # only fires if some FUTURE refactor breaks the resolver call.
    if _eps_inputs_ready and profile is None:
        raise SchemaError(
            "adr_profile.json", "<missing>",
            f"ADR-aware EPS consumers ready to run but no AdrProfile was derived. "
            f"This indicates a resolver-gate bug — both should fail-close together. "
            f"Run `python3 -m scripts.fetch -t {ticker} -o {output_dir} "
            f"--categories 03_company_news` and investigate."
        )

    # The adr_profile detector is authoritative for ADR status — it overrides
    # company_data.is_adr (which can be False for a known ADR like MRAAY).
    # Placed after profile resolution and BEFORE all ADR-gated consumers
    # (validation.is_adr write, market_cap reconciliation, ADR per-share paths).
    is_adr = _resolve_is_adr(profile, is_adr)

    # H (codex Loop review): a subset fetch (no 03_company_news) can't run the
    # live ADR detector, leaving profile=None and is_adr=False even for a known
    # ADR — which would let the market_cap reconciliation wrongly fire (ADR price
    # × ordinary shares). Before any ADR-gated consumer, consult a persisted
    # adr_profile.json from a prior full fetch. Only UPGRADES to True (never
    # downgrades) — a known ADR stays an ADR across subset fetches.
    if profile is None and not is_adr:
        _prior_profile_path = output_dir / "adr_profile.json"
        if _prior_profile_path.exists():
            try:
                # Use the typed loader with expected_ticker so a stale/foreign
                # adr_profile.json can't upgrade the wrong ticker (Pattern Y;
                # codex Loop review R5).
                from scripts.schemas.adr_profile import load_adr_profile
                _pp = load_adr_profile(_prior_profile_path, expected_ticker=ticker)
                if bool(getattr(_pp, "is_adr", False)):
                    is_adr = True
            except (OSError, ValueError, SchemaError):
                pass

    # Market-cap reconciliation MUST run before Category H. The EPS-consistency
    # check (H3, validate_eps_consistency) validates P/E × TTM-EPS ≈ price by
    # reading metrics_snapshot.price_to_earnings_ratio. If reconciliation only
    # runs in the tail-writes block (below), the check sees the STALE provider
    # P/E and emits a spurious large-deviation WARNING on exactly the fast-
    # moving stocks reconciliation targets (SNDK 20260522: 55% vs ~5% after
    # reconciliation, because the stale cap was ~half the true cap). Reconcile
    # here so the check validates the corrected P/E; the tail-writes call then
    # re-runs idempotently (sees "consistent", skips re-propagation). ADRs are
    # a no-op (_reconcile_market_cap returns None for is_adr), so the ADR path
    # is unaffected. See tests/test_market_cap_reconcile.py
    # ::test_eps_consistency_passes_after_market_cap_reconciliation.
    if _eps_inputs_ready:
        _early_mc = _reconcile_market_cap(price_data, financials_data, is_adr=is_adr)
        if _early_mc and _early_mc.get("status") in ("corrected", "filled"):
            price_data["market_cap_reconciliation"] = _early_mc
            _propagate_market_cap_to_metrics(metrics_data, _early_mc)
            _early_div = _early_mc.get("divergence_pct")
            _early_div_str = f"{_early_div}%" if _early_div is not None else "n/a"
            print(
                f"    market_cap reconciled ({_early_mc['status']}): "
                f"{_early_mc.get('from')} -> {_early_mc['to']:.0f} "
                f"(divergence {_early_div_str}, "
                f"price x {_early_mc['shares']:.0f} shares)",
                file=sys.stderr,
            )

    # ==================================================================
    # Category H: EPS Data Validation (requires financials+metrics+price+company)
    # ==================================================================
    # Defaults for variables produced by the H block so downstream references
    # (validation_data, save logic) remain safe when skipped.
    financial_freshness = {"status": "SKIPPED", "message": "inputs not fetched"}
    fiscal_format = {"status": "SKIPPED", "message": "inputs not fetched"}
    eps_consistency = {"status": "SKIPPED", "checks": {}, "warnings": []}
    adr_valuation = {"needs_correction": False}
    adr_eps = {}
    adr_classification = classify_ticker(ticker)
    company_mcap = company_data.get("market_cap") if company_data else None

    if not _eps_inputs_ready:
        print("\n[H] EPS Data Validations SKIPPED (inputs not fetched)", file=sys.stderr)
        category_statuses["eps_validation"] = {
            "status": "SKIPPED", "reason": "inputs not fetched",
        }
    else:
        print("\n[H] Running EPS Data Validations...", file=sys.stderr)

        # H1: Financial freshness (report_period <= 120 days)
        financial_freshness = validate_financial_freshness(financials_data, system_date)
        print(
            f"    Financial freshness: {financial_freshness['status']} - "
            f"{financial_freshness['message']}",
            file=sys.stderr,
        )

        # H2: Fiscal period format (YYYY-QN)
        fiscal_format = validate_fiscal_period_format(financials_data)
        print(
            f"    Fiscal period format: {fiscal_format['status']} - "
            f"{fiscal_format['message']}",
            file=sys.stderr,
        )

        # H3: EPS consistency checks
        eps_consistency = validate_eps_consistency(
            metrics_data, financials_data, price_data, profile=profile
        )
        print(f"    EPS consistency: {eps_consistency['status']}", file=sys.stderr)
        for check_name, check_result in eps_consistency["checks"].items():
            if check_result["status"] != "SKIPPED":
                print(
                    f"      - {check_name}: {check_result['status']} "
                    f"({check_result['message']})",
                    file=sys.stderr,
                )
        if eps_consistency["warnings"]:
            for warning in eps_consistency["warnings"]:
                print(f"      {warning}", file=sys.stderr)

        # H4: ADR valuation correction -- ALWAYS called
        # v7: Use direct function call instead of subprocess to adr_detect.py
        company_mcap = company_data.get("market_cap")
        if not company_mcap and is_adr:
            # Priority 1: Auto-detect via yfinance (direct function call)
            try:
                adr_result = detect_adr_market_data(ticker)
                adr_market_info = adr_result.data if adr_result.ok else {}
                _yf_mcap = adr_market_info.get("market_cap")
                if _yf_mcap and _yf_mcap > 0:
                    company_mcap = _yf_mcap
                    _yf_shares = adr_market_info.get("shares_outstanding")
                    _msg = f"    ADR auto-detect (yfinance): market_cap=${_yf_mcap/1e9:.1f}B"
                    if _yf_shares:
                        _msg += f", shares_outstanding={_yf_shares:,.0f}"
                    print(_msg, file=sys.stderr)
            except Exception as _e:
                print(f"    ADR auto-detect failed: {_e}", file=sys.stderr)

            # Priority 2: Fallback to metrics/price market_cap (less reliable for ADR)
            if not company_mcap:
                _fallback_mcap = (
                    (price_data.get("market_cap") if price_data else None)
                    or (metrics_data.get("market_cap") if metrics_data else None)
                )
                if _fallback_mcap and _fallback_mcap > 0:
                    company_mcap = _fallback_mcap
                    print(
                        f"    ADR market_cap fallback: metrics/price "
                        f"${_fallback_mcap/1e9:.1f}B (may be ADR float only)",
                        file=sys.stderr,
                    )

        adr_valuation = compute_adr_valuation_correction(
            profile, metrics_data, financials_data, price_data,
            company_market_cap=company_mcap,
        )
        if is_adr:
            print(
                f"    ADR valuation correction: "
                f"needs_correction={adr_valuation['needs_correction']}, "
                f"ratio={adr_valuation.get('adr_ratio')}",
                file=sys.stderr,
            )
            if adr_valuation.get("corrected_pe"):
                print(
                    f"      Corrected P/E: {adr_valuation['corrected_pe']:.2f}, "
                    f"TTM EPS: ${adr_valuation['corrected_ttm_eps']:.4f}",
                    file=sys.stderr,
                )

        # H5: ADR EPS ratio check -- ALWAYS called
        adr_eps = compute_adr_eps_check(
            profile, metrics_data, financials_data, price_data,
            company_market_cap=company_mcap,
        )
        if is_adr:
            print(
                f"    ADR EPS check: "
                f"ratio~={adr_eps.get('estimated_ratio')}:1, "
                f"adjustment={'needed' if adr_eps.get('needs_ratio_adjustment') else 'not needed'}",
                file=sys.stderr,
            )

        # Attach ADR results to eps_consistency
        eps_consistency["adr_valuation_correction"] = adr_valuation
        eps_consistency["adr_eps_check"] = adr_eps

        # Phase 2b: Write frozen ADR anchor (adr_correction.json)
        adr_classification = classify_ticker(ticker)
        # Honor the data-driven adr_profile: a foreign ADR the static table
        # doesn't list (e.g. SIVEF/SEK) must not be anchored as domestic/10-K.
        # Pass the H-block's resolved `is_adr` as the fallback so the subset
        # path (profile None but is_adr upgraded from prior adr_profile.json)
        # is still reconciled.
        adr_classification = _reconcile_anchor_with_profile(
            adr_classification, profile, fallback_is_adr=is_adr,
        )
        if adr_valuation.get("corrected_pe"):
            adr_classification["corrected_pe"] = adr_valuation["corrected_pe"]
        if adr_valuation.get("corrected_ttm_eps"):
            adr_classification["corrected_eps"] = adr_valuation["corrected_ttm_eps"]
        if adr_valuation.get("adr_ratio"):
            adr_classification["adr_ratio"] = adr_valuation["adr_ratio"]
        adr_anchor_path = output_dir / "adr_correction.json"
        write_adr_anchor(adr_classification, adr_anchor_path)
        print(
            f"    ADR anchor written: {adr_anchor_path} "
            f"(tier={adr_classification['data_quality_tier']})",
            file=sys.stderr,
        )

        # Store EPS validation results
        category_statuses["eps_validation"] = {
            "status": eps_consistency["status"],
            "financial_freshness": financial_freshness,
            "fiscal_period_format": fiscal_format,
            "eps_consistency": eps_consistency,
        }

    # ==================================================================
    # Growth Stock Mode Detection (requires financials + metrics)
    # ==================================================================
    if _inputs_fetched(category_statuses, "financials", "metrics"):
        print("\n[DETECTION] Growth Stock Mode...", file=sys.stderr)
        # Reconcile the post-H-block per-field currency mix to all-USD BEFORE
        # detection — growth-stock-mode reads balance-sheet cash + total_assets,
        # which the H-block leaves in mixed currency until the save boundary.
        # See _growth_stock_mode_reconciled (root-cause fix for the MRAAY
        # cash_ratio=0.011 garbage that was persisted to 00_validation.json).
        growth_stock_mode = _growth_stock_mode_reconciled(metrics_data, financials_data, ticker=ticker)
        category_statuses["growth_stock_mode"] = growth_stock_mode
        if growth_stock_mode["enabled"]:
            print(
                f"    Growth Stock Mode ENABLED (score={growth_stock_mode['score']:.1f})",
                file=sys.stderr,
            )
            for trigger, value in growth_stock_mode["triggers"].items():
                if value:
                    print(f"      - {trigger}: True", file=sys.stderr)
        else:
            print(
                f"    Growth Stock Mode disabled (score={growth_stock_mode['score']:.1f})",
                file=sys.stderr,
            )
    else:
        growth_stock_mode = {
            "enabled": False, "score": 0.0, "triggers": {},
            "status": "SKIPPED", "reason": "inputs not fetched",
        }
        category_statuses["growth_stock_mode"] = growth_stock_mode
        print("\n[DETECTION] Growth Stock Mode SKIPPED (inputs not fetched)", file=sys.stderr)

    # ==================================================================
    # Determine Final Status
    # ==================================================================
    # Known limitation: category_statuses for metrics/financials/company/analyst
    # still reflect pre-yfinance-fallback results. The yfinance fallback updates
    # the *data* dicts in-place but does NOT recompute category_statuses entries.
    # This means final_status may report FAILED for a category that yfinance
    # actually backfilled. A proper fix would re-evaluate each category status
    # after fallback, but that requires non-trivial refactoring.
    critical_statuses = [
        category_statuses.get("price", {}).get("status"),
        category_statuses.get("metrics", {}).get("status"),
        category_statuses.get("financials", {}).get("status"),
        category_statuses.get("filing", {}).get("status"),
    ]
    # EPS validation contributes to overall status (any non-PASSED -> PARTIAL)
    eps_status = category_statuses.get("eps_validation", {}).get("status")
    if eps_status and eps_status not in ("PASSED", "SKIPPED"):
        critical_statuses.append(eps_status)

    if "FAILED" in critical_statuses or "CIRCUIT_BREAKER" in critical_statuses:
        final_status = "FAILED"
    elif all(s == "PASSED" for s in critical_statuses if s):
        final_status = "PASSED"
    elif "INCOMPLETE" in critical_statuses:
        final_status = "INCOMPLETE"
    else:
        final_status = "PARTIAL"

    # ISS-120 (Loop8 cycle 2): non-critical category failures must also
    # downgrade PASSED → PARTIAL. Pre-fix only `historical` was wired
    # to downgrade; macro_rates / news / insider / analyst_estimates /
    # earnings / institutional / company FAILED would silently leave
    # final_status PASSED. Loop8 cycle 1 ISS-103 fixed
    # `macro_combined_status` per-category but the user-facing
    # `final_status` still hid it. Now any non-critical category that's
    # FAILED, PARTIAL, or INCOMPLETE downgrades a top-level PASSED to
    # PARTIAL — preserves the strict "PASSED means everything attempted
    # succeeded" semantics consumers rely on.
    #
    # ISS-130 (Loop9 cycle 1): keys must match the ACTUAL strings written
    # by the per-category dispatch above — the real keys are
    # `analyst_estimates` (not "analyst") and `segmented_revenues` (not
    # "segmented"); my Loop8c2 placeholders silently never matched, so
    # those two categories' failures still slipped through. Verified
    # by grep over fetch.py for `category_statuses["..."]` writes.
    #
    # ISS-131 (Loop9 cycle 1): include `historical` in this loop so
    # PARTIAL/INCOMPLETE historical states also downgrade — pre-fix
    # only the special-cased FAILED branch handled historical, leaving
    # PARTIAL/INCOMPLETE invisible at the top level.
    # ISS-220 4.26 (Loop36 cycle 1): include `growth_stock_mode`.
    # Pre-fix when `detect_growth_stock_mode` swallowed an exception
    # and emitted `status=FAILED` (ISS-198 Loop28), the FAILED state
    # was invisible at top level — final_status stayed PASSED. Add to
    # non_critical_keys so a compute-time growth-stock-detection
    # failure surfaces as PARTIAL (informational signal to operator).
    non_critical_keys = (
        "historical", "macro_rates", "company", "news", "insider",
        "analyst_estimates", "earnings", "institutional",
        "segmented_revenues",
        "growth_stock_mode",
    )
    if final_status == "PASSED":
        for nk in non_critical_keys:
            nk_status = category_statuses.get(nk, {}).get("status")
            if nk_status in ("FAILED", "PARTIAL", "INCOMPLETE"):
                final_status = "PARTIAL"
                break

    # ==================================================================
    # Save Output Files
    # ==================================================================
    print("\n[OUTPUT] Saving files...", file=sys.stderr)

    # 00_validation.json -- ALWAYS written (even for subset fetches).
    validation_data = {
        "status": final_status,
        "system_date": system_date_str,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "is_adr": is_adr,
        "is_tech_stock": is_tech_stock,
        "growth_stock_mode": growth_stock_mode,
        "yfinance_fallback": yfinance_summary,
        "fmp_fallback": fmp_fallback_summary,
        "tier_decided": args.tier_decided,
        "categories": category_statuses,
        "files": {
            "price": "01_price_data.json",
            "financial": "02_financial_data.json",
            "company_news": "03_company_news.json",
            "insider": "04_insider_data.json",
            "filing_summary": "05_filing_summary.json",
            "filing_content": [
                f"05_filing_{k}.md" for k in filing_content.keys()
            ],
            "analyst_estimates": "06_analyst_estimates.json",
            "earnings": "07_earnings.json",
            "institutional": "08_institutional.json",
            "macro_rates": "09_macro_rates.json",
        },
    }
    save_json(validation_data, output_dir / "00_validation.json")
    print("    00_validation.json", file=sys.stderr)

    # Tail writes -- gated by --categories so subset fetches NEVER overwrite
    # unrelated existing files with defaults. The category-gating produced
    # empty defaults for skipped categories; writing those would clobber
    # authoritative prior content.

    # 01_price_data.json -- backfill market_cap if missing
    if _should_fetch("01_price_data"):
        if price_data.get("price") and not price_data.get("market_cap"):
            if is_adr and company_data.get("market_cap"):
                price_data["market_cap"] = company_data["market_cap"]
            elif metrics_data.get("market_cap"):
                price_data["market_cap"] = metrics_data["market_cap"]
        # Reconcile market_cap against current price × shares. The provider
        # field (Yahoo chart meta.marketCap) can be stale for fast-moving
        # stocks; correct it so downstream valuation multiples aren't silently
        # computed off a lagged cap (see _reconcile_market_cap docstring).
        mc_reconciliation = _reconcile_market_cap(price_data, financials_data, is_adr=is_adr)
        if mc_reconciliation and mc_reconciliation.get("status") in ("corrected", "filled"):
            price_data["market_cap_reconciliation"] = mc_reconciliation
            # Push the same correction into metrics_snapshot so its cap-derived
            # multiples (P/E, P/S, P/B, PEG, EV/EBITDA, EV/Rev, FCF yield) are
            # not silently computed off the lagged provider cap. Without this,
            # historical_multiples (current_from_api) + the valuation agent read
            # stale multiples (MU: P/E 19.4 vs true ~35 after a 44.8% cap fix).
            _propagate_market_cap_to_metrics(metrics_data, mc_reconciliation)
            _div = mc_reconciliation.get("divergence_pct")
            _div_str = f"{_div}%" if _div is not None else "n/a"
            print(
                f"    market_cap reconciled ({mc_reconciliation['status']}): "
                f"{mc_reconciliation.get('from')} -> {mc_reconciliation['to']:.0f} "
                f"(divergence {_div_str}, "
                f"price x {mc_reconciliation['shares']:.0f} shares)",
                file=sys.stderr,
            )
        # Fetch equity beta via yfinance (always attempted, independent of fallback)
        beta_info = _fetch_beta(ticker, historical_data)
        price_output = {
            "snapshot": price_data,
            "historical": historical_data,
            "beta": beta_info,
        }
        save_json(price_output, output_dir / "01_price_data.json")
        print("    01_price_data.json", file=sys.stderr)

    # 02_financial_data.json
    if _should_fetch("02_financial_data"):
        financial_output = {
            "metrics_snapshot": metrics_data,
            "income_statements": financials_data.get("income_statements", []),
            "balance_sheets": financials_data.get("balance_sheets", []),
            "cash_flows": financials_data.get("cash_flows", []),
            "segmented_revenues": segmented_data.get("segments", []),
        }
        # Growth-stock-mode detection (_growth_stock_mode_reconciled) already
        # reconciled the mix in place and stamped the marker on financials_data
        # when financials+metrics were fetched. Carry that marker onto the saved
        # object so the repair call below is an idempotent no-op (detect →
        # not-mixed → early return without re-stamping). When growth-mode was
        # skipped (metrics absent), no early reconcile ran, the statements are
        # still mixed, and the call below performs the authoritative repair.
        if "currency_consistency" in financials_data:
            financial_output["currency_consistency"] = financials_data["currency_consistency"]
        # Mixed-currency detect + repair at the save boundary (AFTER the H-block
        # may have converted only the 12-field master set in place). Produces a
        # clean all-USD statement + repaired marker, or flags unrepairable.
        _repair_financials_currency_marker(financial_output)
        # P1 (SNDK): flag extreme-QoQ quarters (real cyclical peak vs corrupt)
        # into the file the fundamental agent actually reads, cross-checked
        # against 07_earnings actuals — so the agent gets a deterministic
        # real-vs-suspect signal instead of guessing "corrupted" and dropping a
        # genuine peak. Runs on the post-currency-repair income statements.
        from scripts.anomaly import detect_anomalous_quarters
        # Cross-check against the SAME earnings object that gets saved to
        # 07_earnings.json — `earnings_combined` is replaced by the FMP-fallback
        # result above, so the pre-fallback `earnings_data` can be stale/empty for
        # FMP-backfilled tickers (the foreign ADR / FDS-starved names most likely
        # to need it). Pass the earnings object's OWN currency (NOT the combined-
        # level normalized one, which is computed at FDS-fetch time and can be
        # stale after fallback) so the detector judges currency basis honestly.
        # On the mixed-currency-unrepairable path gross_profit can be native while
        # revenue is USD, so skip the margin signal (revenue QoQ stays valid — it
        # is always in the USD master set).
        _margin_reliable = (
            financial_output.get("currency_consistency", {}).get("status")
            != "mixed_unrepairable"
        )
        financial_output["anomalous_quarters"] = detect_anomalous_quarters(
            financial_output.get("income_statements", []),
            earnings_combined.get("earnings") or {},
            ticker=ticker,
            margin_reliable=_margin_reliable,
        )
        save_json(financial_output, output_dir / "02_financial_data.json")
        print("    02_financial_data.json", file=sys.stderr)

    # 03_company_news.json
    if _should_fetch("03_company_news"):
        company_news_output = {
            "company": company_data,
            "news": news_data,
        }
        save_json(company_news_output, output_dir / "03_company_news.json")
        print("    03_company_news.json", file=sys.stderr)

    # 04_insider_data.json
    if _should_fetch("04_insider_data"):
        from scripts.sources.common import resolve_artifact_currency
        insider_data["currency"] = resolve_artifact_currency(
            in_memory=price_data,
            in_memory_path="currency",
            output_dir=output_dir,
            artifact_name="01_price_data.json",
            artifact_path="snapshot.currency",
        )
        save_json(insider_data, output_dir / "04_insider_data.json")
        print("    04_insider_data.json", file=sys.stderr)

    # 05_filing_summary.json + 05_filing_*.md
    if _fetch_filing_category:
        save_json(filing_summary, output_dir / "05_filing_summary.json")
        print("    05_filing_summary.json", file=sys.stderr)

        for key, content in filing_content.items():
            filename = f"05_filing_{key}.md"
            save_text(content, output_dir / filename)
            print(f"    {filename} ({len(content):,} chars)", file=sys.stderr)

        # 05_filing_intelligence.json (guidance extraction -- zero LLM cost)
        try:
            from scripts.filing_intelligence import run as run_filing_intelligence
            run_filing_intelligence(str(output_dir))
        except ImportError:
            # filing_intelligence not yet migrated to v7 -- skip gracefully
            pass
        except Exception as e:
            print(f"    05_filing_intelligence.json SKIPPED: {e}", file=sys.stderr)

    # 06_analyst_estimates.json
    if _should_fetch("06_analyst_estimates"):
        # Conditional-repair: fill quote_currency / statement_currency if neither
        # FD primary path NOR yfinance fallback supplied them. Both paths have run
        # by this point — this repair sees the union and only writes when the
        # field is genuinely missing/None (NOT `.setdefault` which doesn't repair
        # existing-None values, per FIX-5.1).
        from scripts.sources.common import resolve_artifact_currency as _rac
        if not analyst_data.get("quote_currency"):
            analyst_data["quote_currency"] = _rac(
                in_memory=price_data, in_memory_path="currency",
                output_dir=output_dir, artifact_name="01_price_data.json",
                artifact_path="snapshot.currency",
            )
        if not analyst_data.get("statement_currency"):
            _first_income = None
            if isinstance(financials_data, dict):
                _is = financials_data.get("income_statements") or []
                _first_income = _is[0] if _is else None
            analyst_data["statement_currency"] = _rac(
                in_memory=_first_income, in_memory_path="currency",
                output_dir=output_dir, artifact_name="02_financial_data.json",
                artifact_path="income_statements.0.currency",
            )
        save_json(analyst_data, output_dir / "06_analyst_estimates.json")
        print("    06_analyst_estimates.json", file=sys.stderr)

    # 07_earnings.json
    if _should_fetch("07_earnings"):
        save_json(earnings_combined, output_dir / "07_earnings.json")
        print("    07_earnings.json", file=sys.stderr)

    # 08_institutional.json
    if _should_fetch("08_institutional"):
        save_json(institutional_data, output_dir / "08_institutional.json")
        print("    08_institutional.json", file=sys.stderr)

    # 09_macro_rates.json
    if _should_fetch("09_macro_rates"):
        save_json(macro_rates_data, output_dir / "09_macro_rates.json")
        print("    09_macro_rates.json", file=sys.stderr)

    # ==================================================================
    # Final Summary
    # ==================================================================
    print(f"\n{'=' * 60}", file=sys.stderr)
    print("VALIDATION COMPLETE", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    print(f"Final Status: {final_status}", file=sys.stderr)
    print(f"Output Directory: {output_dir}", file=sys.stderr)
    print(f"Files Created: {2 + 4 + len(filing_content) + 4}", file=sys.stderr)
    if is_adr:
        print(
            f"ADR Stock: category='{company_data.get('category')}', "
            f"ratio~={adr_eps.get('estimated_ratio')}:1",
            file=sys.stderr,
        )
    print(f"{'=' * 60}\n", file=sys.stderr)

    # Print summary JSON to stdout
    summary = {
        "status": final_status,
        "system_date": system_date_str,
        "ticker": ticker,
        "output_dir": str(output_dir),
        "is_adr": is_adr,
        "categories": {
            k: v.get("status") if isinstance(v, dict) else v
            for k, v in category_statuses.items()
        },
        "files_created": [
            "00_validation.json",
            "01_price_data.json",
            "02_financial_data.json",
            "03_company_news.json",
            "04_insider_data.json",
            "05_filing_summary.json",
        ]
        + [f"05_filing_{k}.md" for k in filing_content.keys()]
        + [
            "06_analyst_estimates.json",
            "07_earnings.json",
            "08_institutional.json",
            "09_macro_rates.json",
        ],
    }
    print(json.dumps(summary, indent=2))

    # ISS-119 (Loop8 cycle 2): INCOMPLETE on a critical category means
    # we lack data the downstream pipeline NEEDS (price/metrics/
    # financials/filing). Pre-fix this returned exit 0 — only FAILED
    # mapped to 1 — so an "incomplete" run silently passed CI/orches-
    # tration as success. Documented contract is `0=success, 1=failure,
    # 2=error` (project exit-code convention), so map INCOMPLETE to 1
    # alongside FAILED. PARTIAL stays 0 because the pipeline can still
    # produce a degraded report.
    return 1 if final_status in ("FAILED", "INCOMPLETE") else 0


# ---------------------------------------------------------------------------
# _build_di_wrappers -- bake env config into DI callables
# ---------------------------------------------------------------------------

def _build_di_wrappers(fmp_api_key: str):
    """Build DI wrappers for direct CLI execution.

    Each wrapper closes over environment-specific values (API keys,
    yfinance availability) so that ``_main_impl`` receives callables
    with the simple signatures it expects.
    """
    from scripts.sources.yahoo_finance import (
        _run_yfinance_fallback_impl,
        HAS_YFINANCE as has_yfinance,
    )
    from scripts.sources.fmp import (
        _fetch_filing_metadata_from_fmp_impl,
        _fetch_filing_date_impl,
        _run_fmp_fallback_impl,
    )

    yf_module = None
    _has_yf = has_yfinance
    if _has_yf:
        try:
            import yfinance as yf_module  # type: ignore[no-redef]
        except ImportError:
            _has_yf = False

    def run_yfinance_fallback(ticker, financials, metrics, company, analyst):
        return _run_yfinance_fallback_impl(
            ticker, financials, metrics, company, analyst,
            has_yfinance=_has_yf, yf_module=yf_module,
        )

    def fetch_filing_data(ticker, is_adr=False):
        return _fetch_filing_data_impl(
            ticker, is_adr=is_adr, fmp_api_key=fmp_api_key,
            fetch_fmp_metadata_fn=_fetch_filing_metadata_from_fmp_impl,
            fetch_filing_date_fn=_fetch_filing_date_impl,
        )

    def fetch_fmp_metadata(ticker, filing_type, limit=1):
        return _fetch_filing_metadata_from_fmp_impl(
            ticker, filing_type, limit, fmp_api_key=fmp_api_key,
        )

    def run_fmp_fallback(ticker, *, financials_data, metrics_data,
                         analyst_data, earnings_combined,
                         want_financials=False, want_metrics=False,
                         want_analyst=False, want_earnings=False,
                         as_of_date=""):
        return _run_fmp_fallback_impl(
            ticker,
            financials_data=financials_data, metrics_data=metrics_data,
            analyst_data=analyst_data, earnings_combined=earnings_combined,
            fmp_api_key=fmp_api_key,
            want_financials=want_financials, want_metrics=want_metrics,
            want_analyst=want_analyst, want_earnings=want_earnings,
            as_of_date=as_of_date,
        )

    return (fetch_filing_data, run_yfinance_fallback, fetch_fmp_metadata,
            run_fmp_fallback)


# ---------------------------------------------------------------------------
# main -- direct CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Direct CLI execution: ``python3 scripts/fetch.py -t AAPL -o /tmp/out``

    Reads FMP_API_KEY from env and builds DI wrappers for all callables.
    """
    args = parse_args()
    fmp_api_key = os.environ.get("FMP_API_KEY", "")
    (fetch_filing_data_fn, run_yfinance_fallback_fn, fetch_fmp_metadata_fn,
     run_fmp_fallback_fn) = _build_di_wrappers(fmp_api_key)
    exit_code = _main_impl(
        args,
        fetch_filing_data_fn=fetch_filing_data_fn,
        run_yfinance_fallback_fn=run_yfinance_fallback_fn,
        fetch_filing_metadata_from_fmp_fn=fetch_fmp_metadata_fn,
        run_fmp_fallback_fn=run_fmp_fallback_fn,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
