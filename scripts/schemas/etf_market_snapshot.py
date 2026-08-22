"""Typed contract for `A.ETF_MARKET_SNAPSHOT`.

Path: `reports/{T}/{DATE}/data/etf_market_snapshot.json`
Producer: `scripts.macro` (PR.MARKET_SNAPSHOT)
Consumers: PR.PROFILE, PR.STAMP, PR.MANIFEST, the ETF thesis bundle loader.

`strict: true` in the contract. This artifact is the run-day evidence an ETF
buy is argued from, so a drifted shape must surface at the load boundary
rather than reaching a consumer as a plausible-looking hole — a null price
that reads as "no position value" is the shape of the failure.

One access rule is worth stating because the contract names its violation as
a forbidden path: the price lives at `ticker_prices[T]` and its date lives at
`chart_statuses.ticker_prices[T].price_as_of`. There is no nested
`ticker_prices[T].price_as_of`; a consumer reading it would get None on every
fund and silently treat every price as undated. `price_as_of()` below is the
only supported reader.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from scripts.schemas.errors import SchemaError

_ARTIFACT = "etf_market_snapshot.json"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PRICE_STATUSES = frozenset({"PASSED", "FAILED"})
_INDICATOR_STATUSES = frozenset({"PASSED", "TOO_YOUNG", "UNAVAILABLE"})
_MARKET_STATUSES = frozenset({"PASSED", "PARTIAL", "FAILED"})

_RATE_FIELDS = ("fed_funds", "us_10y", "us_5y", "spread_10y_5y")

# Returned by `resolve_snapshot_field` when a path does not resolve. A distinct
# sentinel, not None: `rs_vs_qqq_3m` is legitimately null when that benchmark
# was unavailable, and "present and null" is a different fact from "absent".
ABSENT = object()


def _err(field: str, message: str) -> SchemaError:
    return SchemaError(_ARTIFACT, field, message)


def _finite_number(value, field: str, *, allow_none: bool = True):
    if value is None:
        if allow_none:
            return None
        raise _err(field, "required, got null")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(field, f"expected number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise _err(field, f"must be finite, got {value}")
    return float(value)


def _positive_price(value, field: str):
    v = _finite_number(value, field)
    if v is not None and v <= 0:
        raise _err(field, f"a price must be positive, got {v}")
    return v


def _mapping(value, field: str) -> Mapping:
    if not isinstance(value, dict):
        raise _err(field, f"expected object, got {type(value).__name__}")
    return value


def _iso_date_or_none(value, field: str):
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise _err(field, f"expected an ISO date (YYYY-MM-DD), got {value!r}")
    return value


@dataclass(frozen=True)
class EtfMarketSnapshot:
    raw: Mapping[str, Any]

    def price_usd(self, ticker: str):
        """`F.SNAPSHOT.PRICE_USD`. None for a ticker the snapshot does not
        carry — absent is not zero."""
        return self.raw["ticker_prices"].get(ticker)

    def price_as_of(self, ticker: str):
        """`F.SNAPSHOT.PRICE_AS_OF`, read from the status block. The ONLY
        supported reader; see the module docstring on the forbidden path."""
        st = self.raw["chart_statuses"]["ticker_prices"].get(ticker)
        return st.get("price_as_of") if isinstance(st, dict) else None

    def price_status(self, ticker: str) -> str:
        st = self.raw["chart_statuses"]["ticker_prices"].get(ticker)
        return st.get("status") if isinstance(st, dict) else "FAILED"

    def indicator_status(self, ticker: str) -> dict:
        st = self.raw.get("ticker_indicator_status", {}).get(ticker)
        if not isinstance(st, dict):
            return {"status": "UNAVAILABLE", "usable_finite_closes": None}
        return st

    def indicators(self, ticker: str):
        return self.raw.get("ticker_indicators", {}).get(ticker)

    def price_conflict_same_ts(self, ticker: str):
        """Read from the per-ticker STATUS block, where the producer puts it.
        There is no top-level `price_conflict_same_ts` map — the design
        document named one, and reading it there returns None for every
        fund."""
        st = self.raw["chart_statuses"]["ticker_prices"].get(ticker)
        return st.get("price_conflict_same_ts") if isinstance(st, dict) else None

    def rates(self) -> Mapping[str, Any]:
        return self.raw["rates"]

    def benchmark_status(self, symbol: str) -> str:
        st = self.raw["chart_statuses"].get("market", {}).get(symbol)
        return st.get("status") if isinstance(st, dict) else "FAILED"

    def tickers(self) -> tuple[str, ...]:
        return tuple(self.raw["ticker_prices"])


def resolve_snapshot_field(snapshot: Mapping[str, Any], path: str,
                           ticker: str):
    """Resolve one `REQUIRED_SNAPSHOT_FIELDS` path against a snapshot.

    Paths are written with a literal `[T]` placeholder
    (`ticker_indicators[T].macd.macd_line`); `ticker` fills it. Returns
    `ABSENT` when any segment is missing, so a caller can tell an absent leaf
    from a present-but-null one.
    """
    cursor: Any = snapshot
    for part in path.replace("[T]", f".{ticker}").split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return ABSENT
        cursor = cursor[part]
    return cursor


def validate_etf_market_snapshot(raw: Any) -> EtfMarketSnapshot:
    if not isinstance(raw, dict):
        raise _err("<root>", f"expected object, got {type(raw).__name__}")

    prices = raw.get("ticker_prices")
    if not isinstance(prices, dict):
        raise _err("ticker_prices",
                   f"expected object, got {type(prices).__name__}")

    chart = raw.get("chart_statuses")
    if not isinstance(chart, dict):
        raise _err("chart_statuses",
                   f"expected object, got {type(chart).__name__}")
    price_statuses = _mapping(chart.get("ticker_prices"),
                              "chart_statuses.ticker_prices")

    for ticker, price in prices.items():
        fp = f"ticker_prices.{ticker}"
        value = _positive_price(price, fp)
        st = price_statuses.get(ticker)
        if not isinstance(st, dict):
            raise _err(f"chart_statuses.ticker_prices.{ticker}",
                       "every priced ticker needs a status block")
        status = st.get("status")
        if status not in _PRICE_STATUSES:
            raise _err(f"chart_statuses.ticker_prices.{ticker}.status",
                       f"{status!r} is outside {sorted(_PRICE_STATUSES)}")
        _iso_date_or_none(st.get("price_as_of"),
                          f"chart_statuses.ticker_prices.{ticker}.price_as_of")
        # A PASSED status with no price is the shape that reads downstream as
        # a real, zero-valued position. Missing data is a failure, not zero.
        if status == "PASSED" and value is None:
            raise _err(fp, "status is PASSED but the price is null; a "
                           "successful fetch must carry a price")

    ind_status = raw.get("ticker_indicator_status")
    if ind_status is not None:
        _mapping(ind_status, "ticker_indicator_status")
        for ticker, st in ind_status.items():
            fp = f"ticker_indicator_status.{ticker}"
            _mapping(st, fp)
            if st.get("status") not in _INDICATOR_STATUSES:
                raise _err(fp, f"{st.get('status')!r} is outside "
                               f"{sorted(_INDICATOR_STATUSES)}")
            closes = st.get("usable_finite_closes")
            if closes is not None:
                if isinstance(closes, bool) or not isinstance(closes, int):
                    raise _err(f"{fp}.usable_finite_closes",
                               f"expected int, got {type(closes).__name__}")
                if closes < 0:
                    raise _err(f"{fp}.usable_finite_closes",
                               f"cannot be negative, got {closes}")

    conflicts = raw.get("price_conflict_same_ts")
    if conflicts is not None:
        _mapping(conflicts, "price_conflict_same_ts")
        for ticker, flag in conflicts.items():
            if not isinstance(flag, bool):
                raise _err(f"price_conflict_same_ts.{ticker}",
                           f"expected bool, got {type(flag).__name__}")

    rates = _mapping(raw.get("rates"), "rates")
    for name in _RATE_FIELDS:
        _finite_number(rates.get(name), f"rates.{name}")

    market = chart.get("market")
    if market is not None:
        _mapping(market, "chart_statuses.market")
        for symbol, st in market.items():
            fp = f"chart_statuses.market.{symbol}"
            _mapping(st, fp)
            if st.get("status") not in _MARKET_STATUSES:
                raise _err(f"{fp}.status", f"{st.get('status')!r} is outside "
                                           f"{sorted(_MARKET_STATUSES)}")

    return EtfMarketSnapshot(raw=raw)


def load_etf_market_snapshot(path: str | Path) -> EtfMarketSnapshot:
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _err("<file>", f"unreadable: {exc}") from exc
    return validate_etf_market_snapshot(raw)
