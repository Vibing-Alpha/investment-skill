# Growth Stock Analysis — Extended Methodology

This reference is loaded conditionally when a company shows growth stock
characteristics. It EXTENDS the standard fundamental scoring — it does not
replace it. All four fundamental dimensions still apply, but their
interpretation adapts.

## When to Use This Reference

Apply growth stock analysis when the company shows 2+ of these signals:
- Net income negative for the trailing 4 quarters
- Gross margin > 50% (asset-light business model)
- SBC > 15% of revenue
- Cash + short-term investments > 40% of total assets
- Revenue growing > 25% YoY with no clear profitability yet

Use judgment — a biotech with no revenue is different from a SaaS company
with 60% gross margins burning cash on sales. This methodology is designed
for the latter category (high-gross-margin companies investing for growth).

## 1. Unit Economics

For companies where user/customer metrics are more informative than
traditional financial ratios.

### Core Metrics

| Metric | Formula | What it tells you |
|--------|---------|-------------------|
| ARPU | Total Revenue / Active Users | Revenue per user |
| CAC | S&M Expenses / Net New Users | Cost to acquire a user |
| LTV | ARPU × Gross Margin / Churn Rate | Lifetime value per user |
| LTV/CAC | LTV / CAC | Unit economics health |
| Payback | CAC / (ARPU × Gross Margin) | Months to recoup acquisition cost |

### Health Benchmarks

| Metric | Excellent | Healthy | Warning | Danger |
|--------|-----------|---------|---------|--------|
| LTV/CAC | > 5x | 3-5x | 1-3x | < 1x |
| Payback | < 6 mo | 6-12 mo | 12-18 mo | > 18 mo |
| CAC trend | Declining >20% | Declining | Flat | Rising |
| ARPU trend | Rising >10% | Rising | Flat | Declining |

### Data Sources (priority order)

- User counts (DAU/MAU): SEC filings > earnings call transcripts > third-party estimates
- ARPU: Calculate from revenue/users, or company disclosure
- Churn: Company disclosure > industry average > assume 10-15% **annual** churn
  (do not use this fallback for monthly churn — if monthly churn is unknown, do not compute LTV)
- SBC: API cash flow statements (`share_based_compensation`)

When user metrics are not publicly available, note the limitation and
use whatever proxy is available (subscribers, paid seats, etc.).

### How This Affects Fundamental Scoring

- **Profitability**: A pre-profit company with LTV/CAC > 3x and improving
  unit economics has demonstrated business model viability — score based on
  unit economics trajectory, not current GAAP margins
- **Growth Quality**: User growth + ARPU expansion is higher quality than
  revenue growth alone. Decompose revenue growth into user growth × ARPU change
- **Cash Flow**: For growth companies, evaluate FCF excluding growth investment.
  "What would FCF be if they stopped growing?" is the key question

## 2. SBC and Non-GAAP Analysis

Stock-based compensation is often the single largest gap between GAAP and
economic reality for tech companies. Analyze it, don't just add it back.

### SBC Impact Assessment

| SBC / Revenue | Assessment |
|---------------|------------|
| < 10% | Low — manageable dilution |
| 10-15% | Moderate — typical for tech |
| 15-25% | Elevated — monitor dilution trends |
| 25-35% | High — significant shareholder dilution |
| > 35% | Excessive — question whether growth is real or funded by dilution |

### Non-GAAP Adjustments

Legitimate add-backs (one-time, non-recurring):
- Restructuring charges
- Acquisition-related costs
- Litigation settlements
- Impairment charges

SBC is NOT a legitimate add-back for economic analysis — it is a real cost
to shareholders via dilution. However, showing both GAAP and GAAP-ex-SBC
reveals the operating leverage trajectory.

### Operating Leverage Check

The most important signal for growth companies: is the business becoming
more efficient as it scales?

```
Operating leverage = Revenue growth rate vs Expense growth rate (ex-SBC)

Strong positive:  Revenue +40%, Expenses +20% → clear operating leverage
Neutral:          Revenue +30%, Expenses +28% → growth but no leverage
Negative:         Revenue +20%, Expenses +35% → scaling problems
```

If revenue is growing faster than expenses (excluding SBC), the company
will eventually become profitable through scale — the question is when.
If expenses are growing faster than revenue, the business model may not work.

### How This Affects Fundamental Scoring

- **Profitability**: Show both GAAP and ex-SBC operating margin. Score based
  on ex-SBC trajectory IF operating leverage is positive. If operating leverage
  is negative, GAAP profitability is the right lens.
- **Cash Flow**: SBC is a non-cash charge — FCF may be positive even when net
  income is negative. This is real cash but comes at the cost of dilution.
  Note both: "FCF positive due to SBC add-back; dilution rate X% annually"

## 3. Adjusted ROIC

Traditional ROIC is misleading for cash-rich tech companies because excess
cash (not used in operations) dilutes the denominator.

### Calculation

```
Step 1: Minimum operating cash
  Min_Cash = (G&A + S&M) × 1.25
  (12 months of operating expenses + 25% safety buffer)

Step 2: Adjusted invested capital
  Excess_Cash = Total_Cash - Min_Cash
  Adjusted_IC = Working_Capital - Excess_Cash + Net_PPE

Step 3: Core business ROIC
  Core_ROIC = FCF / Adjusted_IC
```

### Why This Matters

Example:
- Book ROIC: FCF $216M / Total IC $3,500M = 6.2% (looks mediocre)
- Core ROIC: FCF $216M / Adjusted IC $886M = 24.4% (excellent business)
- The difference: $2.6B in excess cash sitting on the balance sheet

### ROIC Trajectory Signals

| Trend | Signal | Implication |
|-------|--------|-------------|
| Negative → Positive | Business model validated | Inflection point |
| Positive and rising | Scale economics working | Strong signal |
| Positive but falling | Efficiency eroding | Investigate cause |
| Persistently negative | Business model unproven | Caution |

### How This Affects Fundamental Scoring

- **Balance Sheet**: High cash is normally positive, but for growth companies,
  also assess how efficiently cash is deployed. A company sitting on $5B while
  earning 2% ROIC is hoarding, not building.
- **Cash Flow**: Pair FCF with Adjusted ROIC to distinguish "generates cash
  because the business is efficient" from "generates cash because SBC covers
  real expenses"

## Output Extension

When growth stock analysis applies, add a `growth_mode` section to the
fundamental score JSON:

```json
{
  "growth_mode": {
    "enabled": true,
    "trigger_signals": ["net_income_negative", "gross_margin_above_50", "sbc_above_15"],
    "unit_economics": {
      "arpu": {"value": 15.2, "trend": "rising", "source": "[Filing: 10-K FY2025]"},
      "cac": {"value": 45.0, "trend": "declining", "source": "[Calc: S&M / net new users]"},
      "ltv_cac_ratio": 4.2,
      "payback_months": 8,
      "health": "healthy"
    },
    "sbc_analysis": {
      "sbc_revenue_pct": 18.5,
      "assessment": "elevated",
      "dilution_annual_pct": 3.2,
      "operating_leverage": "positive",
      "revenue_growth_vs_expense_growth": "+40% vs +22%"
    },
    "adjusted_roic": {
      "book_roic_pct": 6.2,
      "core_roic_pct": 24.4,
      "excess_cash_pct": 65,
      "trajectory": "positive_and_rising"
    }
  }
}
```

**Not-computable unit-economics metrics.** CAC, LTV/CAC, and payback are
*derived* (tag KIND `[Calc:]`) and need a net-new-customer count that is
rarely in the fetched financial statements — so they are *commonly* not
derivable. When the input is missing, set `value: null` and
`trend: "not_computable"`, but you MUST still give a canonical source tag —
one of `[API: ...]`, `[WebSearch: ...]`, `[Filing: ...]`, `[Calc: ...]` —
naming the blocked derivation. Never put bare explanatory prose in a
`source` field: the `bq_analysis` loader (`scripts/schemas/source_tag.py`)
fail-closes on any `source` value that lacks a canonical tag (and also on
`source: null`), which aborts `assemble`:

```json
{
  "cac": {"value": null, "trend": "not_computable", "source": "[Calc: S&M / net-new customers; denominator unavailable in fetched data]"}
}
```

For bare-number fields (`ltv_cac_ratio`, `payback_months`) there is no
`source` sub-key — use `null` directly. For a *disclosed-channel* input that
simply isn't present (e.g. ARPU is read from a filing, not derived), keep the
channel tag (`[Filing: ...]` / `[API: ...]`), not `[Calc:]`.
