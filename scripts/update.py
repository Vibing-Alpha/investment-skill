#!/usr/bin/env python3
"""Consumer-side skill update: detect a new release + apply it (gstack-style).

A recipient clones the published org repo (e.g. Vibing-Alpha/investment-skill).
This module lets them DETECT when the maintainer has shipped a new release and
CHOOSE to update — it never auto-mutates the repo.

    python3 -m scripts.update check    # is a newer release available? (manual: always live + prints; network-safe)
    python3 -m scripts.update apply    # fast-forward to the latest release + show the changelog

`check` is wired to run on session start for two agents:
  - Claude Code: .claude/settings.json `SessionStart` →
    `update check --quiet --emit-hook-json` (defaults to `--emit-hook-json
    claude`) → user-visible `systemMessage`.
  - Codex: .codex/hooks.json `SessionStart` →
    `update check --quiet --emit-hook-json codex` → `hookSpecificOutput.
    additionalContext` only (Codex's exact schema; it has no user-visible hook
    field, so the notice lands in Codex's model context and the agent relays
    it). See check(hook_format=...).
That AUTOMATIC `--quiet` path self-throttles (default once/hour via a gitignored
state file), times out fast, and is SILENT unless an update exists — so it never
slows or breaks a session. A SessionStart hook's plain stdout reaches only the
agent's context, not the user, which is why we emit the structured hook JSON.
The THROTTLE applies ONLY to the `--quiet` path: a manual `check` (no `--quiet`)
is an explicit human request, so it ALWAYS does the live fetch+compare and always
prints its conclusion ("up to date" or the release notice). `--force` bypasses
the throttle on either path. Cursor/OpenCode users run `check`/`apply` manually
(or wire their own hook — Cursor's sessionStart is informational-only; OpenCode
needs a plugin).

Update model = git pull (the repo IS the distribution unit): `check` does a
lightweight `git fetch`; `apply` does `git pull --ff-only` (refuses to clobber
local edits). Version + notes come from the shipped `VERSION` + `CHANGELOG.md`.

stdlib-only, cross-platform; `check` ALWAYS exits 0 (must never fail a session).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

_SEMVER = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


def _version_gt(a: str, b: str) -> bool:
    """True iff semver a > b. Unparseable either side → False (don't notify)."""
    ma, mb = _SEMVER.match(a), _SEMVER.match(b)
    if not ma or not mb:
        return False
    return tuple(int(x) for x in ma.groups()) > tuple(int(x) for x in mb.groups())

STATE_FILE = ".skill-update-check"   # gitignored; mtime = last check time
DEFAULT_THROTTLE_S = 3600            # once per hour, like gstack
FETCH_TIMEOUT_S = 8                  # never hang a session on a slow network


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git(args: list[str], *, cwd: Path, timeout: int | None = None) -> tuple[int, str, str]:
    """Run git; return (rc, stdout, stderr). Never raises (timeout/OS error → rc=-1)."""
    try:
        # errors="replace": this helper's docstring promises "Never raises" and update runs
        # on SessionStart (must exit 0). It only catches TimeoutExpired/OSError, so a strict
        # decode of malformed git bytes would ESCAPE — replace degrades instead of crashing
        # the hook. Other scripts keep strict utf-8 (Linux parity) where bad output is signal.
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return -1, "", str(e)


def _upstream_branch(root: Path) -> tuple[str, str]:
    """(remote, branch) to check against — the current branch's upstream, else
    origin/main."""
    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=root)
    if rc == 0 and "/" in out:
        remote, _, branch = out.partition("/")
        return remote, branch
    return "origin", "main"


def _file_at(root: Path, ref: str, path: str) -> str:
    rc, out, _ = _git(["show", f"{ref}:{path}"], cwd=root)
    return out if rc == 0 else ""


def _changelog_top(text: str) -> str:
    """The first `## ...` section of a CHANGELOG (the latest release notes)."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return "\n".join(lines[start:end]).strip()


def _throttled(root: Path, throttle_s: int) -> bool:
    state = root / STATE_FILE
    try:
        if state.is_file() and (time.time() - state.stat().st_mtime) < throttle_s:
            return True
    except OSError:
        pass
    return False


def _touch_state(root: Path) -> None:
    try:
        (root / STATE_FILE).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def check(root: Path, *, throttle_s: int, force: bool, quiet: bool,
          hook_format: str | None = None) -> int:
    """Detect whether a newer release is available. ALWAYS returns 0.

    `hook_format` (agent SessionStart adapter): emit a JSON object instead of
    plain text when an update is found; silent (empty stdout) when there is
    none. A SessionStart hook's plain stdout reaches only the agent's context,
    NOT the user's terminal, so plain text alone is invisible to the recipient.
      - "claude": top-level `systemMessage` (Claude Code shows it to the USER)
        + `hookSpecificOutput.additionalContext`.
      - "codex":  `hookSpecificOutput.additionalContext` ONLY — Codex's exact
        documented schema (it has no user-visible field; the notice reaches
        Codex's model context and the agent relays it). We omit `systemMessage`
        there since Codex may not expect that key.
    Default None keeps the portable plain-text behavior for manual runs and
    non-hook agents.
    """
    # Throttle ONLY the automatic path (the SessionStart hook runs `check
    # --quiet`). A manual `check` is an explicit human request — silencing it
    # because the hook stamped the state file within the hour gives the user
    # zero output, indistinguishable from "up to date" even when a release is
    # available. `quiet` is the proxy for "automatic context" (the hook is the
    # only --quiet caller); the network-protection guarantee for session-start
    # is preserved because the hook still passes --quiet. `--force` bypasses
    # regardless.
    if not force and quiet and _throttled(root, throttle_s):
        return 0
    _touch_state(root)  # stamp BEFORE network so a hang can't cause a re-check storm

    remote, branch = _upstream_branch(root)
    rc, _, _ = _git(["fetch", "--quiet", remote, branch], cwd=root, timeout=FETCH_TIMEOUT_S)
    if rc != 0:
        return 0  # offline / no remote / timeout → silent

    rc_l, local, _ = _git(["rev-parse", "HEAD"], cwd=root)
    rc_r, remote_sha, _ = _git(["rev-parse", "FETCH_HEAD"], cwd=root)
    if rc_l != 0 or rc_r != 0:
        return 0
    # A "release" is signalled by a HIGHER VERSION — NOT merely being behind by
    # commits. This keeps the session-start check silent on the maintainer's own
    # dev commits (which don't bump VERSION) and on any non-release upstream
    # movement; recipients are notified only on a real release.
    have = (_file_at(root, "HEAD", "VERSION") or "0.0.0").strip()
    new = (_file_at(root, "FETCH_HEAD", "VERSION") or "0.0.0").strip()
    if not _version_gt(new, have):
        # hook-json consumers expect JSON-or-nothing; never emit the plain
        # up-to-date line into that channel.
        if not quiet and hook_format is None:
            print("Skills are up to date (no newer release).")
        return 0

    # Build the release notice ONCE — one implementation shared by the plain
    # and hook-json renderings (.claude/rules/producer-consumer.md §3).
    notes = _changelog_top(_file_at(root, "FETCH_HEAD", "CHANGELOG.md"))
    lines = [
        f"✨ A new skill release is available: v{new} (you have v{have}).",
        "   Update with:  python3 -m scripts.update apply",
    ]
    if notes:
        lines.append("   What's new:")
        lines.extend(f"     {ln}" for ln in notes.splitlines())
    # if local has its own commits, ff-only apply may need a manual merge
    if _git(["merge-base", "--is-ancestor", local, remote_sha], cwd=root)[0] != 0:
        lines.append("   (note: your repo has local commits; `apply` is "
                     "fast-forward-only and may need a manual merge.)")
    msg = "\n".join(lines)

    if hook_format is not None:
        # Agent SessionStart adapter: a hook's plain stdout reaches only the
        # agent's context, NOT the user. ensure_ascii=True (default) keeps the
        # ✨ on the wire as \uXXXX — safe on a Windows cp936 console; the agent
        # decodes it back for display. `additionalContext` (model context) is
        # always emitted; Claude Code ALSO gets a top-level `systemMessage` it
        # shows the user — Codex has no such field so we omit it there.
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": msg,
            },
        }
        if hook_format == "claude":
            # Claude Code shows a top-level `systemMessage` to the USER. Codex
            # has no such field (and may not expect the key), so omit it there.
            payload["systemMessage"] = msg
        print(json.dumps(payload))
        return 0

    print("\n" + msg + "\n")
    return 0


def apply(root: Path) -> int:
    """Fast-forward to the latest release. Refuses to clobber local edits."""
    remote, branch = _upstream_branch(root)
    before = _file_at(root, "HEAD", "VERSION") or "?"
    rc, out, err = _git(["pull", "--ff-only", remote, branch], cwd=root, timeout=60)
    if rc != 0:
        print("Could not fast-forward — you have local commits or uncommitted "
              "changes that diverge from the release. Resolve manually "
              f"(git status). Details:\n{err or out}", file=sys.stderr)
        return 1
    _touch_state(root)
    after = _file_at(root, "HEAD", "VERSION") or "?"
    if before == after:
        print("Already up to date.")
        return 0
    print(f"✅ Updated skills: v{before} → v{after}")
    notes = _changelog_top((root / "CHANGELOG.md").read_text(encoding="utf-8")
                           if (root / "CHANGELOG.md").is_file() else "")
    if notes:
        print(notes)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="update",
                                description="Detect + apply skill releases.")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check",
                       help="Is a newer release available? (manual: always live; "
                            "--quiet/auto path: throttled + silent)")
    c.add_argument("--throttle", type=int, default=DEFAULT_THROTTLE_S,
                   help=f"min seconds between AUTOMATIC (--quiet) checks "
                        f"(default {DEFAULT_THROTTLE_S}); ignored for a manual check")
    c.add_argument("--force", action="store_true", help="ignore the throttle")
    c.add_argument("--quiet", action="store_true",
                   help="auto/hook mode: print only on an update AND honor the "
                        "once/hour throttle")
    c.add_argument("--emit-hook-json", nargs="?", const="claude", default=None,
                   choices=["claude", "codex"], dest="hook_format",
                   help="agent SessionStart adapter: on an update, emit a JSON "
                        "object instead of plain text. 'claude' (default) adds a "
                        "user-visible `systemMessage`; 'codex' emits only "
                        "`hookSpecificOutput.additionalContext` (Codex's exact "
                        "schema). Plain hook stdout reaches only the agent's "
                        "context, not the user.")
    sub.add_parser("apply", help="fast-forward to the latest release")
    args = p.parse_args(argv)
    root = repo_root()
    if args.command == "check":
        return check(root, throttle_s=args.throttle, force=args.force,
                     quiet=args.quiet, hook_format=args.hook_format)
    if args.command == "apply":
        return apply(root)
    return 2


if __name__ == "__main__":
    sys.exit(main())
