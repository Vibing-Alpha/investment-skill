"""ADR valuation correction and EPS cross-validation.

Migrated from v6.5 pipeline/adr.py (correction functions).

Cross-platform fixes applied:
- pathlib.Path instead of hardcoded '/' separators
- encoding="utf-8" on all file I/O
"""

import math
import re
import sys
from typing import Dict

from scripts.cli_utils import read_json, write_output, emit_dl3c_root_marker
# fresh-loop2 ISS-026: parse_bool_flag + EPS_*_THRESHOLD imports were dead
# (zero usage in this module after the in-place threshold literals at the
# divergence-check sites stabilized). Drop them; producer-consumer single-
# point-of-truth via shared constants is the right pattern when actually
# referenced — silent dead-imports drift over time as the constants get
# tweaked at the source without affecting this consumer.
from scripts.schemas.adr_profile import AdrProfile
from scripts.schemas.quarter_window import (
    AlignedQuarterWindow,
    InsufficientQuartersError,
    aligned_quarters,
    iter_aligned_quarter_windows,
    row_matches_period,
)
# DL3c §3.4 imports (cycle-23 agent finding — required by both
# compute_adr_valuation_correction §3.4.1 and compute_adr_eps_check §3.4.5).
# `apply_fx_conversion` + `build_cert_block` are imported LAZILY at the
# call site (inside each producer function) — same pattern as extract_fcf
# §3.2. Reason: top-level binding makes monkeypatching `scripts.fx_apply.
# apply_fx_conversion` from tests ineffective (the local name is already
# resolved by import time). Lazy import re-resolves on every call, so
# tests can swap the helper. No import-cycle problem exists between
# fx_apply and adr/correct (unlike extract_fcf), but the lazy pattern is
# kept for symmetry + testability.
from scripts.sources.fx_rates import SUPPORTED_FX_CURRENCIES

from scripts.adr import safe_float as _sf, is_finite_numeric as _ifn

_PREFIX = "adr.correct"


# ---------------------------------------------------------------------------
# DL3c §3.4 — consumer-fields registries + annual-mode selector
# ---------------------------------------------------------------------------

# Field subset consumed by compute_adr_valuation_correction's TTM aggregation.
# Used by apply_fx_conversion to know which fields need rate-multiply. Income
# carries net_income + revenue/total_revenue aliases + operating_income;
# cash_flows carry OCF + capex + D&A; balance carries debt/cash/equity.
ADR_CORRECT_MONEY_FIELDS = (
    ("income_statements", ("revenue", "total_revenue", "operating_income",
                           "net_income")),
    ("cash_flows", ("net_cash_flow_from_operations", "capital_expenditure",
                    "depreciation_and_amortization")),
    ("balance_sheets", ("total_debt", "cash_and_equivalents",
                        "shareholders_equity")),
)

# Field subset consumed by compute_adr_eps_check — only net_income from income
# (verified per spec §3.4.5 / superpowers-agent reading of adr/correct.py:
# 1116-1241; the function reads ONLY net_income from income, no cash_flow
# operands → no two-path carve-out needed).
# post-impl loop-1 H1 fix: cash_flows + balance_sheets declared
# alignment-only (empty tuple). compute_adr_eps_check passes all 3 families
# to aligned_quarters at line 1372; without the retag, _build_aligned_quarter
# saw mixed currency (USD on income, JPY on cash_flows + balance) after a
# successful FX conversion and raised statement_metadata_mismatch — defeating
# DL3c for adr-eps-check on every non-USD ADR.
ADR_EPS_CHECK_MONEY_FIELDS = {
    "income_statements": ("net_income",),
    "cash_flows": (),
    "balance_sheets": (),
}


def _any_explicit_non_usd_across_families(
    income_statements: list, cash_flows: list, balance_sheets: list,
):
    """Return the first explicit non-USD currency value found across any
    of the 3 statement families, or None if every row is USD / missing.

    post-impl loop-2 ISS-025: pre-fix adr/correct.py read ONLY
    `income_statements[0].get("currency")` for the USD-vs-non-USD gate.
    If a provider mis-reported income_statements[0] as USD while cash_flows
    were still in JPY (a stale-row drift case), the USD-default branch
    consumed JPY values as USD in TTM math without entering the FX
    conversion path. Matches extract_fcf.py's `_any_explicit_non_usd`
    scan-all-families pattern.
    """
    for fam in (cash_flows, income_statements, balance_sheets):
        for row in fam:
            if not isinstance(row, dict):
                continue
            cur = row.get("currency")
            if cur is None:
                continue
            if isinstance(cur, str) and cur.strip().upper() == "USD":
                continue
            return cur
    return None


def _uses_annual_mode(income_statements: list) -> bool:
    """True iff adr/correct.py would select annual-mode processing for this
    dataset. Mirrors the live selector at adr/correct.py:219 / 1073:

        is_annual = row_matches_period(income_statements[0], "annual")

    DL3c §3.4.3 — keeps the annual carve-out gate (invariant 8) in lock-step
    with the existing mode-selection. Empty input → False (let downstream
    "no quarters" gate fire).

    Note (cycle-20 F-20-1 HIGH): `row_matches_period` lives in
    `scripts.schemas.quarter_window`, NOT `scripts.normalize` — earlier
    spec draft had the wrong module.
    """
    if not income_statements:
        return False
    if not isinstance(income_statements[0], dict):
        return False
    return row_matches_period(income_statements[0], "annual")


def _enter_annual_carve_out_assert_usd(income_row, cash_flow_row, balance_row,
                                       *, ticker: str) -> None:
    """DL3c §3.4.2 — defensive assert at the entry of annual carve-out.

    Triple-check: by §3.4.1 this should be unreachable for non-USD inputs
    (the upstream gate fires `fx_unsupported_annual_path` before the annual
    branch ever runs). This defensive raise catches refactor errors that
    bypass the upstream gate — if it ever fires, it surfaces a regression
    in the DL3c FX flow loudly instead of silently emitting partially-
    converted JPY ratios.

    Raises ValueError so the test suite can verify the invariant fires.
    Note F-5: this is the ONE deliberate raise in adr/correct's FX layer;
    the upstream §3.4.1 gate uses marker-wrapped returns (no raise) so the
    CLI cannot bypass write_output + emit_dl3c_root_marker.
    """
    for label, row in (("income", income_row),
                       ("cash_flow", cash_flow_row),
                       ("balance", balance_row)):
        if row is None:
            continue
        if not isinstance(row, dict):
            continue
        row_cur = str(row.get("currency", "")).strip().upper()
        if row_cur and row_cur != "USD":
            raise ValueError(
                f"fx_unsupported_annual_path: ticker={ticker} "
                f"{label} row currency={row_cur!r}; "
                f"DL3c invariant 8 — annual path is USD-only. "
                f"Upstream gate at §3.4.1 should have fail-closed."
            )


# ---------------------------------------------------------------------------
# compute_adr_valuation_correction
# ---------------------------------------------------------------------------

def compute_adr_valuation_correction(
    profile: AdrProfile,
    metrics_data: Dict,
    financials_data: Dict,
    price_data: Dict,
    company_market_cap: float = None,
) -> Dict:
    """Compute corrected valuation metrics for ADR stocks.

    First-principles (validated against Seeking Alpha):
    - company_facts.market_cap = ADR_units x ADR_price (correct total market cap)
    - metrics_snapshot.market_cap = ordinary_shares x ADR_price (inflated, WRONG)
    - income_statements.earnings_per_share_diluted uses INCONSISTENT denominator
    - metrics_snapshot.earnings_per_share = per-ordinary-share (UNRELIABLE for ADR)

    Correct approach:
    1. market_cap = company_facts.market_cap (validated by Seeking Alpha P/S, P/B)
    2. adr_units = market_cap / price
    3. corrected_ttm_eps = ttm_net_income / adr_units
    4. adr_ratio = ordinary_shares / adr_units
    5. All ratio metrics use company_facts.market_cap
    """
    is_adr = profile.is_adr
    result = {
        "is_adr": is_adr,
        "needs_correction": False,
        "correction_status": "not_applicable",  # not_applicable / skipped / applied
        "adr_ratio": None,
        "corrected_pe": None,
        "corrected_pb": None,
        "corrected_ps": None,
        "corrected_ev": None,
        "corrected_ev_ebitda": None,
        "corrected_ev_revenue": None,
        "corrected_fcf_yield": None,
        "corrected_peg": None,
        "corrected_bvps": None,
        "corrected_fcfps": None,
        "market_cap_used": None,
        "ttm_net_income": None,
        "ttm_revenue": None,
        "ttm_ebit": None,
        "ttm_da": None,
        "ttm_ebitda": None,
        "ttm_fcf": None,
        "shareholders_equity": None,
        "total_debt": None,
        "cash": None,
        "corrected_ttm_eps": None,
        "eps_growth_rate": None,
        "message": "",
    }

    if not is_adr:
        return emit_dl3c_root_marker(result)

    income_statements = financials_data.get("income_statements", [])
    balance_sheets = financials_data.get("balance_sheets", [])
    cash_flows = financials_data.get("cash_flows", [])

    # Sort all statement families newest-first to avoid stale-data bias
    def _sort_newest(stmts):
        # fresh-loop2-cycle2 C2C-MED-4: `dict.get(k, default)` returns
        # `None` (NOT the default) when the key EXISTS with value None.
        # `sorted([{"report_period": None}, ...], key=lambda s: s.get("report_period", ""))`
        # raises `TypeError: '<' not supported between instances of
        # 'str' and 'NoneType'`. Coerce to empty string at the sort key
        # so malformed rows sort to the start of newest-first order
        # without crashing.
        return sorted(
            [s for s in stmts if isinstance(s, dict)],
            key=lambda s: s.get("report_period") or "", reverse=True,
        )

    income_statements = _sort_newest(income_statements)
    balance_sheets = _sort_newest(balance_sheets)
    cash_flows = _sort_newest(cash_flows)

    # DL3c §3.4.1 — 3-state currency gate. Replaces the prior 2-state
    # USD-or-skip with the 3-state pattern: USD-native (no cert) /
    # supported-non-USD-converted (cert emitted) / unsupported-or-
    # unrecognized (marker-wrapped skip).
    # F-18-1 (cycle-18 HIGH): extract ticker from AdrProfile BEFORE any
    # string interpolation that consumes it (function takes `profile`
    # only, no `ticker` kwarg). Without this, the error-message f-strings
    # below would raise NameError, bypassing emit_dl3c_root_marker.
    # F-5: every FX fail-close path RETURNS a marker-wrapped dict — never
    # raises. The CLI at the bottom of this file does not catch ValueError
    # around compute_adr_valuation_correction; a raise would traceback past
    # write_output + emit_dl3c_root_marker.
    if income_statements:
        ticker = profile.ticker
        stmt_currency_raw = income_statements[0].get("currency")
        # post-impl loop-2 ISS-025: prefer an EXPLICIT non-USD signal from
        # ANY of the 3 families over income[0]. extract_fcf and
        # historical_multiples both scan all 3 families; adr/correct was
        # the odd-one-out reading income[0] only, so a stale income[0]=USD
        # row paired with cash_flows=JPY (provider drift) would silently
        # route to the USD-default branch and consume JPY values as USD.
        cross_family_bad = _any_explicit_non_usd_across_families(
            income_statements, cash_flows, balance_sheets,
        )
        if stmt_currency_raw is None and cross_family_bad is None:
            # Missing currency everywhere — preserve existing 2-state
            # fail-close (no upstream signal we can trust). I3: skip
            # paths emit skip_reason only.
            result["correction_status"] = "skipped"
            result["skip_reason"] = (
                "income_statements[0].currency is missing — cannot safely "
                "assume USD. Fail-closed to prevent FX contamination of "
                "ADR-corrected per-share metrics."
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)

        # If income[0] missing OR USD-or-equivalent but ANOTHER family has
        # an explicit non-USD currency, trust the explicit signal.
        if cross_family_bad is not None and not (
            isinstance(stmt_currency_raw, str)
            and stmt_currency_raw.strip().upper() != "USD"
        ):
            raw_currency = cross_family_bad
        else:
            raw_currency = stmt_currency_raw
        detected_currency = (
            str(raw_currency).strip().upper()
            if isinstance(raw_currency, str) else None
        )

        # Step A — parseable-ISO-4217 gate (D3 routing rule).
        if detected_currency is None or not re.match(r"^[A-Z]{3}$",
                                                      detected_currency):
            # Mutate the base `result` (not a sparse literal) so the
            # documented contract key `needs_correction` survives the
            # skip. fetch.py:_main_impl hard-indexes it on a diagnostic
            # log line; a sparse dict here KeyError'd the whole fetch for
            # SEK / other unsupported-currency ADRs (SIVEF regression).
            result["status"] = "skipped"
            result["correction_status"] = "skipped"
            result["error"] = (
                f"fx_currency_unrecognized: ticker={ticker!r} "
                f"statement currency={raw_currency!r} is not a "
                f"parseable 3-letter ISO 4217 code"
            )
            result["fx_failure_reason"] = "fx_currency_unrecognized"
            result.pop("message", None)
            return emit_dl3c_root_marker(result)

        if detected_currency != "USD":
            # Step B — supported-set lookup.
            if detected_currency not in SUPPORTED_FX_CURRENCIES:
                result["status"] = "skipped"
                result["correction_status"] = "skipped"
                result["error"] = (
                    f"fx_currency_unsupported: {detected_currency} "
                    f"(parseable ISO 4217 but not in v1 supported set "
                    f"{sorted(SUPPORTED_FX_CURRENCIES)})"
                )
                result["fx_failure_reason"] = "fx_currency_unsupported"
                result.pop("message", None)
                return emit_dl3c_root_marker(result)

            # Invariant 8 — annual path is USD-only after DL3c. Fail-close
            # before invoking the carve-out helper. Mirrors the live mode
            # selector at line ~219 below (`row_matches_period(income[0],
            # "annual")`) — keeps the gate in lock-step with the actual
            # branch selection rather than a cross-family scan.
            if _uses_annual_mode(income_statements):
                result["status"] = "skipped"
                result["correction_status"] = "skipped"
                result["error"] = (
                    f"fx_unsupported_annual_path: ticker {ticker} "
                    f"non-USD ({detected_currency}) with annual-mode "
                    f"statements (income[0].period='annual'). DL3c "
                    f"does not convert FX for annual ADR carve-out "
                    f"path; needs DL5."
                )
                result["fx_failure_reason"] = "fx_unsupported_annual_path"
                result.pop("message", None)
                return emit_dl3c_root_marker(result)

            # Apply FX (supported quarterly non-USD path). Step 2 of
            # apply_fx_conversion scans cross-family currency uniformity
            # — subsumes the prior _any_explicit_non_usd scan-all-rows
            # gate. Mixed-currency input fails with `fx_mixed_currency_window`.
            # Lazy import (see module docstring): re-resolves on every call
            # so monkeypatch on scripts.fx_apply.apply_fx_conversion works.
            from scripts.fx_apply import (
                apply_fx_conversion,
                build_cert_block,
            )
            ok, fx_window, reason, fx_warnings = apply_fx_conversion(
                income_statements=income_statements,
                cash_flows=cash_flows,
                balance_sheets=balance_sheets,
                detected_currency=detected_currency,
                consumer_name="adr_correct",
                consumer_fields=dict(ADR_CORRECT_MONEY_FIELDS),
                ticker=ticker,
            )
            if not ok:
                result["status"] = "skipped"
                result["correction_status"] = "skipped"
                result["error"] = f"fx conversion failed: {reason}"
                result["fx_failure_reason"] = reason
                result["warnings"] = fx_warnings
                result.pop("message", None)
                return emit_dl3c_root_marker(result)
            # Surface warnings into result's existing warning channel.
            result.setdefault("warnings", []).extend(fx_warnings)
            # Cert block at root (basis=usd_converted + 3 anti-hallucination tags).
            result.update(build_cert_block(detected_currency, fx_window))
            # Statement rows now USD-tagged in place (Step 7); downstream
            # quarterly logic at line ~350+ runs unchanged on USD values.
        # else: detected_currency == "USD" — NO cert emitted (invariant 7).

    raw_price = price_data.get("price")
    if isinstance(raw_price, bool) or raw_price is None:
        result["message"] = "Insufficient data for ADR correction"
        return emit_dl3c_root_marker(result)
    try:
        current_price = float(raw_price)
    except (TypeError, ValueError):
        result["message"] = f"Non-numeric ADR price: {raw_price}"
        return emit_dl3c_root_marker(result)
    # Post-impl ISS-042 (fresh-loop5; symmetric extension of cycle 1
    # ISS-003/004 NaN/Inf rejection from extract_fcf): reject non-finite
    # prices before they reach `adr_units = market_cap / current_price`.
    # Pre-fix `float("nan")` / `float("inf")` passed the prior bool/None
    # check and propagated through `current_price <= 0` (NaN > 0 is False,
    # so it didn't trip the guard) → adr_units became NaN/Inf and every
    # corrected_* ratio became silently garbage.
    if not math.isfinite(current_price):
        result["message"] = f"Non-finite ADR price: {raw_price}"
        return emit_dl3c_root_marker(result)

    if not income_statements:
        result["message"] = "Insufficient data for ADR correction"
        return emit_dl3c_root_marker(result)
    if current_price <= 0:
        result["message"] = "Invalid non-positive ADR price"
        return emit_dl3c_root_marker(result)

    # Detect annual vs quarterly statements
    # Post-impl ISS-042 (cycle 13): use row_matches_period to detect annual
    # mode — case-insensitive, whitespace-tolerant. Pre-fix raw `== "annual"`
    # would misclassify period="Annual" / " annual" rows as quarterly mode,
    # then the row_matches_period filter (cycle 12 ISS-039) would drop all
    # rows because they ARE annual semantically. Same root cause as the
    # cycle 12 refactor — this is the mode-detection site the refactor missed.
    is_annual = row_matches_period(income_statements[0], "annual")

    if is_annual:
        min_statements = 1
    else:
        min_statements = 4

    if len(income_statements) < min_statements:
        result["message"] = "Insufficient data for ADR correction"
        return emit_dl3c_root_marker(result)

    # Step 1: Use company_facts.market_cap as true market cap
    market_cap = _sf(company_market_cap, default=None)
    if not market_cap or market_cap <= 0:
        result["message"] = "company_facts.market_cap unavailable for ADR correction"
        return emit_dl3c_root_marker(result)

    # Step 2: Derive ADR units (ratio derivation deferred until Step 3 locks
    # the aligned current_window — pre-fix used `income_statements[0]` here,
    # which silently mixed periods when aligned_quarters selected an older
    # window because newer income rows lacked cash_flow / balance counterparts.
    # Post-impl ISS-024 (cycle 4 HIGH).
    adr_units = market_cap / current_price
    latest_shares = None
    adr_ratio = None

    # Step 3: Calculate TTM financial totals
    # Filter via row_matches_period (post-impl ISS-039 structural fix):
    # delegate to the schema-layer helper so caller semantics CANNOT drift
    # from _is_quarterly_vocab. Handles None-period + fiscal_period regex
    # fallback + case-insensitive period matching uniformly.
    # Post-impl ISS-044 (zero-context cycle): cf/balance filter uses
    # accept_missing=True for annual mode — providers commonly omit period
    # on cash_flow / balance_sheet rows even when income carries explicit
    # period="annual". Income filter stays strict (must match is_annual
    # mode detection above for consistency).
    _period_target = "annual" if is_annual else "quarterly"
    income_statements = [
        s for s in income_statements if row_matches_period(s, _period_target)
    ]
    matched_cfs = [
        cf for cf in cash_flows
        if row_matches_period(cf, _period_target, accept_missing=is_annual)
    ]
    matched_bss = [
        bs for bs in balance_sheets
        if row_matches_period(bs, _period_target, accept_missing=is_annual)
    ]
    # Revalidate length after period filter (filtering may drop below minimum)
    min_required = 1 if is_annual else 4
    if len(income_statements) < min_required:
        result["message"] = "Insufficient {} income statements after period filter ({} < {})".format(
            _period_target, len(income_statements), min_required)
        return emit_dl3c_root_marker(result)

    # Prior-period TTM holder (populated for quarterly via iter helper [-2:]).
    prior_window = None
    growth_unavailable_reason = None

    if is_annual:
        # Annual path — DL4 §3.2 carve-out. Single-row slices are the
        # canonical annual semantic (1 year = 1 TTM). aligned_quarters is
        # quarterly-only.
        # Post-impl ISS-007 (fresh-loop1): align income / cf / bs by
        # report_period before slicing. Pre-fix the three sources were
        # independently sliced [:1] / [0]; if providers returned annual
        # income for FY2024 but annual cash_flow for FY2023 (FY-end shifts
        # / restatements), the rows silently combined across fiscal years
        # and produced cross-period EPS / FCF / balance math under
        # `correction_status="applied"`. Now: select income[0]'s
        # report_period as anchor, pick the cf/bs row with the SAME
        # report_period (or fail-close if none).
        ttm_slice = income_statements[:1]  # fail-open-ok: annual one-row slice (DL4 §3.2 carve-out)
        anchor_rp = ttm_slice[0].get("report_period") if ttm_slice else None
        if not isinstance(anchor_rp, str) or not anchor_rp:
            result["correction_status"] = "skipped"
            result["skip_reason"] = (
                "annual path: income[0] missing report_period; cannot "
                "align cash_flow / balance_sheet (ISS-007 fail-close)."
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        # Locate cf / bs rows matching anchor_rp.
        # fresh-loop2 cycle 4 C4C-MED-1: remove the legacy "no_rp_rows ==
        # single accept" fallback. The previous fallback accepted a single
        # cf/bs row with missing/empty report_period as "unambiguous" —
        # but that row could be from any fiscal year (provider-side
        # sparsity does not guarantee year-alignment with the income
        # anchor). Real-world risk: an annual run picks up a prior-FY
        # balance sheet because the current FY balance row was missing
        # report_period entirely. Verified against fixtures + reports/ —
        # zero existing artifacts exercise the no-rp branch, so removal
        # has no replay-drift impact.
        def _pick_aligned(rows: list, label: str):
            same_rp = [r for r in rows if r.get("report_period") == anchor_rp]
            if same_rp:
                return same_rp[0]
            return None
        cf_row = _pick_aligned(matched_cfs, "cash_flow") if matched_cfs else None
        bs_row_pick = _pick_aligned(matched_bss, "balance_sheet") if matched_bss else None
        # Post-impl ISS-031 (fresh-loop2): handle EMPTY-after-period-filter
        # cleanly — the EPS correction (income / adr_units) is still valid
        # when only income data is available, so we must NOT blanket
        # fail-close here. Instead, mark cf/bs availability so downstream
        # per-ratio gates emit None (not 0) for FCF / D&A / EBITDA / PB /
        # EV ratios. Pre-fix the empty-list path silently defaulted those
        # to 0 under correction_status="applied" — making it look like the
        # company had zero FCF / zero equity rather than data unavailable.
        cf_data_available = bool(cf_row is not None)
        bs_data_available = bool(bs_row_pick is not None)
        # Mismatch case: rows existed but none aligned at anchor_rp. Distinct
        # failure mode from "no rows at all" — operator should investigate
        # before assuming "data unavailable" is benign.
        if cf_row is None and matched_cfs:
            result["correction_status"] = "skipped"
            result["skip_reason"] = (
                f"annual path: no cash_flow row at income.report_period="
                f"{anchor_rp} (and no unambiguous fallback); ISS-007 fail-close."
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        if bs_row_pick is None and matched_bss:
            result["correction_status"] = "skipped"
            result["skip_reason"] = (
                f"annual path: no balance_sheet row at income.report_period="
                f"{anchor_rp} (and no unambiguous fallback); ISS-007 fail-close."
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        ttm_income_rows = ttm_slice
        ttm_cf_rows = [cf_row] if cf_row is not None else []
        bs_row = bs_row_pick

        # DL3c §3.4.2 — defensive triple-check assertion. Invariant 8: the
        # annual carve-out branch is USD-only after DL3c. The upstream
        # §3.4.1 gate fail-closes on non-USD annual input before this
        # branch executes; this raise catches refactor errors that bypass
        # that gate. F-5 carve-out: this is the ONE deliberate raise in
        # adr/correct's FX flow (compared to the marker-wrapped returns
        # in §3.4.1 / §3.4.5). If it fires, the CLI traceback surfaces
        # the regression loudly instead of emitting partially-converted
        # ratios. The existing ISS-008 row-level USD checks below are a
        # belt-and-suspenders second layer.
        _enter_annual_carve_out_assert_usd(
            ttm_income_rows[0] if ttm_income_rows else None,
            cf_row,
            bs_row_pick,
            ticker=profile.ticker,
        )

        # Post-impl ISS-008 (fresh-loop1): annual path must also enforce
        # explicit USD on the aligned income / cash_flow / balance rows.
        # The quarterly path delegates to _build_aligned_quarter which now
        # raises on missing/disagreeing currency; the annual carve-out
        # bypasses that helper, so check here at the row level. Missing
        # currency or non-USD → fail-close (don't trust upstream alone).
        for label, row in (("income", ttm_income_rows[0] if ttm_income_rows else None),
                           ("cash_flow", cf_row),
                           ("balance_sheet", bs_row_pick)):
            if row is None:
                continue
            row_cur = row.get("currency")
            if not isinstance(row_cur, str) or not row_cur.strip():
                result["correction_status"] = "skipped"
                result["skip_reason"] = (
                    f"annual path: {label} row at {anchor_rp} has no "
                    f"currency field; cannot establish USD invariant "
                    f"(ISS-008 fail-close)."
                )
                result.pop("message", None)
                return emit_dl3c_root_marker(result)
            if row_cur.strip().upper() != "USD":
                result["correction_status"] = "skipped"
                result["skip_reason"] = (
                    f"annual path: {label} row at {anchor_rp} currency="
                    f"{row_cur!r}; USD required (ISS-008 fail-close)."
                )
                result.pop("message", None)
                return emit_dl3c_root_marker(result)
    else:
        # Quarterly path — DL4 §3.2: lock current TTM to the STRICT trailing-4
        # aligned window (via aligned_quarters) and derive the YoY prior block
        # from iter_aligned_quarter_windows. The cycle-2 HIGH-1 lock on
        # aligned_quarters (forbids delegating to iter) applies here too: if
        # we read current_window from `valid_windows[-1]`, an iter run on
        # non-consec latest-4 silently returns the older valid window —
        # reintroducing the very stale-latest anti-pattern aligned_quarters
        # was built to prevent (post-impl ISS-016).
        try:
            current_window = aligned_quarters(
                income_statements, matched_cfs, matched_bss,
                ticker=profile.ticker,
            )
        except InsufficientQuartersError as e:
            result["correction_status"] = "skipped"
            result["skip_reason"] = (
                f"aligned_quarters: failure_kind={e.failure_kind} "
                f"detail={e!s}"
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)

        # Post-impl ISS-031 (cycle 7; 4th P1 instance — read-against-pre-
        # aligned-window root cause). `_build_aligned_quarter` defaults
        # statement_currency to "UNKNOWN" when no row carries a currency
        # field. The upstream gate validates income[0] + scans-all-rows for
        # explicit non-USD, but it cannot guarantee the SELECTED aligned
        # window has known-USD rows (the dropped-first-row + all-rest-
        # currency-missing case yields a "UNKNOWN" window past every
        # upstream check). Post-window assertion closes the gap until the
        # v2 structural fix (`expected_currency` kwarg on aligned_quarters
        # at the schema layer) ships.
        for q in current_window:
            # statement_currency is canonicalized via strip().upper() inside
            # _build_aligned_quarter (post-impl ISS-032), so direct == "USD"
            # comparison works for "usd" / " USD " inputs too.
            if q.statement_currency != "USD":
                result["correction_status"] = "skipped"
                result["skip_reason"] = (
                    f"aligned window statement_currency="
                    f"{q.statement_currency!r} at {q.report_period}; "
                    f"USD required (post-impl ISS-031)."
                )
                result.pop("message", None)
                return emit_dl3c_root_marker(result)

        # YoY prior block: iter_aligned_quarter_windows yields sliding stride-1
        # windows oldest-first. The legacy `income_statements[4:8]` was a
        # non-overlapping prior block 4 quarters earlier — replicate that by
        # finding the iter window anchored at current_window[3].report_period
        # minus 4 quarters (= valid_windows[-5] when current matches the
        # latest iter anchor).
        # iter eagerly raises InsufficientQuartersError on certain failure_kinds
        # (statement_metadata_mismatch, duplicate_report_period) per spec
        # §3.1 — even mid-iteration on an OLDER historical window. Wrap in
        # try/except so a historical metadata defect on a non-current window
        # doesn't crash the function after current TTM was already locked by
        # aligned_quarters() above. Post-impl ISS-021 (cycle 3 HIGH).
        # Post-impl ISS-049 (zero-context round 5 MEDIUM): manual `for` loop
        # instead of list-comp so a mid-iteration raise preserves the windows
        # iter ALREADY yielded. Pre-fix list-comp discarded everything on
        # raise — even when current TTM + YoY prior had both been yielded
        # cleanly and only a still-older historical window triggered the
        # eager raise. The cycle-3 ISS-021 wrap solved "crash propagates"
        # but the comp-vs-for distinction matters for "preserve partial
        # success on mid-iter raise."
        valid_windows: list[AlignedQuarterWindow] = []
        try:
            for w, _skip in iter_aligned_quarter_windows(
                income_statements, matched_cfs, matched_bss,
                ticker=profile.ticker,
            ):
                if w is not None:
                    valid_windows.append(w)
        except InsufficientQuartersError:
            # Iter aborted on a historical defect; keep pre-yielded windows.
            # Current TTM was already locked above via aligned_quarters().
            pass
        # Find YoY prior window by fiscal_period match (post-impl ISS-053
        # MEDIUM): pre-fix `valid_windows[-5]` assumed sliding-stride-1
        # with no intervening skips. But iter yields (None, SkippedWindow)
        # pairs for non_consecutive / unparseable_fiscal_period at
        # intermediate positions; the consumer's `if w is not None` filter
        # drops those, so list-index distance NO LONGER equals
        # report-period distance. A mid-iter skip silently misaligns YoY
        # by 1+ extra quarters, producing wrong eps_growth_rate / PEG.
        # Search by anchor fiscal_period instead: target is current_window[3]
        # fiscal_period minus 4 quarters (same Q, previous year).
        prior_window = None
        cur_fp = current_window[3].fiscal_period or ""
        if "-Q" in cur_fp:
            try:
                cur_year, cur_q = cur_fp.split("-Q", 1)
                target_yoy_fp = f"{int(cur_year) - 1}-Q{int(cur_q)}"
                for w in valid_windows:
                    if w[3].fiscal_period == target_yoy_fp:
                        # Post-impl ISS-054 (zero-context round 7 HIGH):
                        # symmetric extension of ISS-031 USD assertion from
                        # current_window to prior_window. _build_aligned_quarter
                        # defaults statement_currency="UNKNOWN" when no row
                        # carries currency; the cycle-7 fix asserted USD on
                        # current but not on prior. A prior_window with UNKNOWN
                        # currency mixed with USD current_window produces
                        # cross-currency eps_growth_rate / corrected_peg math.
                        if all(q.statement_currency == "USD" for q in w):
                            prior_window = w
                        break
            except ValueError:
                pass

        ttm_income_rows = [q.income_row for q in current_window]
        ttm_cf_rows = [q.cash_flow_row for q in current_window]
        bs_row = current_window[3].balance_row  # latest quarter's balance

        if prior_window is None:
            growth_unavailable_reason = (
                "YoY prior 4-quarter window not found (anchor fiscal_period "
                "4 quarters before current must exist as a valid aligned "
                "window from iter_aligned_quarter_windows)"
            )

    # Step 2 (deferred — post-impl ISS-024): derive latest_shares from the
    # aligned-window's latest income row (quarterly path) or income_statements[0]
    # (annual carve-out). Pre-fix this lived at line 173 before the period
    # filter + aligned_quarters call, so income_statements[0] could be a newer
    # row than current_window[3].income_row when newest income lacked
    # cash_flow / balance counterparts — silently mixing periods between the
    # share-count baseline and the TTM aggregation.
    if is_annual:
        _latest_inc_row = income_statements[0]  # fail-open-ok: annual one-row slice (DL4 §3.2 carve-out)
    else:
        _latest_inc_row = current_window[3].income_row
    latest_shares = _sf(_latest_inc_row.get("weighted_average_shares_diluted"), default=None)
    if latest_shares is None:
        latest_shares = _sf(_latest_inc_row.get("weighted_average_shares"), default=None)

    if latest_shares is not None and latest_shares > 0 and adr_units > 0:
        raw_ratio = latest_shares / adr_units
        if abs(raw_ratio - round(raw_ratio)) < 0.15:
            adr_ratio = round(raw_ratio)
        else:
            adr_ratio = round(raw_ratio, 1)

    # Post-impl ISS-014 (fresh-loop1): fail-close when net_income is absent
    # or non-numeric on ANY consumed income row. Pre-fix `_sf(None) → 0`
    # made `ttm_net_income` a partial sum (e.g. 3-of-4 quarters' values)
    # but the result was still emitted as `corrected_ttm_eps` with
    # `correction_status="applied"`, silently understating EPS by 1/4 or
    # more. The downstream prompt (and any external reader of
    # bq_analysis.json) cannot distinguish this from a real low-EPS quarter.
    _missing_ni = [
        f"{s.get('report_period', '?')}={s.get('net_income')!r}"
        for s in ttm_income_rows
        if not (isinstance(s.get("net_income"), (int, float))
                and not isinstance(s.get("net_income"), bool)
                and math.isfinite(float(s.get("net_income"))))
    ]
    if _missing_ni:
        result["correction_status"] = "skipped"
        result["skip_reason"] = (
            "net_income missing or non-numeric on "
            f"{len(_missing_ni)}/{len(ttm_income_rows)} income row(s): "
            + "; ".join(_missing_ni[:4])
            + ("; ..." if len(_missing_ni) > 4 else "")
            + " (ISS-014 fail-close)"
        )
        result.pop("message", None)
        return emit_dl3c_root_marker(result)

    ttm_net_income = sum(_sf(s.get("net_income")) for s in ttm_income_rows)

    # Post-impl ISS-036 (fresh-loop3; symmetric extension of ISS-014):
    # parallel availability tracking for revenue / EBIT / D&A / OCF /
    # capex / balance-sheet fields. Pre-fresh-loop3 only net_income had
    # the fail-close gate; all other fields silently `_sf(None) → 0`,
    # producing partial-sum TTM totals that emitted ratios under
    # correction_status="applied" with wrong values. Each missing
    # field flips the corresponding `*_complete` flag; downstream
    # ratio emission gates on the flag and emits None when incomplete.
    def _all_rows_have_finite(rows: list, field: str) -> bool:
        return all(_ifn(r.get(field)) for r in rows) if rows else False
    def _all_rows_have_revenue(rows: list) -> bool:
        # total_revenue preferred, else revenue (mirrors _revenue_of below).
        for r in rows:
            tr = r.get("total_revenue")
            v = tr if tr is not None else r.get("revenue")
            if not _ifn(v):
                return False
        return bool(rows)
    revenue_complete = _all_rows_have_revenue(ttm_income_rows)
    ebit_complete = _all_rows_have_finite(ttm_income_rows, "operating_income")
    da_complete = _all_rows_have_finite(ttm_cf_rows, "depreciation_and_amortization")
    ocf_complete = _all_rows_have_finite(ttm_cf_rows, "net_cash_flow_from_operations")
    capex_complete = _all_rows_have_finite(ttm_cf_rows, "capital_expenditure")
    fcf_complete = ocf_complete and capex_complete

    # Post-impl ISS-058 (zero-context round 8 LOW): two-step `is not None`
    # selection (ISS-220 4.34 pattern applied here too). Pre-fix `_sf(tr)
    # or _sf(rev)` treated a legitimate `total_revenue=0` as falsy and
    # silently fell through to the `revenue` field — same false-trigger
    # surface that the SBC site in detect.py already fixed. The trade-off
    # is conservative: zero-revenue companies are vanishingly rare, but
    # the asymmetry across sites is real.
    def _revenue_of(s):
        tr = s.get("total_revenue")
        return _sf(tr if tr is not None else s.get("revenue"))
    ttm_revenue = sum(_revenue_of(s) for s in ttm_income_rows)
    ttm_ebit = sum(_sf(s.get("operating_income")) for s in ttm_income_rows)

    ttm_da = 0
    if ttm_cf_rows:
        ttm_da = sum(
            _sf(s.get("depreciation_and_amortization")) for s in ttm_cf_rows
        )

    ttm_ebitda = ttm_ebit + ttm_da

    # Post-impl ISS-031 (fresh-loop2): track cf_data_available / bs_data_available
    # explicitly. Quarterly path always has both (aligned_quarters requires
    # 3-family intersection); annual path can have either empty after the
    # period filter — those ratios must emit None, not 0, so the prompt
    # layer sees "data unavailable" rather than "company has zero FCF".
    if is_annual:
        # cf_data_available and bs_data_available were set above in the
        # annual branch. Re-confirm using the actual row presence.
        cf_data_available = bool(ttm_cf_rows)
        bs_data_available = bs_row is not None
    else:
        cf_data_available = True  # quarterly path: window guarantees presence
        bs_data_available = True

    ttm_fcf = None
    if ttm_cf_rows and fcf_complete:
        ttm_ocf = sum(
            _sf(s.get("net_cash_flow_from_operations")) for s in ttm_cf_rows
        )
        ttm_capex = sum(
            abs(_sf(s.get("capital_expenditure"))) for s in ttm_cf_rows
        )
        ttm_fcf = ttm_ocf - ttm_capex

    # Balance sheet -- coerce all numerics. For quarterly: latest aligned
    # quarter's balance row from the window. For annual: matched_bss[0].
    # Post-impl ISS-037 (fresh-loop3): track per-field availability so EV /
    # PB / BVPS emit None on missing (pre-fix `_sf(None) → 0` made debt /
    # cash absence indistinguishable from "no debt / no cash" companies).
    if bs_row:
        equity_complete = _ifn(bs_row.get("shareholders_equity"))
        debt_complete = _ifn(bs_row.get("total_debt"))
        cash_complete = _ifn(bs_row.get("cash_and_equivalents"))
    else:
        equity_complete = debt_complete = cash_complete = False
    shareholders_equity = (
        _sf(bs_row.get("shareholders_equity")) if bs_row else None
    )
    total_debt = _sf(bs_row.get("total_debt")) if bs_row else None
    cash = (
        _sf(bs_row.get("cash_and_equivalents")) if bs_row else None
    )

    # Step 4: Derive corrected EPS = net_income / adr_units
    corrected_ttm_eps = ttm_net_income / adr_units if adr_units > 0 else None

    if corrected_ttm_eps is None:
        result["message"] = "Cannot compute corrected EPS"
        return emit_dl3c_root_marker(result)

    # Step 5: Compute all corrected ratio metrics.
    # Post-impl ISS-031 (fresh-loop2) + ISS-037 (fresh-loop3): gate ev /
    # fcf / bs-derived ratios on cf_data_available + bs_data_available
    # PLUS per-field availability flags. Pre-fresh-loop3 the data-
    # availability flags only fired in the annual carve-out; quarterly
    # path still silently `_sf(None)→0`'d debt/cash/equity. Now every
    # ratio emits None when ANY required field across the 4 quarters
    # was missing or non-finite — observation-vs-zero distinction
    # finally honest.
    if bs_data_available and debt_complete and cash_complete:
        ev = market_cap + total_debt - cash
    else:
        ev = None

    corrected_pe = (
        current_price / corrected_ttm_eps if corrected_ttm_eps > 0 else None
    )
    corrected_pb = (
        market_cap / shareholders_equity
        if (equity_complete
            and shareholders_equity is not None and shareholders_equity > 0)
        else None
    )
    corrected_ps = (
        market_cap / ttm_revenue
        if (revenue_complete and ttm_revenue and ttm_revenue > 0)
        else None
    )
    corrected_ev_ebitda = (
        ev / ttm_ebitda
        if (ev is not None and cf_data_available
            and ebit_complete and da_complete
            and ttm_ebitda and ttm_ebitda > 0)
        else None
    )
    corrected_ev_revenue = (
        ev / ttm_revenue
        if (ev is not None and revenue_complete
            and ttm_revenue and ttm_revenue > 0)
        else None
    )
    corrected_fcf_yield = (
        ttm_fcf / market_cap
        if (ttm_fcf is not None and fcf_complete and market_cap > 0)
        else None
    )

    # EPS growth (YoY) -- use period-appropriate share counts when available
    eps_growth_rate = None

    def _prev_adr_units(prev_stmt):
        """Derive ADR units for a prior period using its own share count.

        Post-impl ISS-035 (cycle 11 HIGH): wrap share-count reads in `_sf()`
        so numeric strings (provider sometimes returns share counts as str)
        coerce to float instead of crashing `prev_shares / latest_shares`
        with TypeError. The DL4 quarterly path made this code path more
        reachable (prior_window from aligned_quarters), exposing a latent
        brittleness.
        """
        prev_shares = _sf(prev_stmt.get("weighted_average_shares_diluted"), default=None)
        if prev_shares is None:
            prev_shares = _sf(prev_stmt.get("weighted_average_shares"), default=None)
        if prev_shares and latest_shares and latest_shares > 0:
            ratio = prev_shares / latest_shares
            return adr_units * ratio
        return adr_units  # fallback: assume unchanged

    if is_annual:
        if len(income_statements) >= 2:
            # Post-impl ISS-041 (fresh-loop5; symmetric extension of
            # ISS-040 from quarterly path): fail-close on prior annual
            # net_income missing / non-numeric. Pre-fix `_sf(None) → 0`
            # silently produced eps_growth_rate from partial prior data,
            # mirror of the issue already closed for current-period TTM
            # and quarterly prior_window. Also require the prior row to
            # carry the same USD currency invariant the current annual
            # path enforces (ISS-008).
            _prior_row = income_statements[1]  # fail-open-ok: annual one-row slice (DL4 §3.2 carve-out)
            _prior_ni_raw = _prior_row.get("net_income")
            _prior_cur = _prior_row.get("currency")
            if not _ifn(_prior_ni_raw):
                growth_unavailable_reason = (
                    f"annual prior net_income missing or non-numeric "
                    f"(raw={_prior_ni_raw!r}); ISS-041 fail-close."
                )
            elif (not isinstance(_prior_cur, str)
                  or _prior_cur.strip().upper() != "USD"):
                growth_unavailable_reason = (
                    f"annual prior row currency={_prior_cur!r}; "
                    f"USD required for cross-period growth (ISS-041 fail-close)."
                )
            else:
                prev_net_income = _sf(_prior_ni_raw)
                p_units = _prev_adr_units(_prior_row)  # fail-open-ok: annual one-row slice (DL4 §3.2 carve-out)
                prev_eps = prev_net_income / p_units if p_units > 0 else 0
                if prev_eps != 0:
                    eps_growth_rate = (corrected_ttm_eps - prev_eps) / abs(prev_eps)
    else:
        # DL4 §3.2 cycle-3 HIGH-5 fix + post-impl ISS-002 fix: prior-period
        # TTM comes from valid_windows[-5] (the non-overlapping prior aligned
        # 4-quarter window from iter_aligned_quarter_windows, anchored 4
        # quarters before current_window[3]), NOT income_statements[4:8]
        # (which silently used unaligned trailing quarters and ignored
        # cash_flow / balance alignment). When prior_window is None (fewer
        # than 5 valid windows = <8 consecutive aligned quarters),
        # growth_unavailable_reason was populated upstream and
        # eps_growth_rate stays None. Stale comment said "valid_windows[-2]"
        # — corrected post-impl ISS-036 (cycle 11 LOW doc-vs-code drift).
        if prior_window is not None:
            # Post-impl ISS-040 (fresh-loop4; symmetric to ISS-014):
            # gate prior_window net_income with the SAME fail-close check
            # as current TTM. Pre-fix prior_ttm could become a partial-sum
            # 3-of-4 quarters when prior_window had any null net_income,
            # producing wrong eps_growth_rate / corrected_peg under
            # correction_status="applied" — exactly the same fail-open
            # asymmetry the current-window fix closed.
            _prior_missing_ni = [
                f"{q.report_period}={q.income_row.get('net_income')!r}"
                for q in prior_window
                if not _ifn(q.income_row.get("net_income"))
            ]
            if _prior_missing_ni:
                growth_unavailable_reason = (
                    "prior_window net_income missing or non-numeric on "
                    f"{len(_prior_missing_ni)}/4 rows: "
                    + "; ".join(_prior_missing_ni)
                    + " (ISS-040 fail-close)"
                )
            else:
                prev_ttm_net_income = sum(
                    _sf(q.income_row.get("net_income")) for q in prior_window
                )
                # fresh-loop2 cycle 5 C-HIGH-1: use NEWEST quarter of
                # prior_window (= prior_window[3] in oldest-first order)
                # for share-count reference. Symmetric with current-period
                # `current_window[3]` baseline at L513 — pairs Q-1 with
                # Q-5 (4 quarters apart, true YoY).
                #
                # Pre-fix used `prior_window[0]` (oldest of prior TTM),
                # pairing Q-1 with Q-8 — 7 quarters apart, not YoY. The
                # legacy `income_statements[7]` comment WAS the bug: in
                # newest-first ordering, index 7 is the oldest of the
                # prior 4, which mis-anchors the prev-period share count.
                # For ADRs with active buybacks (BABA / JD / PDD / BIDU
                # class), `eps_growth_rate` and `corrected_peg` were
                # silently skewed under `correction_status="applied"`.
                p_units = _prev_adr_units(prior_window[3].income_row)
                prev_eps = prev_ttm_net_income / p_units if p_units > 0 else 0
                if prev_eps != 0:
                    eps_growth_rate = (corrected_ttm_eps - prev_eps) / abs(prev_eps)

    corrected_peg = (
        corrected_pe / (eps_growth_rate * 100)
        if corrected_pe and eps_growth_rate and eps_growth_rate > 0
        else None
    )

    # Per-share metrics — gate on data availability (ISS-031 fresh-loop2;
    # ISS-037 fresh-loop3 adds per-field completeness gating).
    corrected_bvps = (
        shareholders_equity / adr_units
        if (equity_complete and shareholders_equity is not None
            and shareholders_equity != 0 and adr_units > 0)
        else None
    )
    corrected_fcfps = (
        ttm_fcf / adr_units
        if (ttm_fcf is not None and fcf_complete and adr_units > 0)
        else None
    )

    # Determine if correction is meaningful
    result["correction_status"] = "applied"
    raw_meps = metrics_data.get("earnings_per_share")
    try:
        metrics_eps = float(raw_meps) if raw_meps is not None and not isinstance(raw_meps, bool) else None
    except (TypeError, ValueError):
        metrics_eps = None
    needs_correction = True
    if (
        metrics_eps is not None
        and corrected_ttm_eps is not None
        and metrics_eps != 0
        and abs(corrected_ttm_eps - metrics_eps) / abs(metrics_eps) < 0.10
    ):
        needs_correction = False

    result.update(
        {
            "needs_correction": needs_correction,
            "adr_ratio": adr_ratio,
            "corrected_pe": (
                round(corrected_pe, 4) if corrected_pe is not None else None
            ),
            "corrected_pb": (
                round(corrected_pb, 4) if corrected_pb is not None else None
            ),
            "corrected_ps": (
                round(corrected_ps, 4) if corrected_ps is not None else None
            ),
            "corrected_ev": round(ev, 2) if ev is not None else None,
            "corrected_ev_ebitda": (
                round(corrected_ev_ebitda, 4)
                if corrected_ev_ebitda is not None
                else None
            ),
            "corrected_ev_revenue": (
                round(corrected_ev_revenue, 4)
                if corrected_ev_revenue is not None
                else None
            ),
            "corrected_fcf_yield": (
                round(corrected_fcf_yield, 6)
                if corrected_fcf_yield is not None
                else None
            ),
            "corrected_peg": (
                round(corrected_peg, 4) if corrected_peg is not None else None
            ),
            "corrected_bvps": (
                round(corrected_bvps, 4) if corrected_bvps is not None else None
            ),
            "corrected_fcfps": (
                round(corrected_fcfps, 4) if corrected_fcfps is not None else None
            ),
            "market_cap_used": round(market_cap, 2),
            # Post-impl ISS-039 (fresh-loop4): emit TTM totals as None when
            # the completeness flag is False, mirroring the per-ratio gating.
            # Pre-fix `ttm_revenue=0` was emitted when revenue_complete=False,
            # making "data missing" look like "company had zero revenue" in
            # the persisted JSON. Operators reading bq_analysis.json cannot
            # tell those apart without inspecting the completeness flags.
            "ttm_net_income": round(ttm_net_income, 2),
            "ttm_revenue": (
                round(ttm_revenue, 2) if revenue_complete else None
            ),
            "ttm_ebit": (
                round(ttm_ebit, 2) if ebit_complete else None
            ),
            "ttm_da": (
                round(ttm_da, 2) if da_complete else None
            ),
            "ttm_ebitda": (
                round(ttm_ebitda, 2) if (ebit_complete and da_complete) else None
            ),
            "ttm_fcf": round(ttm_fcf, 2) if ttm_fcf is not None else None,
            "shareholders_equity": (
                round(shareholders_equity, 2)
                if (equity_complete and shareholders_equity is not None)
                else None
            ),
            "total_debt": (
                round(total_debt, 2)
                if (debt_complete and total_debt is not None)
                else None
            ),
            "cash": (
                round(cash, 2)
                if (cash_complete and cash is not None)
                else None
            ),
            "corrected_ttm_eps": round(corrected_ttm_eps, 4),
            "eps_growth_rate": (
                round(eps_growth_rate, 4) if eps_growth_rate is not None else None
            ),
            "message": f"ADR ratio={adr_ratio}:1, corrected using company_facts.market_cap",
        }
    )

    # DL4 §3.2: when prior_window unavailable (only 1 valid aligned window),
    # publish growth_unavailable_reason so downstream consumers can distinguish
    # "growth unknowable" from "growth computed and is None" (e.g., zero-prev-eps).
    if growth_unavailable_reason is not None:
        result["growth_unavailable_reason"] = growth_unavailable_reason

    # DL3c §4.2 — emit root marker on success path. CLI _main does a
    # defense-in-depth wrap at write_output time so the early-return
    # `return result` paths (intermediate skip/insufficient-data branches
    # predating DL3c) also acquire the marker before serialization.
    return emit_dl3c_root_marker(result)


# ---------------------------------------------------------------------------
# compute_adr_eps_check
# ---------------------------------------------------------------------------

def compute_adr_eps_check(
    profile: AdrProfile,
    metrics_data: Dict,
    financials_data: Dict,
    price_data: Dict,
    company_market_cap: float = None,
) -> Dict:
    """Cross-validate ADR EPS: compare metrics_snapshot EPS (per-ordinary) against
    corrected EPS (net_income / adr_units) derived from company_facts.market_cap.

    Uses same first-principles as compute_adr_valuation_correction:
    - adr_units = company_facts.market_cap / price
    - corrected_ttm_eps = ttm_net_income / adr_units
    - adr_ratio = ordinary_shares / adr_units
    """
    is_adr = profile.is_adr
    result = {
        "is_adr": is_adr,
        # Symmetric with compute_adr_valuation_correction L56:
        # default to "not_applicable" so missing-data early-return paths
        # leave a value in blocking_statuses, triggering CLI exit-1 for
        # ADRs (impl-loop3 F1 fix — pre-fix returned None → CLI exited 0
        # silently on missing-data ADR runs).
        "check_status": "not_applicable",  # not_applicable / skipped / applied
        "needs_ratio_adjustment": False,
        "estimated_ratio": None,
        "corrected_pe": None,
        "corrected_ttm_eps": None,
        "ttm_net_income": None,
        "message": "",
    }

    if not is_adr:
        return emit_dl3c_root_marker(result)

    income_statements = financials_data.get("income_statements", [])
    # DL4 §3.2 (cycle-3 HIGH-2): NEW INPUT READS — quarterly path now
    # consumes aligned_quarters(income, cash_flow, balance) so window
    # alignment is verified across all 3 statement families. Annual path
    # ignores these (single-row carve-out).
    cash_flows = financials_data.get("cash_flows", [])
    balance_sheets = financials_data.get("balance_sheets", [])
    raw_cp = price_data.get("price")
    if isinstance(raw_cp, bool) or raw_cp is None:
        result["message"] = "Insufficient data for ADR EPS check"
        return emit_dl3c_root_marker(result)
    try:
        current_price = float(raw_cp)
    except (TypeError, ValueError):
        result["message"] = f"Non-numeric ADR price: {raw_cp}"
        return emit_dl3c_root_marker(result)
    # Post-impl ISS-042 (fresh-loop5; parallel guard to compute_adr_
    # valuation_correction's NaN/Inf rejection): reject non-finite price
    # before downstream arithmetic.
    if not math.isfinite(current_price):
        result["message"] = f"Non-finite ADR price: {raw_cp}"
        return emit_dl3c_root_marker(result)

    if not income_statements:
        result["message"] = "Insufficient data for ADR EPS check"
        return emit_dl3c_root_marker(result)
    if current_price <= 0:
        result["message"] = "Invalid non-positive ADR price"
        return emit_dl3c_root_marker(result)

    # Sort newest-first, filtering to dict rows up-front so subsequent
    # `income_statements[0].get(...)` reads (currency guard, period peek,
    # share count) cannot AttributeError on a non-dict row (post-impl
    # ISS-019; parallel to _sort_newest in compute_adr_valuation_correction).
    # fresh-loop2-cycle2 C2C-MED-4: parallel sort-key fix (same as
    # _sort_newest above). `s.get("k", default)` returns None when the
    # key exists with value None — coerce via `or ""` to avoid
    # `'<' not supported between instances of 'str' and 'NoneType'`.
    income_statements = sorted(
        [s for s in income_statements if isinstance(s, dict)],
        key=lambda s: s.get("report_period") or "",
        reverse=True,
    )
    if not income_statements:
        result["message"] = "Insufficient data for ADR EPS check"
        return emit_dl3c_root_marker(result)

    # DL3c §3.4.5 — 3-state currency gate (parallel to §3.4.1 in
    # compute_adr_valuation_correction). F-6 anti-pattern: this function
    # uses `check_status` (NOT `correction_status`); the CLI gate at
    # _main checks `check_status` for the `adr-eps-check` subcommand.
    # F-18-1: extract ticker BEFORE string interpolation.
    # F-5: every FX fail-close path RETURNS a marker-wrapped dict — no raises.
    ticker = profile.ticker
    stmt_currency_raw = income_statements[0].get("currency")
    # post-impl loop-2 ISS-025 (parallel fix to compute_adr_valuation_
    # correction): scan all 3 families for an explicit non-USD signal.
    # If income[0]=USD-or-missing but cash_flows / balance carry an
    # explicit non-USD currency, trust the explicit signal rather than
    # silently routing to USD-default.
    cross_family_bad = _any_explicit_non_usd_across_families(
        income_statements, cash_flows, balance_sheets,
    )
    if stmt_currency_raw is None and cross_family_bad is None:
        # Missing currency everywhere — preserve existing 2-state
        # fail-close.
        result["check_status"] = "skipped"
        result["skip_reason"] = (
            "income_statements[0].currency is missing — cannot safely "
            "assume USD. Fail-closed."
        )
        result.pop("message", None)
        return emit_dl3c_root_marker(result)

    if cross_family_bad is not None and not (
        isinstance(stmt_currency_raw, str)
        and stmt_currency_raw.strip().upper() != "USD"
    ):
        raw_currency = cross_family_bad
    else:
        raw_currency = stmt_currency_raw
    detected_currency = (
        str(raw_currency).strip().upper()
        if isinstance(raw_currency, str) else None
    )

    # Step A — parseable-ISO-4217 gate.
    if detected_currency is None or not re.match(r"^[A-Z]{3}$",
                                                  detected_currency):
        # Mutate base `result` so the contract keys (needs_ratio_adjustment
        # / estimated_ratio / is_adr) survive the skip — parallel to the
        # valuation-function fix (sibling defect, MEMORY lesson 1).
        result["status"] = "skipped"
        result["check_status"] = "skipped"
        result["error"] = (
            f"fx_currency_unrecognized: ticker={ticker!r} "
            f"statement currency={raw_currency!r} is not a "
            f"parseable 3-letter ISO 4217 code"
        )
        result["fx_failure_reason"] = "fx_currency_unrecognized"
        result.pop("message", None)
        return emit_dl3c_root_marker(result)

    if detected_currency != "USD":
        # Step B — supported-set lookup.
        if detected_currency not in SUPPORTED_FX_CURRENCIES:
            result["status"] = "skipped"
            result["check_status"] = "skipped"
            result["error"] = (
                f"fx_currency_unsupported: {detected_currency} "
                f"(parseable ISO 4217 but not in v1 supported set "
                f"{sorted(SUPPORTED_FX_CURRENCIES)})"
            )
            result["fx_failure_reason"] = "fx_currency_unsupported"
            result.pop("message", None)
            return emit_dl3c_root_marker(result)

        # Invariant 8 — annual path is USD-only after DL3c.
        if _uses_annual_mode(income_statements):
            result["status"] = "skipped"
            result["check_status"] = "skipped"
            result["error"] = (
                f"fx_unsupported_annual_path: ticker {ticker} "
                f"non-USD ({detected_currency}) with annual-mode "
                f"statements"
            )
            result["fx_failure_reason"] = "fx_unsupported_annual_path"
            result.pop("message", None)
            return emit_dl3c_root_marker(result)

        # Apply FX via shared helper. consumer_name="adr_eps_check"
        # consumer_fields reads ONLY net_income from income_statements
        # (the function ignores cash_flow/balance entirely for its EPS
        # math — see annual / quarterly branches below). NO carve-out
        # registry entry needed because the two-path FCF operands aren't
        # this consumer's concern.
        # Lazy import (see module docstring): re-resolves on every call
        # so monkeypatch on scripts.fx_apply.apply_fx_conversion works.
        from scripts.fx_apply import (
            apply_fx_conversion,
            build_cert_block,
        )
        ok, fx_window, reason, fx_warnings = apply_fx_conversion(
            income_statements=income_statements,
            cash_flows=cash_flows,
            balance_sheets=balance_sheets,
            detected_currency=detected_currency,
            consumer_name="adr_eps_check",
            consumer_fields=dict(ADR_EPS_CHECK_MONEY_FIELDS),
            ticker=ticker,
        )
        if not ok:
            result["status"] = "skipped"
            result["check_status"] = "skipped"
            result["error"] = f"fx conversion failed: {reason}"
            result["fx_failure_reason"] = reason
            result["warnings"] = fx_warnings
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        result.setdefault("warnings", []).extend(fx_warnings)
        result.update(build_cert_block(detected_currency, fx_window))
        # Rows now USD-tagged; downstream EPS math runs on USD values.
    # else: USD-native — NO cert (invariant 7).

    # Annual vs quarterly awareness
    # Post-impl ISS-042 (cycle 13): use row_matches_period to detect annual
    # mode — case-insensitive, whitespace-tolerant. Pre-fix raw `== "annual"`
    # would misclassify period="Annual" / " annual" rows as quarterly mode,
    # then the row_matches_period filter (cycle 12 ISS-039) would drop all
    # rows because they ARE annual semantically. Same root cause as the
    # cycle 12 refactor — this is the mode-detection site the refactor missed.
    is_annual = row_matches_period(income_statements[0], "annual")
    # Filter via row_matches_period (post-impl ISS-039 structural fix);
    # cf/balance use accept_missing=is_annual (post-impl ISS-044, parallel
    # to compute_adr_valuation_correction site above) so the annual carve-
    # out preserves rows where providers omit period tags on cf/balance.
    _period_target = "annual" if is_annual else "quarterly"
    income_statements = [
        s for s in income_statements if row_matches_period(s, _period_target)
    ]
    matched_cfs = [
        cf for cf in cash_flows
        if row_matches_period(cf, _period_target, accept_missing=is_annual)
    ]
    matched_bss = [
        bs for bs in balance_sheets
        if row_matches_period(bs, _period_target, accept_missing=is_annual)
    ]
    min_required = 1 if is_annual else 4

    if len(income_statements) < min_required:
        result["message"] = "Insufficient data for ADR EPS check"
        return emit_dl3c_root_marker(result)

    company_market_cap = _sf(company_market_cap, default=None)
    if not company_market_cap or company_market_cap <= 0:
        result["message"] = "company_facts.market_cap unavailable for ADR EPS check"
        return emit_dl3c_root_marker(result)

    # Derive ADR units from company_facts.market_cap
    adr_units = company_market_cap / current_price

    # Latest-shares + estimated_ratio derivation is deferred until after the
    # aligned-window selection (quarterly path) or annual-slice peek (annual
    # path). Pre-fix the ADR-ratio baseline came from income_statements[0]
    # BEFORE aligned_quarters() ran, so when the latest income row lacked
    # cash_flow/balance counterparts and was dropped by the 3-family
    # intersection, the share-count baseline drifted from the TTM aggregation.
    # Post-impl ISS-027 (cycle 5; symmetric extension of ISS-024 fix applied
    # to compute_adr_valuation_correction).
    latest_shares = None
    estimated_ratio = None

    # Corrected TTM EPS = net_income / adr_units
    if is_annual:
        # Annual path UNCHANGED — DL4 §3.2 carve-out. Single-row slices are
        # the canonical annual semantic (1 year = 1 TTM). aligned_quarters
        # is quarterly-only. fresh-loop2 cycle 3 C3C-MED-3: dead
        # `_cf_slice` removed (eps_check's downstream math consumes only
        # income.net_income); replaced by the anchor-gate parity below.
        ttm_slice = income_statements[:1]  # fail-open-ok: annual one-row slice (DL4 §3.2 carve-out)
        # fresh-loop2 cycle 3 C3C-MED-3: annual anchor gate parity with
        # compute_adr_valuation_correction. Pre-fix the EPS-check annual
        # path read `ttm_slice[0]` without verifying non-empty
        # `report_period` on the anchor row. Mirrors valuation's
        # annual-mode `anchor_rp = ...` gate at L292-300.
        _anchor_rp_eps = (
            ttm_slice[0].get("report_period")
            if ttm_slice and isinstance(ttm_slice[0], dict) else None
        )
        if not (isinstance(_anchor_rp_eps, str) and _anchor_rp_eps.strip()):
            result["check_status"] = "skipped"
            result["skip_reason"] = (
                "annual EPS-check anchor row missing/empty report_period; "
                "C3C-MED-3 fail-close parity with valuation path."
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        # Post-impl ISS-029 (fresh-loop2; symmetric extension of ISS-014):
        # parallel fail-close on missing/non-numeric net_income. Pre-fix
        # `_sf(ttm_slice[0].get("net_income"))` returned 0 on absence,
        # silently producing `corrected_ttm_eps = 0 / adr_units = 0` and
        # `check_status="applied"` — exactly the same fail-open shape
        # ISS-014 closed for compute_adr_valuation_correction.
        raw_ni = ttm_slice[0].get("net_income")
        if not (isinstance(raw_ni, (int, float)) and not isinstance(raw_ni, bool)
                and math.isfinite(float(raw_ni))):
            result["check_status"] = "skipped"
            result["skip_reason"] = (
                f"annual income row net_income missing or non-numeric "
                f"(raw={raw_ni!r}); ISS-029 fail-close."
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        ttm_net_income = _sf(raw_ni)
        _latest_inc_row = ttm_slice[0]  # fail-open-ok: annual one-row slice (DL4 §3.2 carve-out)
    else:
        # Quarterly path — DL4 §3.2: consume aligned_quarters (strict
        # trailing-4 entry point). LOCAL try/except per cycle-7 §3.2.0.B
        # mandate: InsufficientQuartersError surfaces as
        # check_status=skipped, NOT a raise (callers expect the legacy
        # result-dict skip envelope, and the CLI exit-1 gate is wired to
        # blocking_statuses).
        try:
            window = aligned_quarters(
                income_statements, matched_cfs, matched_bss,
                ticker=profile.ticker,
            )
        except InsufficientQuartersError as e:
            result["check_status"] = "skipped"
            result["skip_reason"] = (
                f"aligned_quarters: failure_kind={e.failure_kind} "
                f"detail={e!s}"
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        # Post-impl ISS-031 (cycle 7; parallel to compute_adr_valuation_
        # correction same-cycle fix): verify the selected aligned window's
        # statement_currency is USD. _build_aligned_quarter defaults to
        # "UNKNOWN" when no row carries currency — upstream scan cannot
        # prove the selected window is USD-known. Fail-close on
        # non-USD/UNKNOWN window.
        for q in window:
            if q.statement_currency != "USD":
                result["check_status"] = "skipped"
                result["skip_reason"] = (
                    f"aligned window statement_currency="
                    f"{q.statement_currency!r} at {q.report_period}; "
                    f"USD required (post-impl ISS-031)."
                )
                result.pop("message", None)
                return emit_dl3c_root_marker(result)
        # Post-impl ISS-029 (fresh-loop2; symmetric extension of ISS-014):
        # quarterly path parallel to compute_adr_valuation_correction's
        # _missing_ni gate. Any of the 4 aligned income rows lacking a
        # finite numeric net_income → fail-close, not silent zero.
        _missing_ni = [
            f"{q.report_period}={q.income_row.get('net_income')!r}"
            for q in window
            if not (isinstance(q.income_row.get("net_income"), (int, float))
                    and not isinstance(q.income_row.get("net_income"), bool)
                    and math.isfinite(float(q.income_row.get("net_income"))))
        ]
        if _missing_ni:
            result["check_status"] = "skipped"
            result["skip_reason"] = (
                "net_income missing or non-numeric on "
                f"{len(_missing_ni)}/4 aligned income row(s): "
                + "; ".join(_missing_ni)
                + " (ISS-029 fail-close)"
            )
            result.pop("message", None)
            return emit_dl3c_root_marker(result)
        ttm_net_income = sum(
            _sf(q.income_row.get("net_income")) for q in window
        )
        _latest_inc_row = window[3].income_row

    # Defer latest_shares + estimated_ratio derivation to here (post-impl
    # ISS-027): use the aligned-window's latest income row for quarterly,
    # the annual-slice peek for annual. Pre-fix used income_statements[0]
    # before aligned_quarters ran, silently mixing periods when newest
    # income lacked cash_flow/balance counterparts.
    latest_shares = _sf(_latest_inc_row.get("weighted_average_shares_diluted"), default=None)
    if latest_shares is None:
        latest_shares = _sf(_latest_inc_row.get("weighted_average_shares"), default=None)
    if latest_shares is not None and latest_shares > 0 and adr_units > 0:
        raw_ratio = latest_shares / adr_units
        if abs(raw_ratio - round(raw_ratio)) < 0.15:
            estimated_ratio = round(raw_ratio)
        else:
            estimated_ratio = round(raw_ratio, 1)

    corrected_ttm_eps = ttm_net_income / adr_units if adr_units > 0 else None

    if corrected_ttm_eps is None:
        result["message"] = "Cannot compute corrected EPS"
        return emit_dl3c_root_marker(result)

    # Compare against metrics_snapshot EPS (per-ordinary-share)
    # fresh-loop2-cycle2 C2C-HIGH-1: filter NaN/Inf at the metrics_eps
    # boundary AND treat metrics_eps == 0 as a non-comparable state.
    # Pre-fix `float("NaN")` succeeded, `metrics_eps != 0` was True for
    # NaN, then `abs(corrected - nan) / abs(nan)` = NaN → `NaN > 0.10` =
    # False → `needs_adjustment = False` → `check_status="applied"` +
    # "correction not needed". Provider-EPS = 0 also passed because the
    # `metrics_eps != 0` guard skipped the deviation check entirely with
    # the default False. Both paths silently exited the CLI gate with
    # status 0 on real data-quality corruption.
    raw_metrics_eps = metrics_data.get("earnings_per_share")
    try:
        metrics_eps = float(raw_metrics_eps) if raw_metrics_eps is not None and not isinstance(raw_metrics_eps, bool) else None
    except (TypeError, ValueError):
        metrics_eps = None
    if metrics_eps is not None and not math.isfinite(metrics_eps):
        metrics_eps = None
    needs_adjustment = False
    eps_check_status: str = "applied"
    if metrics_eps is None:
        # Provider EPS missing / non-finite / non-numeric — cannot make
        # a comparison; route to skipped so the CLI blocking gate fires
        # rather than silently emitting "correction not needed".
        eps_check_status = "skipped"
    elif metrics_eps == 0:
        # Provider EPS == 0 is itself a data-quality signal: a real
        # company with non-zero corrected_ttm_eps and provider-reported
        # zero EPS indicates ordinary-share-vs-ADR-ratio mis-application
        # upstream. Mark needs_adjustment=True for visibility.
        if corrected_ttm_eps is not None and corrected_ttm_eps != 0:
            needs_adjustment = True
    else:
        deviation = abs(corrected_ttm_eps - metrics_eps) / abs(metrics_eps)
        needs_adjustment = deviation > 0.10

    corrected_pe = (
        current_price / corrected_ttm_eps if corrected_ttm_eps > 0 else None
    )

    result.update(
        {
            # Symmetric with compute_adr_valuation_correction L311 (the
            # `result["correction_status"] = "applied"` at the success-path
            # exit). Without this, the init default "not_applicable" would
            # propagate to successful ADR runs and the CLI gate would
            # exit 1 on every valid ADR (regression caught by code-reviewer
            # pre-implementation in impl-loop3).
            # fresh-loop2-cycle2 C2C-HIGH-1: route to "skipped" when
            # provider EPS is non-finite or genuinely missing — distinct
            # from the success path. The CLI blocking_statuses gate
            # already exits 1 on "skipped" so the operator sees the gap.
            "check_status": eps_check_status,
            "needs_ratio_adjustment": needs_adjustment,
            "estimated_ratio": estimated_ratio,
            "corrected_pe": (
                round(corrected_pe, 4) if corrected_pe is not None else None
            ),
            "corrected_ttm_eps": (
                round(corrected_ttm_eps, 4) if corrected_ttm_eps is not None else None
            ),
            "ttm_net_income": (
                round(ttm_net_income, 2) if ttm_net_income is not None else None
            ),
            "message": (
                # fresh-loop2-cycle2 sub-loop R-MED4: message must match
                # check_status semantics. The C2C-HIGH-1 fix introduced
                # `check_status="skipped"` for non-finite/missing
                # metrics_eps but left the message template that says
                # "EPS correction not needed" — operator reading the
                # message alone would conclude success when the gate
                # actually fired skipped.
                f"ADR ratio={estimated_ratio}:1, "
                f"provider metrics_eps unavailable / non-finite — "
                f"cross-validation skipped"
                if eps_check_status == "skipped"
                else f"ADR ratio={estimated_ratio}:1, "
                f"EPS correction {'needed' if needs_adjustment else 'not needed'}"
            ),
        }
    )

    # DL3c §4.2 — emit root marker on success path. _main also wraps
    # at write_output for defense-in-depth on intermediate returns.
    return emit_dl3c_root_marker(result)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _parse_args():
    """Parse CLI arguments for ADR correction functions."""
    import argparse
    parser = argparse.ArgumentParser(
        description="ADR valuation correction and EPS cross-validation."
    )
    sub = parser.add_subparsers(dest="command")

    # adr-valuation
    av = sub.add_parser("adr-valuation", help="Compute corrected ADR valuation metrics.")
    av.add_argument("--ticker", required=True, help="Ticker symbol; loader verifies it matches the adr_profile.")
    av.add_argument("--adr-profile", required=True, type=str, help="Path to data/adr_profile.json")
    av.add_argument("--metrics-json", required=True, help="Path to JSON file with metrics_data dict")
    av.add_argument("--financials-json", required=True, help="Path to JSON file with financials_data dict")
    av.add_argument("--price-json", required=True, help="Path to JSON file with price_data dict")
    av.add_argument("--company-market-cap", type=float, default=None, help="company_facts.market_cap value")
    av.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    # adr-eps-check
    ae = sub.add_parser("adr-eps-check", help="Cross-validate ADR EPS consistency.")
    ae.add_argument("--ticker", required=True, help="Ticker symbol; loader verifies it matches the adr_profile.")
    ae.add_argument("--adr-profile", required=True, type=str, help="Path to data/adr_profile.json")
    ae.add_argument("--metrics-json", required=True, help="Path to JSON file with metrics_data dict")
    ae.add_argument("--financials-json", required=True, help="Path to JSON file with financials_data dict")
    ae.add_argument("--price-json", required=True, help="Path to JSON file with price_data dict")
    ae.add_argument("--company-market-cap", type=float, default=None, help="company_facts.market_cap value")
    ae.add_argument("--output", default=None, help="Output file path (atomic write). Default: stdout")

    return parser.parse_args()




def _main():
    """CLI main: dispatch to ADR correction functions based on subcommand."""
    args = _parse_args()

    if not args.command:
        print("adr.correct: no subcommand specified. Use --help for usage.", file=sys.stderr)
        sys.exit(1)

    if args.command == "adr-valuation":
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
        result = compute_adr_valuation_correction(
            profile=profile,
            metrics_data=metrics,
            financials_data=financials,
            price_data=price,
            company_market_cap=args.company_market_cap,
        )

    elif args.command == "adr-eps-check":
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
        result = compute_adr_eps_check(
            profile=profile,
            metrics_data=metrics,
            financials_data=financials,
            price_data=price,
            company_market_cap=args.company_market_cap,
        )

    else:
        print(f"{_PREFIX}: unknown subcommand '{args.command}'", file=sys.stderr)
        sys.exit(1)

    # DL3c §4.2 defense-in-depth: wrap through emit_dl3c_root_marker at the
    # CLI write boundary. Idempotent (the producer functions already wrap
    # the success path); this also catches intermediate-return skip paths
    # predating DL3c that still call `return result` without marker.
    result = emit_dl3c_root_marker(result)
    write_output(result, args.output)

    # Fail-closed: if this IS an ADR and we couldn't complete the correction
    # (adr-valuation) OR the EPS check (adr-eps-check), exit nonzero so the
    # pipeline halts rather than silently using uncorrected per-share metrics
    # downstream. Both subcommands emit a `_status="skipped"` field on the
    # currency / data fail-close path; gate uniformly on whichever field the
    # subcommand owns. impl-loop1 cycle 2 fresh-challenge identified the
    # adr-eps-check exit-0 asymmetry — `adr-eps-check && downstream` did not
    # halt on currency skip pre-fix.
    is_adr_arg = profile.is_adr
    blocking_statuses = {"skipped", "insufficient_data", "not_applicable"}
    if args.command == "adr-valuation":
        _status_field = "correction_status"
    elif args.command == "adr-eps-check":
        _status_field = "check_status"
    else:
        _status_field = None
    if _status_field and is_adr_arg and isinstance(result, dict) and result.get(_status_field) in blocking_statuses:
        print(
            f"{_PREFIX}: ADR {args.command} not completed "
            f"(status={result.get(_status_field)}, "
            f"reason={result.get('skip_reason') or result.get('message') or '(none)'}) "
            f"— downstream should NOT use uncorrected per-share metrics.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    _main()
