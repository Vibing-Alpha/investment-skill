"""Shared constants for DL3c FX work."""

from typing import Final

SUPPORTED_FX_CURRENCIES: Final[frozenset[str]] = frozenset({
    "USD", "JPY", "EUR", "GBP", "KRW", "CNY", "HKD", "TWD", "CHF",
})

FX_BASIS_USD_NATIVE: Final[str] = "usd_native"
FX_BASIS_USD_CONVERTED: Final[str] = "usd_converted"

FX_BASIS_VOCAB: Final[frozenset[str]] = frozenset({
    FX_BASIS_USD_NATIVE, FX_BASIS_USD_CONVERTED,
})

FX_TWO_PATH_CARVE_OUT: Final[frozenset[tuple[str, str, str]]] = frozenset({
    # Consumer-specific carve-out for extract_fcf's two-path state machine
    # (extract_fcf.py:420-428). Pre-scan in apply_fx_conversion MUST skip
    # non-finite check on these 3 fields ONLY when consumer_name=="extract_fcf"
    # (historical_multiples + adr/correct still reject non-finite values on
    # the same fields — see invariant 6).
    ("extract_fcf", "cash_flows", "free_cash_flow"),
    ("extract_fcf", "cash_flows", "net_cash_flow_from_operations"),
    ("extract_fcf", "cash_flows", "capital_expenditure"),
})


FX_FAILURE_REASONS: Final[frozenset[str]] = frozenset({
    "fx_source_unavailable",       # yfinance unreachable / empty / parse error
    "fx_currency_unsupported",     # parseable ISO 4217 but not in SUPPORTED_FX_CURRENCIES (e.g. BRL, INR)
    "fx_history_insufficient",     # report_period precedes yfinance =X start
    "fx_rate_outlier",             # single quarter rate > 10× window median (cycle-4 also <)
    "fx_mixed_currency_window",    # 4 TTM quarters have differing currencies
    "fx_currency_unrecognized",    # currency field not a parseable 3-letter ISO 4217 code (e.g. "Y", "Yen", "")
    "fx_unsupported_annual_path",  # invariant 8 — annual mode + non-USD source
    "fx_partial_conversion",       # invariant 6 — reserved; pre-scan should prevent
    "fx_consumer_field_missing",   # invariant 6 — non-finite-non-None in consumed field
    "fx_ytd_basis_unsupported",    # invariant 19 — period_value_basis="ytd" in TTM window
    # post-impl loop-2 ISS-026: malformed report_period (not YYYY-MM-DD)
    # surfaced by get_fx_window as SHAPE_MISMATCH; pre-fix mapped through
    # _map_fx_fetch_error to fx_source_unavailable, misleading operators
    # who would chase yfinance / network issues instead of data shape.
    "fx_period_malformed",
    # codex Loop review: FDS returned a USD/native field MIX under a single
    # "USD" row label (foreign-issuer ADR); detected via the gross-profit
    # accounting identity. extract_fcf / historical_multiples fail-close with
    # this reason when the upstream mix could not be repaired to clean USD.
    "financials_currency_mixed",
    # codex Loop review F: ratio-correction ADR — extract_fcf / historical_
    # multiples compute per-ORDINARY-share metrics, a unit mismatch against the
    # per-ADR price. Fail-close until per-ADR-units handling exists.
    "adr_ratio_correction_required",
})


# Subset of FX_FAILURE_REASONS that are NOT genuine FX-conversion failures.
# `adr_ratio_correction_required` is a per-ADR / per-ordinary-share UNIT
# mismatch: the statements' currency is fine (USD-native, or already repaired
# to USD via the currency_consistency / gross-profit-identity path), but the
# per-share output is fail-closed because the ADR ratio is unknown. The field
# is plumbed through `fx_failure_reason` only because that is the producers'
# single fail-close-reason channel — semantically it is currency-INDEPENDENT.
# `dl3c_dispatch.dispatch_dl3c_mode` MUST NOT route these to
# `post_dl3c_failed_fx` (whose contract is "underlying data did NOT convert to
# USD — still local currency"); doing so makes `assemble._check_mixed_dl3c_
# modes` abort with a misleading "FX conversion failed / add the currency to
# SUPPORTED_FX_CURRENCIES" FATAL when score-business is re-assembled in a dir
# that already holds an /investment-thesis run's gated artifacts for a
# ratio-unknown ADR (e.g. MRAAY). See tests/test_dl3c_dispatch.py +
# tests/test_assemble_dl3c.py for the regression pins.
NON_FX_FAILCLOSE_REASONS: Final[frozenset[str]] = frozenset({
    "adr_ratio_correction_required",
})


# Cycle-8 F9: warning vocab (distinct from fail-close reasons; populated by
# apply_fx_conversion Step 0' and surfaced via result["warnings"]).
FX_WARNING_REASONS: Final[frozenset[str]] = frozenset({
    "fx_basis_unattested",        # producer omits period_value_basis field; YTD contamination cannot be ruled out (DL3e will add producer support)
})


def validate_fx_warning_reason(reason: str | None) -> None:
    """Raise ValueError if reason not in closed warning vocab (None accepted)."""
    if reason is None:
        return
    if reason not in FX_WARNING_REASONS:
        raise ValueError(
            f"invalid fx warning reason {reason!r}; "
            f"closed vocab is {sorted(FX_WARNING_REASONS)}"
        )

# Routing rule (D3 resolution):
#   _unrecognized: raw currency value is not a parseable 3-letter ISO 4217
#                  code (None / "" / "Y" / "Yen" / non-string types).
#                  Trigger BEFORE checking SUPPORTED_FX_CURRENCIES.
#   _unsupported:  raw value IS a parseable ISO 4217 3-letter code but not
#                  in SUPPORTED_FX_CURRENCIES. Trigger AFTER parse OK.

# Outlier multiplier (invariant 11)
FX_OUTLIER_MULTIPLE: Final[float] = 10.0

# Max lag from report_period to bar_date (invariant 3, matches forward_anchor_price)
FX_MAX_LAG_DAYS: Final[int] = 14


def validate_fx_failure_reason(reason: str | None) -> None:
    """Raise ValueError if reason not in closed vocab (None accepted)."""
    if reason is None:
        return
    if reason not in FX_FAILURE_REASONS:
        raise ValueError(
            f"invalid fx_failure_reason {reason!r}; "
            f"closed vocab is {sorted(FX_FAILURE_REASONS)}"
        )
