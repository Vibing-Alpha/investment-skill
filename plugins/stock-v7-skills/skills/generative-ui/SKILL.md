---
name: generative-ui
description: |
  Turn ALREADY-COMPUTED analysis into a visual, self-contained HTML dashboard you open in a
  browser — either for ONE stock or for your WHOLE portfolio + watchlist on one page. A standalone
  HTML file is the PORTABLE form of generative UI — it works in any harness that can run a script
  and open a file; it is also the only option in Claude Code, which (unlike claude.ai) has no inline
  widget renderer / show_widget. The capability's core (scripts/viz.py + prompts/generative-ui.md)
  is portable; this SKILL is its Claude Code adapter. Use this when the user wants to SEE the
  analysis, not read markdown.
  Trigger phrases: "可视化", "生成式 ui", "做个仪表盘", "dashboard", "visualize TICKER",
  "看板", "图表化", "把分析做成网页", "visual report", "生成 html", "render the analysis",
  "整个持仓", "组合仪表盘", "portfolio dashboard", "把持仓做成看板", "visualize my portfolio",
  "全部持仓可视化", "watchlist dashboard".
  Composes ONLY from existing artifacts (bq_analysis.json + investment_thesis/valuation/
  technical/events) via a deterministic view-model — it does NOT re-analyze, fetch, or decide.
  Its source data is verified faithful, and a gate rejects any data literal hardcoded in the
  static markup; the dashboard is built to render numbers from that verified view-model.
  Requires a prior /score-business run (per ticker); portfolio mode reads portfolio-state.yaml.
  NOT for running the analysis itself (use score-business / investment-thesis).
  NOT for the markdown write-up (use write-report).
  NOT for portfolio-level decisions or orders (use portfolio).
user_invocable: true
---

# Generative UI — visual dashboard from existing analysis

Orchestration only. It builds a deterministic **view-model** (`scripts.viz`), hands it to a
generator that produces a self-contained HTML dashboard per `prompts/generative-ui.md`, then runs
a **fidelity gate** — the embedded view-model equals the source AND no data literal is hardcoded in
the static markup — before handing it to the user. (The gate does not execute the render JS;
rendering each value verbatim from the view-model is the generator prompt's rule.) Methodology +
the data-fidelity contract live in that prompt — read it; do not duplicate here. It decides nothing
and re-analyzes nothing.

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

## Step 0: Pick the mode

Decide the mode from the user's argument — no shell state involved:

- **Portfolio mode** — argument is empty, `portfolio`, `all`, `组合`, or `持仓`
  (case-insensitive): visualize the whole portfolio-state universe (holdings +
  watchlist). Run the Preflight below first.
- **Single-ticker mode** — argument matches `^[A-Z][A-Z.]{0,9}$` (and isn't a mode
  keyword): visualize that ticker. SKIP the Preflight heading below (ticker mode
  composes from already-gated artifacts) and go straight to its build block in Step 1.

## Preflight: Money-path config (portfolio mode)

Portfolio mode reads holdings (`portfolio-state.yaml`), so it is gated like
`/portfolio`. Run this ONLY in portfolio mode:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.config_gate check --portfolio
```

If it exits non-zero, STOP and show its stderr to the user (config not confirmed / API
key missing / portfolio-state malformed) — do NOT build the view-model or produce numbers.
Then continue below.

## Step 1: Build the view-model

Each build block prints `VM=` / `OUT=` **absolute** paths — CAPTURE them and substitute
the literals for `<captured-VM-path>` / `<captured-OUT-path>` in Steps 2–4 (fresh shells
do not carry `$VM`/`$OUT` across blocks).

**Portfolio mode:**

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
RUN_DATE=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y-%m-%d'))")
REPORT_DIR="reports/portfolio/$(echo "$RUN_DATE" | tr -d '-')"
mkdir -p "$REPORT_DIR"
printf 'VM=%s/portfolio_view_model.json\nOUT=%s/portfolio_dashboard.html\n' "$PWD/$REPORT_DIR" "$PWD/$REPORT_DIR"
"$PYBIN" -m scripts.viz build-portfolio-view-model --state portfolio-state.yaml \
  --output "$REPORT_DIR/portfolio_view_model.json" --run-date "$RUN_DATE"
```
This fails closed (non-zero) if `portfolio-state.yaml` is empty/missing — tell the user
and STOP. A ticker with no analysis is included as `analyzed: false` (never dropped or
fabricated).

**Single-ticker mode:**

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"   # agent-substituted (e.g. AMD)
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
[ -n "$REPORT_DIR" ] \
  || { echo "No analysis for $TICKER — run /score-business $TICKER first" >&2; exit 1; }
printf 'VM=%s/view_model.json\nOUT=%s/dashboard.html\n' "$PWD/$REPORT_DIR" "$PWD/$REPORT_DIR"
"$PYBIN" -m scripts.viz build-view-model --ticker "$TICKER" \
  --report-dir "$REPORT_DIR" --output "$REPORT_DIR/view_model.json"
```
If `find-latest-prior` is empty there is nothing to visualize — tell the user to run
`/score-business {TICKER}` first and STOP. Do not invent a dashboard.

## Step 2: Generate the dashboard (the "generative" step)

First clear any stale dashboard so the existence gate proves THIS run produced a fresh one (the
report dir is reused across runs):

```bash
cd "<captured-abs-ROOT>"
OUT="<captured-OUT-path>"   # agent-substituted absolute literal from Step 1
rm -f "$OUT"
```

Dispatch a subagent with `<captured-abs-ROOT>/prompts/generative-ui.md` as its instructions and
the **concrete resolved, absolute** view-model path as its sole input. **CRITICAL — a subagent does
NOT inherit this shell's `$VM`/`$OUT` or its cwd** (`.claude/rules/skill-architecture.md` #8): when
you compose the dispatch prompt, SUBSTITUTE the captured absolute literals — e.g. read
`<captured-VM-path>` and WRITE `<captured-OUT-path>` — never a literal `$VM`/`$OUT` string or a
relative `reports/...` path. Writing `.html` is allowed for subagents (only the `.md`-write tool is
blocked; if it ever refuses, write via a Bash heredoc with a quoted, content-unique sentinel). The
generator reads ONLY the view-model and dispatches on its `kind` (`ticker` vs `portfolio`). After
dispatch, HARD existence gate — if it fails, re-dispatch ONCE; on a second failure surface the
error and STOP:

```bash
cd "<captured-abs-ROOT>"
OUT="<captured-OUT-path>"
[ -s "$OUT" ] || { echo "FATAL: generator produced no dashboard" >&2; exit 1; }
```

## Step 3: Verify data fidelity (HARD gate)

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
VM="<captured-VM-path>"
OUT="<captured-OUT-path>"
"$PYBIN" -m scripts.viz verify --html "$OUT" --view-model "$VM"
```
Non-zero means the dashboard embeds data that drifted from the view-model, or hardcodes a data
number in static markup, or is missing/duplicating the embedded block. Do NOT hand it to the user.
**Exactly ONE repair attempt:** re-dispatch the SAME generator prompt with the SAME concrete
view-model path plus the verifier's stderr appended ("your dashboard failed fidelity verification:
<stderr> — embed the view-model verbatim in `<script id=\"v7-view-model\" type=\"application/json\">`
and render every number from it via JS; put no data literal in static markup"), then re-run
`verify` once. If it fails a SECOND time, surface the stderr and STOP — never hand the user a
dashboard whose numbers don't trace to the source.

## Step 4: Hand off

Tell the user the dashboard is ready and give the captured OUT path to open in a browser. It is a
view of existing machine verdicts — it decides nothing; point them to `/portfolio` for any action.
In portfolio mode, note that prices are each name's last-analysis price (not live; see the
dashboard's own caveat).
