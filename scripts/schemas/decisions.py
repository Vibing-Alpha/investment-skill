"""Typed contract for `decisions.json` written by scripts.portfolio_log.

Closes the audit gap surfaced 2026-05-22: decisions.json is read by
prior-run review (`portfolio_log review` subcommand) and may cascade
into future tracking, but had no typed loader. Schema drift between
write+read would silently corrupt downstream analysis.

This loader is **consumer-driven**: it validates only the fields that
downstream consumers actually use, not every key portfolio_log writes.
Extra keys are allowed (don't strip them; the schema is forward-
compatible).

Public API:
    validate_decisions(data: Mapping) -> Decisions   # in-memory
    load_decisions(path: str | Path) -> Decisions    # I/O + validate

Enum source-of-truth: `DECISION_ACTIONS` / `ORDER_ACTIONS` / `ORDER_TYPES`
are duplicated from `scripts/portfolio_log.py` (the producer). A
parity test in `tests/test_schemas_decisions.py` asserts they match,
so divergence is caught at CI time rather than at runtime in production.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from scripts.schemas.errors import SchemaError


_ARTIFACT = "decisions"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.TZ+]+$")

# Mirrored from scripts/portfolio_log.py. Keep in sync — parity test
# asserts these are identical.
DECISION_ACTIONS = frozenset({"exit", "reduce", "hold", "add", "buy", "skip"})
ORDER_ACTIONS = frozenset({"sell", "buy"})
# Order types — empirically observed in decisions.json across portfolio
# runs. Constraint kept loose (validated only by membership when present).
ORDER_TYPES = frozenset({
    "market", "limit", "stop_limit", "stop", "moc", "loc", "gtc",
    "stop_market",
})


__all__ = [
    "Decisions",
    "Decision",
    "Order",
    "DECISION_ACTIONS",
    "ORDER_ACTIONS",
    "ORDER_TYPES",
    "validate_decisions",
    "load_decisions",
]


@dataclass(frozen=True)
class Decision:
    ticker: str
    action: str  # DECISION_ACTIONS
    rationale: str
    principle_cited: str
    # Optional fields with consumer-relevant defaults; loaders preserve
    # them when present but do not require them.
    target_weight_pct: float | None = None
    invalidation_trigger: str | None = None


@dataclass(frozen=True)
class Order:
    sequence: int
    ticker: str
    action: str  # ORDER_ACTIONS — buy / sell
    type: str   # ORDER_TYPES — market / limit / etc.
    shares: float  # may be fractional via /portfolio interface
    linked_decision: str  # ticker that this order executes
    limit_price: float | None = None
    duration: str | None = None  # GTC / DAY / etc.


@dataclass(frozen=True)
class Decisions:
    """Top-level decisions.json envelope.

    `extras` carries the rich set of additional fields portfolio_log
    writes (portfolio_before, macro, stress_test, follow_ups,
    principle_audit, notes, user_confirmation, execution_outcomes) so
    consumers that DO need those have access without bypassing the loader.
    """
    run_id: str
    date: str
    status: str
    decisions: tuple[Decision, ...]
    orders_proposed: tuple[Order, ...]
    extras: Mapping[str, Any]  # forward-compat passthrough


def _require(cond: bool, field: str, message: str) -> None:
    if not cond:
        raise SchemaError(_ARTIFACT, field, message)


def _require_str(d: Mapping[str, Any], key: str, *, path: str,
                 allow_empty: bool = False) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise SchemaError(_ARTIFACT, f"{path}.{key}",
                          f"must be str, got {type(v).__name__}")
    if not allow_empty and not v.strip():
        raise SchemaError(_ARTIFACT, f"{path}.{key}", "must be non-empty")
    return v


def _opt_float(d: Mapping[str, Any], key: str, *, path: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool):  # bool is subclass of int; reject explicitly
        raise SchemaError(_ARTIFACT, f"{path}.{key}",
                          "bool not accepted as float")
    if not isinstance(v, (int, float)):
        raise SchemaError(_ARTIFACT, f"{path}.{key}",
                          f"must be number or null, got {type(v).__name__}")
    return float(v)


def _opt_str(d: Mapping[str, Any], key: str, *, path: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise SchemaError(_ARTIFACT, f"{path}.{key}",
                          f"must be str or null, got {type(v).__name__}")
    return v


def _validate_decision(d: Mapping[str, Any], idx: int) -> Decision:
    path = f"decisions[{idx}]"
    _require(isinstance(d, Mapping), path, f"must be dict, got {type(d).__name__}")

    ticker = _require_str(d, "ticker", path=path)
    _require(_TICKER_RE.match(ticker) is not None,
             f"{path}.ticker", f"{ticker!r} must match {_TICKER_RE.pattern}")

    action = _require_str(d, "action", path=path)
    _require(action in DECISION_ACTIONS,
             f"{path}.action", f"{action!r} not in {sorted(DECISION_ACTIONS)}")

    rationale = _require_str(d, "rationale", path=path)
    principle_cited = _require_str(d, "principle_cited", path=path)

    target_w = _opt_float(d, "target_weight_pct", path=path)
    if target_w is not None:
        _require(0.0 <= target_w <= 100.0,
                 f"{path}.target_weight_pct",
                 f"must be in [0, 100], got {target_w}")
    invalidation = _opt_str(d, "invalidation_trigger", path=path)

    return Decision(
        ticker=ticker, action=action, rationale=rationale,
        principle_cited=principle_cited,
        target_weight_pct=target_w, invalidation_trigger=invalidation,
    )


def _validate_order(d: Mapping[str, Any], idx: int) -> Order:
    path = f"orders_proposed[{idx}]"
    _require(isinstance(d, Mapping), path, f"must be dict, got {type(d).__name__}")

    sequence = d.get("sequence")
    _require(isinstance(sequence, int) and not isinstance(sequence, bool),
             f"{path}.sequence",
             f"must be int, got {type(sequence).__name__}")

    ticker = _require_str(d, "ticker", path=path)
    _require(_TICKER_RE.match(ticker) is not None,
             f"{path}.ticker", f"{ticker!r} must match {_TICKER_RE.pattern}")

    action = _require_str(d, "action", path=path)
    _require(action in ORDER_ACTIONS,
             f"{path}.action", f"{action!r} not in {sorted(ORDER_ACTIONS)}")

    order_type = _require_str(d, "type", path=path)
    _require(order_type in ORDER_TYPES,
             f"{path}.type", f"{order_type!r} not in {sorted(ORDER_TYPES)}")

    shares = d.get("shares")
    if isinstance(shares, bool) or not isinstance(shares, (int, float)):
        raise SchemaError(_ARTIFACT, f"{path}.shares",
                          f"must be number, got {type(shares).__name__}")
    _require(shares > 0, f"{path}.shares",
             f"must be positive, got {shares}")

    linked = _require_str(d, "linked_decision", path=path)

    limit_price = _opt_float(d, "limit_price", path=path)
    if limit_price is not None:
        _require(limit_price > 0,
                 f"{path}.limit_price",
                 f"must be positive when set, got {limit_price}")

    duration = _opt_str(d, "duration", path=path)

    return Order(
        sequence=sequence, ticker=ticker, action=action,
        type=order_type, shares=float(shares),
        linked_decision=linked,
        limit_price=limit_price, duration=duration,
    )


def validate_decisions(data: Mapping[str, Any]) -> Decisions:
    """In-memory validation. Raises SchemaError on contract violation."""
    _require(isinstance(data, Mapping), "<root>", "top-level must be mapping")

    run_id = _require_str(data, "run_id", path="<root>")
    _require(_RUN_ID_RE.match(run_id) is not None,
             "run_id", f"{run_id!r} must match {_RUN_ID_RE.pattern}")
    date_s = _require_str(data, "date", path="<root>")
    _require(_DATE_RE.match(date_s) is not None,
             "date", f"{date_s!r} must be YYYY-MM-DD")
    status = _require_str(data, "status", path="<root>")

    raw_decisions = data.get("decisions", [])
    _require(isinstance(raw_decisions, list),
             "decisions", f"must be list, got {type(raw_decisions).__name__}")
    decisions = tuple(_validate_decision(d, i) for i, d in enumerate(raw_decisions))

    raw_orders = data.get("orders_proposed", [])
    _require(isinstance(raw_orders, list),
             "orders_proposed",
             f"must be list, got {type(raw_orders).__name__}")
    orders = tuple(_validate_order(o, i) for i, o in enumerate(raw_orders))

    # orders.linked_decision must reference a ticker in decisions[].
    # Convention observed in real fixtures: bare ticker (e.g. "NVDA") OR
    # "<TICKER>.<action>" suffix form (e.g. "NOK.reduce") when the same
    # ticker has multiple linkable decisions. We accept both.
    #
    # Class-share tickers (BRK.B, BF.B, etc.) themselves contain a dot, so
    # we cannot use a left-greedy split. Strategy: prefer exact match
    # against the decision ticker set; if no exact match, try the longest
    # prefix that ends at a `.` boundary and matches a decision ticker.
    # This handles both:
    #   - "NOK.reduce"   → exact match fails → strip suffix → "NOK" hit
    #   - "BRK.B"        → exact match hits
    #   - "BRK.B.exit"   → exact match fails → strip last suffix → "BRK.B" hit
    decision_tickers = {d.ticker for d in decisions}
    for i, o in enumerate(orders):
        link = o.linked_decision
        resolved: str | None = None
        if link in decision_tickers:
            resolved = link
        else:
            # Strip trailing ".<suffix>" segments from right to left until we
            # find a decision ticker. Stop after at most 3 strips — a real
            # ticker has at most one embedded dot (class shares).
            candidate = link
            for _ in range(3):
                dot = candidate.rfind(".")
                if dot <= 0:
                    break
                candidate = candidate[:dot]
                if candidate in decision_tickers:
                    resolved = candidate
                    break
        _require(
            resolved is not None,
            f"orders_proposed[{i}].linked_decision",
            f"{link!r} does not reference any ticker in decisions[] "
            f"(tried exact match + right-strip suffix; known tickers: "
            f"{sorted(decision_tickers)})",
        )

    # Pass-through everything else for forward-compat.
    extras = {k: v for k, v in data.items()
              if k not in {"run_id", "date", "status",
                           "decisions", "orders_proposed"}}

    return Decisions(
        run_id=run_id, date=date_s, status=status,
        decisions=decisions, orders_proposed=orders,
        extras=extras,
    )


def load_decisions(path: str | Path) -> Decisions:
    """I/O wrapper: read JSON from disk then validate."""
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise SchemaError(_ARTIFACT, str(p), f"failed to load: {e}") from e
    return validate_decisions(data)
