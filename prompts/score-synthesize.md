# Synthesis — Business Quality Verdict

You are synthesizing three independent dimension analyses into a unified
business quality (BQ) assessment and producing the final consolidated output.

Core question: **Based on all evidence, is this business worth adding to the watchlist?**

You are NOT making a buy/sell decision. You are assessing the business itself.

## Input

Three JSON files from Wave 1 scoring:
- `scores/fundamental.json` — Financial health and earnings power
- `scores/forward.json` — Future trajectory and direction of change
- `scores/industry.json` — Competitive position and industry dynamics

Also read:
- `data/00_validation.json` — Data freshness and validation status
- `strategy.yaml` — output_language, scoring weights (defaults if missing)

### Tier Context (delta-era, required)

You receive a `tier_context` block describing what kind of run this is:

```yaml
tier_context:
  tier: full | partial | no_op
  prior_synthesis_path: <path to prior synthesis.json, or null>
  pruned_catalyst_count: <int>
  low_signal_news_count: <int>         # only on no_op
  low_signal_headlines: [...]          # only on no_op
  dimensions_copied: [list]            # e.g. ["fundamental"] on partial, all three on no_op
```

**On `full` tier**: behave as pre-delta (all fields present in scores
are fresh; you derive all synthesis outputs from scratch). EXCEPT
`catalyst_calendar`: carry it from the dimension calendars (esp.
`scores/forward.json`'s `catalyst_density.calendar`), preserving each
item's `source` and `date_precision` verbatim. Synthesis does NOT
WebSearch or read raw API files, so never re-tag a carried date — keep
the forward earnings date's upstream `[WebSearch: ...]` source verbatim,
including its url + access-date binding
(`[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]`); never strip the
binding and never relabel it `[API: 07_earnings]`.

**On `partial` tier**: one or two dims were copied from the prior run.
Reconsider `watchlist_recommendation`, `conviction`, and the `thesis`
sentence in light of the fresh dims (forward + industry), but preserve
the fundamental dim's perspective if it's in `dimensions_copied`.

**On `no_op` tier**: do NOT re-derive scores, thesis, strengths, risks
or key_metrics. Read from `prior_synthesis_path` and:

- Copy verbatim: `watchlist_recommendation`, `conviction`, `thesis`,
  `key_strengths`, `key_risks`, `contradictions`, `key_metrics`.
- Modify `catalyst_calendar`: past entries are already pruned before
  you read (pruned_catalyst_count tells you how many); optionally
  append newly-discovered catalysts from low_signal news.
- Emit a new `delta_note` field (one sentence) describing what was
  observed this run and why the view is unchanged. This is what
  renders in `summary.md`'s "This Run's Update" section.

### Output: delta_note (new on no_op tier)

```json
{
  "delta_note": "No material news or filings since <YYYY-MM-DD prior full/partial date>; view unchanged."
}
```

Keep `delta_note` under 25 words. It's rendered verbatim in the delta
section of summary.md.

## Process

### 0. Handle Missing Dimensions

If any agent failed to produce output:
- With 2 of 3 dimensions: proceed, but set `conviction` to no higher than "low"
  and note the gap explicitly in synthesis
- With 1 or 0 dimensions: do NOT produce a verdict. Report the failure.

### 1. Cross-Validate

Before synthesizing, check for contradictions across dimensions:
- Fundamental says "margins improving" but Industry says "moat narrowing" — which is it?
- Forward says "strong catalysts" but Fundamental says "cash flow deteriorating" — is the
  catalyst enough to reverse the fundamental trend?
- Industry says "leader" but Forward says "management lacks strategic clarity" — is the
  leadership position sustainable without clear strategy?

Surface contradictions explicitly. Don't average them away — they are often the
most important signals.

### 2. Compute BQ Score

Weighted average of dimension scores. Default weights (override via strategy.yaml
`scoring.dimension_weights` if present):
- Fundamental: 35% — financial reality is the foundation
- Forward: 35% — trajectory matters as much as current state for growth investing
- Industry: 30% — structural position enables or constrains everything else

The overall BQ score is 1-10. But the score alone is not the verdict —
a company with BQ 7.0 and widening moat is different from BQ 7.0 with
compressing margins.

### 3. Determine Watchlist Recommendation

Based on BQ score AND the qualitative picture:

| BQ Score | Default Recommendation | Override Conditions |
|----------|----------------------|---------------------|
| 8.0+ | `strong_add` | Unless red flags in any dimension |
| 6.5-7.9 | `add` | Upgrade to strong_add if all dimensions aligned |
| 5.0-6.4 | `monitor` | Upgrade to add if ANY ONE dimension is genuinely high-conviction strong — roughly an 8+ / high-quality score on that dimension, NOT merely above-average — via OR-logic: forward trajectory strongly positive, OR industry position clearly advantaged (durable moat / structural leadership), OR current fundamentals clearly superior (high-quality, not merely profitable/stable). A single qualifying dimension admits to the pool even when the blended average sits in monitor range — do NOT require all three. When upgrading on one dimension, state explicitly which other dimensions remain weak and why they don't block admission. Keep `monitor` if no single dimension clears the ~8+ bar. |
| <5.0 | `pass` | Upgrade to monitor only with clear turnaround evidence |

### 4. Determine Conviction

Conviction reflects how CERTAIN you are about the BQ assessment, not how good
the company is. A company can have high BQ with low conviction (great business
but data is sparse) or low BQ with high conviction (clearly deteriorating).

- **high**: All three dimensions tell a consistent story, evidence is strong,
  no major contradictions, data is fresh
- **medium**: Generally consistent but with 1-2 contradictions or data gaps,
  or moderate evidence strength
- **low**: Significant contradictions across dimensions, stale data, thin
  evidence, or missing dimension(s)

### 5. Extract Key Thesis

Write a one-sentence investment thesis that captures WHY this business is
interesting (or not). This should be memorable and specific.

Bad: "AAPL is a high-quality tech company with strong fundamentals."
Good: "Apple's services transition is creating a recurring-revenue flywheel
that is structurally improving margins while its installed base moat widens."

### 6. Identify Top Risks

List the 3-5 most important risks, ranked by probability × impact.
Each risk should be specific and evidence-based, not generic.

Bad: "Competition could intensify."
Good: "Huawei's re-entry into premium smartphones with Kirin chips threatens
Apple's China revenue (18% of total), which declined 3% YoY last quarter [API: income_statements]."

### 7. Extract Key Metrics Dashboard

Surface the most important quantitative indicators from the dimension evidence
into a flat `key_metrics` object. These are NOT new calculations — they are
already in the dimension JSONs. Your job is to find the best available number
for each metric and surface it with its source tag.

The metrics answer five fundamental questions about business quality:

| Question | Metrics |
|----------|---------|
| Can it earn? | `eps_ttm`, `gross_margin`, `operating_margin` |
| Is it getting better? | `revenue_growth_yoy`, `eps_growth_yoy` |
| Is it capital-efficient? | `roic`, `capex_intensity`, `opex_ratio` (SGA+other, excludes R&D), `rd_intensity` |
| Is it safe? | `net_cash_or_debt` |
| Is the cash real? | `fcf_margin`, `fcf_to_net_income` |

For each metric:
- `value`: the number (string for percentages, number for absolute values)
- `trend`: "accelerating" / "expanding" / "stable" / "compressing" / "decelerating"
  (include only when meaningful — omit for point-in-time metrics like net cash)
- `interpretation`: one-line context when the number is misleading in isolation
  (e.g., "CapEx-heavy manufacturing" for a low FCF/NI ratio). Omit when obvious.
- `source`: standard source tag

If a metric is unavailable from the dimension data, omit it — do not fabricate.

### 8. Assess Data Freshness (optional)

The mechanical freshness note (days old, circuit breakers, EPS warnings) is
computed by the assembly script. If you see data quality issues that need
interpretive context — e.g., GAAP vs non-GAAP EPS divergence, unusual API
warnings — add a `freshness_interpretation` field in synthesis.json.
The script will append it to the mechanical note.

If no interpretation is needed, omit `freshness_interpretation`.

## Output — synthesis.json

Write ONLY the synthesis section. The mechanical assembly of dimension
scores, meta, and the full `bq_analysis.json` is handled by
`scripts/assemble.py` — you do NOT write `bq_analysis.json`.

Note: do NOT emit a `business_quality` field. That value is computed by
`scripts/assemble.py` from dimension scores + weights and would be
silently discarded if present here (see `assemble.py:391` —
`synthesis.pop("business_quality", None)`).

```json
{
  "watchlist_recommendation": "add",
  "conviction": "medium",
  "thesis": "One sentence investment thesis",
  "key_strengths": [
    "Specific strength with evidence reference"
  ],
  "key_risks": [
    {"risk": "Specific risk description", "probability": "medium", "impact": "high"}
  ],
  "contradictions": [
    "Description of any cross-dimension contradictions found"
  ],
  "catalyst_calendar": [
    {"event": "Next earnings", "date": "YYYY-MM-DD", "date_precision": "confirmed", "impact": "high", "direction": "uncertain", "source": "[WebSearch: company IR earnings calendar, https://ir.example.com/events, accessed <YYYY-MM-DD>] [API: 06_analyst_estimates, fiscal_period quarter-end]"},
    {"event": "Contract / partnership / product catalyst", "date": "YYYY-MM-DD", "date_precision": "estimated", "impact": "medium", "direction": "positive", "source": "[API: 03_company_news, Reuters product-launch headline]"}
  ],
  "key_metrics": {
    "eps_ttm": {"value": 12.25, "unit": "$/share", "source": "[API: income_statements]"},
    "eps_growth_yoy": {"value": "73.0%", "trend": "accelerating", "source": "[Calc: eps_ttm vs eps_ttm_prior_year]"},
    "revenue_growth_yoy": {"value": "84.3%", "trend": "accelerating", "source": "[API: income_statements]"},
    "gross_margin": {"value": "74.4%", "trend": "expanding", "source": "[API: income_statements]"},
    "operating_margin": {"value": "57.8%", "trend": "expanding", "source": "[API: income_statements]"},
    "fcf_margin": {"value": "17.7%", "trend": "expanding", "source": "[Calc: fcf_ttm / revenue_ttm]"},
    "fcf_to_net_income": {"value": "40%", "interpretation": "CapEx-heavy, cash conversion below earnings", "source": "[Calc: fcf_ttm / net_income_ttm]"},
    "roic": {"value": "28.5%", "trend": "expanding", "source": "[Calc: ebit_ttm * (1 - tax_rate) / invested_capital]"},
    "net_cash_or_debt": {"value": "$3.1B net cash", "source": "[Calc: cash - total_debt]"},
    "capex_intensity": {"value": "26.8%", "interpretation": "High — capital-intensive manufacturing", "source": "[Calc: capex_ttm / revenue_ttm]"},
    "opex_ratio": {"value": "16.6%", "trend": "compressing", "source": "[Calc: opex_ttm / revenue_ttm]"},
    "rd_intensity": {"value": "7.8%", "source": "[API: income_statements]"}
  },
  "freshness_interpretation": "Optional — only when validation data needs interpretive context"
}
```

Do NOT include:
- `meta` (assembled mechanically from validation data)
- `scores` (computed mechanically as weighted average of dimension scores)
- `dimensions` (copied verbatim from score files by the assembly script)

## Output — summary.md

Write a one-page summary (under 800 words) in the language specified by
`output_language` in strategy.yaml (default: zh-CN).

If the combined `summary.md` (This Run's Update section ≤150 words +
Current View section ≤650 words) exceeds 800 words after you've
truncated low-priority items (low_signal_news list, optional agent
notes), log a warning to `run_meta.warnings` but still emit the
output. The machine artifact `bq_analysis.json` is unaffected; the
≤800 constraint applies only to `summary.md`.

Structure:
1. **Verdict line**: Ticker | BQ score | Recommendation | Conviction | One-line thesis
2. **Key metrics table**: 6-8 most telling numbers from key_metrics (compact table)
3. **Bull case** (2-3 bullets): What's working, with evidence
4. **Bear case** (2-3 bullets): What could go wrong, with evidence
5. **Key contradictions** (if any): Where the dimensions disagree
6. **Catalyst calendar**: Next 3-6 months of key events — factual dates only,
   no investment impact analysis (mark estimated dates with ~)
7. **Bottom line**: One paragraph synthesis — is this a high-quality business?

This is the ONLY human-facing output from score-business. Make it sharp,
opinionated, and useful for a fast decision on whether to dig deeper.
All evidence references should point back to specific data
(e.g., "[Q3 revenue +12% YoY]") but do not need full source tags — those
are in the JSON.

## DL3c — Currency note (conditional)

The assembled `bq_analysis.json` may carry a `currency_conversion` block at
root when the underlying statements are non-USD and were FX-converted to USD
by upstream producers (extract_fcf / historical_multiples / adr/correct). The
block is present iff `basis == "usd_converted"`; USD-native artifacts emit no
`currency_conversion` key (invariant 7).

When the block IS present, render a one-line note immediately under the
Verdict line in `summary.md` and translate to the configured
`output_language`:

> **Currency note**: Statements originally in `{source_currency}`; valuation
> metrics converted to USD at quarter-end FX (`{fx_source}`). FX risk is
> excluded from the intrinsic-value calculation.

Fields read from `bq_analysis.currency_conversion`:
- `source_currency` — ISO 4217 (e.g. `JPY`, `EUR`)
- `fx_source` — producer identifier (e.g. `yfinance:JPY=X`)

When the block is absent (USD-native or legacy pre-DL3c), DO NOT render any
currency note. Absence ⇔ usd_native by contract.

The cert block is propagated by `scripts/assemble.py` (§3.7.4) from any
`post_dl3c_usd_converted` input artifact; consumers of this prompt read it
via the typed loader `scripts/schemas/bq_analysis.load_bq_analysis`.
