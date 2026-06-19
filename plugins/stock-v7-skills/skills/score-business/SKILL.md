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

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here. `<TICKER>` below is likewise substituted by you into EACH block (never
carried as a shell variable across blocks); each block re-runs the idempotent
`allocate-bq-run` (current run dir) / `find-latest-prior` (prior run dir) to
re-derive its dirs; and the one piece of COMPUTED cross-step state — the tier —
lives in the run-scoped state file `$REPORT_DIR/.run_state.json`, written once in
Step 2 and re-read by every later block.

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
"$PYBIN" -m scripts.version_skew --expected-min "1.2.4" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync
```

## Preflight: Money-path config

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.config_gate check
```

If it exits non-zero, STOP and show its stderr to the user (config not confirmed /
required API key missing) — do NOT run any analysis or produce numbers. Then continue
below.

## Output

- `bq_analysis.json` — Complete BQ analysis with all dimension scores,
  evidence, and synthesis (the canonical machine output, self-contained)
- `summary.md` — One-page human-readable summary (in output_language)

Intermediate files (in `scores/`) are working artifacts kept for traceability.

## Scripts Available

Every Bash block below first `cd`s to the captured root and runs scripts via
`"$PYBIN" -m scripts.<module>` (venv-or-python3 indirection).

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

### Step 0: Resolve prior, allocate today's run dir

**Validate the ticker symbol before anything else.** `$TICKER` and
values derived from it ($REPORT_DIR, $PRIOR_DIR) are interpolated into
`"$PYBIN" -c '...'` snippets in later steps. An unsanitized ticker
containing quotes / path separators / shell metacharacters could
escape the single-quoted snippet and execute arbitrary Python/shell.
Restrict to the actual US ticker vocabulary (letters + dot, 1-10 chars).
If this block exits non-zero (invalid ticker), STOP and tell the user.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"   # agent-substituted (e.g. AAPL) — substituted into every block, never exported across them
echo "$TICKER" | grep -Eq '^[A-Z][A-Z.]{0,9}$' \
  || { echo "FATAL: invalid ticker format: '$TICKER' (expected [A-Z][A-Z.]{0,9})" >&2; exit 1; }

REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
# find-latest-prior excludes today's ET dir by default (safe-by-construction
# guard against same-day self-comparison). No extra flags needed.
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)
printf 'REPORT_DIR=%s\nPRIOR_DIR=%s\n' "$REPORT_DIR" "$PRIOR_DIR"
```

Note the printed `REPORT_DIR` (relative to the repo root) and `PRIOR_DIR`
(possibly empty = first-time run). Later blocks RE-RUN the same idempotent
commands rather than relying on these variables; you use the printed values
for (1) the first-time-run branch in Step 2 and (2) composing absolute
subagent-dispatch paths as `<captured-abs-ROOT>/<REPORT_DIR>/...`.

Read `<captured-abs-ROOT>/strategy.yaml` for user preferences (output_language,
scoring weights). If `strategy.yaml` does not exist, use defaults:
output_language=zh-CN, weights fundamental=0.35/forward=0.35/industry=0.30.

### Step 1: Probe fetch (subset)

Fetch the probe-relevant categories only. News uses `--news-limit 10`
(Financial Datasets API's per-call ceiling; fetch.py clamps larger
values with a warning) which is enough articles for the materiality
classifier to judge.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
"$PYBIN" -m scripts.fetch -t "$TICKER" -o "$REPORT_DIR/data/" \
  --categories 01_price_data,02_financial_data,03_company_news,04_insider_data,06_analyst_estimates,07_earnings,09_macro_rates \
  --news-limit 10 \
  --tier-decided probe
```

### Step 2: Classifier + tier decision

The decided tier is the run's ONE piece of computed cross-step state: this step
persists it to `$REPORT_DIR/.run_state.json`, and Steps 4/4.5/5/6 re-read it
from there (fresh shells lose variables). If either block below exits non-zero,
STOP and surface the error.

**If `PRIOR_DIR` is empty (first-time run):** skip the classifier and force
tier=full — record it:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
"$PYBIN" -c "
import json, pathlib
pathlib.Path('$REPORT_DIR/.run_state.json').write_text(
    json.dumps({'tier': 'full'}, indent=2), encoding='utf-8')
print('TIER=full')
"
```

**Otherwise**, spawn a subagent with `<captured-abs-ROOT>/prompts/delta/classify-news.md`
as its instructions, passing articles from
`<captured-abs-ROOT>/<REPORT_DIR>/data/03_company_news.json` with
`since_date = <prior run's et_trading_day>`. The rubric is at
`<captured-abs-ROOT>/.claude/rules/delta-materiality.md`. Instruct it to WRITE its
output to `<captured-abs-ROOT>/<REPORT_DIR>/.classifier_output.json` — substitute
the concrete absolute path into the dispatch prompt (the subagent inherits neither
this shell's variables nor its cwd; `.json` writes are allowed for subagents).

Then decide the BQ tier via the dedicated CLI subcommand. The script
internally calls `build_bq_tier_inputs` (centralizes fail-open reads +
3-condition classifier health check) then `decide_bq_tier`:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)
TIER=$("$PYBIN" -m scripts.delta.probe decide-bq-tier \
  --report-dir "$REPORT_DIR" \
  --prior-dir "$PRIOR_DIR" \
  --classifier-output "$REPORT_DIR/.classifier_output.json")

# Validate capture (guards against silent empty-string propagation)
case "$TIER" in
    full|partial|no_op) ;;
    *) echo "FATAL: decide_bq_tier returned unexpected value: '$TIER'" >&2; exit 1 ;;
esac

# Persist the tier into the run-scoped state file for later fresh-shell blocks.
"$PYBIN" -c "
import json, pathlib
pathlib.Path('$REPORT_DIR/.run_state.json').write_text(
    json.dumps({'tier': '$TIER'}, indent=2), encoding='utf-8')
"
printf 'TIER=%s\n' "$TIER"
```

### Step 3: Tier-specific fetch + agents

**If `no_op`:** copy remaining categories + prior scores + prior dimensions from the prior run. Skip dim agents.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)
"$PYBIN" -c "
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
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)

# Save phase 1's validation before phase 2 fetch (merge in Step 4.5).
# Use a run-scoped transient dotfile in $REPORT_DIR, NOT /tmp/...$$ — the
# Step 3 save and the Step 4.5 merge run in SEPARATE shells (agent dispatch
# happens between them), so a $$ (PID) name would not match across calls.
cp "$REPORT_DIR/data/00_validation.json" "$REPORT_DIR/.validation_phase1.json"

"$PYBIN" -m scripts.fetch -t "$TICKER" -o "$REPORT_DIR/data/" \
  --categories 05_filing_summary,08_institutional \
  --tier-decided partial

# Copy fundamental dim from prior
"$PYBIN" -c "
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
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")

cp "$REPORT_DIR/data/00_validation.json" "$REPORT_DIR/.validation_phase1.json"

"$PYBIN" -m scripts.fetch -t "$TICKER" -o "$REPORT_DIR/data/" \
  --categories 05_filing_summary,08_institutional \
  --tier-decided full

"$PYBIN" -m scripts.indicators --price-json "$REPORT_DIR/data/01_price_data.json" \
  --output "$REPORT_DIR/data/indicators.json"

# Spawn all three agents (see below)
```

#### Agent dispatch (full + partial only)

Spawn agents in parallel:

```
Agent A (full only): <captured-abs-ROOT>/prompts/score-fundamental.md → <captured-abs-ROOT>/<REPORT_DIR>/scores/fundamental.json
Agent B: <captured-abs-ROOT>/prompts/score-forward.md → <captured-abs-ROOT>/<REPORT_DIR>/scores/forward.json
Agent C: <captured-abs-ROOT>/prompts/score-industry.md → <captured-abs-ROOT>/<REPORT_DIR>/scores/industry.json
```

Compose each dispatch prompt with **concrete absolute paths** (substitute the
captured root + the printed `REPORT_DIR`) — a subagent inherits neither this
shell's variables nor its cwd, and `.json` writes via the Write tool are allowed.
Agent inputs as in pre-delta (02_financial for fundamental; 06+03+05+07 for
forward; 02+03 + WebSearch for industry), read from
`<captured-abs-ROOT>/<REPORT_DIR>/data/`. Follow this input list verbatim — do
NOT add `00_validation.json` (between phase 2 and the Step 4.5 merge it is
phase-2-SKIPPED-stubbed and would mislead the agents).

The forward + industry agents MUST have WebSearch access — their prompts
carry a fail-closed preflight (one real WebSearch call before any
analysis; host lacks the tool → the agent reports
`cannot complete: host lacks WebSearch`). If either agent reports that,
STOP the run — never let a dim be scored from model memory. All agents
must emit WebSearch citations in the bound form
`[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]`; `scripts.assemble`
strict-validates the fresh dims and fail-closes on unbound tags.

### Step 4: Synthesis (with tier_context)

Write tier_context.yaml:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)
TIER=$("$PYBIN" -c "import json; print(json.load(open('$REPORT_DIR/.run_state.json', encoding='utf-8'))['tier'])")
TIER_CONTEXT_YAML="$REPORT_DIR/.tier_context.yaml"
cat > "$TIER_CONTEXT_YAML" <<TIER_CONTEXT_YAML_EOF
tier_context:
  tier: $TIER
  prior_synthesis_path: $PRIOR_DIR/synthesis.json
  pruned_catalyst_count: <from prior prune>
  low_signal_news_count: <from classifier>
  low_signal_headlines: <from classifier>
  dimensions_copied: <list of dims copied this run>
TIER_CONTEXT_YAML_EOF
```

Spawn the synthesis agent with `<captured-abs-ROOT>/prompts/score-synthesize.md`.
On no_op, the agent reads `prior_synthesis_path` and copies most fields verbatim,
emitting only a `delta_note`. Output:
`<captured-abs-ROOT>/<REPORT_DIR>/synthesis.json` +
`<captured-abs-ROOT>/<REPORT_DIR>/summary.md`.

> **Dispatch note (`.claude/rules/skill-architecture.md` #8):** the harness
> blocks subagent `.md` writes via the Write tool. Instruct the synthesis
> agent to write `summary.md` via a Bash heredoc with a content-unique quoted
> delimiter
> (`cat > "<captured-abs-ROOT>/reports/<TICKER>/<DATE>/summary.md" <<'SUMMARY_MD_EOF' … SUMMARY_MD_EOF`,
> UTF-8; NOT a bare `EOF`/`MD` — collision truncates) — `synthesis.json` writes
> fine via the Write tool. Substitute the ACTUAL ABSOLUTE path into the
> dispatch prompt — the subagent's Bash shell has no `$REPORT_DIR` and its cwd
> is ephemeral, so a relative `reports/…` path would land in the wrong place.

Hard-gate the synthesis deliverables before proceeding (mirrors
investment-thesis Step 6): a Bash-heredoc write can leave the file
missing/empty (wrong path, heredoc not run), and `summary.md` is what Step 8
links + the changelog distils, so it must fail-closed here, not surface later.
(The gate catches missing/empty only; a mid-file delimiter collision is
prevented by the content-unique sentinel above, not by this gate.) If the gate
exits non-zero, re-dispatch the synthesis agent ONCE; if it fails again, STOP
and surface the failure.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
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
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
TIER=$("$PYBIN" -c "import json; print(json.load(open('$REPORT_DIR/.run_state.json', encoding='utf-8'))['tier'])")
if [ "$TIER" != "no_op" ]; then
"$PYBIN" -m scripts.score_business.validation_merge \
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
and invoke assembler. If the assembler or the schema gate exits non-zero,
STOP and surface the error — do not proceed with a partial artifact.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
TIER=$("$PYBIN" -c "import json; print(json.load(open('$REPORT_DIR/.run_state.json', encoding='utf-8'))['tier'])")

# 1) Rewrite 00_validation.tier_decided to terminal tier
"$PYBIN" -c "
import json, pathlib
p = pathlib.Path('$REPORT_DIR/data/00_validation.json')
v = json.loads(p.read_text(encoding='utf-8'))
v['tier_decided'] = '$TIER'
p.write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding='utf-8')
"

# 2) Write tier-context-json (transient)
TIER_CONTEXT_JSON="$REPORT_DIR/.tier_context.json"
cat > "$TIER_CONTEXT_JSON" <<TIER_CONTEXT_JSON_EOF
{
  "tier_this_run": "$TIER",
  "component_provenance": {
    "dimensions.fundamental": {"source_date": "...", "reason": "fresh|copied from prior run"},
    "dimensions.forward":     {"source_date": "...", "reason": "..."},
    "dimensions.industry":    {"source_date": "...", "reason": "..."}
  }
}
TIER_CONTEXT_JSON_EOF

# 3) Run assembler (cross-checks both sources, aborts on mismatch)
"$PYBIN" -m scripts.assemble \
  --report-dir "$REPORT_DIR" \
  --tier-context-json "$TIER_CONTEXT_JSON"

rm "$TIER_CONTEXT_JSON"

# 4) Explicit fail-close schema validation. Mirrors investment-thesis
# Step 6.4 — bq_analysis.json must load through the typed loader before
# we declare success. Catches assemble bugs that produce malformed JSON
# (would silently propagate to downstream /investment-thesis and /portfolio).
"$PYBIN" -c "
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
Accumulate these across all agent invocations for this run (every LLM call
contributes its `usage.total_tokens`; wall time runs from Step 0 to here) and
substitute the totals into the heredoc below.

Compose `$AGENTS_RUN` from what ACTUALLY ran this run, not from the
tier alone. The **classifier** runs only when there was a prior run to
diff against — Step 2 spawns it iff `PRIOR_DIR` is non-empty, and skips
it entirely on a first-time run (where tier is forced to `full`). So it
must NOT be hardcoded into the list; prepend it only when `PRIOR_DIR` is
non-empty. (no_op + partial tiers always have a prior, so classifier is
always recorded there; only `full` can occur on a first-time run.)

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)
TIER=$("$PYBIN" -c "import json; print(json.load(open('$REPORT_DIR/.run_state.json', encoding='utf-8'))['tier'])")

# Run-scoped transient dotfile under $REPORT_DIR (NOT /tmp/...$$): portable
# across OSes (native Windows has no /tmp) and stable across step boundaries
# (a separate shell loses $$). $REPORT_DIR already isolates by ticker+date.
COSTS_FILE="$REPORT_DIR/.delta_costs.json"
cat > "$COSTS_FILE" <<COSTS_JSON_EOF
{"tokens": <accumulated total_tokens>, "duration_s": <wall seconds since Step 0>}
COSTS_JSON_EOF

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

"$PYBIN" -m scripts.delta.run_meta write \
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

Write the delta section to a file, then append. The header line is built
via `printf` (so `$TIER` and today's date interpolate); the free-prose
body uses a QUOTED heredoc — the delta note is agent-substituted prose
that may contain `$` («$NVDA», «$X.XB» — any digit after $ would be arg-substituted, so even this example avoids it) or backticks, which an unquoted
heredoc would silently expand / command-substitute:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business)
TIER=$("$PYBIN" -c "import json; print(json.load(open('$REPORT_DIR/.run_state.json', encoding='utf-8'))['tier'])")
TODAY_ET=$("$PYBIN" -c "from scripts.delta.calendar import session_et; print(session_et().isoformat())")
DELTA_FILE="$REPORT_DIR/.delta_section.md"
printf '## Update %s (tier: %s)\n\n' "$TODAY_ET" "$TIER" > "$DELTA_FILE"
cat >> "$DELTA_FILE" <<'DELTA_SECTION_EOF'
<one or two sentences from synthesis.delta_note>
DELTA_SECTION_EOF

"$PYBIN" -m scripts.delta.append_changelog \
  --prior "$PRIOR_DIR/summary.changelog.md" \
  --current "$REPORT_DIR/summary.changelog.md" \
  --ticker "$TICKER" \
  --delta-section "$DELTA_FILE"

rm "$DELTA_FILE"
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
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKER="<TICKER>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-bq-run --ticker "$TICKER")
# Existence-guard the gate (mirrors investment-thesis): a missing summary.md
# would make the inline interpreter call fail, leave WORDS empty, and turn the
# numeric test into a bash "integer expression expected" error. summary.md is
# a guaranteed Step 4 deliverable, so this is belt-and-suspenders.
if [ -s "$REPORT_DIR/summary.md" ]; then
    WORDS=$("$PYBIN" -c 'import sys; from scripts.cli_utils import count_word_equivalents; print(count_word_equivalents(open(sys.argv[1], encoding="utf-8").read()))' "$REPORT_DIR/summary.md")
    if [ "$WORDS" -gt 800 ]; then
        "$PYBIN" -m scripts.delta.run_meta warn \
          --run-dir "$REPORT_DIR" \
          --ticker "$TICKER" \
          --warning "summary.md exceeded 800 words"
    fi
fi
```

Do NOT abort — the word budget is a soft-fail per spec.

### Step 8: Report to user

Report: tier decided, what changed, link to
`<captured-abs-ROOT>/<REPORT_DIR>/summary.md` (absolute — the harness Read tool
does not follow the bash `cd`). Mention `/investment-thesis TICKER` for
next-step analysis.

### Gotchas

Read `<captured-abs-ROOT>/.claude/skills/score-business/gotchas.md` for known
failure patterns. Key ones:
- ADR stocks have unreliable per-share metrics — check `adr_correction.json`
- Pre-profit companies need adapted scoring (read
  `<captured-abs-ROOT>/prompts/references/growth-stock-analysis.md`)
- Multi-industry companies need explicit industry scoping
- API array order is per-TYPE, not per-source (see gotchas.md): price bars
  (`historical.daily`/`weekly`) are OLDEST-first → `[-N:]` for recent; but
  financial-statement arrays (`income_statements`/`cash_flows`/`balance_sheets`)
  are NEWEST-first → `[0]` = latest quarter, `[:4]` for TTM. Do NOT use `[-N:]`
  on statements (it returns the OLDEST rows). When unsure, sort by `report_period`.
