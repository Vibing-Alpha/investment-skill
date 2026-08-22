"""PR.DETECT — instrument identity for the ETF path.

Answers one question per ticker: is this an ETF, an ordinary equity, or
unresolved? Two sources must agree (WF.IDENTITY.verdict_table); anything else
is `unknown`, and `unknown` never falls through to stock authoring or to a
buy authorization.

Outputs
-------
A.INSTRUMENT_REGISTRY  reports/{T}/instrument_type.json      (per-ticker cache)
A.IDENTITY_MAP         reports/{portfolio,monitor}/{DATE}/.etf_identity.json

CLI
---
    python3 -m scripts.etf.detect --ticker SOXX [--root .]
    python3 -m scripts.etf.detect --tickers SOXX,AMD --output <map path>

Exit codes are part of the contract:
    single  0 resolved | 1 structured refusal (no traceback) | 2 arg error
    prepass 0 map written (per-ticker refusals included) | 1 not written | 2 arg
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from scripts.cli_utils import write_output
from scripts.schemas.instrument_registry import (
    REGISTRY_OUTPUT_VERSION,
    load_instrument_registry,
)

# C.REGISTRY_MAX_AGE_DAYS. A cached verdict older than this is re-probed:
# funds are reorganised, delisted, and reclassified, and a 90-day-old "equity"
# verdict on a converted fund would route it back down the stock path.
ETF_REGISTRY_MAX_AGE_DAYS = 90

_PREFIX = "etf.detect"

# Set only by `_install_offline_fakes`. Read by `_fmp_verdict` /
# `_yfinance_verdict` (to mark what they emit) and by `_read_cached` (to reject
# what a fake run left behind).
_FAKE_SOURCES = False


# ---------------------------------------------------------------------------
# source probes — the only two network surfaces, both isolated so tests and
# the offline CLI can substitute them without patching the network layer
# ---------------------------------------------------------------------------

def _fetch_fmp_etf_info(ticker: str, *, fmp_api_key: str = ""):
    """WF.AUTHORING: 'FMP uses PR.FMP_ETF_INFO' — no direct HTTP here."""
    from scripts.sources.fmp import fetch_etf_info
    return fetch_etf_info(ticker, fmp_api_key=fmp_api_key or _fmp_key())


def _fetch_yfinance_quote_type(ticker: str):
    """WF.AUTHORING: 'yfinance uses yfinance_guard.yfinance_call'.

    Returns `(quote_type, currency)`. Raises on a source error (rate limit,
    transport). The caller turns that into `unknown` — never into `equity`,
    which is what a bare `.get()` returning None would have produced.

    The CURRENCY is read because this system is US-market-only and the ETF
    valuation stack has no FX layer: a fund has no statement currency to
    convert FROM, so every money field is taken at face value and labelled
    USD. A London line quoted in GBp (pence) therefore writes its pence price
    into `price_usd` — measured on ISF.L at 1060.0, about 79x its true USD
    value — and into the liquidity screen that gates entry.
    """
    import yfinance as yf

    from scripts.sources.yfinance_guard import validate_yfinance_ticker, yfinance_call

    safe = validate_yfinance_ticker(ticker)
    info = yfinance_call(lambda: yf.Ticker(safe).info)
    if not isinstance(info, Mapping):
        return None, None
    qt = info.get("quoteType")
    cur = info.get("currency")
    return (qt if isinstance(qt, str) and qt else None,
            cur if isinstance(cur, str) and cur else None)


def _fmp_key() -> str:
    return os.environ.get("FMP_API_KEY", "")


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

def _fmp_verdict(ticker: str) -> dict:
    """Typed source verdict for FMP. `PASSED` carries `item_count`; the
    identity meaning of that count is applied in `_combine`, so a transport
    failure can never be mistaken for a zero-row success."""
    try:
        res = _fetch_fmp_etf_info(ticker)
    except Exception as exc:  # noqa: BLE001 — a batch must survive one bad row
        return {"status": "FAILED", "code": "internal_error", "detail": str(exc)[:200]}
    if getattr(res, "ok", False):
        items = (res.data or {}).get("items")
        if not isinstance(items, list):
            return {"status": "FAILED", "code": "shape_mismatch",
                    "detail": "data.items is not a list"}
        verdict = {"status": "PASSED", "item_count": len(items)}
        if _FAKE_SOURCES:
            verdict["fake"] = True
        return verdict
    err = getattr(res, "error", None)
    code = getattr(getattr(err, "code", None), "value", None) or "upstream_error"
    return {"status": "FAILED", "code": code,
            "detail": (getattr(err, "detail", "") or "")[:200]}


def _yfinance_verdict(ticker: str) -> dict:
    try:
        result = _fetch_yfinance_quote_type(ticker)
    except Exception as exc:  # noqa: BLE001 — source error, not a classification
        return {"status": "FAILED", "code": type(exc).__name__,
                "detail": str(exc)[:200]}
    qt, currency = result if isinstance(result, tuple) else (result, None)
    if qt is None:
        return {"status": "FAILED", "code": "quote_type_absent"}
    # Non-USD quotation is a REFUSAL, not a note. Measured 13/13 coverage on
    # US funds, ADRs and foreign lines, so an absent currency is drift rather
    # than a normal shape — and both fail closed.
    if currency is None:
        return {"status": "FAILED", "code": "currency_absent",
                "quote_type": qt}
    if currency.upper() != "USD":
        return {"status": "FAILED", "code": "non_usd_quotation",
                "quote_type": qt, "currency": currency,
                "detail": (f"quoted in {currency}; this system is US-market "
                           f"only and has no FX layer for funds, so every "
                           f"money field would be labelled USD at face value")}
    verdict = {"status": "PASSED", "quote_type": qt, "currency": currency}
    if _FAKE_SOURCES:
        verdict["fake"] = True
    return verdict


def _combine(fmp: Mapping[str, Any], yfin: Mapping[str, Any]) -> str:
    """WF.IDENTITY.verdict_table, executed exactly as written.

    | FMP                | yfinance quoteType | result |
    | PASSED and nonempty| ETF                | etf    |
    | PASSED and empty   | EQUITY             | equity |
    | anything else      | anything else      | unknown|

    Both legs must agree. The measured KRMA case — FMP `200 + non-empty`
    against yfinance `EQUITY` — lands in row 3, which is the point: a
    provider disagreement is not a tiebreak, it is an unresolved identity.
    """
    fmp_passed = fmp.get("status") == "PASSED"
    count = fmp.get("item_count")
    qt = yfin.get("quote_type") if yfin.get("status") == "PASSED" else None
    if fmp_passed and isinstance(count, int) and count > 0 and qt == "ETF":
        return "etf"
    if fmp_passed and count == 0 and qt == "EQUITY":
        return "equity"
    return "unknown"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(ts: datetime.datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# registry cache
# ---------------------------------------------------------------------------

def registry_path(ticker: str, *, root: Path) -> Path:
    return Path(root) / "reports" / ticker / "instrument_type.json"


def _read_cached(ticker: str, *, root: Path) -> Optional[str]:
    """A.INSTRUMENT_REGISTRY.read.

    valid and age < C.REGISTRY_MAX_AGE_DAYS  -> authoritative
    absent / expired / unreadable / invalid / version-mismatched -> re-probe

    Returns the cached instrument_type, or None meaning "re-probe". Every
    failure mode collapses to None on purpose: a half-understood cache entry
    must cost a network call, not a guess.
    """
    path = registry_path(ticker, root=root)
    try:
        reg = load_instrument_registry(path)
    except (FileNotFoundError, ValueError, OSError):
        return None
    if reg.output_version != REGISTRY_OUTPUT_VERSION:
        return None
    if reg.ticker != ticker:
        return None
    if reg.age_days(now=_now()) >= ETF_REGISTRY_MAX_AGE_DAYS:
        return None
    for verdict in (reg.fmp_verdict, reg.yfinance_verdict):
        if isinstance(verdict, dict) and verdict.get("fake"):
            return None
    return reg.instrument_type


def _write_registry(ticker: str, row: Mapping[str, Any], *, root: Path) -> Path:
    path = registry_path(ticker, root=root)
    write_output({
        "ticker": ticker,
        "instrument_type": row["instrument_type"],
        "fmp_verdict": row["source_verdicts"]["fmp"],
        "yfinance_verdict": row["source_verdicts"]["yfinance"],
        "resolved_at": row["resolved_at"],
        "output_version": REGISTRY_OUTPUT_VERSION,
    }, str(path))
    return path


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def resolve_identity(ticker: str, *, root: Path | str = ".",
                     vendor_symbol: Optional[str] = None,
                     use_cache: bool = True) -> dict:
    """Resolve one ticker to an identity row.

    `vendor_symbol` is what the sources are asked about; `ticker` is the
    canonical name the row is filed under. They differ when portfolio state
    carries a vendor alias (BRK.B vs BRK-B).
    """
    root = Path(root)
    probe_symbol = vendor_symbol or ticker

    if use_cache:
        cached = _read_cached(ticker, root=root)
        if cached is not None:
            return {
                "ticker": ticker,
                "instrument_type": cached,
                "resolution_status": "resolved" if cached != "unknown" else "refused",
                "source_verdicts": {"fmp": {"status": "CACHED"},
                                    "yfinance": {"status": "CACHED"}},
                "resolved_at": _stamp(_now()),
                "output_version": REGISTRY_OUTPUT_VERSION,
                "from_cache": True,
            }

    fmp = _fmp_verdict(probe_symbol)
    yfin = _yfinance_verdict(probe_symbol)
    itype = _combine(fmp, yfin)
    row = {
        "ticker": ticker,
        "instrument_type": itype,
        "resolution_status": "resolved" if itype != "unknown" else "refused",
        "source_verdicts": {"fmp": fmp, "yfinance": yfin},
        "resolved_at": _stamp(_now()),
        "output_version": REGISTRY_OUTPUT_VERSION,
        "from_cache": False,
    }
    if itype != "unknown":
        # Only a resolved verdict is worth caching. Persisting `unknown` would
        # turn a transient outage into 90 days of refusals.
        _write_registry(ticker, row, root=root)
    return row


def resolve_batch(tickers, *, output_path: Path | str,
                  root: Path | str = ".",
                  vendor_alias_map: Optional[Mapping[str, str]] = None) -> int:
    """A.IDENTITY_MAP — one atomic whole-map write.

    Batch contract: a per-ticker refusal is represented in the map, one failed
    ticker never aborts the batch, and batch success means the map was written.
    """
    alias = dict(vendor_alias_map or {})
    rows: dict[str, dict] = {}
    for t in tickers:
        if not isinstance(t, str) or not t.strip():
            continue
        t = t.strip()
        try:
            rows[t] = resolve_identity(t, root=root, vendor_symbol=alias.get(t))
        except Exception as exc:  # noqa: BLE001 — contract: never abort the batch
            print(f"{_PREFIX}: {t} refused: {exc}", file=sys.stderr)
            rows[t] = {
                "ticker": t,
                "instrument_type": "unknown",
                "resolution_status": "refused",
                "source_verdicts": {"fmp": {"status": "FAILED",
                                            "code": "internal_error"},
                                    "yfinance": {"status": "FAILED",
                                                 "code": "internal_error"}},
                "resolved_at": _stamp(_now()),
                "output_version": REGISTRY_OUTPUT_VERSION,
                "from_cache": False,
            }
    try:
        write_output({"rows": rows, "output_version": REGISTRY_OUTPUT_VERSION},
                     str(output_path))
    except OSError as exc:
        print(f"{_PREFIX}: identity map not written: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _install_offline_fakes() -> None:
    """ETF_DETECT_FAKE=1 substitutes both source probes with deterministic
    stubs. The CLI's exit codes and stdout shape are part of the contract and
    must be testable without a network or an API key.

    The seam is deliberately self-marking. A fake verdict carries
    `fake: True`, and `_read_cached` refuses any cached row carrying it. Without
    that, an env var leaking into a real environment would resolve every ticker
    as `etf` and persist it for C.REGISTRY_MAX_AGE_DAYS — a fabricated identity
    outliving the mistake that produced it, on the path that decides whether a
    ticker is even allowed to be bought.
    """
    from scripts.sources.adapter_result import AdapterResult, ErrorCode

    print(f"{_PREFIX}: WARNING — ETF_DETECT_FAKE=1, sources are stubs; "
          f"verdicts are not real and will not be cached", file=sys.stderr)

    def fake_fmp(ticker, **_):
        if ticker == "REFUSE":
            return AdapterResult.failed(code=ErrorCode.HTTP_TRANSPORT,
                                        detail="offline fake", source="fake")
        return AdapterResult.passed({"items": [{"symbol": ticker}]},
                                    meta={"fake": True})

    def fake_yf(ticker):
        return "ETF", "USD"

    globals()["_fetch_fmp_etf_info"] = fake_fmp
    globals()["_fetch_yfinance_quote_type"] = fake_yf
    globals()["_FAKE_SOURCES"] = True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.etf.detect", description=__doc__)
    ap.add_argument("--ticker", help="resolve one ticker")
    ap.add_argument("--tickers", help="comma-separated prepass batch")
    ap.add_argument("--output", help="identity-map path (required with --tickers)")
    ap.add_argument("--root", default=".", help="repo root holding reports/")
    ap.add_argument("--aliases", help="JSON object mapping ticker -> vendor symbol")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore an existing registry entry and re-probe")
    args = ap.parse_args(argv)

    if os.environ.get("ETF_DETECT_FAKE") == "1":
        _install_offline_fakes()

    if bool(args.ticker) == bool(args.tickers):
        print(f"{_PREFIX}: pass exactly one of --ticker / --tickers",
              file=sys.stderr)
        return 2
    if args.ticker is not None and not args.ticker.strip():
        print(f"{_PREFIX}: --ticker must be non-empty", file=sys.stderr)
        return 2

    aliases: dict[str, str] = {}
    if args.aliases:
        try:
            aliases = json.loads(args.aliases)
            if not isinstance(aliases, dict):
                raise ValueError("not a JSON object")
        except ValueError as exc:
            print(f"{_PREFIX}: --aliases is not a JSON object: {exc}",
                  file=sys.stderr)
            return 2

    root = Path(args.root)

    if args.tickers:
        if not args.output:
            print(f"{_PREFIX}: --tickers requires --output", file=sys.stderr)
            return 2
        names = [t.strip() for t in args.tickers.split(",") if t.strip()]
        if not names:
            print(f"{_PREFIX}: --tickers listed no ticker", file=sys.stderr)
            return 2
        return resolve_batch(names, output_path=args.output, root=root,
                             vendor_alias_map=aliases)

    ticker = args.ticker.strip()
    try:
        row = resolve_identity(ticker, root=root,
                               vendor_symbol=aliases.get(ticker),
                               use_cache=not args.no_cache)
    except Exception as exc:  # noqa: BLE001 — structured refusal, no traceback
        print(json.dumps({"ticker": ticker, "instrument_type": "unknown",
                          "resolution_status": "refused",
                          "detail": str(exc)[:300]}, ensure_ascii=False))
        print(f"{_PREFIX}: {ticker} refused: {exc}", file=sys.stderr)
        return 1

    payload = dict(row)
    if row["instrument_type"] != "unknown":
        # WF.IDENTITY: "paths printed on stdout use Path.as_posix" — a native
        # Windows `str(Path)` interpolated into a shell heredoc parses its
        # backslashes as escapes.
        payload["registry_path"] = registry_path(ticker, root=root).as_posix()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if row["resolution_status"] == "resolved" else 1


if __name__ == "__main__":
    sys.exit(main())
