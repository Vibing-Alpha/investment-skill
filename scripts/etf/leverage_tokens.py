"""P.LEVERAGE_SCAN — text evidence that a fund is leveraged or inverse.

This is **secondary detection, not certification**. No provider field states
"this fund is 3x"; the scan reads names, descriptions and taxonomy strings and
reports what it found. So it has three outcomes, not two:

    suspected      block-strength evidence — refuse
    unknown        ambiguous evidence, or the scan could not look everywhere
    not_suspected  every input was present, the fund is legally an ETF, and
                   nothing matched

`not_suspected` is the only one that can lead to entry, and it is deliberately
the hardest to reach: it claims the scan looked at all five fields and found
nothing, so a single absent field downgrades it to `unknown`. Silence from a
field nobody read is not evidence of absence.

Every pattern here was executed against the design's measurement ledger before
being written down. Two of them exist only because the obvious version was run
and failed: a positive-percentage rule starting at 100 matched "tracks 100% of
the index" — ordinary prose for a plain index fund — and a bare `short` token
matched 17 of 50 ordinary short-duration bond funds.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

_ETF_LEGAL_TYPE = "exchange traded fund"

# --- normalization ---------------------------------------------------------
# Dashes and the multiplication sign are the two characters providers vary on:
# `−3x`, `–3x` and `-3x` are the same claim, and `3×` is `3x`.
_DASHES = "‐‑‒–—―−"
_MULT_SIGNS = "×✕✖"


def normalize_scan_text(raw) -> str:
    """Lowercase, width-normalize, unify dashes and multiplication signs,
    collapse whitespace. Punctuation INSIDE tokens is preserved: stripping the
    hyphen from `short-term` would turn every short-duration bond fund back
    into a bare `short` hit."""
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFKC", raw).lower()
    for ch in _MULT_SIGNS:
        text = text.replace(ch, "x")
    for ch in _DASHES:
        text = text.replace(ch, "-")
    return re.sub(r"\s+", " ", text).strip()


# --- patterns --------------------------------------------------------------

# Scoped to fund_name. A brand name carrying one of these is unambiguous.
NAME_ONLY_BLOCK = ("bull", "bear", "double", "triple", "ultrapro", "ultrashort")
_NAME_ONLY_BLOCK_RE = re.compile(
    "|".join(rf"\b{re.escape(t)}\b" for t in NAME_ONLY_BLOCK))

# Scoped to fund_name. `short` names an inverse fund AND a maturity; the
# collocations below are the maturity sense and are removed before the test.
_SHORT_COLLOCATIONS = (r"short[- ]term", r"short[- ]duration",
                       r"short[- ]maturity", r"ultra[- ]short",
                       r"short[- ]treasury")
_SHORT_COLLOCATION_RE = re.compile("|".join(_SHORT_COLLOCATIONS))
_BARE_SHORT_RE = re.compile(r"\bshort\b")

# Scoped to every field.
ANY_PATTERNS = (
    r"\bleverag\w*",
    r"\blever(?:s|ed|ing)\b",
    r"\bgear(?:ed|ing)\b",
    r"\bultrapro\b",
    r"\binverse\w*",
)
_ANY_RES = tuple(re.compile(p) for p in ANY_PATTERNS)

# A bare multiplier is not evidence — `1x the index` describes an unlevered
# fund. Suspicion attaches to the VALUE: negative, or above one.
_MULTIPLIER_RE = re.compile(
    r"(?<![0-9A-Za-z.])(-?[0-9]+(?:\.[0-9]+)?)x(?![0-9A-Za-z])")

# Negative percentages, or positive ones from 110 up. Starting at 100 was
# measured matching "tracks 100% of the index".
_PERCENT_RE = re.compile(r"(?<![0-9])(?:-[0-9]+%|(?:1[1-9][0-9]|[2-9][0-9]{2,})%)")

PHRASES = ("twice the", "two times the", "three times the", "daily reset",
           "daily investment results", "of the daily performance")
_PHRASE_RE = re.compile("|".join(re.escape(p) for p in PHRASES))

_SCAN_FIELDS = ("fund_name", "description", "category", "asset_class",
                "legal_type")


@dataclass(frozen=True)
class LeverageScanResult:
    status: str                      # V.LEVERAGE_STATUS
    evidence: tuple                  # ({field, matched_text, pattern_family},)


def _any_scope_hits(field: str, text: str) -> list[dict]:
    """The patterns applied to every field. Returns one record per match kind
    so the artifact can show WHAT was found and WHERE."""
    hits: list[dict] = []
    for rx in _ANY_RES:
        m = rx.search(text)
        if m:
            hits.append({"field": field, "matched_text": m.group(0),
                         "pattern_family": "ANY"})
    for m in _MULTIPLIER_RE.finditer(text):
        try:
            value = float(m.group(1))
        except ValueError:  # unreachable via the pattern; not worth trusting
            continue
        if value < 0 or abs(value) > 1:
            hits.append({"field": field, "matched_text": m.group(0),
                         "pattern_family": "MULTIPLIER_RE"})
            break
    m = _PERCENT_RE.search(text)
    if m:
        hits.append({"field": field, "matched_text": m.group(0),
                     "pattern_family": "PERCENT_RE"})
    m = _PHRASE_RE.search(text)
    if m:
        hits.append({"field": field, "matched_text": m.group(0),
                     "pattern_family": "PHRASE"})
    return hits


def leverage_scan(*, fund_name: Optional[str], description: Optional[str],
                  category: Optional[str], asset_class: Optional[str],
                  legal_type: Optional[str]) -> LeverageScanResult:
    raw = {"fund_name": fund_name, "description": description,
           "category": category, "asset_class": asset_class,
           "legal_type": legal_type}
    norm = {k: normalize_scan_text(v) for k, v in raw.items()}
    # A blank string is an absent field, not an empty one that was read. FMP
    # returns `""` for absent `etfCompany` / `website` rather than null, so an
    # `is None` guard would treat a hole as a field that scanned clean.
    all_inputs_present = all(norm[f] for f in _SCAN_FIELDS)

    evidence: list[dict] = []

    # Block strength: an unambiguous brand token in the name, or any
    # ANY-scope hit in any field.
    name_block = _NAME_ONLY_BLOCK_RE.search(norm["fund_name"])
    if name_block:
        evidence.append({"field": "fund_name",
                         "matched_text": name_block.group(0),
                         "pattern_family": "NAME_ONLY_BLOCK"})
    for field in _SCAN_FIELDS:
        evidence.extend(_any_scope_hits(field, norm[field]))
    block_strength_match = bool(evidence)

    # `short` in the name, once the maturity senses are removed. Ambiguous on
    # its own, so it is recorded separately and only reaches `unknown`.
    name_without_collocations = _SHORT_COLLOCATION_RE.sub(" ", norm["fund_name"])
    name_only_unknown_match = bool(_BARE_SHORT_RE.search(name_without_collocations))
    if name_only_unknown_match:
        evidence.append({"field": "fund_name", "matched_text": "short",
                         "pattern_family": "NAME_ONLY_UNKNOWN"})

    any_pattern_match = block_strength_match or name_only_unknown_match

    # D.LEVERAGE.* — ordered first match.
    if block_strength_match:
        return LeverageScanResult("suspected", tuple(evidence))
    if name_only_unknown_match:
        return LeverageScanResult("unknown", tuple(evidence))
    if (all_inputs_present
            and norm["legal_type"] == _ETF_LEGAL_TYPE
            and not any_pattern_match):
        return LeverageScanResult("not_suspected", ())
    return LeverageScanResult("unknown", tuple(evidence))
