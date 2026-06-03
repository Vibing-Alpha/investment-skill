---
name: investment-thesis
description: |
  Generate an investment thesis for a US stock that already has a BQ analysis.
  Use this skill when the user wants to go beyond business quality assessment
  into investment analysis: valuation, technical timing, event catalysts, and
  an integrated investment thesis with entry/exit conditions.
  Trigger phrases: "investment thesis", "should I invest in", "valuation of",
  "what's the thesis for", "is TICKER worth buying", "analyze investment in",
  "thesis for TICKER", "evaluate TICKER as investment", or any request that
  goes beyond BQ into price/timing/catalyst analysis.
  Requires a prior /score-business run — will not work without bq_analysis.json.
  NOT for portfolio-level decisions (use portfolio).
  NOT for order generation (use portfolio).
user_invocable: true
---

# Investment Thesis — Investment Analysis

Build an integrated investment thesis by analyzing a stock across three
independent dimensions (valuation, technical, events) and synthesizing
them into a conviction-weighted thesis with entry conditions.

This skill answers: **Is this stock worth investing in, and under what
conditions?**

It does NOT answer: "How much to buy?" or "What order type?" — those
belong to the portfolio/decision layer.

## Prerequisites

A complete BQ analysis must exist. Use the delta-aware resolver — NOT
raw `ls | sort -r | head -1`. Per CLAUDE.md, raw glob sort returns
same-day not-yet-assembled dirs and failed-tier runs, silently
picking broken data.

```bash
TICKER="AAPL"  # replace with actual ticker
# delta-aware: skips corrupt / same-day-not-assembled / failed-tier dirs
REPORT_DIR=$(python3 -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
```

If `REPORT_DIR` is empty, no valid BQ analysis exists. Inform the user
and suggest running `/score-business TICKER` first. Step 0 below does
the authoritative probe (it uses the same resolver) and handles
cascading to /score-business if needed.

## Output

- `investment_thesis.json` — Integrated thesis with conviction, ER/CE,
  entry and invalidation conditions (canonical machine output)
- `thesis_summary.md` — Human-readable summary (in output_language)

Intermediate files (`valuation.json`, `technical.json`, `events.json`)
are working artifacts kept for traceability.

## Scripts Available

| Script | Purpose | CLI |
|--------|---------|-----|
| `scripts.indicators` | Technical indicators (if not already computed) | `python3 -m scripts.indicators --price-json PATH --output PATH` |
| `scripts.historical_multiples` | 2Y historical P/E, P/S, P/B, EV/EBITDA range | `python3 -m scripts.historical_multiples --ticker $TICKER --financial-json PATH --price-json PATH --output PATH` |
| `scripts.peers` | Peer valuation multiples via yfinance batch | `python3 -m scripts.peers --tickers T1 T2 T3 --output PATH` |
| `scripts.extract_fcf` | Extract TTM FCF/share + WACC from data files | `python3 -m scripts.extract_fcf --ticker $TICKER --financial-json PATH --price-json PATH --macro-json PATH --output PATH` |
| `scripts.reverse_dcf` | Implied growth rate from current price | `python3 -m scripts.reverse_dcf --price PRICE --fcf-per-share FCF --discount-rate RATE --output PATH` |

## Execution (delta-era)

### Step 0: Resolve + cheap-read probe (no LLM yet)

**Validate the ticker symbol before anything else.** `$TICKER` and
values derived from it ($REPORT_DIR, $PRIOR_THESIS_DIR) are interpolated
into `python3 -c '...'` heredocs. An unsanitized ticker containing
quotes / path separators could escape the single-quoted heredoc and
execute arbitrary Python. Restrict to the actual US ticker vocabulary:

```bash
TICKER="AAPL"
if ! [[ "$TICKER" =~ ^[A-Z][A-Z.]{0,9}$ ]]; then
    echo "FATAL: invalid ticker format: '$TICKER' (expected [A-Z][A-Z.]{0,9})" >&2
    exit 1
fi

REPORT_DIR=$(python3 -c "
from scripts.delta.calendar import session_et
from pathlib import Path
# session_et (not today_et) — directory anchor is the trading day
# whose close is being analyzed. Stable across ET midnight for one
# continuous session, so consecutive /score-business + /investment-thesis
# invocations land in the same date dir.
p = Path('reports') / '$TICKER' / session_et().strftime('%Y%m%d')
p.mkdir(parents=True, exist_ok=True)
(p / 'data').mkdir(exist_ok=True)
print(str(p))
")

# Find prior thesis (for events reuse decision)
PRIOR_THESIS_DIR=$(python3 -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill investment-thesis)

# Find same-day BQ (for cascade decision — uses include-today)
SAME_DAY_BQ_DIR=$(python3 -m scripts.delta.resolver find-latest-prior \
  --ticker "$TICKER" --skill score-business --include-today)
```

### Step 1: Ensure same-day BQ exists (cascade if not)

If `$SAME_DAY_BQ_DIR` is empty OR it differs from `$REPORT_DIR`, invoke
`/score-business TICKER` as a cascade before proceeding. The cascade
will decide its own tier (full / partial / no_op).

Compare against `$REPORT_DIR` (the `session_et` directory anchor from
Step 0), NOT against the `today_et` calendar date. Both dirs are
`session_et`-anchored, so on a weekend/holiday `today_et` (e.g.
2026-05-25) legitimately differs from the session dir (e.g. 20260522);
a `today_et` comparison would force a spurious cascade for a BQ that is
in fact current for the session being analyzed. Equal paths ⇒ the latest
valid BQ is for this session ⇒ no cascade.

- If cascade's tier is `full` and requires confirmation, prompt the
  user: "`/score-business $TICKER` needs full refresh (new 10-Q;
  ~45s, ~20k tokens). Continue? [Y/n]".
- User declines: skip the cascade. Run probe-level fetch alone so
  events has fresh data:
  ```bash
  python3 -m scripts.fetch -t "$TICKER" -o "$REPORT_DIR/data/" \
    --categories 01_price_data,02_financial_data,03_company_news,04_insider_data,06_analyst_estimates,07_earnings,09_macro_rates \
    --news-limit 10 \
    --tier-decided probe
  ```
  Events agent runs fresh on this data. Note in thesis_summary that
  full BQ re-analysis was declined.
- User accepts or cascade tier < full: `/score-business` completes,
  producing same-day run_meta.bq + fresh data.
- If cascade fails (API outage, circuit breaker): abort thesis with
  error message pointing at the cascade failure (spec §7.4).

### Step 2: Gate 1 — classifier (prior-events-scoped), ONE call

Extract the canonical anchor from prior events.json **ONCE, BEFORE any
mutation**, then use it as `since_date`:

```bash
PRIOR_EVENTS=""
if [ -n "$PRIOR_THESIS_DIR" ]; then
    PRIOR_EVENTS="$PRIOR_THESIS_DIR/events.json"
fi
CANONICAL_ANCHOR=$(python3 -m scripts.delta.probe canonical-events-anchor \
    --prior-events "$PRIOR_EVENTS" 2>/dev/null)
```

If `$CANONICAL_ANCHOR` is empty (first run, pre-delta artifact,
malformed meta): skip the classifier entirely (no prior to diff
against); tier will be fresh-events.

Otherwise spawn a subagent with `prompts/delta/classify-news.md`
passing articles from `$REPORT_DIR/data/03_company_news.json` with
`since_date=$CANONICAL_ANCHOR`. Rubric at `.claude/rules/delta-materiality.md`.

This single call drives Gate 1 (material_count==0) AND supplies
`low_signal_news_since` + `low_signal_headlines` for §7.5 synthesis
context. Do NOT call the classifier a second time.

If classifier output fails `validate_classifier_output` (garbled JSON)
or `input_healthy` is False: fail-open → events rerun. Log to
`run_meta.warnings`.

### Step 3: Gates 2-5 (estimates, catalysts, schema, ceiling)

Build gate inputs via the pure helper and call `decide_events_reuse`.
The helper extracts `canonical_events_anchor_et` BEFORE any mutation
of the prior events doc (spec R11/R12) and enforces the 3-condition
classifier health check. Do NOT reimplement these inline.

```bash
# DECISION format: "reuse|gates_passed|gates_failed|override_reason|anchor"
DECISION=$(python3 -m scripts.delta.probe decide-events-reuse \
    --report-dir "$REPORT_DIR" \
    --prior-thesis-dir "$PRIOR_THESIS_DIR" \
    --classifier-output "$REPORT_DIR/.classifier_output.json")

# Parse decision + preserve anchor for Step 4 reuse_meta stamping.
# The anchor field is Step 2's CANONICAL_ANCHOR re-derived by the
# helper; both are the same value since read_prior_events_run_date
# is pure.
CANONICAL_ANCHOR=$(echo "$DECISION" | cut -d'|' -f5)
```

### Step 4: Events — reuse or rerun

Parse gate results AND write the audit-trail JSON consumed by Step 7's
`run_meta write --events-reuse-json`. Both paths write the file — on
`reuse` it records what was reused from where; on `rerun` it records
why the gates failed:

```bash
DECISION_KIND=$(echo "$DECISION" | cut -d'|' -f1)
GATES_PASSED=$(echo "$DECISION" | cut -d'|' -f2)
GATES_FAILED=$(echo "$DECISION" | cut -d'|' -f3)
OVERRIDE=$(echo "$DECISION" | cut -d'|' -f4)
```

If `reuse`: copy prior events.json forward with prune + stale-date
rewrite + reuse_meta stamping via the dedicated CLI:

```bash
python3 -m scripts.thesis.reuse_events \
    --decision-kind reuse \
    --report-dir "$REPORT_DIR" \
    --prior-thesis-dir "$PRIOR_THESIS_DIR" \
    --canonical-anchor "$CANONICAL_ANCHOR" \
    --gates-passed "$GATES_PASSED"
```

If `rerun`: spawn events agent (prompts/evaluate-events.md) on today's
probe data AND still write the audit record so run_meta captures WHY
fresh regeneration was chosen:

```bash
python3 -m scripts.thesis.reuse_events \
    --decision-kind rerun \
    --report-dir "$REPORT_DIR" \
    --gates-failed "$GATES_FAILED" \
    --override-reason "$OVERRIDE"
```

Then, AFTER the events agent has written `events.json`, stamp its
`meta.generated_at` deterministically. The agent is asked to emit this
timestamp, but a wall-clock value is the one thing an LLM can't reliably
produce: the observed failure is a midnight-UTC value, which the downstream
ET normalizer (`probe._safe_normalize_to_et_date`, also used by
`alpha_freshness`) shifts to the PRIOR ET day — moving the canonical events
anchor + the next run's ceiling_7d reuse gate a day early (and a general
hallucination is not even fail-safe). The orchestrator owns the run clock,
so it stamps authoritatively here — mirroring Step 6.3's `stamp_thesis_meta`
for `investment_thesis.json`. This is RERUN-ONLY: the reuse path above
preserves the prior `generated_at` as the chain-stable anchor and must NOT
be re-stamped.

This stamp is **WARN-then-verify** (unlike the Step 6.3 thesis stamp, which
is fail-closed). The agent still emits `generated_at` as a fallback, so a
stamp failure normally just leaves the LLM value in place — the prior
fail-safe (possibly date-early) behavior, NOT a regression. But "leave the
fallback" is only safe if the fallback is actually a normalizable timestamp:
if the stamp failed AND `generated_at` is missing/garbled, the downstream
`derive_events_freshness` (Step 6.5) would hard-`raise`. So on stamp failure
we VERIFY the fallback and abort only when it is genuinely unusable. This
keeps the strict Pareto property: correct value in the normal case; the
prior fail-safe value if the stamp fails but the fallback is fine; a clear
early abort only in the case Step 6.5 would have crashed anyway.

```bash
if ! python3 -m scripts.thesis.stamp_events_meta --report-dir "$REPORT_DIR"; then
  echo "WARN: stamp_events_meta failed — verifying the agent's fallback generated_at" >&2
  python3 -c "
import json, sys
from scripts.delta.probe import _safe_normalize_to_et_date
try:
    m = json.load(open('$REPORT_DIR/events.json', encoding='utf-8')).get('meta', {})
except Exception:
    sys.exit(1)
ga = m.get('generated_at') if isinstance(m, dict) else None
sys.exit(0 if (ga and _safe_normalize_to_et_date(ga)) else 1)
" || { echo "[fatal] stamp_events_meta failed AND the agent fallback generated_at is missing/unnormalizable — events.json would hard-fail Step 6.5. Aborting." >&2; exit 1; }
fi
```

### Step 5: Valuation + Technical (always fresh)

**Step 5a — deterministic valuation inputs.** The valuation agent READS
`historical_multiples.json`, `peer_multiples.json`, `fcf_inputs.json`, and
`reverse_dcf.json`; it does NOT produce them, and `/score-business` does not
either. Per the delta spec §7.1, the thesis run writes ALL of these
intermediates itself — produce them here before dispatch or the agent
silently degrades (it merely lowers `confidence` when files are missing — no
loud error). They have no interdependencies except `reverse_dcf`, which reads
`fcf_inputs.json` and so runs last.

```bash
# indicators.json is normally produced by /score-business; (re)compute only
# if absent — e.g. the Step 1 BQ-declined probe-only path does not run it,
# yet the technical agent requires it.
if [ ! -f "$REPORT_DIR/data/indicators.json" ]; then
  python3 -m scripts.indicators \
    --price-json "$REPORT_DIR/data/01_price_data.json" \
    --output "$REPORT_DIR/data/indicators.json"
fi

# Clear THIS run's valuation producer outputs first. $REPORT_DIR is reused for
# the whole session date, so a same-day rerun can leave stale artifacts. Without
# this, the `|| true` crash guard below could pass on a STALE parseable file if a
# producer dies before write_output() replaces it; and a skipped reverse_dcf
# (below) would leave a prior run's reverse_dcf.json for the agent to read
# instead of emitting a `status: skipped` stub. (indicators.json is NOT cleared —
# it may be the valid /score-business output the technical agent needs.)
rm -f "$REPORT_DIR/data/historical_multiples.json" \
      "$REPORT_DIR/data/fcf_inputs.json" \
      "$REPORT_DIR/data/peer_multiples.json" \
      "$REPORT_DIR/data/reverse_dcf.json"

# Historical multiples + extract_fcf EXIT 1 (after writing a structured
# `status: error` JSON) when they DL4 fail-close — expected for Dec-FYE names
# (INTC/MU/VSH report Q4 only in the 10-K, so every TTM window is
# non-consecutive). That non-zero exit is NOT fatal to the thesis: the
# valuation agent reads the error JSON and flags the 2Y-range / DCF lenses
# UNAVAILABLE, leaning on peers + snapshot. Tolerate exit 1 here (`|| true`),
# then verify below that the artifact was actually written — a MISSING /
# unparseable file is a genuine producer crash and MUST abort.
# --ticker is REQUIRED on both (DL4 aligned-window gate; extract_fcf also DL3c FX).
python3 -m scripts.historical_multiples --ticker "$TICKER" \
  --financial-json "$REPORT_DIR/data/02_financial_data.json" \
  --price-json "$REPORT_DIR/data/01_price_data.json" \
  --output "$REPORT_DIR/data/historical_multiples.json" || true

python3 -m scripts.extract_fcf --ticker "$TICKER" \
  --financial-json "$REPORT_DIR/data/02_financial_data.json" \
  --price-json "$REPORT_DIR/data/01_price_data.json" \
  --macro-json "$REPORT_DIR/data/09_macro_rates.json" \
  --output "$REPORT_DIR/data/fcf_inputs.json" || true

# Genuine-crash guard: each producer MUST have written THIS run's artifact (we
# rm'd stale ones above) carrying a `status` key (ok | ok_with_warnings |
# partial | error — the guard accepts any value). A missing/unparseable/
# status-less file is a real crash, NOT a DL4 fail-close.
# (Deliberately a bare structural check — NOT the typed loader, which rejects
# the null-FCF error artifact that fail-close legitimately produces.)
for f in historical_multiples.json fcf_inputs.json; do
  python3 -c "import json,sys; d=json.load(open('$REPORT_DIR/data/$f', encoding='utf-8')); sys.exit(0 if isinstance(d,dict) and 'status' in d else 1)" \
    || { echo "FATAL: $REPORT_DIR/data/$f missing/unparseable/status-less — producer crash, not a DL4 fail-close" >&2; exit 1; }
done

# Peer multiples — peer_tickers come from bq_analysis dimensions.industry.peer_tickers.
python3 -c "
import json, subprocess, sys
with open('$REPORT_DIR/bq_analysis.json', encoding='utf-8') as f:
    pts = json.load(f).get('dimensions',{}).get('industry',{}).get('peer_tickers',[])
if pts:
    subprocess.run([sys.executable, '-m', 'scripts.peers', '--tickers'] + pts +
                   ['--output', '$REPORT_DIR/data/peer_multiples.json'], check=True)
"

# Reverse DCF — orchestrator-produced per delta spec §7.1; runs LAST (reads
# fcf_inputs.json). Guard mirrors the valuation prompt's skip rules
# (prompts/evaluate-valuation.md §Null FCF guard / §Currency error guard):
# skip on status==error (extract_fcf fail-close) OR null / non-positive
# fcf_per_share (negative FCF has no implied-growth meaning). ISS-009's
# non-USD caller-chain risk is satisfied implicitly: post-DL3c a non-error
# status means fcf_per_share is USD (native or FX-converted); an unsupported
# currency would have fail-closed to status==error and been skipped here.
# When this skips, the valuation agent emits a `reverse_dcf: {status: skipped}`
# stub instead of reading a non-existent file.
python3 -c "
import json, subprocess, sys
with open('$REPORT_DIR/data/fcf_inputs.json', encoding='utf-8') as f:
    inp = json.load(f)
fcf = inp.get('fcf_per_share') or 0
price = inp.get('current_price') or 0
dr = inp.get('discount_rate', 0.10)
if inp.get('status') != 'error' and fcf > 0 and price > 0:
    subprocess.run([sys.executable, '-m', 'scripts.reverse_dcf',
                    '--price', str(price), '--fcf-per-share', str(fcf),
                    '--discount-rate', str(dr),
                    '--output', '$REPORT_DIR/data/reverse_dcf.json'], check=True)
"
```

`historical_multiples` and `extract_fcf` fail-CLOSED (`status: error`) for
Dec-FYE names (INTC/MU/VSH) per the comment above; `reverse_dcf` is then
skipped (no valid FCF/share). This is correct DL4 behavior, NOT a retry-able
failure — the valuation agent flags the 2Y-range / DCF lenses UNAVAILABLE.

**Step 5b — analysis agents.** Run both on today's data:

```
Agent V: prompts/evaluate-valuation.md → $REPORT_DIR/valuation.json
Agent T: prompts/evaluate-technical.md → $REPORT_DIR/technical.json
```

Both agents MUST first read `investment-thesis/gotchas.md` for their domain's
known failure patterns — Agent V the valuation-producer fail-close classes
(DL4 non-consecutive quarters / unknown ADR ratio / non-USD annual-only =
expected, not a bug) + peer-set hygiene; Agent T the partial-day-volume and
MA200-approximation notes.

### Step 6: Synthesis (with events_reuse_context if reused)

Spawn synthesis agent with `prompts/evaluate-thesis.md`. If events was
reused (Step 4 took the reuse path), include the `events_reuse_context`
block per prompts/evaluate-thesis.md. Read `bq_analysis.json` from
same-day or prior BQ dir.

The synthesis agent MUST write BOTH deliverables (the two `## Output`
sections of `prompts/evaluate-thesis.md`) — do NOT let the dispatch
emphasise the JSON and silently drop the human file:
- `$REPORT_DIR/investment_thesis.json` — canonical machine output (Write tool)
- `$REPORT_DIR/thesis_summary.md` — human-facing summary in
  `output_language` (default zh-CN), <600 words

> **Dispatch note (`.claude/rules/skill-architecture.md` #8):** the harness
> blocks subagent `.md` writes via the Write tool. Instruct the synthesis
> agent to write `thesis_summary.md` via a Bash heredoc with a content-unique
> quoted delimiter (`cat > "reports/<TICKER>/<DATE>/thesis_summary.md" <<'THESIS_MD_EOF' … THESIS_MD_EOF`,
> UTF-8; NOT a bare `EOF`/`MD` — collision truncates; substitute the ACTUAL
> `reports/…` path — the subagent shell has no `$REPORT_DIR`) —
> `investment_thesis.json` writes fine via the Write tool. The hard
> `[ -s thesis_summary.md ]` gate below catches a missing/empty deliverable (a
> mid-file delimiter collision is prevented by the content-unique sentinel, not
> by the gate).

It MUST ALSO return, in its final message, a one-paragraph `delta_note`
(≤3 sentences) stating what THIS run concluded. This feeds Step 7's
changelog; it is NOT an `investment_thesis.json` field and the prompt
does not define it, so the orchestrator gets it from the agent's return.
On a first thesis run (empty `$PRIOR_THESIS_DIR`) it is just "First
thesis run — no prior to diff" + the headline stance.

**Verify both files before proceeding** — a synthesis agent that emits
only the JSON is a known failure mode (the human deliverable Step 8
links to then 404s). Unlike `investment_thesis.json` (guarded by 6.3
stamp + 6.4 contract validate), `thesis_summary.md` has no other gate,
so add one here mirroring score-business's post-synthesis `summary.md`
check. If it fires, RE-DISPATCH the synthesis agent (it is the
contracted producer) — do not skip the deliverable:

```bash
[ -s "$REPORT_DIR/investment_thesis.json" ] \
  || { echo "FATAL: synthesis produced no investment_thesis.json — re-dispatch synthesis agent" >&2; exit 1; }
[ -s "$REPORT_DIR/thesis_summary.md" ] \
  || { echo "FATAL: synthesis produced no thesis_summary.md (human deliverable) — re-dispatch synthesis agent" >&2; exit 1; }
# Word budget is a SOFT-fail per the prompt's <600-word spec (warn, do not abort),
# mirroring score-business's summary.md word-count gate. Use
# cli_utils.count_word_equivalents, NOT `wc -w`: default output_language is
# zh-CN and CJK has no inter-word spaces, so `wc -w` undercounts ~3x and the
# gate would never fire (helper counts non-CJK tokens + CJK chars/2).
WORDS=$(python3 -c 'import sys; from scripts.cli_utils import count_word_equivalents; print(count_word_equivalents(open(sys.argv[1], encoding="utf-8").read()))' "$REPORT_DIR/thesis_summary.md")
if [ "$WORDS" -gt 600 ]; then
    echo "WARN: thesis_summary.md exceeded 600 words" >&2
fi
```

### Step 6.3: Stamp orchestrator-owned meta (deterministic)

`investment_thesis.json` is fully LLM-authored, but the typed loader
requires a top-level `meta.{ticker, analysis_date, generated_at}`. Rather
than depend on the synthesis agent reliably emitting them (a fresh run
once produced an artifact with no `meta` and aborted Step 6.4), the
orchestrator stamps the three required fields deterministically —
mirroring `assemble.py` for `bq_analysis.json`. The stamper is idempotent
and preserves any agent-emitted `meta.current_price` / `current_price_source`.

```bash
# --analysis-date defaults to today_et() — the ET CALENDAR run date (NOT the
# session being analyzed; that is market_asof_date in bq_analysis). This is the
# same source assemble.py stamps onto bq_analysis.json.meta.analysis_date, so
# both artifacts in this run dir agree. On a weekend/holiday it differs from the
# session_et directory date (e.g. dir 20260522 vs analysis_date 2026-05-25).
if ! python3 -m scripts.thesis.stamp_thesis_meta \
  --report-dir "$REPORT_DIR" \
  --ticker "$TICKER"; then
  echo "[fatal] stamp_thesis_meta failed — meta would be missing/invalid at Step 6.4. Aborting." >&2
  exit 1
fi
```

**Compute `capital_efficiency` deterministically (do NOT trust the LLM's CE).**
CE = `expected_return / |max_downside|` is pure arithmetic of two fields the
agent already emits — and LLM-emitted CE drifts (a live MRVL artifact stored
`+0.37` where the formula gives `-0.37`, flipping a bearish thesis to "favorable
risk/reward"). The orchestrator computes it here, AFTER meta-stamping and BEFORE
Step 6.4 validation. It sets CE null when ER is null (they travel together) and
never divides by a zero/garbage `max_downside` (the schema's strict-negative MD
gate catches that in 6.4). The synthesis agent no longer emits CE.

```bash
if ! python3 -m scripts.thesis.compute_thesis_ce --report-dir "$REPORT_DIR"; then
  echo "[fatal] compute_thesis_ce failed — capital_efficiency would be missing at Step 6.4. Aborting." >&2
  exit 1
fi
```

### Step 6.4: Contract Validation (fail-closed)

Validate the LLM-produced `investment_thesis.json` against its typed
schema contract (`scripts/schemas/investment_thesis.py`). This runs
BEFORE Step 6.5 reads the file for alpha discovery, so a drift
artifact can't contaminate downstream steps.

```bash
if ! python3 -m scripts.schemas.investment_thesis "$REPORT_DIR/investment_thesis.json"; then
  echo "[fatal] investment_thesis.json failed contract validation — see SchemaError above. Aborting run." >&2
  exit 1
fi
```

### Step 6.5: Alpha Discovery (automatic Phase 1, interactive Phase 2-4)

After synthesis completes, run the automatic alpha scan. Phase 1 is
ALWAYS executed — it is deterministic, cheap, and "no significant
alpha" is a valid and expected output for most well-covered names.

**Phase 1 — Automatic scan:**

Perform divergence detection by cross-referencing `bq_analysis.json`,
`valuation.json`, `technical.json`, `events.json`, and
`investment_thesis.json` against the 6 patterns in
`prompts/evaluate-alpha.md` (framework mismatch, growth expectation
gap, dimension split, smart money divergence, technical-fundamental
disconnect, peer valuation outlier). Write Phase 1 output to
`$REPORT_DIR/alpha_scan.json` following the schema in the prompt's
Scan Output section. Maximum 3 candidates, ranked by magnitude and
novelty.

The `events_freshness` block in `alpha_scan.json` is mandatory — it
derives from `events.json.meta` (fresh vs reused, days_stale) so a
reader can judge whether insider/analyst/macro signals driving a
candidate are "as of today" or up to 7 days stale (the ceiling_7d
gate cap). Use the pure helper `scripts.delta.alpha_freshness.
derive_events_freshness(events_meta, today_et)` — do NOT reimplement
the derivation inline (see .claude/rules/producer-consumer.md #3). This
field is write-only; the delta layer does not read it.

Phase 1 may run as a focused subagent OR inline by the lead — it is
pure pattern matching with no WebSearch needed. Keep it cheap.

**Phase 2-4 — Interactive, user-gated:**

If `alpha_scan.json.alpha_candidates` is empty, skip to Step 7 with a
one-line note to the user ("Alpha scan: no significant divergence
detected — expected for well-covered large-cap").

If candidates exist, present them to the user via AskUserQuestion with
options: (0) skip all, (1..N) investigate candidate N. On skip,
proceed to Step 7.

On user selection:

- **Phase 2 — Hypothesis Articulation:** walk through the four
  questions (market consensus / variant view / necessary conditions /
  strongest evidence) via AskUserQuestion per
  `prompts/evaluate-alpha.md` Phase 2. Synthesize into the hypothesis
  statement and confirm with user before proceeding.

  **Phase 2 may terminate here with NO testable hypothesis.** Per
  `prompts/evaluate-alpha.md` Phase 2 Q2 + Critical Rule 6, if the user
  cannot name a *specific non-consensus disagreement* (only a directional
  lean, or restating the consensus bull/bear case), there is no alpha to
  test — say so respectfully and STOP. Do NOT spawn Phase 3 on a
  non-hypothesis: forcing the adversarial pass onto "I just like the
  story" manufactures exactly the false alpha the prompt forbids
  (false positives are worse than false negatives). Record this terminal
  state (third `.alpha_status.json` shape below) and proceed to Step 7.

- **Phase 3 — Adversarial Testing:** spawn two parallel agents that
  cannot see each other's work. The structural adversarialism is
  non-negotiable — a single "balanced" agent does not substitute.

  ```
  Agent Advocate:
    Read prompts/evaluate-alpha.md (Phase 3, Agent A section)
    Data: bq_analysis.json, valuation.json, technical.json, events.json
    Hypothesis: [the articulated hypothesis]
    Output: $REPORT_DIR/alpha_advocate.json

  Agent Prosecutor:
    Read prompts/evaluate-alpha.md (Phase 3, Agent B section)
    Data: same as Advocate
    Hypothesis: [the articulated hypothesis]
    Output: $REPORT_DIR/alpha_prosecutor.json
  ```

- **Phase 4 — Verdict:** the lead reads both outputs and produces
  `$REPORT_DIR/alpha_verdict.json` per Phase 4 of
  `prompts/evaluate-alpha.md` — hypothesis rating, evidence balance,
  forced pre-mortem (specific narrative with names/dates/numbers),
  actionable kill criteria (time-bound + measurable + data source),
  conditional valuation. Present verdict to user.

Record alpha state for Step 7's run_meta. Write
`$REPORT_DIR/.alpha_status.json` with shape:

```json
{
  "phase_1_ran": true,
  "candidates_found": 1,
  "user_selected": 1,
  "phases_completed": ["phase_1", "phase_2", "phase_3", "phase_4"],
  "verdict_rating": "moderate"
}
```

When Phase 2-4 is skipped (no candidates OR user chose 0):

```json
{
  "phase_1_ran": true,
  "candidates_found": 0,
  "user_selected": null,
  "phases_completed": ["phase_1"],
  "verdict_rating": null
}
```

When the user selected a candidate but Phase 2 articulation revealed NO
testable hypothesis (the pseudo-alpha filter above — user could not name
a specific non-consensus disagreement, so Phase 3 was correctly NOT
spawned):

```json
{
  "phase_1_ran": true,
  "candidates_found": 2,
  "user_selected": 1,
  "phases_completed": ["phase_1", "phase_2"],
  "verdict_rating": null,
  "phase_2_outcome": "no_testable_hypothesis"
}
```

`phase_3` is absent from `phases_completed` in this case, so Step 7's
AGENTS_RUN composition correctly omits `alpha_advocate,alpha_prosecutor`
(they never ran). `verdict_rating` stays null — no verdict was produced.

### Step 7: Write run_meta thesis section + append thesis_summary.changelog.md

#### Cost accumulation

Each Task subagent call returns usage info with `total_tokens`. The
orchestrator accumulates these across all calls this run (classifier,
valuation, technical, events [if rerun], synthesis, alpha_advocate +
alpha_prosecutor [if Phase 3 ran]) plus wall time into a per-run costs
file with shape `{"tokens": N, "duration_s": N}`. Write it to a run-scoped
transient dotfile under `$REPORT_DIR` — portable (native Windows has no
/tmp) and stable across step boundaries (a separate shell loses `$$`):

```bash
COSTS_FILE="$REPORT_DIR/.delta_costs.json"
```

Pass `$COSTS_FILE` to `run_meta write --cost-json`.

Compose `$AGENTS_RUN` from what ACTUALLY ran this run, not from the
decision kind alone:
- The **classifier** runs only when there was a prior events doc to diff
  against. Step 2 spawns it iff `$CANONICAL_ANCHOR` is non-empty, and skips
  it on a first run / empty anchor / pre-delta artifact — so it must NOT be
  hardcoded into the list. Prepend it only when `$CANONICAL_ANCHOR` is
  non-empty. Gate on the anchor (not the `.classifier_output.json` file) so
  a classifier that was invoked but failed to write valid output (Step 2's
  fail-open path) is still recorded — it ran and incurred cost, and
  agents_run is a write-only cost/audit trail.
- When events was **reused**, the events agent was NOT invoked, so it must
  NOT appear in the list.
- When alpha **Phase 3** ran, append `alpha_advocate,alpha_prosecutor`
  (read `$REPORT_DIR/.alpha_status.json` written in Step 6.5 to know).

```bash
case "$DECISION_KIND" in
    reuse)  AGENTS_RUN="valuation,technical,synthesis" ;;
    rerun)  AGENTS_RUN="valuation,technical,events,synthesis" ;;
    *)      AGENTS_RUN="valuation,technical,events,synthesis" ;;
esac

# Prepend classifier ONLY if it was actually invoked. Step 2 spawns it iff
# CANONICAL_ANCHOR is non-empty; a first run / empty anchor skips it. Gate on
# CANONICAL_ANCHOR (not the .classifier_output.json file) so a classifier that
# ran but failed to write valid output is still recorded; agents_run is a
# write-only cost/audit trail. Safe because CANONICAL_ANCHOR is captured as a
# string at Step 3 (= Step 2's dispatch value — both derive from the same
# prior-events path via the pure read_prior_events_run_date helper) and is NOT
# reassigned between Step 3 and this gate, so Step 4's prior-events mutation
# cannot affect it; the value here equals the Step 2 dispatch predicate. (If a
# future change reassigns CANONICAL_ANCHOR before Step 7, switch to an explicit
# CLASSIFIER_INVOKED flag set at dispatch in Step 2.)
if [ -n "$CANONICAL_ANCHOR" ]; then
    AGENTS_RUN="classifier,${AGENTS_RUN}"
fi

# Append alpha Phase 3 agents if they ran
if [ -f "$REPORT_DIR/.alpha_status.json" ]; then
    PHASE_3_RAN=$(python3 -c "
import json
s = json.load(open('$REPORT_DIR/.alpha_status.json', encoding='utf-8'))
print('yes' if 'phase_3' in s.get('phases_completed', []) else 'no')
")
    if [ "$PHASE_3_RAN" = "yes" ]; then
        AGENTS_RUN="${AGENTS_RUN},alpha_advocate,alpha_prosecutor"
    fi
fi

python3 -m scripts.delta.run_meta write \
  --run-dir "$REPORT_DIR" \
  --ticker "$TICKER" \
  --skill investment-thesis \
  --agents-run "$AGENTS_RUN" \
  --events-reuse-json "$REPORT_DIR/.events_reuse.json" \
  --cost-json "$COSTS_FILE"
```

Write the delta section, then append changelog:

```bash
TODAY_ET=$(python3 -c "from scripts.delta.calendar import session_et; print(session_et().isoformat())")
cat > "$REPORT_DIR/.delta_section.md" <<EOF
## Update $TODAY_ET (thesis)

<substitute the one-paragraph delta_note the synthesis agent returned in
Step 6; on a first run (empty \$PRIOR_THESIS_DIR) use "First thesis run —
no prior to diff" + the headline stance>
EOF

python3 -m scripts.delta.append_changelog \
  --prior "$PRIOR_THESIS_DIR/thesis_summary.changelog.md" \
  --current "$REPORT_DIR/thesis_summary.changelog.md" \
  --ticker "$TICKER" \
  --delta-section "$REPORT_DIR/.delta_section.md"

rm "$REPORT_DIR/.delta_section.md"
```

### Step 8: Report to user

Report: events reuse status, thesis conviction, ER/CE, link to
thesis_summary.md, AND alpha scan summary — candidates found / phases
completed / verdict rating if Phase 4 ran / path to `alpha_scan.json`
(plus `alpha_verdict.json` if produced).
