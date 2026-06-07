"""Detect mixed-currency financial-statement rows (FDS foreign-issuer bug).

The Financial Datasets API returns some foreign-issuer (ADR) statements in a
MIXED-currency shape: a subset of fields converted to USD at a per-period FX
rate, the rest left in the native reporting currency, with the row stamped
`currency: "USD"`. Every currency-aware component that trusts the row-level
label is fooled (fetch's `_reconcile_financials_currency`, the DL3c FX gate).

This detector does NOT trust the label — it uses the gross-profit accounting
identity, which a currency mismatch violates by ~the FX factor:

    revenue ≈ cost_of_revenue + gross_profit

It is a BI-DIRECTIONAL detector. A clean row's factor is exactly 1.0 (the
accounting identity); a mix shifts it by the FX scale in one of two directions:
  - HIGH (factor ≫ 1): native worth LESS than USD → native cost/gross are
    numerically huge vs USD revenue (MRAAY/Murata JPY ~142–157, TWD ~31).
  - LOW  (factor ∈ [0.5, 0.95]): native worth MORE than USD → native cost/gross
    are numerically smaller than USD-converted revenue (NOK/Nokia EUR ~0.87,
    GBP ~0.79, CHF ~0.89). This direction was originally NOT flagged (651744af);
    NOK surfaced the gap. `repair_mixed_currency` converts both directions
    identically (usd = native / factor), so detect stays aligned with repair.
Factors in the (0.95, 1.25) deadband — including clean 1.0, and factor<=0 sign
artifacts (VELO negative gross) — are NOT mixed evidence.

DOMAIN LIMIT (revenue floor): the identity is only meaningful when revenue is a
material denominator. Pre-revenue / pre-commercial companies (revenue ≈ 0, e.g.
ASTS 2024: revenue ~$0.5–1.1M vs cost_of_revenue ~$24–29M, with FDS
mis-populating gross_profit) make the factor explode into a meaningless
artifact (~28×) that is NOT an FX rate. Rows with revenue below
`_REV_FLOOR_USD` are therefore NOT treated as mixed evidence; absent any other
high-factor row they fall to "clean" (meaning "no actionable currency mix" —
not "identity verified"). This is safe: consumers act only on status=="mixed",
and fetch persists the currency_consistency marker only on the mixed path, so a
pre-revenue artifact carries no marker.

Consumers:
  - scripts.fetch._reconcile_financials_currency (detect + flag, no raise — so
    the qualitative BQ analysis still runs)
  - scripts.extract_fcf / scripts.historical_multiples (fail-close — these
    compute USD-denominated per-share/multiple values that a mixed statement
    silently corrupts)
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Optional

# Gross-profit identity relative tolerance. On a CLEAN row the factor
# (cost_of_revenue + gross_profit)/revenue is the accounting identity revenue/
# revenue == 1.0 exactly (empirically: every USD-native ticker in reports/ sits
# at 1.0000). A mixed row's factor is the cross-currency scale ratio.
#  - HIGH direction (factor > 1 + _REL_TOL): native worth LESS than USD, so the
#    native cost+gross are numerically huge vs USD revenue (JPY ~149, TWD ~31,
#    KRW/CNY/HKD). 0.25 leaves a wide margin below the smallest such ratio.
_REL_TOL = 0.25

# LOW direction (major-unit native currency worth MORE than USD: EUR/GBP/CHF).
# Here native cost+gross are numerically SMALLER than the USD-converted revenue,
# so factor ≈ 1/(USD-per-native) lands in ~[0.71 (GBP), 0.93 (EUR/CHF)] — BELOW
# 1 yet INSIDE the high-direction tolerance, which is why the original single-
# high-direction gate (commit 651744af) was blind to it (NOK/Nokia escape). A
# row is low-factor mixed evidence iff factor ∈ [_LOW_FACTOR_MIN, _LOW_FACTOR_MAX]:
#  - _LOW_FACTOR_MAX 0.95 keeps a 5% deadband below the clean 1.0. This margin is
#    the PRIMARY false-positive defense, NOT the repair safety net: empirically a
#    repair re-validation (I4 etc., 3% tol) does NOT reliably catch a clean row
#    wrongly flagged just under 1.0 — at low operating_income/gross_profit the
#    post-scale identity error stays < 3% and a false flag would be silently
#    "repaired" (corrupted). So the band MUST stay clear of where clean rows sit
#    (exactly 1.0); 0.95 is about the closest-to-1.0 a flag can be while real
#    EUR/GBP/CHF mixes (≤0.93) are still caught and clean 1.0 rows are excluded.
#    INHERENT LIMITATION: a major-unit currency trading within ~5% of USD parity
#    (EUR/CHF near 1.0 → factor in (0.95, 1.0)) is indistinguishable from clean
#    via this identity and is NOT flagged — closing that needs an EXTERNAL signal
#    (reporting-currency registry / FX cross-check), out of scope here.
#  - _LOW_FACTOR_MIN 0.5 excludes factor<=0 artifacts (negative/zero gross —
#    VELO) and sits below GBP's ~0.71; exotic >2x-USD currencies are out of the
#    supported FX set and intentionally not in scope.
_LOW_FACTOR_MAX = 0.95
_LOW_FACTOR_MIN = 0.5

# Revenue floor for the gross-profit identity to be meaningful. Below this the
# denominator is too small and the factor becomes a near-zero-revenue artifact
# rather than an FX rate (pre-revenue / pre-commercial companies — ASTS). Set
# below the known MRAAY sample (~$3.27B); NOT a universal ADR lower bound.
_REV_FLOOR_USD = 10_000_000.0


def _num(v: Any) -> Optional[float]:
    """Finite numeric or None. Rejects bool AND non-finite (NaN/Inf) — a NaN
    revenue must NOT be classified 'clean', and a non-finite field must never
    be divided or written as a market cap (codex Loop review A/B)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _row_currency_factor(row: dict) -> Optional[float]:
    """(cost_of_revenue + gross_profit) / revenue for a row, or None.

    Returns None when any of the three fields is missing/non-positive (cannot
    evaluate the identity for this row).
    """
    rev = _num(row.get("revenue"))
    cor = _num(row.get("cost_of_revenue"))
    gp = _num(row.get("gross_profit"))
    if rev is None or cor is None or gp is None or rev <= 0:
        return None
    return (cor + gp) / rev


def _mixed_evidence_factor(row: dict) -> Optional[float]:
    """The gross-profit factor IFF `row` is mixed-currency evidence.

    Gate 1 (always): `revenue >= _REV_FLOOR_USD` so the identity denominator is
    material (excludes pre-revenue near-zero-denominator artifacts — ASTS).
    Gate 2 (either direction): the factor is a cross-currency scale ratio, not
    the clean 1.0 —
      - HIGH `factor > 1 + _REL_TOL`  (native worth less than USD: JPY/KRW/TWD…)
      - LOW  `_LOW_FACTOR_MIN <= factor <= _LOW_FACTOR_MAX`
                                       (native worth more than USD: EUR/GBP/CHF)
    Returns the factor (used directly as the per-period implied FX divisor by
    repair: usd = native / factor, valid in both directions) when the gates
    hold, else None.

    SINGLE SOURCE OF TRUTH for the mixed-evidence predicate, shared by
    `detect_mixed_currency` (classification) and `repair_mixed_currency`
    (per-period FX selection) so the two can never drift (producer-consumer #3 —
    a divergence would let repair convert a sub-floor artifact period that
    detect does not count as mixed).
    """
    if not isinstance(row, dict):
        return None
    factor = _row_currency_factor(row)
    if factor is None:
        return None
    rev = _num(row.get("revenue"))  # _row_currency_factor guarantees rev>0 here
    if rev is None or rev < _REV_FLOOR_USD:
        return None
    high = factor > 1.0 + _REL_TOL
    low = _LOW_FACTOR_MIN <= factor <= _LOW_FACTOR_MAX
    if high or low:
        return factor
    return None


def detect_mixed_currency(income_statements: list[dict]) -> dict:
    """Classify a list of income-statement rows for mixed-currency contamination.

    Returns a dict:
      {
        "status": "clean" | "mixed" | "unknown",
        "checked_rows": int,
        "mixed_rows": [ {"report_period": str, "factor": float}, ... ],
        "implied_fx": float | None,   # median identity factor of mixed rows
      }

    - "mixed"   — at least one checkable row is mixed evidence: factor in the
                  high (> 1 + _REL_TOL) OR low ([_LOW_FACTOR_MIN, _LOW_FACTOR_MAX])
                  direction AND revenue at/above the _REV_FLOOR_USD domain floor.
    - "clean"   — at least one row checked, none qualifies as mixed evidence
                  (in the (0.95, 1.25) deadband, sub-floor, or sign artifact).
    - "unknown" — no row had revenue+cost_of_revenue+gross_profit to check.
    """
    rows = income_statements or []
    mixed_rows: list[dict] = []
    checked = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        factor = _row_currency_factor(row)
        if factor is None:
            continue
        checked += 1
        # Mixed evidence via the shared predicate (high OR low direction AND
        # material revenue floor). Same helper drives repair's per-period FX so
        # the two cannot drift (producer-consumer #3).
        if _mixed_evidence_factor(row) is not None:
            mixed_rows.append({
                "report_period": row.get("report_period"),
                "factor": round(factor, 4),
            })
    if checked == 0:
        status = "unknown"
    elif mixed_rows:
        status = "mixed"
    else:
        status = "clean"
    implied_fx = (
        round(median(m["factor"] for m in mixed_rows), 4) if mixed_rows else None
    )
    return {
        "status": status,
        "checked_rows": checked,
        "mixed_rows": mixed_rows,
        "implied_fx": implied_fx,
    }


# ---------------------------------------------------------------------------
# Self-validating repair (un-mix a foreign-issuer statement to clean USD)
# ---------------------------------------------------------------------------

# Fields FDS already converted to USD on mixed rows (left UNCHANGED by repair).
# Empirically observed (MRAAY/Murata). The repair does NOT trust this set
# blindly — it converts the native monetary whitelist below and then RE-VALIDATES
# cross-currency accounting identities that span a USD-classified and a
# native-classified field; a misclassification breaks an identity → fail-close.
# KNOWN LIMITATION (net_income_non_controlling_interests): classified
# already-USD from the original MRAAY observation, but NOK reports it in native
# EUR (small round values, e.g. -1M..-6M). On NOK-shaped data the repair leaves
# it unconverted, so that single field stays native post-repair. Materiality is
# negligible (≤~5.7% of net_income, absolute ≤$6M, and NO downstream consumer
# reads it — net_income itself is correctly USD). Reclassifying to native risks
# double-converting issuers where FDS DID convert it (no data to disambiguate),
# so it is intentionally left as-is pending an issuer with material native nci.
_CONVERTED_USD_FIELDS: dict[str, frozenset[str]] = {
    "income_statements": frozenset({
        "revenue", "operating_income", "net_income", "interest_expense",
        "net_income_non_controlling_interests",
    }),
    "balance_sheets": frozenset({
        "cash_and_equivalents", "current_debt", "non_current_debt",
        "total_debt", "shareholders_equity",
    }),
    "cash_flows": frozenset({
        "net_cash_flow_from_operations", "depreciation_and_amortization",
        "capital_expenditure", "investment_acquisitions_and_disposals",
        "issuance_or_repayment_of_debt_securities",
    }),
}

# Native (reporting-currency) monetary fields the repair divides by the per-row
# FX. EXHAUSTIVE per family (every FDS financial-statement monetary line item
# not in the converted-USD set). Any numeric monetary field present that is in
# NEITHER set and is not recognized as non-monetary → the repair FAIL-CLOSES
# (codex Loop review D: the curated set must not silently leave another ADR's
# fields native). Completeness is ENFORCED against
# scripts/sources/financial_datasets._FINANCIALS_NUMERIC_FIELDS by
# tests/test_currency_consistency.py::
#   test_every_fds_financials_field_is_classified_for_repair — if FDS adds a
# monetary field, that test fails until it is classified here (else the repair
# fail-closes every foreign issuer carrying it; the P2 NOK failure class).
_NATIVE_MONETARY_FIELDS: dict[str, frozenset[str]] = {
    "income_statements": frozenset({
        "cost_of_revenue", "gross_profit", "operating_expense",
        "selling_general_and_administrative_expenses", "research_and_development",
        "ebit", "ebitda", "income_tax_expense", "net_income_common_stock",
        "net_income_discontinued_operations", "consolidated_income",
    }),
    "balance_sheets": frozenset({
        "total_assets", "current_assets", "current_investments", "inventory",
        "trade_and_non_trade_receivables", "non_current_assets",
        "property_plant_and_equipment", "goodwill_and_intangible_assets",
        "investments", "non_current_investments", "tax_assets",
        "total_liabilities", "current_liabilities", "trade_and_non_trade_payables",
        "deferred_revenue", "deposit_liabilities", "non_current_liabilities",
        "tax_liabilities", "retained_earnings",
        "accumulated_other_comprehensive_income",
    }),
    "cash_flows": frozenset({
        "net_income", "share_based_compensation", "net_cash_flow_from_investing",
        "property_plant_and_equipment", "business_acquisitions_and_disposals",
        "net_cash_flow_from_financing", "issuance_or_purchase_of_equity_shares",
        "dividends_and_other_cash_distributions", "change_in_cash_and_equivalents",
        "effect_of_exchange_rate_changes", "free_cash_flow", "ending_cash_balance",
    }),
}

# Non-monetary numeric fields that must NEVER be divided by FX (per-share,
# share counts, ratios, margins, growth, yields). Exact set + suffix patterns.
_NON_MONETARY_EXACT: frozenset[str] = frozenset({
    "outstanding_shares", "weighted_average_shares",
    "weighted_average_shares_diluted", "shares",
    # numeric metadata (not money) — excluded so they don't trip the
    # unclassified-monetary fail-close.
    "fiscal_year", "calendar_year", "report_year",
})


def _is_non_monetary(key: str) -> bool:
    if key in _NON_MONETARY_EXACT:
        return True
    if "_per_share" in key:
        return True
    return key.endswith(("_shares", "_ratio", "_margin", "_growth", "_yield"))


_IDENTITY_TOL = 0.03  # 3% — covers rounding + per-period FX approximation


def _v(row: dict, key: str) -> Optional[float]:
    return _num(row.get(key)) if isinstance(row, dict) else None


def _rel_ok(lhs: Optional[float], rhs: Optional[float]) -> Optional[bool]:
    """True/False if both present (rel diff within tol); None if not checkable."""
    if lhs is None or rhs is None:
        return None
    denom = max(abs(lhs), abs(rhs))
    if denom == 0:
        return abs(lhs - rhs) < 1.0
    return abs(lhs - rhs) / denom <= _IDENTITY_TOL


def _validate_cross_currency_identities(
    income: list[dict], balance: list[dict], fx_by_period: dict,
) -> list[str]:
    """Post-repair check of identities spanning USD + native fields.

    These are NON-tautological (the gross-profit identity used to derive FX is
    tautological post-repair and is intentionally excluded). A wrong currency
    classification of equity / operating_income / net_income breaks one of
    these → the caller fail-closes.

    Note: a free_cash_flow ≈ OCF - |capex| identity is intentionally NOT used —
    this repo's FCF model supports legitimate api_fcf vs ocf-capex divergence
    (codex Loop review R4), so it would false-fail-close valid statements. FCF
    correctness for ADRs is instead guarded by the per-ADR-ratio fail-close in
    extract_fcf / historical_multiples.
    """
    violations: list[str] = []
    bal_by_period = {
        r.get("report_period"): r for r in balance if isinstance(r, dict)
    }
    for row in income:
        if not isinstance(row, dict):
            continue
        period = row.get("report_period")
        if period not in fx_by_period:
            continue
        # I4: gross_profit = operating_income + operating_expense (validates op_income USD)
        oi, oe = _v(row, "operating_income"), _v(row, "operating_expense")
        if _v(row, "gross_profit") is not None and oi is not None and oe is not None:
            if _rel_ok(_v(row, "gross_profit"), oi + oe) is False:
                violations.append(f"{period}: gross_profit != operating_income + operating_expense")
        # net_income (USD) validates against the SAME line item in native
        # currency (now USD post-repair). FDS `net_income` is the common-stock
        # (after-minority) figure, so pair it with net_income_common_stock when
        # present; consolidated_income is the BEFORE-minority total and differs
        # by the non-controlling interest, which false-fails this identity for
        # issuers with material minority interest (NOK 2025-06-30: 6.25% gap).
        ni_native = _v(row, "net_income_common_stock")
        if ni_native is None:
            ni_native = _v(row, "consolidated_income")
        if _rel_ok(_v(row, "net_income"), ni_native) is False:
            violations.append(f"{period}: net_income != net_income_common_stock")
        # I2 (accounting equation): total_assets = total_liabilities + shareholders_equity (validates equity USD)
        bal = bal_by_period.get(period)
        if bal is not None:
            ta, tl, eq = _v(bal, "total_assets"), _v(bal, "total_liabilities"), _v(bal, "shareholders_equity")
            if ta is not None and tl is not None and eq is not None:
                if _rel_ok(ta, tl + eq) is False:
                    violations.append(
                        f"{period}: total_assets != total_liabilities + shareholders_equity")
    return violations


def repair_mixed_currency(financial_data: dict) -> dict:
    """Un-mix an FDS foreign-issuer statement to clean USD, or fail-close.

    Strategy: derive a per-period FX from the gross-profit identity, divide the
    native-monetary whitelist by it (the converted-USD set + non-monetary fields
    are left as-is), then RE-VALIDATE cross-currency accounting identities. If
    all hold → mutate `financial_data` in place to the repaired USD statement +
    stamp a `currency_consistency` provenance marker. If any identity fails →
    leave `financial_data` UNCHANGED and report unrepairable (caller fail-closes).

    Returns {"status": "repaired" | "unrepairable" | "not_needed", ...}.
    """
    import copy

    income = financial_data.get("income_statements") or []
    if detect_mixed_currency(income)["status"] != "mixed":
        return {"status": "not_needed"}

    # B: per-period FX via the SAME mixed-evidence predicate as detect (high OR
    # low direction AND revenue floor). Sub-floor artifact periods, deadband
    # rows, sign artifacts, and non-finite are excluded → they get no FX and the
    # period-coverage check below fail-closes any row needing one (rather than
    # dividing an artifact period by a meaningless ~28× — producer-consumer #3).
    fx_by_period: dict[str, float] = {}
    for row in income:
        f = _mixed_evidence_factor(row)
        if f is not None and math.isfinite(f):
            fx_by_period[row.get("report_period")] = f

    fams = {
        "income_statements": copy.deepcopy(income),
        "balance_sheets": copy.deepcopy(financial_data.get("balance_sheets") or []),
        "cash_flows": copy.deepcopy(financial_data.get("cash_flows") or []),
    }
    converted_fields: dict[str, int] = {}
    for fam, rows in fams.items():
        usd = _CONVERTED_USD_FIELDS.get(fam, frozenset())
        native = _NATIVE_MONETARY_FIELDS.get(fam, frozenset())
        for row in rows:
            if not isinstance(row, dict):
                continue
            fx = fx_by_period.get(row.get("report_period"))
            for key, val in list(row.items()):
                num = _num(val)
                if num is None:
                    continue              # non-numeric / non-finite — leave as-is
                # Explicit classification takes precedence over the suffix-based
                # non-monetary heuristic (codex Loop review R1: a native field
                # like `issuance_or_purchase_of_equity_shares` ends in `_shares`
                # and would otherwise be skipped by the heuristic).
                if key in usd:
                    continue              # already USD — leave
                if key in native:
                    if fx is None:
                        # C: a native monetary field in a period with no valid FX
                        # cannot be converted → fail-close (leave data unchanged).
                        return {
                            "status": "unrepairable",
                            "reason": f"no_fx_for_period: {row.get('report_period')!r} field {key!r}",
                            "implied_fx_by_period": {k: round(v, 4) for k, v in fx_by_period.items()},
                        }
                    row[key] = num / fx
                    converted_fields[key] = converted_fields.get(key, 0) + 1
                elif _is_non_monetary(key):
                    continue              # shares / per-share / ratio — never divide
                else:
                    # D: an unclassified monetary field (another ADR's shape) →
                    # unsafe to assume native/USD → fail-close rather than risk
                    # leaving it mixed or wrongly dividing.
                    return {
                        "status": "unrepairable",
                        "reason": f"unclassified_monetary_field: {fam}.{key}",
                        "implied_fx_by_period": {k: round(v, 4) for k, v in fx_by_period.items()},
                    }

    violations = _validate_cross_currency_identities(
        fams["income_statements"], fams["balance_sheets"], fx_by_period
    )
    if violations:
        return {
            "status": "unrepairable",
            "violations": violations,
            "implied_fx_by_period": {k: round(v, 4) for k, v in fx_by_period.items()},
        }

    # Accept: write repaired rows back + provenance marker.
    financial_data["income_statements"] = fams["income_statements"]
    financial_data["balance_sheets"] = fams["balance_sheets"]
    financial_data["cash_flows"] = fams["cash_flows"]
    marker = {
        "status": "repaired",
        "method": "gross_profit_identity_implied_fx",
        "provenance": "calc_implied_fx",
        "implied_fx_by_period": {k: round(v, 4) for k, v in fx_by_period.items()},
        "converted_fields": sorted(converted_fields),
        "note": (
            "Native-currency fields divided by the per-period FX implied by the "
            "gross-profit identity (reconstructs the rate FDS used for its USD-"
            "converted fields); validated against cross-currency accounting "
            "identities. These fields are DERIVED [Calc], NOT raw [API] — "
            "downstream source tags must reflect that."
        ),
    }
    financial_data["currency_consistency"] = marker
    return {"status": "repaired", **marker}
