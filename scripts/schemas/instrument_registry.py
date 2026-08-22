"""Typed contracts for the ETF identity layer.

Two artifacts, one producer (`scripts.etf.detect` / PR.DETECT):

- `A.INSTRUMENT_REGISTRY` — `reports/{T}/instrument_type.json`, the per-ticker
  cached verdict. `strict: true` in the contract: any drift is a load failure,
  never a silently-degraded read, because a mis-loaded verdict is what lets an
  ETF reach the stock authoring path.
- `A.IDENTITY_MAP` — `reports/{portfolio,monitor}/{DATE}/.etf_identity.json`,
  one whole-map write per batch.

Contract: docs/superpowers/specs/2026-08-21-etf-thesis-design.md
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from scripts.schemas.errors import SchemaError

# V.INSTRUMENT_TYPE / V.RESOLUTION_STATUS — closed vocabularies. A value
# outside these sets is drift, not a new state: every consumer branches
# exhaustively on them.
INSTRUMENT_TYPES = frozenset({"etf", "equity", "unknown"})
RESOLUTION_STATUSES = frozenset({"resolved", "refused"})

InstrumentType = Literal["etf", "equity", "unknown"]
ResolutionStatus = Literal["resolved", "refused"]

# A.INSTRUMENT_REGISTRY.read: "version-mismatched => re-probe". Bump this
# whenever the emitted shape or the verdict semantics change, so every cached
# file written by the old code is re-derived instead of reinterpreted.
REGISTRY_OUTPUT_VERSION = 1

_REGISTRY_ARTIFACT = "instrument_type.json"
_MAP_ARTIFACT = ".etf_identity.json"


def _parse_timestamp(artifact: str, field: str, raw: Any) -> datetime.datetime:
    if not isinstance(raw, str) or not raw:
        raise SchemaError(artifact, field, f"expected ISO timestamp string, got {raw!r}")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        ts = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchemaError(artifact, field, f"not an ISO timestamp: {raw!r} ({exc})") from exc
    # Naive timestamps would compare as local time against a UTC now(), which
    # silently shifts the age check by the host's offset. Producers always
    # write UTC; a naive value is treated as UTC rather than rejected so a
    # hand-edited fixture stays usable.
    return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)


def _require_str(artifact: str, obj: Mapping, key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise SchemaError(artifact, key, f"expected non-empty string, got {v!r}")
    return v


def _require_member(artifact: str, obj: Mapping, key: str,
                    allowed: frozenset[str]) -> str:
    v = _require_str(artifact, obj, key)
    if v not in allowed:
        raise SchemaError(artifact, key,
                          f"{v!r} is outside {sorted(allowed)}")
    return v


def _require_int(artifact: str, obj: Mapping, key: str) -> int:
    v = obj.get(key)
    # bool is an int subclass; `output_version: true` must not read as 1.
    if not isinstance(v, int) or isinstance(v, bool):
        raise SchemaError(artifact, key, f"expected int, got {v!r}")
    return v


def _require_mapping(artifact: str, obj: Mapping, key: str) -> Mapping:
    v = obj.get(key)
    if not isinstance(v, dict):
        raise SchemaError(artifact, key, f"expected object, got {type(v).__name__}")
    return v


def _read_json(artifact: str, path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SchemaError(artifact, "<file>", f"unreadable: {exc}") from exc


# ---------------------------------------------------------------------------
# A.INSTRUMENT_REGISTRY
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstrumentRegistry:
    ticker: str
    instrument_type: InstrumentType
    fmp_verdict: Mapping[str, Any]
    yfinance_verdict: Mapping[str, Any]
    resolved_at: datetime.datetime
    output_version: int

    def age_days(self, *, now: datetime.datetime | None = None) -> float:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return (now - self.resolved_at).total_seconds() / 86400.0


def validate_instrument_registry(raw: Any) -> InstrumentRegistry:
    a = _REGISTRY_ARTIFACT
    if not isinstance(raw, dict):
        raise SchemaError(a, "<root>", f"expected object, got {type(raw).__name__}")
    return InstrumentRegistry(
        ticker=_require_str(a, raw, "ticker"),
        instrument_type=_require_member(a, raw, "instrument_type", INSTRUMENT_TYPES),
        fmp_verdict=_require_mapping(a, raw, "fmp_verdict"),
        yfinance_verdict=_require_mapping(a, raw, "yfinance_verdict"),
        resolved_at=_parse_timestamp(a, "resolved_at", raw.get("resolved_at")),
        output_version=_require_int(a, raw, "output_version"),
    )


def load_instrument_registry(path: str | Path) -> InstrumentRegistry:
    return validate_instrument_registry(_read_json(_REGISTRY_ARTIFACT, Path(path)))


# ---------------------------------------------------------------------------
# A.IDENTITY_MAP
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdentityRow:
    ticker: str
    instrument_type: InstrumentType
    resolution_status: ResolutionStatus
    source_verdicts: Mapping[str, Any]
    resolved_at: datetime.datetime
    output_version: int


@dataclass(frozen=True)
class IdentityMap:
    rows: Mapping[str, IdentityRow]
    output_version: int


def validate_identity_map(raw: Any) -> IdentityMap:
    a = _MAP_ARTIFACT
    if not isinstance(raw, dict):
        raise SchemaError(a, "<root>", f"expected object, got {type(raw).__name__}")
    rows_raw = raw.get("rows")
    if not isinstance(rows_raw, dict):
        raise SchemaError(a, "rows", f"expected object, got {type(rows_raw).__name__}")
    rows: dict[str, IdentityRow] = {}
    for key, row in rows_raw.items():
        if not isinstance(row, dict):
            raise SchemaError(a, f"rows.{key}",
                              f"expected object, got {type(row).__name__}")
        field = f"rows.{key}"
        ticker = _require_str(a, row, "ticker")
        if ticker != key:
            raise SchemaError(a, field,
                              f"row key {key!r} disagrees with ticker {ticker!r}")
        rows[key] = IdentityRow(
            ticker=ticker,
            instrument_type=_require_member(a, row, "instrument_type", INSTRUMENT_TYPES),
            resolution_status=_require_member(a, row, "resolution_status",
                                              RESOLUTION_STATUSES),
            source_verdicts=_require_mapping(a, row, "source_verdicts"),
            resolved_at=_parse_timestamp(a, f"{field}.resolved_at",
                                         row.get("resolved_at")),
            output_version=_require_int(a, row, "output_version"),
        )
    return IdentityMap(rows=rows, output_version=_require_int(a, raw, "output_version"))


def load_identity_map(path: str | Path) -> IdentityMap:
    return validate_identity_map(_read_json(_MAP_ARTIFACT, Path(path)))
