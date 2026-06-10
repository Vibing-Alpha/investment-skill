---
name: stock-v7-setup
description: |
  Run this first — one-time setup for the stock-v7 investment-analysis system.
  Clones the published repo into persistent storage (the Cowork mount, or
  ~/Claude/stock-v7 on a plain machine), installs Python deps into a
  persistent venv, seeds personal config (.env / strategy.yaml /
  portfolio-state.yaml), then — after you edit the config — verifies and
  stamps it via "/stock-v7-setup confirm".
  Invoked explicitly by the user (never model-triggered): "/stock-v7-setup",
  "set up stock-v7", "install the stock skills", "首次安装", "初始化配置".
  NOT for running any analysis (use score-business / investment-thesis).
  NOT for updating an existing install (use scripts.update apply).
disable-model-invocation: true
---

# Stock v7 Setup — one-time clone-launcher

Sets up the stock-v7 system once: resolve-or-install the repo clone, build a
persistent venv, seed config, then confirm. Two invocation modes:

- `/stock-v7-setup` — first run (or re-run after problems): Steps 1–2.
- `/stock-v7-setup confirm` — after editing the config: Step 3 only
  (run the Step 1 block first if you don't have a concrete `ROOT` from this
  session — it is idempotent).

Environment facts this skill is built around: on Cowork every Bash call is a
FRESH shell and `$HOME`/`$TMPDIR`/`$PWD` are ephemeral — the ONLY persistent
storage is the mounted folder(s) `/sessions/<id>/mnt/<name>`. Cowork shells
are NON-interactive: never prompt with `read`; print instructions and exit
instead. Carry state across steps by substituting the concrete `ROOT` path
this skill echoes — never a shell variable from a previous block.

## Step 1: Resolve ROOT, clone-or-update, venv, seed config

Run this single self-contained block. It embeds the canonical resolver-core
(shared with every business skill's Step 0), then a setup tail keyed on the
post-core `ROOT` state, then builds the venv and runs the Plan-A bootstrap
seed. Capture the final `stock-v7: setup ROOT=...` line — later steps
substitute that concrete path.

```bash
# --- resolver-core --- (embedded verbatim from scripts/templates/root_resolver.sh — edit the template, then re-embed)
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
  HITS=$(ls -d /sessions/*/mnt/*/stock-v7 2>/dev/null | grep -vE '/mnt/(outputs|uploads|\.[^/]*)(/|$)' \
    | while IFS= read -r h; do (cd "$h" 2>/dev/null && [ -d scripts ] && [ -d prompts ] \
        && [ -f strategy.example.yaml ] && pwd -P); done | sort -u || true)
  if [ "$(printf '%s\n' "$HITS" | grep -c .)" -gt 1 ]; then
    echo "stock-v7: multiple stock-v7 roots in mounts — keep ONE:" >&2; printf '%s\n' "$HITS" >&2; exit 1
  fi
  ROOT=$(printf '%s\n' "$HITS" | head -1)   # the sole hit, or EMPTY — the consumer tail handles empty
fi
# --- end resolver-core ---

# --- setup tail: state machine keyed on the post-core ROOT state (set+valid / set+absent / set+invalid / empty) ---
REPO_URL="https://github.com/Vibing-Alpha/investment-skill.git"
if [ -n "$ROOT" ]; then
  if [ ! -e "$ROOT" ]; then
    # env-set fresh install (the multi-mount re-run path): clone into the chosen location
    git clone "$REPO_URL" "$ROOT" || exit 1
  elif [ -d "$ROOT/scripts" ] && [ -d "$ROOT/prompts" ] && [ -f "$ROOT/strategy.example.yaml" ]; then
    git -C "$ROOT" pull --ff-only || exit 1
  else
    # fail-closed: never pull or clone into a non-empty foreign dir
    echo "stock-v7: $ROOT exists but is not a stock-v7 clone — remove/repair it or point STOCK_V7_HOME elsewhere" >&2
    exit 1
  fi
else
  RAW=$(ls -d /sessions/*/mnt/* 2>/dev/null || true)                                      # bare mount listing — no trailing slash
  ELIGIBLE=$(printf '%s\n' "$RAW" | grep -vE '/mnt/(outputs|uploads|\.[^/]*)(/|$)' | grep . || true)
  N=$(printf '%s\n' "$ELIGIBLE" | grep -c .)
  if [ -z "$RAW" ]; then
    # zero mounts => not Cowork: CC-CLI / plain machine — install under $HOME
    ROOT="$HOME/Claude/stock-v7"
    if [ -d "$ROOT/scripts" ] && [ -d "$ROOT/prompts" ] && [ -f "$ROOT/strategy.example.yaml" ]; then
      git -C "$ROOT" pull --ff-only || exit 1
    else
      # a partial/stale dir NAMED stock-v7 fails the marker — git clone then fails LOUD on the
      # existing dir (acceptable, never silent): tell the user to remove/repair it
      git clone "$REPO_URL" "$ROOT" || exit 1
    fi
  elif [ "$N" -gt 1 ]; then
    # NON-interactive shell: never `read`/prompt — print the numbered mounts + the re-run interface, then exit
    echo "stock-v7: multiple eligible mounts — choose ONE and re-run as:" >&2
    echo "  STOCK_V7_HOME=<chosen-mount>/stock-v7 /stock-v7-setup   (ABSOLUTE path)" >&2
    printf '%s\n' "$ELIGIBLE" | awk '{printf "  %d) %s\n", NR, $0}' >&2
    exit 1
  elif [ "$N" -eq 1 ]; then
    MNT=$(printf '%s\n' "$ELIGIBLE" | head -1)
    [ -n "$MNT" ] || { echo "stock-v7: empty mount path — aborting" >&2; exit 1; }    # never compose ROOT=/stock-v7
    ROOT="$MNT/stock-v7"
    git clone "$REPO_URL" "$ROOT" || exit 1
  else
    echo "stock-v7: Cowork mounts exist but none is eligible (only outputs/uploads/dot-folders) — mount a project folder and re-run" >&2
    exit 1
  fi
fi

# --- persistent venv IN THE CLONE (the mount on Cowork — deps must survive the session) ---
# Probe BOTH venv layouts: POSIX .venv/bin/python and Windows-CPython .venv/Scripts/python.exe
# (git-bash is a supported surface — same dual probe as the per-skill $PYBIN recipe).
PYVENV="$ROOT/.venv/bin/python"; [ -x "$PYVENV" ] || PYVENV="$ROOT/.venv/Scripts/python.exe"
if [ ! -x "$PYVENV" ]; then
  # Cowork quirk: python3 -m venv can FAIL at the ensurepip step yet still produce a USABLE venv.
  # Do NOT gate on venv's exit code — the functional probe below is the gate.
  python3 -m venv "$ROOT/.venv" || echo "stock-v7: venv creation reported failure — probing pip anyway (Cowork ensurepip quirk)" >&2
  PYVENV="$ROOT/.venv/bin/python"; [ -x "$PYVENV" ] || PYVENV="$ROOT/.venv/Scripts/python.exe"
fi
if ! "$PYVENV" -m pip --version; then
  echo "stock-v7: venv at $ROOT/.venv is broken (pip probe failed — see stderr above). Remove $ROOT/.venv and re-run /stock-v7-setup." >&2
  exit 1
fi
"$PYVENV" -m pip install -r "$ROOT/requirements.txt" || exit 1

# --- seed personal config (Plan A bootstrap; idempotent — never clobbers existing files) ---
cd "$ROOT" || exit 1
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.distribute bootstrap || exit 1
echo "stock-v7: setup ROOT=$ROOT"
```

If the block exits non-zero, show the user its stderr and STOP — do not
improvise around a failed clone, broken venv, or bootstrap error.

## Step 2: Tell the user to edit, then confirm

Print (in the user's language) the edit-then-confirm instruction, using the
concrete `ROOT` path from Step 1:

- `$ROOT/.env` — set the two required API keys: `FINANCIAL_DATASETS_API_KEY`
  (financialdatasets.ai) and `FMP_API_KEY` (financialmodelingprep.com);
  `FINNHUB_API_KEY` (finnhub.io) optional.
- `$ROOT/strategy.yaml` — investment preferences; `mandate.edge` is REQUIRED
  (the confirm step fails without it).
- `$ROOT/portfolio-state.yaml` — fill in real holdings, cash, and watchlist.
- Then run `/stock-v7-setup confirm`.

Setup is NOT complete until the confirm step passes — the analysis skills'
config gate stays closed.

## Step 3 (confirm mode): verify + stamp

Only on `/stock-v7-setup confirm`. Substitute the concrete `ROOT` echoed by
Step 1 (re-run the Step 1 block first if this is a new session — it is
idempotent):

```bash
ROOT=/substitute/the/concrete/path/echoed/by/step-1   # from "stock-v7: setup ROOT=..."
cd "$ROOT" || exit 1
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.distribute bootstrap --confirm
```

`bootstrap --confirm` verifies the edited config, stamps
`setup.config_version` into `strategy.yaml` (the PERSISTENT confirm signal on
Cowork — it lives in the clone, i.e. the mount), and writes `~/.stock-v7-home`
(the CC-CLI root marker; moot on Cowork where `$HOME` is ephemeral).

On non-zero exit: show the user its stderr (what is missing/invalid) and
STOP. On success, tell the user setup is complete and to try
`/score-business AAPL`.
