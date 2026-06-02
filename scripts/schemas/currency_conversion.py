"""Typed contract for the `currency_conversion` certificate block embedded
in fcf_inputs.json / historical_multiples.json / adr_correction.json.

Validates the §3.1.2 schema shape.

Mirror `quarter_window.py` discipline: every check raises
SchemaError(ValueError); dataclasses are frozen; no defaults that hide
errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scripts.fx_constants import (
    FX_BASIS_USD_CONVERTED,
    FX_BASIS_USD_NATIVE,
    FX_BASIS_VOCAB,
    SUPPORTED_FX_CURRENCIES,
)
from scripts.schemas.errors import SchemaError
from scripts.schemas.fx_window import FxWindow, load_fx_window

_ARTIFACT = "currency_conversion"


@dataclass(frozen=True)
class CurrencyConversion:
    basis: str                          # "usd_native" | "usd_converted"
    source_currency: str                # ISO 4217
    fx_source: str                      # "yfinance:JPY=X" | "usd_native"
    window: Optional[FxWindow] = None   # required iff basis == "usd_converted"


def load_currency_conversion(data: dict) -> CurrencyConversion:
    """Reject:
    - basis ∉ {usd_native, usd_converted}
    - source_currency ∉ SUPPORTED_FX_CURRENCIES
    - basis == usd_converted but window missing/empty
    - basis == usd_native but source_currency != "USD"
    """
    if not isinstance(data, dict):
        raise SchemaError(_ARTIFACT, "data",
                          f"must be dict, got {type(data).__name__}")

    basis = data.get("basis")
    if not isinstance(basis, str) or basis not in FX_BASIS_VOCAB:
        raise SchemaError(_ARTIFACT, "basis",
                          f"must be one of {sorted(FX_BASIS_VOCAB)}, "
                          f"got {basis!r}")

    source_currency = data.get("source_currency")
    if not isinstance(source_currency, str) or \
            source_currency not in SUPPORTED_FX_CURRENCIES:
        raise SchemaError(_ARTIFACT, "source_currency",
                          f"must be one of {sorted(SUPPORTED_FX_CURRENCIES)}, "
                          f"got {source_currency!r}")

    fx_source = data.get("fx_source")
    if not isinstance(fx_source, str) or not fx_source:
        raise SchemaError(_ARTIFACT, "fx_source",
                          f"must be non-empty str, got {fx_source!r}")

    raw_window = data.get("window")
    loaded_window: Optional[FxWindow] = None

    if basis == FX_BASIS_USD_NATIVE:
        # usd_native semantics: source_currency must be USD, fx_source must
        # be the literal "usd_native". window may be absent.
        if source_currency != "USD":
            raise SchemaError(_ARTIFACT, "source_currency",
                              f"basis={basis!r} requires source_currency=USD, "
                              f"got {source_currency!r}")
        if fx_source != "usd_native":
            raise SchemaError(_ARTIFACT, "fx_source",
                              f"basis={basis!r} requires fx_source='usd_native', "
                              f"got {fx_source!r}")
        if raw_window is not None:
            # Tolerate empty list (legacy producers may emit []), but reject
            # populated window — usd_native must not carry FX rows.
            if isinstance(raw_window, list) and len(raw_window) == 0:
                loaded_window = None
            else:
                raise SchemaError(_ARTIFACT, "window",
                                  f"basis={basis!r} must not carry a "
                                  f"populated window; got "
                                  f"{type(raw_window).__name__} of "
                                  f"len={_safe_len(raw_window)}")
    elif basis == FX_BASIS_USD_CONVERTED:
        # post-impl loop-1 cycle-2 ISS-022: inverse guard. The usd_native
        # branch above enforces (source_currency=USD AND
        # fx_source="usd_native"). usd_converted MUST be the inverse:
        # source_currency != USD AND fx_source != "usd_native". Pre-fix
        # a hand-crafted or drift-introduced cert like {basis:
        # "usd_converted", source_currency: "USD", fx_source: "usd_native",
        # window: [{currency: "USD", ...}]} would pass schema validation
        # (USD ∈ SUPPORTED_FX_CURRENCIES) and propagate through assemble
        # claiming an FX conversion was performed when it wasn't.
        if source_currency == "USD":
            raise SchemaError(_ARTIFACT, "source_currency",
                              f"basis={basis!r} must have non-USD "
                              f"source_currency; usd_converted with "
                              f"source_currency=USD is a contradictory state "
                              f"(usd_native is the correct basis for "
                              f"USD-native data per invariant 7)")
        if fx_source == "usd_native":
            raise SchemaError(_ARTIFACT, "fx_source",
                              f"basis={basis!r} must have non-'usd_native' "
                              f"fx_source; the sentinel 'usd_native' is "
                              f"reserved for basis=usd_native artifacts")
        # post-impl loop-1 cycle-3 HIGH-3: bind fx_source to source_currency.
        # Pre-fix the loader only enforced window.source == fx_source
        # (self-consistency). A cert with source_currency="JPY" +
        # fx_source="yfinance:EUR=X" + window rows tagged EUR would pass
        # validation because the inter-row consistency held. The
        # canonical fx_source format for yfinance-sourced certs is
        # exactly `yfinance:<source_currency>=X` (set by the producer at
        # `fx_apply.build_cert_block` line 327). Anything else is a sign
        # of drift or hand-construction and must fail-close.
        expected_yf_source = f"yfinance:{source_currency}=X"
        if fx_source != expected_yf_source:
            raise SchemaError(_ARTIFACT, "fx_source",
                              f"basis={basis!r} requires fx_source="
                              f"{expected_yf_source!r} to match "
                              f"source_currency={source_currency!r}; "
                              f"got {fx_source!r}")
        if raw_window is None:
            raise SchemaError(_ARTIFACT, "window",
                              f"basis={basis!r} requires non-empty window")
        # Empty list / empty dict → reject before delegating.
        if isinstance(raw_window, list) and len(raw_window) == 0:
            raise SchemaError(_ARTIFACT, "window",
                              f"basis={basis!r} requires non-empty window; "
                              f"got empty list")
        if isinstance(raw_window, dict) and not raw_window:
            raise SchemaError(_ARTIFACT, "window",
                              f"basis={basis!r} requires non-empty window; "
                              f"got empty dict")
        loaded_window = load_fx_window(raw_window)
        # The window's currency must agree with source_currency.
        if loaded_window.currency != source_currency:
            raise SchemaError(_ARTIFACT, "window.currency",
                              f"window currency {loaded_window.currency!r} "
                              f"does not match source_currency "
                              f"{source_currency!r}")
        if loaded_window.source != fx_source:
            raise SchemaError(_ARTIFACT, "window.source",
                              f"window source {loaded_window.source!r} does "
                              f"not match fx_source {fx_source!r}")

    return CurrencyConversion(
        basis=basis,
        source_currency=source_currency,
        fx_source=fx_source,
        window=loaded_window,
    )


def _safe_len(obj: object) -> int:
    try:
        return len(obj)  # type: ignore[arg-type]
    except TypeError:
        return -1
