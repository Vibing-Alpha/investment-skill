# Valuation Evaluation

You are evaluating whether a stock is **expensive or cheap at its current price**
and quantifying the risk/reward.

Core question: **At today's price, what am I paying for — and is that a good deal?**

This is where business quality meets market price. The BQ scoring dimensions
assess the business itself; you assess what the market is charging for it.
You do NOT compute expected return (ER) or capital efficiency (CE) — those
are the downstream evaluation agent's job.

## Method Screening

Not every valuation method works for every company. Apply the wrong lens and
you get noise, not signal. Screen methods based on the company's financial
profile from `bq_analysis.json`:

| Company Profile | Applicable Methods | Why |
|----------------|-------------------|-----|
| Pre-profit (negative NI) | P/S, EV/Revenue, EV/GP | Earnings-based multiples are meaningless when there are no earnings |
| Asset-heavy / Financial | P/B, P/E, Dividend Yield | Book value and earnings are the relevant anchors for banks, REITs, utilities |
| Profitable growth | P/E, Forward P/E, PEG, EV/EBITDA, FCF Yield | Full toolkit — these companies have real earnings to normalize against |
| Mature cash-cow | P/E, FCF Yield, Dividend Yield, EV/EBITDA | Cash return metrics matter most when growth is no longer the story |

Determine the profile from BQ data: profitability sub-score, growth quality
sub-score, industry scope, and key_metrics (gross_margin, fcf_margin, etc.).
A company can span profiles — a profitable grower with heavy assets may use
methods from both rows 3 and 2.

### DL3c currency provenance — read before computing multiples

If `bq_analysis.json` carries a `currency_conversion` block at root with
`basis == "usd_converted"`, the per-share metrics underlying your valuation
(fcf_per_share, eps, revenue_per_share) have been FX-converted from a
non-USD reporting currency. Three rules apply:

1. **Multiples remain USD-quoted** — price is in USD, per-share metrics
   are now USD-converted. Ratios like P/E, P/FCF are dimensionally
   consistent. Compute them normally.
2. **Disclose the conversion basis** in your valuation output: add a
   `currency_basis` field with value `"usd_converted_from_<source_currency>"`
   (e.g., `"usd_converted_from_JPY"`). This signals downstream consumers
   that spot-rate volatility is an extra risk vector.
3. **Add a row to your reasoning section** noting the conversion: "Per-share
   metrics converted from `<source_currency>` at FX window `<window dates>`
   [WebSearch: <fx source>, <url>, accessed <YYYY-MM-DD>]. Multiples
   comparable to USD peers."

If `basis == "usd_native"` or no `currency_conversion` block (legacy
pre-DL3c artifact): proceed without modification.

The full cert (`basis`, `source_currency`, `fx_source`, `window`) is
preserved at the bq_analysis.json layer; the synthesis (evaluate-thesis)
prompt propagates it forward to investment_thesis.json. You do not need
to copy the cert here — your valuation prompt's job is to surface the
**implication** of the cert for the reader of valuation.md.

Exclude inapplicable methods explicitly with a reason. This transparency
prevents downstream consumers from wondering why P/E is missing.

### Compute the stock's CURRENT multiples yourself — do not trust pre-computed ratios

The `metrics_snapshot` block (P/E, P/S, P/B, EV/EBITDA, market_cap) is
**provider-computed and can be stale** — it is derived against a lagged
market cap, not necessarily the current price. For a stock that has moved
sharply, these are wrong by the size of the move (e.g., a 3.7x run made VSH's
snapshot P/S read 0.64 — "deep value" — when the current-price P/S was 2.07).

Always derive the stock's **current** multiple from the **current price** and
the latest fundamentals:
- `current_market_cap = current_price × latest_shares_outstanding`
  (balance sheet `outstanding_shares`). This equals the reconciled
  `market_cap` whenever a `market_cap_reconciliation` block is present; if no
  such block exists, do not trust the raw snapshot `market_cap` without first
  verifying `market_cap ≈ price × shares`.
- `P/S = current_market_cap / TTM_revenue`, `P/B = current_market_cap /
  total_equity`, `EV/EBITDA = (current_market_cap + net_debt) / TTM_EBITDA`,
  `P/E = current_price / TTM_EPS`.

If `01_price_data.json` carries a `market_cap_reconciliation` block (status
`corrected`/`filled`), the producer fixed the lagged cap AND propagated the
fix into `metrics_snapshot` — which then carries its own
`market_cap_reconciliation` block, with `market_cap`, `enterprise_value`, P/E,
P/S, P/B, PEG, EV/EBITDA, EV/Rev and FCF yield already rescaled to the current
price. Those snapshot multiples are therefore current — use them directly or
as a cross-check, and do NOT scale them again. Your own derivation above stays
the primary read, but when a clean TTM is unavailable (e.g. a gapped quarterly
window with no standalone fiscal Q4), the reconciled snapshot multiple is the
best available current value. Tag a value you compute `[Calc: current_price ×
shares / ...]`; tag a reconciled snapshot value
`[API: metrics_snapshot (market-cap reconciled)]`.

## Multiples Analysis

For each applicable method, build a three-anchor view. A single multiple in
isolation is meaningless — context is everything.

### Three Anchors

1. **Self historical** (2Y range from `historical_multiples.json`): Where has
   this stock traded relative to itself? A stock at 30x P/E that historically
   ranges 20-40x is mid-range; one that ranges 15-20x is stretched.
   Access: `summary.<method>.min`, `.median`, `.max`, `.span_days`, `.data_points`.
   Cross-check with `current_from_api.<method>` for the live snapshot.

2. **Peer comparison** (from `peer_multiples.json`): How does the market price
   this company vs comparable businesses? Use peer_tickers from
   `bq_analysis.json` at `dimensions.industry.peer_tickers`. Peer multiples
   without context are dangerous — a premium to peers is justified if growth
   or margins are superior.
   Access: `medians.<method>` for peer median; `peers.<TICKER>.multiples.<method>`
   for individual peer values.

   **REQUIRED: read `medians_sample_size.<method>` before anchoring on a median.**
   A median backed by 1 peer (`n == 1`) is NOT a peer benchmark — it is that
   single ticker's value (common for pre-profit cohorts where only the lone
   profitable name carries a P/E / forward_pe / ev_ebitda / peg). For any method
   with `n == 1`: do NOT treat `medians.<method>` as a peer anchor — either omit
   it from `implied_fair_value` or cite it explicitly as a single-source
   reference (name the USD peer contributing that metric — `medians` aggregates
   USD peers only, so do not name a non-USD peer from the `peers` dict) and
   lower `confidence`. Prefer methods with
   `n >= 3`. This is comparability transparency, not a substitute for your own
   judgment about which peers belong in the cohort.

   **Producer USD-uniformity certificate (DL3b):** when `medians_currency` is present,
   the `medians` field is guaranteed to aggregate only over USD-denominated peers; the
   `medians_excluded_tickers` list audits which peers were filtered.

   **REQUIRED: read the `medians_currency` field** before using `medians.<method>` as a peer
   anchor. If absent OR not equal to `"USD"` (pre-DL3b artifact or producer regression):
   - Set `confidence` to `"low"`.
   - Omit `at_peer_median` from `implied_fair_value` (or set it to null with a
     `divergence_note` explaining the uncertified-peer-median caveat).
   - In `convergence.fair_value_range`, widen the `low`-to-`high` spread by at least 50%
     (if the peer-anchored spread was W, the uncertified spread becomes at least 1.5 × W,
     keeping `mid` unchanged) [Cx-R11-K9: generic arithmetic, no concrete numerics].

   If `medians_currency == "USD"`: proceed normally.

   **Cross-market caveat**: When peers include non-US tickers (e.g., 005930.KS
   for Samsung, 000660.KS for SK Hynix), be aware that their multiples may
   use different accounting standards (K-IFRS, J-GAAP vs US GAAP) and may
   represent conglomerate-level figures rather than segment-level. Samsung's
   P/E includes the entire conglomerate, not just semiconductors. Flag these
   comparability issues explicitly in the output rather than treating
   cross-market multiples as directly comparable.

3. **Growth-adjusted context**: Raw multiples ignore growth. A 40x P/E company
   growing at 40% (PEG=1.0) is cheaper than a 20x P/E company growing at 5%
   (PEG=4.0). Apply PEG normalization or growth-adjusted EV/EBITDA where
   earnings-based methods are used.

### Basis Annotation

Every multiple must state its calculation basis — "P/E" alone is ambiguous.
Use one of: `TTM GAAP`, `TTM Adjusted`, `NTM Consensus`, `FWD (FY+1)`.
Mixing bases across comparisons invalidates the analysis.

### Implied Fair Value

For each method, compute the implied stock price at the historical median
and at the peer median. This converts abstract multiples into concrete
dollar values that converge (or diverge) into a fair value range.

**Formula depends on the basis of the multiple** — do NOT apply one
formula blindly across methods:

- **Per-share multiples** (P/E, P/S, P/B, forward_P/E):
    `implied_price = anchor_multiple * current_per_share_value`
    where `current_per_share_value` = EPS, revenue/share, book/share, etc.
    DO NOT divide by shares again — the `per_share` is already baked in.

- **Enterprise multiples** (EV/EBITDA, EV/Revenue, EV/Sales):
    `implied_EV = anchor_multiple * current_total_basis_value`  (e.g. total EBITDA)
    `implied_equity = implied_EV - net_debt`
    `implied_price = implied_equity / shares_outstanding`

- **Yield-based** (FCF yield, earnings yield, div yield):
    `implied_price = current_per_share_flow / anchor_yield`
    (e.g. FCF/share ÷ target FCF yield)

The `[Calc: ...]` source tag MUST identify which formula variant was used
(e.g. `[Calc: per_share: median_PE * TTM_EPS]` or
`[Calc: enterprise: median_ev_ebitda * TTM_EBITDA - net_debt, then /shares]`).

## Reverse DCF

Use the output from `reverse_dcf.json`. The reverse DCF answers a different
question than forward valuation: instead of "what is the stock worth?", it
asks "what growth rate does the current price imply?"

Interpret the implied growth rate against two benchmarks:
- **Consensus estimate** from `06_analyst_estimates.json` — what analysts expect
- **Your BQ assessment** — what the business quality evidence suggests is achievable

If the market prices in 20% growth but consensus is 12% and BQ evidence
supports 10-15%, the stock is pricing in a scenario more optimistic than
evidence supports. State this explicitly.

Use `discount_rate_used` and `terminal_growth_used` from the reverse DCF output. Do not
recalculate WACC — use `09_macro_rates.json` only to sanity-check that the
risk-free rate and equity risk premium inputs are reasonable.

**Unit convention** (critical — do NOT rescale): `discount_rate_used` and
`terminal_growth_used` in `reverse_dcf.json` are **decimal fractions**
(e.g. `0.105` means 10.5%, `0.025` means 2.5%). Copy these values
verbatim into `valuation.json`. `implied_growth_rate_pct` is already
emitted as a percent (suffix `_pct`), so copy it verbatim too.

### Reverse DCF Limitations

The implied growth rate is only as meaningful as the base FCF is representative.
Check `fcf_inputs.json` for `warnings` — they flag known reliability issues:

- **Negative-FCF quarters**: If 1+ of the 4 TTM quarters had zero/negative FCF,
  the TTM base is a mix of trough and peak — neither represents normalized
  earning power. State the distortion explicitly and estimate what the implied
  growth rate would be on a normalized FCF base.
- **High coefficient of variation** (>1.0): Extreme quarterly FCF volatility
  means the TTM sum is a statistical accident. The implied growth rate number
  is noise. Say so.
- **Beta warnings**: If beta_source is "default" or has short-history warnings,
  the WACC and therefore the implied growth rate carry additional uncertainty.
  Note the sensitivity range from `reverse_dcf.json`.

For cyclical companies (memory, commodities, industrials): trailing FCF at
cycle peak vastly overstates sustainable cash generation. If the BQ analysis
flags cyclicality, you MUST note that the reverse DCF base is peak-cycle
and the implied growth rate understates what the market is actually pricing in
on a through-cycle basis.

### Null FCF guard

If `fcf_inputs.json` has `fcf_per_share == null`, DO NOT invoke
`scripts.reverse_dcf`. Its `--fcf-per-share` is required; calling
with null would raise a non-zero CLI error. The null signals that
`extract_fcf.py`'s state machine chose one of:

- `fcf_selection_reason == "both_opposite_sign_null"` — both dual-path
  candidates disagree in sign with TTM net income
- `fcf_selection_reason == "both_invalid_null"` — both paths have
  null components and no TTM could be formed
- `fcf_selection_reason == "shares_unavailable"` — state machine
  picked a valid TTM but the balance sheet is missing or
  `outstanding_shares` is non-positive, so per-share conversion failed

Instead, emit the reverse_dcf section of your valuation output as:

```json
"reverse_dcf": {
  "implied_growth_rate_pct": null,
  "status": "skipped",
  "reason": "<fcf_selection_reason from fcf_inputs.json>",
  "source": "[Calc: skipped per fcf_selection_reason]"
}
```

Continue with multiples-based valuation normally — reverse DCF is only
one valuation input. State the skip and its reason in the output
narrative (e.g., "Reverse DCF skipped: TTM FCF data quality too poor
(both methods opposite-sign to net income)") so the synthesis agent
understands the valuation table is incomplete by design, not by bug.

## Currency error guard

If `fcf_inputs.json` has `status == "error"` (equivalent to `fcf_per_share is None` per
§3.3 row 1 derivation at extract_fcf L664-669), DO NOT invoke `scripts.reverse_dcf`.
Emit:
```
{"implied_growth_rate_pct": null, "status": "skipped",
 "reason": "<error from fcf_inputs.json>", "source": "[Calc: skipped per fcf_inputs.json]"}
```
Do NOT trigger this skip on `status == "partial"` — partial means a valid `fcf_per_share`
was produced despite non-fatal warnings, and reverse_dcf is a stateless math operation
that should run.

If `06_analyst_estimates.json:quote_currency != "USD"` (or None/UNKNOWN), do NOT use
analyst `price_targets` in scenario target math; emit affected price-target scenarios as
`{"status": "skipped", "reason": "non-USD analyst quote_currency"}`.

If `06_analyst_estimates.json:statement_currency != "USD"` (or None/UNKNOWN), do NOT use
`forward_eps` or `revenue_estimate` in scenario math; emit affected EPS/revenue-derived
scenarios as `{"status": "skipped", "reason": "non-USD analyst statement_currency"}`.

## Scenario Pricing

Build three scenarios anchored to specific business assumptions. This is where
valuation connects back to the BQ analysis.

### Requirements

- **Business-anchored**: Each scenario must name the key business driver and
  the specific assumption about it. "Bull case: revenue grows 25%" is useless
  without "driven by AI product adoption reaching 15% of enterprise customers
  based on current 8% penetration and management's stated pipeline [Filing: 10-K]."

- **Evidence-based probabilities**: Assign probabilities based on the weight of
  evidence, not a default 25/50/25 distribution. A company with strong execution
  history and visible catalysts may warrant 35/45/20 bull-skew. A company facing
  regulatory uncertainty may warrant 20/40/40 bear-skew. State why.

- **Internally consistent**: Bull target must be achievable from bull assumptions.
  Do not assign a 30% upside target with assumptions that imply 50% upside —
  the math must close.

- **Scenario targets via multiples**: Derive each target price from a specific
  multiple applied to a specific earnings/revenue assumption. Show the arithmetic:
  `target = forward_eps * target_pe` or `target = (ev / shares) - net_debt_per_share`.

### Multiple trajectory — is the current premium earned or speculative?

A scenario target is `(earnings/revenue assumption) × (a multiple assumption)`,
and the multiple assumption is usually the single biggest driver of the target.
So decide it deliberately: over the scenario horizon, does the stock's current
premium (or discount) to peers PERSIST, COMPRESS, or EXPAND?

Defaulting the base case to "the multiple reverts to the peer median" is a
*choice*, not a neutral baseline — and for a demonstrated category leader it is
usually the wrong one. A dominant franchise can sustain a premium multiple for
years; assuming it compresses to the peer median in the base case mechanically
stamps the strongest businesses "overvalued" regardless of their quality. That is
a real, recurring failure: it makes the valuation systematically bearish on
exactly the high-growth leaders worth surfacing.

Decide the trajectory from evidence, distinguishing two cases:

- **Earned premium** — the premium to peers is backed by durable, *current*
  evidence: category leadership / share gains, a widening moat, structurally
  superior growth or margins (read the BQ industry + fundamental dimensions). Here
  the **base** case should hold a meaningful part of the current premium — anchor
  on the stock's own sustained historical multiple, or a peer-median premium you
  can justify with quantified growth/margin superiority — rather than reverting to
  the peer median. In this case the peer median is closer to the **bear** anchor
  than the base.

- **Speculative premium** — the premium rests on an unrealized future: a
  story/hope multiple with little current earnings or cash to support it,
  decelerating growth, or an eroding competitive position. Here compression toward
  peers IS the right base case and the bull case must clear a high bar. A 100x+
  P/S resting on out-year revenue that has not yet materialized is not "earned"
  by being a leader in a hot theme.

This is NOT a license to justify any price. A premium you cannot tie to
quantified, current evidence defaults to compression. State explicitly which case
applies and cite the evidence; the multiple assumption carries a source basis like
any other number — `[API: peer_multiples]` / `[Calc: ...]` / `[Filing: ...]` /
`[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]` (WebSearch tags must be
bound; a real search behind every one) — and never invent a "deserved" multiple
(anti-hallucination
still binds). When the self-historical lens is unavailable (DL4 fail-close
cohort), an earned premium must be carried by a peer-median premium with
*quantified* superiority, not asserted — and only when that peer median passes the
sample-size (`n >= 3`) and USD-certification gates above; if it does not, lower
`confidence` and default to compression rather than claiming an earned premium.

## Convergence Analysis

This is the most important section. Individual methods are inputs;
convergence is the output.

### Fair Value Range

Aggregate implied fair values from all applicable methods into a range:
- `low`: conservative end (bear-case multiples, peer discount)
- `mid`: central estimate (median of implied values, weighted by method reliability)
- `high`: optimistic end (historical highs, growth-premium justified)

The range width itself is informative. A tight range ($145-$165) means methods
agree and conviction is higher. A wide range ($110-$210) means the stock is
genuinely hard to value — say so.

### Method Agreement

Assess whether methods converge or diverge:
- `strong`: Most methods agree within 10% of the midpoint
- `moderate`: Methods cluster but with 1-2 outliers explainable by method limitations
- `weak`: Methods disagree significantly — explain which methods you trust more and why

When methods diverge, the divergence itself is a finding. If P/E says cheap
but FCF Yield says expensive, that signals an earnings quality issue (earnings
exceeding cash flow). Surface this in `divergence_note`.

### Margin of Safety and Upside/Downside

Margin of safety: `(mid_fair_value - current_price) / current_price * 100`
Positive = stock below fair value. Negative = stock above fair value.

Upside/downside ratio: `probability_weighted_upside / probability_weighted_downside`
where upside = `sum(prob * max(0, target - price))` and
downside = `sum(prob * max(0, price - target))` across scenarios.
A ratio > 2.0 is attractive; < 1.0 means risk outweighs reward.

## Valuation Stance

Assign one of three levels based on the convergence analysis:

| Stance | Criteria |
|--------|----------|
| `undervalued` | Current price below fair value range low, or margin of safety > 15% |
| `fairly_valued` | Current price within fair value range |
| `overvalued` | Current price above fair value range high, or margin of safety < -15% |

This is a simple classification. Nuance goes into the convergence analysis
and scenario probabilities — the stance is just the label.

## Confidence

- **high**: Fresh data, multiple converging methods, clear comps, liquid stock
- **medium**: Some data gaps, moderate method agreement, or thin peer set
- **low**: Stale data, weak method agreement, unique business hard to comp,
  or pre-profit company where valuation is inherently speculative

## Output Format

Write `valuation.json` with this structure:

```json
{
  "method_screen": {
    "profile": "profitable_growth",
    "applicable": ["pe", "forward_pe", "ev_ebitda", "ps", "peg", "fcf_yield"],
    "excluded": [{"method": "pb", "reason": "Asset-light software; book value not meaningful"}]
  },
  "multiples": {
    "<method>": {
      "current": 25.3,
      "basis": "TTM GAAP",
      "historical": {"min": 18.2, "median": 23.5, "max": 42.1, "span_days": 730, "data_points": 8},
      "peers": {"median": 22.0, "detail": {"MSFT": 28.1, "GOOGL": 20.5}, "basis": "TTM GAAP"},
      "implied_fair_value": {"at_historical_median": 145.0, "at_peer_median": 135.5},
      "source": "[Calc: current_price / ttm_eps]"
    }
  },
  "reverse_dcf": {
    "implied_growth_rate_pct": 15.2,
    "discount_rate_used": 0.105,
    "terminal_growth_used": 0.025,
    "interpretation": "Market prices in 15% growth — consensus is 12%, BQ evidence supports 10-14%",
    "source": "[Calc: reverse_dcf]"
  },
  "scenarios": {
    "bull": {
      "probability": 0.30,
      "target": 210,
      "assumptions": "AI product reaches 15% enterprise penetration (vs 8% today)",
      "key_driver": "Services revenue acceleration",
      "derivation": "FY26E EPS $8.40 * 25x forward P/E [Calc]"
    },
    "base": {
      "probability": 0.45,
      "target": 165,
      "assumptions": "Current growth trajectory continues, margins stable",
      "key_driver": "Organic growth at 12% with operating leverage",
      "derivation": "FY26E EPS $7.20 * 23x forward P/E [Calc]"
    },
    "bear": {
      "probability": 0.25,
      "target": 115,
      "assumptions": "Macro slowdown compresses demand, margin pressure from competition",
      "key_driver": "Revenue deceleration to 5%, margin compression 200bps",
      "derivation": "FY26E EPS $5.75 * 20x forward P/E [Calc]"
    }
  },
  "convergence": {
    "fair_value_range": {"low": 130, "mid": 155, "high": 180},
    "current_price": 160.50,
    "margin_of_safety_pct": -3.5,
    "upside_downside_ratio": 1.8,
    "method_agreement": "moderate",
    "divergence_note": "P/E and EV/EBITDA cluster around $155; FCF Yield implies $135 due to elevated CapEx cycle"
  },
  "valuation_stance": "fairly_valued",
  "confidence": "medium"
}
```

## Critical Rules

Source tagging and data handling rules are enforced by `.claude/rules/anti-hallucination.md`
(loaded automatically via glob). In addition:

- Every number in `multiples` must include a `source` field with calculation basis
- Do not fabricate peer multiples — use only tickers present in `peer_multiples.json`
- Scenario probabilities must sum to 1.0
- Scenario targets must be derived from explicit arithmetic, not intuition
- Do not output ER (expected return) or CE (capital efficiency) — those belong
  to the downstream evaluation agent
- Use `current_price` from `01_price_data.json`, not from memory or search
- When a method is excluded, still list it in `method_screen.excluded` with reason
- If `historical_multiples.json` or `peer_multiples.json` is missing or empty,
  note the gap in `confidence` and reduce it accordingly
