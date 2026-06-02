"""Yahoo Finance adapter.

Provides price/quote fetching via the Yahoo Finance v8 chart API, plus
yfinance-library wrappers for fallback financial data, metrics, company
info, and analyst estimates.

Price bars emit BOTH `close` (unadjusted) and `adjclose` (split/dividend-
adjusted). Downstream indicators and historical multiples should read
`adjclose` with fallback to `close`.

The ``_find_portfolio_root()`` helper walks up the directory tree looking for
``portfolio.yaml`` or ``.claude/`` to locate the project root
(no hard-coded parent traversal depth).
"""

import functools
import json
import math
import os
import re
import sys
import time as _time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from scripts.constants import YAHOO_BASE_URL
from .common import (
    is_us_country,
    http_get,
    YAHOO_CHART_POLICY,
    ResponseTooLargeError,
    SsrfBlockedError,
    RetryExhaustedError,
    HttpTransportError,
    HttpStatusError,
    safe_http_get_json,
    sanitize_dict_numerics,
    normalize_currency,
)
from .yfinance_guard import yfinance_call

# DL2 imports — MODULE-TOP (DL1 v2.9 lesson: inline imports break mock.patch)
# v21 correction (Codex round 16 H1): prior import line only exposed
# AdapterResult + ErrorCode; T8 template calls adapter_error_from_exception
# (helper from T1 v20), and T9 partial/failed paths construct AdapterError
# directly. Import all four symbols at module top so neither NameErrors at
# runtime.
from scripts.sources.adapter_result import (
    AdapterResult,
    AdapterError,
    ErrorCode,
    adapter_error_from_exception,
)
from scripts.sources.api_shapes import (
    validate_api_shape,
    YAHOO_CHART_SHAPE,
    YAHOO_HISTORICAL_OHLCV_SHAPE,
    ShapeError,
)


# ---------------------------------------------------------------------------
# yfinance import guard
# ---------------------------------------------------------------------------

try:
    import yfinance as yf
    import pandas as pd
    HAS_YFINANCE = True
except ImportError:
    yf = None  # type: ignore[assignment]
    pd = None  # type: ignore[assignment]
    HAS_YFINANCE = False


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------

# ISS-141 (Loop12 cycle 1): _is_bool_like promoted to common.is_bool_like
# so financial_datasets.py can share the same numpy-bool defense without
# duplicating drift-prone code. Local alias kept for back-compat with the
# 4 existing callsites; new callers should import from common directly.
from scripts.sources.common import is_bool_like as _is_bool_like  # noqa: E402


def _find_portfolio_root() -> str:
    """Walk up the directory tree to find the project root.

    Looks for a directory containing ``portfolio.yaml`` or a ``.claude/``
    subdirectory.  Starts from this file's location and walks towards the
    filesystem root.

    Returns the path as a string, or None if not found.
    """
    current = Path(__file__).resolve().parent
    # Walk up at most 20 levels (safety limit)
    for _ in range(20):
        if (current / "portfolio.yaml").exists():
            return str(current)
        if (current / ".claude").is_dir():
            return str(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ---------------------------------------------------------------------------
# Native ticker (ADR) loading from portfolio.yaml
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def load_native_ticker(ticker: str) -> Tuple[str, str]:  # adapter-helper-ok: fill helper; DL3 decides wrap/internalize
    """Load native_ticker and native_currency mapping from portfolio.yaml.

    For ADRs like TTDKY, returns ("6762.T", "JPY").
    Returns ("", "") if no mapping found.

    Results are cached (lru_cache) to avoid re-reading portfolio.yaml on
    every call.
    """
    root = _find_portfolio_root()
    if root is None:
        return "", ""

    portfolio_path = Path(root) / "portfolio.yaml"
    if not portfolio_path.exists():
        return "", ""

    try:
        content = portfolio_path.read_text(encoding="utf-8")
        lines = content.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped in (
                f"ticker: {ticker}", f'ticker: "{ticker}"',
                f"- ticker: {ticker}", f'- ticker: "{ticker}"',
            ):
                native_t = ""
                native_c = ""
                for j in range(i + 1, min(i + 16, len(lines))):
                    next_line = lines[j].strip()
                    if next_line.startswith("- ticker:"):
                        break
                    m_t = re.match(
                        r'native_ticker:\s*["\']?([^"\'#\s]+)', next_line,
                    )
                    if m_t:
                        native_t = m_t.group(1)
                    m_c = re.match(
                        r'native_currency:\s*["\']?([^"\'#\s]+)', next_line,
                    )
                    if m_c:
                        native_c = m_c.group(1)
                if native_t:
                    # DL3a §2 invariant 3 — normalize portfolio.yaml currency
                    # regex extract so a user typo (`"jpy"`, `"USD "`) lands
                    # in run_meta as a verified ISO code or sentinel, never
                    # raw. Preserves "" for "no mapping" semantics.
                    if native_c:
                        native_c = normalize_currency(native_c) or "UNKNOWN"
                    return native_t, native_c
        return "", ""
    except Exception as e:
        print(
            f"[WARNING] Failed to load portfolio.yaml for native_ticker: {e}",
            file=sys.stderr,
        )
        return "", ""


# ---------------------------------------------------------------------------
# Yahoo Finance v8 chart API
# ---------------------------------------------------------------------------

# ISS-220 4.15 (Loop34 cycle 1): typed exceptions for Yahoo upstream
# signals — distinguished from generic JSON parse failures so the
# canonical mapper routes them to NOT_FOUND / UPSTREAM_ERROR instead
# of PARSE_ERROR. Both inherit ValueError so existing
# `except ValueError` callers remain compatible.
class YahooNoDataError(ValueError):
    """Yahoo returned an empty `chart.result`. Maps to NOT_FOUND.
    Not retryable: re-issuing the same request will not produce data."""


class YahooApiError(ValueError):
    """Yahoo returned a non-empty `chart.error` envelope. Maps to
    UPSTREAM_ERROR (vendor-acknowledged failure)."""


def _yahoo_ticker(ticker: str) -> str:
    """Convert ticker to Yahoo Finance format.

    Yahoo uses hyphens for share classes (MOG-A, BRK-B) while most
    financial data APIs use periods (MOG.A, BRK.B).
    """
    return ticker.replace(".", "-")


# ISS-048 + ISS-064 (Loop3/4 backlog): centralized yfinance exception
# message sanitizer. yfinance internal errors can carry:
#   - Session cookie/crumb headers
#   - Cache file absolute paths (e.g. /home/USER/.cache/py-yfinance/...)
#   - URL with auth/query params
#   - Custom-session-injected API key headers
# Apply to ALL yfinance fallback prints (8 sites) and the outer error
# field, replacing the prior fragmented per-site `print(f"... {e}")`
# raw text and the env-only scrub at the outer site.

# ISS-078 (Loop6): broaden auth/secret regex.
# Pre-fix only matched bare `Header: value` form. Real yfinance
# exception messages use Python dict-repr (`{'Authorization':
# 'Bearer abc'}`), URL query (`access_token=abc`), or plain
# assignment (`apikey="abc"`). Quote-aware key + greedy-until-separator
# value.
# ISS-220 SF-D / 4.12 (Loop33 cycle 1): _AUTH_HEADER_PAT + _HOME_PATH_PAT +
# the yfinance-specific scrub logic moved to common._yfinance_scrub. The
# `_yfinance_safe_msg` wrapper here remains as a thin composer for legacy
# stderr-print callers that need both yfinance-aware scrub AND env-key
# scrub at the same call site (the canonical mapper handles this
# composition itself for envelope.error.detail; print sites bypass the
# mapper).


def _company_text_present(value: object) -> bool:
    """Return True iff `value` is a non-empty string after `.strip()`.

    ISS-220 Logic-1 (iter9 inline → iter11 helper per superpowers review):
    yfinance returns `description=""` on scrape miss; the empty string is
    semantically vacuous and must NOT count as "field present" for status
    classification. Defensive `isinstance(str)` covers non-string drift
    (None / NaN float / list / dict) without AttributeError.

    Used by `_run_yfinance_fallback_impl` company-info status gate.
    """
    return isinstance(value, str) and bool(value.strip())


def _yfinance_safe_msg(exc: BaseException) -> str:
    """Sanitize a yfinance exception's str() for stderr-print persistence.

    Composes:
      - yfinance-specific scrub (cookies / crumbs / auth headers / cache
        home paths) via `common._yfinance_scrub`
      - env API key scrub (FINANCIAL_DATASETS_API_KEY / FINNHUB_API_KEY /
        FMP_API_KEY) via `common._scrub_detail`
      - 400-char truncation
    """
    from scripts.sources.common import _yfinance_scrub, _scrub_detail
    secrets = tuple(
        os.environ.get(k, "")
        for k in ("FINANCIAL_DATASETS_API_KEY", "FINNHUB_API_KEY", "FMP_API_KEY")
    )
    scrubbed = _yfinance_scrub(str(exc))
    scrubbed = _scrub_detail(scrubbed, secrets)
    return scrubbed[:400]


def fetch_yahoo_quote(
    ticker: str,
    range_param: str = "1d",
    interval: str = "1d",
) -> Dict:
    """Fetch data from Yahoo Finance v8 chart API.

    Returns raw chart result dict or raises Exception.
    Retries are handled by http_get (YAHOO_CHART_POLICY).
    """
    yahoo_sym = _yahoo_ticker(ticker)
    safe_sym = urllib.parse.quote(yahoo_sym, safe='')
    # ISS-037 (Loop2): urlencode the query params. Pre-fix, range_param /
    # interval interpolated as raw f-string. While current callers pass
    # internal literals ("1d", "5d", "1y" etc), the entrypoint signature
    # accepts arbitrary str — `range_param="1d&interval=1m"` would inject
    # extra Yahoo query params and pollute envelope.data. Defense-in-depth.
    query = urllib.parse.urlencode({
        "range": range_param,
        "interval": interval,
        "includePrePost": "false",
    })
    url = f"{YAHOO_BASE_URL}/{safe_sym}?{query}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36"
        ),
        "Accept": "application/json",
    }
    # Outer retry covers parse/semantic failures that http_get cannot see
    # (JSONDecodeError on HTTP 200 bodies, {chart.error} Yahoo error envelope,
    # empty-result responses). Pre-DL1 code retried on any Exception 3 times;
    # DL1 http_get policy retry only covers transport+status. Two extra
    # attempts on top of policy-level retries preserve the old retry envelope
    # without double-retrying transport failures (which http_get already
    # handles internally).
    last_error: Optional[Exception] = None
    for attempt in range(3):
        # ISS-200 (Loop29 cycle 1 fresh-session-16): include JSON parse
        # failures in the semantic retry. Pre-fix `safe_http_get_json`
        # was outside the inner try, so a JSONDecodeError on a 200 body
        # (Yahoo intermittent garbled response) escaped immediately
        # without retrying — defeating the docstring's "Two extra
        # attempts on top of policy-level retries preserve the old
        # retry envelope" promise. Move the call inside the try and
        # let parse failures land in the semantic-retry path alongside
        # the chart-error / empty-result branches.
        try:
            # Structural (post Loop21): safe_http_get_json centralizes
            # status check + JSON parse. Transport-failure types
            # propagate untouched (helper does not catch them).
            try:
                data = safe_http_get_json(url, policy=YAHOO_CHART_POLICY, headers=headers)
            except (RetryExhaustedError, HttpTransportError, SsrfBlockedError, ResponseTooLargeError) as e:
                # Transport / SSRF / size are non-retryable here —
                # http_get already ran its policy-level retries.
                print(f"[WARNING] Yahoo Finance fetch failed: {_yfinance_safe_msg(e)}", file=sys.stderr)
                raise
            # ISS-204 (Loop30 cycle 1 fresh-session-17): guard non-dict
            # JSON root. Pre-fix `data.get("chart", {})` raised
            # AttributeError on list/scalar JSON, which fell through to
            # adapter_error_from_exception Row10 → INTERNAL_ERROR
            # (looks like an adapter bug). Yahoo upstream-drift ought to
            # surface as SHAPE_MISMATCH instead. ShapeError is mapped
            # explicitly at adapter_result.py L770 before the generic
            # ValueError row.
            if not isinstance(data, dict):
                raise ShapeError(
                    "yahoo_chart_root",
                    "data",
                    f"expected dict, got {type(data).__name__}",
                )
            chart = data.get("chart", {})
            if not isinstance(chart, dict):
                raise ShapeError(
                    "yahoo_chart_root",
                    "chart",
                    f"expected dict, got {type(chart).__name__}",
                )
            if chart.get("error"):
                # ISS-220 4.15 (Loop34 cycle 1): typed Yahoo error
                # → mapper Row 8.8 → UPSTREAM_ERROR (vs PARSE_ERROR
                # via plain ValueError pre-fix). YahooApiError inherits
                # ValueError so existing `except ValueError` callers
                # remain compatible.
                raise YahooApiError(f"Yahoo Finance error: {chart['error']}")
            results = chart.get("result", [])
            if not results:
                # ISS-220 4.15: typed no-data → mapper Row 8.7 → NOT_FOUND.
                raise YahooNoDataError("Yahoo Finance returned empty result")
            return results[0]
        except (
            RetryExhaustedError, HttpTransportError, SsrfBlockedError,
            ResponseTooLargeError,
            YahooNoDataError,  # ISS-220 4.15 (R3 reviewer): no-data is not retry-able
            ShapeError,        # R3 sub-item: malformed shape isn't fixed by retry
        ):
            raise  # let transport / no-data / shape errors propagate as before
        except Exception as e:
            last_error = e
            if attempt < 2:
                wait = 0.5 * (2 ** attempt)
                # ISS-094 (Loop7): consistent yfinance scrub even on
                # parse retry path.
                print(
                    f"[WARNING] Yahoo Finance parse/semantic attempt {attempt + 1} "
                    f"failed: {_yfinance_safe_msg(e)}. Retrying in {wait}s...",
                    file=sys.stderr,
                )
                _time.sleep(wait)
                continue
            raise last_error
    # Loop exhausted without return — unreachable (last attempt either
    # returns or raises), but satisfies the type checker.
    raise last_error if last_error else RuntimeError("Yahoo Finance fetch failed")


def fetch_yahoo_quote_result(
    ticker: str,
    range_param: str = "1d",
    interval: str = "1d",
) -> AdapterResult:
    """DL2 thin wrapper around fetch_yahoo_quote.

    fetch_yahoo_quote stays Dict-returning (shared primitive used by
    macro.py + financial_datasets.fetch_price_data). This wrapper
    adds AdapterResult envelope + YAHOO_CHART_SHAPE validation for
    DL2-aware consumers (macro.py migrated in Slice 6).
    """
    # ISS-014: source field must match the entrypoint name
    # (`yahoo_finance.fetch_yahoo_quote_result` — registered in
    # ADAPTER_ENTRYPOINTS), not the wrapped dict-returning primitive
    # `fetch_yahoo_quote`. Audit/debug correlation needs the entrypoint
    # name; preserve the wrapper-vs-primitive split via meta side-channel.
    src = "yahoo_finance.fetch_yahoo_quote_result"
    # v20: use canonical DL1→DL2 mapping helper (T1 adapter_error_from_exception).
    # Handles RetryExhaustedError(429)→RATE_LIMIT, ValueError/KeyError→PARSE_ERROR,
    # etc., per spec §Types mapping table — no per-template drift.
    try:
        raw = fetch_yahoo_quote(ticker, range_param=range_param, interval=interval)
    except Exception as e:
        return adapter_error_from_exception(e, source=src)

    v = validate_api_shape(raw, YAHOO_CHART_SHAPE)
    if not v.ok:
        return AdapterResult.failed_from_shape(v, source=src)
    return AdapterResult.passed(
        data=raw,
        meta={"source_hint": "yahoo_v8", "wrapped_primitive": "fetch_yahoo_quote"},
    )


# ---------------------------------------------------------------------------
# Historical prices
# ---------------------------------------------------------------------------

def fetch_historical_prices(
    ticker: str,
    daily_limit: int = 60,   # kept for call-site back-compat
    weekly_limit: int = 52,  # kept for call-site back-compat
) -> AdapterResult:
    """DL2-envelope return: {"result": {...}, "raw_daily_chart": {...}}
    on PASSED; .failed with mapped ErrorCode on failure.

    Note: daily_limit/weekly_limit remain vestigial (Yahoo's v8 chart
    API ranges on fixed tokens, not integer days). Arg kept for
    existing test callers; internal body still uses range_param="6mo"
    (daily) + range_param="2y" (weekly) as before.
    """
    src = "yahoo_finance.fetch_historical_prices"
    # v20 correction (Codex round 15 H1): pre-migration body uses TWO
    # independent try/except blocks (one per daily/weekly). Single
    # combined try would kill already-fetched daily data when weekly
    # raises → violates PARTIAL contract.
    result = {"daily": [], "weekly": [], "daily_count": 0, "weekly_count": 0}
    raw_daily_chart: Dict = {}
    daily_error: Exception | None = None
    weekly_error: Exception | None = None

    # ---- Daily (6mo) fetch + parse ----
    try:
        daily_chart = fetch_yahoo_quote(
            ticker, range_param="6mo", interval="1d",
        )
        # ISS-142 (Loop14 cycle 1 fresh-session): validate OHLCV shape
        # BEFORE the zip parse loop. zip() silently truncates mismatched
        # arrays — pre-fix a 3-timestamp / 1-close response returned
        # PASSED with daily_count=1, and a string close like "bad" was
        # persisted verbatim. YAHOO_HISTORICAL_OHLCV_SHAPE enforces all
        # OHLCV arrays present, parallel-length to timestamp, numeric
        # (not bool / not string) when not None. Failure raises
        # ShapeError → maps to SHAPE_MISMATCH via adapter_error_from
        # _exception (adapter_result.py L770 ShapeError row).
        ohlcv_errs = YAHOO_HISTORICAL_OHLCV_SHAPE(daily_chart)
        if ohlcv_errs:
            raise ShapeError(
                "yahoo_historical_ohlcv",
                "daily",
                "; ".join(ohlcv_errs[:3]),
            )
        raw_daily_chart = daily_chart
        timestamps = daily_chart.get("timestamp", [])
        indicators = daily_chart.get("indicators", {})
        quotes = (indicators.get("quote") or [{}])[0]
        adj_block = (indicators.get("adjclose") or [{}])[0]
        adj_closes = adj_block.get("adjclose", [])
        opens = quotes.get("open", [])
        highs = quotes.get("high", [])
        lows = quotes.get("low", [])
        closes = quotes.get("close", [])
        volumes = quotes.get("volume", [])

        for idx, (ts, o, h, l, c, v) in enumerate(zip(
            timestamps, opens, highs, lows, closes, volumes,
        )):
            if c is not None:
                # Fill partial bars — Yahoo emits None for incomplete OHLC
                o = o if o is not None else c
                h = h if h is not None else max(o, c)
                l = l if l is not None else min(o, c)
                # Missing volume stays None — writing 0 here poisons every
                # downstream MA20/MA5/OBV calculation with phantom zero-volume
                # bars. calc_volume + indicator pair-filtering drop None bars
                # cleanly; consumers that sum volumes must guard against None.
                v = v if v is not None else None  # fail-open-ok: explicit None propagation; see audit pattern N
                adjc = (
                    adj_closes[idx]
                    if idx < len(adj_closes) and adj_closes[idx] is not None
                    else c
                )
                result["daily"].append({
                    "time": datetime.fromtimestamp(
                        ts, tz=timezone.utc,
                    ).strftime("%Y-%m-%d"),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,           # unadjusted — retained for legacy consumers
                    "adjclose": adjc,     # split/dividend-adjusted — use for returns/SMA/indicators
                    "volume": v,
                })
    except Exception as e:
        daily_error = e
        # ISS-081 (Loop6): consistent yfinance scrub even on Yahoo URL
        # path (no API key in URL today, but defense-in-depth + log
        # uniformity with all other yfinance/Yahoo error sites).
        print(
            f"[WARNING] Yahoo daily prices fetch failed: {_yfinance_safe_msg(e)}",
            file=sys.stderr,
        )

    # ---- Weekly (2y) fetch + parse ----
    try:
        weekly_chart = fetch_yahoo_quote(
            ticker, range_param="2y", interval="1wk",
        )
        # ISS-142 (Loop14): same OHLCV shape validation as daily path.
        ohlcv_errs = YAHOO_HISTORICAL_OHLCV_SHAPE(weekly_chart)
        if ohlcv_errs:
            raise ShapeError(
                "yahoo_historical_ohlcv",
                "weekly",
                "; ".join(ohlcv_errs[:3]),
            )
        timestamps = weekly_chart.get("timestamp", [])
        indicators = weekly_chart.get("indicators", {})
        quotes = (indicators.get("quote") or [{}])[0]
        adj_block = (indicators.get("adjclose") or [{}])[0]
        adj_closes = adj_block.get("adjclose", [])
        opens = quotes.get("open", [])
        highs = quotes.get("high", [])
        lows = quotes.get("low", [])
        closes = quotes.get("close", [])
        volumes = quotes.get("volume", [])

        for idx, (ts, o, h, l, c, v) in enumerate(zip(
            timestamps, opens, highs, lows, closes, volumes,
        )):
            if c is not None:
                o = o if o is not None else c
                h = h if h is not None else max(o, c)
                l = l if l is not None else min(o, c)
                # Missing volume stays None — writing 0 here poisons every
                # downstream MA20/MA5/OBV calculation with phantom zero-volume
                # bars. calc_volume + indicator pair-filtering drop None bars
                # cleanly; consumers that sum volumes must guard against None.
                v = v if v is not None else None  # fail-open-ok: explicit None propagation; see audit pattern N
                adjc = (
                    adj_closes[idx]
                    if idx < len(adj_closes) and adj_closes[idx] is not None
                    else c
                )
                result["weekly"].append({
                    "time": datetime.fromtimestamp(
                        ts, tz=timezone.utc,
                    ).strftime("%Y-%m-%d"),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,           # unadjusted — retained for legacy consumers
                    "adjclose": adjc,     # split/dividend-adjusted — use for returns/SMA/indicators
                    "volume": v,
                })
    except Exception as e:
        weekly_error = e
        # ISS-081: same as daily.
        print(
            f"[WARNING] Yahoo weekly prices fetch failed: {_yfinance_safe_msg(e)}",
            file=sys.stderr,
        )

    # Hard-fail only when BOTH fetches blew up AND the errors are of
    # a class the envelope must surface specifically.
    # ISS-025 (Cycle 4): pre-fix only mapped daily_error after the
    # SSRF/size precedence, dropping weekly's potentially more severe
    # signal (e.g. daily=ValueError parse, weekly=429 RetryExhausted →
    # would have surfaced PARSE_ERROR retryable=False instead of
    # RATE_LIMIT retryable=True). Now select by severity priority:
    # SSRF/size > RATE_LIMIT > HTTP_TRANSPORT > parse > internal.
    if daily_error is not None and weekly_error is not None:
        errs = (daily_error, weekly_error)
        for e in errs:
            if isinstance(e, SsrfBlockedError):
                return AdapterResult.failed(
                    code=ErrorCode.SSRF_BLOCKED, detail=str(e)[:400],
                    source=src, cause="SsrfBlockedError",
                )
        for e in errs:
            if isinstance(e, ResponseTooLargeError):
                return AdapterResult.failed(
                    code=ErrorCode.RESPONSE_TOO_LARGE, detail=str(e)[:400],
                    source=src, cause="ResponseTooLargeError",
                )
        # ISS-074 (Loop5): use centralized severity_of_exception to keep
        # ranking consistent with financials + filing.
        from scripts.sources.adapter_result import severity_of_exception
        chosen = min(errs, key=severity_of_exception)
        return adapter_error_from_exception(chosen, source=src)

    # Summary stats (byte-equivalent with pre-migration lines 386-397)
    if result["daily"]:
        closes_list = [
            p["close"] for p in result["daily"] if p.get("close") is not None
        ]
        if len(closes_list) >= 20:
            result["sma_20"] = round(sum(closes_list[-20:]) / 20, 2)
        if len(closes_list) >= 50:
            result["sma_50"] = round(sum(closes_list[-50:]) / 50, 2)
        result["daily_count"] = len(result["daily"])

    if result["weekly"]:
        result["weekly_count"] = len(result["weekly"])

    # ISS-117 (Loop8 cycle 2): Yahoo chart JSON may contain NaN / +Inf /
    # -Inf tokens (Python's json.loads accepts those silently as
    # float('nan') / float('inf')). The OHLCV bars are appended with
    # only an `if c is not None` filter, so a NaN close passes the
    # is-not-None check and lands raw in result["daily"]. ISS-106's
    # JSON-safety guard now hard-errors at envelope construction on
    # those values. Sanitize daily/weekly bars + raw chart before
    # construction so the producer surface stays JSON-safe regardless
    # of upstream drift (mirrors ISS-105 rates emit-site coverage).
    sanitized_data = sanitize_dict_numerics(
        {"result": result, "raw_daily_chart": raw_daily_chart},
        coerce_bool=False,  # keep any legitimate bool fields (none today)
    )
    # Tri-valued status dispatch via AdapterResult (v8 correction:
    # pre-migration body dispatches PASSED / PARTIAL / FAILED on
    # daily+weekly presence; preserve that so Slice-7 byte-equivalent
    # gate holds).
    if result["daily"] and result["weekly"]:
        return AdapterResult.passed(
            data=sanitized_data,
            meta={"source_hint": "yahoo_v8",
                  "daily_limit": daily_limit,
                  "weekly_limit": weekly_limit},
        )
    if result["daily"] or result["weekly"]:
        # ISS-201 (Loop29 cycle 1 fresh-session-16): preserve the
        # actual failed-leg error code/cause/upstream_status instead
        # of the generic "one of daily/weekly chart empty"
        # UPSTREAM_ERROR. Pre-fix the partial path discarded
        # daily_error / weekly_error (they were only used in the
        # both-failed dual-error severity branch above), so a
        # daily=429 + weekly=PASSED PARTIAL surfaced as UPSTREAM_ERROR
        # retryable=True instead of RATE_LIMIT retryable=True. Same
        # canonical mapper that the dual-error branch uses, applied
        # to the surviving leg's error.
        failed_leg_err = daily_error if daily_error is not None else weekly_error
        if failed_leg_err is not None:
            partial_envelope = adapter_error_from_exception(
                failed_leg_err, source=src,
            )
            partial_error = partial_envelope.error
        else:
            partial_error = AdapterError(
                code=ErrorCode.UPSTREAM_ERROR,
                detail="one of daily/weekly chart empty",
                source=src, retryable=True,
            )
        return AdapterResult.partial(
            data=sanitized_data,
            error=partial_error,
            meta={"source_hint": "yahoo_v8",
                  "daily_limit": daily_limit,
                  "weekly_limit": weekly_limit},
        )
    return AdapterResult.failed(
        code=ErrorCode.UPSTREAM_ERROR,
        detail="both daily and weekly chart empty",
        source=src, retryable=True,
        data=sanitized_data,
        meta={"source_hint": "yahoo_v8",
              "daily_limit": daily_limit,
              "weekly_limit": weekly_limit},
    )


# ---------------------------------------------------------------------------
# yfinance fill helpers
# ---------------------------------------------------------------------------

def yfinance_fill_financial_data(  # adapter-helper-ok: fill helper; DL3 decides wrap/internalize
    yf_ticker_obj,
    existing_financials: Dict,
    info_dict: Dict = None,
) -> Tuple[Dict, Dict]:
    """Map yfinance financial data to API schema fields.

    Tries quarterly data first; if thin or empty, falls back to annual.
    Only fills data if existing data is empty (fallback, not override).

    Args:
        info_dict: Optional pre-fetched yf_ticker_obj.info dict to avoid
            redundant network calls.

    Returns: (updated_financials, summary_dict)
    """
    summary = {"income_filled": 0, "balance_filled": 0, "cashflow_filled": 0}

    # Detect financial currency (use pre-fetched info if available).
    # ISS-153 (Loop17 cycle 1 fresh-session-4): pre-fix defaulted to "USD"
    # silently when financialCurrency was missing or .info failed. For ADRs
    # with foreign financial currency (JPY/EUR/GBP/CNY/etc.), this marked
    # statements as USD when they were not — and the cross-currency
    # filter at L907 (which gates EV/FCF/P-S to USD-only) would let
    # contaminated calculations through. Now use "UNKNOWN" sentinel; the
    # cross-currency check downstream (`if fin_currency != "USD"`) treats
    # UNKNOWN as foreign and skips USD-only computations conservatively.
    try:
        _info = info_dict if info_dict is not None else yfinance_call(lambda: yf_ticker_obj.info)
        # DL3a §2 invariant 3 — route the raw yfinance value through
        # normalize_currency so lowercase / padded / non-string drift can't
        # bypass the consumer fail-close. None / non-ISO / unsupported codes
        # collapse to "UNKNOWN" per the producer convention.
        fin_currency = normalize_currency(_info.get("financialCurrency")) if _info else None
        fin_currency = fin_currency or "UNKNOWN"
    except Exception:
        fin_currency = "UNKNOWN"

    def g(series, key):
        """Get value from pandas Series, rejecting NaN / ±Inf / bool drift.

        ISS-134 (Loop10 cycle 1): pre-fix `val != val` only rejected NaN;
        `float('inf')` slipped through (`fval != fval` is False for Infinity)
        and Python bool inputs coerced to 1.0 / 0.0. Non-finite floats
        then landed in 02_financial_data.json verbatim (cli_utils
        .write_output uses default `allow_nan=True` which emits the
        literal `Infinity`), and AdapterResult JSON-safety
        (adapter_result.py L360) raises on the next downstream wrap.
        ISS-139 (Loop11 cycle 1): extend bool rejection to numpy scalar
        bools via _is_bool_like — pandas DataFrame `.iloc[]` returns
        numpy scalars, and `isinstance(np.bool_(True), bool)` is False.
        """
        if key not in series.index:
            return None
        val = series[key]
        if val is None or _is_bool_like(val):
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    def _first_not_none(*candidates):
        """ISS-206 (Loop30 cycle 1 fresh-session-17): truthiness-safe
        primary/fallback selection. `g(...) or g(...)` was the prior
        idiom but treated 0.0 (a legitimate financial value, e.g. zero
        noncontrolling interest, zero PPE purchase, zero dividends) as
        missing and silently substituted the fallback. Use explicit
        `is not None` so a reported zero stays a zero.
        """
        for c in candidates:
            if c is not None:
                return c
        return None

    def _period_info(col, is_annual):
        if is_annual:
            return "FY", "annual"
        quarter = ((col.month - 1) // 3) + 1
        return f"{col.year}-Q{quarter}", "quarterly"

    def _map_income(df, is_annual=False):
        statements = []
        for col in df.columns:
            report_period = col.strftime("%Y-%m-%d")
            s = df[col]
            fp, period = _period_info(col, is_annual)
            stmt = {
                "ticker": yf_ticker_obj.ticker,
                "report_period": report_period,
                "fiscal_period": fp,
                "period": period,
                "currency": fin_currency,
                "data_source": "yfinance",
                "revenue": g(s, "Total Revenue"),
                "cost_of_revenue": g(s, "Cost Of Revenue"),
                "gross_profit": g(s, "Gross Profit"),
                "operating_expense": g(s, "Operating Expense"),
                "selling_general_and_administrative_expenses": g(s, "Selling General And Administration"),
                "research_and_development": g(s, "Research And Development"),
                "operating_income": g(s, "Operating Income"),
                "interest_expense": g(s, "Interest Expense"),
                "ebit": g(s, "EBIT"),
                "income_tax_expense": g(s, "Tax Provision"),
                "net_income": g(s, "Net Income"),
                "net_income_common_stock": g(s, "Net Income Common Stockholders"),
                "net_income_discontinued_operations": g(s, "Net Income From Continuing And Discontinued Operation"),
                "net_income_non_controlling_interests": g(s, "Minority Interests"),
                "consolidated_income": _first_not_none(
                    g(s, "Net Income Including Noncontrolling Interests"),
                    g(s, "Net Income From Continuing Operations"),
                ),
                "preferred_dividends_impact": g(s, "Preferred Stock Dividends"),
                "dividends_per_common_share": None,
                "earnings_per_share": g(s, "Basic EPS"),
                "earnings_per_share_diluted": g(s, "Diluted EPS"),
                "weighted_average_shares": g(s, "Basic Average Shares"),
                "weighted_average_shares_diluted": g(s, "Diluted Average Shares"),
            }
            statements.append(stmt)
        return statements

    def _map_balance(df, is_annual=False):
        sheets = []
        for col in df.columns:
            report_period = col.strftime("%Y-%m-%d")
            s = df[col]
            fp, period = _period_info(col, is_annual)
            sheet = {
                "ticker": yf_ticker_obj.ticker,
                "report_period": report_period,
                "fiscal_period": fp,
                "period": period,
                "currency": fin_currency,
                "data_source": "yfinance",
                "total_assets": g(s, "Total Assets"),
                "current_assets": g(s, "Current Assets"),
                "cash_and_equivalents": g(s, "Cash And Cash Equivalents"),
                "current_investments": g(s, "Other Short Term Investments"),
                "trade_and_non_trade_receivables": g(s, "Receivables"),
                "inventory": g(s, "Inventory"),
                "non_current_assets": g(s, "Total Non Current Assets"),
                "property_plant_and_equipment": g(s, "Net PPE"),
                "goodwill_and_intangible_assets": g(s, "Goodwill And Other Intangible Assets"),
                "investments": g(s, "Investmentin Financial Assets"),
                "non_current_investments": g(s, "Other Non Current Assets"),
                "tax_assets": g(s, "Tax Assets") if "Tax Assets" in s.index else None,
                "total_liabilities": g(s, "Total Liabilities Net Minority Interest"),
                "current_liabilities": g(s, "Current Liabilities"),
                "trade_and_non_trade_payables": g(s, "Payables And Accrued Expenses"),
                "current_debt": g(s, "Current Debt"),
                "deferred_revenue": g(s, "Current Deferred Revenue"),
                "deposit_liabilities": None,
                "non_current_liabilities": g(s, "Total Non Current Liabilities Net Minority Interest"),
                "non_current_debt": g(s, "Long Term Debt"),
                "tax_liabilities": g(s, "Non Current Deferred Taxes Liabilities"),
                "total_debt": g(s, "Total Debt"),
                "outstanding_shares": g(s, "Ordinary Shares Number"),
                "shareholders_equity": g(s, "Stockholders Equity"),
                "retained_earnings": g(s, "Retained Earnings"),
                "accumulated_other_comprehensive_income": g(s, "Accumulated Other Comprehensive Income"),
            }
            sheets.append(sheet)
        return sheets

    def _map_cashflow(df, is_annual=False):
        flows = []
        for col in df.columns:
            report_period = col.strftime("%Y-%m-%d")
            s = df[col]
            fp, period = _period_info(col, is_annual)
            cfo = g(s, "Operating Cash Flow")
            capex = g(s, "Capital Expenditure")
            fcf = None
            if cfo is not None and capex is not None:
                fcf = cfo - abs(capex)
            flow = {
                "ticker": yf_ticker_obj.ticker,
                "report_period": report_period,
                "fiscal_period": fp,
                "period": period,
                "currency": fin_currency,
                "data_source": "yfinance",
                "net_cash_flow_from_operations": cfo,
                "depreciation_and_amortization": g(s, "Depreciation And Amortization"),
                "share_based_compensation": g(s, "Stock Based Compensation"),
                "net_income": g(s, "Net Income From Continuing Operations"),
                "net_cash_flow_from_investing": g(s, "Investing Cash Flow"),
                "capital_expenditure": capex,
                "business_acquisitions_and_disposals": g(s, "Net Business Purchase And Sale"),
                "investment_acquisitions_and_disposals": g(s, "Net Investment Purchase And Sale"),
                "property_plant_and_equipment": _first_not_none(
                    g(s, "Purchase Of PPE"), capex,
                ),
                "net_cash_flow_from_financing": g(s, "Financing Cash Flow"),
                "issuance_or_repayment_of_debt_securities": g(s, "Net Issuance Payments Of Debt"),
                "dividends_and_other_cash_distributions": _first_not_none(
                    g(s, "Cash Dividends Paid"),
                    g(s, "Common Stock Dividend Paid"),
                ),
                "issuance_or_purchase_of_equity_shares": g(s, "Net Common Stock Issuance"),
                "free_cash_flow": fcf,
                "change_in_cash_and_equivalents": g(s, "Changes In Cash"),
                "ending_cash_balance": g(s, "End Cash Position"),
                "effect_of_exchange_rate_changes": g(s, "Effect Of Exchange Rate Changes"),
            }
            flows.append(flow)
        return flows

    # Track if income fell back to annual
    used_annual_fallback = False

    # --- Income Statements ---
    if not existing_financials.get("income_statements"):
        try:
            statements = []
            inc = yfinance_call(lambda: yf_ticker_obj.quarterly_income_stmt)
            if inc is not None and not inc.empty:
                statements = _map_income(inc, is_annual=False)
            if not statements or (statements and statements[0].get("revenue") is None):
                if statements:
                    print("[yfinance] Quarterly income too thin (no revenue), trying annual...", file=sys.stderr)
                else:
                    print("[yfinance] Quarterly income empty, trying annual...", file=sys.stderr)
                inc_annual = yfinance_call(lambda: yf_ticker_obj.income_stmt)
                if inc_annual is not None and not inc_annual.empty:
                    statements = _map_income(inc_annual, is_annual=True)
                    used_annual_fallback = True
            if statements:
                statements_sorted = sorted(
                    statements, key=lambda r: r.get("report_period", ""), reverse=True,
                )
                existing_financials["income_statements"] = statements_sorted[:8]
                summary["income_filled"] = len(statements_sorted[:8])
        except Exception as e:
            print(f"[WARNING] yfinance income statement fill failed: {_yfinance_safe_msg(e)}", file=sys.stderr)
            # ISS-220 4.3 (Loop32 cycle 2): preserve structured error so
            # the run_meta yfinance_summary surfaces the failure cause
            # (RATE_LIMIT vs UPSTREAM_ERROR vs HTTP_TRANSPORT) per
            # statement type. Symmetric with metrics/company/analyst
            # error preservation (Loop31 ISS-219).
            envelope = adapter_error_from_exception(
                e, source="yahoo_finance.yfinance_fill_financial_data/income",
            )
            if envelope.error is not None:
                summary["income_error"] = {
                    "code": envelope.error.code.value,
                    "detail": envelope.error.detail,
                    "cause": envelope.error.cause,
                }

    # --- Balance Sheets ---
    if not existing_financials.get("balance_sheets"):
        try:
            sheets = []
            if used_annual_fallback:
                print("[yfinance] Using annual balance sheets (matching income period)...", file=sys.stderr)
                bs_annual = yfinance_call(lambda: yf_ticker_obj.balance_sheet)
                if bs_annual is not None and not bs_annual.empty:
                    sheets = _map_balance(bs_annual, is_annual=True)
            else:
                bs = yfinance_call(lambda: yf_ticker_obj.quarterly_balance_sheet)
                if bs is not None and not bs.empty:
                    sheets = _map_balance(bs, is_annual=False)
                if not sheets:
                    print("[yfinance] Quarterly balance sheet empty, trying annual...", file=sys.stderr)
                    bs_annual = yfinance_call(lambda: yf_ticker_obj.balance_sheet)
                    if bs_annual is not None and not bs_annual.empty:
                        sheets = _map_balance(bs_annual, is_annual=True)
            if sheets:
                sheets_sorted = sorted(
                    sheets, key=lambda r: r.get("report_period", ""), reverse=True,
                )
                existing_financials["balance_sheets"] = sheets_sorted[:8]
                summary["balance_filled"] = len(sheets_sorted[:8])
        except Exception as e:
            print(f"[WARNING] yfinance balance sheet fill failed: {_yfinance_safe_msg(e)}", file=sys.stderr)
            # ISS-220 4.3: structured error preservation (see income block).
            envelope = adapter_error_from_exception(
                e, source="yahoo_finance.yfinance_fill_financial_data/balance",
            )
            if envelope.error is not None:
                summary["balance_error"] = {
                    "code": envelope.error.code.value,
                    "detail": envelope.error.detail,
                    "cause": envelope.error.cause,
                }

    # --- Cash Flow Statements ---
    if not existing_financials.get("cash_flows"):
        try:
            flows = []
            if used_annual_fallback:
                print("[yfinance] Using annual cashflow (matching income period)...", file=sys.stderr)
                cf_annual = yfinance_call(lambda: yf_ticker_obj.cashflow)
                if cf_annual is not None and not cf_annual.empty:
                    flows = _map_cashflow(cf_annual, is_annual=True)
            else:
                cf = yfinance_call(lambda: yf_ticker_obj.quarterly_cashflow)
                if cf is not None and not cf.empty:
                    flows = _map_cashflow(cf, is_annual=False)
                if not flows:
                    print("[yfinance] Quarterly cashflow empty, trying annual...", file=sys.stderr)
                    cf_annual = yfinance_call(lambda: yf_ticker_obj.cashflow)
                    if cf_annual is not None and not cf_annual.empty:
                        flows = _map_cashflow(cf_annual, is_annual=True)
            if flows:
                flows_sorted = sorted(
                    flows, key=lambda r: r.get("report_period", ""), reverse=True,
                )
                existing_financials["cash_flows"] = flows_sorted[:8]
                summary["cashflow_filled"] = len(flows_sorted[:8])
        except Exception as e:
            print(f"[WARNING] yfinance cash flow fill failed: {_yfinance_safe_msg(e)}", file=sys.stderr)
            # ISS-220 4.3: structured error preservation (see income block).
            envelope = adapter_error_from_exception(
                e, source="yahoo_finance.yfinance_fill_financial_data/cashflow",
            )
            if envelope.error is not None:
                summary["cashflow_error"] = {
                    "code": envelope.error.code.value,
                    "detail": envelope.error.detail,
                    "cause": envelope.error.cause,
                }

    return existing_financials, summary


def yfinance_fill_metrics(  # adapter-helper-ok: fill helper; DL3 decides wrap/internalize
    yf_ticker_obj,
    existing_metrics: Dict,
    info_dict: Dict = None,
) -> Tuple[Dict, Dict]:
    """Map yfinance .info keys to API financial metrics schema.

    Only fills fields that are missing/empty in existing_metrics.

    Args:
        info_dict: Optional pre-fetched yf_ticker_obj.info dict to avoid
            redundant network calls.

    Returns: (updated_metrics, summary_dict)
    """
    summary = {"fields_filled": 0, "fields_skipped": 0}

    try:
        info = info_dict if info_dict is not None else yfinance_call(lambda: yf_ticker_obj.info)
        if not info:
            return existing_metrics, summary
    except Exception as e:
        print(f"[WARNING] yfinance .info fetch failed: {_yfinance_safe_msg(e)}", file=sys.stderr)
        # ISS-219 (Loop31 cycle 1 fresh-session-18): preserve structured
        # error in summary so the run_meta yfinance_summary surfaces the
        # cause (RATE_LIMIT vs UPSTREAM_ERROR vs HTTP_TRANSPORT) instead
        # of a silent zero-fill. Pre-fix: `summary={"fields_filled":0,
        # "fields_skipped":0}` and the operator could not tell whether
        # the fallback ran cleanly with no data, was rate-limited, or
        # hit an upstream error.
        envelope = adapter_error_from_exception(e, source="yahoo_finance.yfinance_fill_metrics")
        if envelope.error is not None:
            summary["error"] = {
                "code": envelope.error.code.value,
                "detail": envelope.error.detail,
                "cause": envelope.error.cause,
            }
        return existing_metrics, summary

    if not existing_metrics or "error" in existing_metrics:
        existing_metrics = {
            "data_source": "yfinance",
            "ticker": yf_ticker_obj.ticker,
        }

    # ISS-153 (Loop17 cycle 1 fresh-session-4): UNKNOWN sentinel instead
    # of silent USD default — see yfinance_fill_financial_data L611
    # comment for ADR contamination rationale. The downstream
    # cross-currency check (`if fin_currency != "USD"`) treats UNKNOWN
    # as foreign and skips EV/FCF/P-S, which is the conservative path.
    # DL3a §2 invariant 3 — normalize before comparing so lowercase /
    # padded / unsupported drift can't bypass the fail-close.
    fin_currency = normalize_currency(info.get("financialCurrency")) or "UNKNOWN"

    mappings = {
        "trailingPE": "price_to_earnings_ratio",
        "priceToBook": "price_to_book_ratio",
        "priceToSalesTrailing12Months": "price_to_sales_ratio",
        "enterpriseToEbitda": "enterprise_value_to_ebitda_ratio",
        "enterpriseToRevenue": "enterprise_value_to_revenue_ratio",
        "enterpriseValue": "enterprise_value",
        "grossMargins": "gross_margin",
        "operatingMargins": "operating_margin",
        "profitMargins": "net_margin",
        "returnOnEquity": "return_on_equity",
        "returnOnAssets": "return_on_assets",
        "trailingEps": "earnings_per_share",
        "bookValue": "book_value_per_share",
        "currentRatio": "current_ratio",
        "quickRatio": "quick_ratio",
        "revenueGrowth": "revenue_growth",
        "earningsGrowth": "earnings_growth",
        "pegRatio": "peg_ratio",
        "marketCap": "market_cap",
        "payoutRatio": "payout_ratio",
    }

    if fin_currency != "USD":
        cross_currency_skip = {
            "enterpriseValue", "enterpriseToEbitda", "enterpriseToRevenue",
            "freeCashflow", "priceToSalesTrailing12Months",
        }
        mappings = {
            k: v for k, v in mappings.items() if k not in cross_currency_skip
        }
        print(
            f"[yfinance] Skipping EV/FCF/P-S metrics "
            f"(financialCurrency={fin_currency}, cross-currency contamination)",
            file=sys.stderr,
        )

    # ISS-127 (Loop9 cycle 1): tighten the local helper so EVERY site
    # below uses the same finite + non-bool guard. Pre-fix `f != f`
    # only rejected NaN; `float('inf')` slipped through (`fval != fval`
    # is False for Infinity), polluting metrics_data with non-finite
    # numerics that fetch.py later writes directly to
    # 02_financial_data.json.
    import math as _yf_math

    def _yf_num(v):
        # ISS-139 (Loop11 cycle 1): _is_bool_like also catches numpy bool.
        if v is None or _is_bool_like(v):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if _yf_math.isfinite(f) else None

    for yf_key, api_field in mappings.items():
        if yf_key in info and info[yf_key] is not None:
            fval = _yf_num(info[yf_key])
            if fval is None:
                continue
            if existing_metrics.get(api_field) is not None:
                summary["fields_skipped"] += 1
                continue
            existing_metrics[api_field] = fval
            summary["fields_filled"] += 1

    # debt_to_equity: yfinance %, API ratio
    if "debtToEquity" in info and info["debtToEquity"] is not None:
        de_val = _yf_num(info["debtToEquity"])
        if de_val is not None:
            if existing_metrics.get("debt_to_equity") is None:
                existing_metrics["debt_to_equity"] = de_val / 100.0
                summary["fields_filled"] += 1
            else:
                summary["fields_skipped"] += 1

    fcf_total = _yf_num(info.get("freeCashflow"))
    shares = _yf_num(info.get("sharesOutstanding"))
    if (
        existing_metrics.get("free_cash_flow_per_share") is None
        and fin_currency == "USD"
        and fcf_total is not None
        and shares is not None
        and shares > 0
    ):
        existing_metrics["free_cash_flow_per_share"] = (
            float(fcf_total) / float(shares)
        )
        summary["fields_filled"] += 1

    # free_cash_flow_yield
    if (
        existing_metrics.get("free_cash_flow_yield") is None
        and fin_currency == "USD"
    ):
        mcap = _yf_num(info.get("marketCap"))
        if fcf_total is not None and mcap is not None and mcap > 0:
            existing_metrics["free_cash_flow_yield"] = (
                float(fcf_total) / float(mcap)
            )
            # ISS-220 4.9 (Loop33 cycle 1): increment fields_filled.
            # Pre-fix this site wrote a value but did NOT increment the
            # counter, so `_run_yfinance_fallback_impl` saw
            # `met_summary["fields_filled"] == 0` and emitted
            # `met_filled=False`/`met_cat_status=None` even though data
            # had changed — run_meta misreported "no fill occurred."
            summary["fields_filled"] += 1

    if not existing_metrics.get("data_source"):
        existing_metrics["data_source"] = "yfinance"

    return existing_metrics, summary


def yfinance_fill_company_info(  # adapter-helper-ok: fill helper; DL3 decides wrap/internalize
    yf_ticker_obj,
    existing_company: Dict,
    info_dict: Dict = None,
) -> Tuple[Dict, Dict]:
    """Fill sparse company_data fields from yfinance .info.

    Only fills fields that are None/missing in existing data.

    Args:
        info_dict: Optional pre-fetched yf_ticker_obj.info dict to avoid
            redundant network calls.

    Returns: (updated_company, summary_dict)
    """
    summary = {"fields_filled": 0}

    try:
        info = info_dict if info_dict is not None else yfinance_call(lambda: yf_ticker_obj.info)
        if not info:
            return existing_company, summary
    except Exception as e:
        # ISS-091 (Loop7): inconsistent with the other 8 yfinance log
        # sites. Use _yfinance_safe_msg for env-key + auth-header +
        # home-path scrub.
        print(
            f"[WARNING] yfinance company info fetch failed: {_yfinance_safe_msg(e)}",
            file=sys.stderr,
        )
        # ISS-219 (Loop31): symmetric error-preservation with metrics fill.
        envelope = adapter_error_from_exception(e, source="yahoo_finance.yfinance_fill_company_info")
        if envelope.error is not None:
            summary["error"] = {
                "code": envelope.error.code.value,
                "detail": envelope.error.detail,
                "cause": envelope.error.cause,
            }
        return existing_company, summary

    prior_company = existing_company if isinstance(existing_company, dict) else {}
    if not existing_company or "error" in existing_company:
        existing_company = {
            "data_source": "yfinance",
            "ticker": yf_ticker_obj.ticker,
        }
        # Preserve critical classification fields from prior API response
        for key in ("is_adr", "category", "company_type", "exchange"):
            if key in prior_company:
                existing_company[key] = prior_company[key]

    # ISS-150 (Loop16 cycle 1 fresh-session-3): split fill_map into
    # NUMERIC vs STRING fields so a drifted upstream `marketCap: "123"`
    # (string) doesn't pollute company_data.market_cap with non-numeric.
    # Pre-fix the loop only rejected bool / non-finite-float and wrote
    # any other type verbatim — including string. Downstream readers
    # (Pattern S envelope JSON-safety + decision agents) treat market_cap
    # as numeric.
    numeric_fill_map = {
        "fullTimeEmployees": "employees",
        "marketCap": "market_cap",
    }
    string_fill_map = {
        "longBusinessSummary": "description",
        "website": "website_url",
        "country": "country",
        "industry": "industry",
        "sector": "sector",
        "shortName": "name",
    }

    for yf_key, api_field in numeric_fill_map.items():
        if yf_key in info and info[yf_key] is not None:
            val = info[yf_key]
            # ISS-127 (Loop9) + ISS-139 (Loop11) bool/non-finite guards.
            if _is_bool_like(val):
                continue
            # ISS-150 (Loop16): STRICT numeric check — must already be
            # int or float. Reject string/list/dict drift even if str
            # would parse via float() ("123"). Codex empirical proof:
            # `marketCap: "123"` slipped through pre-fix; numeric_fill_map
            # callers (downstream JSON consumers) treat market_cap as
            # numeric, not "parseable string."
            if not isinstance(val, (int, float)):
                continue
            if isinstance(val, float) and not math.isfinite(val):
                continue
            # employees stays int when input is int (avoid 164000 → 164000.0
            # casting noise on a conceptually-discrete field).
            if api_field == "employees":
                if isinstance(val, int):
                    stored: Any = val
                elif isinstance(val, float) and val.is_integer():
                    stored = int(val)
                else:
                    stored = float(val)
            else:
                stored = float(val)
            current_val = existing_company.get(api_field)
            if current_val is None or (isinstance(current_val, str) and not current_val.strip()):
                existing_company[api_field] = stored
                summary["fields_filled"] += 1

    for yf_key, api_field in string_fill_map.items():
        if yf_key in info and info[yf_key] is not None:
            val = info[yf_key]
            # ISS-150 (Loop16): require str for string fields; reject
            # numeric / bool / list drift that would otherwise be
            # written as-is.
            if not isinstance(val, str):
                continue
            if not val.strip():
                continue
            current_val = existing_company.get(api_field)
            if current_val is None or (isinstance(current_val, str) and not current_val.strip()):
                existing_company[api_field] = val
                summary["fields_filled"] += 1

    # Rebuild company_type if lost during error-payload rebuild
    if "company_type" not in existing_company:
        raw_is_adr = existing_company.get("is_adr", False)
        if isinstance(raw_is_adr, str):  # pattern-x-ok: defensive numpy.bool_/JSON-true drift rebuild per ISS-141/ISS-220 SF-D
            is_adr = raw_is_adr.strip().lower() in ("1", "true", "yes")  # pattern-x-ok: defensive numpy.bool_/JSON-true drift rebuild per ISS-141/ISS-220 SF-D
        else:
            is_adr = bool(raw_is_adr)
        _yf_country = existing_company.get("country") or ""
        _is_foreign = is_adr or (bool(_yf_country) and not is_us_country(_yf_country))
        _exchange = str(existing_company.get("exchange", "")).upper()
        _is_otc = any(kw in _exchange for kw in ("OTC", "PINK", "GREY"))
        existing_company["company_type"] = {
            "is_foreign": _is_foreign,
            "is_adr": is_adr,
            "is_otc": _is_otc,
            "home_country": existing_company.get("country", ""),
            "exchange": existing_company.get("exchange", ""),
            "filing_type_hint": "20-F" if (is_adr or _is_foreign) else "10-K",
            "requires_20f": is_adr or _is_foreign,
            "currency_warning": False,
            "detection_source": ["yfinance_fallback"],
            "api_currency": normalize_currency(info.get("currency")) or "UNKNOWN",
        }

    return existing_company, summary


def yfinance_fill_analyst_estimates(  # adapter-helper-ok: fill helper; DL3 decides wrap/internalize
    native_ticker: str,
    existing_estimates: Dict,
    has_yfinance: bool = None,
    yf_module=None,
) -> Tuple[Dict, Dict]:
    """Fetch analyst price targets and recommendations via yfinance.

    Uses native_ticker (e.g., "6762.T") for better coverage on foreign stocks.
    Stores in a separate 'yfinance_analyst' key to avoid schema conflicts.

    Returns: (updated_estimates, summary_dict)
    """
    summary = {"price_targets": False, "recommendations": False}

    if not isinstance(existing_estimates, dict):
        existing_estimates = {}

    # Use DI params if provided, fall back to module globals for backward compat
    _has_yf = has_yfinance if has_yfinance is not None else HAS_YFINANCE
    _yf = yf_module if yf_module is not None else yf

    if not _has_yf:
        return existing_estimates, summary

    try:
        # ISS-145 (Loop15 cycle 1 fresh-session-2): validate native_ticker
        # before constructing the yfinance object below. yfinance
        # interpolates the ticker into URL paths and our validator
        # (yfinance_guard.validate_yfinance_ticker) is the only defense
        # against ticker-injection (e.g. "../path", query separators,
        # percent-encoding). adr/detect.detect_adr_market_data already
        # does this; this site was missed.
        from scripts.sources.yfinance_guard import (
            validate_yfinance_ticker as _validate_yf_ticker,
            InvalidTickerError as _InvalidTickerError,
        )
        try:
            native_ticker = _validate_yf_ticker(native_ticker)
        except _InvalidTickerError as _ite:
            print(
                f"[WARNING] yfinance native_ticker rejected: {_ite}",
                file=sys.stderr,
            )
            return existing_estimates, summary
        # Ticker() is lazy — no HTTP, no wrap needed. HTTP fires on property
        # accesses below, which MUST each go through yfinance_call for rate-
        # limit retry/translation.
        yf_native = _yf.Ticker(native_ticker)  # fail-open-ok: lazy constructor, no HTTP

        # DL3a §3.2 — hoist native_info so it is available for both
        # price-target currency resolution (inner try below) AND the
        # top-level quote_currency / statement_currency emission. Isolated
        # in its own try so an info-fetch failure does NOT abort price-target
        # or recommendations fetching.
        try:
            native_info = yfinance_call(lambda: yf_native.info) or {}
        except Exception:
            native_info = {}
        if not isinstance(native_info, dict):
            # yfinance contract returns dict; defend against future drift
            # (non-dict truthy would crash the .get below at emission time).
            native_info = {}

        # DL3a §3.2 row 5 — top-level analyst currency emission. Hoisted to
        # IMMEDIATELY after native_info so a transient failure inside
        # price_targets / recommendations cannot bypass the emission. The
        # only way to skip emission is if the outer try aborts before
        # native_info is established — in which case Task 14b's
        # resolver-repair at fetch.py post-fallback fills the gap.
        _cur = normalize_currency(native_info.get("currency"))
        if _cur is None:
            # FIX-C4-004: endswith(".T") not substring — mirrors the fix
            # applied to the nested price_targets.currency below.
            _cur = "JPY" if native_ticker.upper().endswith(".T") else "UNKNOWN"
        existing_estimates["quote_currency"] = _cur
        _stmt_cur = normalize_currency(native_info.get("financialCurrency")) or "UNKNOWN"
        existing_estimates["statement_currency"] = _stmt_cur

        # Price targets
        try:
            targets = yfinance_call(lambda: yf_native.analyst_price_targets)
            if targets is not None:
                if isinstance(targets, dict):
                    target_data = {
                        "current": targets.get("current"),
                        "mean": targets.get("mean"),
                        "median": targets.get("median"),
                        "high": targets.get("high"),
                        "low": targets.get("low"),
                    }
                else:
                    target_data = {
                        "current": targets.iloc[0].get("current") if len(targets) > 0 else None,
                        "mean": targets.iloc[0].get("mean") if len(targets) > 0 else None,
                    }
                # ISS-127 (Loop9 cycle 1): reject non-finite (NaN/+Inf/
                # -Inf) and bool drift. Pre-fix `v == v` only rejected
                # NaN — Inf would land as a literal "inf" in
                # 06_analyst_estimates.json, breaking JSON round-trip
                # for any consumer using allow_nan=False.
                def _safe_target_num(v):
                    # ISS-139 (Loop11 cycle 1): catch numpy bool too.
                    if v is None or _is_bool_like(v):
                        return None
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        return None
                    return f if math.isfinite(f) else None

                target_data = {
                    k: _safe_target_num(v)
                    for k, v in target_data.items()
                }
                if any(v is not None for v in target_data.values()):
                    if "yfinance_analyst" not in existing_estimates:
                        existing_estimates["yfinance_analyst"] = {}
                    existing_estimates["yfinance_analyst"]["price_targets"] = target_data
                    # native_info hoisted above (DL3a §3.2) — no re-fetch.
                    # ISS-190 (Loop26 cycle 1 fresh-session-13): use
                    # "UNKNOWN" sentinel instead of silent USD default.
                    # Pre-fix the fallback for any non-`.T` native ticker
                    # was USD — ADRs traded as `.HK` / `.L` / `.PA` /
                    # `.TO` etc. would silently take USD when yfinance
                    # didn't supply currency, contaminating analyst
                    # price-target consumers. Mirror of ISS-153 fix on
                    # financial-statement currency. Tokyo (`.T` → JPY)
                    # is preserved because it was the original specific
                    # mapping with a clear known source.
                    # FIX-C4-004: endswith(".T") not substring (".T" in
                    # ticker) — substring mislabels SHOP.TO / 2330.TW as
                    # JPY because ".T" appears as a substring of ".TO"/".TW".
                    # DL3a §2 invariant 3 — normalize before applying .T-suffix
                    # fallback. Raw `native_info.get("currency")` can be
                    # lowercase / padded / unsupported ISO; normalize_currency
                    # collapses those to None so the suffix fallback fires
                    # correctly (e.g. lowercase `"jpy"` from a .T ticker now
                    # normalizes to `"JPY"` directly instead of relying on the
                    # suffix fallback masking the case-drift).
                    cur = normalize_currency(native_info.get("currency"))
                    if cur is None:
                        cur = "JPY" if native_ticker.upper().endswith(".T") else "UNKNOWN"
                    existing_estimates["yfinance_analyst"]["price_targets"]["currency"] = cur
                    existing_estimates["yfinance_analyst"]["price_targets"]["native_ticker"] = native_ticker
                    summary["price_targets"] = True
        except Exception as e:
            print(f"[WARNING] yfinance analyst price targets failed: {_yfinance_safe_msg(e)}", file=sys.stderr)
            # ISS-220 4.11 (Loop33 cycle 1): preserve structured error
            # in summary so run_meta yfinance_summary surfaces the cause
            # of price_targets failure. Symmetric with ISS-219 (Loop31)
            # outer-block fix; this inner sub-block was the missed
            # sibling.
            envelope = adapter_error_from_exception(
                e, source="yahoo_finance.yfinance_fill_analyst_estimates/price_targets",
            )
            if envelope.error is not None:
                summary["price_targets_error"] = {
                    "code": envelope.error.code.value,
                    "detail": envelope.error.detail,
                    "cause": envelope.error.cause,
                }

        # Recommendations
        try:
            recs = yfinance_call(lambda: yf_native.recommendations)
            if recs is not None and not recs.empty:
                latest = recs.iloc[0] if len(recs) > 0 else None
                if latest is not None:
                    rec_data = {}
                    for col_name in ["strongBuy", "buy", "hold", "sell", "strongSell"]:
                        if col_name in recs.columns:
                            val = latest[col_name] if col_name in latest.index else None
                            # ISS-141 (Loop12 cycle 1): bool guard before
                            # int(val). pre-fix `int(np.bool_(True))` returns
                            # 1, silently inflating a recommendation count
                            # by 1 vote when upstream had a bool-dtype
                            # column. Same root cause as ISS-139 but in a
                            # different yfinance subpath. NaN check via
                            # `val == val` retained — pandas NaT/NaN safe.
                            # ISS-220 4.10 (Loop33 cycle 1): also reject Inf.
                            # Pre-fix `int(float("inf"))` raised OverflowError,
                            # which is NOT caught by `(TypeError, ValueError)`,
                            # so a single Inf cell propagated up to the outer
                            # except and dropped ALL recommendations across
                            # every column. Use math.isfinite to gate.
                            if (
                                val is None or _is_bool_like(val)
                                or val != val  # NaN
                                or (isinstance(val, float) and not math.isfinite(val))
                            ):
                                rec_data[col_name] = 0
                            else:
                                try:
                                    rec_data[col_name] = int(val)
                                except (TypeError, ValueError, OverflowError):
                                    rec_data[col_name] = 0
                    if rec_data:
                        if "yfinance_analyst" not in existing_estimates:
                            existing_estimates["yfinance_analyst"] = {}
                        existing_estimates["yfinance_analyst"]["recommendations"] = rec_data
                        existing_estimates["yfinance_analyst"]["native_ticker"] = native_ticker
                        summary["recommendations"] = True
        except Exception as e:
            print(f"[WARNING] yfinance analyst recommendations failed: {_yfinance_safe_msg(e)}", file=sys.stderr)
            # ISS-220 4.11 (Loop33 cycle 1): same pattern as price_targets.
            envelope = adapter_error_from_exception(
                e, source="yahoo_finance.yfinance_fill_analyst_estimates/recommendations",
            )
            if envelope.error is not None:
                summary["recommendations_error"] = {
                    "code": envelope.error.code.value,
                    "detail": envelope.error.detail,
                    "cause": envelope.error.cause,
                }

    except Exception as e:
        print(f"[WARNING] yfinance analyst estimates failed for {native_ticker}: {_yfinance_safe_msg(e)}", file=sys.stderr)
        # ISS-219 (Loop31): symmetric error-preservation with metrics +
        # company fills. Outer-most try guard for analyst is the right
        # surface for top-level fetch failures (RATE_LIMIT etc).
        envelope = adapter_error_from_exception(e, source="yahoo_finance.yfinance_fill_analyst_estimates")
        if envelope.error is not None:
            summary["error"] = {
                "code": envelope.error.code.value,
                "detail": envelope.error.detail,
                "cause": envelope.error.cause,
            }

    return existing_estimates, summary


# ---------------------------------------------------------------------------
# Structured outcome for _run_yfinance_fallback_impl (DL2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FallbackCategoryUpdate:
    """Per-category result of yfinance fallback fill attempt.

    Preserves the full pre-migration surface so `to_run_meta_dict()` +
    `merge_fallback_outcome()` can reproduce byte-identical output:

    - `filled` is whether the yfinance pass actually wrote any field.
    - `raw_summary` is the legacy per-category summary dict produced by
      `yfinance_fill_*` helpers (e.g., `{"income_filled": N,
      "balance_filled": N, "cashflow_filled": N}` for financials,
      `{"fields_filled": N, "fields_skipped": N}` for metrics,
      `{"fields_filled": N}` for company, `{"price_targets": bool,
      "recommendations": bool}` for analyst). This is what the
      pre-migration `yfinance_summary["fills"][cat]` contained.
    - `category_status` is the legacy `category_statuses[cat]` write
      payload (for financials: `{status, income_count, balance_count,
      cashflow_count, latest_period, data_source}`; for metrics and
      company: `{status, data_source}`; for analyst: `None` because
      pre-migration never wrote `category_statuses["analyst"]`).
      `None` means `merge_fallback_outcome` must not write this
      category into `category_statuses`.
    """
    filled: bool
    raw_summary: dict = field(default_factory=dict)
    category_status: Optional[dict] = None


@dataclass(frozen=True)
class YfinanceFallbackOutcome:
    """Structured result of _run_yfinance_fallback_impl.

    Replaces the v1 5-tuple + cross-module category_statuses mutation.
    fetch.py consumes this via merge_fallback_outcome helper (Task 11).

    Preserves the full pre-migration `yfinance_summary` shape for
    byte-identical run_meta serialization (see `to_run_meta_dict`).
    Pre-migration shape includes top-level `native_ticker` /
    `native_currency` / `error` keys not present on the v1 draft.
    """
    attempted: bool
    available: bool
    reason: Optional[str]
    fills: dict
    # Derived dicts — retain back-compat for in-progress consumers
    financials_data: dict
    metrics_data: dict
    company_data: dict
    analyst_data: dict
    # Legacy top-level yfinance_summary fields (v7 restoration)
    native_ticker: Optional[str] = None
    native_currency: Optional[str] = None
    error: Optional[str] = None

    def to_run_meta_dict(self) -> dict:
        """Serialize to the legacy yfinance_summary dict shape.
        Byte-equivalent with pre-migration output (enforced by
        test_yfinance_fallback_outcome_run_meta_bytes_equivalent +
        Slice-1 snapshot fixture).

        Matches pre-migration `_run_yfinance_fallback_impl` exactly:
        - top-level: attempted / available / reason / fills
        - fills values: the RAW per-category summary dicts
          (income_filled/balance_filled/cashflow_filled for financials,
          fields_filled/fields_skipped for metrics, etc.) — NOT the
          compressed `{filled, status}` form used internally by
          merge_fallback_outcome
        - native_ticker / native_currency: present when set, omitted
          otherwise (pre-migration sets them unconditionally after the
          fallback path runs; early-exit paths don't)
        - error: present when an exception was captured, omitted
          otherwise
        """
        out: dict = {
            "attempted": self.attempted,
            "available": self.available,
            "reason": self.reason,
            "fills": {k: dict(v.raw_summary) for k, v in self.fills.items()},
        }
        if self.native_ticker is not None:
            out["native_ticker"] = self.native_ticker
        if self.native_currency is not None:
            out["native_currency"] = self.native_currency
        if self.error is not None:
            out["error"] = self.error
        return out


# ---------------------------------------------------------------------------
# DI variant: yfinance fallback orchestrator
# ---------------------------------------------------------------------------

def _run_yfinance_fallback_impl(
    ticker: str,
    financials_data: Dict,
    metrics_data: Dict,
    company_data: Dict,
    analyst_data: Dict,
    has_yfinance: bool,
    yf_module,
) -> YfinanceFallbackOutcome:
    """Orchestrator: run yfinance fallback if any critical data is empty.

    v6.3-DL2: no category_statuses mutation; returns structured
    YfinanceFallbackOutcome; caller merges via merge_fallback_outcome.

    This is the DI variant: *has_yfinance* and *yf_module* are injected
    explicitly instead of reading module globals.
    """
    fills: dict = {}

    if not has_yfinance:
        return YfinanceFallbackOutcome(
            attempted=False, available=False,
            reason="yfinance not installed",
            fills={},
            financials_data=financials_data,
            metrics_data=metrics_data,
            company_data=company_data,
            analyst_data=analyst_data,
        )

    # Determine if fallback is needed
    financials_empty = (
        not financials_data.get("income_statements")
        or not financials_data.get("balance_sheets")
        or not financials_data.get("cash_flows")
    )
    metrics_empty = (
        not metrics_data
        or "error" in metrics_data
        or metrics_data.get("price_to_earnings_ratio") is None
    )
    company_sparse = (
        not company_data.get("description")
        or company_data.get("employees") is None
    )

    needs_fallback = financials_empty or metrics_empty or company_sparse

    if not needs_fallback:
        return YfinanceFallbackOutcome(
            attempted=False, available=True,
            reason="API data sufficient, no fallback needed",
            fills={},
            financials_data=financials_data,
            metrics_data=metrics_data,
            company_data=company_data,
            analyst_data=analyst_data,
        )

    reasons = []
    if financials_empty:
        reasons.append("financials_empty")
    if metrics_empty:
        reasons.append("metrics_empty")
    if company_sparse:
        reasons.append("company_sparse")

    print("\n[YF] Running yfinance Fallback (v7.0)...", file=sys.stderr)
    print(f"    Reason: {', '.join(reasons)}", file=sys.stderr)

    # v15 scaffolding: initialize error + native_ticker + native_currency
    # to None BEFORE the try block. If an exception fires during
    # fills (before load_native_ticker is called), these stay None and
    # to_run_meta_dict() omits the keys — byte-equivalent with
    # pre-migration which only writes them after successful fills.
    error: Optional[str] = None
    native_ticker: Optional[str] = None
    native_currency: Optional[str] = None

    try:
        # ISS-145 (Loop15 cycle 1): validate ticker before constructing
        # the yfinance object — see comment in load_native_ticker for
        # the SSRF / injection rationale. The fallback orchestrator
        # receives `ticker` from fetch.py which originates from CLI /
        # portfolio config; never trust caller validation.
        from scripts.sources.yfinance_guard import (
            validate_yfinance_ticker as _validate_yf_ticker,
            InvalidTickerError as _InvalidTickerError,
        )
        try:
            ticker = _validate_yf_ticker(ticker)
        except _InvalidTickerError as _ite:
            print(
                f"[WARNING] yfinance fallback ticker rejected: {_ite}",
                file=sys.stderr,
            )
            return YfinanceFallbackOutcome(
                attempted=False, available=False,
                reason="invalid ticker (failed validate_yfinance_ticker)",
                fills={},
                financials_data=financials_data,
                metrics_data=metrics_data,
                company_data=company_data,
                analyst_data=analyst_data,
                error=str(_ite),
            )
        # Ticker() is lazy — no HTTP, no wrap needed. HTTP fires on .info below,
        # which is wrapped for rate-limit retry/translation.
        yf_obj = yf_module.Ticker(ticker)  # fail-open-ok: lazy constructor, no HTTP

        # Pre-fetch .info once to avoid redundant network calls in fill functions
        try:
            _prefetched_info = yfinance_call(lambda: yf_obj.info)
        except Exception:
            _prefetched_info = None

        # FINANCIALS fill
        if financials_empty:
            financials_data, fin_summary = yfinance_fill_financial_data(
                yf_obj, financials_data, info_dict=_prefetched_info,
            )
            fin_filled = (
                fin_summary["income_filled"] > 0
                or fin_summary["balance_filled"] > 0
                or fin_summary["cashflow_filled"] > 0
            )
            fin_cat_status = None
            if fin_filled:
                has_inc = bool(financials_data.get("income_statements"))
                has_bs = bool(financials_data.get("balance_sheets"))
                has_cf = bool(financials_data.get("cash_flows"))
                if has_inc and has_bs and has_cf:
                    new_status = "PASSED"
                elif has_inc or has_bs or has_cf:
                    new_status = "PARTIAL"
                else:
                    new_status = "FAILED"
                fin_cat_status = {
                    "status": new_status,
                    "income_count": len(financials_data.get("income_statements", [])),
                    "balance_count": len(financials_data.get("balance_sheets", [])),
                    "cashflow_count": len(financials_data.get("cash_flows", [])),
                    "latest_period": (
                        financials_data["income_statements"][0].get("report_period")
                        if financials_data.get("income_statements") else None
                    ),
                    "data_source": "yfinance",
                }
                print(
                    f"    Financials: income={fin_summary['income_filled']}, "
                    f"balance={fin_summary['balance_filled']}, "
                    f"cashflow={fin_summary['cashflow_filled']}",
                    file=sys.stderr,
                )
            fills["financials"] = FallbackCategoryUpdate(
                filled=fin_filled,
                raw_summary=fin_summary,
                category_status=fin_cat_status,
            )

        # METRICS fill — raw_summary UNCONDITIONAL (pre-migration always
        # writes fills["metrics"] when this block enters); category_status
        # gated on fields_filled > 0
        if metrics_empty:
            metrics_data, met_summary = yfinance_fill_metrics(
                yf_obj, metrics_data, info_dict=_prefetched_info,
            )
            met_filled = met_summary["fields_filled"] > 0
            met_cat_status = None
            if met_filled:
                met_cat_status = {
                    "status": (
                        "PASSED"
                        if metrics_data.get("price_to_earnings_ratio") is not None
                        else "PARTIAL"
                    ),
                    "data_source": "yfinance",
                }
                print(
                    f"    Metrics: {met_summary['fields_filled']} fields "
                    f"filled, {met_summary['fields_skipped']} skipped",
                    file=sys.stderr,
                )
            fills["metrics"] = FallbackCategoryUpdate(
                filled=met_filled,
                raw_summary=met_summary,
                category_status=met_cat_status,
            )

        # COMPANY fill — same pattern as metrics
        if company_sparse:
            company_data, comp_summary = yfinance_fill_company_info(
                yf_obj, company_data, info_dict=_prefetched_info,
            )
            comp_filled = comp_summary["fields_filled"] > 0
            comp_cat_status = None
            if comp_filled:
                # ISS-220 4.28 (Loop36 cycle 1): truthy check on
                # `employees` was buggy — a legitimate `0` (e.g.
                # holding company / shell) is falsy and dropped
                # status to PARTIAL. Use `is not None` so 0 counts
                # as "field present" for status semantics.
                # ISS-220 Loop40 Logic-1 (iter9): `description=""`
                # was treated as present by `is not None`. Iter11
                # (per superpowers review): extract `_company_text_present`
                # so the predicate is unit-testable without
                # round-tripping through the whole fallback path.
                desc_present = _company_text_present(company_data.get("description"))
                emp_present = company_data.get("employees") is not None
                comp_cat_status = {
                    "status": "PASSED" if (desc_present and emp_present) else "PARTIAL",
                    "data_source": "yfinance",
                }
                print(
                    f"    Company: {comp_summary['fields_filled']} fields "
                    f"filled",
                    file=sys.stderr,
                )
            fills["company"] = FallbackCategoryUpdate(
                filled=comp_filled,
                raw_summary=comp_summary,
                category_status=comp_cat_status,
            )

        # Resolve native ticker — AFTER financials/metrics/company fills,
        # BEFORE analyst gate. Pre-migration order; if an exception fires
        # in the fill blocks above, native_ticker stays None (matches
        # pre-migration which omits keys on error path).
        native_ticker, native_currency = load_native_ticker(ticker)

        # ANALYST fill — gated; ONLY write fills["analyst"] inside gate
        analyst_count = (
            analyst_data.get("count", 0)
            if isinstance(analyst_data, dict) else 0
        )
        if native_ticker and analyst_count < 4:
            print(
                f"    Native ticker: {native_ticker} ({native_currency}) "
                f"for analyst data",
                file=sys.stderr,
            )
            analyst_data, analyst_summary = yfinance_fill_analyst_estimates(
                native_ticker, analyst_data,
                has_yfinance=has_yfinance, yf_module=yf_module,
            )
            analyst_filled = (
                analyst_summary["price_targets"]
                or analyst_summary["recommendations"]
            )
            if analyst_filled:
                print(
                    f"    Analyst: targets={analyst_summary['price_targets']}, "
                    f"recs={analyst_summary['recommendations']}",
                    file=sys.stderr,
                )
            # analyst category_status is None (pre-migration never writes
            # category_statuses["analyst"])
            fills["analyst"] = FallbackCategoryUpdate(
                filled=analyst_filled,
                raw_summary=analyst_summary,
                category_status=None,
            )

    except Exception as e:
        # ISS-038 / ISS-064 (Loop2/4 backlog): yfinance exception messages
        # can carry session/cookie/path info. `_yfinance_safe_msg` builds
        # a sanitized form: scrub env API keys + scrub Authorization /
        # Cookie / Set-Cookie / token / session headers + scrub absolute
        # local paths (cache dir leakage), then truncate.
        scrubbed = _yfinance_safe_msg(e)
        print(f"[ERROR] yfinance fallback failed: {scrubbed}", file=sys.stderr)
        error = scrubbed

    print("    yfinance fallback complete.", file=sys.stderr)

    # v15: attempted=True hardcoded (this return only fires on main
    # fallback path; early exits have their own returns with attempted=False)
    return YfinanceFallbackOutcome(
        attempted=True,
        available=has_yfinance,
        reason=", ".join(reasons) or "no reasons",
        fills=fills,
        financials_data=financials_data,
        metrics_data=metrics_data,
        company_data=company_data,
        analyst_data=analyst_data,
        native_ticker=native_ticker,
        native_currency=native_currency,
        error=error,
    )
