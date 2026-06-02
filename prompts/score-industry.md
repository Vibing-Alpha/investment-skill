# Industry Scoring

You are evaluating a company's **position within its industry and competitive landscape**.

Core question: **How strong and durable is this company's competitive position?**

This is structural analysis — it looks across time rather than at a specific moment.
You assess the industry environment AND the company's position within it. You do
NOT analyze the company's financials (that's fundamental's job) or future catalysts
(that's forward's job).

**Before scoring, read `prompts/references/scoring-calibration.md` §Forward &
Industry.** Score durable / secular competitive advantage, not a late-cycle demand
peak — the cyclical-peak rule applies to industry-growth durability too.

## Industry Scoping

Before scoring, explicitly define WHICH industry you are analyzing.

**Single-industry companies**: Straightforward — state the industry and proceed.

**Multi-industry companies** (e.g., Amazon: cloud + retail + ads):
- Identify the PRIMARY industry by revenue contribution
- If no single segment dominates (>50% of revenue), analyze the top 2 segments
  separately and note the split
- State your scoping decision in the output: `"industry_scope": "Cloud (AWS, 62% of operating income)"`
- Score based on the scoped industry, not a blended average

This scoping decision matters — Amazon scores very differently when analyzed
as a cloud company vs a retail company.

## Dimensions

### 1. Industry Lifecycle (weight: 25)

Where is this industry on the growth curve, and how attractive is it structurally?

Analyze:
- **S-curve position**: What's the penetration rate of the core technology/product?
  - <10%: Emerging (high uncertainty, high upside)
  - 10-30%: Acceleration (sweet spot for growth investors)
  - 30-60%: Growth maturity (growth slowing, competition intensifying)
  - >60%: Mature (focus shifts to market share and efficiency)
- **TAM and growth rate**: How big is the addressable market? Growing at what rate?
- **Structural drivers**: What's fueling growth? (secular trend vs cyclical demand)
- **Industry structure**: Of Porter's Five Forces, identify the ONE or TWO forces
  that matter most for THIS specific industry and explain why. A semiconductor
  industry where supplier power (ASML monopoly) is the dominant force looks
  completely different from a SaaS market where switching costs dominate.

Scoring anchors:
- 9-10: Acceleration phase, large TAM, strong secular tailwinds, favorable structure
- 7-8: Growth phase with clear drivers, moderate competition
- 5-6: Mixed signals — growing TAM but intensifying competition, or mature but stable
- 3-4: Maturing industry with slowing growth, or high rivalry eroding economics
- 1-2: Declining industry, structural headwinds, commoditization

### 2. Competitive Moat (weight: 30)

How defensible is this company's market position?

This is the highest-weighted dimension because moat durability is the single
best predictor of long-term business quality persistence.

Analyze:
- **Market position**: Market share (absolute and trend), customer base quality
- **Moat type identification** — which of these apply, and how strong?
  - Cost advantage (scale economies, vertical integration)
  - Switching costs (ecosystem lock-in, integration depth)
  - Network effects (platform value grows with users)
  - Intangible assets (patents, brand, regulatory licenses)
  - Efficient scale (market too small for another entrant)
- **Moat trend**: Is the moat widening, stable, or narrowing?
  Specific evidence matters — "losing 3pp share to competitor X in segment Y
  over 2 years [WebSearch: source]" is useful; "faces competition" is not.
- **Competitive dynamics**: Any recent moves by competitors that change the picture?

Scoring anchors:
- 9-10: Dominant position with widening moat, multiple moat sources
- 7-8: Strong position with at least one durable moat, stable share
- 5-6: Reasonable position but moat is single-source or being tested
- 3-4: Weak moat, losing share, or single advantage under threat
- 1-2: No meaningful moat, commodity business, or position actively deteriorating

### 3. Value Chain Position (weight: 25)

Where does this company sit in the industry value chain, and does it have pricing power?

This is especially important for AI/semiconductor/space sectors where value
chain position determines who captures the economics.

Analyze:
- **Position in value chain**: upstream (components/IP) vs midstream (manufacturing)
  vs downstream (end products/services)
- **Supplier concentration**: Does the company depend on a few key suppliers?
- **Customer concentration**: Does it depend on a few key customers?
  (Note: some of this data may be available from API financial data — check
  revenue segment data and 10-K risk factors before defaulting to WebSearch)
- **Pricing power evidence**: Can it raise prices without losing volume?
  Look for gross margin stability through inflationary periods.
- **Supply chain classification**:
  - Sole-source / exclusive supplier (strongest)
  - Preferred / qualified supplier
  - Multi-source but differentiated
  - Commodity / interchangeable (weakest)

Scoring anchors:
- 9-10: Critical node in value chain, sole-source or standard-setter, strong pricing power
- 7-8: Preferred supplier with differentiation, demonstrated pricing power
- 5-6: Multi-source but differentiated, moderate pricing power
- 3-4: Interchangeable supplier, limited pricing power, or high customer concentration
- 1-2: Commodity position, price-taker, high supplier AND customer concentration

### 4. Technology & Innovation (weight: 20)

Is this company a technology leader or follower?

Analyze:
- **R&D intensity**: R&D as % of revenue, trend, and comparison to peers
  (R&D data available from API financial statements — check before WebSearch)
- **Technology position**: Leader, fast-follower, or laggard in key technology areas?
- **Product roadmap visibility**: Are next-generation products visible and credible?
- **Patent/IP portfolio**: Meaningful competitive barrier, or defensive filings?
- **Technology adoption cycle**: Is the company's core technology in the
  innovation trigger, peak of inflated expectations, trough of disillusionment,
  slope of enlightenment, or plateau of productivity? Note: this is directional
  guidance, not a precise measurement — different sources may place the same
  technology differently.

Scoring anchors:
- 9-10: Clear technology leader, robust R&D pipeline, setting industry direction
- 7-8: Strong technology position, competitive R&D, clear product roadmap
- 5-6: On par with peers, adequate R&D but no standout innovation
- 3-4: Technology follower, under-investing in R&D relative to peers
- 1-2: Falling behind technologically, no visible catch-up path

## Currency-Mixed Statements (ADR)

R&D intensity — and ANY ratio you compute from `02_financial_data.json` — is
only valid when numerator and denominator share one currency. For foreign ADRs
the feed sometimes returns a USD/native MIX, so check the `currency_consistency`
block before computing R&D ÷ revenue:

- `status: "mixed_unrepairable"` — some statement fields were FX-converted to
  USD while others remain in the native currency (e.g. `revenue` in USD but
  `research_and_development` / `cost_of_revenue` still native). Computing
  `R&D ÷ revenue` straight from the file is then wrong by the FX factor (often
  ~30x — e.g. 170% instead of ~5%). Do NOT report that number. Recompute on ONE
  currency basis — convert revenue to native using the detector's `implied_fx`
  (read it from `currency_consistency.detector.implied_fx`, the native-per-USD
  rate) and tag `[Calc: R&D_native ÷ (revenue_usd × implied_fx)]` — or take R&D
  intensity from a filing / WebSearch with a source tag. Record the data issue
  in `red_flags`. The artifact self-documents this in
  `currency_consistency.partial_conversion_warning`.
- `status: "repaired"` — the listed `converted_fields` have already been
  converted to USD by the repair step; use them and tag
  `[Calc: native ÷ implied_fx]`.

## Output Format

Write a JSON file with this structure:

```json
{
  "dimension": "industry",
  "ticker": "AAPL",
  "overall": 7.5,
  "industry_scope": "Consumer electronics + services ecosystem",
  "sub_scores": {
    "industry_lifecycle": {"score": 7, "weight": 25},
    "competitive_moat": {"score": 9, "weight": 30},
    "value_chain_position": {"score": 8, "weight": 25},
    "technology_innovation": {"score": 7, "weight": 20}
  },
  "evidence": {
    "industry_lifecycle": {
      "data_points": [],
      "interpretation": "",
      "s_curve_stage": "growth_maturity",   // emerging | acceleration | growth_maturity | mature
      "penetration_rate": "~45% globally [WebSearch: Statista {CURRENT_YEAR}]",
      "dominant_force": "Switching costs — iOS ecosystem lock-in drives 90%+ retention"
    },
    "competitive_moat": {
      "data_points": [],
      "interpretation": "",
      "moat_types": ["switching_costs", "intangible_assets", "network_effects"],
      "moat_trend": "stable"              // widening | stable | narrowing
    },
    "value_chain_position": {
      "data_points": [],
      "interpretation": "",
      "supply_chain_class": "preferred"     // sole_source | preferred | multi_source | commodity
    },
    "technology_innovation": {
      "data_points": [],
      "interpretation": "",
      "rd_intensity": "7.2% of revenue [API: income_statements]",
      "tech_position": "leader"              // leader | fast_follower | laggard
    }
  },
  "peer_tickers": ["MSFT", "GOOGL", "005930.KS"],
  "red_flags": [],
  "key_insight": "One sentence: the most important thing about this company's industry position"
}
```

Note: `peer_tickers` lists 3-5 closest publicly traded competitors. This is
output for potential use by downstream modules (timing, valuation), not consumed
within the current analysis. Tickers must be yfinance-compatible: use US tickers
for NYSE/NASDAQ-listed companies, or add the exchange suffix for non-US stocks
(e.g., `AWE.L` for London, `005930.KS` for Korea, `9984.T` for Tokyo).
Do not use company names or unofficial abbreviations as tickers.

## Critical Rules

Source tagging and data handling rules are enforced by `.claude/rules/anti-hallucination.md`
(loaded automatically via glob). In addition:

- Compute `overall` as weighted average: `sum(score × weight) / 100`
- Market share and TAM data require WebSearch with source name and publication date
- R&D and revenue segment data: check API financial data first, WebSearch only for
  market-level data (TAM, share, penetration) that APIs don't provide
- Every moat claim needs specific evidence, not generic descriptions
- Do not analyze financial performance (fundamental's job) or valuation (timing layer)
- State industry scoping decision explicitly for multi-industry companies
