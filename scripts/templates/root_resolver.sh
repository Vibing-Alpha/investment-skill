# root_resolver.sh — the ONE canonical repo-root resolver snippet (Plan B).
#
# This is NOT an executable script: it is the RESOLVER-CORE template that the
# stock-v7-setup skill and every business skill's Step-0 embed VERBATIM
# (between the two delimiter lines below; byte-identity is enforced by
# `distribute check`, exclusive of the marker lines themselves — the embedded
# start-marker line may carry a trailing comment).
#
# Contract: the core ends with ROOT set to the single verified repo root, or
# EMPTY. It contains NO cd, NO clone, NO marker I/O — each consumer appends
# its own divergent tail (setup = pull/clone state machine; business skills =
# cd-or-"run setup first").
#
# POSIX-portable; safe to run before the clone exists (chicken-and-egg-free).

# --- resolver-core ---
# cwd-or-ancestor: if cwd (or ANY parent) is the repo, USE IT — CC-CLI/Codex/Cursor/OpenCode run from the
# repo (or a subdir), so this is a TRUE no-op (covers subdir runs + multi-worktree dev: always the clone
# you're in). Composite marker = scripts/ + prompts/ + strategy.example.yaml (the last is the
# stock-v7-specific tracked file; tighter than CLAUDE.md/VERSION alone).
ROOT=""; d="$PWD"
while [ "$d" != "/" ]; do                # cwd-or-ancestor; marker = scripts/ + prompts/ + strategy.example.yaml
  if [ -d "$d/scripts" ] && [ -d "$d/prompts" ] && [ -f "$d/strategy.example.yaml" ]; then ROOT="$d"; break; fi
  d=$(dirname "$d")
done
case "${STOCK_V7_HOME:-}" in /*) [ -z "$ROOT" ] && ROOT="$STOCK_V7_HOME";; esac   # env override seam — ABSOLUTE only (relative/~ is ignored, mirroring resolve_root's fail-closed; nothing can set it persistently in Cowork)
if [ -z "$ROOT" ]; then
  # Cowork (ephemeral cwd): glob the clone under USER mounts only (exclude outputs/uploads + dot-folders),
  # verify the composite repo marker (a stray dir merely NAMED stock-v7 must not count — round-11),
  # then realpath-dedup (symlinked mounts → same real dir must NOT count as multiple roots).
  # TWO layouts, because a mount is whatever HOST FOLDER the user picked: the repo may be the mount
  # ITSELF (they picked the clone: /sessions/<id>/mnt/stock-v7 — field report 2026-09-07, which the
  # one-depth glob missed and cost every skill its root) or a child of it (they picked the parent:
  # /sessions/<id>/mnt/<sel>/stock-v7). At mount depth the NAME is not required — it is the user's
  # folder name, not ours (a clone of the published `investment-skill` repo is not called stock-v7);
  # the composite marker is the identity check, and it is what makes dropping the name safe.
  # Two separate `ls` calls, NOT one two-glob command: under a nomatch shell an unmatched pattern
  # kills the whole command, so the layout that does not apply would take the one that does with it.
  HITS=$({ ls -d /sessions/*/mnt/*; ls -d /sessions/*/mnt/*/stock-v7; } 2>/dev/null \
    | grep -vE '/mnt/(outputs|uploads|\.[^/]*)(/|$)' \
    | while IFS= read -r h; do (cd "$h" 2>/dev/null && [ -d scripts ] && [ -d prompts ] \
        && [ -f strategy.example.yaml ] && pwd -P); done | sort -u || true)
  if [ "$(printf '%s\n' "$HITS" | grep -c .)" -gt 1 ]; then
    echo "stock-v7: multiple stock-v7 roots in mounts — keep ONE:" >&2; printf '%s\n' "$HITS" >&2; exit 1
  fi
  ROOT=$(printf '%s\n' "$HITS" | head -1)   # the sole hit, or EMPTY — the consumer tail handles empty
fi
# --- end resolver-core ---
