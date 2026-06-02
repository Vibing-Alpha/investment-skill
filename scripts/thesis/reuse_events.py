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
    """
    from scripts.delta.calendar import today_et
    from scripts.delta.copy_data import rewrite_stale_date_fields
    from scripts.delta.prune_catalysts import prune_past

    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now_utc.isoformat().replace("+00:00", "Z")

    prior_path = prior_thesis_dir / "events.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
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
