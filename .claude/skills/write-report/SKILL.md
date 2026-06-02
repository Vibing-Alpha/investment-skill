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

## Step 0: Validate ticker, locate the latest analyzed run

```bash
TICKER="AAPL"
if ! [[ "$TICKER" =~ ^[A-Z][A-Z.]{0,9}$ ]]; then
    echo "FATAL: invalid ticker format: '$TICKER'" >&2; exit 1
fi

# Most recent run dir that holds this ticker's analysis (include today).
REPORT_DIR=$(python3 -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
```

If `find-latest-prior` returns empty, there is no analysis to write from — tell
the user to run `/score-business {TICKER}` first and stop. Do not invent a report.

## Step 1: Verify required artifact + detect mode (fail-closed)

`bq_analysis.json` is **required** — it is the floor of any report. Verify it
loads through the typed loader before composing; a malformed artifact should fail
loudly here, not produce a garbage report.

```bash
python3 -c "
from scripts.schemas.bq_analysis import load_bq_analysis
import sys
try:
    art = load_bq_analysis('$REPORT_DIR/bq_analysis.json')
    print(f'OK bq: {art.meta.ticker} overall={art.scores.overall}', file=sys.stderr)
except Exception as e:
    print(f'FATAL: bq_analysis.json missing/invalid: {e}', file=sys.stderr); sys.exit(1)
" || exit 1
```

Then detect the **mode** by which optional artifacts are present in `$REPORT_DIR`:
- `investment_thesis.json` present → **full investment report** (BQ + valuation +
  timing + thesis verdict).
- absent → **business-quality report** (BQ only; the report says the thesis layer
  hasn't been run and is a quality read, not an entry decision).

Note which of `valuation.json`, `technical.json`, `events.json` exist — the writer
composes the sections it has data for and names the ones it doesn't (e.g. a DL4
fail-close name with no DCF). Never block on a missing optional artifact.

## Step 2: Compose the report

Read `prompts/write-report.md` (the methodology) and `strategy.yaml` for
`output_language` (default zh-CN). Read the present artifacts in `$REPORT_DIR`
(`bq_analysis.json` always; `investment_thesis.json`, `valuation.json`,
`technical.json`, `events.json`, and the existing `summary.md` when present).

Spawn the writing agent (or compose inline) with the prompt + the artifacts.
The agent writes the report — composing strictly from the artifacts, applying
three-layer fusion, avoiding the anti-patterns — to:

```
reports/{TICKER}/{YYYYMMDD}/report.md
```

> **Dispatch note (`.claude/rules/skill-architecture.md` #8):** if you dispatch
> a SUBAGENT to write `report.md`, the harness blocks subagent `.md` writes via
> the Write tool — instruct it to write the file via a Bash heredoc with a
> content-unique quoted delimiter
> (`cat > "reports/<TICKER>/<DATE>/report.md" <<'REPORT_MD_EOF' … REPORT_MD_EOF`, UTF-8;
> NOT a bare `EOF`/`MD` — collision truncates; substitute the ACTUAL `reports/…`
> path — the subagent shell has no `$REPORT_DIR`). If the lead composes inline,
> the Write tool works (no subagent guard). Either way, hard-gate it before
> reporting success (catches a missing/empty `report.md`):
>
> ```bash
> [ -s "$REPORT_DIR/report.md" ] \
>   || { echo "FATAL: report.md was not produced — re-dispatch / re-compose" >&2; exit 1; }
> ```

Use the same `{YYYYMMDD}` as `$REPORT_DIR` (the report describes that run's
analysis; it does not start a new analysis run).

## Step 3: Self-check + report to user

The writing prompt ends with a self-check (traceability, no orphaned tables, no
hollow conclusions, verdict consistency, length). Confirm the report passed it.

Optionally note the length using the CJK-aware counter (the report is human-facing
prose; `wc -w` undercounts zh-CN ~3x):

```bash
python3 -c 'import sys; from scripts.cli_utils import count_word_equivalents; print(count_word_equivalents(open(sys.argv[1], encoding="utf-8").read()))' "reports/$TICKER/$DATE/report.md"
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
