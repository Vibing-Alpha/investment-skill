"""Stock screener — discover tickers by price action, sector, market cap.

Architecture:
  1. Universe selection (1-3 FMP calls):
       scope=market, window=1d        → /gainers + /losers + /actives (dedupe)
       scope=market, window=5d/1m/3m  → /stock-screener (broad, no sector filter)
       scope=sector:NAME              → /stock-screener?sector=NAME
       scope=watchlist:PATH           → read file (no API call)
  2. OHLCV enrichment via yfinance batch download (no FMP quota burned).
  3. Per-ticker metrics computed locally with scripts/indicators.py:
       change_{1d,5d,1m,3m}_pct, volume_ratio_vs_ma20, RSI(14), MACD state, BB position.
  4. Filter (min_price_usd / min_volume / min_mcap_usd) → rank by --window → top N.
  5. Emit JSON + Markdown to --output-prefix.{json,md}.

Default threshold rationale (single source of truth — SKILL.md points here):

  --min-price (5.0 USD)
      Drops OTC / halt-resume / sub-$5 pumps that can swing 100%+ on no
      fundamentals. Empirically the FMP /gainers list below $5 is dominated
      by penny noise (e.g., a recent run surfaced ZSPC +2400%, KITT +700%).

  --min-volume (500_000 shares)
      Liquidity floor. Anything below is slippage-prohibitive for normal
      orders and more susceptible to paint-the-tape patterns.

  --min-mcap-usd (300_000_000)
      Baseline noise floor. This value is the right default for scope=market
      (where /gainers is unfiltered). For scope=sector the caller typically
      raises it one order of magnitude ($3B-$10B) — sector screens are
      about mid/large caps worth analyzing. The raise is an orchestration
      decision, not hardcoded here, so the script stays scope-agnostic.

  Universe seeding for scope=market (window-gated)
      window=1d uses /gainers + /losers + /actives (dedupe ~100-150 tickers) —
      today's movers ARE the 1d signal, and it is cheap. Multi-day windows
      (5d/1m/3m) instead use a broad /stock-screener query (no sector filter,
      min-mcap floor, capped at BROAD_MARKET_LIMIT), then rank by the window
      return — because a today's-movers seed misses names that ran over the
      window but are flat/down today. If the broad query hits the cap, the run
      records warnings.universe_truncated rather than silently dropping the tail.

Valid scope=sector names: FMP_SECTORS constant below (GICS-11). Unknown
names fail fast with the full valid list echoed to stderr, so callers do
not need to memorize them.

CLI usage::

    # Market-wide daily gainers
    python3 -m scripts.screen --scope market --window 1d --top 20 \
        --output-prefix reports/screen/YYYYMMDD/market_1d

    # Sector screen with higher mcap floor
    python3 -m scripts.screen --scope sector:Technology --window 5d \
        --min-mcap-usd <scope_appropriate_floor> --top 20 \
        --output-prefix reports/screen/YYYYMMDD/tech_5d

    # Personal watchlist, today's losers
    python3 -m scripts.screen --scope watchlist:watchlist.txt \
        --window 1d --direction down \
        --output-prefix reports/screen/YYYYMMDD/watch_1d

Exit codes: 0 success, 1 data failure, 2 config error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

# Trigger .env auto-load (populates FMP_API_KEY among others)
from scripts.sources import common as _sources_common  # noqa: F401
from scripts.sources.common import (
    FMP_POLICY,
    HttpTransportError,
    ResponseTooLargeError,
    RetryExhaustedError,
    SsrfBlockedError,
    http_get,
)
from scripts.sources.yfinance_guard import yfinance_call
from scripts.indicators import (
    calc_bollinger,
    calc_macd,
    calc_rsi,
    calc_volume,
)


FMP_BASE = "https://financialmodelingprep.com/api/v3"
FMP_SECTORS = {
    "Technology", "Consumer Cyclical", "Healthcare", "Communication Services",
    "Financial Services", "Industrials", "Consumer Defensive", "Energy",
    "Basic Materials", "Utilities", "Real Estate",
}

WINDOW_TO_TRADING_DAYS = {"1d": 1, "5d": 5, "1m": 21, "3m": 63}

# /stock-screener result caps. Sector scope is naturally bounded by the sector,
# so 500 covers it. The broad market universe (scope=market, multi-day windows)
# spans the whole filtered market, so it gets a higher cap — the breadth is the
# whole point of routing multi-day windows here instead of today's ~130 movers.
# yfinance batch download (threads=True) handles ~1000 symbols; the min-mcap
# floor does the real bounding. If the screener returns exactly the cap, the run
# records `warnings.universe_truncated` so smaller-cap multi-day leaders below
# the cap are not silently dropped.
SECTOR_LIMIT = 500
BROAD_MARKET_LIMIT = 1000


# ---------------------------------------------------------------------------
# Universe selection
# ---------------------------------------------------------------------------

def _fmp_key() -> str:
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        print("FATAL: FMP_API_KEY not set in environment or .env", file=sys.stderr)
        sys.exit(2)
    return key


def _fmp_get(path: str, **params) -> list:
    """GET a FMP endpoint with API-key scrubbing and FMP-specific error-body check.

    Retry + 429/5xx handling + timeout = http_get(FMP_POLICY).
    We retain caller-side handling for:
    - FMP's "error" JSON body convention (data.get("error"))
    - API-key scrub in RuntimeError messages
    """
    key = _fmp_key()
    params["apikey"] = key
    query = urlencode(params)
    url = f"{FMP_BASE}/{path.lstrip('/')}?{query}"
    # Preserve prior 4-attempt retry envelope (was [1,2,4] + final attempt = 4).
    # FMP_POLICY defaults to max_retries=3 for general use; this caller historically
    # needed one more attempt to survive free-tier 429 bursts.
    _fmp_call_policy = replace(FMP_POLICY, max_retries=4)

    def _scrub(s: str) -> str:
        """Replace raw API key with *** in any string before it's logged or
        surfaced via an exception. Applied uniformly across every exit path
        so a key accidentally embedded in exception text / body / URL cannot
        leak to stderr or logs (M2 — Codex post-impl finding).
        """
        return s.replace(key, "***") if key else s

    try:
        resp = http_get(url, policy=_fmp_call_policy)
    except RetryExhaustedError as e:
        raise RuntimeError(f"FMP HTTP error on {path}: {_scrub(str(e))}") from None
    except HttpTransportError as e:
        raise RuntimeError(f"FMP connection error on {path}: {_scrub(str(e))}") from None
    except ResponseTooLargeError as e:
        # M2: exception message includes full URL (with ?apikey=...) from http_get.
        # MUST scrub before rethrowing or the key leaks into stderr.
        raise RuntimeError(f"FMP response too large on {path}: {_scrub(str(e))}") from None
    except SsrfBlockedError as e:
        # Unlikely (FMP_BASE is an allowed public host), but scrub for defense.
        raise RuntimeError(f"FMP SSRF blocked on {path}: {_scrub(str(e))}") from None

    if resp.status >= 400:
        body_snippet = _scrub(resp.body.decode("utf-8", errors="replace")[:500])
        raise RuntimeError(
            f"FMP HTTP error on {path}: HTTP {resp.status} — {body_snippet}"
        ) from None

    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        # FMP error envelope may echo the request URL or key — scrub just in case.
        raise RuntimeError(f"FMP error on {path}: {_scrub(str(data['error']))}")
    return data if isinstance(data, list) else []


def _universe_market() -> Tuple[List[Dict], Dict[str, str]]:
    """Dedupe /gainers + /losers + /actives. Returns (universe, source_status).

    source_status is {endpoint: "ok" | "failed: reason"} so the caller can
    record which of the three endpoints contributed to today's universe.
    A partial-success run (1 of 3 endpoints returning) produces a universe
    that is NOT comparable to a full run — recording this prevents the delta
    layer from treating degraded output as canonical.
    """
    seen: Dict[str, Dict] = {}
    status: Dict[str, str] = {}
    for path in ("stock_market/gainers", "stock_market/losers", "stock_market/actives"):
        try:
            rows = _fmp_get(path)
            status[path] = "ok"
            for row in rows:
                sym = row.get("symbol")
                if sym and sym not in seen:
                    seen[sym] = {"symbol": sym, "name": row.get("name", "")}
        except Exception as e:
            status[path] = f"failed: {e}"
            print(f"WARN: FMP {path} failed: {e}", file=sys.stderr)
    return list(seen.values()), status


def _stock_screener_universe(*, sector: Optional[str], min_mcap_usd: int,
                             limit: int) -> List[Dict]:
    """Shared FMP /stock-screener universe builder.

    `sector=None` → broad market universe (no sector filter); a sector name →
    that sector. Single implementation so the sector and broad-market paths
    cannot drift in their filters (per producer-consumer rule). Returns
    list of {symbol, name, sector, industry, market_cap_usd}.
    """
    params: Dict[str, object] = {
        "marketCapMoreThan": int(min_mcap_usd),
        "isEtf": "false",
        "isFund": "false",
        "isActivelyTrading": "true",
        "limit": limit,
    }
    if sector is not None:
        params["sector"] = sector
    rows = _fmp_get("stock-screener", **params)
    return [
        {
            "symbol": r["symbol"],
            "name": r.get("companyName", ""),
            "sector": r.get("sector") or (sector or ""),
            "industry": r.get("industry", ""),
            "market_cap_usd": r.get("marketCap"),
        }
        for r in rows
        if r.get("symbol")
    ]


def _universe_sector(sector: str, min_mcap_usd: int) -> List[Dict]:
    """FMP /stock-screener scoped to one GICS sector."""
    if sector not in FMP_SECTORS:
        print(f"FATAL: unknown sector '{sector}'. Valid: {sorted(FMP_SECTORS)}", file=sys.stderr)
        sys.exit(2)
    return _stock_screener_universe(sector=sector, min_mcap_usd=min_mcap_usd,
                                    limit=SECTOR_LIMIT)


def _universe_market_broad(min_mcap_usd: int, limit: int) -> List[Dict]:
    """Broad market universe via /stock-screener (no sector filter).

    Used for scope=market on multi-day windows (5d/1m/3m). The today's-movers
    seed (/gainers+/losers+/actives) only contains stocks moving TODAY, so a
    multi-day "biggest winners" rank computed off it is blind to names that ran
    over the window but are flat/down today. This broad universe — the same
    endpoint sector scope uses, minus the sector filter — is then ranked by the
    window return downstream, which is the correct way to find multi-day leaders.
    """
    return _stock_screener_universe(sector=None, min_mcap_usd=min_mcap_usd,
                                    limit=limit)


def _market_universe_strategy(window: str) -> str:
    """Which market-scope universe to build for a given window.

    `1d` → today's movers (/gainers+/losers+/actives): cheap, and the day's
    movers literally ARE the 1d signal. Multi-day windows → `broad`: today's
    movers would miss a name that ran over the window but isn't moving today.
    """
    return "movers" if window == "1d" else "broad"


import re as _re

# Accepts: AAPL, BRK.B, BRK-B, BF-B, TEST1. Rejects: $AAPL, NVDA;, ../etc,
# unicode, anything over 10 chars. Pinned because a permissive filter lets
# malicious watchlist lines flow into MD reports / JSON output (display
# injection) or into API URLs via batching. Tighter than isalpha() (which
# rejects BRK-B) but safer than no filter (which YAML branch had).
_TICKER_RE = _re.compile(r"^[A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?$")


def _universe_watchlist(path: str) -> List[Dict]:
    """Read tickers from file. Supports newline-separated text or YAML list.

    Security: resolves symlinks and rejects them to prevent a symlink pointing
    at /etc/passwd or ~/.ssh/id_rsa from flowing into process memory. Even
    though the regex filter would drop the content, a future maintainer who
    logs the raw text would create an exfiltration path.
    """
    p = Path(path)
    if not p.is_file():
        print(f"FATAL: watchlist file not found: {path}", file=sys.stderr)
        sys.exit(2)
    if p.is_symlink():
        print(f"FATAL: watchlist path is a symlink: {path} → {p.resolve()}. "
              f"Symlinks rejected to prevent reading sensitive files. Copy the "
              f"target into place instead.", file=sys.stderr)
        sys.exit(2)
    text = p.read_text(encoding="utf-8")

    raw_tickers: List[str] = []
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
            if isinstance(data, list):
                raw_tickers = [str(t).strip().upper() for t in data if t]
            elif isinstance(data, dict):
                # support holdings-style: {AAPL: {...}, ...}
                raw_tickers = [str(k).strip().upper() for k in data.keys()]
        except ImportError:
            print("FATAL: yaml module required for .yaml watchlist", file=sys.stderr)
            sys.exit(2)
    else:
        for line in text.splitlines():
            t = line.split("#", 1)[0].strip().upper().lstrip("$")
            if t:
                raw_tickers.append(t)

    # Apply the same regex gate to both branches — previously only text branch
    # was filtered, YAML branch let arbitrary strings through.
    rejected: List[str] = []
    accepted: List[str] = []
    for t in raw_tickers:
        if _TICKER_RE.match(t):
            accepted.append(t)
        else:
            rejected.append(t)
    if rejected:
        print(f"WARN: {len(rejected)} watchlist entries rejected (invalid "
              f"ticker format): {rejected[:5]}"
              f"{'...' if len(rejected) > 5 else ''}", file=sys.stderr)

    # dedupe preserving order
    seen = set()
    out = []
    for t in accepted:
        if t not in seen:
            seen.add(t)
            out.append({"symbol": t, "name": ""})
    return out


def _batch_quote(symbols: List[str]) -> Tuple[Dict[str, Dict], List[str]]:
    """FMP batch quote — fills mcap/volume for universe from /gainers etc.

    Returns ({symbol: quote_dict}, missing_symbols). A silently-empty chunk
    (network hiccup, 429 after retries) would otherwise cause downstream
    `_filter` to drop rows for "missing mcap" — invisible failure mode. The
    missing list lets main() record it in the output JSON.
    """
    if not symbols:
        return {}, []
    out: Dict[str, Dict] = {}
    missing: List[str] = []
    for i in range(0, len(symbols), 100):
        chunk = symbols[i : i + 100]
        joined = quote(",".join(chunk), safe=",")
        try:
            for r in _fmp_get(f"quote/{joined}"):
                sym = r.get("symbol")
                if sym:
                    out[sym] = r
        except Exception as e:
            print(f"WARN: FMP batch quote failed for chunk {i}: {e}", file=sys.stderr)
            missing.extend(chunk)
            continue
        # Also record symbols the API returned nothing for (ticker
        # delisted / symbol changed since /gainers captured it).
        for sym in chunk:
            if sym not in out:
                missing.append(sym)
    return out, missing


def _batch_profile(symbols: List[str]) -> Tuple[Dict[str, Dict], List[str]]:
    """FMP batch profile — fills sector/industry for scope=market ranked survivors.

    /gainers + /quote both omit sector; /profile is the only endpoint that
    returns it in a batch-friendly way (accepts comma-separated symbols).
    Called AFTER ranking + truncation so we only pay for top-N, not the
    full universe.

    Returns ({symbol: {sector, industry}}, missing_symbols).
    """
    if not symbols:
        return {}, []
    out: Dict[str, Dict] = {}
    missing: List[str] = []
    for i in range(0, len(symbols), 100):
        chunk = symbols[i : i + 100]
        joined = quote(",".join(chunk), safe=",")
        try:
            for r in _fmp_get(f"profile/{joined}"):
                sym = r.get("symbol")
                if sym:
                    out[sym] = {
                        "sector": r.get("sector", ""),
                        "industry": r.get("industry", ""),
                    }
        except Exception as e:
            print(f"WARN: FMP batch profile failed for chunk {i}: {e}", file=sys.stderr)
            missing.extend(chunk)
            continue
        for sym in chunk:
            if sym not in out:
                missing.append(sym)
    return out, missing




# ---------------------------------------------------------------------------
# Enrichment + metric computation
# ---------------------------------------------------------------------------

def _bulk_ohlcv(symbols: List[str], period: str = "3mo") -> Dict[str, Dict[str, List[float]]]:
    """yfinance batch download. Returns {symbol: {close: [...], volume: [...], high: [...], low: [...]}}.

    Arrays are oldest-first to match scripts/indicators.py convention.
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        print("FATAL: yfinance required (pip install yfinance)", file=sys.stderr)
        sys.exit(2)

    # Single-ticker case: yfinance returns a flat DataFrame
    df = yfinance_call(lambda: yf.download(
        symbols, period=period, interval="1d",
        progress=False, auto_adjust=True, group_by="ticker", threads=True,
    ))
    out: Dict[str, Dict[str, List[float]]] = {}
    if df.empty:
        return out

    # All four arrays MUST be aligned bar-for-bar. Dropping NaN independently
    # per column produces different lengths when yfinance emits sparse rows,
    # which means `volumes[-1]` is no longer the same trading day as
    # `closes[-1]` — the technical-indicator layer silently uses a price from
    # Tuesday with volume from Friday. Drop on close (the anchor) and forward-
    # fill OHLC siblings. Volume stays None on missing bars — the previous
    # `fillna(0)` wrote phantom zero-volume days that polluted every MA20/
    # MA5/OBV downstream. calc_volume has pair-filtering that drops None
    # bars cleanly.
    def _align(sub) -> Dict[str, List]:
        aligned = sub[["Close", "High", "Low", "Volume"]].dropna(subset=["Close"])
        if aligned.empty:
            return {}
        # ffill/bfill on H/L because indicators compute multi-bar windows
        # that need contiguous values; ATR tolerates imputed H/L better than
        # a gap. Volume stays sparse — caller handles None.
        aligned = aligned.assign(
            High=aligned["High"].ffill().bfill(),
            Low=aligned["Low"].ffill().bfill(),
        )
        return {
            "close": [float(v) for v in aligned["Close"].tolist()],
            "high": [float(v) for v in aligned["High"].tolist()],
            "low": [float(v) for v in aligned["Low"].tolist()],
            "volume": [
                None if (v is None or (isinstance(v, float) and math.isnan(v)))
                else int(v)
                for v in aligned["Volume"].tolist()
            ],
        }

    if len(symbols) == 1:
        sym = symbols[0]
        rec = _align(df)
        if rec:
            out[sym] = rec
        return out

    # Multi-ticker: columns are MultiIndex (ticker, field)
    for sym in symbols:
        try:
            sub = df[sym]
        except (KeyError, AttributeError):
            continue
        rec = _align(sub)
        if rec:
            out[sym] = rec
    return out


def _pct(current: float, baseline: float) -> Optional[float]:
    if baseline is None or current is None:
        return None
    if not math.isfinite(baseline) or baseline == 0:
        return None
    return round((current / baseline - 1.0) * 100.0, 2)


def _returns_from_closes(closes: List[float]) -> Dict[str, Optional[float]]:
    """Compute 1d/5d/1m/3m percent returns. Arrays oldest-first."""
    if not closes:
        return {f"change_{w}_pct": None for w in WINDOW_TO_TRADING_DAYS}
    cur = closes[-1]
    out = {}
    for window, days in WINDOW_TO_TRADING_DAYS.items():
        idx = -1 - days
        baseline = closes[idx] if len(closes) > days else None
        out[f"change_{window}_pct"] = _pct(cur, baseline)
    return out


def _avg_daily_dollar_volume(
    closes: List[float], volumes: List[float], window: int = 20
) -> Optional[float]:
    """Average daily dollar volume (ADDV) over the last `window` bars where
    BOTH close and volume are present; None when no usable pair exists.

    ADDV is the liquidity primitive `--min-dollar-volume` floors on — the
    correct scalar for mid/small-cap swing targets, vs the share-volume
    `--min-volume` floor which mis-ranks a $6 name above a $400 one.
    [Calc: mean(close*volume) over <=20 most-recent paired bars]
    """
    pairs = [(c, v) for c, v in zip(closes, volumes) if c is not None and v is not None]
    if not pairs:
        return None
    recent = pairs[-window:]
    return sum(c * v for c, v in recent) / len(recent)


def _compute_metrics(
    symbol: str,
    ohlcv: Dict[str, List[float]],
    quote_row: Optional[Dict],
    universe_row: Optional[Dict],
    include_tech: bool,
) -> Optional[Dict]:
    closes = ohlcv.get("close", [])
    highs = ohlcv.get("high", [])
    lows = ohlcv.get("low", [])
    volumes = ohlcv.get("volume", [])
    if len(closes) < 2:
        return None

    returns = _returns_from_closes(closes)
    cur_price = closes[-1]
    # volumes[-1] can be None when yfinance returned NaN for the latest bar —
    # happens near market open before daily volume accumulates. Don't coerce
    # to 0 here; `_filter` and MD renderer handle None explicitly.
    cur_volume = volumes[-1] if volumes else None
    vol_metrics = calc_volume(volumes, closes) if volumes else {}
    addv = _avg_daily_dollar_volume(closes, volumes)

    row: Dict = {
        "ticker": symbol,
        "company_name": (quote_row or {}).get("name") or (universe_row or {}).get("name") or "",
        "sector": (universe_row or {}).get("sector") or (quote_row or {}).get("sector", ""),
        "industry": (universe_row or {}).get("industry", ""),
        "price_usd": round(cur_price, 2),
        "market_cap_usd": (quote_row or {}).get("marketCap") or (universe_row or {}).get("market_cap_usd"),
        "volume": int(cur_volume) if cur_volume is not None else None,
        "volume_ratio_vs_ma20": vol_metrics.get("volume_ratio_vs_ma20"),
        "avg_daily_dollar_volume_usd": round(addv, 2) if addv is not None else None,
        **returns,
    }

    if include_tech:
        rsi = calc_rsi(closes, period=14)
        macd = calc_macd(closes)
        bb = calc_bollinger(closes, current_price=cur_price)
        row["rsi_14"] = rsi.get("rsi")
        row["macd_state"] = _macd_state(macd)
        row["bb_position"] = bb.get("position")
        row["flags"] = _flags(row)

    row["brief"] = _brief(row, include_tech)
    return row


def _macd_state(macd: Dict) -> str:
    # `not macd_line` would fire on macd_line == 0.0 — precisely the moment
    # of a zero-cross, which is the most actionable MACD state. Must check
    # for missing data explicitly (calc_macd returns None on invalid input).
    if macd.get("macd_line") is None:
        return "n/a"
    cx = macd.get("crossover", "none")
    zero = macd.get("zero_side", "below")
    if cx in ("golden", "bullish"):
        return f"bullish_cross({zero})"
    if cx in ("death", "bearish"):
        return f"bearish_cross({zero})"
    trend = macd.get("hist_trend", "flat")
    return f"{trend}({zero})"


def _is_illiquid_stub(row: Dict) -> bool:
    """Flag a zombie listing: a ticker that "exists" but does not really trade.

    The signature is a frozen price — 0.0% change across the medium AND long
    windows — paired with missing/zero volume. A live name drifts: it cannot
    be flat over both 5d and 1m, so two flat long windows is the near-certain
    tell. We require >=2 *present* flat windows so a short-history IPO (only a
    5d number, legitimately ~0) isn't mislabeled, and gate on volume so one
    quiet low-volume session doesn't trip it.

    Most common real instance: a foreign issuer's barely-traded US OTC ADR
    (e.g. YAGOY for Yageo) whose home-market line (2327.TW) holds the real
    liquidity. Surfacing it tells the reader to use the home line — the US
    ticker's RSI/MACD are meaningless on a frozen series, and its 0% return
    would otherwise just sort harmlessly to the bottom and be mistaken for a
    laggard rather than a non-tradeable stub.

    Known limits (deliberate, to keep false positives near zero):
    - Keys off run-day volume being absent/zero, so a zombie that printed a
      few shares THAT day is missed. We accept the miss rather than add a
      tunable low-volume threshold (which would risk flagging real, quiet
      names). The flat-across-5d/1m/3m half of the signal is the strong one.
    - A real listing frozen for 3+ months (a long halt / suspension) also
      matches — which is fine: that is itself a non-tradeable stub regardless
      of cause. The flag asserts "stale + no volume", not specifically "ADR".
    - Only computed when `_flags` runs, i.e. under `--tech` (see _compute_metrics).
    """
    vol = row.get("volume")
    if vol not in (None, 0):
        return False
    longs = [row.get("change_5d_pct"), row.get("change_1m_pct"), row.get("change_3m_pct")]
    present = [c for c in longs if c is not None]
    return len(present) >= 2 and all(c == 0.0 for c in present)


def _flags(row: Dict) -> List[str]:
    flags = []
    # Lead with the data-quality flag: a zombie listing must be read before any
    # momentum flag, since RSI/MACD/Bollinger are noise on a frozen series.
    if _is_illiquid_stub(row):
        flags.append("illiquid_stub")
    rsi = row.get("rsi_14")
    if rsi is not None:
        if rsi >= 70:
            flags.append("overbought")
        elif rsi <= 30:
            flags.append("oversold")
    vr = row.get("volume_ratio_vs_ma20")
    # BUG-1-pattern: `if vr and ...` fires only on non-zero truthy; a
    # legitimate 0.0 volume ratio (halt-resume bar) would be silently treated
    # as missing. Distinguish None from zero.
    if vr is not None and vr >= 2.0:
        flags.append("volume_spike")
    if row.get("bb_position") == "upper_band":
        flags.append("at_upper_band")
    elif row.get("bb_position") == "lower_band":
        flags.append("at_lower_band")
    return flags


def _brief(row: Dict, tech: bool) -> str:
    parts = []
    # Lead with a relevance prefix if this ticker has strong personalization
    # or delta signal — this is what jumps out on scan.
    tags = row.get("tags") or []
    prefix_tags = [t for t in tags if t in ("held", "watchlist", "new_today")
                   or t.startswith("streak_")]
    if prefix_tags:
        parts.append("[" + ",".join(prefix_tags) + "]")
    sector = row.get("sector") or ""
    industry = row.get("industry") or ""
    if industry:
        parts.append(industry)
    elif sector:
        parts.append(sector)
    mcap = row.get("market_cap_usd")
    # Distinguish None (unknown) from 0 (legitimate post-IPO / pink sheets).
    # `if mcap:` collapses both — user can't tell "we don't know" from "it's $0".
    if mcap is not None and mcap > 0:
        if mcap >= 1e12:
            parts.append(f"${mcap/1e12:.1f}T mcap")
        elif mcap >= 1e9:
            parts.append(f"${mcap/1e9:.0f}B mcap")
        else:
            parts.append(f"${mcap/1e6:.0f}M mcap")
    c1 = row.get("change_1d_pct")
    c5 = row.get("change_5d_pct")
    vr = row.get("volume_ratio_vs_ma20")
    if c1 is not None:
        move = f"{c1:+.1f}% 1d"
        if c5 is not None:
            move += f" / {c5:+.1f}% 5d"
        if vr is not None and vr >= 1.5:
            move += f" on {vr:.1f}x vol"
        parts.append(move)
    if tech:
        rsi = row.get("rsi_14")
        if rsi is not None:
            parts.append(f"RSI {rsi:.0f}")
        flags = row.get("flags") or []
        if flags:
            parts.append("[" + ",".join(flags) + "]")
    # Theme tag goes at the end (weaker signal than held/watchlist)
    theme_tags = [t for t in tags if t.startswith("theme:")]
    if theme_tags:
        parts.append(theme_tags[0])
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Filter + rank + output
# ---------------------------------------------------------------------------

def _filter(
    rows: List[Dict],
    min_price_usd: float,
    min_volume: int,
    min_mcap_usd: int,
    min_dollar_volume_usd: float = 0.0,
) -> List[Dict]:
    out = []
    for r in rows:
        if r["price_usd"] < min_price_usd:
            continue
        # Volume=None means yfinance didn't return a value for the latest
        # bar. Fail-closed: unknown volume can't pass a liquidity floor.
        vol = r.get("volume")
        if vol is None and min_volume > 0:
            continue
        if vol is not None and vol < min_volume:
            continue
        # Dollar-volume floor (WS-2B). OFF by default (0.0) so legacy callers
        # are unchanged. Fail-closed on None, same as the share-volume floor.
        if min_dollar_volume_usd > 0:
            addv = r.get("avg_daily_dollar_volume_usd")
            if addv is None or addv < min_dollar_volume_usd:
                continue
        mcap = r.get("market_cap_usd")
        if min_mcap_usd > 0 and (mcap is None or mcap < min_mcap_usd):
            continue
        out.append(r)
    return out


def _rank(rows: List[Dict], window: str, direction: str) -> List[Dict]:
    key = f"change_{window}_pct"
    def sort_key(r):
        v = r.get(key)
        return v if v is not None else (-1e9 if direction == "up" else 1e9)
    reverse = (direction == "up")
    return sorted(rows, key=sort_key, reverse=reverse)


# ---------------------------------------------------------------------------
# Delta layer — "what changed vs prior runs"
#
# Canonical prior-run discovery walks reports/screen/{YYYYMMDD}/*.json,
# matching on a scope fingerprint stored inside each file. This decouples
# delta from the user-chosen --output-prefix path: the fingerprint is the
# truth, the filename is cosmetic.
# ---------------------------------------------------------------------------

SCREEN_REPORTS_ROOT = Path("reports/screen")


def _scope_fingerprint(scope: str, window: str, direction: str,
                       universe: Optional[List[Dict]] = None) -> str:
    """Stable string for comparing runs. Filters (mcap/price/vol) intentionally
    omitted — a caller tuning thresholds still compares against past runs of
    the same scope/window/direction.

    For scope=watchlist, the raw scope string includes the file path, which
    changes when the user re-pastes a watchlist to /tmp/<random>.txt each
    session. That breaks delta even when the ticker set is unchanged. We
    canonicalize watchlist fingerprints to a content hash of the sorted
    ticker list so 'same tickers, different file' still diffs correctly.
    """
    if scope.startswith("watchlist:") and universe:
        import hashlib
        tickers = sorted({u.get("symbol", "").upper() for u in universe if u.get("symbol")})
        # 12 hex chars = 48 bits → birthday collision at ~16M distinct
        # watchlists, safely out of reach. 8 chars (32 bits) had a ~65k
        # birthday boundary which, combined with same-day re-run ambiguity,
        # could have merged two unrelated deltas silently.
        digest = hashlib.md5("|".join(tickers).encode()).hexdigest()[:12]
        return f"watchlist:wl_{digest}__{window}__{direction}"
    return f"{scope}__{window}__{direction}"


def _list_prior_runs(fingerprint: str, before_date: str, lookback_days: int = 10) -> List[Path]:
    """Return JSON paths of prior runs with matching fingerprint, newest first.

    Walks reports/screen/{YYYYMMDD}/ dirs with date < before_date. Lookback caps
    how many days back we scan — enough for streak detection without pulling
    disk for ancient history.
    """
    if not SCREEN_REPORTS_ROOT.is_dir():
        return []
    date_dirs = sorted(
        (d for d in SCREEN_REPORTS_ROOT.iterdir() if d.is_dir() and d.name.isdigit() and len(d.name) == 8),
        reverse=True,
    )
    out: List[Path] = []
    # Cap the CALENDAR-day horizon, not the match count. A counter that
    # advances only on matches would walk arbitrarily far back in mixed-
    # scope environments where most dirs contain other fingerprints —
    # inflating streak counts with stale matches from months ago.
    scanned_days = 0
    for d in date_dirs:
        if d.name >= before_date.replace("-", ""):
            continue
        if scanned_days >= lookback_days:
            break
        scanned_days += 1
        matched = None
        # Prefer the NEWEST fingerprint-matching file in the dir. Alphabetical
        # order (the previous default) would pick `gainers_1d.json` over
        # `market_1d.json` even if the latter was rerun later the same day,
        # producing a stale prior. mtime descending matches author intent.
        for jf in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("scope_fingerprint") == fingerprint:
                    matched = jf
                    break
            except Exception:
                continue
        if matched is not None:
            out.append(matched)
    return out


def _compute_delta(today_tickers: List[str], prior_path: Optional[Path]) -> Dict:
    """new / dropped / sustained based on the most recent prior run.

    Defensive against: None path, unreadable file, non-JSON content, JSON
    that parses to non-dict (list/string/null), dict missing `results`,
    results containing non-dict rows. All of these fall through to the
    "no usable prior" branch — this is the right behavior because the
    alternative is either crashing or inflating `new` vs `sustained` with
    bogus tickers pulled from malformed data.
    """
    empty = {"prior_date": None, "new": today_tickers[:], "dropped": [], "sustained": []}
    if prior_path is None:
        return empty
    try:
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(prior, dict):
        return empty
    results = prior.get("results", []) or []
    prior_tickers = [
        r.get("ticker") for r in results
        if isinstance(r, dict) and r.get("ticker")
    ]
    today_set = set(today_tickers)
    prior_set = set(prior_tickers)
    return {
        "prior_date": prior.get("run_date"),
        "new": [t for t in today_tickers if t not in prior_set],
        "dropped": [t for t in prior_tickers if t not in today_set],
        "sustained": [t for t in today_tickers if t in prior_set],
    }


def _compute_streaks(today_tickers: List[str], fingerprint: str, today_date: str,
                     lookback_days: int = 10) -> Dict[str, int]:
    """For each ticker, how many consecutive prior runs it appeared in.

    Streak is only meaningful for CONSECUTIVE appearances — once a ticker
    misses OR the prior's data is unverifiable (corrupt JSON, missing file,
    no matching fingerprint for that day), the streak resets. Today's run
    counts as day 1 by convention.

    We do a calendar-day walk rather than iterating a pre-filtered priors
    list, because "day present in list" ≠ "day had valid data". If a corrupt
    JSON on day-2 is silently dropped, day-1 and day-3 would appear
    consecutive to the caller — which inflates the streak by bridging a gap
    that should have broken it. Fail-closed per producer-consumer rule §4.
    """
    streaks: Dict[str, int] = {t: 1 for t in today_tickers}
    if not SCREEN_REPORTS_ROOT.is_dir():
        return streaks

    date_dirs = sorted(
        (d for d in SCREEN_REPORTS_ROOT.iterdir()
         if d.is_dir() and d.name.isdigit() and len(d.name) == 8),
        reverse=True,
    )
    before_key = today_date.replace("-", "")
    still_streaking = set(today_tickers)

    scanned_days = 0
    for d in date_dirs:
        if d.name >= before_key:
            continue
        if scanned_days >= lookback_days or not still_streaking:
            break
        scanned_days += 1

        # Find today's fingerprint-matching file in this dir, if any.
        # Newest-mtime first so a same-day re-run's result wins over a stale
        # earlier run — matches _list_prior_runs selection.
        prior_set: Optional[set] = None
        for jf in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                # Unparseable — unknown data, break entire streak chain
                prior_set = None
                break
            if not isinstance(data, dict):
                continue
            if data.get("scope_fingerprint") != fingerprint:
                continue
            results = data.get("results", []) or []
            prior_set = {r.get("ticker") for r in results if isinstance(r, dict) and r.get("ticker")}
            break

        if prior_set is None:
            # No valid match for this day — fail-closed, reset all streaks.
            # Applies to: day had no screener output, output didn't match
            # fingerprint, JSON was corrupt, or file unreadable.
            still_streaking.clear()
            break

        fell_off = still_streaking - prior_set
        for t in still_streaking & prior_set:
            streaks[t] += 1
        still_streaking -= fell_off

    return streaks


# ---------------------------------------------------------------------------
# Personalization layer — read strategy.yaml + portfolio-state.yaml
#
# Both files are optional (gitignored, per-user). If absent, personalization
# is a no-op and the screen still works. This is fail-open by design: a user
# without a strategy still gets useful output, just without the tags.
# ---------------------------------------------------------------------------

# Loose keyword map from user-written themes to industry/sector substrings.
# Users write "AI" in strategy.yaml; FMP returns "Semiconductors" or "Software -
# Application" — this bridges the vocabulary gap without asking the user to
# memorize GICS taxonomy. Extend freely; unmatched themes simply don't tag.
_THEME_KEYWORDS = {
    "ai": ["semiconductor", "software", "computer hardware", "artificial intelligence"],
    "semiconductors": ["semiconductor"],
    "semis": ["semiconductor"],
    "cloud": ["software - infrastructure", "information technology services"],
    "saas": ["software - application", "software - infrastructure"],
    "software": ["software"],
    "space": ["aerospace", "defense"],
    "space_defense": ["aerospace", "defense"],
    "defense": ["aerospace", "defense"],
    "biotech": ["biotechnology", "drug manufacturers"],
    "healthcare": ["healthcare", "medical", "biotechnology"],
    "fintech": ["financial", "credit services", "capital markets"],
    "energy": ["energy", "oil", "solar"],
    "power": ["utilities", "electrical equipment", "renewable utilities"],
    "power_infrastructure": ["utilities", "electrical equipment", "industrial distribution"],
    "utilities": ["utilities"],
    "consumer": ["consumer"],
    "ev": ["auto manufacturers", "auto parts"],
    "crypto": ["capital markets", "financial data"],
}
# Warn-once registry so noise stays bounded across many rows per run
_UNKNOWN_THEMES_WARNED: set = set()  # noqa: audit-fail-open — reset per-test via tests/conftest.py _reset_screen_module_state


def _load_personalization() -> Dict:
    """Read strategy.yaml + portfolio-state.yaml from project root.

    Returns {holdings: set, watchlist: set, themes: list[str], loaded: bool,
             errors: list[str]}. loaded=False if neither file was found —
    caller uses this to suppress personalization-related output entirely.
    errors is non-empty when a file EXISTS but failed to parse; the caller
    surfaces these into the output JSON so orchestrators see broken config
    instead of silently thinking "no themes matched".
    """
    result: Dict = {
        "holdings": set(), "watchlist": set(),
        "themes": [], "loaded": False, "errors": [],
    }
    try:
        import yaml  # type: ignore
    except ImportError:
        return result

    # Root detection anchors on __file__, not cwd — running
    # `python3 -m scripts.screen` from /tmp is a valid use case and must
    # still find strategy.yaml. Mirrors scripts/sources/common._find_project_root.
    here = Path(__file__).resolve().parent
    for path_guess in [here, *here.parents]:
        if (path_guess / "CLAUDE.md").is_file() or (path_guess / ".git").exists():
            root = path_guess
            break
    else:
        return result

    strat_path = root / "strategy.yaml"
    port_path = root / "portfolio-state.yaml"

    if strat_path.is_file():
        try:
            s = yaml.safe_load(strat_path.read_text(encoding="utf-8")) or {}
            mandate = (s.get("mandate") or {})
            edges = mandate.get("edge") or []
            if isinstance(edges, list):
                result["themes"] = [str(e).strip().lower() for e in edges if e]
            result["loaded"] = True
        except Exception as e:
            msg = f"strategy.yaml parse failed: {e}"
            print(f"WARN: {msg}", file=sys.stderr)
            result["errors"].append(msg)

    if port_path.is_file():
        try:
            p = yaml.safe_load(port_path.read_text(encoding="utf-8")) or {}
            holdings = p.get("holdings") or {}
            if isinstance(holdings, dict):
                result["holdings"] = {str(k).upper() for k in holdings.keys()}
            watchlist = p.get("watchlist") or []
            if isinstance(watchlist, list):
                result["watchlist"] = {str(w).strip().upper() for w in watchlist if w}
            result["loaded"] = True
        except Exception as e:
            msg = f"portfolio-state.yaml parse failed: {e}"
            print(f"WARN: {msg}", file=sys.stderr)
            result["errors"].append(msg)

    return result


def _theme_match(industry: str, sector: str, themes: List[str]) -> Optional[str]:
    """Return the first matching theme, or None. Matches are substring-based
    against industry and sector (FMP-provided). Lowercased for robustness.

    Unknown themes (not in _THEME_KEYWORDS) log a one-time warning so operators
    notice when their strategy.yaml uses a token that won't match GICS vocab.
    Silent miss is the failure mode we want to avoid — compound tokens like
    `power_infrastructure` will never substring-match `"Utilities"`.
    """
    if not themes:
        return None
    hay = f"{industry} {sector}".lower()
    for theme in themes:
        keys = _THEME_KEYWORDS.get(theme)
        if keys is None:
            if theme not in _UNKNOWN_THEMES_WARNED:
                _UNKNOWN_THEMES_WARNED.add(theme)
                print(
                    f"WARN: theme '{theme}' not in _THEME_KEYWORDS — substring "
                    f"match on raw token rarely hits GICS vocab. Add an entry "
                    f"to _THEME_KEYWORDS in scripts/screen.py if you want this "
                    f"theme to tag results.",
                    file=sys.stderr,
                )
            keys = [theme]
        if any(k in hay for k in keys):
            return theme
    return None


def _tag_row(row: Dict, perz: Dict, delta: Dict, streaks: Dict[str, int]) -> List[str]:
    """Compute the tag list for a result row. Order-stable for reader scanability.

    `held` subsumes `watchlist` — a position you own is already on your
    watchlist by definition, and tagging both would double-count in the
    attention scorer (held=10 + watchlist=8 = 18 vs clean held=10). We emit
    only the stronger signal.
    """
    tags: List[str] = []
    t = row["ticker"]
    if t in perz["holdings"]:
        tags.append("held")
    elif t in perz["watchlist"]:
        tags.append("watchlist")
    matched = _theme_match(row.get("industry", ""), row.get("sector", ""), perz["themes"])
    if matched:
        tags.append(f"theme:{matched}")
    # Delta tags (only when there WAS a prior run — otherwise 'new' is
    # meaningless since every ticker is "new" by default)
    if delta.get("prior_date"):
        if t in set(delta.get("new", [])):
            tags.append("new_today")
        streak = streaks.get(t, 1)
        if streak >= 3:
            tags.append(f"streak_{streak}d")
    return tags


def _pick_attention(rows: List[Dict], perz_loaded: bool) -> List[str]:
    """The 'what should I actually look at' shortlist.

    Scoring: more tags from the personalization set = higher relevance.
    held/watchlist are strongest signals (user already cares). theme match
    is weaker alone but reinforces. new_today + theme combo catches fresh
    entrants into an area the user tracks. Long streaks catch stable
    leaders even without personalization.

    We only emit attention when either (a) the user has strategy/portfolio
    files loaded (personalization is meaningful), or (b) there are
    genuine multi-day streaks (≥3) worth surfacing. Otherwise we stay
    silent — don't manufacture signal.
    """
    scored = []
    for r in rows:
        tags = r.get("tags") or []
        score = 0
        if "held" in tags:
            score += 10
        if "watchlist" in tags:
            score += 8
        if any(t.startswith("theme:") for t in tags):
            score += 3
        if "new_today" in tags:
            score += 2
        streak_tags = [t for t in tags if t.startswith("streak_")]
        if streak_tags:
            try:
                streak_n = int(streak_tags[0].split("_")[1].rstrip("d"))
                score += min(streak_n, 5)
            except (IndexError, ValueError):
                pass
        if score > 0:
            scored.append((score, r["ticker"]))
    scored.sort(reverse=True)
    # Keep at most 5; drop if there's no personalization AND score is just
    # theme+new (weak without user context)
    return [t for _, t in scored[:5]]


def _emit_json(result: Dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def _emit_markdown(result: Dict, path: Path, include_tech: bool) -> None:
    rows = result["results"]
    scope = result["scope"]
    window = result["window"]
    direction = result["direction"]
    filters = result["filters"]
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append(f"# Screen — {result['run_date']}")
    lines.append("")
    lines.append(f"- **Scope**: `{scope}`")
    lines.append(f"- **Ranked by**: `change_{window}_pct` ({direction})")
    lines.append(f"- **Filters**: price ≥ ${filters['min_price_usd']}, volume ≥ {filters['min_volume']:,}, mcap ≥ ${filters['min_mcap_usd']:,}")
    if filters.get("min_dollar_volume_usd"):
        lines.append(f"- **Min $-volume**: ≥ ${filters['min_dollar_volume_usd']:,.0f}/day (ADDV)")
    lines.append(f"- **Universe**: {result['universe_size']} → **Survivors**: {result['survivors']} → **Top**: {len(rows)}")
    lines.append("")

    # Attention section — surface the 1-5 tickers that combine personalization
    # + delta signals. Human eyes land here first, before scanning the full table.
    attention = result.get("attention") or []
    if attention:
        lines.append("## Attention")
        lines.append("")
        # Render each attention ticker as a one-liner with its reason + a
        # trimmed brief (no leading tag prefix, since we show tags as the reason).
        results_idx = {r["ticker"]: r for r in rows}
        for t in attention:
            r = results_idx.get(t)
            if not r:
                continue
            tags = r.get("tags") or []
            why = ", ".join(tags) if tags else "high rank"
            brief = r.get("brief", "")
            # Trim leading [tags]. from brief since tags are shown as reason
            if brief.startswith("["):
                after = brief.split(".", 1)
                brief = after[1].strip() if len(after) > 1 else brief
            lines.append(f"- **{t}** — {why}. {brief}")
        lines.append("")

    # Delta section — only render when there was a prior run. First-time
    # screens have nothing to diff against, so no section is shown.
    delta = result.get("delta") or {}
    if delta.get("prior_date"):
        lines.append(f"## Delta vs {delta['prior_date']}")
        lines.append("")
        for label, key in [("New", "new"), ("Dropped", "dropped"), ("Sustained", "sustained")]:
            items = delta.get(key) or []
            if items:
                lines.append(f"- **{label}** ({len(items)}): {', '.join(items)}")
        lines.append("")

    if not rows:
        lines.append("_No tickers passed filters._")
    else:
        hdr = ["#", "Ticker", "1d %", "5d %", "1m %", "Price", "MCap", "Vol×20d"]
        if include_tech:
            hdr += ["RSI", "MACD", "BB", "Flags"]
        hdr.append("Brief")
        lines.append("| " + " | ".join(hdr) + " |")
        lines.append("|" + "|".join(["---"] * len(hdr)) + "|")
        for i, r in enumerate(rows, 1):
            mcap = r.get("market_cap_usd")
            # Same falsiness-on-zero fix as _brief: `mcap and mcap >= X` treats
            # 0 as missing; distinguish None from zero explicitly.
            if mcap is None or mcap <= 0:
                mcap_str = "—"
            elif mcap >= 1e12:
                mcap_str = f"${mcap/1e12:.1f}T"
            elif mcap >= 1e9:
                mcap_str = f"${mcap/1e9:.0f}B"
            else:
                mcap_str = f"${mcap/1e6:.0f}M"
            vr = r.get("volume_ratio_vs_ma20")
            cells = [
                str(i), r["ticker"],
                _fmt_pct(r.get("change_1d_pct")),
                _fmt_pct(r.get("change_5d_pct")),
                _fmt_pct(r.get("change_1m_pct")),
                f"${r['price_usd']:.2f}",
                mcap_str,
                f"{vr:.1f}x" if vr is not None else "—",
            ]
            if include_tech:
                rsi = r.get("rsi_14")
                cells += [
                    f"{rsi:.0f}" if rsi is not None else "—",
                    r.get("macd_state", "—"),
                    r.get("bb_position", "—"),
                    ",".join(r.get("flags") or []) or "—",
                ]
            cells.append(r.get("brief", ""))
            lines.append("| " + " | ".join(cells) + " |")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _positive_int(x: str) -> int:
    v = int(x)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be positive, got {v}")
    return v


def _nonneg_float(x: str) -> float:
    v = float(x)
    if v < 0 or not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"must be non-negative and finite, got {v}")
    return v


def _nonneg_int(x: str) -> int:
    v = int(x)
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative, got {v}")
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="Stock screener by price action / sector / watchlist.")
    ap.add_argument("--scope", required=True, help="market | sector:NAME | watchlist:PATH")
    ap.add_argument("--window", choices=list(WINDOW_TO_TRADING_DAYS), default="1d",
                    help="Ranking window (1d/5d/1m/3m). Default: 1d")
    ap.add_argument("--direction", choices=["up", "down"], default="up",
                    help="Rank by biggest gains (up) or biggest losses (down). Default: up")
    ap.add_argument("--top", type=_positive_int, default=20, help="Return top N tickers. Default: 20")
    ap.add_argument("--min-price", dest="min_price_usd", type=_nonneg_float, default=5.0,
                    help="Minimum price USD. Default: 5.0")
    ap.add_argument("--min-volume", type=_nonneg_int, default=500_000, help="Minimum daily volume. Default: 500k")
    ap.add_argument("--min-dollar-volume", dest="min_dollar_volume_usd", type=_nonneg_float, default=0.0,
                    help="Minimum 20d avg daily dollar volume (USD). Default: 0 (off; share-volume --min-volume still applies).")
    ap.add_argument("--min-mcap-usd", type=_nonneg_int, default=300_000_000,
                    help="Minimum market cap USD. Default: 300M. Use 0 to disable.")
    ap.add_argument("--tech", action="store_true",
                    help="Include RSI/MACD/Bollinger indicators + flags.")
    ap.add_argument("--output-prefix", required=True,
                    help="Output path prefix; writes <prefix>.json + <prefix>.md")
    args = ap.parse_args()

    t0 = time.time()
    scope = args.scope.strip()
    # Collects issues surfaced during data gathering so they appear in the
    # output JSON (`warnings` field). Silent partial-success would otherwise
    # let the delta layer treat a degraded run as canonical.
    warnings: Dict[str, object] = {}

    # 1. Universe
    universe_sources: Dict[str, str] = {}
    if scope == "market":
        if _market_universe_strategy(args.window) == "movers":
            universe, universe_sources = _universe_market()
            if all(s != "ok" for s in universe_sources.values()):
                print("FATAL: all FMP universe endpoints failed", file=sys.stderr)
                return 1
            if any(s != "ok" for s in universe_sources.values()):
                warnings["universe_sources"] = universe_sources
        else:
            # Multi-day window: broad /stock-screener universe, not today's
            # movers (which would miss multi-day leaders not moving today).
            universe = _universe_market_broad(args.min_mcap_usd, BROAD_MARKET_LIMIT)
            if len(universe) >= BROAD_MARKET_LIMIT:
                warnings["universe_truncated"] = (
                    f"broad market universe hit the {BROAD_MARKET_LIMIT}-row "
                    f"/stock-screener cap; multi-day leaders below "
                    f"min-mcap-usd={args.min_mcap_usd} or beyond the cap may be "
                    f"missed. Raise --min-mcap-usd to tighten, or screen by sector."
                )
    elif scope.startswith("sector:"):
        sector_name = scope[len("sector:"):].strip()
        if not sector_name:
            print("FATAL: --scope sector: requires a name, e.g. sector:Technology",
                  file=sys.stderr)
            return 2
        universe = _universe_sector(sector_name, args.min_mcap_usd)
    elif scope.startswith("watchlist:"):
        wl_path = scope[len("watchlist:"):].strip()
        if not wl_path:
            print("FATAL: --scope watchlist: requires a path, e.g. watchlist:my.txt",
                  file=sys.stderr)
            return 2
        universe = _universe_watchlist(wl_path)
    else:
        print(f"FATAL: --scope must be 'market' | 'sector:NAME' | 'watchlist:PATH', got '{scope}'", file=sys.stderr)
        return 2

    if not universe:
        print("FATAL: empty universe, nothing to screen", file=sys.stderr)
        return 1
    print(f"[screen] universe: {len(universe)} tickers from scope={scope}", file=sys.stderr)

    symbols = [u["symbol"] for u in universe]
    universe_idx = {u["symbol"]: u for u in universe}

    # 2. FMP batch quote for mcap/volume/sector (skip if already present from /stock-screener)
    needs_quote = any(u.get("market_cap_usd") is None for u in universe)
    quote_rows: Dict[str, Dict] = {}
    if needs_quote:
        quote_rows, quote_missing = _batch_quote(symbols)
        if quote_missing:
            warnings["quote_missing"] = quote_missing

    # 3. yfinance bulk OHLCV
    ohlcv = _bulk_ohlcv(symbols, period="3mo")
    ohlcv_missing = [s for s in symbols if s not in ohlcv]
    if ohlcv_missing:
        warnings["ohlcv_missing"] = ohlcv_missing
        print(f"WARN: OHLCV missing for {len(ohlcv_missing)} tickers: "
              f"{ohlcv_missing[:5]}{'...' if len(ohlcv_missing) > 5 else ''}",
              file=sys.stderr)
    print(f"[screen] OHLCV fetched for {len(ohlcv)}/{len(symbols)} tickers in {time.time()-t0:.1f}s",
          file=sys.stderr)

    # 4. Compute metrics
    metrics: List[Dict] = []
    for sym in symbols:
        data = ohlcv.get(sym)
        if not data:
            continue
        row = _compute_metrics(
            sym, data,
            quote_row=quote_rows.get(sym),
            universe_row=universe_idx.get(sym),
            include_tech=args.tech,
        )
        if row:
            metrics.append(row)

    # 5. Filter + rank + truncate
    survivors = _filter(metrics, args.min_price_usd, args.min_volume, args.min_mcap_usd,
                        min_dollar_volume_usd=args.min_dollar_volume_usd)
    ranked = _rank(survivors, args.window, args.direction)
    top = ranked[: args.top]

    # 5b. Backfill sector/industry for scope=market top-N. The /gainers
    # universe path yields no sector info and /quote doesn't return it
    # either; /profile does, and is batch-friendly. Only call for top-N
    # (1 extra FMP call, cheap) so we don't waste quota on rows that
    # won't be shown. scope=sector already has sector from /stock-screener;
    # scope=watchlist skips this since the user knows what they gave us.
    missing_sector = [r for r in top if not (r.get("sector") or "").strip()]
    if scope == "market" and missing_sector:
        profile, profile_missing = _batch_profile([r["ticker"] for r in missing_sector])
        if profile_missing:
            warnings["profile_missing"] = profile_missing
        for r in missing_sector:
            p = profile.get(r["ticker"])
            if p:
                r["sector"] = p.get("sector") or r.get("sector", "")
                r["industry"] = p.get("industry") or r.get("industry", "")

    # 5c. Delta layer — compute new/dropped/sustained vs most recent prior
    # run of the same scope+window+direction. Streak counts consecutive
    # appearances going back up to 10 trading days.
    # Delta matches on ET calendar dates because the reports tree is
    # organized by ET session (scripts/delta/resolver.py convention).
    # Using UTC here would cause after-hours runs (past 8pm ET) to write
    # into tomorrow's directory and silently skip today's prior run.
    from scripts.delta.calendar import session_et
    today_date = session_et().strftime("%Y-%m-%d")
    fingerprint = _scope_fingerprint(scope, args.window, args.direction, universe=universe)
    today_tickers = [r["ticker"] for r in top]
    priors = _list_prior_runs(fingerprint, today_date, lookback_days=10)
    prior_path = priors[0] if priors else None
    delta = _compute_delta(today_tickers, prior_path)
    streaks = _compute_streaks(today_tickers, fingerprint, today_date)

    # 5d. Personalization — tag rows with held/watchlist/theme/new/streak
    # based on strategy.yaml + portfolio-state.yaml. Both are optional;
    # when absent, perz["loaded"]=False and tag lists will be empty (except
    # delta-derived tags, which are always safe).
    perz = _load_personalization()
    for r in top:
        r["tags"] = _tag_row(r, perz, delta, streaks)
        # Re-render brief now that sector + tags are finalized
        r["brief"] = _brief(r, args.tech)

    # 5e. Attention list — the "what to actually look at" shortlist
    attention = _pick_attention(top, perz["loaded"])

    result = {
        "run_date": today_date,
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": scope,
        "scope_fingerprint": fingerprint,
        "window": args.window,
        "direction": args.direction,
        "filters": {
            "min_price_usd": args.min_price_usd,
            "min_volume": args.min_volume,
            "min_dollar_volume_usd": args.min_dollar_volume_usd,
            "min_mcap_usd": args.min_mcap_usd,
        },
        "universe_size": len(universe),
        "enriched": len(metrics),
        "survivors": len(survivors),
        "delta": delta,
        "personalization": {
            "loaded": perz["loaded"],
            "themes": perz["themes"],
            "n_holdings": len(perz["holdings"]),
            "n_watchlist": len(perz["watchlist"]),
            "errors": perz.get("errors", []),
        },
        "attention": attention,
        "warnings": warnings,
        "results": top,
    }

    prefix = Path(args.output_prefix)
    _emit_json(result, prefix.with_suffix(".json"))
    _emit_markdown(result, prefix.with_suffix(".md"), include_tech=args.tech)

    print(f"[screen] wrote {prefix}.json + {prefix}.md ({len(top)} rows) in {time.time()-t0:.1f}s",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
