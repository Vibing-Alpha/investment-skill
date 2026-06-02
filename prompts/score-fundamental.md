# Fundamental Scoring

You are evaluating a company's **current financial health and earnings power**.

Core question: **How good is this business at making money?**

This is a backward-looking + present-state assessment. You are analyzing what IS,
not what WILL BE (that's the forward dimension's job). You are not assessing the
stock price or valuation — only the business itself.

**Before assigning sub-scores, read `prompts/references/scoring-calibration.md`.**
Its load-bearing rule is cyclical normalization (§4): for a cyclical name, score
through-cycle earning power, not the trailing peak or trough — this is what most
changes the fundamental score. It also carries guardrails for which D/E basis to
use (interest-bearing `total_debt/equity`, NOT the total-liabilities
`metrics_snapshot.debt_to_equity` label), poisoned/null capital-efficiency ratios
alongside a negative-PP&E anomaly, and GAAP-vs-non-GAAP EPS divergence.

## Dimensions

### 0. Revenue Structure (no score — context for all other dimensions)

Before scoring, understand WHERE the money comes from. This frames everything else.

Analyze along three axes:
- **Business segments**: What products/services generate revenue? What % each?
- **End markets**: Who are the customers? (enterprise, consumer, government, etc.)
- **Geography**: Revenue split by region. Concentration risk?

This section is not scored — it provides context. A company with 80% revenue
from one government contract has a fundamentally different risk profile than one
with diversified SaaS subscriptions, even if the numbers look similar.

Output this as a `revenue_structure` field in the evidence section.

### 1. Profitability (weight: 30)

How efficiently does this company convert revenue into profit?

Why highest weight: profitability is the most durable signal of business quality.
Growth can be bought (with debt or dilution), but sustained high margins reflect
genuine competitive advantages in pricing power, cost structure, or operating leverage.

Analyze:
- Gross margin level and trend (3+ years)
- Operating margin vs industry peers
- Net margin trajectory — improving or compressing?
- DuPont decomposition: margin × turnover × leverage — which lever drives ROE?
- SBC impact on real profitability (especially for tech companies)

**Pre-profit companies**: Don't automatically score low. Instead assess:
- Unit economics trajectory (gross margin trend, contribution margin)
- Path to profitability — is there a visible inflection point?
- Deliberate growth investment vs structural inability to profit
- A pre-profit company with improving unit economics may score 4-5

Scoring anchors:
- 9-10: Best-in-class margins, expanding, strong operating leverage
- 7-8: Above-peer margins, stable or slightly improving
- 5-6: In-line with peers, no clear trend
- 3-4: Below peers, compressing margins, or pre-profit with unclear path
- 1-2: Structurally unprofitable with no credible path to profitability

### 2. Growth Quality (weight: 25)

Is revenue/earnings growth real, sustainable, and high-quality?

Why this weight: growth drives long-term compounding, but only if it's real.
Acquisition-fueled or one-time growth destroys value when it stops.

Analyze:
- Revenue growth rate and acceleration/deceleration (quarterly trend)
- EPS growth vs revenue growth — is growth flowing to the bottom line?
- Organic vs acquisition-driven growth
- Revenue concentration risk (top customer dependency, informed by Revenue Structure)
- Growth consistency — steady compounding vs lumpy/cyclical

Focus on the QUALITY of growth, not just the rate. 20% growth from
a sticky SaaS subscription base is higher quality than 30% growth from
one-time government contracts.

Scoring anchors:
- 9-10: Accelerating high-quality growth, strong unit economics
- 7-8: Consistent double-digit growth, mostly organic
- 5-6: Moderate growth or mixed quality signals
- 3-4: Decelerating or heavily acquisition-dependent
- 1-2: Declining revenue or negative earnings growth

### 3. Balance Sheet (weight: 25)

Can this company survive a downturn? How much financial flexibility does it have?

Why this weight: balance sheet strength determines survivability. A company with
great margins but crushing debt can still go bankrupt.

Analyze:
- Debt/equity ratio and net debt position
- Interest coverage ratio
- Current ratio / quick ratio
- Cash runway (especially important for pre-profit companies)
- Debt maturity profile — any near-term refinancing risk?
- Goodwill as % of total assets (>25% is a red flag — potential impairment risk)

Scoring anchors:
- 9-10: Net cash, minimal debt, fortress balance sheet
- 7-8: Conservative leverage, strong coverage ratios
- 5-6: Moderate leverage, adequate but not exceptional
- 3-4: High leverage, tight coverage, refinancing risk
- 1-2: Distressed, covenant risk, liquidity concerns

### 4. Cash Flow (weight: 20)

Is this company generating real cash, or are the profits an accounting illusion?

Why lowest weight: cash flow is a validation check on profitability. Important,
but less informative as a standalone signal than margins or growth quality.

Analyze:
- Free cash flow (FCF) generation and trend
- FCF margin vs net income margin — cash conversion quality
- Operating cash flow consistency
- CapEx intensity and whether it's maintenance vs growth CapEx
- Working capital trends (inventory build, receivables stretching)

Red flag: net income positive but FCF negative for 2+ quarters. This is HIGH
severity — it often signals earnings quality problems (aggressive revenue
recognition, capitalized expenses, or working capital deterioration).

Scoring anchors:
- 9-10: Strong, growing FCF; FCF exceeds net income (clean earnings)
- 7-8: Consistent positive FCF, reasonable conversion
- 5-6: Positive but lumpy FCF, or heavy growth CapEx consuming cash
- 3-4: Weak or inconsistent FCF despite reported profits
- 1-2: Persistent cash burn with no clear path to FCF positive

## Growth Stock Mode

If the company shows growth stock characteristics, it needs adapted analysis.

**Read `prompts/references/growth-stock-analysis.md`** for the trigger signals
and full methodology (unit economics, SBC analysis, adjusted ROIC).

In brief, growth stock mode adds three lenses:
1. **Unit economics** (ARPU, CAC, LTV) — score profitability by unit economics
   trajectory, not GAAP margins
2. **SBC + operating leverage** — separate real cost structure from non-cash charges;
   check if the business gets more efficient as it scales
3. **Adjusted ROIC** — strip excess cash to reveal core business efficiency

When growth mode applies, include a `growth_mode` section in the output JSON
(format defined in the reference file).

## ADR Stock Handling

If the company is an ADR (American Depositary Receipt):
- Per-share metrics from API may be unreliable (ADR ratio distortion)
- Prefer total figures: total revenue, net income, total cash flow
- Calculate per-share metrics manually if needed: `total / shares_outstanding`
- Note `"adr_adjusted": true` in evidence when corrections are applied
- **Currency-repaired statements**: if `02_financial_data.json` carries a
  `currency_consistency` block with `status: "repaired"`, the listed
  `converted_fields` were FX-derived from the implied rate (the FDS feed
  returned a USD/native mix under a "USD" label). Tag those values as
  `[Calc: native ÷ implied_fx]`, NOT `[API: ...]`, and surface the conversion
  basis in evidence.
- **`status: "mixed_unrepairable"` — DO NOT compute ratios from the rows.**
  This is a trap. On this path the statement rows are left PARTIALLY
  converted: a subset (commonly the valuation master set) was converted to USD.
  ⚠️ The per-field split below is **empirically derived (MRAAY/Murata),
  NOT guaranteed per issuer** — FDS converts a different subset for some
  issuers (observed counter-example — KYOCY/Kyocera left `interest_expense`,
  `current_debt` and `non_current_debt` in JPY while converting `total_debt`,
  contradicting the list below). So before treating ANY field as already-USD,
  VERIFY it by magnitude against `currency_consistency.detector.implied_fx`:
  a native field (e.g. JPY at ~150/USD) is ~10²–10³× its USD value, so a
  `revenue` near a few billion sitting beside a `cost_of_revenue` in the
  hundreds of billions means cost_of_revenue is still native. Trust the
  magnitude check over the list when they disagree. The common-case set is,
  per statement family:
  - **income statement**: `revenue`, `operating_income`, `net_income`,
    `interest_expense`, `net_income_non_controlling_interests`
  - **balance sheet**: `cash_and_equivalents`, `total_debt`, `current_debt`,
    `non_current_debt`, `shareholders_equity`
  - **cash-flow statement**: `net_cash_flow_from_operations`,
    `depreciation_and_amortization`, `capital_expenditure`,
    `investment_acquisitions_and_disposals`,
    `issuance_or_repayment_of_debt_securities`

  EVERY other money field stays in the native currency — including
  `cost_of_revenue`, `gross_profit`, `ebit`, `ebitda`, `income_tax_expense`,
  `research_and_development`, `total_assets`, `total_liabilities`,
  `current_investments`, and (watch out) the cash-flow statement's
  `free_cash_flow` and its `net_income`. NOTE the per-statement scope:
  `net_income` is USD on the income statement but native on the cash-flow
  statement; and `free_cash_flow` is native even though its components
  (OCF, capex) are USD — so an `FCF/revenue` margin off the raw `free_cash_flow`
  field is mixed, while `(OCF − capex)/revenue` is consistent USD.
  Yet every row is still tagged `currency: "USD"` (that tag is load-bearing
  for downstream FX producers; it does NOT mean the whole row is USD).
  Therefore any cross-field ratio you compute from these raw rows is
  currency-MIXED and WRONG — e.g. `gross_profit_native / revenue_usd`
  understates gross margin by the FX factor (a real IFNNY case: computed
  33.8% vs true ~38.7%). The same trap hits operating margin, current ratio,
  cash ratio, FCF margin, and `total_liabilities/equity`. So: **do not compute
  or report margins / leverage / liquidity ratios straight from the statement
  rows on this path.** Prefer company-reported figures (latest 10-K/20-F/press
  release via filings or WebSearch), tagged `[Filing: …]` / `[WebSearch: …]`.
  If you must derive a ratio from the rows, first put numerator and
  denominator on ONE currency basis using
  `currency_consistency.detector.implied_fx` (the native-per-USD rate — same
  approach as the industry dimension) and tag the explicit direction, e.g.
  `[Calc: native_field ÷ implied_fx → USD]` or
  `[Calc: usd_field × implied_fx → native]`. Use the raw rows directly only
  for currency-INVARIANT signals (YoY/QoQ growth direction of a single field,
  sign, magnitude bands) and flag the limitation in `red_flags` +
  `data_quality_caveats`. See `currency_consistency.partial_conversion_warning`
  in the artifact.

## Output Format

Write a JSON file with this structure:

```json
{
  "dimension": "fundamental",
  "ticker": "AAPL",
  "overall": 7.5,
  "sub_scores": {
    "profitability": {"score": 8, "weight": 30},
    "growth_quality": {"score": 7, "weight": 25},
    "balance_sheet": {"score": 8, "weight": 25},
    "cash_flow": {"score": 7, "weight": 20}
  },
  "evidence": {
    "revenue_structure": {
      "segments": [
        {"name": "iPhone", "pct": 52, "trend": "stable", "source": "[API: income_statements]"}
      ],
      "end_markets": "Consumer 70%, Enterprise 30% [WebSearch: source]",
      "geography": "Americas 43%, Europe 25%, China 18%, Rest 14% [API: segmented_revenues]",
      "concentration_risk": "No single customer >10% of revenue"
    },
    "profitability": {
      "data_points": [
        "Gross margin 46.2%, up from 43.3% YoY [API: income_statements]",
        "Operating margin 31.5% vs industry median 22% [WebSearch: S&P Capital IQ]"
      ],
      "interpretation": "Best-in-class margins driven by services mix shift...",
      "comparison_anchor": "Industry median gross margin 38% [WebSearch: source]"
    },
    "growth_quality": { "data_points": [], "interpretation": "", "comparison_anchor": "" },
    "balance_sheet": { "data_points": [], "interpretation": "", "comparison_anchor": "" },
    "cash_flow": { "data_points": [], "interpretation": "", "comparison_anchor": "" }
  },
  "red_flags": [],
  "key_insight": "One sentence: the single most important thing about this company's fundamentals"
}
```

## Critical Rules

Source tagging and data handling rules are enforced by `.claude/rules/anti-hallucination.md`
(loaded automatically via glob). In addition:

- Compute `overall` as weighted average: `sum(score × weight) / 100`
- Each sub-score must include at least one comparison anchor (historical, peer, or industry)
- Use API data as primary source; WebSearch only when API is insufficient
- Do not reference or depend on the stock's current price for any scoring
- Revenue Structure has no score — it is context, not a graded dimension
