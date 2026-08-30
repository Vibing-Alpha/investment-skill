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
- Beat/miss history (8 quarters minimum) — look for the PATTERN, not just stats.
  **Data reality:** `07_earnings.json` carries only the LATEST report, not a
  history array. Its `surprise_eps_pct` and `surprise_revenue_pct` name their
  own basis; use them. `surprise_pct` is a deprecated alias of the EPS figure
  kept for older artifacts — it sits between the revenue keys and reads as the
  revenue surprise, which is how five of six tickers were misread in one run
  (RKLB: 48.56% EPS quoted as a 1.05% revenue beat). Never read `surprise_pct`
  as a revenue number. Build the history from WebSearch (bound tags) and/or by
  comparing `06_analyst_estimates` vs actuals in `02_financial_data`; if you
  cannot ground ≥4 quarters, say "beat/miss history data-limited (N quarters)"
  and reduce the weight of this signal — never fabricate quarters.
- Estimate revision trend — are analysts raising or cutting?
  **Data reality:** `06_analyst_estimates.json` is a POINT-IN-TIME snapshot
  (no revision timestamps, no high/low spread). Revision direction must come
  from WebSearch (bound tags); absent that, mark the revision trend `unknown`
  — unknown must not tilt the score as if it were neutral.
  **The LEVEL can be stale too, and there is no timestamp to tell you.**
  Before quoting a forward estimate, sanity-check it against the two things
  you can see: the latest reported actual in `02_financial_data`, and the
  company's own guidance (WebSearch, bound tag). Measured misses in one run:
  RKLB's next-quarter revenue estimate was $217.0M — BELOW the $234.1M it had
  already reported and the $250M guidance floor; BE's was $871.9M against a
  $1,065.4M actual; SNDK's $6.853B / $26.82 EPS against guidance of
  $10.3–10.8B / $44–46 (−33% / −40%). An estimate below the last actual, or
  outside issued guidance, is a reason to CHECK, not a finding on its own:
  it happens in 21 of 41 stored artifacts, and seasonality or a genuine
  contraction produce the same shape. There is no timestamp that settles
  it. Guidance is what settles it — when a company's own issued range
  contradicts the snapshot, the guidance wins and you say the snapshot
  predates it. Absent guidance, mark the estimate `unverified` and do not
  read it as a slowdown. For a company spun off or
  IPO'd within about two years, treat vendor consensus as unusable unless a
  bound source corroborates it (SNDK's vendor Q1 FY27 EPS was $0.650 against
  guidance of $44–46).
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

**Filing access**: Read the filing artifacts in the data directory:
`05_filing_summary.json` (structured metadata) plus the `05_filing_*.md`
extracts (Item 1/1A/7, 10-Q Item 2, revenue notes). There is NO
pre-processed "filing intelligence" file in v7 — do not search for one;
quote directly from the .md extracts.

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
  - `confirmed`: **the issuer or the exchange itself has published this exact
    date** — a company IR events page, a press release, an 8-K, an exchange
    notice. The item's own `source` tag must be that publication. No
    third-party or aggregator calendar can establish `confirmed`, however
    exact its date looks; and where such a source labels its own date
    unconfirmed or estimated, `confirmed` is forbidden outright — repeating
    the date while dropping its publisher's caveat is the error, not the date.
  - `estimated`: inferred from historical patterns, analyst expectations, or
    ANY third-party calendar — this is where a precise-looking secondary
    date belongs.
  - `approximate`: only a rough timeframe (quarter, half-year)
  - Why this is stricter than it looks: the value is carried verbatim through
    synthesis into `bq_analysis.synthesis.catalyst_calendar`, which a human
    reads and acts on. `confirmed` asserts that the date has been PUBLISHED,
    so a reader stops looking — a false one is a certainty nobody can audit,
    and it suppresses the one action that would fix a wrong date (going to
    find the issuer's own announcement). Downstream consumers deliberately
    treat `confirmed` and `estimated` the same where day-precision is all
    they need, so this rule buys honesty of provenance, not a different
    branch: an `estimated` date you are unsure of costs nothing, a
    `confirmed` one you invented costs the reader their check.
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
        {"quarter": "Q? 20XX", "estimate": 0.00, "actual": 0.00, "surprise_eps_pct": 0.0}
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
