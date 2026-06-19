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

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here. The industry input and the `SLUG` Step 0 prints are likewise substituted
by you into EACH block (`<INDUSTRY>` / `<SLUG>` literals — never carried as shell
variables across blocks); each block re-runs the idempotent `allocate-industry-run`
(current run dir) / `find-latest-prior` (prior run dir) to re-derive its dirs; and
COMPUTED cross-step state (industry_name / output_language / tier / force flag)
lives in the run-scoped state file `$REPORT_DIR/.run_state.json`, written in
Steps 0-1 and re-read by every consuming block.

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

Four artifacts in `reports/industry/<slug>/<YYYYMMDD>/`:

| File | Purpose | Schema |
|---|---|---|
| `industry_analysis.json` | Machine output, /score-business consumer | `scripts/schemas/industry_analysis.py` |
| `summary.md` | Human brief, 300-800 words target | Markdown template in `prompts/research-industry.md` |
| `summary.changelog.md` | Append-only delta log | Markdown |
| `run_meta.json` | Delta audit state | `scripts/delta/run_meta.py:IndustrySection` |

## Scripts

Every Bash block below first `cd`s to the captured root and runs scripts via
`"$PYBIN" -m scripts.<module>` (venv-or-python3 indirection).

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

If this block exits non-zero, STOP and surface the error (bad input is a
user-input problem; see the exit-2 note below).

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
RAW_INDUSTRY="<INDUSTRY>"   # agent-substituted user input (e.g. "AI芯片") — never carried across blocks
if [ -z "$RAW_INDUSTRY" ]; then
    echo "FATAL: research-industry requires an industry name argument" >&2
    exit 1
fi
case "$RAW_INDUSTRY" in
    *\`*|*\$*|*\;*|*\|*|*\&*|*\<*|*\>*|*\(*|*\)*|*\{*|*\}*|*\\*|*\"*|*\'*)
        echo "FATAL: industry name contains shell metacharacters: '$RAW_INDUSTRY'" >&2
        exit 1 ;;
esac

NORM=$("$PYBIN" -m scripts.industry.normalize_slug --industry "$RAW_INDUSTRY") || exit $?
SLUG=$(echo "$NORM" | "$PYBIN" -c "import json,sys; print(json.load(sys.stdin)['slug'])")

REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "industry/$SLUG" --skill research-industry)

# Seed the run-scoped state file: fresh shells lose variables, so computed
# cross-step state (industry_name / output_language, later tier + force flag)
# lives here and consuming blocks re-read it. The $NORM stdin pipe is
# ASCII-safe (upstream json.dumps defaults to ensure_ascii=True), so it needs
# no stdin reconfigure (documented exception in .claude/rules/development.md).
echo "$NORM" | "$PYBIN" -c "
import json, pathlib, sys
state = json.load(sys.stdin)   # {'industry_name': ..., 'slug': ...}
try:
    import yaml
    lang = (yaml.safe_load(pathlib.Path('strategy.yaml').read_text(encoding='utf-8')) or {}).get('output_language') or 'zh-CN'
except Exception:
    lang = 'zh-CN'   # strategy.yaml missing/malformed -> default (display language only, not money-path)
state['output_language'] = lang
pathlib.Path('$REPORT_DIR/.run_state.json').write_text(
    json.dumps(state, indent=2), encoding='utf-8')
"
printf 'SLUG=%s\nREPORT_DIR=%s\nPRIOR_DIR=%s\n' "$SLUG" "$REPORT_DIR" "$PRIOR_DIR"
```

**CAPTURE the printed `SLUG`** and substitute it for the literal `<SLUG>` in
every later block (the blocks re-derive `REPORT_DIR` / `PRIOR_DIR` from it via
the idempotent resolver commands). Note `PRIOR_DIR` (possibly empty = first
run) and use `<captured-abs-ROOT>/<REPORT_DIR>/...` when composing absolute
subagent-dispatch paths.

If `normalize_slug` exits 2, the input was non-ASCII and not in the alias
table. Surface the stderr to the user with the suggestion to either
pre-translate or extend `scripts/industry/normalize_slug.py:_CJK_ALIASES`.

`output_language` is captured into the state file from
`<captured-abs-ROOT>/strategy.yaml` (default `zh-CN`).

### Step 1: Decide tier

If this block exits non-zero (invalid tier value), STOP and surface the error.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SLUG="<SLUG>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "industry/$SLUG" --skill research-industry)

# Detect user force-refresh from invocation context. Heuristics: presence of
# "重新" / "refresh" / "再跑一遍" / "from scratch" / "fresh" in the prompt.
# Default to false; orchestrator sets to true when applicable.
FORCE_FLAG=""
# FORCE_FLAG="--force-refresh"  # uncomment when force-refresh detected

TIER=$("$PYBIN" -m scripts.industry.decide_tier --prior-dir "$PRIOR_DIR" $FORCE_FLAG)
case "$TIER" in
    full|partial|no_op) ;;
    *) echo "FATAL: decide_tier returned invalid: '$TIER'" >&2; exit 1 ;;
esac

# Persist tier + force flag into the run-scoped state file — later blocks run
# in FRESH shells and re-read them from here, never from exported variables.
"$PYBIN" -c "
import json, pathlib
p = pathlib.Path('$REPORT_DIR/.run_state.json')
state = json.loads(p.read_text(encoding='utf-8'))
state['tier'] = '$TIER'
state['force_refresh'] = bool('$FORCE_FLAG')
p.write_text(json.dumps(state, indent=2), encoding='utf-8')
"
printf 'TIER=%s\n' "$TIER"
```

### Step 2: Fetch sector ETF signal

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SLUG="<SLUG>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
ETF_JSON=$("$PYBIN" -m scripts.industry.sector_etf_map --slug "$SLUG")
ETF=$(echo "$ETF_JSON" | "$PYBIN" -c "import json,sys; print(json.load(sys.stdin)['etf'])")

"$PYBIN" -m scripts.sector_signal --etf "$ETF" \
    --output "$REPORT_DIR/data/sector_etf_trends.json"
```

`proxy_note` (from the same `sector_etf_map` output, re-derived in Step 3's
block — the mapping is a cheap deterministic local lookup) is non-empty when
the ETF is a thematic proxy (e.g. SOXX for MLCC). It is passed to the agent
via `.tier_context.json` so the regime_rationale can acknowledge it.

### Step 3: Tier-specific work

**If `no_op`**: copy prior JSON, regenerate `summary.md` only (with delta note).

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SLUG="<SLUG>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "industry/$SLUG" --skill research-industry)
cp "$PRIOR_DIR/industry_analysis.json" "$REPORT_DIR/industry_analysis.json"
# Then dispatch summary subagent in no_op mode (see Step 4).
```

**If `partial`**: dispatch the framing-refresh + selection agent. Pass prior
`candidate_tickers` as a hint; agent may add / remove / re-rank.

**If `full`**: dispatch the full 4-phase research agent.

#### Agent dispatch (full + partial)

Assemble the agent's inputs at `$REPORT_DIR/.tier_context.json`. Everything
comes from in-block re-derivation (ETF map) or the run-scoped state file —
nothing user-shaped is interpolated into Python source:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SLUG="<SLUG>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "industry/$SLUG" --skill research-industry)
ETF_JSON=$("$PYBIN" -m scripts.industry.sector_etf_map --slug "$SLUG")
echo "$ETF_JSON" | "$PYBIN" -c "
import json, os, pathlib, sys
from scripts.delta.calendar import session_et
etf = json.load(sys.stdin)   # ASCII-safe pipe (upstream json.dumps)
state = json.loads(pathlib.Path('$REPORT_DIR/.run_state.json').read_text(encoding='utf-8'))
prior = '$PRIOR_DIR'
ctx = {
    'tier': state['tier'],
    'prior_source_date': os.path.basename(prior) if prior else '',
    'industry_name': state['industry_name'],
    'slug': state['slug'],
    'output_language': state['output_language'],
    'analysis_date': session_et().isoformat(),
    'sector_etf': {'symbol': etf['etf'], 'proxy_note': etf['proxy_note']},
    'user_force_refresh': state['force_refresh'],
}
pathlib.Path('$REPORT_DIR/.tier_context.json').write_text(
    json.dumps(ctx, indent=2), encoding='utf-8')
"
```

Spawn ONE subagent whose instructions are
`<captured-abs-ROOT>/prompts/research-industry.md`, with inputs
`<captured-abs-ROOT>/<REPORT_DIR>/.tier_context.json` and
`<captured-abs-ROOT>/<REPORT_DIR>/data/sector_etf_trends.json`, instructed to
WRITE `<captured-abs-ROOT>/<REPORT_DIR>/industry_analysis.json` — substitute the
concrete absolute paths into the dispatch prompt (the subagent inherits neither
this shell's variables nor its cwd; `.json` writes are allowed for subagents).

Agent MUST have WebSearch access — its prompt carries a fail-closed
preflight (one real WebSearch call before any research; host lacks the
tool → the agent reports `cannot complete: host lacks WebSearch`, and
this skill must STOP rather than proceed from model memory). Must produce
`industry_analysis.json` matching
`scripts/schemas/industry_analysis.py`. The hard constraints in
`<captured-abs-ROOT>/rules/research-industry.md` (universe, source tags,
dispersion, OTC ADR, overextended regime) are enforced via schema + prompt; the
agent reads the prompt and the orchestrator validates the result.

### Step 4: Validate JSON + generate summary.md

On full/partial tiers the artifact was freshly agent-authored under the
WebSearch-binding contract, so the block first stamps the deterministic
root marker `_websearch_binding_version: 1` — the loader then
strict-validates every `[WebSearch:]` citation
(`[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]`) fail-closed. The
no_op tier copies the prior artifact verbatim and is NOT stamped (a
pre-binding prior stays legacy-lenient; a post-binding prior already
carries the marker).

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SLUG="<SLUG>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
TIER=$("$PYBIN" -c "import json; print(json.load(open('$REPORT_DIR/.run_state.json', encoding='utf-8'))['tier'])")
if [ "$TIER" != "no_op" ]; then
  "$PYBIN" -c "
import json, pathlib
from scripts.schemas.source_tag import stamp_websearch_binding
p = pathlib.Path('$REPORT_DIR/industry_analysis.json')
data = json.loads(p.read_text(encoding='utf-8'))
p.write_text(json.dumps(stamp_websearch_binding(data), indent=2, ensure_ascii=False), encoding='utf-8')
" || { echo "[fatal] could not stamp _websearch_binding_version onto industry_analysis.json" >&2; exit 1; }
fi
"$PYBIN" -c "
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
inlined (unbound-WebSearch-tag errors mean the agent must re-emit with
real urls + access dates — never invent them). Up to 2 retries; on 3rd
failure, surface the error and STOP.

Generate `summary.md` via a separate light subagent reading the validated
JSON at `<captured-abs-ROOT>/<REPORT_DIR>/industry_analysis.json` +
the `output_language` from `.run_state.json` (state both concretely in the
dispatch prompt). Word count is target ≤1200, not strict gate.

> **Dispatch note (`.claude/rules/skill-architecture.md` #8):** the harness
> blocks subagent `.md` writes via the Write tool. Instruct this subagent to
> write `summary.md` via a Bash heredoc with a content-unique quoted delimiter
> (`cat > "<captured-abs-ROOT>/reports/industry/<SLUG>/<DATE>/summary.md" <<'SUMMARY_MD_EOF' … SUMMARY_MD_EOF`,
> UTF-8; NOT a bare `EOF`/`MD` — collision truncates; substitute the ACTUAL
> ABSOLUTE path — the subagent shell has no `$REPORT_DIR` and its cwd is
> ephemeral), not the Write tool.

Then hard-gate it (catches a missing/empty `summary.md`, which feeds the
changelog + user output). If the gate exits non-zero, re-dispatch ONCE; if it
fails a second time, STOP and surface the failure:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SLUG="<SLUG>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
[ -s "$REPORT_DIR/summary.md" ] \
  || { echo "FATAL: summary subagent produced no summary.md — re-dispatch" >&2; exit 1; }
```

### Step 5: Write run_meta + append changelog

If this block exits non-zero (corrupt run-state tier, mktemp failure), STOP
and surface the error.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SLUG="<SLUG>"
REPORT_DIR=$("$PYBIN" -m scripts.delta.resolver allocate-industry-run --slug "$SLUG")
PRIOR_DIR=$("$PYBIN" -m scripts.delta.resolver find-latest-prior \
  --ticker "industry/$SLUG" --skill research-industry)
TIER=$("$PYBIN" -c "import json; print(json.load(open('$REPORT_DIR/.run_state.json', encoding='utf-8'))['tier'])")
case "$TIER" in
    full|partial|no_op) ;;
    *) echo "FATAL: run-state tier invalid: '$TIER'" >&2; exit 1 ;;
esac

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

CANDIDATES_COUNT=$("$PYBIN" -c "
import json
print(len(json.load(open('$REPORT_DIR/industry_analysis.json', encoding='utf-8'))['candidate_tickers']))
")

# Cost capture — populate from agent run notification metadata
echo '{"tokens": 0, "duration_s": 0}' > "$COST_JSON"  # placeholder; fill from notification

"$PYBIN" -m scripts.delta.run_meta write \
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

TODAY_ET=$("$PYBIN" -c "from scripts.delta.calendar import session_et; print(session_et().isoformat())")
DELTA_FILE="$REPORT_DIR/.delta_section.md"
printf '## %s · %s\n\n' "$TODAY_ET" "$TIER" > "$DELTA_FILE"
cat >> "$DELTA_FILE" <<'RI_DELTA_SECTION_EOF'
<one or two sentences distilled from summary.md's Delta note section, or
"Initial full research" for first runs — QUOTED heredoc: the body is free
prose that may contain $ or backticks, which an unquoted heredoc would
silently expand>
RI_DELTA_SECTION_EOF

"$PYBIN" -m scripts.delta.append_changelog \
    --ticker "industry/$SLUG" \
    --current "$REPORT_DIR/summary.changelog.md" \
    --delta-section "$DELTA_FILE"

rm -f "$DELTA_FILE" "$REPORT_DIR/.tier_context.json"   # -f: .tier_context.json was never written on no_op
```

### Step 6: Tell the user

Present:
- The 300-800 word `summary.md` (already in `output_language`) — read it at
  `<captured-abs-ROOT>/<REPORT_DIR>/summary.md` (absolute — the harness Read
  tool does not follow the bash `cd`)
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
