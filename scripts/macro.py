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
from zoneinfo import ZoneInfo

from scripts.sources.yahoo_finance import fetch_yahoo_quote_result
from scripts.sources.adapter_result import ErrorCode
from scripts.delta.calendar import last_closed_trading_day
from scripts.indicators import merge_adjusted_closes
from scripts.price_structure import (
    INDICATOR_SLICE_SESSIONS,
    compute_cohort_recovery,
    compute_ticker_price_structure,
    select_cohort_event,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MARKET_INDICES = ["SPY", "QQQ", "^DJI"]
VIX_TICKER = "^VIX"
_ET = ZoneInfo("America/New_York")
MAX_EVENT_STALENESS_SESSIONS = 10   # user parameter 2026-08-09

# Price provenance defaults for every failure-path status dict — keys are
# ALWAYS present so consumers never need .get() ambiguity (feedback
# 2026-06-11 #1; plan-review LOW-12/r2 MED-5).
_NO_PRICE_PROVENANCE = {
    "price_source": None,
    "price_as_of": None,
    "stale_meta_quote": False,
    "price_conflict_same_ts": False,
}


def _et_date_iso(epoch):
    """ET calendar date (ISO) for an epoch-seconds timestamp, else None."""
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    try:
        return (datetime.fromtimestamp(epoch, tz=timezone.utc)
                .astimezone(_ET).date().isoformat())
    except (OverflowError, OSError, ValueError):
        return None
TREASURY_10Y = "^TNX"
# ^FVX is the 5-Year Treasury yield (Yahoo Finance lacks a 2Y ticker).
# Historically this was emitted as ``us_2y`` with ``spread_10y_2y`` — a
# mislabel that corrupted the inversion signal. Now emitted canonically as
# ``us_5y`` / ``spread_10y_5y``; the legacy ``us_2y`` / ``spread_10y_2y``
# deprecation shims were REMOVED (consumers migrated to the 5Y keys).
TREASURY_5Y = "^FVX"
# Back-compat alias so any external import keeps working for one release.
TREASURY_5Y_AS_2Y_PROXY = TREASURY_5Y


# Minimum usable closes for indicator block: calc_rsi_series starts RSI at
# index period (14), so the divergence lookback of 60 needs 14+60=74 closes.
_MIN_INDICATOR_BARS = 74

# NOTE (codex cold-round 14): there is deliberately NO disk-cache
# freshness window — the live source is always tried first (even a
# same-day cache can straddle a 14:00 ET FOMC announcement), and a disk
# cache is used only after live failure, always marked PARTIAL with its
# mtime-derived vintage.


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
        _sanitize_closes, adjust_high_low,
    )
    from scripts.indicators import merge_adjusted_closes
    raw_close = list(ohlcv.get("close", []))
    adj = list(ohlcv.get("adjclose", []))
    # Shared merge (cold-round finding): finite-positive preferred, same
    # basis criterion as adjust_high_low — one implementation across the
    # indicators CLI, this run-day path, and _bench_3m.
    closes = merge_adjusted_closes(raw_close, adj)
    # Probe 1A: same basis-consistency as scripts.indicators — highs/lows are
    # scaled onto the adjusted basis so run-day ATR doesn't explode on a
    # ticker with a split inside the chart window.
    highs, lows = adjust_high_low(
        list(ohlcv.get("high", [])),
        list(ohlcv.get("low", [])),
        raw_close,
        adj,
    )
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

def _fin_pos_close(c) -> bool:
    """Finite-positive close gate for display/MA series (round-24 F2)."""
    from scripts.indicators import _fin_pos
    return _fin_pos(c)


def _sma_rounded(closes, period):
    """Simple moving average over the last *period* closes, rounded to 2dp."""
    if period < 1 or len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def _pct_return(closes, days: int = 63):
    """Percent return over `days` trading bars, computed on the
    finite-POSITIVE close series only (None/NaN/Inf/0/negative dropped —
    closes are prices).

    Returns None when fewer than days+1 usable closes exist — never raises.
    [Calc: (c[-1] / c[-1-days] - 1) * 100]
    """
    # Finite POSITIVE only (fourth cold round): closes are prices — one
    # corrupt 0/negative bar previously emitted an extreme numeric return
    # (e.g. -17500%) instead of dropping the bar. Same contract as
    # indicators._sanitize_closes.
    finite = [
        c for c in closes
        if isinstance(c, (int, float)) and not isinstance(c, bool)
        and math.isfinite(c) and c > 0
    ]
    if len(finite) <= days:
        return None
    prior = finite[-1 - days]
    return (finite[-1] / prior - 1) * 100.0


def _bench_3m(raw, idx, anchor_iso):
    """3-month % return for a market index from its fetched 1y closes;
    None when the index fetch is missing/insufficient (rs falls back to null).

    Anchored to the last completed ET session (same rationale as
    _anchored_ohlcv): rs_vs_*_3m compares this to ticker returns computed
    from anchored series — an unanchored benchmark would mix today's live
    partial bar into one side of the subtraction."""
    triple = raw.get(("index", idx))
    # isinstance guard: index slots hold the OHLCV dict post-#2 refactor, but
    # the future-exception fallback still stores (None, [], status) — a LIST.
    ohlcv = triple[1] if triple and isinstance(triple[1], dict) else {}
    ohlcv = _anchored_ohlcv(ohlcv, anchor_iso)
    # Adjusted-close basis, same merge as _compute_ticker_indicators: RS
    # facts compare ticker returns (adjclose-based) to this benchmark — a
    # raw-close benchmark understates SPY/QQQ by every distribution in the
    # window, biasing rs_vs_*_3m (cold review 2026-06-11 R1 HIGH-4).
    from scripts.indicators import merge_adjusted_closes, _fin_pos
    raw_close = list(ohlcv.get("close") or [])
    adj = list(ohlcv.get("adjclose") or [])
    # Shared merge + finite-positive filter (cold-round finding): a corrupt
    # 0-value benchmark bar previously produced a -100% benchmark return →
    # a fabricated +100% rs_vs_* fact. Unusable bars are dropped, matching
    # the ticker path's treatment.
    closes = [c for c in merge_adjusted_closes(raw_close, adj) if _fin_pos(c)]
    return _pct_return(closes, 63)


def _anchored_series(ohlcv, anchor_iso):
    """(close_at_anchor, closes_through_anchor) from ts-aligned OHLCV.

    Anchors regime inputs to the last COMPLETED ET session (feedback
    2026-06-11 #2): ^VIX quotes nearly 24h on Cboe, so pre-market its meta
    quote AND its current-day partial bar are live while index quotes still
    show the prior close — a clock mix that flipped risk_off to risk_on.
    Everything dated after the anchor (the live partial bar) is excluded
    from both the anchored close and the MA window.

    Known limitation (deliberate, anti-ratchet): last_closed_trading_day()
    counts today only after 16:00 ET, so on the ~3 US early-close days/year
    the anchor lags one session between ~13:00-16:00 ET. That is a
    CONSISTENT lag across all inputs — not the clock mix this fixes — and
    an early-close calendar is machinery without a named real failure.
    """
    closes_thru, close_at = [], None
    from scripts.indicators import _fin_pos
    for ts_i, c in zip(ohlcv.get("timestamp") or [], ohlcv.get("close") or []):
        # Round-20 F2: gate on finite-POSITIVE, not just non-None — a
        # provider missing-value drift encoded as numeric 0.0 previously
        # entered the series and read as a subdued VIX close (0.0 < ma20),
        # flipping the deterministic regime to risk_on.
        if not _fin_pos(c):
            continue
        d = _et_date_iso(ts_i)
        if d is None or d > anchor_iso:
            continue
        closes_thru.append(c)
        if d == anchor_iso:
            close_at = c
    return close_at, closes_thru


def _anchored_ohlcv(ohlcv, anchor_iso):
    """OHLCV dict filtered to bars dated <= anchor (drops the live partial bar).

    Same clock hazard as _anchored_series (feedback 2026-06-11 #2), applied
    to the FULL bar arrays: run-day ticker indicators (volume ratio, MACD,
    RSI, price/volume classification) feed the authoritative #2/#3
    entry/reduce gates in prompts/portfolio-decide.md, and during market
    hours the live session bar carries a fraction-complete volume — a
    genuinely volume-confirmed breakout at 10:30 ET reads "0.3x, no volume
    confirmation" from the partial bar.

    Bars whose timestamp is missing/unparseable are dropped (they cannot be
    proven complete). No timestamp array at all → everything drops → the
    74-bar indicator gate fails closed to None rather than serving an
    unanchorable read.
    """
    ts = ohlcv.get("timestamp") or []
    keep = []
    for i, t in enumerate(ts):
        d = _et_date_iso(t)
        if d is not None and d <= anchor_iso:
            keep.append(i)
    out = dict(ohlcv)
    for k in ("timestamp", "open", "high", "low", "close", "adjclose", "volume"):
        arr = ohlcv.get(k)
        if isinstance(arr, list):
            out[k] = [arr[i] for i in keep if i < len(arr)]
    return out


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
    empty = {"close": [], "high": [], "low": [], "volume": [], "adjclose": [], "timestamp": []}
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
                **_NO_PRICE_PROVENANCE,
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
        timestamps = list(chart.get("timestamp", []))
        ohlcv["timestamp"] = timestamps
        # Price-structure Task 5: a SCALAR key — survives _anchored_ohlcv's
        # dict copy (only the known LIST keys are overwritten there) — so
        # compute_ticker_price_structure() can prove/refute a since-
        # inception high against the true firstTradeDate.
        ohlcv["first_trade_session"] = _et_date_iso(meta.get("firstTradeDate"))
        rmp = meta.get("regularMarketPrice")
        rmt = meta.get("regularMarketTime")
        # Round-21 F1: a non-positive/non-finite meta quote is not a price —
        # a provider-bugged rmp=-1.0 with a newer timestamp previously beat
        # a valid positive chart bar and shipped as the current price under
        # PASSED. Same gate on the bar-close candidates.
        from scripts.indicators import _fin_pos as _fp
        if rmp is not None and not _fp(rmp):
            rmp = None
        # Last bar with a usable (finite-positive) close, positionally
        # aligned with timestamp.
        last_bar_ts, last_bar_close = None, None
        for ts_i, close_i in zip(timestamps, ohlcv["close"]):
            if _fp(close_i):
                last_bar_ts, last_bar_close = ts_i, close_i

        # Timestamp-aware pick (feedback 2026-06-11 #1): Yahoo's meta quote
        # for thin OTC ADRs can lag the SAME response's chart bars by a full
        # session (live-reproduced: MRAAY rmp 29.73@06-09 vs bar 27.09@06-10,
        # +9.7% — the stale quote priced a real reduce order unfillable).
        # Never trust rmp unconditionally — use whichever source is stamped
        # later; surface provenance so consumers can see price vintage.
        price = rmp
        price_source = "meta" if rmp is not None else None
        price_as_of = _et_date_iso(rmt) if rmp is not None else None
        stale_meta = False
        if rmp is not None and rmt is None:
            price_source = "meta_unverified"  # clocks can't be compared — flag
            price_as_of = None
        if last_bar_close is not None:
            if rmp is None or (rmt is None and last_bar_ts is not None):
                # No meta price — or an UNDATED meta vs a DATED bar: the
                # source with verifiable provenance wins (cold review
                # 2026-06-11 R2 HIGH-5 — an unverifiable quote must not
                # outrank a dated close on the money path).
                price = last_bar_close
                price_source = "chart_bar"
                price_as_of = _et_date_iso(last_bar_ts)
            elif rmt is not None and last_bar_ts is not None and last_bar_ts > rmt:
                stale_meta = True
                price = last_bar_close
                price_source = "chart_bar"
                price_as_of = _et_date_iso(last_bar_ts)
                print(
                    f"[WARN] _fetch_chart_ohlcv({ticker}): meta quote {rmp} "
                    f"({_et_date_iso(rmt)}) is STALER than last chart bar "
                    f"{last_bar_close} ({_et_date_iso(last_bar_ts)}) — using the bar.",
                    file=sys.stderr,
                )
        # Pathological: same timestamp, materially different price → keep
        # meta, flag loud (plan-review MED-5).
        conflict_same_ts = (
            rmp is not None and rmt is not None and last_bar_ts is not None
            and rmt == last_bar_ts and last_bar_close is not None
            and abs(rmp - last_bar_close) > max(abs(rmp), 1e-9) * 0.001
        )
        if conflict_same_ts:
            print(
                f"[WARN] _fetch_chart_ohlcv({ticker}): meta {rmp} and bar "
                f"{last_bar_close} share timestamp but diverge — keeping meta, flagged.",
                file=sys.stderr,
            )
        status["price_source"] = price_source
        status["price_as_of"] = price_as_of
        status["stale_meta_quote"] = stale_meta
        status["price_conflict_same_ts"] = conflict_same_ts

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
                **_NO_PRICE_PROVENANCE,
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
            **_NO_PRICE_PROVENANCE,
        }


def _fetch_chart(ticker, range_param="1y", interval="1d"):
    """Back-compat wrapper: (price, filtered_closes, status) for treasury callers."""
    price, ohlcv, status = _fetch_chart_ohlcv(ticker, range_param, interval)
    closes_src = ohlcv.get("close") if isinstance(ohlcv, dict) else []
    closes = [c for c in (closes_src or []) if c is not None]
    return price, closes, status


def _load_rates_from_disk(reports_dir="reports"):
    """Glob for the most recent 09_macro_rates.json and extract FED rate.

    The returned dict carries ``as_of_date`` (ISO) — the CACHE-FETCH
    vintage, derived from the file's mtime — so the caller can enforce a
    freshness bound. Two sources are deliberately NOT used (codex
    cold-rounds 4/10): the date-DIR name, because a delta partial/no-op
    run copies the prior ``09_macro_rates.json`` into today's run dir
    (copy_data uses shutil.copy2, which preserves mtime exactly, so mtime
    stays honest for copies); and the FED row's own ``date`` field,
    because it is the rate's EFFECTIVE date (last FOMC change — e.g.
    2026-06-01 inside a file fetched 2026-07-30), which would make every
    fresh cache look weeks stale. Unknown vintage → ``as_of_date`` None
    (caller treats age-unknown as stale).
    """
    from scripts.schemas.macro_rates import load_macro_rates

    reports_path = Path(reports_dir)

    def _mtime_or_zero(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    # Select by FILE MTIME (dir date as tie-break only): candidate
    # selection and the freshness bound must use the same vintage key
    # (codex cold-round 11). Sorting by date-dir name alone let today's
    # delta COPY of a month-old rates file (mtime preserved by copy2)
    # outrank yesterday's genuinely fresh fetch in another ticker's dir —
    # the stale copy then failed the age bound while the fresh cache was
    # never even considered.
    candidates = sorted(
        reports_path.glob("*/*/data/09_macro_rates.json"),
        key=lambda p: (_mtime_or_zero(p),
                       p.parts[-3] if len(p.parts) >= 3 else ""),
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
    try:
        mtime = candidates[0].stat().st_mtime
        as_of_date = (datetime.fromtimestamp(mtime, tz=timezone.utc)
                      .astimezone(_ET).date().isoformat())
    except (OSError, OverflowError, ValueError):
        as_of_date = None
    return {
        "fed_funds": fed.rate,
        "source": str(candidates[0]),
        "as_of_date": as_of_date,
    }


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
                # Round-20 F1: the [0,25] percent-unit domain gate existed
                # only on the disk-cache load path — a live provider unit
                # drift (basis points: 375.0) shipped as fed_funds under
                # PASSED. One implementation: schemas.macro_rates owns the
                # domain.
                from scripts.schemas.macro_rates import rate_in_percent_domain
                if rate_val is not None and not rate_in_percent_domain(rate_val):
                    print(
                        f"[WARN] Live FED rate {rate_val!r} outside percent "
                        f"domain [0, 25] (unit drift?) — discarding live "
                        f"value, treating rates as FAILED.",
                        file=sys.stderr,
                    )
                    return None, {
                        "status": "FAILED",
                        "error_code": "unit_domain",
                        "error_detail": (
                            f"FED rate {rate_val!r} outside percent domain "
                            f"[0, 25]"
                        ),
                    }
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

def fetch_macro_snapshot(tickers=None, rates_fallback=None, reports_dir="reports",
                         vendor_aliases=None):
    """Fetch macro market snapshot.

    Args:
        tickers: Portfolio ticker symbols (current prices only).
        rates_fallback: Pre-loaded rates dict (``{"fed_funds": ...}``).
        reports_dir: Root reports directory for disk rate fallback.
        vendor_aliases: optional ``{state_key: vendor_symbol}`` map (feedback
            2026-06-11 #5 — broker renamed the symbol while the data vendor
            still quotes the old one). The VENDOR symbol is fetched; ALL
            output stays keyed by the state key, with ``vendor_symbol``
            recorded in that ticker's chart status.

    Returns:
        dict with keys: market, volatility, regime_inputs, rates,
        ticker_prices, ticker_indicators, ticker_price_structure,
        universe_rebound_structure, as_of. ``ticker_price_structure`` is a
        per-ticker closing-basis price-structure block (or ``None`` on
        fetch failure) from ``scripts.price_structure.
        compute_ticker_price_structure``; ``universe_rebound_structure`` is
        the shared cohort selloff/recovery block (``compute_cohort_recovery``
        wrapping ``select_cohort_event``) over ALL requested tickers —
        failed tickers stay in the denominator with an empty series.
    """
    tickers = tickers or []
    vendor_aliases = vendor_aliases or {}

    # ------------------------------------------------------------------
    # Build job list — all fetches run in parallel
    # ------------------------------------------------------------------
    futures = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        # Market indices (2y — was 1y). OHLCV variant: regime anchoring
        # needs the timestamp array (feedback 2026-06-11 #2). The 2y range
        # also serves as the broad-index MARKET CALENDAR consensus below,
        # which must cover hold episodes up to ~2y old.
        for idx in MARKET_INDICES:
            futures[pool.submit(_fetch_chart_ohlcv, idx, "2y", "1d")] = ("index", idx)

        # VIX (3mo for MA20) — OHLCV variant, same reason.
        futures[pool.submit(_fetch_chart_ohlcv, VIX_TICKER, "3mo", "1d")] = ("vix", VIX_TICKER)

        # Portfolio tickers (2y — was 6mo; price-structure Task 5: hold
        # detection needs each candidate's own preceding 252 sessions, so
        # the fetch window must reach back that far. Run-day indicator input
        # is then sliced BACK to INDICATOR_SLICE_SESSIONS (126) below —
        # a longer input can flip MACD's crossover read).
        for t in tickers:
            futures[pool.submit(
                _fetch_chart_ohlcv, vendor_aliases.get(t, t), "2y", "1d",
            )] = ("ticker", t)

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
            price, ohlcv_i, status = None, {}, _missing_status
        else:
            price, ohlcv_i, status = triple
        # isinstance guard: the future-exception fallback stores (None, [],
        # status) — the middle slot can be a LIST, not the OHLCV dict.
        if not isinstance(ohlcv_i, dict):
            ohlcv_i = {}
        # Round-24 F2: finite-positive, not just non-None — a 0.0 close
        # (provider missing-value drift) skewed the displayed MA under
        # PASSED (100.0-avg window read 95.0).
        closes = [c for c in (ohlcv_i.get("close") or []) if _fin_pos_close(c)]
        market[idx] = {
            "price": price,
            "ma20": _sma_rounded(closes, 20),
            "ma50": _sma_rounded(closes, 50),
            "ma200": _sma_rounded(closes, 200),
        }
        market_statuses[idx] = status

    # Clock anchor (last COMPLETED ET session — feedback 2026-06-11 #2).
    # Computed once here: the regime block, the benchmark 3m returns, and the
    # per-ticker run-day indicators must all exclude the live partial bar.
    anchor = last_closed_trading_day().isoformat()

    # Benchmark 3m returns for relative-strength facts (computed ONCE; spec
    # 2026-05-31-relative-strength-fact). SPY/QQQ closes were fetched @1y above.
    bench_returns = {
        "SPY": _bench_3m(raw, "SPY", anchor),
        "QQQ": _bench_3m(raw, "QQQ", anchor),
    }

    # ------------------------------------------------------------------
    # Assemble VIX
    # ------------------------------------------------------------------
    vix_triple = raw.get(("vix", VIX_TICKER))
    if vix_triple is None:
        vix_price, vix_ohlcv, vix_status = None, {}, _missing_status
    else:
        vix_price, vix_ohlcv, vix_status = vix_triple
    if not isinstance(vix_ohlcv, dict):  # future-exception fallback shape
        vix_ohlcv = {}
    vix_closes = [c for c in (vix_ohlcv.get("close") or [])
                  if _fin_pos_close(c)]  # round-24 F2: same gate as indices
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
    # Clock-anchored regime inputs (feedback 2026-06-11 #2)
    # ------------------------------------------------------------------
    # All values anchored to the last COMPLETED ET session so the regime
    # classifier never mixes a live VIX with prior-close indices. The
    # live values stay in market/volatility above (display semantics);
    # _classify_regime consumes ONLY this block when present. `anchor`
    # was computed once above (shared with bench_returns + ticker
    # indicators).
    regime_indices = {}
    for idx in MARKET_INDICES:
        triple = raw.get(("index", idx))
        ohlcv_i = triple[1] if triple and isinstance(triple[1], dict) else {}
        close_at, closes_thru = _anchored_series(ohlcv_i, anchor)
        # ma200 + distance off the 52w high: the strategy regime layer's
        # primary gauges (index vs MA200; drawdown depth as trend-structure
        # proxy). Anchored series only — the live `market` block mixes a
        # pre-market quote with prior-close MAs (the clock hazard this
        # block exists to prevent).
        # Price-structure Task 5: the index fetch is now 2y (was 1y) so the
        # broad-index MARKET CALENDAR consensus below can cover ~2y hold
        # episodes. high_52w must stay a TRUE 52-week (252-session) high —
        # re-slice to the trailing 252 closes so the extra year of history
        # doesn't silently turn this into a 2-year high.
        closes_thru = closes_thru[-252:]
        high_52w = round(max(closes_thru), 2) if closes_thru else None
        off_52w_high_pct = (
            round((close_at - high_52w) / high_52w * 100, 2)
            if close_at is not None and high_52w
            else None
        )
        regime_indices[idx] = {
            "close": close_at,
            "ma50": _sma_rounded(closes_thru, 50),
            "ma200": _sma_rounded(closes_thru, 200),
            "high_52w": high_52w,
            "off_52w_high_pct": off_52w_high_pct,
        }
    vix_close_at, vix_thru = _anchored_series(vix_ohlcv, anchor)
    regime_inputs = {
        "anchor_session": anchor,
        "indices": regime_indices,
        "vix": {"close": vix_close_at, "ma20": _sma_rounded(vix_thru, 20)},
    }

    # ------------------------------------------------------------------
    # Interest rates: fallback → disk → API
    # ------------------------------------------------------------------
    # ISS-133 (Loop9 cycle 1): never mutate caller-provided rates_fallback
    # in place. We add us_10y / us_5y / spreads below; with the pre-fix
    # `rates = rates_fallback` aliasing, two successive calls of
    # `fetch_macro_snapshot(rates_fallback=X)` would see X grow each time.
    # ISS-220 4.13 (Loop33 cycle 1): use deepcopy (not a shallow `dict(...)`)
    # so any caller-owned NESTED mutable stays untouched even if a future
    # field appends to a nested list. (Historically the `_deprecated_keys`
    # shim list was the nested mutable; that shim has since been removed.)
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
        # Live FIRST (codex cold-rounds 1/14). Pre-fix, ANY disk cache
        # short-circuited the live fetch and shipped as PASSED — but even
        # a same-day cache can straddle an FOMC change (announcements are
        # 14:00 ET), so a cached fed_funds must never preempt a healthy
        # live source. One rates call per run is cheap; a plausible wrong
        # policy rate feeding rate-sensitive principles is not.
        #
        # ISS-144 (Loop14 cycle 1): _fetch_rates_live returns a status
        # side-channel so the macro envelope surfaces upstream rate-fetch
        # outcomes (RATE_LIMIT / UPSTREAM_ERROR / etc.) alongside the
        # chart_statuses mapping, instead of silently emitting `rates: {}`
        # with no observability.
        rates, rates_status = _fetch_rates_live()
    if rates is None:
        # Live failed → newest disk cache (by mtime), ALWAYS marked
        # PARTIAL with its vintage — cached-but-labeled beats nothing,
        # and it must never masquerade as PASSED regardless of age.
        disk_rates = _load_rates_from_disk(reports_dir)
        if disk_rates is not None:
            age_days = None
            as_of = disk_rates.get("as_of_date")
            if as_of:
                try:
                    from datetime import date as _date
                    from scripts.delta.calendar import today_et
                    age_days = (today_et() - _date.fromisoformat(as_of)).days
                except ValueError:
                    age_days = None
            rates = disk_rates
            rates_status = {
                "status": "PARTIAL",
                "source": "disk",
                "error_detail": (
                    f"live rates fetch failed; disk cache is "
                    f"{'of unknown age' if age_days is None else f'{age_days} days old'} "
                    f"(as_of {disk_rates.get('as_of_date')!r})"
                ),
            }
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
    # ^FVX is the 5Y (not 2Y) — canonical keys are ``us_5y`` / ``spread_10y_5y``.
    # The legacy ``us_2y`` / ``spread_10y_2y`` deprecation shims (identical 5Y
    # values under a 2Y label) were REMOVED here — do NOT re-introduce a
    # ``us_2y`` key for ^FVX (it is a 5Y; audit pattern J / rules/units.md HIGH-4).
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

    # Yield spread (10Y - 5Y) → ``spread_10y_5y``. The deprecated
    # ``spread_10y_2y`` shim has been removed (only the 5Y spread is emitted).
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

    # ------------------------------------------------------------------
    # Broad-index MARKET CALENDAR (price-structure Task 5)
    # ------------------------------------------------------------------
    # Consensus of the 3 broad indices (>=2-of-3 vote) — a single-index
    # source would let one vendor gap delete a real session (and a below-
    # level close on that session would vanish, fabricating a false
    # "active" hold that authorizes an entry).
    _sets = []
    for _idx in ("SPY", "QQQ", "^DJI"):
        _tr = raw.get(("index", _idx))
        _oh = _tr[1] if _tr and isinstance(_tr[1], dict) else {}
        _ds = {d for d in (_et_date_iso(ts) for ts in
                           (_anchored_ohlcv(_oh, anchor).get("timestamp") or []))
               if d is not None}
        if _ds:
            _sets.append(_ds)
    if len(_sets) == 3:
        # CONSENSUS calendar: a session is real when >=2 of 3 series carry it —
        # one vendor gap cannot delete a real session, one glitch cannot
        # invent one.
        _market_sessions = sorted(d for d in set.union(*_sets)
                                  if sum(d in s for s in _sets) >= 2)
    else:
        # Fewer than 3 sources: completeness is UNPROVEN. A 2-source "vote"
        # is an intersection — one vendor gap deletes a real session, the
        # ticker's genuine bar there gets dropped as off-calendar, and a
        # below-level close can vanish, fabricating a false `active` hold
        # that authorizes an entry. Money-path rule: no proven calendar ->
        # every structure fact unknown, entries blocked by data-integrity.
        _market_sessions = []

    # ------------------------------------------------------------------
    # Ticker prices + run-day technical indicators + closing-basis
    # price structure (price-structure Task 5)
    # ------------------------------------------------------------------
    ticker_prices = {}
    ticker_price_statuses = {}
    ticker_indicators = {}
    ticker_price_structure = {}
    _series_by_ticker = {}
    for t in tickers:
        triple = raw.get(("ticker", t))
        if triple is None:
            ticker_prices[t] = None
            ticker_price_statuses[t] = _missing_status
            ticker_indicators[t] = None
            ticker_price_structure[t] = None
            continue
        price, ohlcv, status = triple
        ticker_prices[t] = price
        if t in vendor_aliases:
            status = dict(status)  # don't mutate a possibly-shared dict
            status["vendor_symbol"] = vendor_aliases[t]
        ticker_price_statuses[t] = status
        if status.get("status") != "PASSED" or not isinstance(ohlcv, dict):
            ticker_indicators[t] = None
            ticker_price_structure[t] = None
            continue

        anchored = _anchored_ohlcv(ohlcv, anchor)

        # Closing-basis series for compute_ticker_price_structure + the
        # cohort event — exact positional alignment (merge_adjusted_closes
        # returns a bare close list, scripts/indicators.py:58; the zip
        # must be explicit so a None at index k does not shift later
        # indices' dates).
        ts = anchored.get("timestamp") or []
        closes = merge_adjusted_closes(anchored.get("close") or [],
                                       anchored.get("adjclose") or [])
        vols_arr = anchored.get("volume") or []
        pairs, vols = [], {}
        for k, tsv in enumerate(ts):
            d = _et_date_iso(tsv)
            if d is None:
                continue
            c = closes[k] if k < len(closes) else None
            if c is not None:
                pairs.append((d, c))
            v = vols_arr[k] if k < len(vols_arr) else None
            if v is not None:
                vols[d] = v
        _series_by_ticker[t] = pairs          # cohort input (assembled below)
        ticker_price_structure[t] = compute_ticker_price_structure(
            pairs, anchor_session=anchor, market_sessions=_market_sessions,
            volumes=vols, first_trade_session=anchored.get("first_trade_session"))

        # null when there are too few bars; otherwise the raw block (its
        # legs carry their own null/insufficient_data sentinels). Series
        # anchored to the last completed session (_anchored_ohlcv): the
        # indicator block's "current bar" is the last COMPLETED one, so
        # volume_ratio_vs_ma20 / price_volume_relationship read completed
        # volume, not the mid-session partial bar. `price` (the live quote)
        # is intentionally NOT anchored — display + where-is-price-now-vs-
        # established-bands semantics.
        #
        # ONE index range, mastered by the timestamp array. Only arrays
        # POSITIONALLY ALIGNED with it (equal length) are sliced by the
        # shared range; absent/short optional arrays (adjclose, volume —
        # macro.py:277 permits them) pass through so their indicator
        # legs degrade independently, exactly as today. A min() over ALL
        # lists would let one empty optional array zero the whole block
        # and kill valid RS/RSI (write-time lens refusal follows).
        _ts_len = len(anchored.get("timestamp") or [])
        _start = max(0, _ts_len - INDICATOR_SLICE_SESSIONS)
        # EVERY positional array is sliced by the SAME timestamp-mastered
        # range, each keeping its own overlap. Passing a short-but-present
        # array through unsliced would let merge_adjusted_closes
        # (indicators.py:58, index-merge over max(len)) overlay OLD
        # adjusted closes onto RECENT raw closes — corrupting RSI/MACD/
        # Bollinger/RS. An array with no overlap becomes [] (absent).
        sliced = {k: ((v[_start:_ts_len] if len(v) > _start else [])
                      if isinstance(v, list) else v)
                  for k, v in anchored.items()}
        ticker_indicators[t] = _compute_ticker_indicators(
            sliced, price, bench_returns=bench_returns)

    # Cohort recovery over ALL requested tickers (failed -> [], staying in
    # the denominator — a failed fetch must not silently shrink the universe).
    _series = {t: _series_by_ticker.get(t, []) for t in tickers}
    _event = select_cohort_event(
        _series, market_sessions=_market_sessions, anchor_session=anchor,
        max_staleness_sessions=MAX_EVENT_STALENESS_SESSIONS)
    _cohort = compute_cohort_recovery(_event, _series,
                                      market_sessions=_market_sessions)

    return {
        "market": market,
        "volatility": volatility,
        "regime_inputs": regime_inputs,
        "rates": rates,
        "ticker_prices": ticker_prices,
        "ticker_indicators": ticker_indicators,
        "ticker_price_structure": ticker_price_structure,
        "universe_rebound_structure": _cohort,
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
    parser.add_argument(
        "--vendor-aliases", default=None,
        help='JSON object {state_key: vendor_symbol} — fetch the vendor '
             'symbol, key output by the state key (feedback 2026-06-11 #5). '
             '"{}" / omitted = no aliasing.',
    )
    args = parser.parse_args()

    # Probe 3C: the portfolio SKILL substitutes {ALL_TICKERS} UNQUOTED into
    # --tickers — a glob-expanded `*` (AGENTS.md, CHANGELOG.md, …) or a
    # word-split "BRK B" would silently fetch the wrong symbol set and the
    # real ticker would simply lack a price downstream. Validate every arg
    # against the canonical symbology and fail LOUDLY pre-fetch.
    from scripts.cli_utils import normalize_ticker
    canonical_tickers = []
    for raw_t in args.tickers:
        try:
            canonical_tickers.append(normalize_ticker(raw_t))
        except ValueError as exc:
            parser.error(
                f"--tickers argument {raw_t!r} is not a valid ticker "
                f"({exc}). If this came from portfolio-state.yaml, fix that "
                "entry; a glob/word-split artifact means the orchestration "
                "substituted an unquoted list."
            )
    args.tickers = canonical_tickers

    vendor_aliases = {}
    if args.vendor_aliases not in (None, ""):
        # User-editable money-path config: any shape other than a
        # dict[str, non-empty str] fails CLOSED — a silently-partial alias
        # map fetches the wrong symbol (the exact failure this flag fixes).
        try:
            vendor_aliases = json.loads(args.vendor_aliases)
        except ValueError as exc:
            parser.error(f"--vendor-aliases is not valid JSON: {exc}")
        if not isinstance(vendor_aliases, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip()
            for k, v in vendor_aliases.items()
        ):
            parser.error(
                "--vendor-aliases must be a JSON object mapping state keys "
                "to non-empty vendor symbol strings")
        # Cold-round finding: --tickers args are canonicalized below, so the
        # alias KEYS must canonicalize identically or a lowercase key
        # silently bypasses its alias (wrong-listing fetch). Values are
        # vendor symbols (foreign suffixes allowed) — left as-is.
        canon_aliases = {}
        for k, v in vendor_aliases.items():
            try:
                ck = normalize_ticker(k)
            except ValueError as exc:
                parser.error(
                    f"--vendor-aliases key {k!r} is not a valid ticker: {exc}")
            if ck in canon_aliases and canon_aliases[ck] != v.strip():
                parser.error(
                    f"--vendor-aliases keys collide after normalization "
                    f"({k!r} → {ck!r}) with conflicting vendors")
            canon_aliases[ck] = v.strip()
        vendor_aliases = canon_aliases

    result = fetch_macro_snapshot(tickers=args.tickers,
                                  vendor_aliases=vendor_aliases)

    from scripts.cli_utils import write_output
    write_output(result, args.output)


if __name__ == "__main__":
    _main()
