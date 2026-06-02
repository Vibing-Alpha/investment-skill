"""Compute sector ETF multi-window trend signal.

`scripts.macro` emits only scalar current prices (used by /portfolio).
The research-industry skill needs explicit 5d/20d/60d return windows to
classify the sector regime (tailwind / neutral / headwind), so this
producer fills that gap.

Usage:
    python3 -m scripts.sector_signal --etf SOXX --output PATH
    python3 -m scripts.sector_signal --etf SOXX  # stdout

Output JSON shape:
    {
      "etf_symbol": "SOXX",
      "etf_name": "iShares Semiconductor ETF",  // may be null
      "current_close": 537.33,
      "trend_5d_pct": 5.67,
      "trend_20d_pct": 16.41,
      "trend_60d_pct": 50.69,
      "history_count": 124,
      "as_of_index_offset": 0  // 0 = today's bar is last; 1 = yesterday's was last
    }

Any window with insufficient history (e.g. ETF younger than 60 trading
days) emits `null` for that field rather than guessing. The downstream
regime-classification logic in the skill prompt knows to treat null
windows as "insufficient data" rather than as a zero/neutral signal.

Determinism: yfinance is the network dependency. CI tests must mock
`yfinance.Ticker` — see tests/test_sector_signal.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.cli_utils import write_output


_DEFAULT_PERIOD = "6mo"  # yfinance period string — covers 60d window with margin

_WINDOWS = (
    ("trend_5d_pct", 5),
    ("trend_20d_pct", 20),
    ("trend_60d_pct", 60),
)


def _pct(now: float, then: float) -> float | None:
    """Percentage change from `then` to `now`. Returns None for non-finite or zero baseline."""
    import math
    if not math.isfinite(now) or not math.isfinite(then) or then == 0:
        return None
    return round((now - then) / then * 100, 2)


def compute_sector_signal(etf_symbol: str, *, period: str = _DEFAULT_PERIOD,
                          ticker_factory=None) -> dict[str, Any]:
    """Compute trend windows for one ETF symbol.

    `ticker_factory` is a callable taking `etf_symbol` and returning an
    object with a `.history(period=...)` method returning a pandas-like
    DataFrame with a "Close" column. Tests inject a fake factory.

    Production (ticker_factory=None) goes through `yfinance_guard.yfinance_call`
    for retry + rate-limit handling per DL1 spec.
    """
    if ticker_factory is None:
        from scripts.sources.yfinance_guard import (
            yfinance_call,
            validate_yfinance_ticker,
        )
        import yfinance as _yf
        sym = validate_yfinance_ticker(etf_symbol)
        # Wrap in yfinance_call for retry + rate-limit handling per DL1.
        # audit Q skips this line because `yfinance_call(` is on the same line.
        hist = yfinance_call(lambda: _yf.Ticker(sym).history(period=period))
    else:
        t = ticker_factory(etf_symbol)
        hist = t.history(period=period)

    if hist is None or hasattr(hist, "empty") and hist.empty:
        raise RuntimeError(f"sector_signal: empty history for {etf_symbol!r}")

    closes = list(hist["Close"].values)
    if not closes:
        raise RuntimeError(f"sector_signal: no Close values for {etf_symbol!r}")

    latest = float(closes[-1])
    out: dict[str, Any] = {
        "etf_symbol": etf_symbol,
        "etf_name": _resolve_etf_name(etf_symbol),
        "current_close": round(latest, 2),
        "history_count": len(closes),
        "as_of_index_offset": 0,
    }
    for field, window in _WINDOWS:
        # Window N means "N trading days ago", i.e. closes[-1-N]. If the
        # history is shorter than that, emit null rather than guess.
        if len(closes) >= window + 1:
            out[field] = _pct(latest, float(closes[-1 - window]))
        else:
            out[field] = None
    return out


# Short curated map of common sector ETF display names. Not exhaustive —
# the field is informational, and a null value is fine for less common ETFs.
_ETF_NAMES = {
    "SOXX": "iShares Semiconductor ETF",
    "XLK":  "Technology Select Sector SPDR Fund",
    "XLF":  "Financial Select Sector SPDR Fund",
    "XLE":  "Energy Select Sector SPDR Fund",
    "XLV":  "Health Care Select Sector SPDR Fund",
    "XLY":  "Consumer Discretionary Select Sector SPDR Fund",
    "XLP":  "Consumer Staples Select Sector SPDR Fund",
    "XLI":  "Industrial Select Sector SPDR Fund",
    "XLU":  "Utilities Select Sector SPDR Fund",
    "XLC":  "Communication Services Select Sector SPDR Fund",
    "XLRE": "Real Estate Select Sector SPDR Fund",
    "HACK": "Amplify Cybersecurity ETF",
    "DRIV": "Global X Autonomous & Electric Vehicles ETF",
    "ICLN": "iShares Global Clean Energy ETF",
    "SPY":  "SPDR S&P 500 ETF Trust",
    "QQQ":  "Invesco QQQ Trust",
}


def _resolve_etf_name(etf_symbol: str) -> str | None:
    return _ETF_NAMES.get(etf_symbol.upper())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf", required=True, help="ETF ticker (e.g. SOXX, XLK)")
    p.add_argument("--output", default=None,
                   help="Output JSON path. If omitted, prints to stdout.")
    p.add_argument("--period", default=_DEFAULT_PERIOD,
                   help=f"yfinance history period (default: {_DEFAULT_PERIOD})")
    args = p.parse_args(argv)

    try:
        signal = compute_sector_signal(args.etf, period=args.period)
    except RuntimeError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # network / yfinance failures
        print(f"FATAL: sector_signal failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.output:
        write_output(signal, args.output)
        print(args.output)
    else:
        print(json.dumps(signal, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
