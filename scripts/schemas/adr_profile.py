"""Typed contract for `data/adr_profile.json` written by fetch.py Mode A.

Single SoT for ADR identity (replaces 15 fetch.py local-bool sites + 4
ADR function call sites + 4 CLI command surfaces per DL3a §3.1).
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from scripts.schemas.errors import SchemaError

DetectionConfidence = Literal["high", "low", "none"]

_AS_OF_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_ARTIFACT = "adr_profile.json"

PrimaryAdrSignal = Literal[
    "filings_20f_or_6k", "category_string", "explicit_flag",
    "portfolio_yaml", "foreign_domicile", "none_observed",
    # ISS-020: static-table fallback when the FD API doesn't classify
    # a known-foreign ticker as ADR (Financial Datasets returns
    # is_adr=False for MRAAY/NOK/TTDKY even though they are 20-F
    # filers). Forces is_adr=True via the curated
    # KNOWN_ADR_CLASSIFICATIONS table at scripts/constants.py.
    "known_adr_table",
]

_PRIMARY_SIGNALS = frozenset({
    "filings_20f_or_6k", "category_string", "explicit_flag",
    "portfolio_yaml", "foreign_domicile", "none_observed",
    "known_adr_table",
})

_DETECTION_CONFIDENCE = frozenset({"high", "low", "none"})


@dataclass(frozen=True)
class AdrProfile:
    ticker: str
    is_adr: bool
    primary_signal: PrimaryAdrSignal
    secondary_signals: tuple[PrimaryAdrSignal, ...]
    detection_confidence: DetectionConfidence
    native_ticker: Optional[str]
    requires_20f: bool
    as_of_date: str
    source_ticker: str
    provenance: tuple[str, ...]


def _err(field: str, message: str) -> SchemaError:
    return SchemaError(_ARTIFACT, field, message)


def _require_bool(obj: dict, key: str) -> bool:
    if key not in obj:
        raise _err(key, "required key missing")
    v = obj[key]
    if not isinstance(v, bool):
        raise _err(key, f"must be bool, got {type(v).__name__}")
    return v


def _require_nonempty_str(obj: dict, key: str) -> str:
    if key not in obj:
        raise _err(key, "required key missing")
    v = obj[key]
    if not isinstance(v, str) or not v.strip():
        raise _err(key, "must be non-empty str")
    return v.strip().upper() if key in {"ticker", "source_ticker"} else v.strip()


def _validate_secondary_signals(value: Any, primary: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _err("secondary_signals", "must be JSON list")
    for item in value:
        if not isinstance(item, str) or item not in _PRIMARY_SIGNALS:
            raise _err("secondary_signals",
                       f"element {item!r} not in PrimaryAdrSignal enum")
    if len(set(value)) != len(value):
        raise _err("secondary_signals", "duplicate entries forbidden")
    if primary in value:
        raise _err("secondary_signals",
                   f"primary {primary!r} must not appear in secondary_signals")
    if "none_observed" in value:
        raise _err("secondary_signals",
                   "'none_observed' is absence-marker; never a secondary signal")
    return tuple(value)


def _validate_provenance(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _err("provenance", "must be JSON list")
    if not value:
        raise _err("provenance", "must be non-empty list")
    for item in value:
        if not isinstance(item, str):
            raise _err("provenance", "elements must be str")
    return tuple(value)


def load_adr_profile(
    path: Path,
    *,
    expected_ticker: Optional[str] = None,
) -> AdrProfile:
    """Load and validate adr_profile.json from a file path.

    Loader divergence from ``scripts/schemas/macro_rates.py``: this loader
    DOES wrap FileNotFoundError / OSError / json.JSONDecodeError as
    SchemaError per spec §3.1 FIX-2.4. This is deliberate — §5 test 10
    CLI symmetry tests require ``except ValueError`` to catch
    ``--adr-profile <nonexistent-path>`` and ``--adr-profile <malformed.json>``
    uniformly. macro_rates.py does NOT need this because no CLI consumes
    macro_rates.json via a file flag.

    expected_ticker default-None is for unit-test/debug fixtures only;
    production CLIs MUST pass it (§3.5 per-call-site source matrix).
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw_text = f.read()
    except (OSError, IsADirectoryError) as e:
        raise _err("<file_io>", str(e)) from e
    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise _err("<json_decode>", str(e)) from e
    if not isinstance(doc, dict):
        raise _err("<root>", "JSON root must be object")

    ticker = _require_nonempty_str(doc, "ticker")
    is_adr = _require_bool(doc, "is_adr")

    primary_signal = doc.get("primary_signal")
    if primary_signal not in _PRIMARY_SIGNALS:
        raise _err("primary_signal",
                   f"value {primary_signal!r} not in {sorted(_PRIMARY_SIGNALS)}")

    if "secondary_signals" not in doc:
        raise _err("secondary_signals", "required key missing")
    secondary_signals = _validate_secondary_signals(doc["secondary_signals"], primary_signal)

    detection_confidence = doc.get("detection_confidence")
    if detection_confidence not in _DETECTION_CONFIDENCE:
        raise _err("detection_confidence",
                   f"value {detection_confidence!r} not in {sorted(_DETECTION_CONFIDENCE)}")

    if "native_ticker" not in doc:
        raise _err("native_ticker", "required key missing")
    nt = doc["native_ticker"]
    if nt is None:
        native_ticker: Optional[str] = None
    elif isinstance(nt, str):
        # Strip whitespace; treat blank as None (producer also normalizes via
        # `(strip() or None)` — loader enforces same invariant for hand-edited
        # artifacts).
        nt_stripped = nt.strip()
        native_ticker = nt_stripped or None
    else:
        raise _err("native_ticker", "must be Optional[str]")

    requires_20f = _require_bool(doc, "requires_20f")

    if "as_of_date" not in doc:
        raise _err("as_of_date", "required key missing")
    aod = doc["as_of_date"]
    if not isinstance(aod, str):
        raise _err("as_of_date", "must be str (YYYY-MM-DD)")
    # Python 3.11+ `date.fromisoformat` relaxed the parser to accept basic
    # format (`20260513`) and week dates (`2026-W19-3`). Spec §3.1 requires
    # YYYY-MM-DD shape — pre-screen with regex to keep behavior consistent
    # across 3.10/3.11+.
    if not _AS_OF_DATE_RE.fullmatch(aod):
        raise _err("as_of_date", f"must match YYYY-MM-DD, got {aod!r}")
    try:
        datetime.date.fromisoformat(aod)
    except ValueError as e:
        raise _err("as_of_date", f"calendar invalid: {e}") from e

    source_ticker = _require_nonempty_str(doc, "source_ticker")

    if "provenance" not in doc:
        raise _err("provenance", "required key missing")
    provenance = _validate_provenance(doc["provenance"])

    profile = AdrProfile(
        ticker=ticker,
        is_adr=is_adr,
        primary_signal=primary_signal,
        secondary_signals=secondary_signals,
        detection_confidence=detection_confidence,
        native_ticker=native_ticker,
        requires_20f=requires_20f,
        as_of_date=aod,
        source_ticker=source_ticker,
        provenance=provenance,
    )

    if expected_ticker is not None:
        expected_norm = expected_ticker.strip().upper()
        if profile.ticker != expected_norm or profile.source_ticker != expected_norm:
            raise _err(
                "ticker",
                f"Loaded profile ticker={profile.ticker!r} / "
                f"source_ticker={profile.source_ticker!r} does not match "
                f"expected_ticker={expected_norm!r} "
                f"(likely misplaced or stale profile; check --adr-profile path)."
            )

    # Stale-profile WARNING (ISS-023). Stderr log only.
    try:
        today = datetime.date.today()
        derived = datetime.date.fromisoformat(aod)
        if (today - derived).days > 30:
            import sys
            print(
                f"WARNING adr_profile.json: as_of_date={aod} is "
                f"{(today - derived).days}d old; refresh via full fetch.",
                file=sys.stderr,
            )
    except Exception:  # fail-open-ok: staleness warning only; malformed date handled by prior validator
        pass

    return profile
