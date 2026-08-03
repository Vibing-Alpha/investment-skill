"""Events reuse / rerun audit writer.

Replaces two inline `python3 -c` blocks previously in investment-thesis
SKILL.md Step 4 (~37 lines combined):

1. `reuse` path: load prior events.json, prune past catalysts, rewrite
   stale-date fields, stamp reuse_meta, write events.json + .events_reuse.json.
2. `rerun` path: write .events_reuse.json with status=fresh and the
   gates_failed audit trail.

This script handles BOTH paths via the --decision-kind flag, so SKILL.md
collapses to a single branch in a case statement.

Usage:
    python3 -m scripts.thesis.reuse_events \\
        --decision-kind reuse \\
        --canonical-anchor 2026-04-15 \\
        --gates-passed news_classifier_clean,estimates_hash_unchanged,... \\
        --report-dir reports/AAPL/20260520 \\
        --prior-thesis-dir reports/AAPL/20260415

    python3 -m scripts.thesis.reuse_events \\
        --decision-kind rerun \\
        --gates-failed news_classifier_material,thesis_freshness_ceiling \\
        --override-reason "" \\
        --report-dir reports/AAPL/20260520
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

# Round-21 F2: ONE definition of the fresh-events structural floor —
# the sections downstream consumers read (probe: catalyst_calendar;
# thesis synthesis: signals/bias/density/macro_context/confidence).
# Used by the SKILL's post-dispatch gate AND the fresh-destination
# guard below (an aborted producer leftover must not earn the guard).
EVENTS_STRUCTURAL_FLOOR = (
    "catalyst_calendar", "macro_context", "signals", "event_density",
    "overall_event_bias", "confidence",
)


def _split_csv(s: str) -> list[str]:
    """Split comma-separated list, dropping empties."""
    return [g for g in (s or "").split(",") if g]


def write_reuse_path(
    report_dir: Path,
    prior_thesis_dir: Path,
    canonical_anchor: str,
    gates_passed: list[str],
    now_utc: datetime.datetime | None = None,
) -> None:
    """Reuse path: copy prior events.json forward with prune + stale-date
    rewrite + reuse_meta stamping. Writes:
    - $REPORT_DIR/events.json
    - $REPORT_DIR/.events_reuse.json (audit for run_meta)

    Fresh-destination guard (third cold round — the events analog of
    copy_dimension_scores' probe-4B guard): a FRESH same-session
    events.json (present, parseable, meta WITHOUT reuse_meta — the events
    agent never stamps reuse_meta) is never overwritten with an older
    prior copy. A second same-session thesis invocation that passes the
    reuse gates would otherwise resolve yesterday's dir and silently
    downgrade today's fresh events. The audit records status "fresh"
    (truthful: the file on disk IS fresh) with an explanatory note.
    """
    from scripts.delta.calendar import today_et
    from scripts.delta.copy_data import rewrite_stale_date_fields
    from scripts.delta.prune_catalysts import prune_past
    from scripts.cli_utils import write_output as _atomic_write_guard

    dst_path = report_dir / "events.json"
    if dst_path.exists():
        try:
            cur = json.loads(dst_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cur = None  # unreadable dst → replace with the reuse copy
        # Round-21 F2: the guard must not protect an ABORTED producer
        # leftover — a stamped-but-partial fresh artifact (failed the
        # SKILL's structural floor, run aborted, file remained) would
        # otherwise be recorded as a successful "fresh" events result.
        # Only a floor-passing fresh artifact earns the guard; a partial
        # one is replaced by the prior VALID copy below.
        _floor_missing = (
            [k for k in EVENTS_STRUCTURAL_FLOOR if k not in cur]
            if isinstance(cur, dict) else []
        )
        if (isinstance(cur, dict)
                and isinstance(cur.get("meta"), dict)
                and "reuse_meta" not in cur["meta"]
                and _floor_missing):
            print(
                f"[reuse_events] WARN: destination events.json looks like an "
                f"ABORTED fresh output (missing sections {_floor_missing}) — "
                f"replacing it with the prior VALID copy instead of "
                f"recording it as fresh.",
                file=sys.stderr,
            )
        if (isinstance(cur, dict)
                and isinstance(cur.get("meta"), dict)
                and "reuse_meta" not in cur["meta"]
                and not _floor_missing):
            print(
                "[reuse_events] SKIP: destination events.json is a fresh "
                "same-session output (no reuse_meta) — not overwriting with "
                "a prior-run copy (fresh-destination guard).",
                file=sys.stderr,
            )
            _atomic_write_guard(
                {
                    "status": "fresh",
                    "gates_failed": [],
                    "override_reason": None,
                    "note": ("same-session fresh events kept — reuse skipped "
                             "by the fresh-destination guard"),
                },
                str(report_dir / ".events_reuse.json"),
            )
            return

    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now_utc.isoformat().replace("+00:00", "Z")

    prior_path = prior_thesis_dir / "events.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    # Round-24 F3: a BINDING-MARKED prior that fails the structural floor
    # is an aborted producer leftover sitting behind a stale
    # completed=true (the abort replaced the valid artifact, the run
    # stopped, the resolver still served the dir). Reusing it propagates
    # missing event evidence for another 7 days — refuse; the
    # orchestrator falls back to the rerun branch. Pre-binding (unmarked)
    # priors stay legacy-lenient (design: reused pre-binding artifacts).
    if prior.get("_websearch_binding_version") is not None:
        _prior_missing = [k for k in EVENTS_STRUCTURAL_FLOOR
                          if k not in prior]
        if _prior_missing:
            print(
                f"[reuse_events] REFUSED: prior events.json at {prior_path} "
                f"is binding-marked but missing sections {_prior_missing} — "
                f"an aborted partial artifact must not be reused. Fall back "
                f"to the RERUN branch (fresh events agent).",
                file=sys.stderr,
            )
            raise SystemExit(2)
    prior["catalyst_calendar"] = prune_past(
        prior.get("catalyst_calendar", []), today_et(),
    )
    rewrite_stale_date_fields(prior, today_et().isoformat())

    prior.setdefault("meta", {})
    prior["meta"]["reuse_meta"] = {
        "reused_from": canonical_anchor,  # chain-stable anchor
        "gates_passed": gates_passed,
        "copied_at": now_iso,
    }
    # meta.generated_at is preserved (provenance — never rewritten)

    # F14 (codex review cycle 2): atomic writes via write_output (temp +
    # os.replace) so crash mid-write doesn't leave a partial events.json
    # or a torn audit record that downstream consumers ingest.
    from scripts.cli_utils import write_output as _atomic_write
    _atomic_write(prior, str(report_dir / "events.json"))

    _atomic_write(
        {
            "status": "reused",
            "from_date": canonical_anchor,
            "gates_passed": gates_passed,
            "gates_failed": [],
            "copied_at": now_iso,
        },
        str(report_dir / ".events_reuse.json"),
    )


def write_rerun_audit(
    report_dir: Path,
    gates_failed: list[str],
    override_reason: str | None,
) -> None:
    """Rerun path: events agent will write events.json fresh; we only
    record the audit trail explaining WHY gates failed."""
    # F14 (codex review cycle 2): atomic write — see reuse path comment.
    from scripts.cli_utils import write_output as _atomic_write
    _atomic_write(
        {
            "status": "fresh",
            "gates_failed": gates_failed,
            "override_reason": override_reason or None,
        },
        str(report_dir / ".events_reuse.json"),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--decision-kind", required=True, choices=("reuse", "rerun"))
    p.add_argument("--report-dir", required=True)
    p.add_argument("--prior-thesis-dir", default="",
                   help="Required when --decision-kind=reuse")
    p.add_argument("--canonical-anchor", default="",
                   help="Required when --decision-kind=reuse")
    p.add_argument("--gates-passed", default="",
                   help="Comma-separated list; reuse path only")
    p.add_argument("--gates-failed", default="",
                   help="Comma-separated list; rerun path only")
    p.add_argument("--override-reason", default="",
                   help="rerun path only")
    args = p.parse_args(argv)

    report_dir = Path(args.report_dir)
    if not report_dir.is_dir():
        print(f"FATAL: --report-dir {report_dir} not found", file=sys.stderr)
        return 1

    try:
        if args.decision_kind == "reuse":
            if not args.prior_thesis_dir or not args.canonical_anchor:
                print(
                    "FATAL: --decision-kind=reuse requires --prior-thesis-dir "
                    "and --canonical-anchor",
                    file=sys.stderr,
                )
                return 2
            write_reuse_path(
                report_dir=report_dir,
                prior_thesis_dir=Path(args.prior_thesis_dir),
                canonical_anchor=args.canonical_anchor,
                gates_passed=_split_csv(args.gates_passed),
            )
        else:  # rerun
            write_rerun_audit(
                report_dir=report_dir,
                gates_failed=_split_csv(args.gates_failed),
                override_reason=args.override_reason or None,
            )
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"FATAL: reuse_events failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    print(str(report_dir / ".events_reuse.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
