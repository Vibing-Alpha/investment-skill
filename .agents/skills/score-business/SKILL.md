---
name: score-business
description: |
  Score a US stock's business quality (BQ). Use this skill whenever the user
  mentions a ticker symbol and wants to understand the company, even casually.
  Trigger phrases include "analyze TICKER", "score TICKER", "look at XXX",
  "what about this stock", "BQ analysis", "is this a good company",
  "tell me about TICKER", "research TICKER", or any request to evaluate whether
  a company is worth watching. Also trigger when the user pastes a ticker and
  asks "thoughts?" or similar short prompts.
  NOT for ETFs (no ETF support in v7), NOT for buy/sell timing decisions,
  NOT for portfolio-level work (use portfolio).
user_invocable: true
---

# Score Business — Business Quality Assessment

Evaluate a company's intrinsic business quality across three dimensions:
fundamental strength, forward trajectory, and industry position.

This skill answers ONE question: **Is this business worth watching?**
It does NOT answer: "Should I buy now?" or "What price to pay?" — those
belong to the timing/portfolio layer.

## Output

- `bq_analysis.json` — Complete BQ analysis with all dimension scores,
  evidence, and synthesis (the canonical machine output, self-contained)
- `summary.md` — One-page human-readable summary (in output_language)

Intermediate files (in `scores/`) are working artifacts kept for traceability.

## Scripts Available

All scripts run from the project root using `python3 -m scripts.<module>`.

| Script | Purpose | CLI |
|--------|---------|-----|
| `scripts.delta.resolver` | Locate prior runs / allocate today's run dir | `find-latest-prior` / `allocate-bq-run` |
| `scripts.delta.run_meta` | Write per-run audit state | `write` subcommand |
| `scripts.delta.append_changelog` | Append delta section to summary.changelog.md | CLI |
| `scripts.fetch` | Fetch data (12 categories, with `--categories` + `--tier-decided` gating) | `-t TICKER -o DIR [--categories ...] [--news-limit N] --tier-decided {probe,full,partial,no_op}` |
| `scripts.indicators` | MACD/BB/ATR/RSI/Volume | `--price-json PATH --output PATH` |
| `scripts.assemble` | Assemble bq_analysis.json (with --tier-context-json + tier validation) | `--report-dir DIR --tier-context-json PATH` |

Data sources (called internally by fetch):
- Financial Datasets API (primary) — requires `FINANCIAL_DATASETS_API_KEY` in `.env`
- Yahoo Finance v8 (price data, yfinance fallback)
- FMP (filing metadata) — requires `FMP_API_KEY` in `.env`
- SEC EDGAR (direct filing download)
- Finnhub (news fallback) — optional `FINNHUB_API_KEY` in `.env`

## Execution (delta-era)

### Step 0: Resolve prior, allocate today's run dir, decide tier

**Validate the ticker symbol before anything else.** `$TICKER` and
values derived from it ($REPORT_DIR, $PRIOR_DIR) are interpolated into
`python3 -c '...'` heredocs in later steps. An unsanitized ticker
containing quotes / path separators / shell metacharacters could
escape the single-quoted heredoc and execute arbitrary Python/shell.
Restrict to the actual US ticker vocabulary (letters + dot, 1-10 chars):

```bash
TICKER="AAPL"
if ! [[ "$TICKER" =~ ^[A-Z][A-Z.]{0,9}$ ]]; then
    echo "FATAL: invalid ticker format: '$TICKER' (expected [A-Z][A-Z.]{0,9})" >&2
    exit 1
fi

REPORT_DIR=$(python3 -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
# find-latest-prior excludes today's ET dir by default (safe-by-construction
# guard against same-day self-comparison). No extra flags needed.
PRIOR_DIR=$(python3 -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)
```

Read `strategy.yaml` for user preferences (output_language, scoring weights).
If `strategy.yaml` does not exist, use defaults: output_language=zh-CN,
weights fundamental=0.35/forward=0.35/industry=0.30.

### Step 1: Probe fetch (subset)

Fetch the probe-relevant categories only. News uses `--news-limit 10`
(Financial Datasets API's per-call ceiling; fetch.py clamps larger
values with a warning) which is enough articles for the materiality
classifier to judge.

```bash
python3 -m scripts.fetch -t "$TICKER" -o "$REPORT_DIR/data/" \
  --categories 01_price_data,02_financial_data,03_company_news,04_insider_data,06_analyst_estimates,07_earnings,09_macro_rates \
  --news-limit 10 \
  --tier-decided probe
```

### Step 2: Classifier + tier decision

If `PRIOR_DIR` is empty (first-time run), skip classifier and force tier=full.

Otherwise, spawn a subagent with `prompts/delta/classify-news.md` passing
articles from `$REPORT_DIR/data/03_company_news.json` with
`since_date = <prior run's et_trading_day>`. The rubric is at
`.claude/rules/delta-materiality.md`.

Decide the BQ tier via the dedicated CLI subcommand. The script
internally calls `build_bq_tier_inputs` (centralizes fail-open reads +
3-condition classifier health check) then `decide_bq_tier`. Wrap in
`TIER=$(...)` so downstream steps see the decision.

```bash
TIER=$(python3 -m scripts.delta.probe decide-bq-tier \
  --report-dir "$REPORT_DIR" \
  --prior-dir "$PRIOR_DIR" \
  --classifier-output "$REPORT_DIR/.classifier_output.json")

# Validate capture (guards against silent empty-string propagation)
case "$TIER" in
    full|partial|no_op) ;;
    *) echo "FATAL: decide_bq_tier returned unexpected value: '$TIER'" >&2; exit 1 ;;
esac
```

### Step 3: Tier-specific fetch + agents

**If `no_op`:** copy remaining categories + prior scores + prior dimensions from `$PRIOR_DIR`. Skip dim agents.

```bash
python3 -c "
from scripts.delta.copy_data import copy_data_categories, copy_dimension_scores
from pathlib import Path
# Copy data files that weren't fetched in probe
copy_data_categories(
    src_dir=Path('$PRIOR_DIR/data'), dst_dir=Path('$REPORT_DIR/data'),
    categories=['05_filing_*', '08_institutional', 'adr_profile'],
)
# Copy all three dim scores with provenance stamp
copy_dimension_scores(
    src_dir=Path('$PRIOR_DIR/scores'), dst_dir=Path('$REPORT_DIR/scores'),
    dimensions=['fundamental', 'forward', 'industry'],
    source_date='<prior ET date>',
)
"
```

**If `partial`:** fetch `05_filing_summary`; copy `fundamental` dim; spawn forward + industry score agents.

```bash
# Save phase 1's validation before phase 2 fetch (merge in Step 4.5).
# Use a run-scoped transient dotfile in $REPORT_DIR, NOT /tmp/...$$ — the
# Step 3 save and the Step 4.5 merge run in SEPARATE shells (agent dispatch
# happens between them), so a $$ (PID) name would not match across calls.
cp "$REPORT_DIR/data/00_validation.json" "$REPORT_DIR/.validation_phase1.json"

python3 -m scripts.fetch -t "$TICKER" -o "$REPORT_DIR/data/" \
  --categories 05_filing_summary,08_institutional \
  --tier-decided partial

# Copy fundamental dim from prior
python3 -c "
from scripts.delta.copy_data import copy_dimension_scores
from pathlib import Path
copy_dimension_scores(
    src_dir=Path('$PRIOR_DIR/scores'), dst_dir=Path('$REPORT_DIR/scores'),
    dimensions=['fundamental'], source_date='<prior ET date>',
)
"

# Spawn forward + industry agents (see below)
```

**If `full`:** fetch all remaining categories; run indicators; spawn fundamental + forward + industry agents.

```bash
cp "$REPORT_DIR/data/00_validation.json" "$REPORT_DIR/.validation_phase1.json"

python3 -m scripts.fetch -t "$TICKER" -o "$REPORT_DIR/data/" \
  --categories 05_filing_summary,08_institutional \
  --tier-decided full

python3 -m scripts.indicators --price-json "$REPORT_DIR/data/01_price_data.json" \
  --output "$REPORT_DIR/data/indicators.json"

# Spawn all three agents (see below)
```

#### Agent dispatch (full + partial only)

Spawn agents in parallel:

```
Agent A (full only): prompts/score-fundamental.md → $REPORT_DIR/scores/fundamental.json
Agent B: prompts/score-forward.md → $REPORT_DIR/scores/forward.json
Agent C: prompts/score-industry.md → $REPORT_DIR/scores/industry.json
```

Agent inputs as in pre-delta (02_financial for fundamental; 06+03+05+07 for forward; 02+03 + WebSearch for industry).

### Step 4: Synthesis (with tier_context)

Write tier_context.yaml and spawn synthesis agent:

```bash
cat > "$REPORT_DIR/.tier_context.yaml" <<EOF
tier_context:
  tier: $TIER
  prior_synthesis_path: $PRIOR_DIR/synthesis.json
  pruned_catalyst_count: <from prior prune>
  low_signal_news_count: <from classifier>
  low_signal_headlines: <from classifier>
  dimensions_copied: <list of dims copied this run>
EOF
```

Spawn synthesis agent with `prompts/score-synthesize.md`. On no_op, the
agent reads `prior_synthesis_path` and copies most fields verbatim, emitting
only a `delta_note`. Output: `$REPORT_DIR/synthesis.json` +
`$REPORT_DIR/summary.md`.

> **Dispatch note (`.claude/rules/skill-architecture.md` #8):** the harness
> blocks subagent `.md` writes via the Write tool. Instruct the synthesis
> agent to write `summary.md` via a Bash heredoc with a content-unique quoted
> delimiter (`cat > "reports/<TICKER>/<DATE>/summary.md" <<'SUMMARY_MD_EOF' … SUMMARY_MD_EOF`,
> UTF-8; NOT a bare `EOF`/`MD` — collision truncates) — `synthesis.json` writes
> fine via the Write tool. Substitute the ACTUAL `reports/…` path into the
> dispatch prompt — the subagent's Bash shell has no `$REPORT_DIR`.

Hard-gate the synthesis `.md` deliverable before proceeding (mirrors
investment-thesis Step 6): a Bash-heredoc write can leave the file
missing/empty (wrong path, heredoc not run), and `summary.md` is what Step 8
links + the changelog distils, so it must fail-closed here, not surface later.
(The gate catches missing/empty only; a mid-file delimiter collision is
prevented by the content-unique sentinel above, not by this gate.)

```bash
[ -s "$REPORT_DIR/summary.md" ] \
  || { echo "FATAL: synthesis produced no summary.md — re-dispatch synthesis agent" >&2; exit 1; }
[ -s "$REPORT_DIR/synthesis.json" ] \
  || { echo "FATAL: synthesis produced no synthesis.json — re-dispatch synthesis agent" >&2; exit 1; }
```

### Step 4.5: Merge two-phase validation (before assemble)

If tier > no_op, phase 2 overwrote `00_validation.json` with only its own
category_statuses. Merge phase 1's entries back so assembler's
`build_meta` can read `categories.financials.latest_period`:

```bash
if [ "$TIER" != "no_op" ]; then
python3 -m scripts.score_business.validation_merge \
    --phase1 "$REPORT_DIR/.validation_phase1.json" \
    --phase2 "$REPORT_DIR/data/00_validation.json"
# In-place merge: phase 2's SKIPPED stubs that would have clobbered
# phase 1's live entries are reverted to phase 1's PASSED/WARN data.
# Top-level fields (tier_decided, validated_at) keep phase 2's value
# as terminal truth.
rm -f "$REPORT_DIR/.validation_phase1.json"
fi
```

### Step 5: Assemble

Rewrite 00_validation.tier_decided from "probe" to the terminal TIER
(needed on no_op, where no phase 2 ran). Write transient tier_context.json
and invoke assembler:

```bash
# 1) Rewrite 00_validation.tier_decided to terminal tier
python3 -c "
import json, pathlib
p = pathlib.Path('$REPORT_DIR/data/00_validation.json')
v = json.loads(p.read_text())
v['tier_decided'] = '$TIER'
p.write_text(json.dumps(v, indent=2, ensure_ascii=False))
"

# 2) Write tier-context-json (transient)
cat > "$REPORT_DIR/.tier_context.json" <<EOF
{
  "tier_this_run": "$TIER",
  "component_provenance": {
    "dimensions.fundamental": {"source_date": "...", "reason": "fresh|copied from prior run"},
    "dimensions.forward":     {"source_date": "...", "reason": "..."},
    "dimensions.industry":    {"source_date": "...", "reason": "..."}
  }
}
EOF

# 3) Run assembler (cross-checks both sources, aborts on mismatch)
python3 -m scripts.assemble \
  --report-dir "$REPORT_DIR" \
  --tier-context-json "$REPORT_DIR/.tier_context.json"

rm "$REPORT_DIR/.tier_context.json"

# 4) Explicit fail-close schema validation. Mirrors investment-thesis
# Step 6.4 — bq_analysis.json must load through the typed loader before
# we declare success. Catches assemble bugs that produce malformed JSON
# (would silently propagate to downstream /investment-thesis and /portfolio).
python3 -c "
from scripts.schemas.bq_analysis import load_bq_analysis
import sys
try:
    art = load_bq_analysis('$REPORT_DIR/bq_analysis.json')
    print(f'OK: {art.meta.ticker} overall={art.scores.overall}', file=sys.stderr)
except Exception as e:
    print(f'FATAL: bq_analysis.json failed schema validation: {e}', file=sys.stderr)
    sys.exit(1)
" || exit 1
```

`scripts.assemble` now reads `strategy.yaml.scoring.dimension_weights`
automatically when `--weights` is not passed. Precedence:
`--weights` CLI > `strategy.yaml` > `DEFAULT_WEIGHTS` (0.35 / 0.35 /
0.30). A malformed / partial strategy block falls back to defaults.

Single-dimension runs **fail closed** (exit 1, no `bq_analysis.json`
written) — at least 2 of 3 dimensions
(`fundamental` / `forward` / `industry`) must be present in
`$REPORT_DIR/scores/` for a valid BQ verdict. If a dim agent failed,
re-run the skill; do not attempt to patch around the missing dimension.

### Step 6: Write run_meta.json and append changelog

#### Cost accumulation

Each Task subagent call returns a usage block with `total_tokens`.
Accumulate these across all agent invocations for this run into a
single cost dict, then pass to `run_meta write`:

```bash
# Pseudocode — orchestrator accumulates per-call token counts + wall time
# into $COSTS_FILE with shape {"tokens": N, "duration_s": N}.
# Every LLM call this run contributes its usage.total_tokens to "tokens".
# Start timer at Step 0, close it here.
#
# Run-scoped transient dotfile under $REPORT_DIR (NOT /tmp/...$$): portable
# across OSes (native Windows has no /tmp) and stable across step boundaries
# (a separate shell loses $$). $REPORT_DIR already isolates by ticker+date.
COSTS_FILE="$REPORT_DIR/.delta_costs.json"
```

Compose `$AGENTS_RUN` from what ACTUALLY ran this run, not from the
tier alone. The **classifier** runs only when there was a prior run to
diff against — Step 2 spawns it iff `PRIOR_DIR` is non-empty, and skips
it entirely on a first-time run (where tier is forced to `full`). So it
must NOT be hardcoded into the list; prepend it only when `PRIOR_DIR` is
non-empty. (no_op + partial tiers always have a prior, so classifier is
always recorded there; only `full` can occur on a first-time run.)

```bash
case "$TIER" in
    no_op)   AGENTS_RUN="synthesis_light" ;;
    partial) AGENTS_RUN="forward,industry,synthesis" ;;
    full)    AGENTS_RUN="fundamental,forward,industry,synthesis" ;;
esac

# Prepend classifier ONLY if it was actually invoked. Step 2 spawns it iff
# PRIOR_DIR is non-empty; a first-time run skips it (and forces tier=full).
# Gate on PRIOR_DIR (not the .classifier_output.json file) so a classifier
# that ran but failed to write its output is still recorded — it was invoked
# and incurred cost, and agents_run is a write-only cost/audit trail.
if [ -n "$PRIOR_DIR" ]; then
    AGENTS_RUN="classifier,${AGENTS_RUN}"
fi

python3 -m scripts.delta.run_meta write \
  --run-dir "$REPORT_DIR" \
  --ticker "$TICKER" \
  --skill score-business \
  --tier "$TIER" \
  --agents-run "$AGENTS_RUN" \
  --data-fetched "01_price_data,02_financial_data,03_company_news,..." \
  --data-copied-from-prior "05_filing_summary,08_institutional" \
  --prior-source "$PRIOR_DIR" \
  --cost-json "$COSTS_FILE"
# NOTE: the two --data-* values above are TIER-DEPENDENT — the example shows
# (part of) the no_op case, where phase 2 is skipped and 05/08 are copied from
# prior. The full no_op copied set is 05_filing_summary,08_institutional,adr_profile
# (see Step 3 no_op). On partial/full, phase 2 FETCHES 05_filing_summary +
# 08_institutional, so add them to --data-fetched and pass
# --data-copied-from-prior "" (nothing copied). Passing the no_op values on a
# full run records a cosmetically wrong provenance trail. (run_meta provenance
# is write-only audit — no consumer reads it — but keep it accurate.)
```

Write the delta section to a file, then append. Use an unquoted heredoc
so `$TIER` and today's date interpolate:

```bash
TODAY_ET=$(python3 -c "from scripts.delta.calendar import session_et; print(session_et().isoformat())")
cat > "$REPORT_DIR/.delta_section.md" <<EOF
## Update $TODAY_ET (tier: $TIER)

<one or two sentences from synthesis.delta_note>
EOF

python3 -m scripts.delta.append_changelog \
  --prior "$PRIOR_DIR/summary.changelog.md" \
  --current "$REPORT_DIR/summary.changelog.md" \
  --ticker "$TICKER" \
  --delta-section "$REPORT_DIR/.delta_section.md"

rm "$REPORT_DIR/.delta_section.md"
```

### Step 7: Validation (soft-fail word budget)

Check `summary.md` word count. If >800, append a warning via the
standalone `warn` subcommand (which preserves the bq/thesis sections
— do NOT use `run_meta write` as a follow-up since that would clobber
the section with partial args).

Use `cli_utils.count_word_equivalents`, NOT `wc -w`: the default
`output_language` is zh-CN and CJK text has no inter-word spaces, so
`wc -w` undercounts Chinese ~3x and the gate would never fire. The
helper counts non-CJK tokens (== `wc -w` for English) + CJK chars/2:

```bash
# Existence-guard the gate (mirrors investment-thesis): a missing summary.md
# would make the `python3 -c` fail, leave WORDS empty, and turn the numeric
# test into a bash "integer expression expected" error. summary.md is a
# guaranteed Step 4 deliverable, so this is belt-and-suspenders.
if [ -s "$REPORT_DIR/summary.md" ]; then
    WORDS=$(python3 -c 'import sys; from scripts.cli_utils import count_word_equivalents; print(count_word_equivalents(open(sys.argv[1], encoding="utf-8").read()))' "$REPORT_DIR/summary.md")
    if [ "$WORDS" -gt 800 ]; then
        python3 -m scripts.delta.run_meta warn \
          --run-dir "$REPORT_DIR" \
          --ticker "$TICKER" \
          --warning "summary.md exceeded 800 words"
    fi
fi
```

Do NOT abort — the word budget is a soft-fail per spec.

### Step 8: Report to user

Report: tier decided, what changed, link to summary.md. Mention
`/investment-thesis TICKER` for next-step analysis.

### Gotchas

Read `score-business/gotchas.md` for known failure patterns. Key ones:
- ADR stocks have unreliable per-share metrics — check `adr_correction.json`
- Pre-profit companies need adapted scoring (read growth-stock-analysis.md)
- Multi-industry companies need explicit industry scoping
- API array order is per-TYPE, not per-source (see gotchas.md): price bars
  (`historical.daily`/`weekly`) are OLDEST-first → `[-N:]` for recent; but
  financial-statement arrays (`income_statements`/`cash_flows`/`balance_sheets`)
  are NEWEST-first → `[0]` = latest quarter, `[:4]` for TTM. Do NOT use `[-N:]`
  on statements (it returns the OLDEST rows). When unsure, sort by `report_period`.
