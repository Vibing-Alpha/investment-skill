"""Typed JSON/YAML artifact contracts for inter-module data flow.

Stdlib-only. Each module exports a frozen dataclass shape + a
`load_<artifact>(path)` function that returns the dataclass or raises
`SchemaError`. Schemas for macro_rates are intentionally consumer-loose
(fields not required by any consumer are optional at load time); the
strategy schema is stricter because its fields have fewer valid shapes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scripts.schemas.errors import SchemaError, DataQualityError
from scripts.schemas.source_tag import (
    SOURCE_TAG_RE,
    PLACEHOLDER_DESCRIPTORS,
    WEBSEARCH_BINDING_MARKER,
    WEBSEARCH_BINDING_VERSION,
    WEBSEARCH_BOUND_RE,
    check_websearch_binding,
    stamp_websearch_binding,
    validate_source_tags,
    websearch_binding_active,
)
from scripts.schemas.bq_analysis import (
    BqAnalysis,
    BqMeta,
    BqScores,
    validate_bq_analysis,
    load_bq_analysis,
)
from scripts.schemas.adr_profile import AdrProfile, load_adr_profile  # noqa: F401
from scripts.schemas.quarter_window import (  # noqa: F401
    AlignedQuarter,
    AlignedQuarterWindow,
    SkippedWindow,
    aligned_pair,
    aligned_quarters,
    iter_aligned_quarter_windows,
    InsufficientQuartersError,
    row_matches_period,
)

if TYPE_CHECKING:  # pragma: no cover - static type hints only
    from scripts.schemas.investment_thesis import (
        InvestmentThesis,
        ThesisMeta,
        validate_investment_thesis,
        load_investment_thesis,
    )

__all__ = [
    "SchemaError",
    "DataQualityError",
    "SOURCE_TAG_RE",
    "PLACEHOLDER_DESCRIPTORS",
    "WEBSEARCH_BINDING_MARKER",
    "WEBSEARCH_BINDING_VERSION",
    "WEBSEARCH_BOUND_RE",
    "check_websearch_binding",
    "stamp_websearch_binding",
    "validate_source_tags",
    "websearch_binding_active",
    "BqAnalysis",
    "BqMeta",
    "BqScores",
    "validate_bq_analysis",
    "load_bq_analysis",
    "AdrProfile",
    "load_adr_profile",
    "InvestmentThesis",
    "ThesisMeta",
    "validate_investment_thesis",
    "load_investment_thesis",
    "AlignedQuarter",
    "AlignedQuarterWindow",
    "SkippedWindow",
    "aligned_pair",
    "aligned_quarters",
    "iter_aligned_quarter_windows",
    "InsufficientQuartersError",
    "row_matches_period",
]


# Lazy import for investment_thesis: PEP 562 __getattr__ keeps the
# `from scripts.schemas import InvestmentThesis` surface working while
# avoiding the `python3 -m scripts.schemas.investment_thesis` RuntimeWarning
# that occurs when the submodule is eagerly imported during package init.
def __getattr__(name: str) -> Any:
    _lazy = {
        "InvestmentThesis",
        "ThesisMeta",
        "validate_investment_thesis",
        "load_investment_thesis",
    }
    if name in _lazy:
        from scripts.schemas import investment_thesis as _mod
        return getattr(_mod, name)
    raise AttributeError(f"module 'scripts.schemas' has no attribute {name!r}")
