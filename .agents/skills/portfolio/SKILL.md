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

## Step 0: Review Prior Run (Cross-Check Follow-ups)

Before assembling today's context, look at what the last run flagged.
This closes the loop between "what I said I'd watch" and "what I'm
deciding now".

```bash
python3 -m scripts.portfolio_log review
```

The script prints the most recent prior `decisions.json` (excluding
today), along with:
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

## Step 1: Read Portfolio State

Read `portfolio-state.yaml` from the project root.

If the file does not exist, ask the user for their current holdings,
cash balance, and watchlist tickers. Create the file from their response.

Extract:
- `holdings`: dict of ticker → {shares, cost_basis}
- `cash`: number
- `watchlist`: list of tickers
- `open_orders`: list of existing GTC orders (optional)

## Step 2: Compile Principles

Read `strategy.yaml` from the project root. Extract the `principles:` field.

**If `strategy.yaml` exists and has `principles:`:**
1. Compute hash of the current principles list (pipe via stdin to
   avoid shell-quoting issues with special characters):
   ```bash
   python3 -c "
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
   and `principle_notes`. **Notes-freshness guard:** because `source_hash`
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
TICKERS="AAPL,MU,NVDA,..."  # comma-separated holdings + watchlist
STATUSES_JSON=$(python3 -m scripts.delta.portfolio_classify --tickers "$TICKERS")
# → {"AAPL": "fresh", "MU": "stale_bq", "NVDA": "bq_only", ...}
```

For single-ticker ad-hoc checks, `--ticker TICKER` still prints the
state as a bare string. Batch mode (`--tickers T1,T2,...`) returns
JSON for easy jq parsing.

Returns one of (spec §8.1, 5-state contract):
- `fresh` — last full-tier BQ <14 ET days old AND a completed thesis within the last 7 ET days (both `run_meta.{bq,thesis}.completed == true`; windows per `classify.py`)
- `stale_bq` — BQ ≥14 ET days old (partial/full tier required)
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
- `[c]` customize: show toggles; then behave as `[a]` for the selected subset.

No timeout — wait for explicit user choice.

For every ticker (fresh, bq_only, AND stale when `[s]` was chosen),
resolve the latest artifacts via the delta resolver and read them as
below. Tickers classified `none` that weren't cascaded should be
flagged in decisions.md as "no analysis available".

For tickers with `bq_analysis.json`, read the **summary only**:
- `scores` (overall, fundamental, forward, industry)
- `synthesis.watchlist_recommendation`
- `synthesis.conviction`
- `synthesis.thesis`
- `synthesis.key_strengths` (first 3)
- `synthesis.key_risks` (first 3)
- `synthesis.catalyst_calendar`

For tickers with `investment_thesis.json`, read the **full file** (~10KB).

## Step 4: Fetch Macro Data

```bash
python3 -m scripts.macro \
  --tickers {ALL_TICKERS_SPACE_SEPARATED} \
  --output reports/portfolio/{YYYYMMDD}/macro.json
```

Where `{ALL_TICKERS}` = all tickers from holdings + watchlist,
and `{YYYYMMDD}` = today's date.

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

## Step 5: Make Decisions

Read `prompts/portfolio-decide.md`.

Read `strategy.yaml` for `output_language` (default: `zh-CN`). Present all
human-facing output (decisions, rationale, order recommendations) in this
language. JSON field names and source tags remain in English.

Assemble the full context and reason through the decision framework:

**Context provided to the decision:**
1. Portfolio state (holdings + cash + watchlist + open orders)
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

Structure the proposed orders as a JSON array and write to a temp file:

```bash
python3 -c "
import json, tempfile, os, sys
orders = json.loads(sys.stdin.read())
fd, path = tempfile.mkstemp(suffix='.json')
with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(orders, f)
print(path)
" <<'ORDERS'
[{"ticker":"MU","action":"buy","type":"market","shares":50,"est_price":90.0}]
ORDERS
```

Then validate, capturing the stress-test JSON so Step 8 can attach it to
the decision log. Use a deterministic run-scoped path (NOT /tmp/...$$):
Step 8 runs in a LATER shell — the conversational Step 7 sits between
validate and the log write, and a re-validation in Step 7 must overwrite
the same path so Step 8 reads the latest. A `$$`/PID temp name is lost
across that boundary, and `portfolio_log --stress-test` silently SKIPS a
missing path (`if ... .exists()`), dropping the stress test from the log
with no error. The fixed path is reconstructable per-call, exactly like
`macro.json`:

```bash
VALIDATOR_OUTPUT=reports/portfolio/{YYYYMMDD}/.validator_output.json

python3 -m scripts.validate \
  --state portfolio-state.yaml \
  --prices reports/portfolio/{YYYYMMDD}/macro.json \
  --orders {TEMP_ORDERS_JSON} \
  --constraints strategy.compiled.yaml \
  --output "$VALIDATOR_OUTPUT"
```

Without `--output`, scripts.validate writes to stdout and the JSON is
lost before Step 8 needs it (codex review 2026-05-22 F7).

**If validation passes:** Include stress test results in the output.

**If validation fails:**
- Read the violations from the output.
- Adjust the order set to resolve violations.
- Re-run validation.
- Max 3 attempts. If still failing, present the unresolved violations
  to the user and ask how to proceed.

## Step 7: Present and Iterate

Present the portfolio assessment, decisions, and orders to the user
in the conversation. Follow the output format from `portfolio-decide.md`.

The user may:
- Ask "why" about a specific decision → explain the reasoning
- Request adjustments ("change MU price to $84") → update and re-validate
- Confirm ("looks good") → optionally update `portfolio-state.yaml`
  if positions changed (ask before writing)
- Ask to analyze missing tickers → run the appropriate skill

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
(matching the `.validator_output.json` convention above) — portable
(native Windows has no `/tmp`) and stable across step boundaries:

```bash
BLOB=reports/portfolio/{YYYYMMDD}/.decisions_blob.json
cat > "$BLOB" << 'EOF'
{
  "decisions": [ ... one entry per ticker in holdings + watchlist ... ],
  "orders_proposed": [ ... sequence-numbered orders ... ],
  "follow_ups": [ ... future catalysts to watch ... ],
  "candidate_scan": { ... },   # REQUIRED when orders_proposed is empty (Phase 3 zero-order discipline)
  "principle_audit_interpretation": "Explain why any principle was not cited",
  "notes": [ "Any structural observations" ]
}
EOF
```

Then call the logger:

```bash
python3 -m scripts.portfolio_log write \
  --decisions-blob "$BLOB" \
  --state portfolio-state.yaml \
  --macro reports/portfolio/{YYYYMMDD}/macro.json \
  --constraints strategy.compiled.yaml \
  --stress-test reports/portfolio/{YYYYMMDD}/.validator_output.json \
  --output-dir reports/portfolio/{YYYYMMDD}

# Clean up the validator output (its content is now in decisions.json).
# Use the literal run-scoped path, not $VALIDATOR_OUTPUT — that variable
# was set in Step 6's shell and does not survive into this later call.
rm -f reports/portfolio/{YYYYMMDD}/.validator_output.json
```

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
