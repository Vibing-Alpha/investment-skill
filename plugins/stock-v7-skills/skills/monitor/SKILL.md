---
name: monitor
description: |
  Daily entry-point / triage-router for the stock system. Run it each day: it
  assesses cross-system state (holdings + watchlist) and produces a ranked action
  plan, routing each flagged item to the right existing skill, then triggers the
  selected one(s) on your confirmation. It states facts and routes — it does NOT
  decide or place trades.
  Trigger phrases: "monitor", "/monitor", "daily check", "what should I look at
  today", "what needs attention", "morning check", "any alerts", "run my daily
  monitor", "what's new on my holdings/watchlist".
  Requires portfolio-state.yaml (holdings + watchlist). Reuses prior
  /score-business + /investment-thesis artifacts; does not itself analyze.
  NOT for making portfolio/allocation decisions (it routes to portfolio).
  NOT for discovering new tickers (it routes to screen-stocks).
  NOT for writing reports (use write-report).
user_invocable: true
---

# /monitor — Daily Entry-Point / Triage-Router

Orchestration only. This skill runs the deterministic `scripts.monitor` CLI and the
router prompt, then routes you to the right skill. It owns NO analysis logic and makes
NO trade decisions.

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here.

Run this block first and **CAPTURE the `STOCK_V7_ROOT=...` value it prints**. Substitute
that absolute path for the literal `<captured-abs-ROOT>` in every later Bash block, every
harness Read path, and every subagent-dispatch path in this skill. If this block exits
non-zero (multiple candidate roots, or no repo found), show its stderr to the user and
**STOP** — run nothing else.

If it prints a `stock-v7: WARNING — version skew` line, relay that warning to the user verbatim and continue — it is advisory only (the installed plugin and the clone are at different versions; it tells the user which half to update), never a stop.

```bash
# --- resolver-core ---   (byte-identical to scripts/templates/root_resolver.sh — Task 5 enforces)
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
# BUSINESS tail (the setup skill replaces everything below the end-marker with its clone/pull tail):
if [ -z "$ROOT" ]; then                                   # CC-CLI marker fallback (rare: not-in-repo + no env)
  ROOT=$(cat "$HOME/.stock-v7-home" 2>/dev/null | tr -d '\r')   # strip CRLF if the marker was hand-edited on Windows
  ROOT="${ROOT:-$HOME/Claude/stock-v7}"
fi
cd "$ROOT" 2>/dev/null || { echo "stock-v7: run the setup skill first" >&2; exit 1; }
printf 'STOCK_V7_ROOT=%s\n' "$PWD"   # Step 0 EMITS the resolved abs root (post-cd $PWD) for the agent to capture
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.version_skew --expected-min "1.2.1" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync
```

## Preflight: Money-path config

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.config_gate check --portfolio
```

If it exits non-zero, STOP and show its stderr to the user (config not confirmed / API
key missing / portfolio-state malformed) — do NOT run any analysis or produce numbers.
Then continue below.

## Step 0: Resolve the run directory

The run dir is `reports/monitor/{YYYYMMDD}` — a pure function of the ET session date,
so each later block **re-derives it in-block** (a fresh shell loses these variables;
re-deriving returns the same dir all day).

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
RUN_DATE=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y-%m-%d'))")
REPORT_DIR="reports/monitor/$(echo "$RUN_DATE" | tr -d '-')"
printf 'RUN_DATE=%s\nREPORT_DIR=%s\n' "$RUN_DATE" "$REPORT_DIR"
```

Note the printed `REPORT_DIR` (relative to the repo root) — you will substitute the
absolute form `<captured-abs-ROOT>/<REPORT_DIR>` into the subagent dispatch in Step 2.

## Step 1: Probe (deterministic facts)

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
RUN_DATE=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y-%m-%d'))")
REPORT_DIR="reports/monitor/$(echo "$RUN_DATE" | tr -d '-')"
"$PYBIN" -m scripts.monitor probe --state portfolio-state.yaml \
  --output "$REPORT_DIR/monitor_probe.json" --run-date "$RUN_DATE"
```
If this **exits non-zero** (empty universe — no holdings or watchlist, or missing
`portfolio-state.yaml`), there is nothing to monitor: tell the user and STOP. Do not
proceed to Step 2.

## Step 2: Dispatch the router (judgment → raw plan)

First clear any stale raw plan. The date directory is reused on a same-day rerun, so without
this a router that fails to write would leave the existence gate below satisfied by an EARLIER
run's `action_plan.raw.json`, and the pipeline would go on to validate/render stale router
output. Removing it first makes the gate prove THIS run produced a fresh plan:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
REPORT_DIR="reports/monitor/$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")"
rm -f "$REPORT_DIR/action_plan.raw.json"
```

Dispatch a subagent with `<captured-abs-ROOT>/prompts/monitor-route.md` as its
instructions and the **concrete resolved, absolute** probe path as its sole input.
**CRITICAL — a subagent does NOT inherit this shell's `$REPORT_DIR` or its cwd**
(`.claude/rules/skill-architecture.md` #8): when you compose the dispatch prompt,
SUBSTITUTE the literal absolute paths — e.g.
`<captured-abs-ROOT>/reports/monitor/20260601/monitor_probe.json` as the input and
instruct it to WRITE `<captured-abs-ROOT>/reports/monitor/20260601/action_plan.raw.json`
— never a literal `$REPORT_DIR/...` string or a bare relative `reports/...` path (the
subagent's shell would resolve those against ITS ephemeral cwd). Writing `.json` is
allowed for subagents (only the `.md`-write tool is blocked). The router reads ONLY the
probe (no other files, no WebSearch). After dispatch, HARD existence gate — if it exits
non-zero the router produced nothing: re-dispatch ONCE with the same inputs; if the gate
fails a second time, surface the error and STOP:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
REPORT_DIR="reports/monitor/$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")"
[ -s "$REPORT_DIR/action_plan.raw.json" ] || { echo "FATAL: router produced no action_plan.raw.json" >&2; exit 1; }
```

## Step 3: Validate + stamp (HARD gate)

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
REPORT_DIR="reports/monitor/$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")"
"$PYBIN" -m scripts.monitor validate \
  --raw "$REPORT_DIR/action_plan.raw.json" \
  --probe "$REPORT_DIR/monitor_probe.json" \
  --output "$REPORT_DIR/action_plan.json"
```
If this exits non-zero, the router produced an invalid or boundary-violating plan
(e.g. an order/advice in a reason, a dangling evidence_ref, a `status` field). Do NOT
render or trigger. **Exactly ONE repair attempt:** re-dispatch the SAME router prompt with
the SAME concrete absolute probe path + the validator's stderr appended ("your previous plan
failed validation: <stderr> — fix and re-emit to the same path"), then re-run `validate`
once. If it fails the SECOND time, surface the stderr and STOP (no further retries —
escalate to the user). This bounds the loop and never renders an unvalidated plan.

## Step 4: Render the digest (deterministic)

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
REPORT_DIR="reports/monitor/$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")"
"$PYBIN" -m scripts.monitor render-digest \
  --plan "$REPORT_DIR/action_plan.json" \
  --probe "$REPORT_DIR/monitor_probe.json" \
  --output "$REPORT_DIR/digest.md"
```

## Step 5: Present + choose

Show the user the digest at `<captured-abs-ROOT>/reports/monitor/{YYYYMMDD}/digest.md`
(use the absolute path — the harness Read tool does not follow the bash `cd`). Then use
AskUserQuestion to ask which item(s) to act on — options are the plan's items (each shown
as `<ticker> — <priority> → <route>`) plus "none". The plan is a triage; the user decides
what to run.

## Step 6: Trigger the selected skill(s)

For each item the user selected, invoke its `route` skill **sequentially** (never in
parallel), passing the ticker where applicable:
- `/investment-thesis <ticker>` · `/portfolio` · `/score-business <ticker>` · `/screen-stocks`

Invoke one, let it complete (it owns its own cascades), then the next. Do NOT trigger
anything the user did not select. `/monitor` itself decides nothing about the trades —
the target skill does.
