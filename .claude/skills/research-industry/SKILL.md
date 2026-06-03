---
name: research-industry
description: |
  Research a US-stock industry/sector and surface 5-12 stock-picking candidate
  tickers that downstream /score-business can drill into. Use this skill whenever
  the user names an industry or sector and wants to know "which stocks in this
  space should I look at" — e.g., "research AI chips", "找一下网络安全行业的票",
  "what are the players in regional banks", "云计算选股", "screen the
  cybersecurity sector", "industry research on EV", "看看半导体里有哪些值得研究的".
  Output: industry_analysis.json (strict schema, feeds /score-business) +
  summary.md (300-800 word human brief). Sister skill to /score-business — runs
  BEFORE it to scope the universe to candidates.
  NOT for analyzing one specific known ticker (use /score-business).
  NOT for portfolio-level allocation (use /portfolio).
  NOT for price-action screening (use /screen-stocks).
user_invocable: true
---

# Research Industry — Industry-Level Stock-Picking Scout

Turn an industry name into a ranked list of stock-picking candidate tickers
plus a brief human-readable summary. The candidates feed `/score-business`
for per-ticker BQ scoring.

This SKILL.md is an **orchestration adapter** — analysis methodology lives
in `prompts/research-industry.md`, hard constraints in `rules/research-industry.md`,
deterministic computation in `scripts/industry/*.py` + `scripts/sector_signal.py`.

## Output

Four artifacts in `reports/industry/<slug>/<YYYYMMDD>/`:

| File | Purpose | Schema |
|---|---|---|
| `industry_analysis.json` | Machine output, /score-business consumer | `scripts/schemas/industry_analysis.py` |
| `summary.md` | Human brief, 300-800 words target | Markdown template in `prompts/research-industry.md` |
| `summary.changelog.md` | Append-only delta log | Markdown |
| `run_meta.json` | Delta audit state | `scripts/delta/run_meta.py:IndustrySection` |

## Scripts

All CLI from project root via `python3 -m scripts.<module>`.

| Script | Purpose |
|---|---|
| `scripts.industry.normalize_slug` | `--industry "AI芯片"` → `{industry_name, slug}` JSON |
| `scripts.industry.sector_etf_map` | `--slug ai-chips` → `{etf, proxy_note}` JSON |
| `scripts.industry.decide_tier` | `--prior-dir PATH [--force-refresh]` → `full|partial|no_op` |
| `scripts.sector_signal` | `--etf SOXX --output PATH` — multi-window trend JSON |
| `scripts.delta.resolver` | `allocate-industry-run --slug` / `find-latest-prior` |
| `scripts.delta.run_meta` | `write --skill research-industry --tier ...` |
| `scripts.delta.append_changelog` | Append delta section to changelog |
| `scripts.schemas.industry_analysis.load_industry_analysis` | Validate written JSON |

## Execution

### Step 0: Validate input + normalize slug + allocate run dir

```bash
RAW_INDUSTRY="$1"
if [ -z "$RAW_INDUSTRY" ]; then
    echo "FATAL: research-industry requires an industry name argument" >&2
    exit 1
fi
case "$RAW_INDUSTRY" in
    *\`*|*\$*|*\;*|*\|*|*\&*|*\<*|*\>*|*\(*|*\)*|*\{*|*\}*|*\\*|*\"*|*\'*)
        echo "FATAL: industry name contains shell metacharacters: '$RAW_INDUSTRY'" >&2
        exit 1 ;;
esac

NORM=$(python3 -m scripts.industry.normalize_slug --industry "$RAW_INDUSTRY") || exit $?
INDUSTRY_NAME=$(echo "$NORM" | python3 -c "import json,sys; print(json.load(sys.stdin)['industry_name'])")
SLUG=$(echo "$NORM" | python3 -c "import json,sys; print(json.load(sys.stdin)['slug'])")

REPORT_DIR=$(python3 -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
PRIOR_DIR=$(python3 -m scripts.delta.resolver find-latest-prior \
  --ticker "industry/$SLUG" --skill research-industry)
```

If `normalize_slug` exits 2, the input was non-ASCII and not in the alias
table. Surface the stderr to the user with the suggestion to either
pre-translate or extend `scripts/industry/normalize_slug.py:_CJK_ALIASES`.

Read `strategy.yaml:output_language` (default `zh-CN`).

### Step 1: Decide tier

```bash
# Detect user force-refresh from invocation context. Heuristics: presence of
# "重新" / "refresh" / "再跑一遍" / "from scratch" / "fresh" in the prompt.
# Default to false; orchestrator sets to true when applicable.
FORCE_FLAG=""
# FORCE_FLAG="--force-refresh"  # uncomment when force-refresh detected

TIER=$(python3 -m scripts.industry.decide_tier --prior-dir "$PRIOR_DIR" $FORCE_FLAG)
case "$TIER" in
    full|partial|no_op) ;;
    *) echo "FATAL: decide_tier returned invalid: '$TIER'" >&2; exit 1 ;;
esac
```

### Step 2: Fetch sector ETF signal

```bash
ETF_JSON=$(python3 -m scripts.industry.sector_etf_map --slug "$SLUG")
ETF=$(echo "$ETF_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['etf'])")
PROXY_NOTE=$(echo "$ETF_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['proxy_note'])")

python3 -m scripts.sector_signal --etf "$ETF" \
    --output "$REPORT_DIR/data/sector_etf_trends.json"
```

`proxy_note` is non-empty when the ETF is a thematic proxy (e.g. SOXX for
MLCC). Pass it to the agent so the regime_rationale can acknowledge it.

### Step 3: Tier-specific work

**If `no_op`**: copy prior JSON, regenerate `summary.md` only (with delta note).

```bash
cp "$PRIOR_DIR/industry_analysis.json" "$REPORT_DIR/industry_analysis.json"
# Then dispatch summary subagent in no_op mode (see Step 4).
```

**If `partial`**: dispatch the framing-refresh + selection agent. Pass prior
`candidate_tickers` as a hint; agent may add / remove / re-rank.

**If `full`**: dispatch the full 4-phase research agent.

#### Agent dispatch (full + partial)

Spawn ONE subagent that reads `prompts/research-industry.md`, plus the
inputs assembled at `$REPORT_DIR/.tier_context.json`:

```bash
python3 -c "
import json
ctx = {
    'tier': '$TIER',
    'prior_source_date': '$(basename $PRIOR_DIR 2>/dev/null || echo)',
    'industry_name': '$INDUSTRY_NAME',
    'slug': '$SLUG',
    'output_language': '$OUTPUT_LANGUAGE',
    'analysis_date': '$(python3 -c \"from scripts.delta.calendar import session_et; print(session_et().isoformat())\")',
    'sector_etf': {'symbol': '$ETF', 'proxy_note': '''$PROXY_NOTE'''},
    'user_force_refresh': bool('$FORCE_FLAG'),
}
import pathlib
pathlib.Path('$REPORT_DIR/.tier_context.json').write_text(json.dumps(ctx, indent=2), encoding='utf-8')
"
```

Agent has WebSearch access. Must produce `industry_analysis.json` matching
`scripts/schemas/industry_analysis.py`. The hard constraints in
`rules/research-industry.md` (universe, source tags, dispersion, OTC ADR,
overextended regime) are enforced via schema + prompt; the agent reads
the prompt and the orchestrator validates the result.

### Step 4: Validate JSON + generate summary.md

```bash
python3 -c "
from scripts.schemas.industry_analysis import load_industry_analysis
import sys
try:
    art = load_industry_analysis('$REPORT_DIR/industry_analysis.json')
    print(f'OK: {art.meta.industry_name} mode={art.meta.research_mode} candidates={len(art.candidate_tickers)}')
except Exception as e:
    print(f'FATAL: industry_analysis.json failed validation: {e}', file=sys.stderr)
    sys.exit(1)
" || exit 1
```

On validation failure, re-dispatch the agent with the SchemaError message
inlined. Up to 2 retries; on 3rd failure, surface error.

Generate `summary.md` via a separate light subagent reading the validated
JSON + `output_language`. Word count is target ≤1200, not strict gate.

> **Dispatch note (`.claude/rules/skill-architecture.md` #8):** the harness
> blocks subagent `.md` writes via the Write tool. Instruct this subagent to
> write `summary.md` via a Bash heredoc with a content-unique quoted delimiter
> (`cat > "reports/<SLUG>/<DATE>/summary.md" <<'SUMMARY_MD_EOF' … SUMMARY_MD_EOF`, UTF-8;
> NOT a bare `EOF`/`MD` — collision truncates; substitute the ACTUAL `reports/…`
> path — the subagent shell has no `$REPORT_DIR`), not the Write tool. Then
> hard-gate it (catches a missing/empty `summary.md`, which feeds the changelog
> + user output):
>
> ```bash
> [ -s "$REPORT_DIR/summary.md" ] \
>   || { echo "FATAL: summary subagent produced no summary.md — re-dispatch" >&2; exit 1; }
> ```

### Step 5: Write run_meta + append changelog

```bash
# F15 (codex review cycle 2): slug-specific mktemp template so two parallel
# /research-industry runs in the same shell don't collide on a $$-based path.
# Full-path template with TRAILING Xs (NOT `mktemp -t "...XXXX.json"`): portable
# across GNU (Linux / Windows git-bash) and BSD (macOS) mktemp — BSD `-t` has
# divergent semantics and rejects a suffix after the Xs. The .json extension is
# dropped (run_meta reads these by path, not by extension). ${TMPDIR:-/tmp}
# mirrors what `-t` selected, so git-bash still gets a POSIX temp path.
# trap ensures cleanup on any exit path.
TIER_REFRESH_JSON=$(mktemp "${TMPDIR:-/tmp}/research_industry_${SLUG}_framing.XXXXXXXX") || exit 1
COST_JSON=$(mktemp "${TMPDIR:-/tmp}/research_industry_${SLUG}_cost.XXXXXXXX") || { rm -f "$TIER_REFRESH_JSON"; exit 1; }
trap 'rm -f "$TIER_REFRESH_JSON" "$COST_JSON"' EXIT INT TERM

case "$TIER" in
    full)
        echo '{"tam_refreshed":true,"players_refreshed":true,"etf_refreshed":true}' > "$TIER_REFRESH_JSON"
        AGENTS_RUN="framing,enumerate,sector,select,summary" ;;
    partial)
        echo '{"tam_refreshed":true,"players_refreshed":false,"etf_refreshed":true}' > "$TIER_REFRESH_JSON"
        AGENTS_RUN="framing,sector,select,summary" ;;
    no_op)
        echo '{"tam_refreshed":false,"players_refreshed":false,"etf_refreshed":false}' > "$TIER_REFRESH_JSON"
        AGENTS_RUN="summary" ;;
esac

CANDIDATES_COUNT=$(python3 -c "
import json
print(len(json.load(open('$REPORT_DIR/industry_analysis.json', encoding='utf-8'))['candidate_tickers']))
")

# Cost capture — populate from agent run notification metadata
echo '{"tokens": 0, "duration_s": 0}' > "$COST_JSON"  # placeholder; fill from notification

python3 -m scripts.delta.run_meta write \
    --run-dir "$REPORT_DIR" \
    --ticker "industry/$SLUG" \
    --skill research-industry \
    --tier "$TIER" \
    --framing-refresh-json "$TIER_REFRESH_JSON" \
    --candidates-count "$CANDIDATES_COUNT" \
    --agents-run "$AGENTS_RUN" \
    --cost-json "$COST_JSON" \
    --prior-source "$PRIOR_DIR"

# Cleanup handled by trap above; the explicit rm is now belt-and-suspenders
rm -f "$TIER_REFRESH_JSON" "$COST_JSON"

TODAY_ET=$(python3 -c "from scripts.delta.calendar import session_et; print(session_et().isoformat())")
DELTA_FILE="$REPORT_DIR/.delta_section.md"
cat > "$DELTA_FILE" <<EOF
## $TODAY_ET · $TIER

<one or two sentences distilled from summary.md's Delta note section, or
"Initial full research" for first runs>
EOF

python3 -m scripts.delta.append_changelog \
    --ticker "industry/$SLUG" \
    --current "$REPORT_DIR/summary.changelog.md" \
    --delta-section "$DELTA_FILE"

rm "$DELTA_FILE" "$REPORT_DIR/.tier_context.json"
```

### Step 6: Tell the user

Present:
- The 300-800 word `summary.md` (already in `output_language`)
- One-line next-step hint: `Run /score-business <TICKER> on priority-1
  candidates first: <list>`

## What this skill does NOT do

- Does NOT fetch per-ticker fundamentals (that's `/score-business`)
- Does NOT compute BQ scores (that's `/score-business`)
- Does NOT decide buy/sell/hold (that's `/portfolio`)
- Does NOT scan technical setups (that's `/screen-stocks`)
- Does NOT write 10000-word industry essays (master branch has that)

## Error recovery

| Error | Recovery |
|---|---|
| `normalize_slug` exit 2 (unknown CJK) | Surface stderr; suggest pre-translation OR extending `_CJK_ALIASES` |
| Agent JSON fails schema | Re-dispatch with SchemaError message inlined; ≤2 retries |
| Sector ETF unmappable | `sector_etf_map` already falls back to SPY with proxy_note |
| Word count > 1200 | Warn via `run_meta warn`, write anyway |
| `append_changelog` failure | Run_meta still written; manual edit OK as recovery |

## Why this layout

Per project CLAUDE.md "SKILL.md is orchestration only":

- **Methodology** → `prompts/research-industry.md` (portable, agent reads)
- **Hard rules** → `rules/research-industry.md` (canonical) + `.claude/rules/research-industry.md` (auto-loaded adapter)
- **Determinism** → `scripts/industry/*.py` + `scripts/sector_signal.py`
- **Schema** → `scripts/schemas/industry_analysis.py`
- **Orchestration** → this file (thin shell of CLI calls + tier dispatch)

When fixing a bug, first decide which layer it belongs to. Bash-heredoc
inlining in this file is a smell; if logic deserves >5 lines, it goes to
a script.
