"""DL3c FX rate adapter — fetch yfinance FX history for a (currency,
list of report_period dates) request and return per-quarter rates with
an ``AdapterResult`` envelope.

Public surface (spec §3.0.1): ``YFINANCE_FX_POLICY``, ``FxRate``,
``get_fx_window``, ``detect_outlier_rates``. ``SUPPORTED_FX_CURRENCIES``
is RE-EXPORTED from ``scripts.fx_constants`` (closed-vocab SoT — do
NOT redeclare; cycle-18 F-18-2).

Envelope contract (CLAUDE.md adapter authoring §3.0.1): HTTP via
``yfinance_call`` (NOT raw http_get — yfinance has its own requests
stack); empty rows → PARSE_ERROR; YfCallError → HTTP_TRANSPORT;
YfRateLimitError → RATE_LIMIT; RetryExhaustedError → routed via
``adapter_error_from_exception`` (Pattern V).
"""
from __future__ import annotations

import dataclasses
import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from scripts.fx_constants import (  # noqa: F401 — re-export
    SUPPORTED_FX_CURRENCIES,
)
from scripts.sources.adapter_result import (
    AdapterResult,
    ErrorCode,
    adapter_error_from_exception,
)
from scripts.sources.common import HttpPolicy, RetryExhaustedError
from scripts.sources.yfinance_guard import (
    YfCallError,
    YfRateLimitError,
    yfinance_call,
)

__all__ = [
    "SUPPORTED_FX_CURRENCIES",
    "FxRate",
    "YFINANCE_FX_POLICY",
    "get_fx_window",
    "detect_outlier_rates",
]


# Policy is documentation-only (yfinance uses its own HTTP stack; the
# values here parallel other adapters for parity / future migration).
YFINANCE_FX_POLICY: HttpPolicy = HttpPolicy(
    timeout_s=30,
    max_retries=3,
    retry_after_cap_s=5.0,
    allowed_host_suffixes=frozenset(
        {"finance.yahoo.com", "query2.finance.yahoo.com"}
    ),
    max_response_bytes=10_000_000,
)


@dataclass(frozen=True)
class FxRate:
    """One FX rate row aligned to a single report_period.

    ``fx_rate_usd_per_local``: multiply a local-currency value by this
    rate to get USD. For USD short-circuits, rate is 1.0 and source is
    ``"usd_native"``.
    """
    currency: str                   # e.g. "JPY" (post-normalization)
    date: str                       # YYYY-MM-DD report_period sampled
    fx_rate_usd_per_local: float    # USD per 1 local-currency unit
    source: str                 # "yfinance:JPY=X" | "usd_native"
    bar_date: str               # actual yfinance bar date used
    lag_days: int               # (date - bar_date) days; 0 for usd_native


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _normalize_currency(raw: str | None) -> str | None:
    """Strip + upper-case; None on empty / non-string. Parity with
    historical_multiples + extract_fcf. Surfaces the "unrecognized"
    case BEFORE the SUPPORTED_FX_CURRENCIES membership check (see
    routing rule in scripts/fx_constants.py)."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return stripped.upper()


def _parse_iso_date(s: str) -> Optional[datetime]:
    """Parse YYYY-MM-DD; None on any parse error."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _extract_close(row):
    """Pull 'Close' (pandas) / 'close' (test dict) / row[1] (tuple)."""
    if isinstance(row, dict):
        return row.get("Close", row.get("close"))
    if hasattr(row, "get"):
        return row.get("Close")
    try:
        return row["Close"]
    except (KeyError, TypeError, IndexError):
        try:
            return row[1]
        except (TypeError, IndexError):
            return None


def _iter_history_rows(df):
    """Yield (idx, row) from a pandas DataFrame or a test-double
    iterable of (date, close) tuples / dicts."""
    if hasattr(df, "iterrows") and hasattr(df, "index"):
        for idx, row in df.iterrows():
            yield idx, row
        return
    for item in df:
        if isinstance(item, dict):
            yield item.get("date", ""), item
        else:
            yield item[0], item


def _fetch_yfinance_history(yf_ticker: str) -> AdapterResult:
    """Fetch full yfinance history for ``yf_ticker`` (e.g. "JPY=X").

    Ok shape: ``data={"items": [{"date": "YYYY-MM-DD", "close": float},
    ...]}`` sorted ascending. Failure routing per envelope contract.
    """
    src = f"yfinance:{yf_ticker}"
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return AdapterResult.failed(
            code=ErrorCode.HTTP_TRANSPORT,
            detail="yfinance package not installed",
            source=src, retryable=False,
        )

    try:
        df = yfinance_call(
            lambda: yf.Ticker(yf_ticker).history(period="max", interval="1d")  # fail-open-ok: wrapped in yfinance_call() above (multi-line)
        )
    except YfRateLimitError as e:
        return AdapterResult.failed(
            code=ErrorCode.RATE_LIMIT, detail=str(e)[:400],
            source=src, retryable=True, cause=type(e).__name__,
        )
    except YfCallError as e:
        return AdapterResult.failed(
            code=ErrorCode.HTTP_TRANSPORT, detail=str(e)[:400],
            source=src, retryable=True, cause=type(e).__name__,
        )
    except RetryExhaustedError as e:
        # Pattern V — canonical mapper handles 429 / SEC-403 routing.
        return adapter_error_from_exception(e, source=src)

    if df is None:
        return AdapterResult.failed(
            code=ErrorCode.PARSE_ERROR,
            detail=f"yfinance returned None for {yf_ticker}",
            source=src, retryable=False,
        )
    if getattr(df, "empty", None) is True:
        return AdapterResult.failed(
            code=ErrorCode.PARSE_ERROR,
            detail=f"yfinance returned empty history for {yf_ticker}",
            source=src, retryable=False,
        )

    rows: list[dict[str, object]] = []
    try:
        for idx, row in _iter_history_rows(df):
            close = _extract_close(row)
            if close is None:
                continue
            try:
                close_f = float(close)
            except (TypeError, ValueError):
                continue
            if close_f <= 0 or close_f != close_f:  # NaN/non-positive
                continue
            date_str = (
                idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime")
                else str(idx)[:10]
            )
            if not date_str:
                continue
            rows.append({"date": date_str, "close": close_f})
    except (KeyError, ValueError, TypeError) as e:
        return AdapterResult.failed(
            code=ErrorCode.PARSE_ERROR,
            detail=f"yfinance row parse failure for {yf_ticker}: {e}"[:400],
            source=src, retryable=False,
        )

    if not rows:
        return AdapterResult.failed(
            code=ErrorCode.PARSE_ERROR,
            detail=f"yfinance returned no usable rows for {yf_ticker}",
            source=src, retryable=False,
        )

    rows.sort(key=lambda r: r["date"])
    return AdapterResult.passed(data={"items": rows})


def _lookup_rate_for_date(
    history: list[tuple[str, float]],
    target_date: str,
    *,
    max_lag_days: int = 14,
) -> Optional[tuple[float, str, int]]:
    """Forward-no-skew: latest history bar with bar_date <= target AND
    target - bar_date <= ``max_lag_days``. None otherwise (caller maps
    to fx_history_insufficient)."""
    target_dt = _parse_iso_date(target_date)
    if target_dt is None:
        return None
    best: Optional[tuple[float, str, int]] = None
    for bar_date, close in history:
        bar_dt = _parse_iso_date(bar_date)
        if bar_dt is None or bar_dt > target_dt:
            continue
        lag = (target_dt - bar_dt).days
        if lag > max_lag_days:
            continue
        # History is sorted ascending; the last qualifying row wins.
        best = (float(close), bar_date, lag)
    return best


def detect_outlier_rates(rates: list[float]) -> bool:
    """True iff any rate is more than 10× above OR more than 10× below the
    rates' median (invariant 11 — symmetric outlier).

    PUBLIC (no underscore prefix) — promoted per cycle-23 F-23-1 so
    ``scripts/fx_apply.py`` can import via the legitimate public
    interface. Empty / single-element / non-finite lists return False.

    post-impl loop-1 cycle-4 HIGH: pre-fix only checked the upper tail
    (`r > 10×med`). After the ISS-019 inversion (rate = 1.0 / close),
    a yfinance ``close`` spike UPWARD becomes a tiny rate value DOWNWARD
    that escapes upper-bound detection. A 100× over-stated close →
    100× understated rate → consumer multiplies local-currency values by
    a near-zero rate → silently produces USD values that look 100× too
    small (FCF / earnings collapse). The symmetric check catches both
    tails of the corrupted-close distribution.
    """
    if not rates:
        return False
    finite = [
        r for r in rates
        if isinstance(r, (int, float))
        and r == r
        and r not in (float("inf"), float("-inf"))
    ]
    if len(finite) < 2:
        return False
    med = statistics.median(finite)
    if med <= 0:
        return False
    upper = 10.0 * med
    lower = med / 10.0
    return any(r > upper or r < lower for r in finite)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def get_fx_window(
    currency: str,
    report_periods: list[str],
    *,
    ticker: str,
) -> AdapterResult:
    """Fetch FX rates for ``currency`` aligned to ``report_periods``.

    Ok shape: ``data={"items": [FxRate-as-dict, ...]}`` aligned to
    ``report_periods`` ORDER (NOT sorted). On failure, ``error.detail``
    carries one of the §2 closed fx_failure_reasons strings.

    USD short-circuits: returns ok immediately with rate=1.0 per period
    (no HTTP, no yfinance call).
    """
    src = f"fx_rates:{ticker}"

    # Step 1: validate inputs.
    # ISO 4217 parseability gate (spec L1430-1435 / D3): unrecognized
    # routes BEFORE the SUPPORTED membership check. Inputs that are
    # None / empty / non-string / not exactly 3 uppercase letters
    # (e.g. "Y", "Yen", "FOOBAR", "JPY1") → _unrecognized. Only
    # parseable 3-letter ISO codes that fall outside SUPPORTED (e.g.
    # "BRL") → _unsupported.
    normalized = _normalize_currency(currency)
    if normalized is None or not re.fullmatch(r"[A-Z]{3}", normalized):
        return AdapterResult.failed(
            code=ErrorCode.SHAPE_MISMATCH,
            detail="fx_currency_unrecognized",
            source=src, retryable=False,
        )
    if normalized not in SUPPORTED_FX_CURRENCIES:
        return AdapterResult.failed(
            code=ErrorCode.SHAPE_MISMATCH,
            detail=f"fx_currency_unsupported: {normalized}",
            source=src, retryable=False,
        )
    if not isinstance(report_periods, list) or not report_periods:
        return AdapterResult.failed(
            code=ErrorCode.SHAPE_MISMATCH,
            detail="report_periods must be a non-empty list",
            source=src, retryable=False,
        )
    for p in report_periods:
        if _parse_iso_date(p) is None:
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=f"report_period not YYYY-MM-DD: {p!r}",
                source=src, retryable=False,
            )

    # Step 2: USD short-circuit (no HTTP).
    if normalized == "USD":
        items = [
            dataclasses.asdict(FxRate(
                currency="USD", date=p, fx_rate_usd_per_local=1.0,  # fail-open-ok: USD short-circuit (rate=1.0)
                source="usd_native", bar_date=p, lag_days=0,
            ))
            for p in report_periods
        ]
        return AdapterResult.passed(data={"items": items})

    # Step 3: fetch yfinance history.
    yf_ticker = f"{normalized}=X"
    hist_result = _fetch_yfinance_history(yf_ticker)
    if not hist_result.ok:
        assert hist_result.error is not None  # narrow for type-checkers
        return AdapterResult.failed_from_child(hist_result.error, source=src)

    history_rows = hist_result.data.get("items", [])
    history: list[tuple[str, float]] = [
        (r["date"], float(r["close"])) for r in history_rows
    ]
    first_date = history[0][0] if history else "<empty>"

    # Step 4: look up rate per report_period (preserve input order).
    # post-impl loop-1 ISS-019 (CRITICAL silent corruption): yfinance
    # ``<CCY>=X`` returns local-per-USD (e.g. JPY=X close=142.78 on
    # 2024-09-30 means 142.78 JPY per 1 USD). The DL3c contract is
    # ``fx_rate_usd_per_local`` (USD per 1 local) so consumers can
    # ``local_val * rate → usd_val`` (fx_rates.py:66-68 + spec §3.0.2).
    # Pre-fix the adapter stored ``close`` raw, inverting the semantic:
    # 84.9B JPY × 142.78 ≈ 12T (nonsense) instead of 84.9B ÷ 142.78
    # ≈ $595M. The 81 fx_apply unit tests used canned ~0.006 rates
    # that already encoded the inverted semantic, so the test suite
    # passed; only the live MRAAY/NOK path (and the golden-snapshot
    # regenerator at scripts/dev/regen_fx_golden.py which DOES invert
    # — that's where the asymmetry surfaced) exposed the bug. The
    # outlier-detection threshold (10× median) still works correctly
    # on the inverted scale.
    rows: list[FxRate] = []
    for p in report_periods:
        lookup = _lookup_rate_for_date(history, p)
        if lookup is None:
            return AdapterResult.failed(
                code=ErrorCode.PARSE_ERROR,
                detail=(
                    f"fx_history_insufficient: report_period {p} precedes "
                    f"yfinance {yf_ticker} history start {first_date}"
                ),
                source=src, retryable=False,
            )
        close, bar_date, lag = lookup
        # close was already filtered to > 0 by _fetch_yfinance_history;
        # safe to invert.
        rate_usd_per_local = 1.0 / close
        rows.append(FxRate(
            currency=normalized, date=p,
            fx_rate_usd_per_local=rate_usd_per_local,
            source=f"yfinance:{yf_ticker}", bar_date=bar_date, lag_days=lag,
        ))

    # Step 5: outlier guard (invariant 11).
    if detect_outlier_rates([r.fx_rate_usd_per_local for r in rows]):
        return AdapterResult.failed(
            code=ErrorCode.UPSTREAM_ERROR,
            detail="fx_rate_outlier",
            source=src, retryable=False,
        )

    # Step 6: emit.
    items = [dataclasses.asdict(r) for r in rows]
    return AdapterResult.passed(data={"items": items})
