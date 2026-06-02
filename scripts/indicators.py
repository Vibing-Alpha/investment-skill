"""Technical indicator calculations for stock analysis.

Pure functions: same input -> same output, no external I/O.
Uses only stdlib (math). Arrays are oldest-first throughout.

Indicators provided:
  - calc_macd(closes, fast=12, slow=26, signal=9)
        MACD line, signal line, histogram, crossover detection, histogram trend.
  - calc_bollinger(closes, current_price=None, period=20, num_std=2.0)
        Upper/middle/lower bands, bandwidth %, %B, squeeze detection, position.
  - calc_atr(highs, lows, closes, current_price=None, period=14)
        ATR(14), ATR as % of price, stop-loss levels at 1x/1.5x/2x ATR.
  - calc_rsi(closes, period=14)
        RSI with Wilder's smoothing, avg gain/loss.
  - calc_rsi_series(closes, period=14)
        Full RSI history aligned tail-to-tail with closes.
  - detect_rsi_divergence(closes, rsi_values, lookback=60, ...)
        Bullish/bearish price-RSI divergence via pivot structure +
        magnitude thresholds. Alignment contract enforced.
  - calc_volume(volumes, closes)
        Volume MA(20), 5d/20d ratio, OBV trend, price-volume relationship.

Guards:
  - NaN/Inf rejection via _sanitize_closes and _fin helpers
  - Division-by-zero protection on all denominators
  - Array length validation before every calculation
  - Bool rejection for numeric parameters
"""
import math
from typing import Dict, List, Optional


def _sanitize_closes(closes: List) -> List[float]:
    """Remove non-numeric/None/NaN/Inf values from close prices."""
    return [
        c for c in closes
        if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
    ]


def _ema(values: List[float], period: int) -> List[float]:
    """Exponential Moving Average. Returns list starting from index period-1.

    Uses SMA-seed initialization (standard in most trading platforms).
    Values converge after ~3x period.
    """
    if period < 1 or len(values) < period:
        return []
    k = 2.0 / (period + 1)
    ema_values = [sum(values[:period]) / period]  # SMA seed
    for v in values[period:]:
        ema_values.append(v * k + ema_values[-1] * (1 - k))
    return ema_values


def _sma(values: List[float], period: int) -> Optional[float]:
    """Simple Moving Average of last `period` values."""
    if period < 1 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def calc_macd(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict:
    """MACD(12,26,9) calculation.

    Returns: {macd_line, signal_line, histogram, crossover, hist_trend, zero_side}
    """
    # Validate parameters: fast < slow, all positive
    if not (0 < fast < slow) or signal < 1:
        return {
            'macd_line': None, 'signal_line': None, 'histogram': None,
            'crossover': 'none', 'hist_trend': 'flat', 'zero_side': 'below',
        }
    closes = _sanitize_closes(closes)
    # Minimum: slow + signal - 1 (SMA-seeded EMA needs slow points, then signal-1 more for signal EMA)
    if len(closes) < slow + signal - 1:
        return {
            'macd_line': None, 'signal_line': None, 'histogram': None,
            'crossover': 'none', 'hist_trend': 'flat', 'zero_side': 'below',
        }

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)

    # Align: ema_fast starts at index fast-1, ema_slow at index slow-1
    # MACD line = EMA_fast - EMA_slow (aligned from slow onwards)
    offset = slow - fast
    macd_line_series = [
        ema_fast[offset + i] - ema_slow[i]
        for i in range(len(ema_slow))
    ]

    if len(macd_line_series) < signal:
        return {
            'macd_line': None, 'signal_line': None, 'histogram': None,
            'crossover': 'none', 'hist_trend': 'flat', 'zero_side': 'below',
        }

    signal_series = _ema(macd_line_series, signal)
    # Align histogram
    hist_offset = len(macd_line_series) - len(signal_series)
    histogram_series = [
        macd_line_series[hist_offset + i] - signal_series[i]
        for i in range(len(signal_series))
    ]

    macd_val = macd_line_series[-1]
    signal_val = signal_series[-1]
    hist_val = histogram_series[-1]
    hist_prev = histogram_series[-2] if len(histogram_series) >= 2 else 0.0

    # Crossover detection
    crossover = 'none'
    if len(histogram_series) >= 2:
        if histogram_series[-2] <= 0 < histogram_series[-1]:
            crossover = 'golden'
        elif histogram_series[-2] >= 0 > histogram_series[-1]:
            crossover = 'death'

    # Histogram trend.
    #   Sign change (zero-crossing) → 'reversal' — even if |hist| shrank,
    #     this is an inflection, not a fade. Prior implementation labeled
    #     cross-zero bars as 'contracting' via |abs| comparison, which
    #     contradicted the concurrent 'golden'/'death' crossover signal.
    #   Same sign → 'expanding' / 'contracting' by |abs| comparison.
    hist_trend = 'flat'
    if len(histogram_series) >= 2:
        if (hist_val > 0 > hist_prev) or (hist_val < 0 < hist_prev):
            hist_trend = 'reversal'
        elif abs(hist_val) > abs(hist_prev):
            hist_trend = 'expanding'
        elif abs(hist_val) < abs(hist_prev):
            hist_trend = 'contracting'

    # Only 'above'/'below' -- no 'neutral' (consumers don't handle it)
    zero_side = 'above' if macd_val > 0 else 'below'

    return {
        'macd_line': round(macd_val, 4),
        'signal_line': round(signal_val, 4),
        'histogram': round(hist_val, 4),
        'crossover': crossover,
        'hist_trend': hist_trend,
        'zero_side': zero_side,
    }


def calc_bollinger(
    closes: List[float],
    current_price: Optional[float] = None,
    period: int = 20,
    num_std: float = 2.0,
) -> Dict:
    """Bollinger Bands(20,2sigma) calculation.

    Args:
        closes: Price array, oldest-first.
        current_price: Real-time price (may differ from closes[-1] intraday).
                       Defaults to closes[-1] if None.

    Returns: {upper, middle, lower, width_pct, pct_b, squeeze, position}

    Uses sample std dev (n-1).
    Squeeze uses dynamic comparison: current bandwidth < 75% of prior-period
    bandwidth. Requires len(closes) >= 2 * period for prior-period reference.
    If insufficient history, squeeze=False.
    """
    if num_std <= 0:
        return {
            'upper': None, 'middle': None, 'lower': None,
            'width_pct': None, 'pct_b': None, 'squeeze': False,
            'position': 'lower_half',
        }
    closes = _sanitize_closes(closes)
    if period < 2:
        return {
            'upper': None, 'middle': None, 'lower': None,
            'width_pct': None, 'pct_b': None, 'squeeze': False,
            'position': 'lower_half',
        }
    if len(closes) < period:
        return {
            'upper': None, 'middle': None, 'lower': None,
            'width_pct': None, 'pct_b': None, 'squeeze': False,
            'position': 'lower_half',
        }

    # Safe coercion -- reject bool, coerce string, reject non-finite
    _cp = current_price
    if isinstance(_cp, bool):
        _cp = None
    elif not isinstance(_cp, (int, float)):
        try:
            _cp = float(_cp) if _cp is not None else None
        except (TypeError, ValueError):
            _cp = None
    price = (
        _cp
        if _cp is not None and math.isfinite(_cp) and _cp > 0
        else closes[-1]
    )
    window = closes[-period:]
    middle = sum(window) / period
    # Sample std dev (n-1)
    variance = sum((x - middle) ** 2 for x in window) / (period - 1)
    std = variance ** 0.5

    if std == 0:
        # All values identical -> bands collapse onto middle.
        # Prior implementation set upper/lower to None and position to
        # 'lower_half' — consumers then mis-read a flat/collapsed band
        # as an indicator gap and/or a below-band condition. Report the
        # collapsed bands explicitly and neutral position.
        middle_r = round(middle, 2)
        return {
            'upper': middle_r, 'middle': middle_r, 'lower': middle_r,
            'width_pct': 0.0, 'pct_b': 0.5, 'squeeze': True,
            'position': 'middle',
        }

    upper = middle + num_std * std
    lower = middle - num_std * std

    # Guard division by zero
    width_pct = ((upper - lower) / middle * 100) if middle != 0 else 0.0
    band_range = upper - lower
    pct_b = ((price - lower) / band_range) if band_range != 0 else 0.5

    # Dynamic squeeze: current bandwidth < 75% of prior-period bandwidth
    # Requires 2 * period data points for a valid prior-period reference
    squeeze = False
    if len(closes) >= 2 * period:
        prev_window = closes[-(2 * period):-period]
        prev_middle = sum(prev_window) / period
        prev_variance = sum((x - prev_middle) ** 2 for x in prev_window) / (period - 1)
        prev_std = prev_variance ** 0.5
        if prev_middle != 0 and prev_std != 0:
            prev_upper = prev_middle + num_std * prev_std
            prev_lower = prev_middle - num_std * prev_std
            prev_width = (prev_upper - prev_lower) / prev_middle * 100
            if prev_width > 0:
                squeeze = width_pct < prev_width * 0.75

    # Position classification (uses current_price, not closes[-1])
    if price > upper:
        position = 'above_upper'
    elif price < lower:
        position = 'below_lower'
    elif pct_b >= 0.5:
        position = 'upper_half'
    else:
        position = 'lower_half'

    return {
        'upper': round(upper, 2),
        'middle': round(middle, 2),
        'lower': round(lower, 2),
        'width_pct': round(width_pct, 2),
        'pct_b': round(pct_b, 4),
        'squeeze': squeeze,
        'position': position,
    }


def calc_atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    current_price: Optional[float] = None,
    period: int = 14,
) -> Dict:
    """ATR(14) with Wilder smoothing and gap-aware NaN handling.

    Key correctness property: when `closes[i-1]` is invalid (halt day /
    missing data), the True Range for bar `i` MUST still use the most
    recent valid close — not skip the bar. Skipping drops the TR that
    captures the reopening gap, systematically underestimating volatility
    by 30-40% immediately after any halt and ~10% for days afterward.
    That ATR underestimate propagates to stop_1x/1.5x/2x, placing stops
    too tight and inviting premature exit.

    Args:
        highs/lows/closes: OHLC arrays, oldest-first. Rows where high OR
                           low is invalid are dropped. Rows where only
                           close is invalid are retained; the close falls
                           back to the most recent valid close for the
                           next bar's TR calculation.
        current_price: Real-time price for stop level (may differ from
                       closes[-1] intraday). Defaults to last valid close.

    Returns: {atr_14, atr_pct, stop_1x, stop_1_5x, stop_2x}
    """
    def _none_result():
        return {
            'atr_14': None, 'atr_pct': None,
            'stop_1x': None, 'stop_1_5x': None, 'stop_2x': None,
        }

    def _fin(v):
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v))

    n = min(len(highs), len(lows), len(closes))
    if period < 1 or n < period:
        return _none_result()

    # Single pass: update prev_valid_close from ANY valid close (even on
    # a bar dropped because high/low is invalid), but only emit a TR for
    # bars where high AND low are valid. The previous two-pass design
    # filtered bars first and therefore lost close updates on high/low-
    # invalid bars — making TR on later bars reference a stale close and
    # systematically inflating the ATR (~55% in measured repro).
    true_ranges: List[float] = []
    prev_valid_close: Optional[float] = None
    for i in range(n):
        h, l, c = highs[i], lows[i], closes[i]
        if _fin(h) and _fin(l):
            if prev_valid_close is None:
                tr = abs(h - l)
            else:
                tr = max(
                    abs(h - l),
                    abs(h - prev_valid_close),
                    abs(l - prev_valid_close),
                )
            true_ranges.append(tr)
        # ALWAYS update the close-state machine regardless of high/low
        # validity — a valid close on a no-TR bar is still the correct
        # gap reference for the next TR-eligible bar.
        if _fin(c):
            prev_valid_close = c

    if len(true_ranges) < period:
        return _none_result()

    # Initial ATR = SMA of first `period` true ranges; then Wilder smoothing.
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    # Anchor price for pct + stops: prefer current_price (live), else last valid close.
    price = current_price if _fin(current_price) else None
    if price is None:
        price = next((c for c in reversed(closes) if _fin(c)), None)
    if price is None or price <= 0:
        return {
            'atr_14': round(atr, 4), 'atr_pct': None,
            'stop_1x': None, 'stop_1_5x': None, 'stop_2x': None,
        }
    atr_pct = atr / price * 100

    return {
        'atr_14': round(atr, 4),
        'atr_pct': round(atr_pct, 2),
        'stop_1x': round(price - atr, 2),
        'stop_1_5x': round(price - 1.5 * atr, 2),
        'stop_2x': round(price - 2.0 * atr, 2),
    }


def calc_rsi(
    closes: List[float],
    period: int = 14,
) -> Dict:
    """RSI(14) calculation with Wilder's smoothing.

    Returns: {rsi, avg_gain, avg_loss}
    """
    if period < 1:
        return {'rsi': None, 'avg_gain': None, 'avg_loss': None}
    closes = _sanitize_closes(closes)
    if len(closes) < period + 1:
        return {'rsi': None, 'avg_gain': None, 'avg_loss': None}

    # Calculate price changes (oldest-first)
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Initial averages (first `period` changes)
    gains = [max(c, 0) for c in changes[:period]]
    losses = [abs(min(c, 0)) for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Smoothed averages (Wilder's method)
    for c in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(c, 0)) / period
        avg_loss = (avg_loss * (period - 1) + abs(min(c, 0))) / period

    # Guard division by zero
    if avg_loss == 0:
        rsi = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    return {
        'rsi': round(rsi, 2),
        'avg_gain': round(avg_gain, 6),
        'avg_loss': round(avg_loss, 6),
    }


def calc_rsi_series(
    closes: List[float],
    period: int = 14,
) -> List[float]:
    """Full RSI history using Wilder's smoothed average.

    Returns one RSI value per close starting from index `period`.
    Output is oldest-first, aligned so rsi_series[-k] corresponds to closes[-k].
    Empty list if insufficient data (< period + 1 closes).

    Uses the SAME Wilder smoothing as calc_rsi -- the last element of the
    returned series will match calc_rsi(closes, period)['rsi'].
    """
    if period < 1:
        return []
    closes = _sanitize_closes(closes)
    if len(closes) < period + 1:
        return []

    # Calculate price changes (oldest-first)
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Initial averages from first `period` changes
    gains = [max(c, 0) for c in changes[:period]]
    losses = [abs(min(c, 0)) for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # First RSI value (at index `period` in closes)
    def _rsi_from_avgs(ag: float, al: float) -> float:
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        rs = ag / al
        return 100 - (100 / (1 + rs))

    series = [round(_rsi_from_avgs(avg_gain, avg_loss), 2)]

    # Smoothed averages for remaining changes (Wilder's method)
    for c in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(c, 0)) / period
        avg_loss = (avg_loss * (period - 1) + abs(min(c, 0))) / period
        series.append(round(_rsi_from_avgs(avg_gain, avg_loss), 2))

    return series


def detect_rsi_divergence(
    closes: List[float],
    rsi_values: List[float],
    lookback: int = 60,
    pivot_left: int = 2,
    pivot_right: int = 2,
    min_separation: int = 5,
    min_price_move_frac: float = 0.015,
    min_rsi_delta: float = 4.0,
) -> str:
    """Detect price-RSI divergence using a pivot-structure heuristic.

    Replaces the earlier half-window-min/max heuristic, which had:
    - ~54% trigger rate on random walks (measured in Phase 2 Monte Carlo) —
      effectively "high-sensitivity, low-specificity" and not fit to carry
      a "most powerful signal" label
    - Single-bar spike contamination (one noisy close could dictate a half
      window's extremum)
    - Fixed directional bias on conflicting windows

    This pivot-based version requires:
    1. A confirmed swing point: a price low/high with `pivot_left` bars
       strictly above/below on the left AND `pivot_right` bars on the right.
    2. Two pivots of the same type with at least `min_separation` bars
       between them.
    3. Minimum magnitude thresholds on BOTH the price and the RSI delta to
       suppress micro-wiggle false positives.
    4. Mutual exclusion: bullish and bearish conditions can both be
       searched but a conflict resolves to 'none'.

    Alignment contract: `rsi_values[-k]` MUST correspond to `closes[-k]`.
    `calc_rsi_series` satisfies this upstream. Any NaN in the lookback
    window is fail-closed — independent sanitizing would drift the
    positional anchor silently.

    Args:
        closes: Price series (oldest-first). Finite values required in lookback.
        rsi_values: RSI series tail-aligned with closes.
        lookback: Bars considered for pivot search (recent tail).
        pivot_left / pivot_right: Bars required strictly above (low) or
            below (high) the candidate on each side for confirmation.
        min_separation: Minimum bar gap between the two pivots compared.
        min_price_move_frac: Minimum fractional price move between the two
            pivots, expressed as a decimal fraction (e.g. 0.015 = 1.5%).
            Renamed from `_pct` per rules/units.md: `_pct` is reserved for
            raw-percent integer form (e.g. 35 for 35%), `_frac` /
            `_decimal` for fractional form.
        min_rsi_delta: Minimum RSI point delta in the opposing direction.

    Returns: 'bullish_divergence' | 'bearish_divergence' | 'none' | 'insufficient_data'
    """
    # Parameter validation: fail-closed on invalid knobs rather than
    # crashing inside pivot-detection with an IndexError.
    if pivot_left < 0 or pivot_right < 0 or min_separation < 1:
        return 'insufficient_data'
    if lookback < pivot_left + pivot_right + 3:
        return 'insufficient_data'
    if len(closes) < lookback or len(rsi_values) < lookback:
        return 'insufficient_data'

    price = closes[-lookback:]
    rsi = rsi_values[-lookback:]

    def _fin(v):
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v))

    if not all(_fin(p) and _fin(r) for p, r in zip(price, rsi)):
        return 'insufficient_data'

    def _pivot_indices(arr: List[float], left: int, right: int, is_low: bool) -> List[int]:
        """Strict pivot detection: neighbors must be strictly ABOVE (for a
        low) or strictly BELOW (for a high). Equal-value neighbors
        disqualify — flat plateaus are not pivots."""
        out: List[int] = []
        n = len(arr)
        for i in range(left, n - right):
            v = arr[i]
            confirmed = True
            for j in range(i - left, i + right + 1):
                if j == i:
                    continue
                if is_low and arr[j] <= v:
                    confirmed = False
                    break
                if (not is_low) and arr[j] >= v:
                    confirmed = False
                    break
            if confirmed:
                out.append(i)
        return out

    def _last_two_with_gap(idxs: List[int], min_sep: int):
        # Iterate from newest toward oldest; pair the newest pivot with
        # the nearest older pivot that respects min_sep.
        for b in range(len(idxs) - 1, 0, -1):
            i2 = idxs[b]
            for a in range(b - 1, -1, -1):
                i1 = idxs[a]
                if i2 - i1 >= min_sep:
                    return i1, i2
        return None

    low_idxs = _pivot_indices(price, pivot_left, pivot_right, is_low=True)
    high_idxs = _pivot_indices(price, pivot_left, pivot_right, is_low=False)

    is_bullish = False
    pair = _last_two_with_gap(low_idxs, min_separation)
    if pair is not None:
        i1, i2 = pair
        price_drop_pct = (price[i1] - price[i2]) / max(abs(price[i1]), 1e-9)
        rsi_rise = rsi[i2] - rsi[i1]
        if (price[i2] < price[i1]
                and price_drop_pct >= min_price_move_frac
                and rsi_rise >= min_rsi_delta):
            is_bullish = True

    is_bearish = False
    pair = _last_two_with_gap(high_idxs, min_separation)
    if pair is not None:
        i1, i2 = pair
        price_rise_pct = (price[i2] - price[i1]) / max(abs(price[i1]), 1e-9)
        rsi_drop = rsi[i1] - rsi[i2]
        if (price[i2] > price[i1]
                and price_rise_pct >= min_price_move_frac
                and rsi_drop >= min_rsi_delta):
            is_bearish = True

    if is_bullish and is_bearish:
        return 'none'  # ambiguous — never prefer one side by ordering
    if is_bullish:
        return 'bullish_divergence'
    if is_bearish:
        return 'bearish_divergence'
    return 'none'


def _valid_volume(v) -> bool:
    """Volume validity: numeric (non-bool), finite, non-negative."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0)


def _valid_close(c) -> bool:
    """Close validity: numeric (non-bool), finite."""
    return (isinstance(c, (int, float)) and not isinstance(c, bool)
            and math.isfinite(c))


def _empty_volume_result() -> Dict:
    return {
        'current_volume': None, 'volume_ma20': None,
        'volume_ratio_vs_ma20': None, 'volume_ratio_5d_20d': None,
        'obv_trend': 'insufficient_data',
        'price_volume_relationship': 'insufficient_data',
    }


def calc_volume(
    volumes: List[float],
    closes: List[float],
) -> Dict:
    """Volume analysis with paired-bar alignment.

    Keys: current_volume, volume_ma20, volume_ratio_vs_ma20,
    volume_ratio_5d_20d, obv_trend, price_volume_relationship.

    Input contract: volumes[i] and closes[i] describe the SAME bar.
    Mismatched lengths are fail-closed (no silent tail-align) because
    there is no timestamp to anchor the join. Within matched-length
    input, bars where EITHER volume or close is invalid are dropped as
    a pair so the OBV / price-volume relationship never compares
    misaligned dates.
    """
    # Length mismatch: no timestamps → cannot safely align.
    if len(volumes) != len(closes):
        return _empty_volume_result()

    # Pair-filter: keep bars where BOTH sides are valid.
    pairs = [
        (float(v), float(c))
        for v, c in zip(volumes, closes)
        if _valid_volume(v) and _valid_close(c)
    ]
    n = len(pairs)
    if n < 2:
        return _empty_volume_result()

    volumes = [p[0] for p in pairs]
    closes = [p[1] for p in pairs]

    current_vol = volumes[-1]
    vol_ma20 = _sma(volumes, 20)
    vol_ratio_vs_ma20 = (
        round(current_vol / vol_ma20, 2)
        if vol_ma20 and vol_ma20 > 0 else None
    )
    vol_ma5 = _sma(volumes, 5)
    vol_ratio_5d_20d = (
        round(vol_ma5 / vol_ma20, 2)
        if vol_ma5 and vol_ma20 and vol_ma20 > 0 else None
    )

    # OBV (cumulative, starting from 0 on the oldest bar we have).
    obv = [0.0]
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    obv_trend = _obv_trend(obv)

    # Price-volume relationship over the last 5 sessions.
    # Requires a full 20-day prior window as baseline — the degraded
    # "use whatever prior you have" fallback produced misleading labels
    # when the baseline and current window sizes differed materially.
    price_volume_rel = 'insufficient_data'
    if n >= 25:  # 20 prior + 5 recent
        price_change = closes[-1] - closes[-6]
        vol_recent_avg = sum(volumes[-5:]) / 5
        prior = volumes[:-5]
        vol_prior_avg = _sma(prior, 20)
        if vol_prior_avg and vol_prior_avg > 0:
            vol_expanding = vol_recent_avg > vol_prior_avg * 1.1
            vol_contracting = vol_recent_avg < vol_prior_avg * 0.9
            if price_change > 0 and vol_expanding:
                price_volume_rel = 'bullish_confirmation'
            elif price_change > 0 and vol_contracting:
                price_volume_rel = 'bearish_divergence'
            elif price_change < 0 and vol_expanding:
                price_volume_rel = 'distribution'
            elif price_change < 0 and vol_contracting:
                price_volume_rel = 'low_conviction_decline'
            else:
                price_volume_rel = 'neutral'

    return {
        'current_volume': int(round(current_vol)),
        'volume_ma20': int(round(vol_ma20)) if vol_ma20 is not None else None,
        'volume_ratio_vs_ma20': vol_ratio_vs_ma20,
        'volume_ratio_5d_20d': vol_ratio_5d_20d,
        'obv_trend': obv_trend,
        'price_volume_relationship': price_volume_rel,
    }


def _obv_trend(obv: List[float]) -> str:
    """Classify OBV trend via MA5 vs MA20 with scale-normalized threshold.

    Ratio-to-MA20 thresholds fail when MA20 is near zero or negative
    (OBV is cumulative and can cross zero). Normalize the MA5-MA20
    delta by the MA5/MA20 scale, not a multiplicative ±2% band.
    """
    if len(obv) < 20:
        return 'insufficient_data'
    obv_ma5 = sum(obv[-5:]) / 5
    obv_ma20 = sum(obv[-20:]) / 20
    scale = max(abs(obv_ma5), abs(obv_ma20), 1.0)
    delta = (obv_ma5 - obv_ma20) / scale
    if delta > 0.02:
        return 'rising'
    if delta < -0.02:
        return 'falling'
    return 'flat'


# Backwards-compat alias: prior _sanitize_volumes was documented.
# Keep a thin wrapper for any external caller that might exist.
def _sanitize_volumes(volumes: List) -> List[float]:
    """Deprecated: used only by tests that predate pair-filtering.
    Prefer calc_volume itself for any alignment-sensitive computation.
    """
    return [float(v) for v in volumes if _valid_volume(v)]


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _parse_args():
    """Parse CLI arguments for technical indicator calculations."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Calculate technical indicators (MACD/Bollinger/ATR/RSI/RSI-divergence) from price data JSON."
    )
    parser.add_argument(
        "--price-json", required=True,
        help="Path to price data JSON file (structure: {snapshot, historical: {daily: [...], weekly: [...]}})"
    )
    parser.add_argument(
        "--current-price", type=float, default=None,
        help="Override current price (defaults to snapshot.price)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Output file path (atomic write via temp+rename). Default: stdout"
    )
    return parser.parse_args()


def _main():
    """CLI main: read price JSON, compute all indicators, output combined JSON."""
    import json
    import sys

    args = _parse_args()

    # --- Read input ---
    from scripts.cli_utils import read_json, write_output
    data = read_json(args.price_json, "--price-json", "indicators")

    # --- Extract arrays from price data ---
    try:
        daily = data["historical"]["daily"]
    except (KeyError, TypeError) as exc:
        print(f"indicators: missing historical.daily in {args.price_json}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not daily:
        print(f"indicators: historical.daily is empty in {args.price_json}", file=sys.stderr)
        sys.exit(1)

    try:
        # Prefer split/dividend-adjusted close; fall back to raw close for
        # bars emitted by legacy producers that don't carry adjclose.
        # `adj if adj is not None else close` (not `adj or close`) so a
        # legitimate adjclose=0 bar doesn't silently fall through to raw.
        closes = [
            bar["adjclose"] if bar.get("adjclose") is not None else bar["close"]
            for bar in daily
        ]
        highs = [bar["high"] for bar in daily]
        lows = [bar["low"] for bar in daily]
        # Missing volume → None, not 0. `0` pollutes MA20/MA5/OBV as if it
        # were a real zero-volume day. None is dropped by calc_volume's
        # pair-filter, preserving the "MA20 of last 20 valid paired bars"
        # contract.
        volumes = [bar.get("volume") for bar in daily]
    except KeyError as exc:
        print(f"indicators: malformed daily bar in {args.price_json}: missing key {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Determine current price ---
    current_price = args.current_price
    if current_price is None:
        snapshot = data.get("snapshot")
        if snapshot and isinstance(snapshot.get("price"), (int, float)):
            current_price = snapshot["price"]

    # --- Bool rejection for numeric params ---
    if isinstance(current_price, bool):
        print("indicators: --current-price must be numeric, not bool", file=sys.stderr)
        sys.exit(1)

    # --- Compute all indicators ---
    result = {}
    result["macd"] = calc_macd(closes)
    result["bollinger"] = calc_bollinger(closes, current_price=current_price)
    result["atr"] = calc_atr(highs, lows, closes, current_price=current_price)
    result["rsi"] = calc_rsi(closes)

    # RSI divergence: pass the SAME sanitized close series that calc_rsi_series
    # used, so rsi_series[-k] corresponds to closes_clean[-k] (the positional
    # contract detect_rsi_divergence now enforces).
    closes_clean = _sanitize_closes(closes)
    rsi_series = calc_rsi_series(closes_clean)
    result["rsi_divergence"] = detect_rsi_divergence(closes_clean, rsi_series)

    # Volume indicators (pair-alignment contract enforced inside)
    result["volume"] = calc_volume(volumes, closes)

    # --- Output ---
    write_output(result, args.output)


if __name__ == "__main__":
    _main()
