# Forward-Looking Scoring

You are evaluating a company's **future trajectory and direction of change**.

Core question: **Is this business getting better or worse over the next 6-18 months?**

This is forward-looking. You are assessing WHERE things are headed, not where
they are now (that's the fundamental dimension's job). You own the management
deep-dive — the fundamental dimension only records basic revenue/profit facts.

**Before scoring, read `prompts/references/scoring-calibration.md` §Forward &
Industry.** Its cyclical-peak rule is load-bearing here: distinguish a secular
trajectory from a late-cycle tailwind before scoring forward direction — a demand
inflection that is really a cycle peak is not a durable catalyst.

## WebSearch preflight & source binding (hard gate)

This dimension's methodology REQUIRES current external information
(catalyst dates, guidance, analyst actions).

1. **Preflight — run FIRST.** Before producing any analysis content,
   execute ONE real WebSearch tool call (e.g.
   `"<TICKER> stock news {CURRENT_YEAR}"`). If the WebSearch tool is
   unavailable on this host or the call errors: STOP and report exactly
   `cannot complete: host lacks WebSearch`. Never fall back to model
   memory, and never emit a `[WebSearch: ...]` tag without a real search
   result behind it.
2. **Bound tag form.** Every WebSearch-sourced claim must bind
   outlet + url + access-date:
   `[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]` — the url is the
   actual page consulted (http/https, no whitespace; percent-encode any
   comma in it), the access date is today's run date. Multiple sources →
   multiple tags. Bare `[WebSearch: outlet]` tags fail the runtime
   validator and abort the run.

## Dimensions

### 1. EPS Expectations (weight: 20)

What does the MARKET expect, and how does this company perform against those expectations?

This is about external consensus — what analysts collectively predict. It is
distinct from Guidance Quality, which is about what management themselves say.
The two can diverge significantly (management guides conservatively while
analysts model aggressively, or vice versa).

Analyze:
- Consensus EPS estimates for next quarter and next fiscal year
- Beat/miss history (8 quarters minimum) — look for the PATTERN, not just stats
- Estimate revision trend — are analysts raising or cutting?
- Spread between highest and lowest estimate (consensus tightness)

A company that consistently beats by 5%+ with rising estimates has a fundamentally
different trajectory than one that alternates beats and misses.

Scoring anchors:
- 9-10: Consistent beats, estimates rising, tight consensus
- 7-8: Mostly beats, stable or rising estimates
- 5-6: Mixed beat/miss record, flat estimates
- 3-4: Recent misses or estimate cuts
- 1-2: Serial misser, estimates collapsing

### 2. Guidance Quality (weight: 20)

How useful and reliable is MANAGEMENT'S OWN forward outlook?

This is about what management says and whether they deliver on it. It is
distinct from EPS Expectations, which is about what external analysts predict.

Analyze:
- Does the company provide quantitative guidance? (some don't — see below)
- Historical accuracy — does guidance tend to be conservative or aggressive?
- Guidance breadth — revenue only, or full P&L?
- Recent guidance changes — raised, maintained, or lowered?

The best companies give conservative, specific guidance and consistently exceed it.

**Companies without quantitative guidance**: Some excellent companies (Berkshire,
certain non-US firms) choose not to provide numeric guidance. In these cases:
- Score based on qualitative forward commentary (conference calls, shareholder letters)
- Assess whether management's qualitative statements have been directionally accurate
- Do not penalize below 4 solely for absence of numeric guidance
- Note the absence explicitly in evidence

Scoring anchors:
- 9-10: Specific, conservative guidance with strong beat-and-raise track record
- 7-8: Clear guidance, generally reliable, occasional raise
- 5-6: Provides guidance but accuracy is mixed
- 3-4: Vague, frequently missed, or no guidance with unclear qualitative signals
- 1-2: Guidance is consistently misleading or management avoids all forward commentary

### 3. Management Credibility (weight: 25)

Does this management team deliver on what they promise?

This is the DEEP management analysis. Go beyond surface-level bios. This
dimension gets the highest weight because management quality is the strongest
predictor of whether current business strengths will persist or erode.

Analyze:
- **Execution track record**: Compare past promises to actual results. Find
  specific examples from filings (direct quotes with source tags)
- **Capital allocation**: Are they good stewards of shareholder capital?
  (buybacks at reasonable valuations, disciplined M&A, sensible CapEx)
- **Transparency**: Do they acknowledge problems honestly, or spin narratives?
- **Insider alignment**: Skin in the game (ownership), compensation structure
- **Strategic consistency**: Are they pivoting too often, or staying the course?

Use filing quotes. "Management said X in Q{QUARTER} {CURRENT_YEAR} 10-Q, and Y actually happened"
is 10x more valuable than "management seems competent."

**Filing access**: Read SEC filing summaries from the data directory. If
pre-processed filing intelligence files exist (e.g., `filing_intelligence.json`),
use those first for efficiency — they contain pre-extracted key passages sorted
by signal strength. Only read raw filing text when you need surrounding context
for a specific quote.

Scoring anchors:
- 9-10: Exceptional track record, strong insider alignment, honest communicators
- 7-8: Solid execution, good capital allocation, mostly transparent
- 5-6: Adequate management, no major red flags but no standouts
- 3-4: Mixed execution, questionable capital allocation, or spin-heavy
- 1-2: Poor track record, value-destroying decisions, trust deficit

### 4. Strategic Clarity (weight: 20)

Is the company's strategy clear, coherent, and executable?

Analyze:
- Can you explain the company's strategy in one sentence? (if not, it lacks clarity)
- Does the strategy play to the company's strengths?
- How does the strategy relate to industry tailwinds/headwinds?
- Is CapEx/R&D allocation aligned with stated strategy?
- Any strategic pivots in the last 2 years — were they reactive or proactive?

Scoring anchors:
- 9-10: Crystal clear strategy, well-resourced, aligned with structural trends
- 7-8: Clear strategy with reasonable resource allocation
- 5-6: Strategy is visible but execution path is uncertain
- 3-4: Muddled strategy or reactive pivoting
- 1-2: No coherent strategy, or strategy misaligned with reality

### 5. Catalyst Density (weight: 15)

What specific events could materially change the thesis in the next 6-18 months?

Identify and assess:
- Earnings dates (next 2 quarters)
- Product launches or major milestones
- Regulatory decisions
- Contract wins/renewals
- Industry events or conferences
- M&A activity (acquirer or target)

For each catalyst:
- Date or expected timeframe (MUST come from API or WebSearch, never memory)
- Date precision — be honest about what you actually know:
  - `confirmed`: company or exchange has published the exact date
  - `estimated`: inferred from historical patterns or analyst expectations
  - `approximate`: only a rough timeframe (quarter, half-year)
- The forward **earnings** date comes from WebSearch (earnings calendar /
  company IR); `06_analyst_estimates` `fiscal_period` is the quarter-END
  (≈weeks before the report), so cite it only as corroboration, never as the
  date itself, and never tag a forward date `[API: 07_earnings]`.
- Potential impact (high/medium/low)
- Direction (positive/negative/uncertain)
- Source

Scoring anchors:
- 9-10: Multiple near-term positive catalysts with high visibility
- 7-8: Several identifiable catalysts, mostly positive
- 5-6: Few catalysts, or balanced positive/negative
- 3-4: Limited catalysts, or upcoming risks dominate
- 1-2: No visible catalysts, or major negative events ahead

## Output Format

Write a JSON file with this structure:

```json
{
  "dimension": "forward",
  "ticker": "AAPL",
  "overall": 7.0,
  "sub_scores": {
    "eps_expectations": {"score": 7, "weight": 20},
    "guidance_quality": {"score": 8, "weight": 20},
    "management_credibility": {"score": 7, "weight": 25},
    "strategic_clarity": {"score": 7, "weight": 20},
    "catalyst_density": {"score": 6, "weight": 15}
  },
  "evidence": {
    "eps_expectations": {
      "data_points": [],
      "interpretation": "",
      "beat_miss_history": [
        {"quarter": "Q? 20XX", "estimate": 0.00, "actual": 0.00, "surprise_pct": 0.0}
      ]
    },
    "guidance_quality": {
      "data_points": [],
      "interpretation": "",
      "guidance_available": true,
      "guidance_track_record": "conservative"  // conservative | accurate | aggressive | unavailable
    },
    "management_credibility": {
      "data_points": [],
      "interpretation": "",
      "key_quotes": [
        {"quote": "Specific management promise", "source": "[Filing: QN YYYY 10-Q, management_discussion]", "outcome": "What actually happened"}
      ]
    },
    "strategic_clarity": { "data_points": [], "interpretation": "" },
    "catalyst_density": {
      "data_points": [],
      "interpretation": "",
      "calendar": [
        {"event": "Next earnings", "date": "YYYY-MM-DD", "date_precision": "confirmed", "impact": "high", "direction": "uncertain", "source": "[WebSearch: company IR earnings calendar, https://ir.example.com/events, accessed <YYYY-MM-DD>] [API: 06_analyst_estimates, fiscal_period quarter-end]"},
        {"event": "Contract / partnership / product catalyst", "date": "YYYY-MM-DD", "date_precision": "estimated", "impact": "medium", "direction": "positive", "source": "[API: 03_company_news, Reuters product-launch headline]"}
      ]
    }
  },
  "red_flags": [],
  "key_insight": "One sentence: the single most forward-looking signal about this company"
}
```

## Critical Rules

Source tagging and data handling rules are enforced by `.claude/rules/anti-hallucination.md`
(loaded automatically via glob). In addition:

- Compute `overall` as weighted average: `sum(score × weight) / 100`
- Management quotes must include filing source (e.g., "Q{QUARTER} {CURRENT_YEAR} 10-Q")
- News-sourced catalysts (contract wins, partnerships, product launches) are
  tagged `[API: 03_company_news, ...]`, NOT `[News: ...]` — the only canonical
  KINDs are API/WebSearch/Filing/Calc; the assembler fail-closes on anything else
- EPS data from API is primary; WebSearch consensus is supplementary — label which is which
- ADR currency mix: if `02_financial_data.json` carries a `currency_consistency`
  block with `status: "mixed_unrepairable"`, treat statement-derived figures as
  suspect — some fields are FX-converted to USD while others stay native, so any
  ratio mixing the two is wrong by the FX factor. Use the USD-clean fields
  (`revenue`, `net_income`) for trajectory and cite filings / WebSearch for the rest.
- Do not assess valuation (P/E, target price, etc.) — that is not this dimension's job
