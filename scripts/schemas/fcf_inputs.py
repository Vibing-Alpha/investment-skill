"""Typed contract for `fcf_inputs.json` with DL3c §3.8.0 dispatch.

Single SoT for consumers that read FCF inputs. The dispatch surfaces
the artifact's DL3c mode so downstream code knows whether
`currency_conversion` is synthesized (legacy / usd_native) or loaded
from disk (usd_converted).

Consumers: scripts.reverse_dcf, scripts.assemble.
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

_ARTIFACT = "fcf_inputs.json"


@dataclass(frozen=True)
class FcfInputsDoc:
    status: str
    fcf_per_share: Optional[float]
    discount_rate: Optional[float]
    currency_conversion: CurrencyConversion   # synthesized for usd_native; loaded for usd_converted
    dl3c_mode: Dl3cMode


def _optional_finite(value: object, field: str) -> Optional[float]:
    """Validate that `value` is None or a finite numeric.

    Rejects bool (subclass of int but not a meaningful numeric here),
    str, NaN, and Inf. Returns float(value) on success.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(
            _ARTIFACT,
            field,
            f"must be numeric or None; got {type(value).__name__}={value!r}",
        )
    f = float(value)
    if not math.isfinite(f):
        raise SchemaError(
            _ARTIFACT, field, f"must be finite (NaN/Inf rejected); got {value!r}"
        )
    return f


def load_fcf_inputs(path: Path | str) -> FcfInputsDoc:
    """Loads fcf_inputs.json with DL3c dispatch contract (§3.8.0).

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

    return FcfInputsDoc(
        status=status,
        fcf_per_share=_optional_finite(data.get("fcf_per_share"), "fcf_per_share"),
        discount_rate=_optional_finite(data.get("discount_rate"), "discount_rate"),
        currency_conversion=cc,
        dl3c_mode=mode,
    )
