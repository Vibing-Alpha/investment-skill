"""Extract TTM free cash flow per share from financial data.

Reads 02_financial_data.json and computes trailing-twelve-month FCF/share.
Replaces the fragile inline bash/Python in SKILL.md with a cross-platform script.

Also extracts current price and WACC inputs (risk-free rate + ERP)
from price and macro data, outputting everything the reverse DCF script needs.
"""

import json
import math
import re
import sys
from pathlib import Path
from typing import Dict


def _is_finite_number(v) -> bool:
    """Reject None, bool, NaN, Inf, and non-numeric values. Pre-fix many
    sites used `is not None` or `isinstance(v, (int, float))` which both
    let bool through (bool is int subclass) and didn't guard NaN / Inf.
    Promoted to module scope so all extraction paths share one definition
    (post-impl ISS-034 fresh-loop3)."""
    if v is None or isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False

from scripts.fcf_constants import (
    DIVERGENCE_THRESHOLD,
    FCF_SELECTION_REASON_BOTH_INVALID_NULL,
    FCF_SELECTION_REASON_BOTH_OPPOSITE_SIGN_NULL,
    FCF_SELECTION_REASON_FALLBACK_MIN_ABS,
    FCF_SELECTION_REASON_INSUFFICIENT_QUARTERS,
    FCF_SELECTION_REASON_LOW_DIVERGENCE_DEFAULT,
    FCF_SELECTION_REASON_NI_SIGN_ANCHOR,
    FCF_SELECTION_REASON_SHARES_UNAVAILABLE,
    FCF_SELECTION_REASON_SINGLE_PATH_ONLY,
    FCF_SOURCE_API_FCF,
    FCF_SOURCE_OCF_MINUS_CAPEX,
    validate_fcf_selection_reason,
)
from scripts.schemas.quarter_window import (
    InsufficientQuartersError,
    aligned_quarters,
)
# DL3c §3.2 — 3-state currency gate imports.
# `scripts.fx_apply` is imported lazily at the call site (inside
# extract_fcf_inputs) to break the import cycle: fx_apply itself imports
# `_is_finite_number` from this module (cycle-20 agent finding). Top-level
# imports here would deadlock the `python -m scripts.extract_fcf` entry
# point with `ImportError: cannot import name 'apply_fx_conversion' from
# partially initialized module 'scripts.fx_apply'`.
from scripts.cli_utils import emit_dl3c_root_marker
from scripts.sources.fx_rates import SUPPORTED_FX_CURRENCIES


def _is_quarterly_row(cf):
    """Return True if this cash-flow row is a quarterly period.

    Accept only explicit quarterly markers — a row must have either:
    - period in {'quarter', 'quarterly'}
    - fiscal_period matching 'YYYY-QN' pattern
    Rows lacking both are treated as NOT quarterly (fail-closed).
    This matches the plan's HIGH-1 spec and rules/producer-consumer.md §4
    (missing data = failure, not zero): allowing rows without period
    metadata to fall through to TTM summation would revive the exact
    TTDKY regression for any data source that omits period markers.
    """
    period = str(cf.get("period", "")).strip().lower()  # fail-open-ok: ACCEPT-list requires explicit marker; empty → returns False (HIGH-1 fix)
    if period in {"quarter", "quarterly"}:
        return True
    fp = cf.get("fiscal_period", "")  # fail-open-ok: ACCEPT-list requires YYYY-QN pattern; empty → returns False (HIGH-1 fix)
    if isinstance(fp, str) and re.match(r"^\d{4}-Q[1-4]$", fp):
        return True
    return False


def _sign(x):
    if x is None or x == 0:
        return 0
    return 1 if x > 0 else -1


def _same_nonzero_sign(values):
    """True iff every value has the same non-zero sign.

    Direct read of the NI-anchor-usable predicate: "all 4 NI rows share
    a single non-zero sign". Empty list / any-zero / mixed signs → False.
    """
    if not values:
        return False
    signs = [_sign(v) for v in values]
    return signs[0] != 0 and all(s == signs[0] for s in signs)


def _pick_min_abs_prefer_api(ttm_api, ttm_calc):
    """Return (chosen_value, fcf_source) with abs tiebreak; api wins on tie."""
    if abs(ttm_calc) < abs(ttm_api):
        return ttm_calc, FCF_SOURCE_OCF_MINUS_CAPEX
    return ttm_api, FCF_SOURCE_API_FCF


def extract_fcf_inputs(
    financial_path: Path,
    price_path: Path,
    macro_path: Path,
    beta_override: float = None,
    *,
    ticker: str,
) -> Dict:
    """Extract reverse DCF inputs from existing score-business data files.

    Args:
        financial_path: Path to 02_financial_data.json
        price_path: Path to 01_price_data.json
        macro_path: Path to 09_macro_rates.json
        beta_override: If set, use this beta instead of data-driven value.
        ticker: Ticker symbol (required, kwarg-only per DL4 §3.2.0.A
            Pattern Z.3 — threaded through to aligned_quarters for
            structured error messages).

    Returns: Dict with price, fcf_per_share, discount_rate, warnings, and metadata.
    """
    result = {"status": "ok", "errors": [], "warnings": []}

    # --- Price + Beta ---
    beta_from_data = None
    try:
        with open(price_path, "r", encoding="utf-8") as f:
            price_data = json.load(f)
        price = price_data.get("snapshot", {}).get("price")
        # Post-impl ISS-003 (fresh-loop1): explicitly reject bool. Python's
        # `isinstance(True, (int, float))` is True (bool is int subclass), and
        # `True > 0` is also True, so a JSON payload with snapshot.price=true
        # otherwise propagated as a "valid" price of 1.0. Same shape applied
        # to beta below. Also reject NaN / Inf via math.isfinite.
        if (not isinstance(price, (int, float)) or isinstance(price, bool)
                or not math.isfinite(price) or price <= 0):
            result["errors"].append("Invalid or missing snapshot.price")
            price = None
        result["current_price"] = price
        if price is not None:
            result["current_price_tag"] = "[API: 01_price_data.snapshot.price]"

        # Read beta from fetch-provided data
        beta_info = price_data.get("beta", {})
        if isinstance(beta_info, dict) and beta_info.get("value") is not None:
            beta_raw = beta_info["value"]
            # Post-impl ISS-003 / ISS-004 (fresh-loop1): reject bool, NaN, Inf
            # at the data boundary. Pre-fix `float("nan")` propagated into
            # CAPM math, and `float("inf")` was silently clamped to 0.15 by
            # the downstream guard at the cost-of-equity stage.
            if (isinstance(beta_raw, bool)
                    or not isinstance(beta_raw, (int, float))
                    or not math.isfinite(float(beta_raw))):
                result["warnings"].append(
                    f"beta: rejected non-finite or non-numeric value "
                    f"{beta_raw!r}"
                )
            else:
                beta_from_data = float(beta_raw)
                result["beta_source"] = beta_info.get("source", "price_data")
                for w in beta_info.get("warnings", []):
                    result["warnings"].append(f"beta: {w}")
    except (OSError, json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
        result["errors"].append(
            f"Failed to read price data: {type(exc).__name__}: {exc}"
        )
        result["current_price"] = None

    # --- FCF per share (TTM) — dual-path auto-selection (§2 state machine) ---
    # Spec: docs/specs/2026-04-19-extract-fcf-dual-path-design.md
    # Stages: Select → Validate paths → Decide → Emit.
    #
    # Why dual path: FD API's cash_flows[] has inconsistent field semantics —
    # net_cash_flow_from_operations / capital_expenditure / free_cash_flow
    # can be YTD-cumulative in one ticker's series and single-quarter in
    # another. Neither field wins universally.
    chosen = None
    quarterly_fcfs_for_cycle = []
    sorted_cfs_final = []
    try:
        with open(financial_path, "r", encoding="utf-8") as f:
            fin_data = json.load(f)

        cash_flows_raw = fin_data.get("cash_flows", [])
        balance_sheets = fin_data.get("balance_sheets", [])
        income_statements = fin_data.get("income_statements", [])

        # Filter strictly to quarterly rows. Annual (FY) rows pollute TTM
        # by making 4 years look like 4 quarters — observed on TTDKY
        # where fcf_inputs.json reported fcf_per_share=170.57 computed
        # as sum(4 FY FCFs) / shares, roughly 4x the true TTM. Done
        # before the length guard so the error message below correctly
        # reflects the count of *usable* (quarterly) rows.
        cash_flows = [cf for cf in cash_flows_raw if _is_quarterly_row(cf)]

        # Currency check — extract_fcf does NOT apply ADR FX conversion.
        # The check ONLY fires when cash_flows has at least one quarterly row
        # (otherwise there are no usable rows to currency-validate; the
        # downstream `len(cash_flows) < 4` branch handles empty-input). Within
        # that gate: first-row strict check (existing — None ⇒ fail-close,
        # preserved by test_missing_currency_fails_closed) PLUS scan of ALL
        # rows across the 3 statement families for explicit non-USD (post-impl
        # ISS-006): a non-USD row in any non-first position would otherwise be
        # shadowed by an iter-raised InsufficientQuartersError. Missing
        # currency outside the first-row position is tolerated.
        currency_ok = True

        def _any_explicit_non_usd(rows):
            # fresh-loop2 cycle 4 C4B-MED-2: normalize before comparison.
            # Pre-fix `cur != "USD"` strict-equality rejected legitimate
            # USD spellings `"usd"` / `"Usd"` / `" USD "` — these are
            # rare in current API responses but exist in some legacy /
            # provider-side fixtures. Parity with Pattern W AST helper's
            # cycle-3 `_is_usd_constant` normalization
            # (`value.value.strip().upper() == "USD"`).
            for row in rows:
                if isinstance(row, dict):
                    cur = row.get("currency")
                    if cur is None:
                        continue
                    if isinstance(cur, str) and cur.strip().upper() == "USD":
                        continue
                    return cur
            return None

        # fresh-loop2 ISS-004: explicit non-USD scan across all three
        # statement families MUST run independently of cash_flows length.
        # Pre-fix the entire currency block was nested under `if cash_flows:`
        # which meant an empty quarterly-filtered cash_flows list (e.g.
        # provider returned only YTD-cumulative rows that all failed
        # _is_quarterly_row) bypassed the income/balance non-USD scan, and
        # the downstream `len(cash_flows) < 4` branch emitted
        # BOTH_INVALID_NULL instead of surfacing the actual JPY/EUR/etc.
        # statement-currency signal.
        # fresh-loop2 cycle 3 C3B-MED-5: scan cash_flows_raw (pre-quarterly-
        # filter) instead of `cash_flows`. Pre-fix a provider that emits
        # annual JPY rows + quarterly USD rows in cash_flows would have
        # the JPY rows filtered out by `_is_quarterly_row` BEFORE the
        # currency scan, hiding the cross-period currency mix. Income /
        # balance scans already use the raw lists.
        #
        # post-impl loop-3 F2 fix: use explicit `is None` short-circuit
        # instead of `or` chain. The helper returns the actual currency
        # value found (which can be falsy: empty string `""` from a row
        # with `currency: ""`). Pre-fix `helper(cf) or helper(inc) or
        # helper(bal)` treated `""` as falsy and continued to the next
        # family, masking the empty-string signal and routing to USD-
        # default — a silent-corruption path if a provider ever emits
        # `currency: ""`. Defense-in-depth.
        explicit_bad_any = _any_explicit_non_usd(cash_flows_raw)
        if explicit_bad_any is None:
            explicit_bad_any = _any_explicit_non_usd(income_statements)
        if explicit_bad_any is None:
            explicit_bad_any = _any_explicit_non_usd(balance_sheets)

        # The label-based scan above trusts row.currency. It cannot catch the
        # FDS foreign-issuer bug where rows are labeled "USD" but a subset of
        # fields is still native currency (MRAAY/Murata). Detect via the
        # gross-profit accounting identity and fail-close — a mixed statement
        # silently corrupts every per-share / FCF computation, and the DL3c FX
        # gate (which also trusts the "USD" label) would NOT convert it.
        if explicit_bad_any is None:
            from scripts.schemas.currency_consistency import detect_mixed_currency
            _cc = detect_mixed_currency(income_statements)
            if _cc["status"] == "mixed":
                result["errors"].append(
                    f"financials_currency_mixed: gross-profit identity violated "
                    f"(implied FX ~{_cc['implied_fx']} on {len(_cc['mixed_rows'])} "
                    f"rows) — FDS returned a USD/native field mix under a single "
                    f"'USD' label; per-share/FCF math would be garbage."
                )
                result["fx_failure_reason"] = "financials_currency_mixed"
                result["fcf_per_share"] = None
                result["ttm_fcf"] = None
                result["fcf_source"] = None
                result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                result["fcf_divergence_pct"] = None
                currency_ok = False

        # F (codex Loop review): ratio-correction ADRs compute per-ORDINARY-share
        # FCF here (ttm_fcf / outstanding_shares) but the price is per-ADR — a
        # unit mismatch that currency repair does NOT fix. Fail-close until
        # per-ADR-units handling exists.
        if currency_ok:
            from scripts.schemas.adr_correction import adr_ratio_correction_required
            if adr_ratio_correction_required(Path(financial_path).parent):
                result["errors"].append(
                    "adr_ratio_correction_required: per-share FCF is "
                    "per-ordinary-share but price is per-ADR; extract_fcf does "
                    "not apply the ADR ratio. Fail-closed."
                )
                result["fx_failure_reason"] = "adr_ratio_correction_required"
                result["fcf_per_share"] = None
                result["ttm_fcf"] = None
                result["fcf_source"] = None
                result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                result["fcf_divergence_pct"] = None
                currency_ok = False

        if currency_ok and explicit_bad_any is not None:
            # DL3c §3.2 — three-state currency gate. Pre-DL3c this branch
            # unconditionally fail-closed on any non-USD; v1 adds the
            # supported-currency conversion path.
            #
            # `currency_ok and` (codex-review HIGH): a prior fail-close
            # (financials_currency_mixed at L272-285, or
            # adr_ratio_correction_required at L291-305) already set
            # currency_ok=False. Those fail-closes are TERMINAL — the per-share
            # output is garbage regardless of FX, so we must NOT enter the FX-
            # conversion branch and attach a usd_converted cert on top of an
            # already-failed artifact. Doing so made a ratio-unknown ADR with
            # clean non-USD statements emit fcf_inputs=usd_converted while
            # historical_multiples (which exits before FX) stayed usd_native →
            # assemble mixed-mode FATAL on re-assemble.
            raw_currency = explicit_bad_any
            detected_currency = (
                str(raw_currency).strip().upper()
                if isinstance(raw_currency, str) else None
            )
            # cycle-12 F-12-3: parseable-ISO-4217 gate per §3.6.1 D3 routing
            # (was missing in extract_fcf — only adr/correct had it).
            # `None` / `""` / `"Y"` / `"Yen"` route to `_unrecognized`,
            # NOT `_unsupported`.
            if (detected_currency is None
                    or not re.match(r"^[A-Z]{3}$", detected_currency)):
                result["errors"].append(
                    f"fx_currency_unrecognized: statement currency="
                    f"{raw_currency!r} is not a parseable 3-letter "
                    f"ISO 4217 code"
                )
                result["fx_failure_reason"] = "fx_currency_unrecognized"
                result["fcf_per_share"] = None
                result["ttm_fcf"] = None
                result["fcf_source"] = None
                result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                result["fcf_divergence_pct"] = None
                currency_ok = False
            elif detected_currency not in SUPPORTED_FX_CURRENCIES:
                result["errors"].append(
                    f"fx_currency_unsupported: {detected_currency!r} "
                    f"(parseable ISO 4217 but not in v1 supported set "
                    f"{sorted(SUPPORTED_FX_CURRENCIES)}). Add to "
                    f"SUPPORTED_FX_CURRENCIES + write fixture to extend."
                )
                result["fx_failure_reason"] = "fx_currency_unsupported"
                result["fcf_per_share"] = None
                result["ttm_fcf"] = None
                result["fcf_source"] = None
                result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                result["fcf_divergence_pct"] = None
                currency_ok = False
            else:
                # DL3c — supported non-USD: fetch FX, apply conversion via
                # shared helper, set basis=usd_converted. consumer_name=
                # "extract_fcf" activates the FX_TWO_PATH_CARVE_OUT
                # (P3 cycle-2 + cycle-3 #1: 3 two-path fields are
                # corruption-tolerant for this consumer; pre-scan skips them).
                # Lazy import to break the cycle (see top-of-file note).
                from scripts.fx_apply import (
                    apply_fx_conversion,
                    build_cert_block,
                )
                ok, fx_window, reason, fx_warnings = apply_fx_conversion(
                    income_statements=income_statements,
                    cash_flows=cash_flows,
                    balance_sheets=balance_sheets,
                    detected_currency=detected_currency,
                    consumer_name="extract_fcf",
                    consumer_fields={
                        "income_statements": ("net_income",),
                        "cash_flows": (
                            "free_cash_flow",
                            "net_cash_flow_from_operations",
                            "capital_expenditure",
                        ),
                        # post-impl loop-1 H1: balance_sheets is declared
                        # alignment-only (empty tuple). extract_fcf passes
                        # balance_sheets to aligned_quarters at line 462;
                        # without the retag, _build_aligned_quarter sees
                        # mixed currency (USD on income+cash_flows, JPY on
                        # balance) and raises statement_metadata_mismatch.
                        "balance_sheets": (),
                    },
                    ticker=ticker,
                )
                # cycle-8 F9 propagation: always extend warnings, even on
                # failure path.
                result["warnings"].extend(fx_warnings)
                if not ok:
                    result["fx_failure_reason"] = reason
                    result["errors"].append(
                        f"fx conversion failed: {reason}"
                    )
                    result["fcf_per_share"] = None
                    result["ttm_fcf"] = None
                    result["fcf_source"] = None
                    result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                    result["fcf_divergence_pct"] = None
                    currency_ok = False
                else:
                    # cycle-15 F-15-3 + cycle-16 fix: use build_cert_block
                    # helper (single emission site for cert + 3 anti-
                    # hallucination tags per invariant 14). DO NOT
                    # hand-construct — implementer drift risk.
                    result.update(
                        build_cert_block(detected_currency, fx_window)
                    )
                    currency_ok = True
                    # statement rows are now USD-tagged; downstream Stage 1-4
                    # reads USD. The first-row recheck below would re-fail
                    # without the post-apply tag — guarded by `elif cash_flows`
                    # which only fires when `explicit_bad_any is None`.
        elif currency_ok and cash_flows:
            # Missing-first-row-currency check (existing strict semantic
            # preserved — None ⇒ fail-close). Only meaningful when at least
            # one quarterly cash_flow row exists; the explicit-non-USD scan
            # above handled the populated-non-USD case for all 3 families.
            # `currency_ok and` mirrors the terminal-fail-close guard above so a
            # prior fail-close skips this recheck and falls straight to the
            # `if not currency_ok: pass` gate below.
            first_cur = cash_flows[0].get("currency")
            # fresh-loop2 cycle 4 C4B-MED-2: normalize before compare.
            first_cur_norm = (
                first_cur.strip().upper()
                if isinstance(first_cur, str) else first_cur
            )
            if first_cur_norm != "USD":
                result["errors"].append(
                    f"statement currency={first_cur!r}; extract_fcf requires "
                    f"USD-normalized statements. For ADRs with non-USD "
                    f"statements, run adr_correction upstream or supply "
                    f"pre-corrected financials. "
                    f"HIGH-26: extract_fcf does not yet apply ADR FX conversion."
                )
                result["fcf_per_share"] = None
                result["ttm_fcf"] = None
                result["fcf_source"] = None
                result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                result["fcf_divergence_pct"] = None
                currency_ok = False

        if not currency_ok:
            # Currency failure already recorded — skip remaining FCF work;
            # WACC section below still runs so discount_rate is emitted.
            pass
        elif len(cash_flows) < 4:
            # fresh-loop2 ISS-014: surface insufficient-quarter signal via
            # the DL4 closed-vocab reason (parallels the aligned_quarters
            # failure mode that fires when statements ARE present but the
            # 3-family intersection is too small). Pre-fix BOTH_INVALID_NULL
            # conflated "two TTM paths both produced null" with "we never
            # had enough rows to compute either path" — downstream consumers
            # routing on fcf_selection_reason couldn't disambiguate.
            result["errors"].append(
                f"Need 4+ quarterly cash-flow rows for TTM; got "
                f"{len(cash_flows)} after accepting only rows with "
                f"period='quarter'/'quarterly' or fiscal_period='YYYY-QN' "
                f"(prevents annual-as-TTM bug; had "
                f"{len(cash_flows_raw)} total rows before filter)"
            )
            result["fcf_per_share"] = None
            result["ttm_fcf"] = None
            result["fcf_source"] = None
            result["fcf_selection_reason"] = FCF_SELECTION_REASON_INSUFFICIENT_QUARTERS
            result["fcf_divergence_pct"] = None
        else:
            # === Stage 1: Select ===
            # fresh-loop2-cycle2 C2C-MED-4 parallel fix: `s.get("k", default)`
            # returns None on key-exists-with-None — coerce via `or ""`.
            sorted_all = sorted(cash_flows, key=lambda x: x.get("report_period") or "")

            # Alt data sources (non-FD) may never populate free_cash_flow
            # at all. The trailing-null skip is a YTD-in-progress signal
            # SPECIFIC to the FD API — applying it blindly to an alt
            # source would drop every row and lose the legitimate
            # ocf/capex fallback path. Only skip if we've seen fcf
            # populated at least once in the dataset.
            any_fcf_populated = any(
                cf.get("free_cash_flow") is not None for cf in sorted_all
            )

            if any_fcf_populated:
                # Trailing-null skip — walk from the end, drop rows
                # whose free_cash_flow is None. FD API's signal for a
                # YTD-in-progress partial quarter (ocf/capex on that
                # row are also typically YTD-contaminated). Known
                # limitation: this can drop rows where only the api
                # path lags — see spec §Known limitations.
                last_idx = len(sorted_all)
                for i in range(len(sorted_all) - 1, -1, -1):
                    if sorted_all[i].get("free_cash_flow") is None:
                        last_idx = i
                    else:
                        break
                usable = sorted_all[:last_idx]
            else:
                # Alt source — no fcf field at all. Trust the full
                # window; Stage 2 will validate ocf/capex per-row.
                usable = sorted_all

            if len(usable) < 4:
                # Error text preserves the keywords the regression
                # test `test_insufficient_populated_quarters_returns_none`
                # asserts on: "populated free_cash_flow" and
                # "refusing to fallback".
                result["errors"].append(
                    f"Only {len(usable)}/4 recent quarters have populated "
                    f"free_cash_flow; refusing to fallback to ocf-|capex| "
                    f"on potentially YTD-contaminated rows. TTM cannot be "
                    f"computed safely."
                )
                result["fcf_per_share"] = None
                result["ttm_fcf"] = None
                result["fcf_source"] = None
                result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                result["fcf_divergence_pct"] = None
            else:
                # DL4 §3.2.0.B — aligned_quarters replaces the legacy
                # independent `usable[-4:]` slice + income/balance re-sorts
                # with one cross-statement-aligned 4-quarter window.
                # LOCAL try/except runs BEFORE the outer broad-catch so
                # InsufficientQuartersError (a DataQualityError(ValueError))
                # routes to FCF_SELECTION_REASON_INSUFFICIENT_QUARTERS, NOT
                # the legacy "Failed to read financial data" both_invalid_null
                # path (cycle-3 HIGH-3 + §3.2.0.B regression lock).
                window = None
                ni_rows: list = []
                latest_balance: dict = {}
                try:
                    window = aligned_quarters(
                        income_statements,
                        usable,
                        balance_sheets,
                        ticker=ticker,
                    )
                except InsufficientQuartersError as e:
                    result["errors"].append(
                        f"aligned_quarters: failure_kind={e.failure_kind} "
                        f"available={e.available} "
                        f"dropped_rows={e.dropped_rows} detail={e!s}"
                    )
                    result["fcf_per_share"] = None
                    result["ttm_fcf"] = None
                    result["fcf_source"] = None
                    result["fcf_selection_reason"] = FCF_SELECTION_REASON_INSUFFICIENT_QUARTERS
                    result["fcf_divergence_pct"] = None
                    # Fall through to Stage 4 emit — `chosen is None` guard
                    # keeps fcf_per_share None.

                if window is not None:
                    # window[3] is the latest aligned quarter (oldest-first
                    # per spec invariant 3). Read TTM rows via
                    # window[i].cash_flow_row per invariant 4 — no
                    # independent re-sort needed (DL4 §3.2).
                    sorted_cfs = [q.cash_flow_row for q in window]
                    ni_rows = [q.income_row for q in window]
                    latest_balance = window[3].balance_row
                    sorted_cfs_final = sorted_cfs
                    latest_overall = sorted_all[-1].get("report_period", "?")
                    latest_used = sorted_cfs[-1].get("report_period", "?")
                    if any_fcf_populated and latest_overall != latest_used:
                        result["warnings"].append(
                            f"Most recent quarter ({latest_overall}) had "
                            f"free_cash_flow=null; TTM uses quarters through "
                            f"{latest_used} to avoid YTD-cumulative pollution"
                        )

                    # === Stage 2: Validate paths (fail-closed, no zero-fill) ===
                    # Post-impl ISS-024 (fresh-loop2): require not just "is
                    # not None" but a finite real number (rejecting bool,
                    # NaN, Inf, numeric strings). Pre-fix, a cash_flow row
                    # with `free_cash_flow: true` or `free_cash_flow: NaN`
                    # passed the `is not None` validation and propagated
                    # into the TTM sum as `1` or `NaN` — the latter
                    # poisoned every downstream multiple silently.
                    # `_is_finite_number` lives at module scope (fresh-loop3
                    # ISS-034) so all extraction paths share one validator.
                    api_valid = all(
                        _is_finite_number(cf.get("free_cash_flow")) for cf in sorted_cfs
                    )
                    calc_valid = all(
                        _is_finite_number(cf.get("net_cash_flow_from_operations"))
                        and _is_finite_number(cf.get("capital_expenditure"))
                        for cf in sorted_cfs
                    )

                    if not api_valid and not calc_valid:
                        result["errors"].append(
                            "Neither API free_cash_flow nor computed ocf-|capex| "
                            "could be formed over 4 quarters; missing components. "
                            "reverse_dcf will be skipped."
                        )
                        result["fcf_per_share"] = None
                        result["ttm_fcf"] = None
                        result["fcf_source"] = None
                        result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
                        result["fcf_divergence_pct"] = None
                    else:
                        # === Stage 3: Decide ===
                        ttm_api = sum(cf["free_cash_flow"] for cf in sorted_cfs) if api_valid else None
                        ttm_calc = sum(
                            cf["net_cash_flow_from_operations"] - abs(cf["capital_expenditure"])
                            for cf in sorted_cfs
                        ) if calc_valid else None

                        # Stage 3b — single-path-only
                        if api_valid != calc_valid:
                            if api_valid:
                                chosen = ttm_api
                                result["fcf_source"] = FCF_SOURCE_API_FCF
                                missing = "ocf/capex"
                                # Suppress warning when ocf/capex are simply
                                # absent from the dataset (alt source that
                                # only exposes free_cash_flow). Only warn
                                # when they're present elsewhere but null in
                                # the selected window — that's a real
                                # data-quality signal, not a source choice.
                                missing_present_anywhere = any(
                                    cf.get("net_cash_flow_from_operations") is not None
                                    and cf.get("capital_expenditure") is not None
                                    for cf in sorted_all
                                )
                            else:
                                chosen = ttm_calc
                                result["fcf_source"] = FCF_SOURCE_OCF_MINUS_CAPEX
                                missing = "free_cash_flow"
                                # Same symmetric guard for the alt-source
                                # fcf-less case.
                                missing_present_anywhere = any_fcf_populated
                            result["fcf_selection_reason"] = FCF_SELECTION_REASON_SINGLE_PATH_ONLY
                            result["fcf_divergence_pct"] = None
                            if missing_present_anywhere:
                                result["warnings"].append(
                                    f"Only {result['fcf_source']} TTM is computable; "
                                    f"{missing} has null component(s) in the 4 "
                                    f"selected quarters. No cross-check possible."
                                )
                        else:
                            # Both paths valid — compute divergence
                            denom = max(abs(ttm_api), abs(ttm_calc))
                            divergence = abs(ttm_api - ttm_calc) / denom if denom > 0 else 0.0
                            result["fcf_divergence_pct"] = round(divergence * 100, 2)

                            # Stage 3d — low divergence
                            if divergence < DIVERGENCE_THRESHOLD:
                                chosen = ttm_api
                                result["fcf_source"] = FCF_SOURCE_API_FCF
                                result["fcf_selection_reason"] = FCF_SELECTION_REASON_LOW_DIVERGENCE_DEFAULT
                                # NO divergence warning — normal case (noise reduction)
                            else:
                                # Stage 3e — must choose
                                # Read NI anchor from the ALIGNED window
                                # (DL4 §3.2 cycle-6 fix — was independent
                                # re-sort over `income_statements`).
                                # Post-impl ISS-034 (fresh-loop3): require
                                # finite real numbers (not bool, NaN, Inf,
                                # or numeric strings) so the sign anchor
                                # is built from real values only.
                                # Pre-fix bool was int subclass and `True`
                                # / `False` propagated as +1/0; `NaN`
                                # silently made _sign return -1 (NaN > 0
                                # is False).
                                ni_populated = [
                                    r["net_income"] for r in ni_rows
                                    if _is_finite_number(r.get("net_income"))
                                ]
                                # Anchor requires exactly 4 populated NI rows
                                # AND all of them sharing a single non-zero sign.
                                ni_anchor_usable = (
                                    len(ni_populated) == 4
                                    and _same_nonzero_sign(ni_populated)
                                )
                                anchor_sign = _sign(ni_populated[0]) if ni_anchor_usable else 0

                                sign_api = _sign(ttm_api)
                                sign_calc = _sign(ttm_calc)

                                if ni_anchor_usable:
                                    if sign_api == anchor_sign and sign_calc != anchor_sign:
                                        chosen = ttm_api
                                        result["fcf_source"] = FCF_SOURCE_API_FCF
                                        result["fcf_selection_reason"] = FCF_SELECTION_REASON_NI_SIGN_ANCHOR
                                    elif sign_calc == anchor_sign and sign_api != anchor_sign:
                                        chosen = ttm_calc
                                        result["fcf_source"] = FCF_SOURCE_OCF_MINUS_CAPEX
                                        result["fcf_selection_reason"] = FCF_SELECTION_REASON_NI_SIGN_ANCHOR
                                    elif sign_api == anchor_sign and sign_calc == anchor_sign:
                                        chosen, result["fcf_source"] = _pick_min_abs_prefer_api(
                                            ttm_api, ttm_calc
                                        )
                                        result["fcf_selection_reason"] = FCF_SELECTION_REASON_FALLBACK_MIN_ABS
                                    elif sign_api == 0 or sign_calc == 0:
                                        # One path is exactly 0 (neutral, not
                                        # opposite). Break-even TTM shouldn't
                                        # force a null result — fall to
                                        # min-abs with an explanatory warning.
                                        chosen, result["fcf_source"] = _pick_min_abs_prefer_api(
                                            ttm_api, ttm_calc
                                        )
                                        result["fcf_selection_reason"] = FCF_SELECTION_REASON_FALLBACK_MIN_ABS
                                        result["warnings"].append(
                                            "One TTM candidate is exactly 0 "
                                            "(neutral sign); treated as "
                                            "fallback_min_abs rather than "
                                            "both_opposite_sign_null."
                                        )
                                    else:
                                        # Both disagree with anchor → no valid pick
                                        chosen = None
                                        result["fcf_source"] = None
                                        result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_OPPOSITE_SIGN_NULL
                                        result["errors"].append(
                                            f"Both TTM FCF candidates "
                                            f"(api={ttm_api / 1e9:+.2f}B, "
                                            f"calc={ttm_calc / 1e9:+.2f}B) "
                                            f"disagree in sign with consistent TTM "
                                            f"net-income sign ({anchor_sign:+d}); "
                                            f"data quality too poor to pick a valid "
                                            f"FCF. reverse_dcf will be skipped."
                                        )
                                else:
                                    # NI anchor not usable → min-abs fallback
                                    chosen, result["fcf_source"] = _pick_min_abs_prefer_api(
                                        ttm_api, ttm_calc
                                    )
                                    result["fcf_selection_reason"] = FCF_SELECTION_REASON_FALLBACK_MIN_ABS
                                    result["warnings"].append(
                                        "NI sign anchor unavailable (missing rows, "
                                        "zero, or mixed signs across 4 quarters); "
                                        "selected smaller-magnitude TTM for conservatism."
                                    )

                                # Divergence warning — emitted for every 3e
                                # branch EXCEPT `both_opposite_sign_null`
                                # (chosen=None). The error message for that
                                # terminal state already carries both TTM
                                # values inline ("api=X.XXB, calc=Y.YYB"),
                                # so a parallel warnings entry would be
                                # pure duplication.
                                if chosen is not None:
                                    result["warnings"].append(
                                        f"TTM FCF divergence {divergence:.0%} between "
                                        f"api_fcf ({ttm_api / 1e9:+.2f}B) and "
                                        f"ocf-|capex| ({ttm_calc / 1e9:+.2f}B); chose "
                                        f"{result['fcf_source']} per "
                                        f"{result['fcf_selection_reason']}."
                                    )

                        # Prepare quarterly_fcfs for cyclicality checks (if we chose a path)
                        if chosen is not None:
                            if result["fcf_source"] == FCF_SOURCE_API_FCF:
                                quarterly_fcfs_for_cycle = [
                                    cf["free_cash_flow"] for cf in sorted_cfs
                                ]
                            else:
                                quarterly_fcfs_for_cycle = [
                                    cf["net_cash_flow_from_operations"] - abs(cf["capital_expenditure"])
                                    for cf in sorted_cfs
                                ]

        # === Stage 4: Emit ===
        ttm_fcf = chosen if chosen is not None else 0

        if chosen is None:
            result["fcf_per_share"] = None
            result["ttm_fcf"] = None
        elif not balance_sheets:
            # State machine picked a valid TTM but shares missing — override
            # fcf_selection_reason so downstream null-fcf guards (valuation
            # prompt, etc.) see a truthful reason rather than a stale
            # Stage 3 terminal state like "ni_sign_anchor".
            result["errors"].append("No balance sheet data for shares")
            result["fcf_per_share"] = None
            result["ttm_fcf"] = None
            result["fcf_source"] = None
            result["fcf_selection_reason"] = FCF_SELECTION_REASON_SHARES_UNAVAILABLE
        else:
            # DL4 §3.2 — shares read from the aligned window's latest
            # balance row (window[3].balance_row). Falls back to the legacy
            # sort if `latest_balance` is empty (e.g. when chosen path
            # bypassed the aligned-window route — defensive, should not
            # occur because `chosen is None` is handled above).
            if latest_balance:
                shares_raw = latest_balance.get("outstanding_shares", 0) or 0  # fail-open-ok: guarded by `_finite_shares` below — fail-closed via FCF_SELECTION_REASON_SHARES_UNAVAILABLE
            else:
                sorted_bs = sorted(balance_sheets, key=lambda x: x.get("report_period") or "")
                shares_raw = sorted_bs[-1].get("outstanding_shares", 0) or 0  # fail-open-ok: guarded by `_finite_shares` below — fail-closed via FCF_SELECTION_REASON_SHARES_UNAVAILABLE
            # Post-impl ISS-024 (fresh-loop2): explicit bool / NaN / Inf
            # / numeric-string guard on outstanding_shares. Pre-fix a
            # bool shares value coerced to 1 (since bool is int subclass),
            # `shares <= 0` passed (since 1 > 0), and ttm_fcf / 1 yielded
            # a wrong FCF/share.
            if (isinstance(shares_raw, bool)
                    or not isinstance(shares_raw, (int, float))
                    or not math.isfinite(float(shares_raw))):
                shares = 0
            else:
                shares = shares_raw
            if shares <= 0:
                result["errors"].append("Invalid outstanding_shares")
                result["fcf_per_share"] = None
                result["ttm_fcf"] = None
                result["fcf_source"] = None
                result["fcf_selection_reason"] = FCF_SELECTION_REASON_SHARES_UNAVAILABLE
            else:
                result["fcf_per_share"] = round(ttm_fcf / shares, 2)
                result["ttm_fcf"] = round(ttm_fcf, 0)
                result["shares"] = shares
                result["fcf_per_share_tag"] = (
                    "[Calc: ttm_fcf / shares; TTM path=" + result["fcf_source"] + "]"
                )
                result["ttm_fcf_tag"] = (
                    "[Calc: sum of 4 quarters per " + result["fcf_source"] + "]"
                )
                result["shares_tag"] = (
                    "[API: 02_financial_data.balance_sheets[-1].outstanding_shares]"
                )
                result["quarters_used"] = [
                    cf.get("report_period", "?") for cf in sorted_cfs_final
                ]

                # Cyclicality warnings — operate on the CHOSEN path's quarterly values
                neg_count = sum(1 for q in quarterly_fcfs_for_cycle if q <= 0)
                if neg_count > 0:
                    result["warnings"].append(
                        f"{neg_count}/4 quarters had zero or negative FCF — "
                        f"TTM FCF may not represent normalized earning power"
                    )

                if len(quarterly_fcfs_for_cycle) == 4:
                    mean_q = sum(quarterly_fcfs_for_cycle) / 4
                    if mean_q != 0:
                        variance = sum(
                            (q - mean_q) ** 2 for q in quarterly_fcfs_for_cycle
                        ) / 3
                        std_q = variance ** 0.5
                        cv = abs(std_q / mean_q)
                        result["fcf_coefficient_of_variation"] = round(cv, 2)
                        result["fcf_coefficient_of_variation_tag"] = (
                            "[Calc: std(4Q FCF) / mean(4Q FCF)]"
                        )
                        if cv > 1.0:
                            result["warnings"].append(
                                f"FCF coefficient of variation {cv:.2f} "
                                f"(>1.0) — extreme quarterly volatility, "
                                f"reverse DCF implied growth is unreliable"
                            )

        # Anti-hallucination source tags on the 3 new fields (spec §3 Option b)
        if result.get("fcf_source") is not None or result.get("fcf_selection_reason") is not None:
            result["fcf_source_tag"] = (
                "[Calc: path selection per fcf_selection_reason]"
            )
            result["fcf_selection_reason_tag"] = (
                "[Calc: state machine over (api_valid, calc_valid, divergence, ni_anchor)]"
            )
            if result.get("fcf_divergence_pct") is not None:
                result["fcf_divergence_pct_tag"] = (
                    "[Calc: |TTM_api − TTM_calc| / max * 100]"
                )
    except (OSError, json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
        # JSON is parseable but has unexpected shape (e.g. cash_flows
        # contains non-dict elements, financial_data is a string, fields
        # are wrong types). Catch these too so malformed inputs produce
        # a structured error rather than a process crash.
        # Known tradeoff: this catch is wide enough that a TypeError
        # originating in our OWN computation (not the input) would be
        # silently re-labeled as "read failed". Log the exception TYPE so
        # operators can at least distinguish "malformed JSON" (JSONDecode)
        # from "arithmetic bug" (ZeroDivisionError / TypeError deep in
        # state machine) via the structured error payload.
        result["errors"].append(
            f"Failed to read financial data: {type(exc).__name__}: {exc}"
        )
        result["fcf_per_share"] = None
        result["ttm_fcf"] = None
        result["fcf_source"] = None
        result["fcf_selection_reason"] = FCF_SELECTION_REASON_BOTH_INVALID_NULL
        result["fcf_divergence_pct"] = None

    # --- WACC from macro rates ---
    # Load via typed contract; any failure (OSError / JSON / Schema /
    # internal TypeError) → 10% default with exception type logged.
    try:
        from scripts.schemas.macro_rates import load_macro_rates

        macro_doc = load_macro_rates(macro_path)

        # Top-level flat fields (risk_free_rate, equity_risk_premium) are
        # NOT part of the current producer contract — they existed as a
        # legacy short-circuit. Drop them. Always derive risk-free from
        # current_rates[bank=FED].rate / 100 (percent → decimal).
        fed = macro_doc.find_current_rate("FED")
        if fed is not None:
            risk_free = fed.rate / 100.0
            risk_free_source_tag = (
                "[API: 09_macro_rates.current_rates[FED].rate / 100]"
            )
        else:
            risk_free = None
            risk_free_source_tag = None

        # ERP is not part of the 09_macro_rates contract; always default.
        erp = 0.055
        erp_source_tag = "[Default: 0.055 long-term US equity risk premium]"

        if isinstance(risk_free, (int, float)) and not isinstance(risk_free, bool) \
                and math.isfinite(risk_free):
            # Resolve beta: CLI override > data file > default 1.0
            # Post-impl ISS-004 (fresh-loop1): validate CLI --beta override
            # for NaN/Inf/bool too (data-file path already filters at lines
            # 134-145). Pre-fix `--beta nan` propagated into CAPM unchecked.
            if beta_override is not None:
                if (isinstance(beta_override, bool)
                        or not isinstance(beta_override, (int, float))
                        or not math.isfinite(float(beta_override))):
                    result["warnings"].append(
                        f"beta_override rejected: non-finite or non-numeric "
                        f"value {beta_override!r}; falling back to data / "
                        f"default."
                    )
                    beta_override = None
            if beta_override is not None:
                beta = float(beta_override)
                beta_source = "cli_override"
            elif beta_from_data is not None:
                beta = beta_from_data
                beta_source = result.get("beta_source", "price_data")
            else:
                beta = 1.0
                beta_source = "default"
                result["warnings"].append(
                    "No beta data available — using market-average 1.0. "
                    "WACC may be understated for volatile stocks."
                )

            # Simple CAPM: WACC ≈ risk_free + beta * ERP
            wacc = risk_free + beta * erp
            # Clamp to reasonable range [6%, 15%]
            # Post-impl ISS-004 (fresh-loop1): if wacc is NaN/Inf (e.g.
            # risk_free=Inf upstream slipped past), `max(0.06, min(0.15, Inf))`
            # returns 0.15 silently. Guard math.isfinite explicitly so the
            # bad input surfaces as a missing discount_rate rather than a
            # spurious clamped value.
            if not math.isfinite(wacc):
                result["warnings"].append(
                    f"WACC computed as non-finite ({wacc!r}); "
                    f"discount_rate omitted. Components: "
                    f"risk_free={risk_free!r} beta={beta!r} erp={erp!r}"
                )
                # Skip the rest of the WACC emission block.
                wacc = None
            else:
                wacc = max(0.06, min(0.15, wacc))
            if wacc is not None:
                result["discount_rate"] = round(wacc, 4)
                result["discount_rate_tag"] = (
                    "[Calc: risk_free + beta * ERP, clamped [0.06, 0.15]]"
                )
                beta_tag_map = {
                    "cli_override": "[CLI: --beta override]",
                    "default": "[Default: 1.0 market average]",
                }
                beta_source_tag = beta_tag_map.get(
                    beta_source, "[API: 01_price_data.beta.value]"
                )
                result["discount_rate_components"] = {
                    "risk_free_rate": round(risk_free, 4),
                    "risk_free_rate_tag": risk_free_source_tag,
                    "equity_risk_premium": erp,
                    "equity_risk_premium_tag": erp_source_tag,
                    "beta": round(beta, 3),
                    "beta_source": beta_source,
                    "beta_tag": beta_source_tag,
                    "formula": "risk_free + beta * ERP",
                }
        else:
            # Post-impl ISS-025 (fresh-loop2): emit explicit warning when
            # WACC falls back to the 10% default. Pre-fix the only signal
            # was `discount_rate_tag` (which prompt-layer didn't surface)
            # and a `note` inside `discount_rate_components`; downstream
            # callers reading `status: ok` had no way to know the macro
            # rate path silently degraded to a placeholder.
            result["discount_rate"] = 0.10
            result["discount_rate_tag"] = (
                "[Default: 0.10 when FED rate cannot be extracted]"
            )
            result["discount_rate_components"] = {
                "note": "No FED entry in current_rates, using 10% default"
            }
            result["warnings"].append(
                "discount_rate fell back to default 0.10 — risk_free_rate "
                "unavailable from macro_rates.json (no FED fed_funds entry). "
                "Reverse DCF / DCF cross-validation should treat WACC=0.10 "
                "as a placeholder, not a real cost of capital."
            )
    except (OSError, json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
        # Broad catch matches pre-migration behaviour. SchemaError is a
        # ValueError subclass so it lands here. Log the exception TYPE so
        # a silent internal error stays distinguishable from "file
        # missing" / "malformed JSON" in stderr.
        # Post-impl ISS-025 (fresh-loop2): also surface the fallback in
        # `result["warnings"]` so the persisted JSON makes the macro-read
        # failure observable downstream (stderr alone is invisible to
        # JSON consumers).
        print(f"[WARN] extract_fcf WACC fallback to 10%: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        result["discount_rate"] = 0.10
        result["discount_rate_tag"] = (
            "[Default: 0.10 when macro rates unreadable/invalid]"
        )
        result["discount_rate_components"] = {
            "note": f"macro rates unreadable: {type(exc).__name__}"
        }
        result["warnings"].append(
            f"discount_rate fell back to default 0.10 — macro_rates.json "
            f"unreadable / invalid ({type(exc).__name__}: {exc}). Treat WACC=0.10 "
            f"as a placeholder, not a real cost of capital."
        )

    if result["errors"]:
        # Use explicit None check — 0.0 is a legitimate TTM-per-share value
        # (break-even quarter) and must not be mistaken for "no result".
        result["status"] = (
            "partial" if result.get("fcf_per_share") is not None else "error"
        )
    elif result["warnings"]:
        result["status"] = "ok_with_warnings"

    # fresh-loop2-cycle2 sub-loop R-MED3 (closes ISS-020 properly):
    # validate fcf_selection_reason at the emit boundary so a typo in
    # one of the ~15 write sites surfaces as a fail-close ValueError
    # rather than a silently mis-classified artifact. The validator
    # accepts None (no reason set yet) and the closed FCF_SELECTION_REASONS
    # frozenset.
    validate_fcf_selection_reason(result.get("fcf_selection_reason"))
    # DL3c §4.2 — emit root marker on every return path (happy + error).
    # Pattern AE walker checks producer function returns; wrap-at-return
    # is the canonical fix. write_output does NOT auto-inject the marker.
    return emit_dl3c_root_marker(result)


def _main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract TTM FCF/share and WACC inputs for reverse DCF."
    )
    parser.add_argument(
        "--financial-json", required=True,
        help="Path to 02_financial_data.json",
    )
    parser.add_argument(
        "--price-json", required=True,
        help="Path to 01_price_data.json",
    )
    parser.add_argument(
        "--macro-json", required=True,
        help="Path to 09_macro_rates.json",
    )
    parser.add_argument(
        "--beta", type=float, default=None,
        help="Override equity beta for CAPM WACC (default: read from price data, "
             "fallback 1.0)",
    )
    parser.add_argument(
        "--ticker", required=True,
        help="Ticker symbol (DL4 §3.2.0.A — threaded through to "
             "aligned_quarters for structured error messages)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output file path (default: stdout)",
    )
    args = parser.parse_args()

    result = extract_fcf_inputs(
        financial_path=Path(args.financial_json),
        price_path=Path(args.price_json),
        macro_path=Path(args.macro_json),
        beta_override=args.beta,
        ticker=args.ticker,
    )

    from scripts.cli_utils import write_output
    # DL3c §4.2 — wrap at CLI write site too (idempotent; defense in
    # depth in case future refactor splits extract_fcf_inputs returns).
    write_output(emit_dl3c_root_marker(result), args.output)
    if args.output:
        fcf = result.get("fcf_per_share", "N/A")
        dr = result.get("discount_rate", "N/A")
        print(
            f"extract_fcf: fcf_per_share={fcf}, discount_rate={dr} → {args.output}",
            file=sys.stderr,
        )
    # fresh-loop2 cycle 5 M1: CLI exit code must reflect application-level
    # status. Pre-fix CLI exited 0 even when result["status"] == "error"
    # (currency mismatch, etc.), so pipeline orchestrators silently
    # consumed error JSON without knowing the script had failed.
    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    _main()
