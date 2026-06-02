"""Macro market indicators and portfolio ticker prices.

Fetches index prices (SPY, QQQ, ^DJI) with moving averages, VIX with
MA20, interest rates, treasury yield spread, and current prices for
portfolio tickers.  All fetches run in parallel via ThreadPoolExecutor.

CLI usage::

    python3 -m scripts.macro --tickers AAPL MSFT NVDA --output reports/macro.json
"""

import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from scripts.sources.yahoo_finance import fetch_yahoo_quote_result
from scripts.sources.adapter_result import ErrorCode

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MARKET_INDICES = ["SPY", "QQQ", "^DJI"]
VIX_TICKER = "^VIX"
TREASURY_10Y = "^TNX"
# ^FVX is the 5-Year Treasury yield (Yahoo Finance lacks a 2Y ticker).
# Historically this was emitted as ``us_2y`` with ``spread_10y_2y`` — a
# mislabel that corrupted the inversion signal. Now emitted canonically
# as ``us_5y`` / ``spread_10y_5y``; the legacy keys remain as a one-
# release deprecation shim (see ``_deprecated_keys`` in the output).
TREASURY_5Y = "^FVX"
# Back-compat alias so any external import keeps working for one release.
TREASURY_5Y_AS_2Y_PROXY = TREASURY_5Y


# Minimum usable closes for indicator block: calc_rsi_series starts RSI at
# index period (14), so the divergence lookback of 60 needs 14+60=74 closes.
_MIN_INDICATOR_BARS = 74


def _compute_ticker_indicators(ohlcv, current_price, bench_returns=None):
    """Run-day indicator block from raw OHLCV — reuses scripts.indicators (DRY).

    Returns the indicators.json-shaped block, or None when there are fewer
    than _MIN_INDICATOR_BARS usable closes (fail-closed: the agent gets no
    run-day read rather than a mostly-empty block). Short/absent high/low/
    volume arrays are not special-cased — the reused calc_* functions emit
    their own None / "insufficient_data" sentinels, which the consumer reads.
    """
    from scripts.indicators import (
        calc_macd, calc_bollinger, calc_atr, calc_rsi,
        calc_rsi_series, detect_rsi_divergence, calc_volume,
        _sanitize_closes,
    )
    raw_close = list(ohlcv.get("close", []))
    adj = list(ohlcv.get("adjclose", []))
    closes = []
    for i, c in enumerate(raw_close):
        a = adj[i] if i < len(adj) else None
        closes.append(a if a is not None else c)
    highs = list(ohlcv.get("high", []))
    lows = list(ohlcv.get("low", []))
    volumes = list(ohlcv.get("volume", []))

    # Gate on FINITE closes: _sanitize_closes also strips Inf/NaN, and the
    # divergence leg runs on this sanitized series — so "74 usable bars" must
    # mean 74 finite, else divergence silently degrades despite passing the gate.
    closes_clean = _sanitize_closes(closes)
    if len(closes_clean) < _MIN_INDICATOR_BARS:
        return None

    result = {
        "macd": calc_macd(closes),
        "bollinger": calc_bollinger(closes, current_price=current_price),
        "atr": calc_atr(highs, lows, closes, current_price=current_price),
        "rsi": calc_rsi(closes),
    }
    rsi_series = calc_rsi_series(closes_clean)
    result["rsi_divergence"] = detect_rsi_divergence(closes_clean, rsi_series)
    result["volume"] = calc_volume(volumes, closes)

    # Relative-strength facts (spec 2026-05-31-relative-strength-fact).
    # NEUTRAL data — no rotation/threshold logic here; the injected
    # strategy.yaml principle interprets it. When bench_returns is None
    # (non-portfolio callers / existing unit tests) the keys are NOT emitted
    # (additive). `closes` is the adj-or-raw series built above.
    if bench_returns is not None:
        ticker_3m_ret = _pct_return(closes, 63)
        for key, bench in (("rs_vs_spy_3m", "SPY"), ("rs_vs_qqq_3m", "QQQ")):
            bench_ret = bench_returns.get(bench)
            if ticker_3m_ret is None:
                # Defensive / gate-subsumed: a <64-bar ticker already returned
                # None above (74-bar gate). Kept so a lowered gate can't crash
                # (also: a zero prior-close data error makes _pct_return None → here).
                result[key] = "insufficient_data"
            elif bench_ret is None:
                result[key] = None  # that benchmark unavailable
            else:
                result[key] = round(ticker_3m_ret - bench_ret, 2)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sma_rounded(closes, period):
    """Simple moving average over the last *period* closes, rounded to 2dp."""
    if period < 1 or len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def _pct_return(closes, days: int = 63):
    """Percent return over `days` trading bars, computed on the finite-numeric
    close series only (None/NaN/Inf dropped; a legitimate 0.0 close is kept).

    Returns None when fewer than days+1 finite closes exist, or when the prior
    bar is 0 (div-by-zero guard — also: a zero prior-close data error makes
    _pct_return None → here) — never raises.
    [Calc: (c[-1] / c[-1-days] - 1) * 100]
    """
    finite = [
        c for c in closes
        if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
    ]
    if len(finite) <= days:
        return None
    prior = finite[-1 - days]
    if prior == 0:
        return None
    return (finite[-1] / prior - 1) * 100.0


def _bench_3m(raw, idx):
    """3-month % return for a market index from its fetched 1y closes;
    None when the index fetch is missing/insufficient (rs falls back to null)."""
    triple = raw.get(("index", idx))
    closes = triple[1] if triple else []
    return _pct_return(closes, 63)


def _fetch_chart_ohlcv(ticker, range_param="6mo", interval="1d"):
    """Call fetch_yahoo_quote_result and extract (price, ohlcv_dict, status).

    ohlcv_dict preserves None entries (NOT filtered) so indicator math keeps
    close/high/low/volume positionally aligned. `adjclose` is the adjusted
    series when Yahoo supplies it, else []. YAHOO_CHART_SHAPE only requires
    `close`; absent high/low/volume come back as [] and the reused indicator
    functions fail-close to their own None/insufficient_data sentinels.

    ISS-121 (Loop8 cycle 2): also returns a `status` dict so callers
    can distinguish "market value legitimately unavailable" from
    RATE_LIMIT / SHAPE_MISMATCH / transport failure when surfacing
    per-symbol status to the output JSON. Pre-fix only stderr carried
    the failure mode; downstream JSON consumers saw `null` regardless
    of the cause, making partial reads not safely skippable.

    status shape:
      {"status": "PASSED"|"PARTIAL"|"FAILED",
       "error_code": str|None,
       "error_detail": str|None}
    """
    status = {"status": "PASSED", "error_code": None, "error_detail": None}
    empty = {"close": [], "high": [], "low": [], "volume": [], "adjclose": []}
    try:
        chart_result = fetch_yahoo_quote_result(
            ticker, range_param=range_param, interval=interval,
        )
        if not chart_result.ok:
            # ISS-030 (Cycle 4): pre-fix dropped envelope.error silently
            # (chart became {} → caller saw `(None, [])` indistinguishable
            # from "no data"). Now log error.code/detail so operators can
            # tell rate-limit / shape-mismatch / transport apart from
            # legit empty market data.
            err = chart_result.error
            status = {
                "status": chart_result.status,
                "error_code": getattr(err, "code", None) and err.code.value,
                "error_detail": getattr(err, "detail", None),
            }
            print(
                f"[WARN] _fetch_chart_ohlcv({ticker}) envelope failed: "
                f"{status['error_code']} ({getattr(err, 'cause', None)}): "
                f"{status['error_detail']}",
                file=sys.stderr,
            )
            return None, empty, status

        chart = chart_result.data
        meta = chart.get("meta", {})
        quotes = chart.get("indicators", {}).get("quote", [{}])[0]
        adj_list = chart.get("indicators", {}).get("adjclose", [])
        adjclose = adj_list[0].get("adjclose", []) if adj_list else []
        ohlcv = {
            "close": list(quotes.get("close", [])),
            "high": list(quotes.get("high", [])),
            "low": list(quotes.get("low", [])),
            "volume": list(quotes.get("volume", [])),
            "adjclose": list(adjclose),
        }
        closes_nonnull = [c for c in ohlcv["close"] if c is not None]
        price = meta.get("regularMarketPrice")
        if price is None and closes_nonnull:
            price = closes_nonnull[-1]

        # ISS-220 4.18 (Loop34 cycle 1): downgrade status when price
        # is None despite envelope PASSED. Pre-fix Yahoo could return
        # a PASSED envelope with regularMarketPrice=None and closes
        # all-None (delisted/halted ticker), and `_fetch_chart`
        # returned `status={"status":"PASSED",...}` with `price=None`
        # → downstream consumers filtered for non-None close but
        # logged the call as "succeeded" — masking real "no usable
        # price" condition.
        if price is None and status.get("status") == "PASSED":
            status = {
                "status": "FAILED",
                "error_code": ErrorCode.NOT_FOUND.value,
                "error_detail": "Yahoo returned no usable price (regularMarketPrice + closes all None)",
            }
        return price, ohlcv, status
    except Exception as exc:  # pragma: no cover - defensive
        # Log exception TYPE too — "failed: timeout" vs "failed: KeyError" tell
        # operators very different stories (network blip vs schema drift).
        # The bare `{exc}` format omits the class name which hides this.
        print(
            f"[WARN] _fetch_chart_ohlcv({ticker}) failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None, empty, {
            "status": "FAILED",
            "error_code": "internal_error",
            "error_detail": f"{type(exc).__name__}: {exc}",
        }


def _fetch_chart(ticker, range_param="1y", interval="1d"):
    """Back-compat wrapper: (price, filtered_closes, status) for index/VIX/treasury."""
    price, ohlcv, status = _fetch_chart_ohlcv(ticker, range_param, interval)
    closes = [c for c in ohlcv["close"] if c is not None]
    return price, closes, status


def _load_rates_from_disk(reports_dir="reports"):
    """Glob for the most recent 09_macro_rates.json and extract FED rate."""
    from scripts.schemas.macro_rates import load_macro_rates

    reports_path = Path(reports_dir)
    # Sort by date directory name (YYYYMMDD), not full path (which sorts by ticker first)
    candidates = sorted(
        reports_path.glob("*/*/data/09_macro_rates.json"),
        key=lambda p: p.parts[-3] if len(p.parts) >= 3 else "",
        reverse=True,
    )
    if not candidates:
        return None

    try:
        doc = load_macro_rates(candidates[0])
    except (OSError, json.JSONDecodeError, ValueError, TypeError,
            AttributeError) as exc:
        # Matches pre-migration breadth. SchemaError is a ValueError
        # subclass so it's caught here. Genuine implementation bugs
        # (AssertionError, unexpected exceptions) still propagate.
        print(f"[WARN] Failed to read disk rates: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    fed = doc.find_current_rate("FED")
    if fed is None:
        return None
    return {"fed_funds": fed.rate, "source": str(candidates[0])}


def _fetch_rates_live():
    """Fetch interest rates snapshot from Financial Datasets API.

    Returns: (rates_dict_or_None, status_dict). status_dict mirrors the
    chart_statuses convention (status / error_code / error_detail) so the
    macro envelope can surface rate-fetch outcomes alongside chart fetches.

    ISS-144 (Loop14 cycle 1 fresh-session): pre-fix returned None on any
    non-PASSED envelope without logging or preserving error info; the
    macro output then showed `rates: {}` with no rates_status sibling,
    so consumers couldn't distinguish "API down" from "FED not in
    response". Now the error path logs the adapter ErrorCode + detail
    and returns a structured status alongside the (possibly None) rates.
    """
    status_dict = {"status": "PASSED"}
    try:
        from scripts.sources.financial_datasets import fetch_interest_rates_snapshot
        result_envelope = fetch_interest_rates_snapshot()
        if result_envelope.status != "PASSED":
            err = result_envelope.error
            err_code = err.code.value if err is not None else "UNKNOWN"
            err_detail = err.detail if err is not None else ""
            print(
                f"[WARN] Live rates fetch non-PASSED: "
                f"status={result_envelope.status} code={err_code} "
                f"detail={err_detail}",
                file=sys.stderr,
            )
            return None, {
                "status": result_envelope.status,
                "error_code": err_code,
                "error_detail": err_detail,
            }
        result = result_envelope.data
        rates = result.get("rates", [])
        for r in rates:
            if r.get("bank") == "FED":
                rate_val = r.get("rate")
                if rate_val is not None:
                    return (
                        {"fed_funds": rate_val, "source": "financial_datasets_api"},
                        status_dict,
                    )
        # PASSED envelope but no FED bank — observable as PARTIAL data.
        # ISS-217 (Loop31 cycle 1 fresh-session-18): use canonical
        # ErrorCode value (lower-case "not_found") so this side-channel
        # status agrees with envelope-emitted error codes elsewhere.
        return None, {
            "status": "PARTIAL",
            "error_code": ErrorCode.NOT_FOUND.value,
            "error_detail": "FED bank not present in rates response",
        }
    except Exception as exc:
        print(f"[WARN] Live rates fetch failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        # ISS-217: emit canonical ErrorCode; preserve the exception class
        # in error_detail so operators still see the underlying cause.
        return None, {
            "status": "FAILED",
            "error_code": ErrorCode.INTERNAL_ERROR.value,
            "error_detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_macro_snapshot(tickers=None, rates_fallback=None, reports_dir="reports"):
    """Fetch macro market snapshot.

    Args:
        tickers: Portfolio ticker symbols (current prices only).
        rates_fallback: Pre-loaded rates dict (``{"fed_funds": ...}``).
        reports_dir: Root reports directory for disk rate fallback.

    Returns:
        dict with keys: market, volatility, rates, ticker_prices, as_of
    """
    tickers = tickers or []

    # ------------------------------------------------------------------
    # Build job list — all fetches run in parallel
    # ------------------------------------------------------------------
    futures = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        # Market indices (1y for MA200)
        for idx in MARKET_INDICES:
            futures[pool.submit(_fetch_chart, idx, "1y", "1d")] = ("index", idx)

        # VIX (3mo for MA20)
        futures[pool.submit(_fetch_chart, VIX_TICKER, "3mo", "1d")] = ("vix", VIX_TICKER)

        # Portfolio tickers (6mo for run-day technical indicators: RSI/MACD/
        # Bollinger/volume + RSI-divergence lookback 60 need >=74 bars; 5d only
        # carried current price and dropped volume — plan
        # 2026-05-27-portfolio-runday-technicals).
        for t in tickers:
            futures[pool.submit(_fetch_chart_ohlcv, t, "6mo", "1d")] = ("ticker", t)

        # Treasury yields (5d)
        futures[pool.submit(_fetch_chart, TREASURY_10Y, "5d", "1d")] = ("treasury", "10y")
        futures[pool.submit(_fetch_chart, TREASURY_5Y, "5d", "1d")] = ("treasury", "5y")

        # Collect results
        raw = {}
        for future in as_completed(futures):
            tag = futures[future]
            try:
                raw[tag] = future.result()
            except Exception as exc:
                print(f"[WARN] Future {tag} raised: {exc}", file=sys.stderr)
                raw[tag] = (None, [], {
                    "status": "FAILED",
                    "error_code": "internal_error",
                    "error_detail": f"{type(exc).__name__}: {exc}",
                })

    # ISS-121 (Loop8 cycle 2): aggregate per-symbol status so JSON
    # consumers can distinguish "no data legitimately" from "Yahoo
    # rate-limited / shape-drifted / transport-failed". Default empty
    # for the tag is a synthetic FAILED — happens only if the
    # ThreadPoolExecutor never produced a future for that tag.
    _missing_status = {
        "status": "FAILED",
        "error_code": "internal_error",
        "error_detail": "no future produced for this tag",
    }

    # ------------------------------------------------------------------
    # Assemble market indices
    # ------------------------------------------------------------------
    market = {}
    market_statuses = {}
    for idx in MARKET_INDICES:
        triple = raw.get(("index", idx))
        if triple is None:
            price, closes, status = None, [], _missing_status
        else:
            price, closes, status = triple
        market[idx] = {
            "price": price,
            "ma20": _sma_rounded(closes, 20),
            "ma50": _sma_rounded(closes, 50),
            "ma200": _sma_rounded(closes, 200),
        }
        market_statuses[idx] = status

    # Benchmark 3m returns for relative-strength facts (computed ONCE; spec
    # 2026-05-31-relative-strength-fact). SPY/QQQ closes were fetched @1y above.
    bench_returns = {"SPY": _bench_3m(raw, "SPY"), "QQQ": _bench_3m(raw, "QQQ")}

    # ------------------------------------------------------------------
    # Assemble VIX
    # ------------------------------------------------------------------
    vix_triple = raw.get(("vix", VIX_TICKER))
    if vix_triple is None:
        vix_price, vix_closes, vix_status = None, [], _missing_status
    else:
        vix_price, vix_closes, vix_status = vix_triple
    volatility = {
        "vix": vix_price,
        "vix_ma20": _sma_rounded(vix_closes, 20),
    }
    # ISS-132 (Loop9 cycle 1): keyed-by-symbol map for shape symmetry
    # with chart_statuses["market"] / ["treasury"] / ["ticker_prices"].
    # Pre-fix this was a single status dict — generic JSON consumers
    # iterating `section -> symbol -> status` would mistake the inner
    # `status` / `error_code` keys for symbols. Now uniformly
    # `chart_statuses[section][symbol] -> status_dict`.
    volatility_statuses = {VIX_TICKER: vix_status}

    # ------------------------------------------------------------------
    # Interest rates: fallback → disk → API
    # ------------------------------------------------------------------
    # ISS-133 (Loop9 cycle 1): never mutate caller-provided rates_fallback
    # in place. We add us_10y / us_5y / spreads / _deprecated_keys
    # below; with the pre-fix `rates = rates_fallback` aliasing, two
    # successive calls of `fetch_macro_snapshot(rates_fallback=X)`
    # would see X grow each time.
    # ISS-220 4.13 (Loop33 cycle 1): a shallow `dict(...)` copy is
    # insufficient — `rates.setdefault("_deprecated_keys", []).append(...)`
    # below mutates the nested list, which a shallow copy still shares
    # with the caller's dict. Use deepcopy so caller-owned nested
    # mutables stay untouched.
    import copy as _copy
    # ISS-220 4.33 (Loop38 cycle 1, iter7): truthy check (was `is not None`).
    # Pre-fix `rates_fallback={}` (empty dict) was treated as "valid
    # fallback provided" → status PASSED + downstream path skipped
    # disk/live fetch entirely → emit empty rates as PASSED. Empty
    # dict is semantically equivalent to "no data available" and
    # should fall through to disk+live; only a populated dict
    # represents a real pre-loaded fallback.
    rates = _copy.deepcopy(rates_fallback) if rates_fallback else None
    rates_status = {"status": "PASSED", "source": "fallback"} if rates else None
    if rates is None:
        rates = _load_rates_from_disk(reports_dir)
        if rates is not None:
            rates_status = {"status": "PASSED", "source": "disk"}
    if rates is None:
        # ISS-144 (Loop14 cycle 1): _fetch_rates_live now returns a
        # status side-channel so the macro envelope surfaces upstream
        # rate-fetch outcomes (RATE_LIMIT / UPSTREAM_ERROR / etc.)
        # alongside the chart_statuses mapping, instead of silently
        # emitting `rates: {}` with no observability.
        rates, rates_status = _fetch_rates_live()
    if rates is None:
        rates = {}
        if rates_status is None:
            # ISS-217 (Loop31 cycle 1 fresh-session-18): "NO_SOURCE" was
            # not an ErrorCode value. NOT_FOUND best fits "no available
            # source produced a rate" — rate is genuinely absent rather
            # than malformed.
            rates_status = {
                "status": "FAILED",
                "error_code": ErrorCode.NOT_FOUND.value,
                "error_detail": "no rates_fallback, no disk cache, no live fetch",
            }

    # Treasury yields from Yahoo
    # ^FVX is the 5Y (not 2Y) — canonical key is ``us_5y``. The legacy
    # ``us_2y`` / ``spread_10y_2y`` keys are kept as a one-release
    # deprecation shim with identical values and flagged in
    # ``_deprecated_keys``. Remove next release.
    t10y_triple = raw.get(("treasury", "10y"))
    t5y_triple = raw.get(("treasury", "5y"))
    t10y_price = t10y_triple[0] if t10y_triple else None
    t5y_price = t5y_triple[0] if t5y_triple else None
    treasury_statuses = {
        "10y": t10y_triple[2] if t10y_triple else _missing_status,
        "5y": t5y_triple[2] if t5y_triple else _missing_status,
    }

    if t10y_price is not None and "us_10y" not in rates:
        rates["us_10y"] = round(t10y_price, 3)
    if t5y_price is not None and "us_5y" not in rates:
        rates["us_5y"] = round(t5y_price, 3)
        # Deprecation shim: preserve us_2y during transition window.
        rates["us_2y"] = rates["us_5y"]
        deprecated = rates.setdefault("_deprecated_keys", [])
        if "us_2y" not in deprecated:
            deprecated.append("us_2y")

    # Yield spread (10Y - 5Y). ``spread_10y_5y`` replaces ``spread_10y_2y``
    # (deprecated); macro.py currently emits both for back-compat.
    # ISS-220 4.22 (Loop35 cycle 1): coerce via safe_num before
    # arithmetic. Pre-fix `is not None` only — a string rate
    # (`"4.5"`) → TypeError on subtraction; a bool rate (True) →
    # silent `1 - 0 = 1.0` bogus spread. safe_num returns None for
    # bool / non-numeric / non-finite, so the surrounding
    # `is not None` gate now also catches drift.
    from scripts.sources.common import safe_num as _safe_num
    y10 = _safe_num(rates.get("us_10y"))
    y5 = _safe_num(rates.get("us_5y"))
    if y10 is not None and y5 is not None:
        rates["spread_10y_5y"] = round(y10 - y5, 3)
        # Deprecation shim
        rates["spread_10y_2y"] = rates["spread_10y_5y"]
        deprecated = rates.setdefault("_deprecated_keys", [])
        if "spread_10y_2y" not in deprecated:
            deprecated.append("spread_10y_2y")

    # ------------------------------------------------------------------
    # Ticker prices + run-day technical indicators
    # ------------------------------------------------------------------
    ticker_prices = {}
    ticker_price_statuses = {}
    ticker_indicators = {}
    for t in tickers:
        triple = raw.get(("ticker", t))
        if triple is None:
            ticker_prices[t] = None
            ticker_price_statuses[t] = _missing_status
            ticker_indicators[t] = None
            continue
        price, ohlcv, status = triple
        ticker_prices[t] = price
        ticker_price_statuses[t] = status
        # null when the fetch failed or there are too few bars; otherwise the
        # raw block (its legs carry their own null/insufficient_data sentinels).
        if status.get("status") == "PASSED" and isinstance(ohlcv, dict):
            ticker_indicators[t] = _compute_ticker_indicators(
                ohlcv, price, bench_returns=bench_returns)
        else:
            ticker_indicators[t] = None

    return {
        "market": market,
        "volatility": volatility,
        "rates": rates,
        "ticker_prices": ticker_prices,
        "ticker_indicators": ticker_indicators,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        # ISS-121 (Loop8 cycle 2): per-symbol chart fetch status maps so
        # JSON consumers can distinguish unavailable-due-to-rate-limit /
        # shape-mismatch / transport from "no data" silently.
        # ISS-132 (Loop9 cycle 1): every section is a map keyed by
        # symbol so generic consumers can iterate uniformly.
        # ISS-144 (Loop14 cycle 1): rates_status surfaces interest-rate
        # fetch outcome (PASSED with source / PARTIAL FED-missing /
        # FAILED upstream) parallel to the per-chart statuses, closing
        # the observability gap for `_fetch_rates_live`.
        "chart_statuses": {
            "market": market_statuses,
            "volatility": volatility_statuses,
            "treasury": treasury_statuses,
            "ticker_prices": ticker_price_statuses,
        },
        "rates_status": rates_status,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch macro market indicators and portfolio ticker prices.",
    )
    parser.add_argument(
        "--tickers", nargs="*", default=[],
        help="Portfolio tickers to fetch current prices for.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output file path (default: stdout).",
    )
    args = parser.parse_args()

    result = fetch_macro_snapshot(tickers=args.tickers)

    from scripts.cli_utils import write_output
    write_output(result, args.output)


if __name__ == "__main__":
    _main()
