#!/usr/bin/env python3
"""Bidirectional plugin↔clone version-skew WARNING (Plan B Task 6).

Runtime context: in Cowork the INSTALLED PLUGIN's skill body executes, but
its Step-0 prelude ``cd``s into the CLONE (the heavy repo, which has its own
``VERSION`` file) and all scripts run from there. The two halves can drift —
the user pulls the clone but forgets to update the plugin (old orchestration
bodies drive new scripts: the post-update common miss), or updates the
plugin but not the clone. Either direction silently runs a mismatched
pipeline, so a business skill compares the expected-min literal BAKED into
its own body against the clone's ``VERSION`` and warns in BOTH directions.

Contract (warning-only — NEVER a gate):
- ``--expected-min`` is the literal baked into the materialized plugin
  SKILL.md by copy-mode ``distribute sync`` (publish-bake). In the dev repo
  the materialization is a symlink to the canonical source, so the
  placeholder ``__BAKED_AT_SYNC__`` arrives unexpanded → silent skip
  (dev / CC-CLI runs execute the clone's own body: tautologically
  self-consistent).
- clone ``VERSION`` <  expected-min → "clone STALE: git pull / re-run
  /stock-v7-setup".
- clone ``VERSION`` >  expected-min → "plugin STALE: update it in the Cowork
  plugin UI".
- Malformed versions, missing files, even argparse misuse → silent skip.
  ALWAYS exit 0: a version string must not block a money-path run (the
  skill line is additionally ``|| true``-guarded).

Stdlib-only. Versions are numeric dotted triples; missing parts pad to 0.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The canonical placeholder in canonical .claude/skills/*/SKILL.md bodies;
# scripts/distribute.py substitutes it with VERSION in copy-mode sync.
PLACEHOLDER = "__BAKED_AT_SYNC__"

_VERSION_RE = re.compile(r"\d+(?:\.\d+){0,2}")


def parse_version(raw: str) -> tuple[int, int, int] | None:
    """``"1.0.15"`` → ``(1, 0, 15)``; tolerate missing parts (``"1.0"`` →
    ``(1, 0, 0)``); anything else (including the placeholder) → None."""
    s = raw.strip()
    if not _VERSION_RE.fullmatch(s):
        return None
    parts = [int(p) for p in s.split(".")]
    padded = (parts + [0, 0])[:3]
    return padded[0], padded[1], padded[2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m scripts.version_skew",
        description="Warn (stderr, exit 0 always) on plugin-vs-clone version skew.",
    )
    ap.add_argument("--expected-min", required=True,
                    help="version literal baked into the plugin skill body "
                         f"(the unexpanded placeholder {PLACEHOLDER!r} skips)")
    ap.add_argument("--version-file", default="VERSION",
                    help="clone VERSION file (default: ./VERSION, cwd = clone root)")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        # fail-open-ok: warning-only helper — even CLI misuse must not gate a
        # money-path skill run (the named failure mode is a SILENT mismatch,
        # not a missed warning).
        return 0

    expected = parse_version(args.expected_min)
    if expected is None:
        return 0  # placeholder (dev/CC-CLI run) or malformed → skip silently
    try:
        clone_raw = Path(args.version_file).read_text(encoding="utf-8").strip()
    except OSError:
        return 0  # no VERSION readable → nothing to compare
    clone = parse_version(clone_raw)
    if clone is None:
        return 0

    if clone < expected:
        print(
            f"stock-v7: WARNING — version skew: clone VERSION {clone_raw} < "
            f"installed plugin {args.expected_min.strip()} (clone STALE). "
            f"In the clone run `git pull` (or re-run /stock-v7-setup) so the "
            f"scripts match the plugin's orchestration. Proceeding anyway.",
            file=sys.stderr,
        )
    elif clone > expected:
        print(
            f"stock-v7: WARNING — version skew: clone VERSION {clone_raw} > "
            f"installed plugin {args.expected_min.strip()} (plugin STALE). "
            f"Update the stock-v7-skills plugin in the Cowork plugin UI so "
            f"its orchestration matches the clone's scripts. Proceeding anyway.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
