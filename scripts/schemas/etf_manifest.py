"""Typed contract for `A.ETF_MANIFEST` — a tagged union with five row kinds.

Path: `reports/portfolio/{DATE}/.etf_manifest.json`
Producer: `scripts.etf.manifest` (PR.MANIFEST)
Consumer: `scripts.validate` (P.ETF_BUY_ORDER)

The manifest is what the validator reads to decide whether a proposed buy may
go through. It covers every holding and every watchlist ticker, so a buy for a
ticker with no row is a buy for something nobody classified — and the reason
this artifact exists at all is that the pre-manifest validator accepted state
`{AAPL}` plus a proposed `buy SOXX` as `passed=True, violations=[]`.

Five kinds, and the distinctions carry weight:

    stock            an ordinary equity. No identity hash — the stock path
                     does not read one, and requiring one would make every
                     stock buy depend on the ETF pipeline.
    etf_thesis       an ETF with a usable thesis. Carries the decision context
                     the prompt reads, so the prompt never re-derives it.
    etf_refusal      an ETF whose thesis refused. Still a complete row: a
                     refusal is information, not an absence.
    etf_unavailable  an ETF whose artifact is missing, unreadable, or
                     contradicted by its own run_meta hash.
    etf_unresolved   identity could not be resolved. Blocks every buy.

`strict: true`: unknown keys, missing fields, malformed paths and malformed
hashes are rejected rather than defaulted. A manifest that half-loads is worse
than one that fails, because the validator would then authorize against the
half it could read.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from scripts.schemas.errors import SchemaError

_ARTIFACT = ".etf_manifest.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ROW_KINDS = frozenset({"stock", "etf_refusal", "etf_thesis",
                       "etf_unavailable", "etf_unresolved"})
BUNDLE_STATUSES = frozenset({"loaded", "absent", "unloadable"})
UNAVAILABLE_REASONS = frozenset({"artifact_absent", "artifact_unloadable",
                                 "run_meta_artifact_hash_mismatch"})
REFUSAL_KINDS = frozenset({"ineligible", "analysis_unavailable"})
ENTRY_ELIGIBILITIES = frozenset({"pass", "block", "unknown"})
ANALYSIS_READINESS = frozenset({"ready", "unavailable"})
MERITS = frozenset({"strong_add", "add", "watch", "pass", "avoid"})
INSTRUMENT_TYPES = frozenset({"etf", "equity", "unknown"})
RESOLUTION_STATUSES = frozenset({"resolved", "refused"})

_THESIS_CONTEXT_FIELDS = ("kind", "technical_timing", "environment",
                          "entry_conditions", "invalidation_conditions",
                          "top_holdings", "coverage_pct", "merit_evidence")
_REFUSAL_CONTEXT_FIELDS = ("invalidation_conditions", "conditions_authored_at",
                           "source_thesis_sha256")

# Every key any row kind may carry. A key outside this set is drift, and
# silently keeping it would let a future producer smuggle a field past the
# consumers that branch on this union.
_COMMON_KEYS = frozenset({"ticker", "row_kind", "instrument_type",
                          "bundle_status"})
_ALLOWED_KEYS = {
    "stock": _COMMON_KEYS,
    "etf_thesis": _COMMON_KEYS | {
        "identity_sha256", "thesis_path", "thesis_sha256", "profile_sha256",
        "market_path", "market_snapshot_sha256", "entry_eligibility",
        "analysis_readiness", "merit_recommendation", "avg_volume_shares",
        "avg_volume_shares_as_of", "decision_context", "profile_retrieved_at",
        "approval_reviewed_on"},
    "etf_refusal": _COMMON_KEYS | {
        "identity_sha256", "thesis_path", "thesis_sha256", "profile_sha256",
        "market_path", "market_snapshot_sha256", "entry_eligibility",
        "analysis_readiness", "refusal_kind", "avg_volume_shares",
        "avg_volume_shares_as_of", "decision_context", "entry_reasons",
        "analysis_reasons", "profile_retrieved_at", "approval_reviewed_on"},
    "etf_unavailable": _COMMON_KEYS | {"identity_sha256", "reason"},
    "etf_unresolved": _COMMON_KEYS | {"resolution_status"},
}


def _err(field: str, message: str) -> SchemaError:
    return SchemaError(_ARTIFACT, field, message)


def _finite(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _require(row: Mapping, key: str, field: str):
    if key not in row:
        raise _err(field, "required key missing")
    return row[key]


def _sha(row: Mapping, key: str, field: str, *, required: bool):
    if key not in row:
        if required:
            raise _err(field, "required key missing")
        return None
    v = row[key]
    if not isinstance(v, str) or not _SHA256_RE.match(v):
        raise _err(field, f"expected a lowercase sha256 hex digest, got {v!r}")
    return v


def _path(row: Mapping, key: str, field: str, *, required: bool):
    if key not in row:
        if required:
            raise _err(field, "required key missing")
        return None
    v = row[key]
    if not isinstance(v, str) or not v.strip():
        raise _err(field, f"expected a non-empty path, got {v!r}")
    if "\\" in v:
        # A native-Windows `str(Path)` interpolated into a shell string
        # parses its backslashes as escapes; producers emit `as_posix`.
        raise _err(field, f"path must be posix-form, got {v!r}")
    return v


def _member(row: Mapping, key: str, field: str, allowed: frozenset[str]):
    v = row.get(key)
    if v not in allowed:
        raise _err(field, f"{v!r} is outside {sorted(allowed)}")
    return v


@dataclass(frozen=True)
class ManifestRow:
    ticker: str
    row_kind: str
    instrument_type: str
    bundle_status: str
    raw: Mapping[str, Any]

    @property
    def entry_eligibility(self):
        return self.raw.get("entry_eligibility")

    @property
    def analysis_readiness(self):
        return self.raw.get("analysis_readiness")

    @property
    def merit_recommendation(self):
        return self.raw.get("merit_recommendation")

    @property
    def avg_volume_shares(self):
        return self.raw.get("avg_volume_shares")


@dataclass(frozen=True)
class EtfManifest:
    rows: Mapping[str, ManifestRow]
    raw: Mapping[str, Any]

    def row(self, ticker: str) -> Optional[ManifestRow]:
        return self.rows.get(ticker)


def _validate_row(ticker: str, row: Any) -> ManifestRow:
    fp = f"rows.{ticker}"
    if not isinstance(row, dict):
        raise _err(fp, f"expected an object, got {type(row).__name__}")

    row_kind = _member(row, "row_kind", f"{fp}.row_kind", ROW_KINDS)
    unknown = sorted(set(row) - _ALLOWED_KEYS[row_kind], key=repr)
    if unknown:
        raise _err(fp, f"{row_kind} row carries unknown key(s) {unknown}")

    if row.get("ticker") != ticker:
        raise _err(fp, f"row key {ticker!r} disagrees with ticker "
                       f"{row.get('ticker')!r}")

    instrument = _member(row, "instrument_type", f"{fp}.instrument_type",
                         INSTRUMENT_TYPES)
    bundle = _member(row, "bundle_status", f"{fp}.bundle_status",
                     BUNDLE_STATUSES)

    def expect_instrument(want: str):
        if instrument != want:
            raise _err(f"{fp}.instrument_type",
                       f"{row_kind} row must be {want!r}, got {instrument!r}")

    def expect_bundle(want):
        allowed = {want} if isinstance(want, str) else set(want)
        if bundle not in allowed:
            raise _err(f"{fp}.bundle_status",
                       f"{row_kind} row must be in {sorted(allowed)}, got "
                       f"{bundle!r}")

    if row_kind == "stock":
        expect_instrument("equity")
        # `identity_sha256` is deliberately absent, not merely optional: the
        # stock path never reads one, and requiring it would make every stock
        # buy depend on the ETF identity pipeline being healthy.
        if "identity_sha256" in row:
            raise _err(fp, "a stock row must not carry identity_sha256")

    elif row_kind == "etf_unresolved":
        expect_instrument("unknown")
        expect_bundle("absent")
        _member(row, "resolution_status", f"{fp}.resolution_status",
                RESOLUTION_STATUSES)
        if "identity_sha256" in row:
            raise _err(fp, "an unresolved row has no identity to hash")

    elif row_kind == "etf_unavailable":
        expect_instrument("etf")
        expect_bundle(("absent", "unloadable"))
        _sha(row, "identity_sha256", f"{fp}.identity_sha256", required=True)
        _member(row, "reason", f"{fp}.reason", UNAVAILABLE_REASONS)

    else:  # etf_thesis / etf_refusal
        expect_instrument("etf")
        expect_bundle("loaded")
        _sha(row, "identity_sha256", f"{fp}.identity_sha256", required=True)
        _sha(row, "thesis_sha256", f"{fp}.thesis_sha256", required=True)
        _sha(row, "profile_sha256", f"{fp}.profile_sha256", required=True)
        _path(row, "thesis_path", f"{fp}.thesis_path", required=True)
        _member(row, "entry_eligibility", f"{fp}.entry_eligibility",
                ENTRY_ELIGIBILITIES)
        _member(row, "analysis_readiness", f"{fp}.analysis_readiness",
                ANALYSIS_READINESS)

        volume = _require(row, "avg_volume_shares", f"{fp}.avg_volume_shares")
        if volume is not None and not (_finite(volume) and volume > 0):
            raise _err(f"{fp}.avg_volume_shares",
                       f"expected a positive number or null, got {volume!r}")
        as_of = _require(row, "avg_volume_shares_as_of",
                         f"{fp}.avg_volume_shares_as_of")
        if as_of is not None and (not isinstance(as_of, str)
                                  or not _TIMESTAMP_RE.match(as_of)):
            raise _err(f"{fp}.avg_volume_shares_as_of",
                       f"expected an ISO timestamp or null, got {as_of!r}")
        if (volume is None) != (as_of is None):
            # The pair travels together: a volume with no vintage cannot be
            # aged, and a vintage with no volume measures nothing.
            raise _err(f"{fp}.avg_volume_shares",
                       "avg_volume_shares and avg_volume_shares_as_of must "
                       "both be present or both be null")

        # `profile_retrieved_at` GATES ENTRY (P.ENTRY_PERMITTED reads it for
        # the 90-day profile-age leg), so it cannot be the one field on this
        # row nobody types. An int or a garbage string here reached the gate
        # and was judged there instead of refused at the boundary.
        retrieved = row.get("profile_retrieved_at")
        if retrieved is not None and (not isinstance(retrieved, str)
                                      or not _TIMESTAMP_RE.match(retrieved)):
            raise _err(f"{fp}.profile_retrieved_at",
                       f"expected an ISO timestamp or null, got {retrieved!r}")
        reviewed = row.get("approval_reviewed_on")
        if reviewed is not None and (not isinstance(reviewed, str)
                                     or not _ISO_DATE_RE.match(reviewed)):
            raise _err(f"{fp}.approval_reviewed_on",
                       f"expected an ISO date or null, got {reviewed!r}")

        market_sha = _sha(row, "market_snapshot_sha256",
                          f"{fp}.market_snapshot_sha256", required=False)
        if market_sha is not None:
            _path(row, "market_path", f"{fp}.market_path", required=True)
        elif "market_path" in row:
            raise _err(f"{fp}.market_path",
                       "a market path without its hash names a file nothing "
                       "binds")

        if row_kind == "etf_thesis":
            if "refusal_kind" in row:
                raise _err(fp, "a thesis row must not carry refusal_kind")
            if market_sha is None:
                raise _err(f"{fp}.market_snapshot_sha256",
                           "required on a thesis row")
            _member(row, "merit_recommendation",
                    f"{fp}.merit_recommendation", MERITS)
            context = _require(row, "decision_context", f"{fp}.decision_context")
            if not isinstance(context, dict):
                raise _err(f"{fp}.decision_context",
                           f"expected an object, got {type(context).__name__}")
            missing = [f for f in _THESIS_CONTEXT_FIELDS if f not in context]
            if missing:
                raise _err(f"{fp}.decision_context",
                           f"missing field(s) {missing}")
        else:  # etf_refusal
            refusal_kind = _member(row, "refusal_kind", f"{fp}.refusal_kind",
                                   REFUSAL_KINDS)
            if refusal_kind == "analysis_unavailable" and market_sha is None:
                raise _err(f"{fp}.market_snapshot_sha256",
                           "required when refusal_kind is analysis_unavailable")
            if "merit_recommendation" in row:
                raise _err(fp, "a refusal row carries no merit")
            context = row.get("decision_context")
            if context is not None:
                if not isinstance(context, dict):
                    raise _err(f"{fp}.decision_context",
                               f"expected an object, got "
                               f"{type(context).__name__}")
                missing = [f for f in _REFUSAL_CONTEXT_FIELDS
                           if f not in context]
                if missing:
                    raise _err(f"{fp}.decision_context",
                               f"missing field(s) {missing}")

    return ManifestRow(ticker=ticker, row_kind=row_kind,
                       instrument_type=instrument, bundle_status=bundle,
                       raw=row)


def validate_etf_manifest(raw: Any) -> EtfManifest:
    if not isinstance(raw, dict):
        raise _err("<root>", f"expected object, got {type(raw).__name__}")
    rows_raw = raw.get("rows")
    if not isinstance(rows_raw, dict):
        raise _err("rows", f"expected object, got {type(rows_raw).__name__}")
    rows = {t: _validate_row(t, r) for t, r in rows_raw.items()}
    return EtfManifest(rows=rows, raw=raw)


def load_etf_manifest(path: str | Path) -> EtfManifest:
    path = Path(path)
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _err("<file>", f"unreadable: {exc}") from exc
    return validate_etf_manifest(raw)
