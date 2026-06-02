"""Typed contract for `strategy.compiled.yaml`.

Produced by the portfolio skill's compile stage (see
.claude/skills/portfolio/SKILL.md §Step 1); consumed by scripts.validate
and scripts.portfolio_log. The compile step already coerces raw-percent
inputs (e.g. 35) to decimal fractions (0.35) via
cli_utils.normalize_percent_fraction — this schema is the belt-and-
suspenders layer that catches stale / hand-edited compiled files that
never went through the compile step.

source_hash is strict (64-hex sha256) at the loader so consumers get a
clean, unambiguous format error separate from the downstream "hash
mismatch" diagnostic. Stale-sentinel tests use `'0'*64` (well-formed
hex, guaranteed non-matching).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from scripts.schemas.errors import SchemaError


_ARTIFACT = "strategy.compiled.yaml"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HardConstraints:
    max_single_position: Optional[float] = None
    max_sector: Optional[float] = None
    min_cash: Optional[float] = None
    max_holdings: Optional[int] = None

    def to_mapping(self) -> dict:
        """Flatten to the dict shape that validate.py + portfolio_log.py
        have historically used. Explicit (not asdict()) so adding a
        non-constraint field to the dataclass later doesn't silently
        leak into the mapping API.
        """
        return {
            "max_single_position": self.max_single_position,
            "max_sector": self.max_sector,
            "min_cash": self.min_cash,
            "max_holdings": self.max_holdings,
        }


@dataclass(frozen=True)
class CompiledStrategy:
    source_hash: str
    hard_constraints: HardConstraints
    soft_principles: tuple[str, ...]
    principle_notes: dict = field(default_factory=dict)


def _parse_fraction(value, field_path: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(_ARTIFACT, field_path,
                          f"expected number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise SchemaError(_ARTIFACT, field_path,
                          f"must be finite, got {value}")
    if value < 0.0 or value > 1.0:
        raise SchemaError(_ARTIFACT, field_path,
                          f"{value} outside [0.0, 1.0] — compiled "
                          "constraints must be decimal fractions, not raw "
                          "percent. Recompile via /portfolio skill.")
    return float(value)


_KNOWN_CONSTRAINT_KEYS = frozenset({
    "max_single_position", "max_sector", "min_cash", "max_holdings",
})


def _parse_hard_constraints(raw, field_prefix: str) -> HardConstraints:
    if raw is None:
        return HardConstraints()
    if not isinstance(raw, dict):
        raise SchemaError(_ARTIFACT, field_prefix.rstrip("."),
                          f"expected mapping, got {type(raw).__name__}")

    # Fail-close on unknown keys: producer-consumer rule 1 ("Field
    # Names Are Contracts") means a typo'd key (e.g. max_single_positon)
    # must surface loudly, not silently default to None.
    unknown = set(raw.keys()) - _KNOWN_CONSTRAINT_KEYS
    if unknown:
        # sorted(unknown) would TypeError on mixed-type keys (e.g. YAML
        # with int key alongside str); sort by repr for safety.
        unknown_display = sorted(unknown, key=repr)
        raise SchemaError(
            _ARTIFACT, field_prefix.rstrip("."),
            f"unknown key(s): {unknown_display}. "
            f"Known keys: {sorted(_KNOWN_CONSTRAINT_KEYS)}. "
            "Typo or drifted contract — fix source yaml / recompile.")

    max_hold_raw = raw.get("max_holdings")
    if max_hold_raw is None:
        max_holdings = None
    elif isinstance(max_hold_raw, bool) or not isinstance(max_hold_raw, int):
        raise SchemaError(_ARTIFACT, f"{field_prefix}max_holdings",
                          f"expected int, got {type(max_hold_raw).__name__}")
    elif max_hold_raw <= 0:
        raise SchemaError(_ARTIFACT, f"{field_prefix}max_holdings",
                          f"must be positive, got {max_hold_raw}")
    else:
        max_holdings = max_hold_raw

    return HardConstraints(
        max_single_position=_parse_fraction(
            raw.get("max_single_position"),
            f"{field_prefix}max_single_position",
        ),
        max_sector=_parse_fraction(
            raw.get("max_sector"), f"{field_prefix}max_sector"),
        min_cash=_parse_fraction(
            raw.get("min_cash"), f"{field_prefix}min_cash"),
        max_holdings=max_holdings,
    )


def load_compiled_strategy(path) -> CompiledStrategy:
    """Load and validate a `strategy.compiled.yaml` file.

    Raises:
        FileNotFoundError: path does not exist.
        yaml.YAMLError: file is not valid YAML.
        SchemaError: YAML is well-formed but violates the contract.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise SchemaError(_ARTIFACT, "<root>",
                          f"expected mapping, got {type(raw).__name__}")

    if "source_hash" not in raw:
        raise SchemaError(_ARTIFACT, "source_hash", "required key missing")
    source_hash = raw["source_hash"]
    if not isinstance(source_hash, str):
        raise SchemaError(_ARTIFACT, "source_hash",
                          f"expected str, got {type(source_hash).__name__}")
    if not source_hash:
        raise SchemaError(_ARTIFACT, "source_hash", "must be non-empty")
    if not _SHA256_RE.match(source_hash):
        raise SchemaError(_ARTIFACT, "source_hash",
                          f"invalid 64-hex format, got {source_hash!r}")

    hc = _parse_hard_constraints(
        raw.get("hard_constraints"), "hard_constraints.")

    # Explicit None check (not `or []`): a YAML value of 0/""/false
    # must NOT be silently coerced to default — that defeats the typed
    # contract's fail-close goal.
    soft = raw.get("soft_principles")
    if soft is None:
        soft = []
    if not isinstance(soft, list):
        raise SchemaError(_ARTIFACT, "soft_principles",
                          f"expected list, got {type(soft).__name__}")
    for i, p in enumerate(soft):
        if not isinstance(p, str):
            raise SchemaError(_ARTIFACT, f"soft_principles[{i}]",
                              f"expected str, got {type(p).__name__}")

    notes = raw.get("principle_notes")
    if notes is None:
        notes = {}
    if not isinstance(notes, dict):
        raise SchemaError(_ARTIFACT, "principle_notes",
                          f"expected mapping, got {type(notes).__name__}")

    return CompiledStrategy(
        source_hash=source_hash,
        hard_constraints=hc,
        soft_principles=tuple(soft),
        principle_notes=dict(notes),
    )
