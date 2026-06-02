"""ADR detection, classification, and valuation correction.

Submodules:
    detect  — detect_adr, detect_growth_stock_mode, detect_adr_market_data
    correct — compute_adr_valuation_correction, compute_adr_eps_check
"""

import math


def safe_float(v, default=0):
    """Safe float coercion — None/bool/string/non-finite -> default.

    Shared by detect and correct submodules.
    """
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def is_finite_numeric(v) -> bool:
    """True iff v is a finite real number (not None, bool, NaN, Inf,
    or non-numeric string). Companion to safe_float: when callers need
    to distinguish "field genuinely absent / non-numeric" from
    "field present but zero" they should check is_finite_numeric BEFORE
    calling safe_float, so missing-field detection isn't lost to the
    default coercion (fresh-loop3 ISS-036).
    """
    if v is None or isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False

from scripts.adr.detect import (
    detect_adr,
    build_instrument_profile,
    detect_adr_market_data,
    detect_growth_stock_mode,
)
from scripts.adr.correct import (
    compute_adr_valuation_correction,
    compute_adr_eps_check,
)

__all__ = [
    "detect_adr",
    "build_instrument_profile",
    "detect_adr_market_data",
    "detect_growth_stock_mode",
    "compute_adr_valuation_correction",
    "compute_adr_eps_check",
    "is_finite_numeric",
]
