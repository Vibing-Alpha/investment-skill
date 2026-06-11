"""Calculate historical valuation multiples from financial statements + price data.

Uses existing score-business outputs (no new API calls):
  - 02_financial_data.json: 8 quarters of income/cash_flow/balance statements
  - 01_price_data.json: weekly price history (~2 years)

DL4 (2026-05-17 spec): consumes `iter_aligned_quarter_windows` from
`scripts.schemas.quarter_window` so the TTM 4-quarter slide is computed
from the intersection of all 3 statement families (income + cash_flow +
balance). For each valid window we pick a forward-anchored price via
`forward_anchor_price` (DL4 invariant 5 — never use a bar dated before
target_date), preventing look-ahead bias from filing-pre-market closing
prints.

Output: JSON with per-multiple min/median/max/current, quarterly detail
(each carrying a `price_anchor` sub-dict), and a top-level
`skipped_windows` list mirroring the iter helper's SkippedWindow records.
"""

import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from scripts.cli_utils import emit_dl3c_root_marker
from scripts.schemas.quarter_window import (
    AlignedQuarterWindow,
    InsufficientQuartersError,
    SkippedWindow,
    iter_aligned_quarter_windows,
)
from scripts.sources.fx_rates import SUPPORTED_FX_CURRENCIES
# `scripts.fx_apply` is imported lazily at the call site (inside
# compute_historical_multiples) to break the import cycle: fx_apply imports
# `_is_finite_number` from `scripts.extract_fcf`, and historical_multiples'
# top-level import is reached during extract_fcf module init in some test
# orderings. The lazy import avoids the partially-initialized module error
# (mirrors extract_fcf.py lines 52-58 convention).


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD date string."""
    return datetime.strptime(s, "%Y-%m-%d")


@dataclass(frozen=True)
class PriceAnchor:
    price: float
    anchor_date: str   # YYYY-MM-DD of the chosen bar
    lag_days: int      # bar.time - target_date (always >= 0 in v1)


def forward_anchor_price(
    target_date: str,
    weekly_bars: list[Mapping],
    *,
    max_lag_days: int = 14,
) -> Optional[PriceAnchor]:
    """Anchor a price lookup to the FIRST weekly bar at or after target_date.

    DL4 invariant 5: NO backward-skewed picks. A weekly bar dated 2026-03-15
    against a target_date of 2026-03-22 is NEVER used. Returned anchor
    satisfies anchor_date >= target_date AND lag_days <= max_lag_days.
    """
    if not weekly_bars:
        return None
    # fresh-loop2 cycle 3 C3B-HIGH-1: guard the target-date parse. Pre-fix
    # callers that fell through to the `report_period_unparseable` branch
    # (target_date_str = period_key when period_key isn't YYYY-MM-DD, e.g.
    # an upstream row with fiscal_period-style "2025-Q4" as report_period)
    # passed a non-ISO string here. `_parse_date(...)` then raised
    # ValueError out of forward_anchor_price → out of
    # compute_historical_multiples → unhandled traceback at the CLI
    # boundary (CLI only catches InsufficientQuartersError).
    try:
        target = _parse_date(target_date)
    except (ValueError, TypeError):
        return None
    best_bar = None
    best_lag = None
    for bar in weekly_bars:
        if not isinstance(bar, Mapping):
            continue
        # Post-impl ISS-012 (fresh-loop1): guard against bars missing "time"
        # or carrying a non-string / unparseable time. Pre-fix `bar["time"]`
        # raised KeyError (unhandled) when a yfinance cache row lost its
        # date, and `_parse_date(non_iso)` raised ValueError on malformed
        # strings. Skip such rows rather than failing the whole anchor.
        bar_time = bar.get("time")
        if not isinstance(bar_time, str) or not bar_time:
            continue
        try:
            bar_date = _parse_date(bar_time)
        except (ValueError, TypeError):
            continue
        lag = (bar_date - target).days
        if lag < 0:
            continue  # forward search — skip pre-target bars
        if best_lag is None or lag < best_lag:
            best_lag = lag
            best_bar = bar
    if best_bar is None or best_lag > max_lag_days:
        return None
    price_val = best_bar.get("adjclose")
    if price_val is None:
        price_val = best_bar.get("close")
    if price_val is None:
        return None
    # Post-impl ISS-059 (zero-context round 8 MEDIUM): coerce price_val to
    # float so the `PriceAnchor.price: float` dataclass annotation is honest.
    # Pre-fix this returned the raw `bar.get("adjclose")` value which could
    # be a numeric string from yfinance cache corruption / mixed JSON
    # encoding, causing downstream `pa.price <= 0` to raise TypeError. The
    # legacy `_find_closest_price` had the same gap; harden at this single
    # entry point rather than every call site. Reject bool (since
    # `isinstance(True, (int, float))` is True in Python).
    if isinstance(price_val, bool):
        return None
    if not isinstance(price_val, (int, float)):
        try:
            price_val = float(price_val)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(price_val):
        return None
    return PriceAnchor(price=float(price_val), anchor_date=best_bar["time"], lag_days=best_lag)


def _median(values: List[float]) -> float:
    """Calculate median of a list of floats."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def compute_historical_multiples(
    financial_data: Dict,
    price_data: Dict,
    *,
    ticker: str,
) -> Dict:
    """Compute historical valuation multiples.

    Args:
        financial_data: Contents of 02_financial_data.json
        price_data: Contents of 01_price_data.json
        ticker: Ticker symbol — threaded through to
            `iter_aligned_quarter_windows` for structured error messages
            (DL4 §3.2.0.A).

    Returns: Dict with per-multiple stats, quarterly detail (each carrying
    a `price_anchor` sub-dict), and a top-level `skipped_windows` list.

    Raises:
        InsufficientQuartersError: surfaces from
            `iter_aligned_quarter_windows` on `duplicate_report_period` or
            `statement_metadata_mismatch` (invariants 11 + 9). The CLI
            wrapper in `_main` translates the raise into a structured
            error JSON; in-process callers should catch.
    """
    # DL3c (cycle-20 F-20-2) accumulator pattern: historical_multiples has NO
    # `result` dict accumulator (unlike extract_fcf or adr/correct) — it
    # returns dict literals at every exit point (lines 167, 176, 199, 278,
    # 314, 672, 789 pre-DL3c). Declare 2 locals at the TOP of the function;
    # the FX path populates them on success; EVERY return literal spreads
    # them via `**` so warnings propagate even on error paths and the cert
    # block lands on the success path. (Pre-fix `result.setdefault(...)`
    # / `result.update(...)` patterns from extract_fcf/adr would NameError
    # here.)
    fx_warnings_to_propagate: list[str] = []
    cert_block_to_add: dict = {}

    # fresh-loop2 cycle 4 C4B-MED-1: root-shape validation. Pre-fix
    # `financial_data.get(...)` would raise AttributeError if the JSON
    # file's top level was a list / string / scalar (valid JSON but
    # wrong shape). The CLI catches InsufficientQuartersError only —
    # AttributeError fell through to an uncaught traceback rather than
    # the envelope-consistent `status: "error"` shape.
    if not isinstance(financial_data, Mapping):
        return emit_dl3c_root_marker({
            "status": "error",
            "error": (
                f"financial_data must be a JSON object (mapping), "
                f"got {type(financial_data).__name__}"
            ),
            "skipped_windows": [],
            **cert_block_to_add,
            **({"warnings": fx_warnings_to_propagate}
               if fx_warnings_to_propagate else {}),
        })
    if not isinstance(price_data, Mapping):
        return emit_dl3c_root_marker({
            "status": "error",
            "error": (
                f"price_data must be a JSON object (mapping), "
                f"got {type(price_data).__name__}"
            ),
            "skipped_windows": [],
            **cert_block_to_add,
            **({"warnings": fx_warnings_to_propagate}
               if fx_warnings_to_propagate else {}),
        })
    income = financial_data.get("income_statements", [])
    cash_flow = financial_data.get("cash_flows", [])
    balance = financial_data.get("balance_sheets", [])
    # fresh-loop2 cycle 5 M2: shape-validate each statement family list
    # before indexing. The cycle-4 Mapping guard ensures top-level is a
    # dict, but `financial_data["income_statements"]` could itself be a
    # dict / scalar / None (malformed but valid-JSON). `income[0]` would
    # then crash with KeyError/TypeError outside the structured error
    # envelope.
    for fam_name, fam_val in (
        ("income_statements", income),
        ("cash_flows", cash_flow),
        ("balance_sheets", balance),
    ):
        if not isinstance(fam_val, list):
            return emit_dl3c_root_marker({
                "status": "error",
                "error": (
                    f"{fam_name} must be a JSON list, got "
                    f"{type(fam_val).__name__}"
                ),
                "skipped_windows": [],
                **cert_block_to_add,
                **({"warnings": fx_warnings_to_propagate}
                   if fx_warnings_to_propagate else {}),
            })

    # Mixed-currency guard (FDS foreign-issuer bug): rows may be labeled "USD"
    # while a subset of fields is still native currency (MRAAY/Murata). The
    # gross-profit accounting identity catches it where the row-label / DL3c
    # gate cannot — multiples computed on a mixed statement are garbage.
    from scripts.schemas.currency_consistency import detect_mixed_currency
    _cc = detect_mixed_currency(income)
    if _cc["status"] == "mixed":
        return emit_dl3c_root_marker({
            "status": "error",
            "error": (
                f"financials_currency_mixed: gross-profit identity violated "
                f"(implied FX ~{_cc['implied_fx']} on {len(_cc['mixed_rows'])} "
                f"rows) — FDS returned a USD/native field mix under a single "
                f"'USD' label; multiples would be garbage."
            ),
            "fx_failure_reason": "financials_currency_mixed",
            "skipped_windows": [],
            **cert_block_to_add,
            **({"warnings": fx_warnings_to_propagate}
               if fx_warnings_to_propagate else {}),
        })

    historical = price_data.get("historical")
    weekly = (
        historical.get("weekly", []) if isinstance(historical, Mapping) else []
    )

    # Currency check — mirrors HIGH-26 fix in extract_fcf.py. When statements
    # are in a non-USD currency (ADRs like MRAAY/TTDKY in JPY), computing
    # price_USD / (net_income_LOCAL / shares_LOCAL) produces nonsense
    # multiples. Observed pre-fix: MRAAY PE=0.08 vs FD-API PE=44.85 (560x off).
    # Fail-closed until upstream delivers USD-normalized statements.
    #
    # DL4 ordering invariant: this gate MUST remain BEFORE
    # iter_aligned_quarter_windows so a JPY ticker returns the
    # structured currency error instead of an InsufficientQuartersError
    # raised from inside the iter helper. (Currency regression sentinel —
    # see test_historical_multiples_fail_close.)
    # First-row currency check (existing strict semantic — None ⇒ fail-close).
    # Then scan ALL rows across the 3 statement families for explicit non-USD
    # (post-impl ISS-005): a single non-USD row in any non-first position would
    # otherwise be shadowed by an iter-raised InsufficientQuartersError. Rows
    # with currency missing are treated by the first-row check only — the
    # broaden ignores missing values (producer convention treats absence as
    # USD-by-default).
    stmt_currency = None
    if income and isinstance(income[0], dict):
        stmt_currency = income[0].get("currency")

    def _any_explicit_non_usd(rows):
        # fresh-loop2 cycle 4 C4B-MED-2: normalize before comparison
        # (parity with extract_fcf + Pattern W AST `_is_usd_constant`).
        for row in rows:
            if isinstance(row, dict):
                cur = row.get("currency")
                if cur is None:
                    continue
                if isinstance(cur, str) and cur.strip().upper() == "USD":
                    continue
                return cur
        return None

    # post-impl loop-3 F2 fix: explicit `is None` short-circuit. The
    # helper returns the row's `currency` value when it's not USD; that
    # value can be falsy (empty string `""`). Pre-fix `or` chain treated
    # `""` as falsy and shadowed it with the next family's None → routed
    # to USD-default. Mirror of the same fix in extract_fcf.
    explicit_bad = _any_explicit_non_usd(income)
    if explicit_bad is None:
        explicit_bad = _any_explicit_non_usd(cash_flow)
    if explicit_bad is None:
        explicit_bad = _any_explicit_non_usd(balance)

    # fresh-loop2 ISS-015: order the currency gate so that empty statements
    # surface as `insufficient_quarters_for_aligned_window` (the real
    # condition), not as the more confusing `currency=None requires USD`
    # error. Pre-fix an entirely empty `income` list triggered the
    # `stmt_currency=None` branch first, masking the actual problem.
    statements_empty = not income and not cash_flow and not balance
    # fresh-loop2 cycle 4 C4B-MED-2: also normalize stmt_currency before
    # comparing (parity with `_any_explicit_non_usd` above).
    def _is_usd_norm(v):
        return isinstance(v, str) and v.strip().upper() == "USD"
    if (not _is_usd_norm(stmt_currency) or explicit_bad is not None) and not statements_empty:
        # Post-impl ISS-047 (zero-context round 3 LOW): pre-fix `bad =
        # stmt_currency if stmt_currency != "USD" else explicit_bad` chose
        # stmt_currency=None when first row had no currency tag AND a
        # non-first row had explicit non-USD (e.g. JPY). The error message
        # then said "currency=None" instead of "currency='JPY'", hiding
        # the actual problem. Prefer the explicit non-USD value when
        # available.
        bad = (
            stmt_currency
            if (stmt_currency is not None and not _is_usd_norm(stmt_currency))
            else explicit_bad
        )
        # DL3c §3.3 — three-state currency gate. Pre-DL3c this branch
        # unconditionally fail-closed on any non-USD; v1 adds the
        # supported-currency conversion path.
        detected_currency = (
            str(bad).strip().upper() if isinstance(bad, str) else None
        )
        # cycle-12 F-12-3: parseable-ISO-4217 gate per §3.6.1 D3 routing.
        # `None` / `""` / `"Y"` / `"Yen"` route to `_unrecognized`,
        # NOT `_unsupported`.
        if detected_currency is None or not re.match(
            r"^[A-Z]{3}$", detected_currency
        ):
            return emit_dl3c_root_marker({
                "status": "error",
                "error": (
                    f"fx_currency_unrecognized: statement currency={bad!r} "
                    f"is not a parseable 3-letter ISO 4217 code"
                ),
                "fx_failure_reason": "fx_currency_unrecognized",
                "skipped_windows": [],
                **cert_block_to_add,
                **({"warnings": fx_warnings_to_propagate}
                   if fx_warnings_to_propagate else {}),
            })

        if detected_currency not in SUPPORTED_FX_CURRENCIES:
            return emit_dl3c_root_marker({
                "status": "error",
                "error": (
                    f"fx_currency_unsupported: statement currency={bad!r} "
                    f"(parseable ISO 4217 but not in v1 supported "
                    f"set {sorted(SUPPORTED_FX_CURRENCIES)}). Add to "
                    f"SUPPORTED_FX_CURRENCIES + write fixture to extend."
                ),
                "fx_failure_reason": "fx_currency_unsupported",
                "skipped_windows": [],
                **cert_block_to_add,
                **({"warnings": fx_warnings_to_propagate}
                   if fx_warnings_to_propagate else {}),
            })

        # DL3c — supported non-USD: fetch FX, apply conversion via shared
        # helper, set basis=usd_converted. consumer_name=
        # "historical_multiples" activates the consumer-specific carve-out
        # set (none for historical_multiples; see §3.6.1).
        # Lazy import to break the cycle (see top-of-file note).
        from scripts.fx_apply import (
            apply_fx_conversion,
            build_cert_block,
        )
        ok, fx_window, reason, fx_warnings = apply_fx_conversion(
            income_statements=income,
            cash_flows=cash_flow,
            balance_sheets=balance,
            detected_currency=detected_currency,
            consumer_name="historical_multiples",
            consumer_fields={
                "income_statements": ("revenue", "net_income", "ebit"),
                "cash_flows": ("depreciation_and_amortization",),
                "balance_sheets": (
                    "total_debt",
                    "cash_and_equivalents",
                    "shareholders_equity",
                ),
            },
            ticker=ticker,
        )
        # cycle-8 F9 + cycle-9 #6: surface fx warnings via the consumer's
        # existing `warnings` field name (not the aux `fx_warnings` key).
        # Always extend warnings, even on failure path (spec L607-610:
        # error-path returns also spread `fx_warnings_to_propagate` so
        # YTD `fx_basis_unattested` warnings surface even when downstream
        # computation fails).
        if fx_warnings:
            fx_warnings_to_propagate.extend(fx_warnings)
        if not ok:
            return emit_dl3c_root_marker({
                "status": "error",
                "error": f"fx conversion failed: {reason}",
                "fx_failure_reason": reason,
                "skipped_windows": [],
                **cert_block_to_add,
                **({"warnings": fx_warnings_to_propagate}
                   if fx_warnings_to_propagate else {}),
            })
        # cycle-15 F-15-3 + cycle-16 + cycle-21 (F-20-2 accumulator):
        # build cert via shared helper, store in local accumulator (NOT
        # `result.update(...)` — no result dict in this function). The
        # success return literal at line 789 spreads `**cert_block_to_add`.
        cert_block_to_add = build_cert_block(detected_currency, fx_window)
        # statement rows are now USD-tagged; downstream loop reads USD.

    # DL4 §3.2 — replace independent sorts + bs_by_period lookup with the
    # iter helper. Yields (window, skip) pairs oldest-first over the
    # intersection of income + cash_flow + balance report_periods.
    #
    # NO inner try/except for InsufficientQuartersError (per spec
    # cycle-6 ISS-022): propagate to the CLI `_main` wrapper. In-process
    # consumers that need to soft-fail should wrap the call themselves.
    valid_windows: list[AlignedQuarterWindow] = []
    skipped_windows: list[SkippedWindow] = []
    for window, skip in iter_aligned_quarter_windows(
        income, cash_flow, balance, ticker=ticker,
    ):
        if window is not None:
            valid_windows.append(window)
        else:
            skipped_windows.append(skip)

    skipped_serialized = [
        {
            "anchor_report_period": s.anchor_report_period,
            "failure_kind": s.failure_kind,
            "detail": s.detail,
        }
        for s in skipped_windows
    ]

    if not valid_windows:
        # fresh-loop2 ISS-003: envelope consistency — always emit `status`.
        # DL3c (cycle-20 F-20-2): spread accumulator locals so YTD
        # fx_basis_unattested warnings + cert (if any) reach the output
        # even on this early-exit path.
        return emit_dl3c_root_marker({
            "status": "error",
            "error": "Need at least 4 quarters for TTM calculation",
            "quarters_available": len(income),
            "skipped_windows": skipped_serialized,
            **cert_block_to_add,
            **({"warnings": fx_warnings_to_propagate}
               if fx_warnings_to_propagate else {}),
        })

    # fresh-loop2-cycle2 C2B-MED-1: bound the trailing horizon to the
    # most recent ~2 years (8 quarters) so min/median/max range
    # statistics reflect a comparable trailing-2Y window per the module
    # docstring's "~2 years" contract. Pre-fix a producer with 10y of
    # statements would compute summary bands across the full history —
    # mixing 2014-2024 valuations into a single "current band"
    # produces useless comparison ranges. The slice runs BEFORE the
    # main loop so quarterly_data and summary stats both see the
    # same horizon.
    valid_windows = valid_windows[-8:]

    # Track the latest aligned anchor BEFORE windows can be dropped to
    # skipped_windows during per-window processing. fresh-loop2-cycle2
    # C2B-HIGH-3: summary.current is taken from quarterly_data[-1],
    # but if the NEWEST valid_window gets skipped (price unavailable,
    # type drift, etc.), quarterly_data[-1] becomes an OLDER quarter
    # and `current` silently reflects stale state under status="ok".
    # Capture the latest-aligned report_period here; the summary build
    # below compares against it and emits `current=None +
    # current_unavailable_reason` when the latest aligned quarter was
    # dropped.
    latest_aligned_report_period = valid_windows[-1][3].report_period

    # Whole-project review 2026-06-11 C7: the anchor above only catches
    # windows dropped DURING per-window processing. A newest quarter that
    # never became an aligned window at all (excluded upstream in the
    # 3-statement intersection — e.g. the cash_flow family lags a filing by
    # a day) leaves valid_windows[-1] at an older quarter with a clean
    # status. Capture the newest REPORTED quarterly income period so the
    # lag can be surfaced (warning + fields, NOT current=None: a one-family
    # filing lag is routine and the older band is still valid data — the
    # detector surfaces, the consuming agent adjudicates).
    newest_reported_period = None
    for _r in income if isinstance(income, list) else []:
        if not isinstance(_r, dict):
            continue
        if str(_r.get("period") or "").strip().lower() not in ("quarter", "quarterly"):
            continue
        _rp = _r.get("report_period")
        if isinstance(_rp, str) and (
            newest_reported_period is None or _rp > newest_reported_period
        ):
            newest_reported_period = _rp

    # Compute TTM multiples for each aligned 4-quarter window
    quarterly_data: list[dict] = []

    def _num(v):
        # None → 0 (legitimate absence). Numeric finite → numeric.
        # Bool rejected (Python bool is int subclass but not a financial value).
        # NaN / Inf rejected so they don't propagate into TTM sums (ISS-038
        # fresh-loop4).
        if v is None or isinstance(v, bool):
            return 0
        if not isinstance(v, (int, float)):
            return 0
        try:
            return v if math.isfinite(float(v)) else 0
        except (TypeError, ValueError):
            return 0

    def _is_type_drift(v) -> bool:
        # Post-impl ISS-005 (fresh-loop1): non-None non-numeric values
        # (most commonly numeric strings like "5000000000" from yfinance
        # cache corruption or mixed JSON encoding) are type drift from the
        # upstream contract. Pre-fix `_num()` silently coerced them to 0,
        # which made `enterprise_value = market_cap + 0 - 0`, `pe`, `ev/*`
        # all silently understate or omit without surfacing the data
        # quality problem. Skip the window with a recorded reason instead.
        # `None` is treated as legitimate absence (sparse field) and not
        # flagged as drift.
        # Post-impl ISS-038 (fresh-loop4): also flag NaN / Inf floats as
        # type drift. Pre-fix `float("nan")` passed isinstance(float) check
        # and propagated into TTM sums (`sum([1.0, 2.0, NaN, 3.0]) = NaN`),
        # silently poisoning every multiple downstream.
        if v is None:
            return False
        if isinstance(v, bool):
            return True
        if not isinstance(v, (int, float)):
            return True
        try:
            return not math.isfinite(float(v))
        except (TypeError, ValueError):
            return True

    for w in valid_windows:
        anchor_q = w[3]
        period_key = anchor_q.report_period

        # Separate two different concepts:
        #   period_key        — index into balance sheets / identity of the quarter
        #   target_date_str   — the date we look up price at (must NOT precede filing_date)
        # fresh-loop2-cycle2 C2B-HIGH-1: knowledge-date completeness requires
        # max(filing_date across income / cash_flow / balance). Pre-fix the
        # anchor used only income.filing_date — but the income statement
        # can be filed BEFORE balance/CF in a multi-document filing chain
        # (e.g. earnings release filed first, 10-Q with balance amended
        # later). Anchoring against the earliest of the three permits
        # look-ahead bias: the market did not yet know the balance/CF
        # numbers at income.filing_date.
        candidate_filings: list[str] = []
        for q in w:
            for row in (q.income_row, q.cash_flow_row, q.balance_row):
                fd = row.get("filing_date")
                if isinstance(fd, str) and fd:
                    try:
                        _parse_date(fd)
                        candidate_filings.append(fd)
                    except (ValueError, TypeError):
                        # malformed filing_date — ignore this one,
                        # other rows may still carry a valid date
                        pass
        filing_date = max(candidate_filings) if candidate_filings else None
        target_date_str = None
        target_source = None
        if filing_date:
            target_date_str = filing_date
            target_source = "max_filing_date_across_statements"
        if target_date_str is None:
            # Fallback: typical 45-day quarterly filing lag — still better than
            # anchoring on report_period (which gives the market clairvoyance).
            # fresh-loop2-cycle2 C2B-HIGH-2 partial mitigation: the true fix
            # requires the upstream fetch script to populate filing_date
            # on income/cash_flow/balance rows. Until then, the rp+45d
            # heuristic estimates the knowledge date with a documented
            # bias: typically OK for 10-Q (filed within 45d) but too
            # aggressive for 10-K (filed 60-90d after fiscal year end).
            # Distinct target_source tag flags that consumers should
            # discount confidence on these windows.
            try:
                rp_dt = _parse_date(period_key)
                target_date_str = (rp_dt + timedelta(days=45)).strftime("%Y-%m-%d")
                target_source = "report_period_plus_45d_estimated"
            except ValueError:
                target_date_str = period_key
                target_source = "report_period_unparseable"

        # Post-impl ISS-005 (fresh-loop1): pre-scan the window for type
        # drift on any TTM-aggregated numeric field. If found, record a
        # skipped_window entry and continue rather than silent-zero through
        # the aggregation. Fields cover income / cash_flow / balance.
        # Track type drift AND per-denominator missing-data, so each
        # multiple can be skipped independently when its critical TTM
        # component is absent (ISS-026 fresh-loop2: pre-fix None→0 made
        # `ttm_revenue` a partial sum when 1+ quarters had revenue=null,
        # producing silently-understated P/S and EV/Revenue under
        # `correction_status="applied"` semantic).
        # Post-impl ISS-035 (fresh-loop3): extend to D&A and ALL balance-
        # sheet TTM components (cycle 2 fix only tracked revenue / net_income
        # / ebit). Pre-fix `_num(None) → 0` silently corrupted ev_ebitda
        # divisor (D&A missing→ttm_ebitda=ttm_ebit only, but the per-ratio
        # gate `ttm_da > 0` still passed if even one quarter had D&A) and
        # enterprise_value (debt/cash missing → silent zero).
        drift_fields: list[str] = []
        missing_revenue = False
        missing_net_income = False
        missing_ebit = False
        missing_da = False
        for q in w:
            for fld in ("revenue", "net_income", "ebit"):
                v = q.income_row.get(fld)
                if _is_type_drift(v):
                    drift_fields.append(f"income.{fld}@{q.report_period}={v!r}")
                elif v is None:
                    if fld == "revenue":
                        missing_revenue = True
                    elif fld == "net_income":
                        missing_net_income = True
                    elif fld == "ebit":
                        missing_ebit = True
            v_da = q.cash_flow_row.get("depreciation_and_amortization")
            if _is_type_drift(v_da):
                drift_fields.append(
                    f"cash_flow.depreciation_and_amortization@{q.report_period}={v_da!r}"
                )
            elif v_da is None:
                missing_da = True
        bs_pre = w[3].balance_row
        missing_shares = False
        missing_equity = False
        missing_debt = False
        missing_cash = False
        for fld in ("outstanding_shares", "shareholders_equity",
                    "total_debt", "cash_and_equivalents"):
            v_bs = bs_pre.get(fld)
            if _is_type_drift(v_bs):
                drift_fields.append(f"balance.{fld}@{w[3].report_period}={v_bs!r}")
            elif v_bs is None:
                if fld == "outstanding_shares":
                    missing_shares = True
                elif fld == "shareholders_equity":
                    missing_equity = True
                elif fld == "total_debt":
                    missing_debt = True
                elif fld == "cash_and_equivalents":
                    missing_cash = True
        if drift_fields:
            skipped_serialized.append({
                "anchor_report_period": period_key,
                "failure_kind": "numeric_field_type_drift",
                "detail": (
                    "non-None non-numeric value(s) in TTM-aggregated fields: "
                    + "; ".join(drift_fields[:6])
                    + ("; ..." if len(drift_fields) > 6 else "")
                ),
            })
            continue

        # TTM aggregates read from the aligned window's income rows
        ttm_revenue = sum(_num(q.income_row.get("revenue")) for q in w)
        ttm_net_income = sum(_num(q.income_row.get("net_income")) for q in w)
        ttm_ebit = sum(_num(q.income_row.get("ebit")) for q in w)
        # Post-impl ISS-006 (fresh-loop1): compute true TTM D&A from
        # cash_flow rows so EV/EBITDA can be honest. Pre-fix the multiple
        # was named `ev_ebitda` but used `ttm_ebit` as the divisor (see the
        # old "approximate EBITDA as EBIT" comment that admitted the
        # mislabel). Downstream prompts expect EV/EBITDA semantics; silently
        # serving EV/EBIT corrupts cross-ticker comparisons (capital-
        # intensive vs asset-light companies have very different D&A%).
        ttm_da = sum(_num(q.cash_flow_row.get("depreciation_and_amortization"))
                     for q in w)

        # Balance sheet from the SAME aligned quarter (no longer an
        # independent lookup map — invariant 1).
        bs = anchor_q.balance_row
        shares = _num(bs.get("outstanding_shares"))  # fail-open-ok: guarded by `if shares <= 0: continue` below — skips quarter rather than divide-by-zero
        equity = _num(bs.get("shareholders_equity"))
        total_debt = _num(bs.get("total_debt"))
        cash = _num(bs.get("cash_and_equivalents"))

        # Forward-anchored price lookup (DL4 §3.3 — never a pre-target bar)
        pa = forward_anchor_price(target_date_str, weekly, max_lag_days=14)
        # Post-impl ISS-035 (fresh-loop3): if outstanding_shares was None
        # we have no way to compute any per-share or capitalization
        # metric — skip the window via missing_shares (the existing
        # `shares <= 0` guard already covers it since _num(None)=0, but
        # be explicit for clarity).
        # fresh-loop2 ISS-016: emit a skipped_windows entry instead of
        # silent continue. Pre-fix the operator had no way to distinguish
        # "TTM not emitted because aligned_quarters rejected this window"
        # from "TTM not emitted because price/shares were unavailable".
        if pa is None:
            skipped_serialized.append({
                "anchor_report_period": period_key,
                "failure_kind": "price_anchor_unavailable",
                "detail": (
                    f"forward_anchor_price returned None for "
                    f"target_date={target_date_str} (no weekly bar within "
                    f"14d forward window of report_period+filing-lag)."
                ),
            })
            continue
        if pa.price is None or pa.price <= 0 or shares <= 0 or missing_shares:
            skipped_serialized.append({
                "anchor_report_period": period_key,
                "failure_kind": "invalid_price_or_shares",
                "detail": (
                    f"price={pa.price!r} shares={shares!r} "
                    f"missing_shares={missing_shares}; window dropped."
                ),
            })
            continue

        price = pa.price
        market_cap = price * shares
        # Post-impl ISS-035 (fresh-loop3): only compute EV when debt + cash
        # are both available. Missing → ev=None, all ev-derived ratios
        # emitted as absent.
        if missing_debt or missing_cash:
            enterprise_value = None
        else:
            enterprise_value = market_cap + total_debt - cash

        # NOTE on bool handling: balance-sheet `outstanding_shares`,
        # `shareholders_equity`, `total_debt`, `cash_and_equivalents`
        # bool inputs are already caught by `_is_type_drift(v_bs)` at
        # the type-drift sweep above (L379) → skipped_windows
        # numeric_field_type_drift entry + `continue` before this
        # point. The fresh-loop2-cycle1 dead `balance_sheet_bool_value`
        # gate that lived here has been removed (cycle1 verification
        # regression R-MED-2: unreachable because `_is_type_drift(True)
        # == True` at L287-288 fires first). bool handling in
        # metrics_snapshot is a separate code path — see L555-568.

        entry = {
            "report_period": period_key,
            "filing_date": filing_date,
            # LEGACY: target_date semantics preserved (NOT anchor_date) per
            # §3.2.0.C. DL5 may collapse via RFC.
            "price_anchor_date": target_date_str,
            # NEW additive sub-dict (DL4 §3.2.0.C)
            "price_anchor": {
                "target_date": target_date_str,
                "anchor_date": pa.anchor_date,
                "lag_days": pa.lag_days,
                "target_source": target_source,
            },
            "fiscal_period": anchor_q.fiscal_period,  # fail-open-ok: metadata passthrough only, not used as a gate
            "price": round(price, 2),
            "market_cap": round(market_cap, 0),
            "multiples": {},
        }

        # Per-multiple denominator gating (ISS-026 fresh-loop2): emit
        # each ratio ONLY when all of its TTM income-statement components
        # were present across all 4 quarters. Pre-fix `_num(None)→0`
        # silently produced partial-sum TTM totals, making P/E, P/S,
        # EV/EBITDA, EV/Revenue look like real values when one or more
        # quarters had `revenue` / `net_income` / `ebit` / D&A absent.
        # P/E (TTM)
        if not missing_net_income and ttm_net_income > 0:
            ttm_eps = ttm_net_income / shares
            entry["multiples"]["pe"] = round(price / ttm_eps, 2)

        # P/S (TTM)
        if not missing_revenue and ttm_revenue > 0:
            revenue_per_share = ttm_revenue / shares
            entry["multiples"]["ps"] = round(price / revenue_per_share, 2)

        # P/B
        if not missing_equity and equity > 0:
            book_per_share = equity / shares
            entry["multiples"]["pb"] = round(price / book_per_share, 2)

        # EV/EBITDA (TTM) — true EBITDA = EBIT + D&A per ISS-006.
        # Gate on EV availability (debt + cash present) + EBIT and D&A
        # both fully tracked across all 4 quarters (no None / no drift).
        # fresh-loop2-cycle2 C2B-MED-2: drop the `ttm_da > 0` requirement.
        # Asset-light companies legitimately report D&A = 0 across the
        # window (no PPE / amortizable intangibles); EBITDA = EBIT + 0 =
        # EBIT is the honest figure. Suppressing the multiple here only
        # forced the operator to read `ev_ebit` separately for the same
        # information. The `not missing_da` flag still gates on "all 4
        # quarters reported D&A", distinguishing "true 0" from "missing".
        ttm_ebitda = ttm_ebit + ttm_da
        if (enterprise_value is not None
                and not missing_ebit and not missing_da
                and ttm_ebitda > 0):
            entry["multiples"]["ev_ebitda"] = round(enterprise_value / ttm_ebitda, 2)
        if (enterprise_value is not None
                and not missing_ebit and ttm_ebit > 0):
            entry["multiples"]["ev_ebit"] = round(enterprise_value / ttm_ebit, 2)

        # EV/Revenue (TTM)
        if (enterprise_value is not None
                and not missing_revenue and ttm_revenue > 0):
            entry["multiples"]["ev_revenue"] = round(enterprise_value / ttm_revenue, 2)

        # fresh-loop2 ISS-017: gate empty-multiples entry. Pre-fix a
        # window that had valid price+shares but ALL denominators
        # (net_income, revenue, equity, ebit, ebitda) were missing/zero
        # produced a quarterly_data entry with `multiples: {}` — counted
        # as a window in `quarters_used` but contributing nothing to any
        # summary stat. Bookkeeping noise; suppress at the gate and
        # surface in skipped_windows.
        if not entry["multiples"]:
            skipped_serialized.append({
                "anchor_report_period": period_key,
                "failure_kind": "no_computable_multiples",
                "detail": (
                    "all denominators missing or <=0; window contributes "
                    "no multiple. price + shares valid but net_income, "
                    "revenue, equity, ebit, ebitda all unusable."
                ),
            })
            continue

        quarterly_data.append(entry)

    if not quarterly_data:
        # fresh-loop2 ISS-003: envelope consistency — always emit `status`.
        # DL3c (cycle-20 F-20-2): spread accumulator locals.
        return emit_dl3c_root_marker({
            "status": "error",
            "error": "Could not compute any historical multiples",
            "quarters_available": len(income),
            "skipped_windows": skipped_serialized,
            **cert_block_to_add,
            **({"warnings": fx_warnings_to_propagate}
               if fx_warnings_to_propagate else {}),
        })

    # Aggregate stats per multiple
    multiple_names = ["pe", "ps", "pb", "ev_ebitda", "ev_ebit", "ev_revenue"]
    summary: dict = {}

    # Compute actual span from oldest to newest quarterly_data; no hardcoded "2Y".
    # fresh-loop2-cycle2 C2B-HIGH-3: detect when the newest valid_window was
    # dropped during per-window processing (price unavailable / type drift /
    # bool field / missing filing_date). When dropped, quarterly_data[-1]
    # reflects an OLDER quarter and `current` would silently use stale state
    # under status="ok". Compare against the latest aligned anchor captured
    # before the loop.
    latest_q = quarterly_data[-1] if quarterly_data else None
    latest_window_dropped = (
        latest_q is not None
        and latest_q.get("report_period") != latest_aligned_report_period
    )
    if quarterly_data:
        try:
            first_d = datetime.strptime(quarterly_data[0]["report_period"], "%Y-%m-%d")
            last_d = datetime.strptime(quarterly_data[-1]["report_period"], "%Y-%m-%d")
            span_days = (last_d - first_d).days
        except (KeyError, ValueError):
            span_days = None
    else:
        span_days = None

    for name in multiple_names:
        values = [q["multiples"][name] for q in quarterly_data
                  if name in q["multiples"]]
        if not values:
            continue
        # current is whatever the LATEST quarter has for this multiple; if the
        # latest quarter is missing it (e.g. negative denominator), current=null
        # rather than silently falling back to an earlier quarter's value.
        # fresh-loop2-cycle2 C2B-HIGH-3: also null `current` when the
        # latest aligned window was dropped to skipped_windows — the
        # most recent value in quarterly_data is stale relative to the
        # latest aligned anchor.
        current_val = (
            None if latest_window_dropped
            else (latest_q.get("multiples", {}).get(name) if latest_q else None)
        )
        entry = {
            "min": round(min(values), 2),
            "median": round(_median(values), 2),
            "max": round(max(values), 2),
            "current": current_val,
            "data_points": len(values),
            "span_days": span_days,
        }
        if current_val is None:
            latest_rp = latest_q["report_period"] if latest_q else "N/A"
            if latest_window_dropped:
                entry["current_unavailable_reason"] = (
                    f"newest aligned window {latest_aligned_report_period} "
                    f"was dropped (see skipped_windows); quarterly_data[-1] is "
                    f"{latest_rp}, which is older than the latest aligned anchor."
                )
            else:
                entry["current_unavailable_reason"] = (
                    f"latest quarter {latest_rp} missing {name} "
                    "(negative/zero denominator or missing field)"
                )
        summary[name] = entry

    # Add current snapshot multiples from metrics_snapshot if available
    ms = financial_data.get("metrics_snapshot", {})
    current_from_api: dict = {}
    field_map = {
        "pe": "price_to_earnings_ratio",
        "ps": "price_to_sales_ratio",
        "pb": "price_to_book_ratio",
        "ev_ebitda": "enterprise_value_to_ebitda_ratio",
        "ev_revenue": "enterprise_value_to_revenue_ratio",
    }
    for short, api_field in field_map.items():
        v = ms.get(api_field)
        # fresh-loop2 ISS-033 (correct site): bool reject. Python bool is an
        # int subclass; `isinstance(True, (int, float))` and `math.isfinite(True)`
        # both return True, so a provider that ships
        # `price_to_earnings_ratio: true` would silently emit `pe: 1`
        # downstream. Reject bool explicitly at the numeric boundary.
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if not math.isfinite(v):
            continue
        # Reject pathologically small values that round to 0.0 — Financial
        # Datasets occasionally returns PE / PS / PB as scientific-notation
        # e-08 values (observed on MU, AVGO, CRDO, GLW, MRAAY, TTDKY). After
        # `round(v, 2)` these silently become 0.0 and downstream consumers
        # interpret "0" as a real multiple (infinite earnings ↔ price), not
        # as "the API gave us garbage". Fail-closed: omit from output.
        rounded = round(v, 2)
        if rounded == 0 and v != 0:
            continue
        current_from_api[short] = rounded

    # fresh-loop2 ISS-003: envelope consistency with extract_fcf.py.
    # fresh-loop2 cycle 3 C3B-HIGH-2: downgrade the success status when
    # the latest aligned window was dropped (current=None) OR when ANY
    # skipped_windows were recorded during per-window processing. Pre-
    # fix hardcoded "ok" silently masked partial coverage even with
    # current_unavailable_reason populated. Aligns with extract_fcf's
    # multi-state ok / ok_with_warnings / partial / error machine.
    if latest_window_dropped:
        status = "partial"
    elif skipped_serialized:
        status = "ok_with_warnings"
    else:
        status = "ok"

    # C7: current lags the newest reported income quarter (upstream
    # intersection exclusion — no SkippedWindow record exists for it).
    current_lags_newest_reported = (
        newest_reported_period is not None
        and newest_reported_period > latest_aligned_report_period
    )
    out_warnings = list(fx_warnings_to_propagate)
    lag_fields: dict = {}
    if current_lags_newest_reported:
        lag_fields = {
            "current_lags_newest_reported": True,
            "newest_reported_quarter": newest_reported_period,
        }
        out_warnings.append(
            f"summary.current reflects {latest_aligned_report_period}, but the "
            f"newest reported income quarter is {newest_reported_period} — that "
            f"quarter could not be aligned across all 3 statement families "
            f"(one family likely lags a filing); treat 'current' as one "
            f"quarter behind."
        )
        if status == "ok":
            status = "ok_with_warnings"
    # DL3c §3.3 spec L623-632: current_from_api block at L745-774 (now L850-)
    # is API pass-through and carries its OWN currency basis from FD API.
    # Do NOT certify it under the same `currency_conversion` cert block.
    # When we DID perform an FX conversion on the native statements, surface
    # a sibling key `current_from_api_currency_basis="api_native_usd"` so
    # consumers can distinguish converted-multiples from API-native ones.
    api_basis_sibling: dict = (
        {"current_from_api_currency_basis": "api_native_usd"}
        if cert_block_to_add else {}
    )
    return emit_dl3c_root_marker({
        "status": status,
        "summary": summary,
        "current_from_api": current_from_api,
        "quarterly_detail": quarterly_data,
        "quarters_used": len(quarterly_data),
        "skipped_windows": skipped_serialized,
        **lag_fields,
        **api_basis_sibling,
        **cert_block_to_add,
        **({"warnings": out_warnings} if out_warnings else {}),
    })


def _main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Calculate historical valuation multiples from financial + price data."
    )
    parser.add_argument("--financial-json", required=True,
                        help="Path to 02_financial_data.json")
    parser.add_argument("--price-json", required=True,
                        help="Path to 01_price_data.json")
    parser.add_argument(
        "--ticker", required=True,
        help="Ticker symbol (DL4 §3.2.0.A — threaded through to "
             "iter_aligned_quarter_windows for structured error messages)",
    )
    parser.add_argument("--output", default=None,
                        help="Output file path (default: stdout)")
    args = parser.parse_args()

    try:
        with open(args.financial_json, "r", encoding="utf-8") as f:
            financial_data = json.load(f)
        with open(args.price_json, "r", encoding="utf-8") as f:
            price_data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"historical_multiples: failed to read input: {exc}", file=sys.stderr)
        sys.exit(1)

    from scripts.cli_utils import write_output

    # F (codex Loop review): ratio-correction ADRs would compute multiples from
    # price (per-ADR) × ordinary shares / per-ordinary-share earnings — a unit
    # mismatch. Fail-close at the CLI boundary (the sibling adr_correction.json
    # is path-relative, not in the financial dict). Currency repair does not
    # fix this independent ADR-units problem.
    from pathlib import Path as _Path
    from scripts.schemas.adr_correction import adr_ratio_correction_required
    if adr_ratio_correction_required(_Path(args.financial_json).parent):
        err = emit_dl3c_root_marker({
            "status": "error",
            "error": (
                "adr_ratio_correction_required: multiples use price (per-ADR) "
                "with ordinary-share denominators; historical_multiples does not "
                "apply the ADR ratio. Fail-closed."
            ),
            "fx_failure_reason": "adr_ratio_correction_required",
            "skipped_windows": [],
        })
        write_output(err, args.output)
        print(f"historical_multiples: adr_ratio_correction_required → {args.output}", file=sys.stderr)
        sys.exit(1)

    # v15 R1 cycle-11 A4 — wrap compute call in try/except so an
    # InsufficientQuartersError raise becomes structured error JSON
    # instead of a Python traceback.
    try:
        result = compute_historical_multiples(
            financial_data, price_data, ticker=args.ticker,
        )
    except InsufficientQuartersError as e:
        # fresh-loop2 ISS-003: include `status: "error"` so the CLI-emitted
        # InsufficientQuartersError path matches the in-process return-shape
        # contract.
        # DL3c §4.2 / spec L1666-1672: CLI-emitted error envelopes wrap
        # through emit_dl3c_root_marker for marker consistency at the
        # write boundary (defense-in-depth — the producer's normal returns
        # already self-wrap; this catches the raise path).
        error_result = emit_dl3c_root_marker({
            "status": "error",
            "error": "insufficient_quarters_for_aligned_window",
            "failure_kind": e.failure_kind,
            "detail": str(e),
            "ticker": args.ticker,
        })
        write_output(error_result, args.output)
        print(
            f"historical_multiples: insufficient quarters "
            f"({e.failure_kind}) → {args.output}",
            file=sys.stderr,
        )
        sys.exit(1)

    write_output(result, args.output)
    if args.output:
        q = result.get("quarters_used", 0)
        methods = list(result.get("summary", {}).keys())
        print(
            f"historical_multiples: {q} quarters, methods={methods} → {args.output}",
            file=sys.stderr,
        )
    # fresh-loop2 cycle 5 M1: parity with extract_fcf — CLI exit code
    # must reflect application-level status. Pre-fix only the
    # InsufficientQuartersError path at L833 exited 1; the in-compute
    # error envelope (currency mismatch, malformed root shape, etc.)
    # exited 0 despite writing `{"status": "error", ...}`.
    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    _main()
