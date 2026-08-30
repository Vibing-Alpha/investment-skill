"""Clear a run-scoped artifact so a missed write fails LOUDLY.

CLI: ``python3 -m scripts.clear_stale PATH [PATH ...]``  (0 = cleared, 1 = refused)

Every skill guards a reused session directory the same way: remove the
artifact an agent is about to regenerate, so that if the agent fails to
write, the MISSING file fails the next gate instead of an earlier
same-session file being stamped and validated as this run's fresh output
(cold-round 7, in four skills independently).

Each of those sites spelled the guard `rm -f`. On a delete-restricted mount
— the Cowork virtiofs/FUSE mount returns `Operation not permitted` for an
EXISTING file while still allowing writes — that guard has never once fired:
the stale file stayed, silently, and the block's exit code was the only
symptom (feedback 2026-08-29 monitor ②a / portfolio #2).

So the rule is stated by OUTCOME rather than by mechanism: after this
command returns 0, the path is one of the two shapes every consumer in this
repo already reads as absent —

  * gone, or
  * present and ZERO BYTES. Consumers reject it, but by DIFFERENT routes,
    and the distinction matters when diagnosing: a `[ -s FILE ]` gate is
    false; `read_envelopes` skips a zero-byte `.json` by name-and-content
    rule; but a consumer that tests `Path.exists()` and then parses — such
    as `portfolio_log --stress-test` — does NOT see it as absent, it dies
    in `json.load` and exits non-zero. Both are fail-closed; only the
    second is loud, so do not tell an operator to expect an
    "absent" message.

Deleting is tried first and is still the normal outcome. Truncation is the
fallback, and it is VERIFIED by reading the size back, because the same
mount is documented to leave the tail of an over-written file in place — an
unverified write would report a clear that did not happen. If neither works,
the command REFUSES: a guard that cannot be honoured must stop the run, not
let it proceed believing it was.

One implementation, many callers (`.claude/rules/producer-consumer.md` §3):
seven bash spellings of this would diverge, and the two that mattered
already had.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def clear_stale(path: Path) -> str:
    """Clear one path. Returns 'absent' | 'removed' | 'truncated'.

    Raises OSError when the path can be neither removed nor emptied.
    """
    try:
        path.unlink()
        return "removed"
    except FileNotFoundError:
        # Already absent IS the goal — never an error. A first run of the day
        # legitimately has no artifact to clear.
        return "absent"
    except OSError as unlink_exc:
        try:
            # "wb" truncates. fsync so the emptied length is on the device
            # before the gate that reads it runs in a LATER shell.
            import os
            with open(path, "wb") as f:
                f.truncate(0)
                f.flush()
                os.fsync(f.fileno())
        except OSError as write_exc:
            raise OSError(
                f"{path.as_posix()}: cannot delete ({unlink_exc.strerror or unlink_exc}) "
                f"and cannot empty ({write_exc.strerror or write_exc})"
            ) from write_exc
        # VERIFIED, not assumed — see the module docstring.
        size = path.stat().st_size
        if size != 0:
            raise OSError(
                f"{path.as_posix()}: deletion was refused "
                f"({unlink_exc.strerror or unlink_exc}) and the file is still "
                f"{size} bytes after being truncated — its stale content is "
                f"intact and would be read as this run's output"
            )
        return "truncated"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m scripts.clear_stale", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)

    truncated: list[str] = []
    for raw in args.paths:
        # A path directly under `/` is what an UNSET shell variable produces:
        # `REPORT_DIR=$(failing-command)` leaves it empty, and
        # `"$REPORT_DIR/events.json"` interpolates to `/events.json`. Every
        # `REPORT_DIR=$(… allocate-bq-run …)` assignment in the skills is
        # unchecked (the house pattern), so this is reachable — and the old
        # `rm -f "/events.json"` failed silently, as did clearing an absent
        # path here, reporting a guard as honoured when it had not run at
        # all. Refuse: no artifact this tool clears ever lives at the root.
        p_raw = Path(raw)
        # An EMPTY PATH COMPONENT is the unset-variable signature, and it is
        # the general one: `"$REPORT_DIR/events.json"` with REPORT_DIR unset
        # gives `/events.json`, while monitor's
        # `REPORT_DIR="reports/monitor/$(…)"` with a failed subshell gives a
        # RELATIVE `reports/monitor//action_plan.raw.json`. `Path` silently
        # collapses the `//`, so this must be read off the RAW string before
        # it becomes a Path — the depth rule below never saw the relative
        # case, and cleared it as "already absent" while the real file sat
        # untouched (pure-fresh probe, 2026-08-29).
        if "//" in raw.replace("\\", "/").lstrip("/"):
            print(f"REFUSED: {raw!r} contains an empty path component — that "
                  f"is an unset shell variable (`\"$REPORT_DIR/…\"` or "
                  f"`\"…/$(failed-command)/…\"`). Check the command that set "
                  f"it; nothing was cleared.", file=sys.stderr)
            return 1
        # Same hazard, absolute form: `/events.json`, `/scores/forward.json`.
        # Every real artifact path is far deeper — the shallowest possible is
        # <repo>/reports/<T>/<DATE>/<file>.
        _SHALLOW = 3
        if p_raw.is_absolute() and len(p_raw.parts) - 1 < _SHALLOW:
            print(f"REFUSED: {p_raw.as_posix()} is too close to the "
                  f"filesystem root to be a run artifact — this is what an "
                  f"unset shell variable looks like (`\"$REPORT_DIR/…\"` with "
                  f"REPORT_DIR empty). Check the command that set it.",
                  file=sys.stderr)
            return 1
        try:
            outcome = clear_stale(p_raw)
        except OSError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        if outcome == "truncated":
            truncated.append(p_raw.as_posix())
    if truncated:
        # Named, because the operator ends up with empty files this tool put
        # there and no other explanation for them.
        print(f"NOTE: this filesystem refuses to delete an existing file, so "
              f"{len(truncated)} stale artifact(s) were emptied instead of "
              f"removed — a zero-byte file reads as absent to every consumer "
              f"here, and cleaning them up is safe: "
              f"{', '.join(truncated)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
