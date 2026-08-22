"""Typed contract for `A.DECISION_SEAL`.

Path: `reports/portfolio/{DATE}/.etf_decision_ctx.json`

**NOT `.decision_ctx.json`.** The design document named that path, and it was
already taken: `/portfolio` Step 5 writes an AUTHORING seal there binding the
state / thesis / compiled-strategy vintages the decisions were written
against, and `portfolio_log` refuses the whole run when it finds a file there
it cannot read as one. Two different artifacts with different producers,
lifetimes and consumers cannot share a filename — implemented as specified,
this stopped `/portfolio` writing a decision log at all.

Producer: `scripts.etf.seal` (PR.SEAL_WRITER), from the manifest
Consumer: `scripts.validate` (Step 6) and `scripts.portfolio_log` (Step 8)

The seal answers one question at order time: **are the artifacts still the
ones the decision was made against?** The manifest says what was read; the
seal binds that manifest and the specific artifact bytes behind each ETF row,
and it is re-verified before presentation AND again before logging.

Two rules are load-bearing and easy to get subtly wrong:

- **The variant is chosen from the MANIFEST, never from the seal.** Selecting
  on which keys the seal happens to carry, or on the `skill` it reports about
  itself, lets a seal choose the variant whose requirements it already meets —
  a document grading its own exam.
- **A seal failure refuses only the affected ETF buy.** It never suppresses a
  sell and never fails the set. A stale artifact is a reason not to open a
  position, not a reason to be unable to close one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from scripts.schemas.errors import SchemaError

_ARTIFACT = ".etf_decision_ctx.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")

SEAL_SKILL = "etf-thesis"

# manifest row_kind (+ refusal_kind) -> the fields that variant requires and
# the fields it forbids. The selector comes from the manifest; this table is
# only consulted once the variant is already known.
_VARIANTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "etf_thesis": (
        ("skill", "artifact_sha256", "profile_sha256", "market_snapshot_sha256",
         "manifest_sha256", "generated_at"), ()),
    "etf_refusal:analysis_unavailable": (
        ("skill", "artifact_sha256", "profile_sha256", "market_snapshot_sha256",
         "manifest_sha256", "generated_at"), ()),
    # An ineligible refusal never consulted a snapshot, so binding one would
    # attest a market reading that did not inform the decision.
    "etf_refusal:ineligible": (
        ("skill", "artifact_sha256", "profile_sha256", "manifest_sha256",
         "generated_at"), ("market_snapshot_sha256",)),
    "etf_unavailable": (
        ("skill", "manifest_sha256", "identity_sha256", "sealed_at"), ()),
    "etf_unresolved": (
        ("skill", "manifest_sha256", "sealed_at"), ("identity_sha256",)),
}

_HASH_FIELDS = frozenset({"artifact_sha256", "profile_sha256",
                          "market_snapshot_sha256", "manifest_sha256",
                          "identity_sha256"})
_TIME_FIELDS = frozenset({"generated_at", "sealed_at"})


def _err(field: str, message: str) -> SchemaError:
    return SchemaError(_ARTIFACT, field, message)


def variant_for_row(row) -> Optional[str]:
    """The seal variant a manifest row calls for, or None for a stock row.

    Derived from `row_kind` (+ `refusal_kind`) — the MANIFEST is the selector
    source. Nothing here reads the seal.
    """
    kind = getattr(row, "row_kind", None) or (
        row.get("row_kind") if isinstance(row, dict) else None)
    if kind == "stock":
        return None
    if kind == "etf_refusal":
        raw = getattr(row, "raw", row)
        return f"etf_refusal:{raw.get('refusal_kind')}"
    return kind


@dataclass(frozen=True)
class DecisionSeal:
    seals: Mapping[str, Mapping[str, Any]]
    raw: Mapping[str, Any]

    def for_ticker(self, ticker: str) -> Optional[Mapping[str, Any]]:
        return self.seals.get(ticker)


def validate_decision_seal(raw: Any) -> DecisionSeal:
    """Shape-only validation. Whether a seal AGREES with its manifest row is a
    separate question, answered by `verify_seal_against_row` at order time —
    a seal can be perfectly well-formed and still describe other bytes."""
    if not isinstance(raw, dict):
        raise _err("<root>", f"expected object, got {type(raw).__name__}")
    seals_raw = raw.get("seals")
    if not isinstance(seals_raw, dict):
        raise _err("seals", f"expected object, got {type(seals_raw).__name__}")
    for ticker, seal in seals_raw.items():
        fp = f"seals.{ticker}"
        if not isinstance(seal, dict):
            raise _err(fp, f"expected object, got {type(seal).__name__}")
        for key, value in seal.items():
            if key in _HASH_FIELDS:
                if not isinstance(value, str) or not _SHA256_RE.match(value):
                    raise _err(f"{fp}.{key}",
                               f"expected a lowercase sha256 digest, got "
                               f"{value!r}")
            elif key in _TIME_FIELDS:
                if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
                    raise _err(f"{fp}.{key}",
                               f"expected an ISO timestamp, got {value!r}")
            elif key == "skill":
                if not isinstance(value, str) or not value:
                    raise _err(f"{fp}.skill",
                               f"expected a non-empty string, got {value!r}")
            else:
                raise _err(fp, f"unknown seal key {key!r}")
    return DecisionSeal(seals=seals_raw, raw=raw)


def load_decision_seal(path: str | Path) -> DecisionSeal:
    path = Path(path)
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _err("<file>", f"unreadable: {exc}") from exc
    return validate_decision_seal(raw)


def verify_seal_against_row(seal: Optional[Mapping[str, Any]], row,
                            *, manifest_sha256: str) -> tuple[bool, list[str]]:
    """`(ok, reasons)` for ONE ticker at order time.

    The variant comes from the row. Every field that variant requires must be
    present and must equal what the row says; every field it forbids must be
    absent. `skill` is checked as a value, never used to pick the variant.
    """
    variant = variant_for_row(row)
    if variant is None:
        return True, []          # a stock row needs no seal
    if seal is None:
        return False, ["decision seal absent for this ticker"]

    required, forbidden = _VARIANTS[variant]
    reasons: list[str] = []
    for field in required:
        if field not in seal:
            reasons.append(f"seal is missing {field} (variant {variant})")
    for field in forbidden:
        if field in seal:
            reasons.append(f"seal carries {field}, forbidden for {variant}")
    if reasons:
        return False, reasons

    if seal.get("skill") != SEAL_SKILL:
        reasons.append(f"seal skill is {seal.get('skill')!r}, expected "
                       f"{SEAL_SKILL!r}")
    if seal.get("manifest_sha256") != manifest_sha256:
        reasons.append("seal binds a different manifest than the one loaded")

    raw = getattr(row, "raw", row)
    # `artifact_sha256` seals the thesis bytes; the manifest calls the same
    # value `thesis_sha256`.
    for seal_field, row_field in (("artifact_sha256", "thesis_sha256"),
                                  ("profile_sha256", "profile_sha256"),
                                  ("market_snapshot_sha256",
                                   "market_snapshot_sha256"),
                                  ("identity_sha256", "identity_sha256")):
        if seal_field not in required:
            continue
        seal_value = seal.get(seal_field)
        row_value = raw.get(row_field)
        if seal_value is None or row_value is None:
            # Two ABSENT values are not a match. `None == None` let a seal
            # with a null hash verify against a row missing that hash, which
            # is the exact shape of "nothing was bound" passing as "bound and
            # equal".
            reasons.append(f"seal {seal_field} or the manifest's {row_field} "
                           f"is absent; an unbound hash cannot agree")
        elif seal_value != row_value:
            reasons.append(f"seal {seal_field} disagrees with the manifest's "
                           f"{row_field}")
    return (not reasons), reasons
