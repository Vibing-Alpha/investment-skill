"""Typed schema loaders for /monitor artifacts.

Loaders: load_action_plan(path) -> ActionPlan
         load_monitor_probe(path) -> MonitorProbe

Raises SchemaError(ValueError) on any structural / enum violation.
Consumer-loose on probe (per_ticker is returned as raw dicts); strict on
the load-bearing contracts: route enum, priority enum, status enum,
evidence kinds.

These are the typed INTER-MODULE contract for /monitor's artifacts — the
executable schema spec (exercised by tests/test_schemas_monitor.py) for the
first external consumer that reads action_plan.json / monitor_probe.json.
They are intentionally NOT wired into scripts.monitor's own probe→validate→
render pipeline: those are intra-module reads of self-produced, atomically
written, deterministic artifacts, and the only real trust boundary there —
the LLM router's raw plan — is guarded by scripts.monitor.validate_raw_plan,
not by a typed loader. (load_prior_evidence_ids reads PRIOR-run plans
deliberately leniently — it must tolerate schema drift across /monitor
versions and needs only evidence_refs — so routing it through the strict
load_action_plan would be a robustness regression, not an improvement.)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from scripts.schemas.errors import SchemaError

_ARTIFACT = "action_plan.json"
_ROUTES = {"/investment-thesis", "/portfolio", "/score-business", "/screen-stocks"}
_PRIORITIES = {"critical", "watch", "info"}
_STATUSES = {"new", "seen", None}


@dataclass(frozen=True)
class ActionItem:
    ticker: Optional[str]
    priority: str
    status: Optional[str]
    route: str
    reason: str
    evidence_refs: tuple


@dataclass(frozen=True)
class ActionPlan:
    run_date: str
    items: tuple
    summary: str


def load_action_plan(path) -> ActionPlan:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SchemaError(_ARTIFACT, "", f"unreadable: {e}")
    if not isinstance(data, dict):
        raise SchemaError(_ARTIFACT, "", "expected object")
    # the typed contract must match the real (always-stamped) final artifact, not silently
    # default a missing run_date/items to ""/[] — that would mask a corrupt or unstamped plan
    if not isinstance(data.get("run_date"), str) or not data.get("run_date"):
        raise SchemaError(_ARTIFACT, "run_date", "missing/invalid run_date")
    if not isinstance(data.get("summary"), str):
        raise SchemaError(_ARTIFACT, "summary", "missing/invalid summary")
    if not isinstance(data.get("items"), list):
        raise SchemaError(_ARTIFACT, "items", "missing/invalid items list")
    items = []
    for i, raw in enumerate(data["items"]):
        fp = f"items[{i}]."
        if not isinstance(raw, dict):
            raise SchemaError(_ARTIFACT, fp.rstrip("."), "expected object")
        route = raw.get("route")
        if route not in _ROUTES:
            raise SchemaError(_ARTIFACT, fp + "route", f"not a route enum: {route!r}")
        if raw.get("priority") not in _PRIORITIES:
            raise SchemaError(_ARTIFACT, fp + "priority", f"bad priority: {raw.get('priority')!r}")
        if "status" not in raw:                     # a FINAL plan must be stamped; absent != null
            raise SchemaError(_ARTIFACT, fp + "status", "missing status (final plan must be stamped)")
        if raw.get("status") not in _STATUSES:
            raise SchemaError(_ARTIFACT, fp + "status", f"bad status: {raw.get('status')!r}")
        if raw.get("ticker") is not None and not isinstance(raw.get("ticker"), str):
            raise SchemaError(_ARTIFACT, fp + "ticker", f"ticker must be null or a string: {raw.get('ticker')!r}")
        if not isinstance(raw.get("reason"), str):
            raise SchemaError(_ARTIFACT, fp + "reason", "reason must be a string")
        refs = raw.get("evidence_refs")             # must be PRESENT (a list, possibly empty); not masked
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise SchemaError(_ARTIFACT, fp + "evidence_refs", "missing/invalid evidence_refs (expected list of strings)")
        items.append(ActionItem(
            ticker=raw.get("ticker"), priority=raw["priority"], status=raw.get("status"),
            route=route, reason=raw.get("reason") or "",
            evidence_refs=tuple(refs),
        ))
    return ActionPlan(run_date=data["run_date"], items=tuple(items), summary=data["summary"])


# ---------------------------------------------------------------------------
# load_monitor_probe
# ---------------------------------------------------------------------------

_PROBE_ARTIFACT = "monitor_probe.json"
_EVIDENCE_KINDS = {"condition", "news", "catalyst", "staleness"}


@dataclass(frozen=True)
class MonitorProbe:
    run_date: str
    per_ticker: tuple   # left as raw dicts (consumer-loose); enums below are the only hard checks


def load_monitor_probe(path) -> MonitorProbe:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SchemaError(_PROBE_ARTIFACT, "", f"unreadable: {e}")
    if not isinstance(data, dict) or not data.get("run_date"):
        raise SchemaError(_PROBE_ARTIFACT, "run_date", "missing run_date")
    pts = data.get("per_ticker")
    if not isinstance(pts, list):
        raise SchemaError(_PROBE_ARTIFACT, "per_ticker", "expected list")
    for i, pt in enumerate(pts):
        if not isinstance(pt, dict):
            raise SchemaError(_PROBE_ARTIFACT, f"per_ticker[{i}]", "expected object")
        evidence = pt.get("evidence") or []
        if not isinstance(evidence, list):          # a truthy non-list must raise SchemaError, not TypeError
            raise SchemaError(_PROBE_ARTIFACT, f"per_ticker[{i}].evidence", "expected list")
        for j, e in enumerate(evidence):
            kind = e.get("kind") if isinstance(e, dict) else None
            if kind not in _EVIDENCE_KINDS:
                raise SchemaError(_PROBE_ARTIFACT, f"per_ticker[{i}].evidence[{j}].kind",
                                  f"bad evidence kind: {kind!r}")
    return MonitorProbe(run_date=data["run_date"], per_ticker=tuple(pts))
