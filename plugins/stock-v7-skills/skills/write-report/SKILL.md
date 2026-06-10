---
name: write-report
description: |
  Turn a stock's ALREADY-COMPUTED analysis into a readable, decision-ready
  investment report (Markdown). Use this skill when the user has run
  /score-business and/or /investment-thesis on a ticker and now wants the
  full human-readable write-up — not the thin auto-summary, but a report that
  carries the reasoning chain so a human can read it top-to-bottom and trust it.
  Trigger phrases: "写报告", "生成报告", "完整报告", "深度报告", "把分析写成报告",
  "出个 writeup", "investment report", "write up TICKER", "full report on TICKER",
  "give me the report", "可读的报告", "research report for".
  Composes ONLY from existing artifacts (bq_analysis.json, investment_thesis.json,
  valuation/technical/events.json) — it does NOT re-analyze, NOT fetch new data.
  Requires a prior /score-business run (bq_analysis.json). Richer when
  /investment-thesis has also run.
  NOT for running the analysis itself (use score-business / investment-thesis).
  NOT for portfolio-level decisions or orders (use portfolio).
  NOT for industry-level candidate discovery (use research-industry).
user_invocable: true
---

# Write Report — readable report from existing analysis

This skill is **orchestration only**: locate the latest analysis artifacts for a
ticker, verify the required one exists, then hand them to the writing agent which
composes the report per `prompts/write-report.md`. The methodology (compose-only
rule, three-layer fusion craft, structure) lives in that prompt — read it; do not
duplicate it here.

The skill answers: **"I have a machine verdict on this name — give me the
readable report that lets me trust it and act."** It does NOT decide, fetch, or
re-analyze. Everything it writes traces to artifacts that already exist.

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here. `<TICKER>` below is likewise substituted by you into EACH block (never
carried as a shell variable across blocks), and each block re-runs the idempotent
`find-latest-prior` to re-derive the run dir.

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
"$PYBIN" -m scripts.version_skew --expected-min "1.0.15" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync
```

## Step 0: Validate ticker, locate the latest analyzed run

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"   # agent-substituted (e.g. AAPL) — substituted into every block, not exported across them
echo "$TICKER" | grep -Eq '^[A-Z][A-Z.]{0,9}$' \
  || { echo "FATAL: invalid ticker format: '$TICKER'" >&2; exit 1; }

# Most recent run dir that holds this ticker's analysis (include today).
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
[ -n "$REPORT_DIR" ] \
  || { echo "No analysis for $TICKER — run /score-business $TICKER first" >&2; exit 1; }
printf 'REPORT_DIR=%s\n' "$REPORT_DIR"
```

If this block exits non-zero, STOP: an invalid ticker is a user-input problem, and an
empty `find-latest-prior` means there is no analysis to write from — tell the user to
run `/score-business {TICKER}` first. Do not invent a report. Note the printed
`REPORT_DIR` (relative to the repo root): the absolute form is
`<captured-abs-ROOT>/<REPORT_DIR>` — use it for every Read and dispatch path below.

## Step 1: Verify required artifact + detect mode (fail-closed)

`bq_analysis.json` is **required** — it is the floor of any report. Verify it
loads through the typed loader before composing; a malformed artifact should fail
loudly here, not produce a garbage report. If this block exits non-zero, STOP and
show the error.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
"$PYBIN" -c "
from scripts.schemas.bq_analysis import load_bq_analysis
import sys
try:
    art = load_bq_analysis('$REPORT_DIR/bq_analysis.json')
    print(f'OK bq: {art.meta.ticker} overall={art.scores.overall}', file=sys.stderr)
except Exception as e:
    print(f'FATAL: bq_analysis.json missing/invalid: {e}', file=sys.stderr); sys.exit(1)
" || exit 1
```

Then detect the **mode** by which optional artifacts are present in the run dir
(`<captured-abs-ROOT>/<REPORT_DIR>/`):
- `investment_thesis.json` present → **full investment report** (BQ + valuation +
  timing + thesis verdict).
- absent → **business-quality report** (BQ only; the report says the thesis layer
  hasn't been run and is a quality read, not an entry decision).

Note which of `valuation.json`, `technical.json`, `events.json` exist — the writer
composes the sections it has data for and names the ones it doesn't (e.g. a DL4
fail-close name with no DCF). Never block on a missing optional artifact.

## Step 2: Compose the report

Read `<captured-abs-ROOT>/prompts/write-report.md` (the methodology) and
`<captured-abs-ROOT>/strategy.yaml` for `output_language` (default zh-CN). Read the
present artifacts at their ABSOLUTE paths under `<captured-abs-ROOT>/<REPORT_DIR>/`
(`bq_analysis.json` always; `investment_thesis.json`, `valuation.json`,
`technical.json`, `events.json`, and the existing `summary.md` when present).

Spawn the writing agent (or compose inline) with the prompt + the artifacts.
The agent writes the report — composing strictly from the artifacts, applying
three-layer fusion, avoiding the anti-patterns — to:

```
<captured-abs-ROOT>/reports/{TICKER}/{YYYYMMDD}/report.md
```

**Dispatch note (`.claude/rules/skill-architecture.md` #8):** if you dispatch
a SUBAGENT to write `report.md`, the harness blocks subagent `.md` writes via
the Write tool — instruct it to write the file via a Bash heredoc with a
content-unique quoted delimiter
(`cat > "<captured-abs-ROOT>/reports/<TICKER>/<DATE>/report.md" <<'REPORT_MD_EOF' … REPORT_MD_EOF`,
UTF-8; NOT a bare `EOF`/`MD` — collision truncates; substitute the ACTUAL
ABSOLUTE path — the subagent shell has no `$REPORT_DIR` and its cwd is
ephemeral, so a relative `reports/…` path would land in the wrong place). If
the lead composes inline, the Write tool works (no subagent guard) — give it
the absolute path too. Either way, hard-gate it before reporting success
(catches a missing/empty `report.md`); if the gate fails twice
(re-dispatch / re-compose once), STOP and surface the failure:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
[ -s "$REPORT_DIR/report.md" ] \
  || { echo "FATAL: report.md was not produced — re-dispatch / re-compose" >&2; exit 1; }
```

Use the same `{YYYYMMDD}` as the located run dir (the report describes that run's
analysis; it does not start a new analysis run).

## Step 3: Self-check + report to user

The writing prompt ends with a self-check (traceability, no orphaned tables, no
hollow conclusions, verdict consistency, length). Confirm the report passed it.

Optionally note the length using the CJK-aware counter (the report is human-facing
prose; `wc -w` undercounts zh-CN ~3x):

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
"$PYBIN" -c 'import sys; from scripts.cli_utils import count_word_equivalents; print(count_word_equivalents(open(sys.argv[1], encoding="utf-8").read()))' "$REPORT_DIR/report.md"
```

There is no hard word cap — the report tracks the data ("readable in ~10 minutes"
is the bar). Tell the user where the report landed and surface the verdict line.
The report is opt-in and heavier than the auto-`summary.md`; it does not replace
it (the summary serves the delta/changelog; the report is the deep read).

## Gotchas

- **Compose-only is the contract.** If you ever want to WebSearch or add a number
  not in the artifacts, stop — note the gap in the report instead. A writing skill
  that enriches is silently re-doing analysis and re-opens the hallucination
  surface the artifacts already closed.
- **BQ↔thesis conflict is common and important** in this system (a strong BQ with
  an "overvalued / don't-chase" thesis). The report must explain the tension, not
  paper over it — the thesis `conflicts_resolved` and the valuation stance are
  where the resolution lives.
- **DL4 fail-close cohort** (MU/VSH/MRVL/CRWD/CRDO/RDW and other non-standard-FYE
  names): `valuation.json` may have no DCF / historical multiples. Compose the
  valuation section from what's there (peers + snapshot + scenarios) and state the
  limitation — never fabricate a fair value.
- This skill reads `reports/{TICKER}/{DATE}/`; it does not create or refresh
  analysis. If the data is stale, say so (cite the run date) — don't silently
  re-run anything.
