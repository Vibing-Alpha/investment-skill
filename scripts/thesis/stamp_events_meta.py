"""Stamp the orchestrator-owned `meta.generated_at` onto events.json on the
RERUN (fresh-generation) path.

`events.json` is LLM-authored. `evaluate-events.md` asks the agent to emit
`meta.generated_at` as "your generation timestamp" — but a wall-clock
timestamp is the one thing an LLM cannot reliably produce. The observed
failure mode is the agent writing midnight-UTC (`...T00:00:00Z`), which the
downstream normalizer `scripts/delta/probe._safe_normalize_to_et_date`
(also used by `scripts/delta/alpha_freshness.derive_events_freshness`)
converts UTC→ET and so shifts to the PRIOR ET day — moving the canonical
events anchor + the ceiling_7d reuse gate a day early. A general
hallucination is not even fail-safe. `events.json` is NOT in the
anti-hallucination exemption (only `run_meta.json` is), so an LLM-invented
timestamp also violates that rule.

The orchestrator owns the run clock, so it stamps `generated_at`
authoritatively here — mirroring `scripts.thesis.stamp_thesis_meta` for
`investment_thesis.json`. Stamping with the real `datetime.now(utc)` makes
`_safe_normalize_to_et_date(generated_at)` round-trip back to `today_et()`
(both are "now" expressed as an ET date), which is exactly the correct
fresh-generation anchor.

**Rerun-path ONLY.** On the REUSE path, `scripts.thesis.reuse_events`
preserves the prior `generated_at` as the chain-stable anchor across a
reuse chain and must NOT be re-stamped — so the orchestrator simply does
not invoke this CLI on reuse. This CLI itself always overwrites, mirroring
how `stamp_thesis_meta` always sets the three required fields.

Run in SKILL.md Step 4 (rerun branch), AFTER the events agent writes
events.json and BEFORE synthesis (Step 6) / alpha freshness (Step 6.5)
consume it.

Idempotent + non-destructive: only `meta.generated_at` is set
authoritatively; every other `meta.*` field (e.g. `output_version`) and the
entire body are preserved.

Usage:
    python3 -m scripts.thesis.stamp_events_meta \\
        --report-dir reports/AAPL/20260522
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any


def stamp_events_generated_at(
    events: dict[str, Any],
    *,
    now_utc: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Return `events` with `meta.generated_at` set to the stamping time in
    UTC ISO-8601 with a `Z` suffix (`YYYY-MM-DDTHH:MM:SSZ`) — the format the
    prompt specifies and the downstream normalizers accept. Any pre-existing
    `meta.*` keys (e.g. `output_version`) and the body are preserved; `meta`
    is created if absent.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    generated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    existing = events.get("meta")
    meta: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    meta["generated_at"] = generated_at
    events["meta"] = meta
    return events


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report-dir", required=True, help="reports/{T}/{YYYYMMDD}/")
    args = p.parse_args(argv)

    events_path = Path(args.report_dir) / "events.json"
    if not events_path.is_file():
        print(f"FATAL: {events_path} not found", file=sys.stderr)
        return 1

    try:
        events = json.loads(events_path.read_text(encoding="utf-8"))
        if not isinstance(events, dict):
            print(
                f"FATAL: {events_path} top-level is "
                f"{type(events).__name__}, expected object",
                file=sys.stderr,
            )
            return 1
        # Reuse-path guard (codex review). This CLI is RERUN-ONLY: the
        # orchestrator never invokes it on the reuse path, where `generated_at`
        # is the chain-stable anchor that `reuse_events` deliberately preserves.
        # Refuse loudly rather than silently overwrite that anchor — so a
        # future wiring bug that points this at a reuse-path events.json
        # surfaces (non-zero) AND leaves the file untouched (anchor safe).
        meta_in = events.get("meta")
        if isinstance(meta_in, dict) and meta_in.get("reuse_meta"):
            print(
                f"FATAL: {events_path} carries meta.reuse_meta — this is a "
                "reuse-path artifact whose generated_at is the chain anchor; "
                "stamp_events_meta is rerun-only and refuses to overwrite it.",
                file=sys.stderr,
            )
            return 1
        stamp_events_generated_at(events)
        # Atomic write (temp + os.replace) so a crash mid-write can't leave a
        # partial events.json for synthesis to ingest — mirrors
        # reuse_events.py / stamp_thesis_meta.py.
        from scripts.cli_utils import write_output as _atomic_write
        _atomic_write(events, str(events_path))
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"FATAL: stamp_events_meta failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    print(f"stamped events generated_at={events['meta']['generated_at']}",
          file=sys.stderr)
    print(str(events_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
