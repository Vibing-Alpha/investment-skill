"""Retry + 429-translation shim for yfinance-library calls.

yfinance uses its own HTTP stack (the `requests` library internally), so
HttpPolicy (which governs urllib) cannot reach it. This module provides
an analogous retry + backoff wrapper for yfinance-based calls.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


# ISS-128 (Loop9 cycle 1): canonical ticker validator.
#
# yfinance internally builds chart URLs as
# `https://query1.finance.yahoo.com/v8/finance/chart/{ticker}` without
# percent-encoding the path segment, so a ticker like
# `"../../etc/passwd?x=y"` traverses path/query into the third-party
# library's HTTP stack — bypassing this project's `http_get` quoting,
# SSRF policy, and redaction. The yfinance library is out of our
# control; the only defensible boundary is to validate adapter args
# BEFORE they reach `yf.Ticker(...)`.
#
# Allowed: uppercase A-Z, digits, dot (BRK.B), hyphen (RDS-A),
# equals + circumflex (futures / index symbols like ^GSPC, ES=F),
# 1-12 chars total. Rejects: empty, control chars, whitespace,
# slashes, query separators, percent-encoding, URL fragments.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.\-=^]{1,12}$")


class InvalidTickerError(ValueError):
    """Raised when an adapter receives a ticker that fails
    yfinance-safe validation. Callers should map this to a FAILED
    AdapterResult with code=ErrorCode.SHAPE_MISMATCH (or PARSE_ERROR
    for the legacy semantics) before the value reaches yfinance.
    """


def validate_yfinance_ticker(ticker: str) -> str:
    """Return the upper-cased ticker if it matches the canonical
    yfinance ticker pattern; raise InvalidTickerError otherwise.
    """
    if not isinstance(ticker, str):
        raise InvalidTickerError(
            f"ticker must be str, got {type(ticker).__name__}"
        )
    upper = ticker.strip().upper()
    if not _TICKER_PATTERN.fullmatch(upper):
        raise InvalidTickerError(
            f"invalid ticker {ticker!r} — must match {_TICKER_PATTERN.pattern}"
        )
    return upper


@dataclass(frozen=True)
class YfPolicy:
    max_retries: int = 3
    backoff_base_s: float = 2.0
    backoff_jitter_s: float = 1.0
    honor_retry_after: bool = False  # yfinance does not expose upstream headers


DEFAULT_YF_POLICY = YfPolicy()


class YfCallError(Exception):
    """All yfinance exceptions are translated to this type."""


class YfRateLimitError(YfCallError):
    """Rate-limited (duck-typed from exception class name or message)."""


_RATE_LIMIT_SIGNALS = (
    "rate limit",
    "rate-limited",
    "too many requests",
    "yfratelimiterror",
)


def _looks_like_rate_limit(e: BaseException) -> bool:
    cls_name = type(e).__name__.lower()
    msg = str(e).lower()
    return cls_name == "yfratelimiterror" or any(s in msg for s in _RATE_LIMIT_SIGNALS)


def yfinance_call(fn: Callable[[], T], *, policy: YfPolicy = DEFAULT_YF_POLICY) -> T:
    """Execute a yfinance callable with retry + 429 translation.

    Usage:
        info = yfinance_call(lambda: yf.Ticker(ticker).info)
    """
    last_error: Exception | None = None
    for attempt in range(policy.max_retries):
        try:
            return fn()
        except (KeyboardInterrupt, SystemExit):
            # H3 fix: don't swallow interpreter-abort signals. A Ctrl-C or
            # pytest SystemExit MUST propagate, not be wrapped as YfCallError.
            raise
        except Exception as e:
            last_error = e
            is_rate_limit = _looks_like_rate_limit(e)
            if attempt < policy.max_retries - 1 and is_rate_limit:
                sleep_s = policy.backoff_base_s * (2 ** attempt) + random.uniform(0, policy.backoff_jitter_s)
                time.sleep(sleep_s)
                continue
            if is_rate_limit:
                raise YfRateLimitError(
                    f"yfinance rate limited after {attempt + 1} attempts: {e}"
                ) from e
            raise YfCallError(f"yfinance call failed: {e}") from e
    raise YfCallError(f"yfinance call failed: {last_error}")  # defensive; unreachable
