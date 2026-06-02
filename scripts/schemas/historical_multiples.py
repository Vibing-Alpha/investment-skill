"""Typed contract for `historical_multiples.json` with DL3c §3.8.0 dispatch.

Most of `historical_multiples.json` content is producer-specific (P/E,
P/S, P/B, EV/EBITDA arrays). This loader validates the structural shape
+ dispatches the DL3c cert; per-multiple validation lives in the
producer (scripts.historical_multiples).

Consumers: scripts.assemble, downstream valuation prompts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from scripts.schemas.currency_conversion import CurrencyConversion
from scripts.schemas.dl3c_dispatch import Dl3cMode, dispatch_dl3c_mode
from scripts.schemas.errors import SchemaError

_ARTIFACT = "historical_multiples.json"


@dataclass(frozen=True)
class HistoricalMultiplesDoc:
    status: str
    currency_conversion: CurrencyConversion   # synthesized for usd_native; loaded for usd_converted
    dl3c_mode: Dl3cMode


def load_historical_multiples(path: Path | str) -> HistoricalMultiplesDoc:
    """Loads historical_multiples.json with DL3c dispatch contract (§3.8.0).

    Legacy artifacts (no `_dl3c_version`) are accepted with a synthesized
    usd_native cert. Post-DL3c artifacts are strictly validated by
    `dispatch_dl3c_mode`.
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

    status = data.get("status")
    if not isinstance(status, str) or not status:
        raise SchemaError(
            _ARTIFACT, "status", f"must be non-empty string; got {status!r}"
        )

    return HistoricalMultiplesDoc(
        status=status,
        currency_conversion=cc,
        dl3c_mode=mode,
    )
