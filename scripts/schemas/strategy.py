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
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_canonical_iso_date(value):
    """Parse `YYYY-MM-DD` and nothing else. Returns a `date`, or None.

    `date.fromisoformat` is broader than the contract: since 3.11 it also
    accepts the separator-less basic form (`20260801`) and ISO week dates
    (`2026-W32-1`, which is a Monday three days from the date it resembles).
    The compiled artifact's own regex admits only the dashed form, so a
    consumer using bare `fromisoformat` would honour spellings the loader
    rejects — two layers disagreeing about which approvals exist.

    ONE implementation, shared by this loader and
    `scripts.etf.policy.etf_policy_approval`. Authoring input is separately
    allowed to be more tolerant: `compile_strategy` accepts what the owner
    typed and emits the canonical form.
    """
    import datetime as _dt
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


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
class EtfPolicy:
    """The owner's authorization to buy ETFs. `approved_equity_etfs` maps a
    canonical ticker to the ISO date the owner reviewed it — an authorization
    record, not a provider verification, and it expires."""
    version: int
    allow_non_leveraged_equity_etfs: bool
    approved_equity_etfs: dict          # canonical ticker -> reviewed_on (ISO)
    merit_admission: tuple[str, ...]


@dataclass(frozen=True)
class CompiledStrategy:
    source_hash: str
    hard_constraints: HardConstraints
    soft_principles: tuple[str, ...]
    principle_notes: dict = field(default_factory=dict)
    # ETF layer. Absent in every artifact compiled before 2026-08-22, so the
    # defaults must be the ones that withhold authorization.
    principles_source: str = "default"
    etf_policy: Optional[EtfPolicy] = None
    etf_entry_enabled: bool = False


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


# V.MERIT restricted to the two admitting values. `watch` / `pass` / `avoid`
# are merit verdicts that do not admit a buy, so allowing them into
# `merit_admission` would let the policy authorize entry on a verdict that
# means "do not enter".
_MERIT_ADMISSION_ALLOWED = ("strong_add", "add")

_PRINCIPLES_SOURCES = frozenset({"explicit", "default"})


def _parse_etf_policy(raw) -> Optional[EtfPolicy]:
    """Parse the compiled `etf_policy` block.

    Strict, like the rest of this loader: this is the artifact a hand edit
    reaches, and every field here gates whether a buy order may be proposed.
    """
    if raw is None:
        return None
    fp = "etf_policy"
    if not isinstance(raw, dict):
        raise SchemaError(_ARTIFACT, fp,
                          f"expected mapping, got {type(raw).__name__}")

    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SchemaError(_ARTIFACT, f"{fp}.version",
                          f"expected int, got {type(version).__name__}")

    allow = raw.get("allow_non_leveraged_equity_etfs")
    if not isinstance(allow, bool):
        raise SchemaError(_ARTIFACT, f"{fp}.allow_non_leveraged_equity_etfs",
                          f"expected bool, got {type(allow).__name__}")

    approved_raw = raw.get("approved_equity_etfs")
    if approved_raw is None:
        approved_raw = {}
    if not isinstance(approved_raw, dict):
        raise SchemaError(_ARTIFACT, f"{fp}.approved_equity_etfs",
                          f"expected mapping, got {type(approved_raw).__name__}")
    # On disk each approval is `{reviewed_on: <ISO date>}` — the compiler keeps
    # the source shape so "contains exactly reviewed_on" stays checkable in the
    # artifact. Consumers only ever need the date, so flatten it here.
    approved: dict[str, str] = {}
    for ticker, approval in approved_raw.items():
        fpt = f"{fp}.approved_equity_etfs.{ticker}"
        if not isinstance(ticker, str) or not ticker:
            raise SchemaError(_ARTIFACT, f"{fp}.approved_equity_etfs",
                              f"ticker key must be a non-empty string, got {ticker!r}")
        # The COMPILED artifact carries canonical keys only. `compile_strategy`
        # canonicalizes what the owner typed and refuses collisions, so a
        # non-canonical key here means the artifact was hand-edited — and the
        # contract is explicit that only an exact canonical key may be current.
        # Tolerating it would put two spellings of one approval in play.
        from scripts.cli_utils import normalize_ticker
        try:
            canonical = normalize_ticker(ticker)
        except (ValueError, TypeError) as exc:
            raise SchemaError(_ARTIFACT, f"{fp}.approved_equity_etfs",
                              f"{ticker!r} is not a usable ticker: {exc}") from exc
        if canonical != ticker:
            raise SchemaError(
                _ARTIFACT, f"{fp}.approved_equity_etfs",
                f"key {ticker!r} is not canonical (expected {canonical!r}); "
                f"recompile rather than hand-editing the compiled artifact")
        if not isinstance(approval, dict):
            raise SchemaError(_ARTIFACT, fpt,
                              f"expected mapping, got {type(approval).__name__}")
        if set(approval.keys()) != {"reviewed_on"}:
            raise SchemaError(_ARTIFACT, fpt,
                              f"must contain exactly `reviewed_on`, got "
                              f"{sorted(map(str, approval))}")
        reviewed_on = approval["reviewed_on"]
        if not isinstance(reviewed_on, str) or not _ISO_DATE_RE.match(reviewed_on):
            raise SchemaError(_ARTIFACT, f"{fpt}.reviewed_on",
                              f"expected ISO date string, got {reviewed_on!r}")
        approved[ticker] = reviewed_on

    merit_raw = raw.get("merit_admission")
    if not isinstance(merit_raw, list) or not merit_raw:
        raise SchemaError(_ARTIFACT, f"{fp}.merit_admission",
                          f"expected a non-empty list, got {merit_raw!r}")
    for m in merit_raw:
        if m not in _MERIT_ADMISSION_ALLOWED:
            raise SchemaError(
                _ARTIFACT, f"{fp}.merit_admission",
                f"{m!r} is outside {list(_MERIT_ADMISSION_ALLOWED)}")
    if len(set(merit_raw)) != len(merit_raw):
        raise SchemaError(_ARTIFACT, f"{fp}.merit_admission",
                          f"contains duplicates: {merit_raw}")

    return EtfPolicy(
        version=version,
        allow_non_leveraged_equity_etfs=allow,
        approved_equity_etfs=approved,
        merit_admission=tuple(merit_raw),
    )


def derive_etf_entry_enabled(principles_source: str,
                             policy: Optional[EtfPolicy]) -> bool:
    """F.COMPILED.ETF_ENTRY_ENABLED — all three legs must hold.

    One function, called by the compiler that writes the bit and by the loader
    that re-checks it. Two implementations of this would diverge, and the
    direction of the divergence that matters is the one that says `true`.
    """
    return (principles_source == "explicit"
            and policy is not None
            and policy.allow_non_leveraged_equity_etfs is True)


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

    principles_source = raw.get("principles_source")
    if principles_source is None:
        # Pre-ETF artifact. `default` is the reading that withholds ETF entry,
        # which is the correct answer for a file compiled before the owner
        # could have authorized anything.
        principles_source = "default"
    if principles_source not in _PRINCIPLES_SOURCES:
        raise SchemaError(_ARTIFACT, "principles_source",
                          f"{principles_source!r} is outside "
                          f"{sorted(_PRINCIPLES_SOURCES)}")

    etf_policy = _parse_etf_policy(raw.get("etf_policy"))

    # Re-derive rather than trust. A hand-edited `etf_entry_enabled: true`
    # beside `allow_non_leveraged_equity_etfs: false` is an authorization
    # nobody granted, and this artifact is what the validator reads before
    # letting an ETF buy through.
    derived = derive_etf_entry_enabled(principles_source, etf_policy)
    stored = raw.get("etf_entry_enabled")
    if stored is not None:
        if not isinstance(stored, bool):
            raise SchemaError(_ARTIFACT, "etf_entry_enabled",
                              f"expected bool, got {type(stored).__name__}")
        if stored != derived:
            raise SchemaError(
                _ARTIFACT, "etf_entry_enabled",
                f"stored {stored} disagrees with the value derived from "
                f"principles_source={principles_source!r} and etf_policy "
                f"(derived {derived}). Recompile; do not hand-edit the "
                f"compiled artifact.")

    return CompiledStrategy(
        source_hash=source_hash,
        hard_constraints=hc,
        soft_principles=tuple(soft),
        principle_notes=dict(notes),
        principles_source=principles_source,
        etf_policy=etf_policy,
        etf_entry_enabled=derived,
    )


# ---------------------------------------------------------------------------
# Canonical policy hash — the SINGLE formula, deliberately PURE
# ---------------------------------------------------------------------------
#
# F4 grew out of two things: the hash covered only `principles`, so a policy
# value written under `risk:` was a cache hit and the decision log kept
# attesting the superseded policy; and the formula was hand-matched in two
# places (SKILL.md's inline snippet and portfolio_log._verify_source_hash),
# which is a drift surface by construction.
#
# This function is the one formula. It **validates nothing and refuses
# nothing** — that separation is load-bearing. Validation lives in
# scripts.compile_strategy, which owns one path; the hash is needed by every
# path, including the orchestration fallback for a null/absent `principles`.
# Welding them together forces that fallback to choose between computing a
# DIFFERENT hash (after which the logger refuses a fresh install permanently)
# and running validation it is explicitly not meant to run.

# `etf_policy` joins the scope for the reason `risk` did: a policy input the
# operator edits that the hash does not cover produces a cache hit, and the
# decision log then attests an authorization the owner has already replaced.
# Here that would mean logging a buy against an ETF approval since revoked.
_HASHED_POLICY_KEYS = ("principles", "principle_notes", "risk", "etf_policy")


def canonical_policy_hash(source) -> str:
    """SHA-256 over the decision policy in `strategy.yaml`.

    Covers `principles`, `principle_notes`, `risk` and `etf_policy` — every
    policy input the OPERATOR can edit. Absent and present-null normalize alike, so the two
    spellings of "not configured" cannot force a spurious recompile, and
    mapping keys are sorted so YAML key order does not change the hash.

    ⚠ **Scope.** When `principles` is empty the effective soft policy is the
    canonical default text in `rules/portfolio-safety.md`, which this hash does
    NOT cover. That text is a version-controlled repo file, not user config, so
    it cannot drift per-install — but the hash does not attest it, and saying
    it covers "every authoring input" would overclaim.

    Totality matters because `portfolio_log` hashes the RAW `yaml.safe_load`
    output before any validation runs: `default=` never applies to mapping
    KEYS, so an unquoted date key beside a string key raised `TypeError` from
    `sort_keys`, and a `!!set` iterates in hash-randomised order — one policy
    hashed three different ways under three interpreter seeds. Both are fatal
    across a process boundary: the config compiles, then the logger refuses it
    as stale, permanently.

    The supported contract is narrower than that (`compile_strategy` requires
    `principle_notes` to be a string→string mapping), so the coercions below
    only ever fire on input the compiler would refuse anyway. They exist to
    keep this function TOTAL for its pre-validation caller, not to bless exotic
    policy.
    """
    import hashlib
    import json

    doc = source if isinstance(source, dict) else {}
    payload = {k: _stable_for_hash(doc.get(k)) for k in _HASHED_POLICY_KEYS}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")
    ).hexdigest()


def _stable_for_hash(value):
    """Coerce to a JSON-dumpable, order-stable shape. See the note above."""
    if isinstance(value, dict):
        return {str(k): _stable_for_hash(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(repr(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_stable_for_hash(v) for v in value]
    return value
