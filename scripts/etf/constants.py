"""Frozen constants for the ETF path.

`REQUIRED_SNAPSHOT_FIELDS` is the fully enumerated set of run-day evidence the
ETF prompt may cite and the readiness predicate validates: the 29 fixed
decision-relevant leaves `scripts.macro._compute_ticker_indicators` emits, plus
the four rates fields.

Enumerated rather than pattern-matched on purpose. A predicate over "whatever
the indicator block happens to contain" cannot fail when a producer stops
emitting a leaf, and cannot stop a prompt citing a field nothing produces.
Three optional leaves are deliberately EXCLUDED — `volume.price_volume_relationship`,
`volume_note`, and the `lookback_complete`/`inception_proven` structure flags —
because they degrade independently on real funds: one live, mature, otherwise
fully-computed fund was refused solely because 88 of its last 126 sessions had
zero volume, so the 20-session prior average was 0 and the relationship never
computed.

Copied verbatim from the design contract's `F.SNAPSHOT.REQUIRED_INDICATORS`
members; every member was execute-verified to resolve against a real indicator
block before this file was written.
"""

from __future__ import annotations

REQUIRED_SNAPSHOT_FIELDS = (
    "ticker_indicators[T].macd.macd_line",
    "ticker_indicators[T].macd.signal_line",
    "ticker_indicators[T].macd.histogram",
    "ticker_indicators[T].macd.crossover",
    "ticker_indicators[T].macd.hist_trend",
    "ticker_indicators[T].macd.zero_side",
    "ticker_indicators[T].bollinger.upper",
    "ticker_indicators[T].bollinger.middle",
    "ticker_indicators[T].bollinger.lower",
    "ticker_indicators[T].bollinger.width_pct",
    "ticker_indicators[T].bollinger.pct_b",
    "ticker_indicators[T].bollinger.squeeze",
    "ticker_indicators[T].bollinger.position",
    "ticker_indicators[T].atr.atr_14",
    "ticker_indicators[T].atr.atr_pct",
    "ticker_indicators[T].atr.stop_1x",
    "ticker_indicators[T].atr.stop_1_5x",
    "ticker_indicators[T].atr.stop_2x",
    "ticker_indicators[T].rsi.rsi",
    "ticker_indicators[T].rsi.avg_gain",
    "ticker_indicators[T].rsi.avg_loss",
    "ticker_indicators[T].rsi_divergence",
    "ticker_indicators[T].volume.current_volume",
    "ticker_indicators[T].volume.volume_ma20",
    "ticker_indicators[T].volume.volume_ratio_vs_ma20",
    "ticker_indicators[T].volume.volume_ratio_5d_20d",
    "ticker_indicators[T].volume.obv_trend",
    "ticker_indicators[T].rs_vs_spy_3m",
    "ticker_indicators[T].rs_vs_qqq_3m",
    "rates.fed_funds",
    "rates.us_10y",
    "rates.us_5y",
    "rates.spread_10y_5y",
)
