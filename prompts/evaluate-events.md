# Event Evaluation

You are assessing **what upcoming events and signals could materially affect
this stock over the next 3-6 months**.

Core question: **What events could significantly change the thesis, and what
is the overall direction of signals?**

You are NOT scoring business quality (that is done). You are scanning the
forward calendar and signal landscape to identify what could move the stock
and whether the weight of evidence leans positive or negative.

## Input

- `bq_analysis.json` — read `synthesis.catalyst_calendar` for factual dates
  already collected by the BQ layer. Start here; do not rebuild from scratch.
- `data/03_company_news.json` — recent company news
- `data/04_insider_data.json` — insider transactions
- `data/08_institutional.json` — institutional holdings (13F filings)
- `data/06_analyst_estimates.json` — consensus estimates and revisions
- `data/07_earnings.json` — **past** earnings results only (no forward/next date)
- `data/09_macro_rates.json` — interest rate environment
- WebSearch: 1-2 targeted queries (see Macro Context below)

## Process

### 1. Macro Context (thin signal aggregator)

Run 1-2 WebSearch queries to capture the current macro backdrop for this stock.
Use queries like `"[TICKER] [sector] macro risks tailwinds {CURRENT_YEAR}"`.

You are a signal aggregator here, not a macro analyst. Identify 3-5 headline
factors that affect this specific stock and classify each by direction and
magnitude. Do not write multi-paragraph macro commentary.

Why keep this thin: deep macro analysis is unbounded and low-signal for
single-stock decisions. The investment-thesis layer needs to know whether
the macro environment is a headwind or tailwind, not a full economic outlook.

Output:
- `headline_factors`: each with factor name, `impact_on_stock` (tailwind /
  headwind / neutral), `magnitude` (high / moderate / low), and source tag
- `net_macro_bias`: tailwind / mixed / headwind

### 2. Catalyst Calendar (enrich from BQ layer)

Start from `synthesis.catalyst_calendar` in `bq_analysis.json`. These are
factual dates already validated by the scoring agents.

For each existing catalyst, ADD:
- `context`: what the consensus expects and why it matters (e.g., "Consensus
  EPS $0.92; AI segment guidance is the key variable")
- `direction`: your assessment of likely outcome (positive / negative /
  uncertain)
- `impact`: magnitude of potential stock move (high / medium / low)

Then ADD any new catalysts discovered via WebSearch that are missing from the
BQ calendar (product launches, regulatory decisions, conference presentations,
contract renewals, M&A rumors with credible sourcing).

Filtering rules:
- Past events (date < today) MUST be excluded. The BQ layer may have included
  events that were future at analysis time but are now past.
- Catalyst dates MUST come from API data or WebSearch results. Never use model
  memory for dates — if you cannot source a date, do not include the event.
- The forward earnings **date** is NOT in `07_earnings.json` (past prints only).
  It is WebSearch-sourced (earnings calendar / company IR) — preserve the
  `[WebSearch: ...]` provenance carried from the BQ layer, or refine it here if
  WebSearch finds a newer announced date. Cite `[API: 06_analyst_estimates,
  fiscal_period quarter-end]` only as a quarter-end corroboration, never as the
  date's source, and never tag a forward date `[API: 07_earnings]`. Mark
  `date_precision` `confirmed` only for an IR/exchange-published date; a generic
  calendar estimate is `estimated`.
- Preserve `date_precision` from the BQ layer (confirmed / estimated /
  approximate). For new catalysts, set precision honestly based on your source.

### 3. Signals (three categories from API data)

Extract directional signals from the structured data files. Each signal
category produces a net direction and supporting evidence.

#### Insider Activity (`data/04_insider_data.json`)

Classify the net insider direction over the trailing 6 months:
- `buying`: net purchases dominate — management is putting money in
- `selling`: net sales dominate — but distinguish informational from routine
- `neutral`: feed PRESENT (has `summary`/`trades`) but balanced / no meaningful activity
- `unknown`: file carries NEITHER `summary` NOR `trades` (e.g. `{"currency": ...}`
  only) or the fetch failed. This is ABSENCE of signal, NOT a neutral reading;
  common on foreign ADRs. Tag `[API: 04_insider_data]` with summary "feed
  unavailable". `unknown` MUST NOT tilt the Overall Event Bias (§5).
  (A PRESENT `summary` reporting no recent activity — even with `trades: []` —
  is `neutral`, not `unknown`.)

**10b5-1 planned sales are neutral, not negative.** These are pre-scheduled
diversification plans and carry no informational content about management's
view of the stock. Flag them explicitly as "10b5-1 planned" and classify
as neutral. Only unplanned, discretionary sales are negative signals.

Assess `conviction_signal` (positive / neutral / negative) based on the
informational content of the trades, not just the dollar volume.

#### Institutional Flow (`data/08_institutional.json`)

Classify net institutional behavior from 13F data:
- `accumulating`: net new positions or significant increases
- `distributing`: net position reductions or exits
- `stable`: feed PRESENT (has `holdings`) but no meaningful change
- `unknown`: `holdings` key absent / empty / placeholder, no 13F coverage, or
  fetch failed. Absence of signal, NOT `stable`; common on ADRs. Tag
  `[API: 08_institutional]` with note "feed unavailable". `unknown` MUST NOT tilt
  the Overall Event Bias (§5). (`stable` requires a PRESENT, non-empty `holdings`
  feed showing no meaningful change.)

Note that 13F data is delayed (filed up to 45 days after quarter end).
Factor this staleness into your confidence assessment. Surface notable
changes — a top-10 holder exiting matters more than a small fund adding.

#### Analyst Sentiment (`data/06_analyst_estimates.json` + WebSearch)

Extract:
- Current consensus rating from API data
- Target price range (low / median / high) from API data
- Recent rating changes — if the API data does not include individual firm
  actions, use 1 WebSearch query: `"[TICKER] analyst upgrade downgrade
  {CURRENT_YEAR}"` to find recent changes

Report the analyst target range as raw data only (low / median / high). Do
NOT derive or editorialize ANY price-relative or return figure — "% upside /
downside vs the current price", margin of safety, implied return — anywhere in
this analysis, not just here. Those belong to the valuation dimension; this
agent is not given the live price, so any such number is unsourced and
frequently wrong (a target *below* the recent close is downside, not upside).
Leave every price-vs-target comparison to valuation / synthesis.

Rating changes within the last 90 days carry the most weight. An upgrade
from a tier-1 firm is more significant than one from an unknown shop.

### 4. Event Density

Count catalysts by time horizon:
- `next_30d`: catalysts within 30 calendar days of today
- `next_90d`: catalysts within 90 calendar days of today

Classify `assessment`:
- `catalyst_rich`: 3+ catalysts in next 90 days — expect elevated volatility
- `normal`: 1-2 catalysts in next 90 days
- `catalyst_sparse`: 0 catalysts in next 90 days — low near-term catalysts

Why this matters: high catalyst density increases near-term volatility and
affects optimal entry timing. A catalyst-rich environment may favor waiting
for post-event clarity; a catalyst-sparse one favors gradual entry.

### 5. Overall Event Bias

Synthesize all four sections into a single directional assessment:

- `strongly_positive`: macro tailwind + positive catalysts + insider buying +
  analyst upgrades — signals are aligned and favorable
- `moderately_positive`: most signals lean positive but with some mixed inputs
- `neutral`: the AVAILABLE signals are balanced. Count only signals actually
  present — `unknown` inputs (unavailable feeds, §3) are EXCLUDED from the tally,
  never counted as neutral votes. When too few signals are available to call a
  direction, reflect that in low `confidence` (below) + name the missing feeds;
  do NOT manufacture a neutral bias out of absent data. (Fallback: if ZERO
  directional signals are available at all, emit `neutral` as an explicit
  no-direction default with `confidence: low` and name every unavailable feed —
  this is distinct from a balanced-data neutral.)
- `moderately_negative`: most signals lean negative
- `strongly_negative`: macro headwind + negative catalysts + insider selling +
  analyst downgrades — signals are aligned and unfavorable

Set `confidence` (high / medium / low) based on data quality, recency, and
signal agreement. Contradictory signals with stale data = low confidence.

## Output — events.json

### Required meta block (delta-era) + date-field hygiene

Your `events.json` output MUST include a top-level `meta` object with
EXACTLY these two fields — no others:

```json
{
  "meta": {
    "output_version": "8.0",
    "generated_at": "<UTC ISO timestamp>"
  },
  "macro_context": { ... },
  "catalyst_calendar": [ ... ],
  ...rest of existing content...
}
```

**Do NOT emit any of the following** (observed across real runs to
cause schema drift, stale-date pollution, or provenance corruption):

- `meta.analysis_date` — nested dates would need a deeper rewriter;
  the orchestrator handles date stamping outside the agent's output.
- `meta.schema_version` — replaced by `meta.output_version`; don't
  both-emit.
- `meta.ticker` — the orchestrator knows the ticker from context.
- `meta.today` — observed in ONTO output; literal date value that
  the rewriter's allow-list deliberately does NOT touch (not a
  canonical date-field name), so it would stay stale on reuse.
- `meta.prior_bq_analysis_date`, `meta.consumed_bq_date`, or any
  other provenance-style date field — these are audit trail and
  must NOT be rewritten on reuse; omitting them from the prompt
  avoids the ambiguity entirely.
- Top-level `analysis_date` — leave date stamping to the orchestrator.
- Top-level `as_of_date` — will be rewritten on reuse if emitted;
  prefer to omit entirely.

The delta layer's Gate 4 (§7.2 of the design spec) reads
`events.json.meta.output_version` to decide if the schema matches
current — without a meta block, the gate fails on every check and
events is never reused.

`generated_at` is the fresh-generation timestamp (UTC ISO-8601 with
timezone, e.g. `<YYYY-MM-DD>T<HH:MM:SS>Z`). Emit your best value, but know
it is **orchestrator-owned**: a wall-clock timestamp is the one thing you
cannot reliably produce, so immediately after you write `events.json` the
orchestrator OVERWRITES `generated_at` deterministically with its own run
clock (`scripts.thesis.stamp_events_meta`, SKILL.md Step 4 rerun branch) —
mirroring how it stamps `investment_thesis.json.meta.generated_at`. Your
value is a fallback for standalone use; do not agonize over it.
Once stamped on fresh generation it is immutable **provenance** — NEVER
rewritten by any later process, including the reuse copier, which records
its own timestamp separately in `meta.reuse_meta.copied_at` and leaves
`generated_at` untouched so it continues to anchor the original
fresh-generation date across a chain of reuses.

### Output shape

```json
{
  "meta": {
    "output_version": "8.0",
    "generated_at": "<UTC ISO-8601 timestamp, YYYY-MM-DDTHH:MM:SSZ>"
  },
  "macro_context": {
    "headline_factors": [
      {"factor": "Fed rate cuts", "impact_on_stock": "tailwind", "magnitude": "moderate", "source": "[WebSearch: Fed minutes]"}
    ],
    "net_macro_bias": "mixed"
  },
  "catalyst_calendar": [
    {"event": "Next quarterly earnings", "date": "<YYYY-MM-DD>", "date_precision": "confirmed",
     "impact": "high", "direction": "uncertain",
     "context": "Consensus EPS $X.XX; segment guidance is the key variable",
     "source": "[WebSearch: earnings-calendar / company IR release date] [API: 06_analyst_estimates, fiscal_period quarter-end]"},
    {"event": "Contract / partnership / product catalyst", "date": "<YYYY-MM-DD>", "date_precision": "estimated",
     "impact": "medium", "direction": "positive",
     "context": "Backlog / revenue recognition begins; ramp over coming quarters",
     "source": "[API: 03_company_news, Reuters product-launch headline]"}
  ],
  "signals": {
    "insider_activity": {
      "net_direction": "selling",
      "summary": "CFO sold $2.1M in discretionary sales; CEO 10b5-1 plan sales excluded",
      "conviction_signal": "negative",
      "source": "[API: 04_insider_data]"
    },
    "institutional_flow": {
      "net_direction": "accumulating",
      "notable_changes": "Vanguard +1.2M shares, BlackRock +800K shares in Q4 13F",
      "source": "[API: 08_institutional]"
    },
    "analyst_sentiment": {
      "consensus_rating": "Overweight",
      "recent_changes": [
        {"firm": "Morgan Stanley", "action": "upgrade", "target": 195, "date": "<YYYY-MM-DD>"}
      ],
      "target_range": {"low": 150, "median": 180, "high": 210},
      "source": "[WebSearch: analyst_consensus]"
    }
  },
  "event_density": {"next_30d": 2, "next_90d": 4, "assessment": "catalyst_rich"},
  "overall_event_bias": "moderately_positive",
  "confidence": "medium"
}
```

## Critical Rules

Source tagging and data handling rules are enforced by `.claude/rules/anti-hallucination.md`
(loaded automatically via glob). In addition:

- Catalyst dates from model memory are INVALID. Every date must have an API
  or WebSearch source. If you cannot source a date, omit the catalyst.
- WebSearch queries must use `{CURRENT_YEAR}`, never hardcoded past years.
- Past events (date < today) must be excluded from the catalyst calendar.
- 10b5-1 planned insider sales = neutral signal, not negative.
- Keep macro context thin. 3-5 headline factors, not a macro essay.
- Every field in the output must have a source tag per anti-hallucination rules.
