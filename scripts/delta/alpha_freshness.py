"""events_freshness derivation for alpha_scan.json.

Alpha Phase 1 reads events.json, which may be fresh or reused from a
prior thesis run (up to 7 days old — ceiling_7d gate). The alpha_scan
output must stamp freshness status so readers can judge how current
the insider / analyst / macro signals driving a candidate are.

Pure function. No I/O. No LLM. Safe to unit-test.

Derivation rules:
  - events_meta.reuse_meta absent → status="fresh",
    events_as_of = date(events_meta.generated_at), reused_from=None,
    days_stale = 0
  - events_meta.reuse_meta.reused_from present → status="reused",
    events_as_of = reused_from (preserved chain anchor across reuses),
    reused_from = same, days_stale = days between today_et and anchor

Returns a dict matching the alpha_scan.json events_freshness schema.
Raises ValueError on malformed meta — callers should only invoke with
validated events.json content. (Alpha orchestration is downstream of
synthesis, which already depends on a well-formed events.json.)
"""

from __future__ import annotations

import datetime
from typing import Optional

from scripts.delta.probe import _safe_normalize_to_et_date


def derive_events_freshness(
    events_meta: dict,
    today_et: datetime.date,
) -> dict:
    """Compute the events_freshness block for alpha_scan.json.

    Args:
        events_meta: The events.json `meta` block (not the whole doc).
        today_et: The ET calendar date of this alpha scan run.

    Returns:
        {
          "status": "fresh" | "reused",
          "events_as_of": "YYYY-MM-DD",
          "reused_from": None | "YYYY-MM-DD",
          "days_stale": int  (>= 0)
        }

    Raises:
        ValueError: if events_meta is not a dict, or if neither
            reuse_meta.reused_from nor generated_at yields a
            normalizable date. Alpha runs downstream of synthesis, so
            a malformed events.json meta is an orchestration bug
            worth surfacing loudly (not silently "fresh").
    """
    if not isinstance(events_meta, dict):
        raise ValueError(
            f"events_meta must be a dict, got {type(events_meta).__name__}"
        )

    reuse_meta = events_meta.get("reuse_meta")
    reused_from_raw = None
    if isinstance(reuse_meta, dict):
        reused_from_raw = reuse_meta.get("reused_from")

    if reused_from_raw:
        anchor = _safe_normalize_to_et_date(reused_from_raw)
        if anchor is None:
            raise ValueError(
                f"events_meta.reuse_meta.reused_from not normalizable: "
                f"{reused_from_raw!r}"
            )
        days_stale = _days_between(anchor, today_et)
        return {
            "status": "reused",
            "events_as_of": anchor,
            "reused_from": anchor,
            "days_stale": days_stale,
        }

    generated_at = events_meta.get("generated_at")
    if not generated_at:
        raise ValueError(
            "events_meta missing both reuse_meta.reused_from AND "
            "generated_at — cannot determine freshness"
        )
    anchor = _safe_normalize_to_et_date(generated_at)
    if anchor is None:
        raise ValueError(
            f"events_meta.generated_at not normalizable: {generated_at!r}"
        )
    return {
        "status": "fresh",
        "events_as_of": anchor,
        "reused_from": None,
        "days_stale": 0,
    }


def _days_between(iso_date: str, today_et: datetime.date) -> int:
    """Non-negative day delta between today_et and the anchor.

    Negative result (anchor in future — clock skew, test fixture, etc.)
    clamps to 0. Alpha doesn't care about future-dated events; staleness
    is a one-sided metric.
    """
    anchor = datetime.datetime.strptime(iso_date, "%Y-%m-%d").date()
    delta = (today_et - anchor).days
    return max(delta, 0)
