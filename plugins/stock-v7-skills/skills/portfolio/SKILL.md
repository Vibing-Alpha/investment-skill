---
name: portfolio
description: |
  Portfolio-level assessment and order generation. Use this skill when the
  user wants to review their entire portfolio, get buy/sell/hold recommendations,
  generate IBKR orders, rebalance, or make allocation decisions.
  Trigger phrases: "portfolio", "review my positions", "what should I do",
  "rebalance", "portfolio check", "generate orders", "position sizing",
  or any request about the portfolio as a whole (not individual stock analysis).
  Requires portfolio-state.yaml with holdings/cash.
  NOT for analyzing a single stock (use score-business).
  NOT for building a thesis on one stock (use investment-thesis).
user_invocable: true
---

# Portfolio — Principle-Based Decision + Orders

Assess portfolio holdings and watchlist against market conditions.
Produce actionable decisions and concrete order recommendations
based on the user's investment principles.

Every run ends by writing a durable **decision log** (`decisions.json` +
`decisions.md` in `reports/portfolio/{YYYYMMDD}/`). The log is what
survives between runs — it's the audit trail, the follow-up calendar,
and the reflection anchor. Treat it as the real output of this skill,
not the conversation.

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here.

Run this block first and **CAPTURE the `STOCK_V7_ROOT=...` value it prints**. Substitute
that absolute path for the literal `<captured-abs-ROOT>` in every later Bash block, every
harness Read path (including `portfolio-state.yaml` / `strategy.yaml`), and every
subagent-dispatch path in this skill. If this block exits non-zero (multiple candidate
roots, or no repo found), show its stderr to the user and **STOP** — run nothing else.

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
"$PYBIN" -m scripts.version_skew --expected-min "1.7.6" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync
```

> **Single-writer note (concurrency probe 2026-08-03):** all same-day
> portfolio runs share `reports/portfolio/<date>/` (macro.json + scratch
> files) with NO session lock — two concurrent /portfolio sessions can
> log one session's decisions against the other's prices. Do not run
> /portfolio in two sessions at once; sequential same-day reruns are
> fine (the earlier log is archived). Also do NOT edit
> portfolio-state.yaml while a run is in flight — the validator now
> binds its state hash and the logger refuses on mismatch.

## Preflight: Money-path config

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.config_gate check --portfolio
```

If it exits non-zero, STOP and show its stderr to the user (config not confirmed / API
key missing / portfolio-state malformed) — do NOT run any analysis or produce numbers.
Then continue below.

## Step 0: Review Prior Run (Cross-Check Follow-ups)

Before assembling today's context, look at what the last run flagged.
This closes the loop between "what I said I'd watch" and "what I'm
deciding now".

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.portfolio_log review
```

The script prints the most recent prior `decisions.json` (**including an
earlier run today** — a same-day rerun archives the prior pair as
`decisions.{run_id}.json/.md` in the same dir, so nothing is clobbered),
along with:
- Prior run's confirmation status (pending/accepted/modified/declined)
- Follow-up events whose date has arrived (`date <= today`)
- A warning if prior run has no reflection recorded yet

Read those due follow-ups into your reasoning. For each one:
- Did the flagged catalyst actually hit? (e.g., earnings on the
  expected date — check news / recent price action)
- Did the prior `what_to_watch` condition trigger? If so, the action
  rule associated with it (e.g., "miss → #3 reduce trigger fires")
  should be explicitly addressed in today's decisions.
- If the prior run was never confirmed, note that — today's decisions
  may need to re-examine the same tickers.

If no prior run exists (first time), the script says so and you proceed
normally.

If the command exits non-zero (typically: the prior `decisions.json` fails
schema validation after a schema change), **STOP**, show its stderr to the
user, and follow the remediation it prints (hand-fix the prior log, or
move/rename it so an older compatible run resolves) — do not proceed with
an unresolved prior log; it is the audit chain today's run builds on.

## Step 1: Read Portfolio State

Read `<captured-abs-ROOT>/portfolio-state.yaml` (the project root).

If the file does not exist, ask the user for their current holdings,
cash balance, and watchlist tickers. Create the file from their response.

Extract:
- `holdings`: dict of ticker → {shares, cost_basis}
- `cash`: number
- `watchlist`: list of tickers
- `open_orders`: the broker's working GTC orders. **The key itself is
  REQUIRED** — if it is ABSENT, stop at this step and ask the user to sync
  their broker's working orders (or confirm there are none, then write
  `open_orders: []`). An absent key is exactly how a working full-clear GTC
  sell goes unseen by the decision engine; Step 8 hard-refuses to write the
  log without the key. Whenever holdings/cash are synced from the broker,
  the broker's working orders are part of the SAME sync.
- `symbol_aliases`: optional `{KEY: {vendor: SYM, broker: SYM}}` map when
  the broker and the data vendor disagree on a symbol (ADR depositary
  changes); Step 4 feeds the vendor side to the price fetch

## Step 2: Compile Principles

Read `<captured-abs-ROOT>/strategy.yaml`. Extract the `principles:` field.

**If `strategy.yaml` exists and has `principles:`:**
1. Compute hash of the current principles list (pipe via stdin to
   avoid shell-quoting issues with special characters):
   ```bash
   cd "<captured-abs-ROOT>"
   PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
   "$PYBIN" -c "
   import hashlib, json, sys, yaml
   sys.stdin.reconfigure(encoding='utf-8')  # Windows cp936: strategy.yaml principles are UTF-8 (e.g. zh-CN) — must match portfolio_log._verify_source_hash's open(encoding='utf-8') or the source_hash diverges
   data = yaml.safe_load(sys.stdin)
   principles = data.get('principles', [])
   print(hashlib.sha256(json.dumps(principles, ensure_ascii=False).encode()).hexdigest())
   " < strategy.yaml
   ```
   Check if `strategy.compiled.yaml` exists and its `source_hash` matches.
   (NB: `source_hash` covers ONLY `principles` — the identical formula
   `scripts/portfolio_log._verify_source_hash` re-checks before writing the
   log, so the two MUST stay in lockstep. `principle_notes` are NOT hashed;
   their freshness is handled by the content comparison in step 2 below, so a
   notes-only edit still propagates without touching the hash.)
2. If hash matches: read cached `hard_constraints`, `soft_principles`,
   and `principle_notes`. **Cache-hit disclosure (probe-2 A4):** the
   `source_hash` covers the extraction INPUT (principles), not the
   extracted constraints — a bad prior extraction persists as long as the
   principles text is unchanged. So on every cache hit, RELAY the cached
   constraints to the user in one line (e.g. "使用缓存约束:
   max_single_position=0.35, min_cash=0.20, max_holdings=8") — and if
   `hard_constraints` is EMPTY, say explicitly that NO numeric floors
   will be enforced this run so the user can catch a lost constraint and
   ask for a recompile. **Notes-freshness guard:** because `source_hash`
   does NOT cover `principle_notes`, after a hash match ALSO compare
   `strategy.yaml`'s `principle_notes` against the compiled file's; if they
   differ in ANY way — missing, empty, OR edited (a `framework` /
   `fundamental_break_definition` / `conflict_priority` / `leverage_policy`
   tweak leaves the principles-only hash matching) — treat the cache as STALE
   and recompile (Step 3 path). Otherwise the load-bearing notes silently
   arrive stale or empty.
3. If hash mismatches or file missing:
   a. Parse each principle — identify quantifiable constraints
      (numbers, percentages, absolute limits).
   b. Extract hard constraints using the canonical keys from
      `rules/portfolio-safety.md`: `max_single_position`, `max_sector`,
      `min_cash`, `max_holdings`.
   c. If any hard constraints were extracted, present them to the user
      for confirmation. If none extracted, skip confirmation.
   d. Normalize percent-point input to decimal fraction (see
      "Constraint Normalization" below), then write
      `strategy.compiled.yaml` with `source_hash`, `hard_constraints`,
      `soft_principles`, and `principle_notes` (copy the notes block
      verbatim from `strategy.yaml` — do NOT drop it; the position-action
      principle (currently #3) and others reference it via "见附注", and
      Step 5 injects it).

**If `strategy.yaml` is missing or has no `principles:`:**
- Check if `strategy.yaml` has a `risk:` section (backward compat):
  map `risk.max_single_position` → `max_single_position`, etc.
- Otherwise use defaults from `rules/portfolio-safety.md`.
- Default principles produce 0 hard constraints — skip confirmation.
- Apply the same normalization before writing (backward-compat `risk:`
  values may be in percent-point form).
- **Always write `strategy.compiled.yaml`** (even with empty
  `hard_constraints: {}`), so validate.py's `--constraints` flag
  always has a valid file to read.

### Constraint Normalization

When compiling hard_constraints, normalize percent-point input to
decimal fraction before writing `strategy.compiled.yaml`. The canonical
format per `rules/portfolio-safety.md` is `[0.0, 1.0]` decimal. Accept
either decimal (`0.35`) or percent-point (`35`) input for ergonomics;
the compiled file MUST be decimal.

Use `scripts.cli_utils.normalize_percent_fraction` (Task 0.1) for the
actual coercion. Its canonical rules are:
- `None` → `None` (skip)
- `0.0 ≤ value ≤ 1.0` → returned unchanged
- `1.0 < value ≤ 100.0` → divided by 100
- otherwise → raise `ValueError`

Apply the helper only to fraction-typed keys (`max_single_position`,
`max_sector`, `min_cash`). `max_holdings` is an integer count and must
pass through untouched.

Example compile snippet:

```python
from scripts.cli_utils import normalize_percent_fraction

FRACTION_KEYS = {"max_single_position", "max_sector", "min_cash"}

def _compile_hard_constraints(raw):
    """Normalize percent-point -> decimal for the compiled file."""
    out = {}
    for k, v in raw.items():
        out[k] = normalize_percent_fraction(v) if k in FRACTION_KEYS else v
    return out
```

Why normalize at compile and not at validate time: the compiled file is
the single source of truth that downstream consumers (`validate.py`,
`portfolio_log`, audit readers) load. Normalizing once here ensures
every consumer sees `0.35`, not `35`. `validate.py` still keeps a
fail-closed guard that rejects values `> 1.0` as belt-and-suspenders,
but it is not the primary coercion point.

## Step 3: Classify each ticker (delta-era staleness)

Classify all portfolio tickers in one batch call to amortize Python
startup across N tickers (avoids ~200ms × N subprocess fork cost):

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKERS="AAPL,MU,NVDA,..."  # comma-separated holdings + watchlist
"$PYBIN" -m scripts.delta.portfolio_classify --tickers "$TICKERS"
# → {"AAPL": "fresh", "MU": "stale_bq", "NVDA": "bq_only", ...}
```

For single-ticker ad-hoc checks, `--ticker TICKER` still prints the
state as a bare string. Batch mode (`--tickers T1,T2,...`) returns
JSON for easy jq parsing.

Returns one of (spec §8.1, 5-state contract):
- `fresh` — last full-tier BQ <14 ET days old AND a completed thesis within the last 7 ET days (both `run_meta.{bq,thesis}.completed == true`; windows per `classify.py`)
- `stale_bq` — BQ ≥14 ET days old, OR its last run lost important data (partial/full tier required)
- `stale_thesis` — thesis >7 ET days old (events reuse ceiling)
- `bq_only` — has BQ, no thesis
- `none` — no reports

## Step 3.5: Batch refresh plan

If any ticker is stale (not `fresh`/`none`/`bq_only`), present the user
with a batch refresh plan BEFORE running any cascades:

```
Portfolio refresh plan:

Full BQ needed (N):
  - TICKER  reason                  ~Ns, ~Nk tokens

Partial BQ needed (N):
  - TICKER  reason                  ~Ns, ~Nk tokens

Thesis refresh (N):
  - TICKER1, TICKER2, ...           ~30s, ~10k each

No refresh needed (N):
  - TICKER1, TICKER2, ...

Total: ~N min, ~Nk tokens.
Proceed?  [a] all  [s] skip stale  [c] customize
```

- `[a]` all: sequentially cascade `/score-business` then `/investment-thesis`
  per ticker, **in alphabetical ticker order**. Sequential, not parallel —
  predictable log output and easier debugging.
- `[s]` skip stale: proceed with whatever artifacts currently exist on
  disk — stale tickers are NOT dropped, just not refreshed. Use
  `scripts.delta.resolver find-latest-prior --include-today` to locate
  each ticker's latest available BQ + thesis, then read them as below.
  Record the stale state + days-since-last-refresh alongside each
  ticker's row in `decisions.md` so the audit shows decisions were
  made on stale data. Include a prominent "⚠ stale data" note in
  the decision summary.
  **Legacy or loader-invalid artifacts must not lose their degradation
  signal here.** A `stale_bq` conflates age with degradation, and a raw
  read must not resurrect what the typed loader rejects — so for each
  skipped ticker, first run the loader itself:
  `python3 -c "from scripts.schemas.bq_analysis import load_bq_analysis;
  load_bq_analysis(r'<resolved-run-dir>/bq_analysis.json')"`. If it exits
  non-zero (any schema rejection: wrong types, unknown status vocabulary,
  asymmetric fields), OR it loads but the raw meta does not carry the
  full post-change pair — a **non-null string** `validation_status` AND a
  **list** `degraded_categories`, both present (every other combination —
  legacy absence, one field missing, `null` status beside an empty list,
  any non-list value — either predates the gate or is a truncated /
  unknowable record; assemble writes a null status exactly when
  completeness was unknowable, and an empty list there means "could not
  enumerate", not "nothing was lost"), run
  `python3 -m scripts.delta.portfolio_classify --degradation-summary
  "<resolved-run-dir>"` and use ITS two fields as the summary fields
  below, labeled "derived from stored validation". A
  `degraded_categories` of `null` means the stored record is unreadable —
  treat it as degraded (entry/add blocked, say so in the rationale).
  **A thesis outlives its source run here too.** When the selected thesis
  dir is OLDER than the selected BQ dir, its ER/CE/entry logic was
  computed from ITS OWN dir's run, not the clean one beside today's BQ —
  so also run `python3 -m scripts.delta.portfolio_classify
  --degradation-summary "<thesis-run-dir>"`; if that `degraded_categories`
  is non-empty or `null`, attach a `thesis_source_degraded:
  <categories|unreadable>` note beside the ticker's thesis fields. And
  when thesis and BQ share ONE dir, the source may have been overwritten
  under it: run `python3 -m scripts.delta.portfolio_classify
  --thesis-orphaned "<run-dir>"`; `{"orphaned": true}` means a same-day
  re-score replaced the run the thesis was built from and its state is
  unrecoverable — attach `thesis_source_degraded: unreadable`. The
  decision prompt treats the note exactly like a non-empty
  `degraded_categories`.
- `[c]` customize: show toggles; then behave as `[a]` for the selected subset.

No timeout — wait for explicit user choice.

For every ticker (fresh, bq_only, AND stale when `[s]` was chosen),
resolve the latest artifacts via the delta resolver and read them as
below (read each artifact at its absolute
`<captured-abs-ROOT>/reports/...` path). **The integrity checks described
under `[s]` apply to EVERY read here — including artifacts freshly
produced by this run's `[a]` cascade**: run the typed loader per
artifact, and if it rejects, or the raw meta lacks the full post-change
pair (a non-null string `validation_status` AND a list
`degraded_categories`), derive the two fields via
`--degradation-summary` exactly as described there. A just-written
artifact earns no exemption — a truncation that happened during THIS
run's cascade is precisely the one no later classification has seen yet. Tickers classified `none` that
weren't cascaded should be flagged in decisions.md as "no analysis
available".

For tickers with `bq_analysis.json`, read the **summary only**:
- `scores` (overall, fundamental, forward, industry)
- `meta.validation_status` (str|null — the fetch's top-level status; disclosure context)
- `meta.degraded_categories` (list — important/critical data the fetch lost; this is the gate. Key MISSING while `validation_status` is non-null = a corrupt record — treat as degraded, do not open/add)
- `meta.data_freshness` + `meta.freshness_note` (disclosure, NOT a gate — the
  latest financial period behind the score and any staleness/anomaly prose.
  200-day-old fundamentals on a quarterly filer deserve a sentence in the
  rationale; they do not block, because filing cadence varies by issuer and
  the delta clocks own artifact staleness)
- `synthesis.watchlist_recommendation`
- `synthesis.conviction`
- `synthesis.thesis`
- `synthesis.key_strengths` (first 3)
- `synthesis.key_risks` (first 3)
- `synthesis.catalyst_calendar`

For tickers with `investment_thesis.json`, read the **full file** (~10KB).

## Step 4: Fetch Macro Data

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
# Vendor aliases (symbol_aliases in portfolio-state.yaml): built by the SAME
# tested helper /monitor uses — no hand-assembled JSON. "{}" is a valid
# no-op, so the flag is passed unconditionally. A malformed symbol_aliases
# raises -> STOP (fetching the wrong symbol is the failure this closes).
VENDOR_ALIASES=$("$PYBIN" -c "
import json, pathlib
from scripts.monitor import load_vendor_aliases
print(json.dumps(load_vendor_aliases(pathlib.Path('portfolio-state.yaml'))))
") || { echo "FATAL: symbol_aliases in portfolio-state.yaml is malformed — fix it before fetching prices" >&2; exit 1; }
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
"$PYBIN" -m scripts.macro \
  --tickers {ALL_TICKERS_SPACE_SEPARATED} \
  --vendor-aliases "$VENDOR_ALIASES" \
  --output "reports/portfolio/$ETDAY/macro.json"
```

Where `{ALL_TICKERS}` = all tickers from holdings + watchlist, substituted
by you per block. The run-day directory is the **ET calendar day**, derived
in-shell as `$ETDAY` inside EVERY block that touches `reports/portfolio/`
(never substituted by you, never carried across blocks — a local-timezone
"today" lands the run in the wrong dir and Step 8 hard-rejects a non-ET
dir; this derives it correctly everywhere).

If the block exits non-zero (malformed `symbol_aliases` FATAL, or the macro
fetch itself fails), **STOP** and show the stderr to the user — do not
proceed to decisions on a partial/unaliased price set.

Read the output JSON. This provides:
- Broad market trend data (SPY, QQQ, ^DJI with MAs)
- VIX + VIX MA20
- Interest rates
- Current prices for all portfolio tickers
- `ticker_indicators[TICKER]` — run-day technical indicators (RSI, MACD,
  Bollinger, ATR, volume confirmation, RSI divergence), computed fresh this
  run. Authoritative for #2 entry timing and #3/#4 momentum reads (the thesis
  `entry_favorability` is a possibly-stale cross-reference). `null`, or a leg
  reading `insufficient_data`, means that read is unavailable — treat as unknown.
- `chart_statuses.ticker_prices[T].price_as_of` / `stale_meta_quote` /
  `price_conflict_same_ts` — per-ticker price vintage + integrity. **Relay
  any `stale_meta_quote: true`, any `price_conflict_same_ts: true` (the
  meta quote and the chart bar disagree at the same timestamp — the price
  used may be contested), or a
  `price_as_of` older than `regime_inputs.anchor_session`, to the user
  together with the limit prices it affects** (thin OTC ADRs lag — a limit
  set off a stale quote does not fill).
- `regime_inputs` — clock-anchored regime block (anchor_session + per-index
  close/ma50/ma200/high_52w/off_52w_high_pct + VIX close/ma20, all at the
  last completed ET session). Two INDEPENDENT consumers read this block (never
  the live values), with distinct vocabularies — do not conflate their labels:
  (a) the decision log's deterministic audit tag (`portfolio_log._classify_regime`
  → risk_on/risk_off/mixed; coarse, generic, identical for every user), and
  (b) the strategy regime layer the user's principles may define (e.g. a
  bull/sideways/bear entry-mode switch), which the decide agent classifies
  per those principles' own criteria.

## Step 5: Make Decisions

**First, seal the authoring context** (closing round-27): record WHAT
state and thesis vintages the decisions are about to be authored
against — the log writer refuses/warns when they drift before Step 8
(an S0-authored rationale beside an S1 snapshot previously persisted
undetected; the validation-time hash only covers validate→log).

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
"$PYBIN" -c "
import hashlib, json, pathlib
import yaml
from scripts.delta.resolver import find_latest_prior
state_bytes = pathlib.Path('portfolio-state.yaml').read_bytes()
state = yaml.safe_load(state_bytes.decode('utf-8')) or {}
tickers = sorted(set(list((state.get('holdings') or {}).keys())
                     + list(state.get('watchlist') or [])))
thesis_ga = {}
for t in tickers:
    d = find_latest_prior(t, 'investment-thesis', include_today=True)
    if d is not None and (d / 'investment_thesis.json').exists():
        try:
            m = json.loads((d / 'investment_thesis.json').read_text(encoding='utf-8')).get('meta') or {}
            thesis_ga[t] = m.get('generated_at')
        except ValueError:
            pass
out = pathlib.Path('reports/portfolio/$ETDAY')
out.mkdir(parents=True, exist_ok=True)
# Atomic write (round-34): a torn/truncated seal is treated by the log
# writer as malformed → hard REFUSE, so never leave a partial file behind.
import os
tmp = out / '.decision_ctx.json.tmp'
tmp.write_text(json.dumps({
    'state_file_sha256': hashlib.sha256(state_bytes).hexdigest(),
    'thesis_generated_at': thesis_ga,
}, indent=2), encoding='utf-8')
os.replace(tmp, out / '.decision_ctx.json')
print('decision ctx sealed for', len(tickers), 'tickers')
"
```

Read `<captured-abs-ROOT>/prompts/portfolio-decide.md`.

Read `<captured-abs-ROOT>/strategy.yaml` for `output_language` (default: `zh-CN`).
Present all human-facing output (decisions, rationale, order recommendations) in this
language. JSON field names and source tags remain in English.

Assemble the full context and reason through the decision framework:

**Context provided to the decision:**
1. Portfolio state — the FULL `portfolio-state.yaml` content (holdings +
   cash + watchlist + open orders + any additional top-level fields, e.g.
   `nav_peak_usd` / `nav_peak_as_of`, which the NAV-drawdown circuit-breaker
   principle reads; enumerating only the four classic sections would starve
   it). **Every decision on a ticker with an in-flight open order must
   explicitly reconcile it (keep / cancel / supersede) in its rationale** —
   the log writer attaches the order snapshot to the decision and warns on
   direction conflicts (e.g. `hold` beside a full-size open sell).
2. Hard constraints (from compiled principles)
3. Soft principles (numbered #1–#N, from compiled `soft_principles` — injected verbatim)
4. Principle notes (from compiled `principle_notes` — injected verbatim):
   `framework` (总纲: 基本面选股 / 技术面择时 — frames HOW to read #1–#N),
   `fundamental_break_definition` (the ONLY mandatory-exit trigger, cited via "见附注" by the position-action principle, currently #3),
   `conflict_priority`, `leverage_policy`. Do NOT omit — these are load-bearing.
5. Macro snapshot (from Step 4)
6. Per-ticker data (BQ summary + thesis, from Step 3)
7. Current prices + run-day technical indicators (`ticker_prices` and
   `ticker_indicators` from macro) — the latter govern the #2 entry-timing
   gate and #3/#4 momentum reads, not the thesis's stale `entry_favorability`.
8. **Earnings-window soft preference** — `orders.earnings_window_days` from
   `strategy.yaml` (default 7; mark it "defaulted" if the field is absent), and
   each ticker's `next_earnings_date` resolved from the per-ticker thesis/BQ
   `catalyst_calendar` / `events.json` (carry the event's `source`; for `as_of`
   use `events.json:meta.generated_at`, or `unknown` if absent — do NOT carry a
   per-item `as_of`, which the catalyst items do not have). The decision treats
   the window as a *named soft deferral* (portfolio-decide.md Phase 2.5): with a
   KNOWN date it may defer/size-down an otherwise-authorized entry; with an
   UNKNOWN date it MUST NOT defer. It is never a technical gate. (`orders.*` is
   read straight from `strategy.yaml`; it is not in `strategy.compiled.yaml`.)

   **`next_earnings_date` resolution rule** (the `catalyst_calendar` is a
   free-form event list; `events.json` dates carry `date_precision` ∈
   {`confirmed`, `estimated`, `approximate`} — note: NO "exact" value exists):
   pick the earnings-typed event (its `event`/`impact` text denotes an
   earnings/results print, NOT a product/legal/macro catalyst) whose date is the
   **nearest on or after** the run date — a same-day after-close print IS
   in-window. Accept `date_precision` `confirmed` OR `estimated` (both are
   day-level, so the window can compare against them). Accepting `estimated` is
   intentional: the window is a SOFT, cautious deferral (size-down / wait, never a
   hard gate), so erring toward caution near a *probable* print matches the user's
   "no chase within the earnings window" preference — rejecting estimated would
   silently disable the deferral for most names and chase into their earnings.
   Resolve to `unknown` ONLY when no earnings-typed event matches or the sole
   match is `approximate` (not day-precise) → Phase 2.5: do NOT defer, judge on
   run-day technicals. Never fuzzy-infer a date from non-earnings event text.

Produce per-ticker decisions with specific order recommendations.

## Step 6: Validate Orders

Structure the proposed orders as a JSON array and write them to the
run-scoped path `reports/portfolio/<ETDAY>/.proposed_orders.json` — NOT a
Python `tempfile` path. Two reasons (both bit before): a `mkstemp` path on
native-Windows git-bash is a backslash string that bash mangles when
substituted into a later block (repo cross-platform rule: bash-consumed
paths never come from Python tempfile), and a system-temp name is not
reconstructable by the later fresh-shell blocks that re-derive their own
paths.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
ORDERS_PATH="reports/portfolio/$ETDAY/.proposed_orders.json"
# Same-day-rerun safety: clear the prior order set FIRST. If parsing the
# heredoc fails below, the validate block must fail loudly on a MISSING
# file — never silently validate an earlier run's stale order set.
rm -f "$ORDERS_PATH"
"$PYBIN" -c "
import json, sys, pathlib
sys.stdin.reconfigure(encoding='utf-8')
orders = json.loads(sys.stdin.read())
p = pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(orders), encoding='utf-8')
print(p.as_posix())
" "$ORDERS_PATH" <<'ORDERS' || { echo "FATAL: proposed-orders JSON invalid — nothing written; fix the order array and re-run this block" >&2; exit 1; }
[{"ticker":"MU","action":"buy","type":"market","shares":50,"est_price":90.0}]
ORDERS
```

If the write block prints FATAL (invalid order JSON), STOP — fix the
order array and re-run this block before going any further; the file was
deliberately cleared first, so skipping ahead would make validation fail
on a missing file rather than silently validate a stale order set.

Then validate, capturing the stress-test JSON so Step 8 can attach it to
the decision log. Both paths are deterministic and run-scoped (NOT
/tmp/...$$): Step 8 runs in a LATER shell — the conversational Step 7 sits
between validate and the log write, and a re-validation in Step 7 must
overwrite the same paths so Step 8 reads the latest. A `$$`/PID temp name
is lost across that boundary, and `portfolio_log --stress-test` FAILS
(exit 2) on a missing path — fix the path, do not drop the flag to
silence it. The fixed paths are reconstructable per-call, exactly like
`macro.json`.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
VALIDATOR_OUTPUT="reports/portfolio/$ETDAY/.validator_output.json"

"$PYBIN" -m scripts.validate \
  --state portfolio-state.yaml \
  --prices "reports/portfolio/$ETDAY/macro.json" \
  --orders "reports/portfolio/$ETDAY/.proposed_orders.json" \
  --constraints strategy.compiled.yaml \
  --output "$VALIDATOR_OUTPUT"
```

Without `--output`, scripts.validate writes to stdout and the JSON is
lost before Step 8 needs it (codex review 2026-05-22 F7).

**If validation passes** (exit 0): Include stress test results in the output.

**If validation fails** (the script exits 1 on `passed: false` — an expected
iteration signal here, NOT a stop-everything error):
- Read the violations from the output.
- Adjust the order set to resolve violations.
- Re-run the ORDER-WRITE block above with the revised array FIRST, then
  re-run validation — the validator reads `.proposed_orders.json`, not
  the conversation; skipping the rewrite re-validates the old order set
  and Step 8 would attach its stress results to your revised
  recommendations. Same rule for any Step 7 revision.
- Max 3 attempts. If still failing, present the unresolved violations
  to the user and ask how to proceed. Note: Step 8 will REFUSE to write
  the log while the validator artifact records `passed: false` — drop or
  fix the failing orders before logging.

## Step 7: Present and Iterate

Present the portfolio assessment, decisions, and orders to the user
in the conversation. Follow the output format from `portfolio-decide.md`.

The user may:
- Ask "why" about a specific decision → explain the reasoning
- Request adjustments ("change MU price to 84") → update and re-validate
- Confirm ("looks good") → the orders are RECOMMENDATIONS only; the user executes
  them manually at their broker. You NEVER submit/place orders and NEVER describe
  them as submitted/placed/executed (advisory-only — `rules/portfolio-safety.md`).
- Report fills + ask you to update holdings → follow the **holdings-update protocol** below
- Ask to analyze missing tickers → run the appropriate skill

**Holdings-update protocol (the ONLY mutation of `portfolio-state.yaml`).** Only when the
user reports actual fills and asks to update positions — never on your own initiative.
**Sole exception — the `nav_peak_usd` ratchet (bookkeeping, not position data):** when the
user's principles define a NAV-peak-anchored rule (e.g. a drawdown circuit breaker) and
run-day NAV (holdings × run-day macro prices + cash) exceeds the stored `nav_peak_usd`,
update `nav_peak_usd` + `nav_peak_as_of` in the same run WITHOUT waiting for a fill
report — monotonic increase only, touch no other field, SHOW the computation (prices
used + arithmetic) in the run output, and re-run the Preflight `config_gate check
--portfolio` after writing. Without this exception a no-fill run that makes a new NAV
high leaves the stored peak stale and understates every later drawdown — silently
suppressing the circuit breaker. The same exception covers the breaker's
staleness-reconfirmation write: when the principle's fail-closed branch fires
(`nav_peak_usd` OR `nav_peak_as_of` missing, or `nav_peak_as_of` past its
staleness bound) and the user
reconfirms the peak in-run, update `nav_peak_as_of` to the run date (and
`nav_peak_usd` only UPWARD, if the user supplies a higher corrected value) — a
user-confirmed bookkeeping write, not a fill; without it the stale-peak branch
deadlocks (reconfirmation required but no authorized writer). Holdings / cash /
open_orders remain strictly below:
1. Show a **before/after diff** of the exact fields changing (e.g. `MU shares: 50 → 100`,
   `cash: 12000 → 3000`, **including `open_orders` — the broker's working GTC
   orders are part of the position sync, not an optional extra**) and have the
   user confirm **that diff** — not a vague "looks good".
2. Keep the prior file (e.g. copy to `portfolio-state.yaml.bak`) so a wrong edit is reversible.
3. Write the update, then re-run the Preflight block above
   (`"$PYBIN" -m scripts.config_gate check --portfolio`, with its `cd`/`PYBIN`
   prelude) — if it fails, STOP and show stderr (a malformed write must not stand).
`config_gate` validates STRUCTURE, not correctness (a mistyped `1000`-for-`100` is
structurally valid) — the user confirming the diff is the control that catches wrong numbers.

This is a conversation, not a pipeline. Stay responsive to the user's
questions and adjustments.

## Step 8: Write Decision Log

Once the orders are stable (whether or not the user has said "accepted"),
persist the run. This is non-optional — the decision log is what makes
audit and reflection possible across future runs.

First, produce a **decisions blob** — a JSON file containing only the
LLM-authored judgment fields. The script will fill in the deterministic
parts (portfolio snapshot, macro, thesis metadata, stress test, etc.).
See `prompts/portfolio-decide.md` §"Decision Log Output" for the blob
schema. Write it to a run-scoped dotfile in the portfolio run dir
(`<captured-abs-ROOT>/reports/portfolio/<ETDAY>/.decisions_blob.json`, where
`<ETDAY>` is the ET day the earlier blocks derived — reuse the exact dir the
Step 4 macro output landed in; the write step hard-rejects a non-ET dir) —
portable (native Windows has no `/tmp`) and stable across step boundaries.

**Use the Write tool** to create this `.json` file — do NOT write it with a
Bash heredoc. You are the orchestrator (the main loop), not a subagent, so the
Write tool works for you on a `.json` path; the `cat <<'EOF'` heredoc rule in
`.claude/rules/skill-architecture.md` #8 exists ONLY for *subagents* (whose
Write tool is blocked for `.md`). Give the Write tool the ABSOLUTE
`<captured-abs-ROOT>/...` path (the Write tool does not follow the bash `cd`).
The blob carries CJK `notes`, apostrophes, and nested JSON — a heredoc
quotes/escapes those fragilely (and a stray delimiter line truncates it
silently); the Write tool sidesteps all of it. Content shape:

```json
{
  "decisions": [ "... one entry per ticker in holdings + watchlist ..." ],
  "orders_proposed": [ "... sequence-numbered orders ..." ],
  "follow_ups": [ "... future catalysts to watch ..." ],
  "candidate_scan": { },
  "principle_audit_interpretation": "Explain why any principle was not cited",
  "notes": [ "Any structural observations" ]
}
```
(`candidate_scan` is REQUIRED when `orders_proposed` is empty — Phase 3
zero-order discipline.)

Then call the logger. If the write command fails (the `|| { …; exit 1; }`
guard fires), STOP — the refusal reason is on stderr, the validator
output is preserved for the re-run, and NO decision log exists yet:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
# Round-20 F4: the exit code MUST be checked — a cmd_write refusal
# (blob shape violation, missing rationale/principle) writes NO
# decisions.json, and the unconditional rm below would then delete the
# only constraint/stress evidence while the block exits 0.
"$PYBIN" -m scripts.portfolio_log write \
  --decisions-blob "reports/portfolio/$ETDAY/.decisions_blob.json" \
  --state portfolio-state.yaml \
  --macro "reports/portfolio/$ETDAY/macro.json" \
  --constraints strategy.compiled.yaml \
  --stress-test "reports/portfolio/$ETDAY/.validator_output.json" \
  --output-dir "reports/portfolio/$ETDAY" \
  || { echo "FATAL: portfolio_log write REFUSED — decisions.json was NOT written. The validator output is KEPT at reports/portfolio/$ETDAY/.validator_output.json; fix the decisions blob per the stderr above and re-run THIS step (do not proceed)." >&2; exit 1; }

# Clean up the validator output (its content is now in decisions.json).
# $ETDAY is re-derived in THIS block (Step 6's shell variables do not
# survive into this later call).
rm -f "reports/portfolio/$ETDAY/.validator_output.json"
```

The script REFUSES (exit 2) when proposed orders or open broker orders
exist but `--stress-test` is absent/missing, and when the artifact records
`passed: false` — run Step 6 first; only an all-hold run with no open
orders may omit the flag.

The script writes `decisions.json` (canonical machine-readable) +
`decisions.md` (hybrid table/narrative for humans). It fills in for
you: `portfolio_before`, `macro` with regime classification,
`constraints_active`, `current_weight_pct` + `thesis_snapshot` +
`report_refs` per decision, `est_cost`/`est_proceeds` per order,
`principle_audit.cited_this_run` + `not_cited_this_run`,
`user_confirmation` placeholders, and `execution_outcomes` placeholders.

Tell the user where the log landed and mention that `execution_outcomes`
+ `user_confirmation.status` are left blank for them to update after
they act. Do not offer to update those yourself — they reflect real
execution, not your proposals.
