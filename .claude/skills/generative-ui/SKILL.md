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

## Step 0: Pick the mode + build the view-model

```bash
RUN_DATE=$(python3 -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y-%m-%d'))")
ARG="AMD"   # the user's argument: a TICKER, or "portfolio"/"all"/empty for the whole book
```

**Portfolio mode** — when `ARG` is empty, `portfolio`, `all`, `组合`, or `持仓` (case-insensitive):
visualize the whole portfolio-state universe (holdings + watchlist).

```bash
REPORT_DIR="reports/portfolio/$(echo "$RUN_DATE" | tr -d '-')"
mkdir -p "$REPORT_DIR"
python3 -m scripts.viz build-portfolio-view-model --state portfolio-state.yaml \
  --output "$REPORT_DIR/portfolio_view_model.json" --run-date "$RUN_DATE"
VM="$REPORT_DIR/portfolio_view_model.json"; OUT="$REPORT_DIR/portfolio_dashboard.html"
```
This fails closed (non-zero) if `portfolio-state.yaml` is empty/missing — tell the user and stop.
A ticker with no analysis is included as `analyzed: false` (never dropped or fabricated).

**Single-ticker mode** — when `ARG` matches `^[A-Z][A-Z.]{0,9}$` (and isn't a mode keyword):

```bash
TICKER="$ARG"
REPORT_DIR=$(python3 -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
[ -z "$REPORT_DIR" ] && { echo "No analysis for $TICKER — run /score-business $TICKER first" >&2; exit 1; }
python3 -m scripts.viz build-view-model --ticker "$TICKER" \
  --report-dir "$REPORT_DIR" --output "$REPORT_DIR/view_model.json"
VM="$REPORT_DIR/view_model.json"; OUT="$REPORT_DIR/dashboard.html"
```
If `find-latest-prior` is empty there is nothing to visualize — tell the user to run
`/score-business {TICKER}` first and stop. Do not invent a dashboard.

## Step 1: Generate the dashboard (the "generative" step)

First clear any stale dashboard so the existence gate proves THIS run produced a fresh one (the
report dir is reused across runs):

```bash
rm -f "$OUT"
```

Dispatch a subagent with `prompts/generative-ui.md` as its instructions and the **concrete
resolved** view-model path as its sole input. **CRITICAL — a subagent does NOT inherit this shell's
`$VM`/`$OUT`** (`.claude/rules/skill-architecture.md` #8): when you compose the dispatch prompt,
SUBSTITUTE the literal resolved paths — e.g. read `reports/portfolio/20260601/portfolio_view_model.json`
and WRITE `reports/portfolio/20260601/portfolio_dashboard.html` — never a literal `$VM`/`$OUT`
string. Writing `.html` is allowed for subagents (only the `.md`-write tool is blocked; if it ever
refuses, write via a Bash heredoc with a quoted, content-unique sentinel). The generator reads ONLY
the view-model and dispatches on its `kind` (`ticker` vs `portfolio`). After dispatch, HARD
existence gate:

```bash
[ -s "$OUT" ] || { echo "FATAL: generator produced no dashboard" >&2; exit 1; }
```

## Step 2: Verify data fidelity (HARD gate)

```bash
python3 -m scripts.viz verify --html "$OUT" --view-model "$VM"
```
Non-zero means the dashboard embeds data that drifted from the view-model, or hardcodes a data
number in static markup, or is missing/duplicating the embedded block. Do NOT hand it to the user.
**Exactly ONE repair attempt:** re-dispatch the SAME generator prompt with the SAME concrete
view-model path plus the verifier's stderr appended ("your dashboard failed fidelity verification:
<stderr> — embed the view-model verbatim in `<script id=\"v7-view-model\" type=\"application/json\">`
and render every number from it via JS; put no data literal in static markup"), then re-run
`verify` once. If it fails a SECOND time, surface the stderr and STOP — never hand the user a
dashboard whose numbers don't trace to the source.

## Step 3: Hand off

Tell the user the dashboard is ready and give the path to open in a browser (`$OUT`). It is a view
of existing machine verdicts — it decides nothing; point them to `/portfolio` for any action. In
portfolio mode, note that prices are each name's last-analysis price (not live; see the dashboard's
own caveat).
