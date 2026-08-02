"""Typed contract for `adr_correction.json` with DL3c §3.8.0 dispatch.

Plus invariant 8 enforcement at load time: if cert basis is
`usd_converted` AND the artifact carries an annual-period marker
(`period == "annual"`/"yearly"`), raise SchemaError. This is a
post-hoc catch — the producer (`scripts.adr.correct`) should have
already fail-closed; this loader guards against producer drift.

Two adr/correct subcommands write artifacts whose loaders share this
shape:
  - adr-valuation  → `correction_status` field ("applied" | "skipped")
  - adr-eps-check  → `check_status` field

Both subcommands may also emit an error envelope (status="error") with
no cert.

Consumers: scripts.assemble, downstream ADR-aware prompts.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from scripts.schemas.currency_conversion import CurrencyConversion
from scripts.schemas.dl3c_dispatch import Dl3cMode, dispatch_dl3c_mode
from scripts.schemas.errors import SchemaError

_ARTIFACT = "adr_correction.json"

# Invariant 8: USD-converted artifacts MUST be quarterly-aligned. Annual
# rows + usd_converted is the documented fail-close path
# (fx_unsupported_annual_path).
_ANNUAL_PERIOD_VOCAB = frozenset({"annual", "yearly"})


@dataclass(frozen=True)
class AdrCorrectionDoc:
    status: str
    currency_conversion: CurrencyConversion   # synthesized for usd_native; loaded for usd_converted
    dl3c_mode: Dl3cMode
    correction_status: Optional[str] = None   # adr-valuation output
    check_status: Optional[str] = None        # adr-eps-check output
    # True iff this is the fetch.py:write_adr_anchor classification anchor
    # (not a DL3c cert artifact). Consumers MUST exclude it from DL3c-gated
    # processing — it shares the adr_correction.json path but carries no
    # cert and is not part of the FX-conversion mode set. See
    # `_is_frozen_anchor` + scripts.assemble._load_dl3c_gated_artifacts.
    is_frozen_anchor: bool = False


def _optional_str(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SchemaError(
            _ARTIFACT,
            field,
            f"must be non-empty string or omitted; got {value!r}",
        )
    return value


def adr_ratio_correction_required(data_dir: Path | str) -> bool:
    """True iff the sibling ``adr_correction.json`` frozen anchor marks this
    ticker as needing ADR-ratio correction.

    Consumed by ``scripts.extract_fcf`` / ``scripts.historical_multiples`` to
    FAIL-CLOSE: those producers compute per-ORDINARY-share metrics
    (total / outstanding_shares) but the price is per-ADR, so for a
    ratio-correction ADR (ADR ratio != 1:1) the per-share-vs-price comparison is
    a unit mismatch (codex Loop review F). Currency repair does NOT fix this —
    it is an independent ADR-units problem.

    Anchor states:
      - MISSING file → False. Legacy pre-anchor report dirs and minimal test
        fixtures never carried one; fetch.py now writes an anchor on every
        fetch (static-fallback when the H-block is skipped), so in-pipeline
        dirs always have it.
      - Present, frozen-anchor shape (bool ``needs_ratio_correction``) →
        that bool.
      - Present, VALID DL3c adr-correct artifact (legitimately shares this
        path) → False (not a classification anchor; guard inert, as before).
        Validity is decided by ``load_adr_correction`` — a merely
        DL3c-SHAPED dict (e.g. ``{"_dl3c_version": 99}`` or
        ``{"status": null}``) fail-closes; a key-presence heuristic here
        would let corrupt state disarm the guard (codex cold-round 6).
      - Present but UNREADABLE or matching NO known shape → True
        (fail-close). A produced-but-broken anchor is a pipeline fault;
        silently treating it as non-ADR would disable the ratio guard
        exactly when upstream state is corrupt (producer-consumer §4).
    """
    p = Path(data_dir) / "adr_correction.json"
    if not p.exists():
        return False
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return True
    if isinstance(d, dict):
        # Honor the classification flag ONLY on the strict frozen-anchor
        # signature — a mixed-shape dict like {"needs_ratio_correction":
        # false, "_dl3c_version": 99} matches NEITHER legit shape and must
        # not disarm the guard via a bare bool key (codex cold-round 7).
        if _is_frozen_anchor(d):
            return d["needs_ratio_correction"]
        try:
            load_adr_correction(p)
        except (OSError, ValueError):
            return True
        # A loader-VALID adr-correct artifact still blocks non-ADR-aware
        # per-share math when it says the ticker needs correction:
        # extract_fcf / historical_multiples recompute from raw statements
        # and do NOT consume the artifact's corrected metrics, so
        # "correction applied over there" is not "safe to divide per-ADR
        # price by per-ordinary-share values here" (codex cold-round 9).
        if bool(d.get("needs_correction")):
            return True
        # needs_correction=false only proves the SNAPSHOT cross-check
        # passed (provider EPS may already be ADR-adjusted) — it says
        # nothing about the statements' outstanding_shares basis this
        # guard protects. A known non-1:1 deposit ratio keeps the guard
        # armed regardless (codex cold-round 10/11).
        ratio = d.get("adr_ratio")
        if (isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
                and math.isfinite(ratio) and ratio > 0 and ratio != 1):
            return True
        return False
    return True


def _is_frozen_anchor(data: dict) -> bool:
    """Recognize the `fetch.py:write_adr_anchor` classification anchor.

    The score-business fetch stage writes a classification-only anchor
    (`classify_ticker` output: `filing_type` / `needs_ratio_correction` /
    `data_quality_tier` [+ optional `corrected_pe`/`corrected_eps`/
    `adr_ratio`]) to the SAME `data/adr_correction.json` path the DL3c
    dispatch reads. It predates DL3c, carries NO currency cert by
    construction, and has NO `status` — it is NOT the adr-valuation /
    adr-eps DL3c artifact this loader was built for.

    The signature is tight: it requires the two classify_ticker fields AND
    the ABSENCE of every DL3c / adr-correct field. A genuinely-malformed
    DL3c artifact (partial `_dl3c_version` / cert / status / correction_status)
    therefore still fail-closes through the normal `status` gate below.
    """
    return (
        "status" not in data
        and "_dl3c_version" not in data
        and "currency_conversion" not in data
        and "correction_status" not in data
        and "check_status" not in data
        and isinstance(data.get("data_quality_tier"), str)
        and isinstance(data.get("needs_ratio_correction"), bool)
    )


def _check_invariant_8(data: dict, mode: Dl3cMode) -> None:
    """Reject usd_converted + annual-period root marker.

    Producer-side enforcement is the primary defense (adr/correct.py
    fail-closes with `fx_unsupported_annual_path`). This loader catches
    producer drift.
    """
    if mode != "post_dl3c_usd_converted":
        return
    period = data.get("period")
    if isinstance(period, str) and period.lower() in _ANNUAL_PERIOD_VOCAB:
        raise SchemaError(
            _ARTIFACT,
            "period",
            f"usd_converted artifact must be quarterly-aligned; got "
            f"period={period!r} (invariant 8 — producer should have "
            f"fail-closed with fx_unsupported_annual_path)",
        )


def load_adr_correction(path: Path | str) -> AdrCorrectionDoc:
    """Loads adr_correction.json with DL3c dispatch contract (§3.8.0).

    Legacy artifacts (no `_dl3c_version`) are accepted with a synthesized
    usd_native cert. Post-DL3c artifacts are strictly validated, and
    invariant 8 (no annual + usd_converted) is enforced.
    """
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(
            _ARTIFACT, "<file>", f"failed to read/parse {p}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise SchemaError(
            _ARTIFACT,
            "<root>",
            f"must be a JSON object; got {type(data).__name__}",
        )

    mode, cc = dispatch_dl3c_mode(data, artifact=_ARTIFACT)

    # Frozen-anchor carve-out: fetch.py writes a status-less classification
    # anchor to this path during the score-business fetch stage. It is a
    # no-cert non-DL3c artifact (mode=legacy_pre_dl3c, usd_native synth cert),
    # NOT a malformed DL3c artifact. Return it so assemble's DL3c dispatch
    # treats it as contributing no currency cert instead of fail-closing.
    if _is_frozen_anchor(data):
        return AdrCorrectionDoc(
            status="frozen_anchor",
            currency_conversion=cc,
            dl3c_mode=mode,
            is_frozen_anchor=True,
        )

    status = data.get("status")
    if not isinstance(status, str) or not status:
        raise SchemaError(
            _ARTIFACT, "status", f"must be non-empty string; got {status!r}"
        )

    _check_invariant_8(data, mode)

    return AdrCorrectionDoc(
        status=status,
        currency_conversion=cc,
        dl3c_mode=mode,
        correction_status=_optional_str(
            data.get("correction_status"), "correction_status"
        ),
        check_status=_optional_str(data.get("check_status"), "check_status"),
    )
