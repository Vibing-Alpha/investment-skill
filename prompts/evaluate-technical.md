# Technical Evaluation

You are evaluating a stock's **current price/volume pattern and key technical levels**.

Core question: **Does the current price action support entering a position, and where are the key levels?**

This is observational analysis. You do NOT make buy/sell recommendations. Present
the technical picture so downstream agents can use it for sizing, timing, and risk.

## Input

- `data/01_price_data.json` — `{snapshot, historical: {daily, weekly, sma_20, sma_50}}`
  - `snapshot`: price, week_52_high, week_52_low, volume, previous_close
  - `historical.daily`: ~125 bars (6mo), `{time, open, high, low, close, volume}`
  - `historical.weekly`: ~104 bars (2yr), same OHLCV format
- `data/indicators.json` — pre-computed by `scripts/indicators.py`:
  - `macd`: `{macd_line, signal_line, histogram, crossover, hist_trend, zero_side}`
    - `hist_trend` ∈ `expanding|contracting|flat|reversal`
      - `reversal` = histogram changed sign between the last two bars
        (zero-crossing). Treat as inflection, not a fade — concurrent
        with `crossover=golden|death`.
      - `expanding`/`contracting` compare `|hist_t|` vs `|hist_{t-1}|` in
        the same-sign case only.
    - `zero_side` ∈ `above|below` — `below` also covers the exact-zero edge
    - Implementation detail: SMA-seeded EMA (standard across most retail
      platforms). May differ in the initial ~3×period bars from Wilder-
      seeded implementations; values converge thereafter.
  - `bollinger`: `{upper, middle, lower, width_pct, pct_b, squeeze, position}`
    - `squeeze=true` means **current bandwidth < 75% of prior-period bandwidth**
      (relative compression). This is separate from absolute-bandwidth buckets
      (`tight`/`normal`/`wide`) — a band can be `squeeze=true` while `width_pct`
      is still in the `normal` range.
    - `position` ∈ `above_upper|upper_half|middle|lower_half|below_lower`
      (`middle` appears when `std==0`, i.e., all window values are identical)
  - `atr`: `{atr_14, atr_pct, stop_1x, stop_1_5x, stop_2x}`
  - `rsi`: `{rsi, avg_gain, avg_loss}`
  - `rsi_divergence`: `bullish_divergence|bearish_divergence|none|insufficient_data`
    - `insufficient_data` is a genuine **unknown** state — do NOT collapse to
      `none` (see Timing Assessment rules below).
  - `volume`: `{current_volume, volume_ma20, volume_ratio_vs_ma20, volume_ratio_5d_20d, obv_trend, price_volume_relationship}`
    - `obv_trend` ∈ `rising|falling|flat|insufficient_data`
    - `price_volume_relationship` ∈ `bullish_confirmation|bearish_divergence|distribution|low_conviction_decline|neutral|insufficient_data`

Use pre-computed values directly. Do not re-derive indicators from raw prices.
When any indicator returns `insufficient_data`, flag reduced confidence in
the output rather than inferring a default directional label.

## Analysis Dimensions

### 1. Trend

Compare price vs MA20/MA50 (from `historical.sma_20/sma_50`) and approximate
MA200 from weekly data (~40 bars). Check MA20 vs MA50 ordering.

Why MA alignment matters: when price > MA20 > MA50 > MA200, every timeframe
participant is in profit — no trapped holders selling into rallies. Fully bearish
alignment means every recent buyer is underwater and rallies face overhead supply.

Classify direction (`uptrend|downtrend|sideways`), strength (`strong|moderate|weak`),
and pattern (`bullish_aligned`: price>MA20>MA50 | `bearish_aligned`: price<MA20<MA50 | `mixed`).

### 2. Momentum

**MACD**: Histogram direction matters more than absolute value. `expanding` =
accelerating momentum; `contracting` = fading even if price still moves;
`reversal` = histogram just crossed zero (concurrent with `golden`/`death`
crossover). Do NOT read `reversal` as momentum fade — it is an inflection.

**RSI**: Zones: `oversold` (<30), `neutral` (30-70), `overbought` (>70). Context
matters — RSI 25 in an uptrend is a pullback opportunity; in a downtrend it confirms weakness.

**RSI Divergence**: A **supporting confirmation signal**, not a standalone
trigger. The detector confirms pivot structure (swing points with left/right
confirmation bars) and enforces magnitude thresholds — still a heuristic,
not a pattern-recognition model. Bullish divergence (lower price low, higher
RSI low) = selling possibly exhausting. Bearish divergence (higher price
high, lower RSI high) = buying possibly fading despite new highs. Use only
in conjunction with trend/structure/volume confirmation; never let a lone
divergence flip the dominant directional assessment.

### 3. Volatility

**ATR**: `atr_pct` normalizes across price levels — a $200 stock with 3% ATR and a
$20 stock with 3% ATR have the same volatility profile.

**Bollinger**: `squeeze=true` means current bandwidth is compressed vs the
prior-period bandwidth (**relative** compression). Classify absolute
bandwidth separately: `tight` (<5%), `normal` (5-15%), `wide` (>15%). A
wide band can be in `squeeze=true` (coming off a wider one), and a narrow
band can be `squeeze=false` (stable narrow regime). Report both.

### 4. Structure

Identify support/resistance from ONLY data-derived sources:
- MA levels (MA20, MA50, MA200) as dynamic support/resistance
- 52-week range from `snapshot.week_52_high/week_52_low`
- Prior swing highs/lows (local extrema held 3+ bars each side)
- ATR stop levels from `atr.stop_1x/stop_1_5x`

Do NOT fabricate round numbers. Every level needs a data source. Rate each as
`strong` (confluent or tested 2+ times), `moderate` (single clear source), or
`weak` (approximate or short timeframe). Calculate distance from current price
to nearest support and resistance as percentages.

### 5. Volume

Volume is the lie detector of price action. A 5% rise on 3x volume has broad
institutional participation; the same rise on 0.5x volume may reverse on the
first real selling pressure.

Read from `indicators.json`:
- `volume_ratio_vs_ma20`: >1.5 = elevated, <0.7 = thin
- `volume_ratio_5d_20d`: detects emerging volume trends
- `obv_trend`: `rising` (accumulation) | `falling` (distribution) | `flat` |
  `insufficient_data` (treat as unknown — do not infer direction)
- `price_volume_relationship` (last 5 sessions vs prior 20-day baseline):
  - `bullish_confirmation`: 5d price up + 5d volume >110% of 20d baseline (healthy)
  - `bearish_divergence`: 5d price up + 5d volume <90% of baseline (rally on fumes)
  - `distribution`: 5d price down + 5d volume expanding (institutional selling)
  - `low_conviction_decline`: 5d price down + 5d volume contracting (orderly pullback)
  - `neutral`: price + volume both within baseline bands (no clear read)
  - `insufficient_data`: not enough paired bars for a reliable read (fewer
    than 25 valid paired bars, mismatched input lengths, or an unusable
    prior-volume baseline) — do not infer a direction

Always state explicitly whether volume confirms or contradicts the price action.
When the label is `neutral` or `insufficient_data`, say so — do not fabricate
a directional read.

## Timing Assessment

Synthesize all five dimensions into an entry favorability judgment (not a formula).

**Entry favorability levels:**
- `strong_buy_zone`: Uptrend + pullback to support + RSI recovering from oversold + expanding volume
- `favorable`: Trend up, indicators healthy, no red flags, not at ideal pullback
- `neutral`: Sideways, MAs tangling, mixed signals — wait for clarity
- `unfavorable`: Trend weakening, breaking below MAs, burden of proof on bulls
- `strong_avoid`: Downtrend + breakdown + high-volume selling. A single
  bullish RSI divergence does NOT soften `strong_avoid` to `unfavorable`
  by itself — divergence is a confirmation signal, and it only changes
  the entry label when corroborated by structure (e.g., price reclaiming
  a broken MA) and volume (e.g., shift to accumulation). Treat
  `rsi_divergence == insufficient_data` as unknown, NOT equivalent to `none`.

**Technical levels** (observations, not trade recommendations):
- `entry_zone`: price range + basis (e.g., "MA20-MA50 support band")
- `stop_reference`: price + basis (e.g., "MA50 - 1xATR")
- `breakout_level`: price + basis (e.g., "52-week high")

**Caution flags** — list regardless of favorability: Bollinger squeeze, bearish RSI
divergence at highs, declining volume during trend, price extended far from MAs,
approaching earnings.

## Output Format

Write `technical.json`:

```json
{
  "ticker": "AAPL",
  "trend": {
    "direction": "uptrend", "strength": "moderate",
    "ma_alignment": {
      "price_vs_ma20": "above", "price_vs_ma50": "above",
      "price_vs_ma200": "above", "ma20_vs_ma50": "above",
      "pattern": "bullish_aligned"
    },
    "source": "[Calc: price_data + historical.sma_20/sma_50]"
  },
  "momentum": {
    "macd": {"value": 1.23, "signal": 0.98, "histogram": 0.25,
      "crossover": "none", "hist_trend": "expanding", "zero_side": "above",
      "interpretation": "Momentum accelerating above zero line"},
    "rsi": {"value": 58.3, "zone": "neutral", "divergence": "none"},
    "source": "[Calc: indicators.json]"
  },
  "volatility": {
    "atr_14": 3.45, "atr_pct": 2.1,
    "bollinger": {"position": "upper_half", "pct_b": 0.72, "width_pct": 8.5,
      "squeeze": false, "bandwidth": "normal",
      "interpretation": "Upper half of bands, normal bandwidth, no squeeze"},
    "source": "[Calc: indicators.json]"
  },
  "structure": {
    "support_levels": [
      {"price": 162.50, "type": "ma50", "strength": "strong"},
      {"price": 155.00, "type": "prior_low", "strength": "moderate"}],
    "resistance_levels": [
      {"price": 178.50, "type": "week_52_high", "strength": "strong"}],
    "distance_to_support_pct": -3.2,
    "distance_to_resistance_pct": 5.8,
    "source": "[Calc: price_data + indicators.json]"
  },
  "volume": {
    "recent_vs_avg": 1.15, "volume_ratio_5d_20d": 1.08,
    "obv_trend": "rising",
    "price_volume_relationship": "bullish_confirmation",
    "trend_confirmation": true,
    "interpretation": "Volume above average, OBV rising — uptrend has participation",
    "source": "[Calc: indicators.json]"
  },
  "timing_assessment": {
    "entry_favorability": "favorable",
    "reasoning": "Bullish MA alignment, healthy momentum, volume confirms.",
    "caution_flags": [],
    "technical_levels": {
      "entry_zone": {"low": 162.50, "high": 165.00, "basis": "MA50 support zone"},
      "stop_reference": {"price": 159.05, "basis": "MA50 minus 1x ATR"},
      "breakout_level": {"price": 178.50, "basis": "52-week high"}
    }
  },
  "confidence": "high"
}
```

**Confidence**: `high` (all indicators, signals agree) | `medium` (some mixed signals
or minor gaps) | `low` (significant data gaps or strongly contradictory signals).

## Critical Rules

Source tagging enforced by `.claude/rules/anti-hallucination.md`. In addition:

- Use pre-computed `indicators.json` values — do not re-derive MACD, RSI, etc.
- Every support/resistance level must cite its data source
- Volume interpretation is mandatory — always state confirm or contradict
- Do not compress into a single timing_strength score; present the full picture
- Do not make buy/sell recommendations; present observations for downstream agents
- Handle missing data gracefully: if `sma_50` is absent, note it and adjust accordingly
