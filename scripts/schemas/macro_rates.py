"""Typed contract for `09_macro_rates.json` written by fetch.py.

Consumer-loose shape. `current_rates` and per-entry `bank`/`rate` are
required (all consumers depend on them). `fed_history`,
`fed_history_count`, per-entry `name` and `date` are optional — if
present, they are validated; if absent, they default to None / empty.

Rationale: legacy test fixtures and early reports carry only
`current_rates` without the historical series. Requiring the full
shape here would blow up ~34 existing test fixtures. Strict
producer-side validation is a separate (deferred) concern.

Consumers: scripts.extract_fcf (WACC derivation) and scripts.macro
(_load_rates_from_disk fallback).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from scripts.schemas.errors import SchemaError


_ARTIFACT = "09_macro_rates.json"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_RATE_MIN_PCT = 0.0
_RATE_MAX_PCT = 25.0


def rate_in_percent_domain(v) -> bool:
    """True iff v is a plausible central-bank policy rate in PERCENT units.

    Round-20 F1: shared with the LIVE fetch path in scripts/macro.py —
    the [0, 25] unit gate previously existed only on the disk-cache load,
    so a provider unit drift (basis points: rate=375.0) rode the live
    path into macro.json as fed_funds under rates_status PASSED.
    """
    return (
        isinstance(v, (int, float)) and not isinstance(v, bool)
        and math.isfinite(v) and _RATE_MIN_PCT <= v <= _RATE_MAX_PCT
    )


@dataclass(frozen=True)
class CentralBankRate:
    bank: str
    rate: float                     # percent, e.g. 3.625 means 3.625%
    name: Optional[str] = None      # optional — legacy fixtures omit it
    date: Optional[str] = None      # optional — YYYY-MM-DD if present


@dataclass(frozen=True)
class MacroRatesDoc:
    current_rates: tuple[CentralBankRate, ...]
    fed_history: tuple[CentralBankRate, ...] = ()
    fed_history_count: Optional[int] = None
    # Probe 1D: 10Y treasury yield in PERCENT (e.g. 4.25), fetched from
    # ^TNX. Optional — legacy artifacts predate it; extract_fcf falls back
    # to the FED overnight policy rate (with a proxy warning) when absent.
    us_10y: Optional[float] = None

    def find_current_rate(self, bank: str) -> Optional[CentralBankRate]:
        for r in self.current_rates:
            if r.bank == bank:
                return r
        return None


def _require_key(obj: dict, key: str, field_prefix: str) -> object:
    if key not in obj:
        raise SchemaError(_ARTIFACT, f"{field_prefix}{key}",
                          "required key missing")
    return obj[key]


def _parse_rate_entry(entry: object, idx_field: str) -> CentralBankRate:
    if not isinstance(entry, dict):
        raise SchemaError(_ARTIFACT, idx_field, "expected object")

    bank = _require_key(entry, "bank", f"{idx_field}.")
    if not isinstance(bank, str) or not bank:
        raise SchemaError(_ARTIFACT, f"{idx_field}.bank",
                          f"expected non-empty str, got {type(bank).__name__}")

    rate = _require_key(entry, "rate", f"{idx_field}.")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise SchemaError(_ARTIFACT, f"{idx_field}.rate",
                          f"expected number, got {type(rate).__name__}")
    if not math.isfinite(rate):
        raise SchemaError(_ARTIFACT, f"{idx_field}.rate",
                          f"must be finite, got {rate}")
    if rate < _RATE_MIN_PCT or rate > _RATE_MAX_PCT:
        raise SchemaError(_ARTIFACT, f"{idx_field}.rate",
                          f"{rate} outside [{_RATE_MIN_PCT}, {_RATE_MAX_PCT}] "
                          "— likely a unit-scale bug (decimal passed as "
                          "percent, or percent passed as bps)")

    name = entry.get("name")
    if name is not None and not isinstance(name, str):
        raise SchemaError(_ARTIFACT, f"{idx_field}.name",
                          f"expected str or absent, got {type(name).__name__}")

    date = entry.get("date")
    if date is not None:
        if not isinstance(date, str) or not _DATE_RE.match(date):
            raise SchemaError(_ARTIFACT, f"{idx_field}.date",
                              f"expected YYYY-MM-DD or absent, got {date!r}")

    return CentralBankRate(bank=bank, rate=float(rate), name=name, date=date)


def load_macro_rates(path) -> MacroRatesDoc:
    """Load and validate a `09_macro_rates.json` file.

    Raises:
        FileNotFoundError: path does not exist.
        json.JSONDecodeError: file is not valid JSON.
        SchemaError: JSON is well-formed but violates the contract.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise SchemaError(_ARTIFACT, "<root>",
                          f"expected object, got {type(raw).__name__}")

    current_raw = _require_key(raw, "current_rates", "")
    if not isinstance(current_raw, list):
        raise SchemaError(_ARTIFACT, "current_rates",
                          f"expected list, got {type(current_raw).__name__}")
    current = tuple(
        _parse_rate_entry(e, f"current_rates[{i}]")
        for i, e in enumerate(current_raw)
    )

    history_raw = raw.get("fed_history", [])
    if not isinstance(history_raw, list):
        raise SchemaError(_ARTIFACT, "fed_history",
                          f"expected list or absent, got {type(history_raw).__name__}")
    history = tuple(
        _parse_rate_entry(e, f"fed_history[{i}]")
        for i, e in enumerate(history_raw)
    )

    count_raw = raw.get("fed_history_count")
    if count_raw is not None:
        if isinstance(count_raw, bool) or not isinstance(count_raw, int):
            raise SchemaError(_ARTIFACT, "fed_history_count",
                              f"expected int or absent, got {type(count_raw).__name__}")
        if count_raw != len(history):
            raise SchemaError(_ARTIFACT, "fed_history_count",
                              f"{count_raw} disagrees with len(fed_history)="
                              f"{len(history)}")

    us_10y_raw = raw.get("us_10y")
    if us_10y_raw is not None:
        if isinstance(us_10y_raw, bool) or not isinstance(us_10y_raw, (int, float)):
            raise SchemaError(_ARTIFACT, "us_10y",
                              f"expected number or absent, got {type(us_10y_raw).__name__}")
        if not math.isfinite(us_10y_raw):
            raise SchemaError(_ARTIFACT, "us_10y",
                              f"must be finite, got {us_10y_raw}")
        # Strictly positive (cold-round finding): the ^TNX producer requires
        # (0, 25] — a 0.0 here is a zeroed/corrupt value, and treating it as
        # a real risk-free would silently understate the CAPM discount rate.
        # (Bank policy rates above legitimately allow 0.0 — SNB.)
        if us_10y_raw <= 0 or us_10y_raw > _RATE_MAX_PCT:
            raise SchemaError(_ARTIFACT, "us_10y",
                              f"{us_10y_raw} outside (0, {_RATE_MAX_PCT}] — "
                              "zero/negative treasury yield is corrupt data, "
                              "or a unit-scale bug (decimal-as-percent / bps)")
        us_10y_raw = float(us_10y_raw)

    return MacroRatesDoc(
        current_rates=current,
        fed_history=history,
        fed_history_count=count_raw,
        us_10y=us_10y_raw,
    )
