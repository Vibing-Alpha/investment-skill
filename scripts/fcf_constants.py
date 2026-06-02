"""String constants for extract_fcf dual-path selection.

Single source of truth — imported by scripts/extract_fcf.py,
prompts/evaluate-valuation.md (docs only), and
tests/test_extract_fcf.py. Do NOT copy-paste these literals; a typo
in a consumer would silently misclassify outputs.
"""

from typing import Final, FrozenSet

# fcf_selection_reason enum:
#   6 state-machine terminal states + 2 fail-close failure modes.
FCF_SELECTION_REASON_LOW_DIVERGENCE_DEFAULT = "low_divergence_default"
FCF_SELECTION_REASON_SINGLE_PATH_ONLY = "single_path_only"
FCF_SELECTION_REASON_NI_SIGN_ANCHOR = "ni_sign_anchor"
FCF_SELECTION_REASON_FALLBACK_MIN_ABS = "fallback_min_abs"
FCF_SELECTION_REASON_BOTH_OPPOSITE_SIGN_NULL = "both_opposite_sign_null"
FCF_SELECTION_REASON_BOTH_INVALID_NULL = "both_invalid_null"
# Emit-phase failure: state machine computed a valid TTM but Stage 4
# couldn't divide by shares (balance sheet missing / outstanding_shares
# non-positive). fcf_per_share=None in this case.
FCF_SELECTION_REASON_SHARES_UNAVAILABLE = "shares_unavailable"
# Pre-aggregation failure (DL4): aligned_quarters could not build a
# valid 4-quarter window (intersection_lt_4, non_consecutive,
# unparseable_fiscal_period, missing_required_field,
# statement_metadata_mismatch, or duplicate_report_period).
# fcf_per_share=None in this case.
FCF_SELECTION_REASON_INSUFFICIENT_QUARTERS = "insufficient_quarters_for_aligned_window"

# fresh-loop2-cycle2 ISS-020: closed-vocabulary frozenset + validator
# so consumers can detect typos at emit time. Pre-fix any string was
# acceptable; a misspelled reason ("low_divergance_default") would
# slip into fcf_inputs.json and silently mis-classify the run.
FCF_SELECTION_REASONS: Final[FrozenSet[str]] = frozenset({
    FCF_SELECTION_REASON_LOW_DIVERGENCE_DEFAULT,
    FCF_SELECTION_REASON_SINGLE_PATH_ONLY,
    FCF_SELECTION_REASON_NI_SIGN_ANCHOR,
    FCF_SELECTION_REASON_FALLBACK_MIN_ABS,
    FCF_SELECTION_REASON_BOTH_OPPOSITE_SIGN_NULL,
    FCF_SELECTION_REASON_BOTH_INVALID_NULL,
    FCF_SELECTION_REASON_SHARES_UNAVAILABLE,
    FCF_SELECTION_REASON_INSUFFICIENT_QUARTERS,
})


def validate_fcf_selection_reason(reason):
    """Raise ValueError if `reason` is not None and not in
    FCF_SELECTION_REASONS. Acts as a boundary check for emit sites.
    Returns the reason for chaining.
    """
    if reason is None:
        return reason
    if reason not in FCF_SELECTION_REASONS:
        raise ValueError(
            f"fcf_selection_reason={reason!r} is not in the closed "
            f"vocabulary {sorted(FCF_SELECTION_REASONS)} — likely a "
            f"typo. Use one of the FCF_SELECTION_REASON_* constants."
        )
    return reason


# fcf_source enum (2 non-null values; None represents "no valid fcf")
FCF_SOURCE_API_FCF = "api_fcf"
FCF_SOURCE_OCF_MINUS_CAPEX = "ocf_minus_capex"

# Algorithm threshold — 20% divergence separates "methods agree" from
# "must decide". Hard threshold (no hysteresis — see spec §Known limitations).
DIVERGENCE_THRESHOLD = 0.20
