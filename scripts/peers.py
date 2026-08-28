"""Fetch peer valuation multiples via yfinance batch query.

Takes a list of peer tickers (from bq_analysis.json → peer_tickers)
and fetches key valuation multiples in a single batch call.

Same data source (yfinance) ensures consistent accounting basis across peers.
"""

import json
import math
import sys
from typing import Dict, Final, List

from scripts.sources.common import normalize_currency
from scripts.sources.yfinance_guard import yfinance_call

# DL3b §3.1: producer USD-uniformity certificate.
#
# Per-line audit suppression [Cx-R10-G9]: the constant name contains the
# "currency" substring that Pattern W AST Form 1 (AnnAssign with currency-
# identifier target + Constant("USD") value) would otherwise flag. This
# is the canonical sanctioned hardcoded-USD location for the producer's
# USD-uniformity contract — Pattern W AST is forward-looking hardening
# AGAINST accidental hardcodes in OTHER producers, not against this one
# declared certificate. The trailing `# fail-open-ok:` annotation is
# the established suppression mechanism (matched by _line_has_allowlist_comment).
# v6/v7's earlier `_MEDIANS_BASE_CCY` naming-workaround was reverted in
# Cycle 2 — naming-as-suppression is an anti-pattern.
_MEDIANS_BASE_CURRENCY: Final[str] = "USD"  # fail-open-ok: dl3b-base-currency-constant


def _try_fetch(ticker: str, field_map: Dict, yf) -> tuple:
    """Try fetching multiples for a single ticker. Returns (multiples, info).

    Requires at least 2 valid multiples AND a non-null marketCap to
    count as a successful fetch — prevents false matches on wrong
    exchange suffixes.

    Logs the failure reason to stderr so operators can distinguish
    "ticker not on this exchange" (expected — tries next suffix) from
    "yfinance rate-limited us" (actionable) from "DNS failed" (retry).
    Silent bare-except previously reported both cases identically as
    "no valid multiples returned" at the caller — indistinguishable.
    """
    try:
        # yfinance Ticker() is lazy; HTTP fires on .info property access.
        # Wrap the full chain so rate-limit retry/translation actually applies.
        info = yfinance_call(lambda: yf.Ticker(ticker).info) or {}
        if not info.get("marketCap"):
            return {}, {}
        multiples = {}
        for short_name, yf_field in field_map.items():
            val = info.get(yf_field)
            # Round-18 F1: bool is an int subclass — a provider-drifted
            # `trailingPE: true` passed every numeric gate as 1.0 and
            # minted a fabricated 1x multiple into the peer median.
            if (val is not None and isinstance(val, (int, float))
                    and not isinstance(val, bool)):
                # Upper bound mirrors historical_multiples'
                # _ABSURD_MULTIPLE_CAP (probe-2 review round-7): a
                # near-zero-earnings peer's 15,000x P/E is
                # denominator-fragile noise, not a comparable — and two
                # such peers could mint an 'n=3 qualified' absurd median.
                if math.isfinite(val) and 0 < val <= 10_000:
                    # Probe-2 D1: a POSITIVE provider enterpriseToEbitda can
                    # be negative-EV ÷ negative-EBITDA (cash-rich loss-making
                    # issuers) — superficially "cheap", actually meaningless.
                    # Sign-gate on the provider's own ebitda field: absent or
                    # non-positive → drop the ratio (fail-closed; the other
                    # double-negative candidates are already filtered by the
                    # val > 0 gate since one negative factor makes the ratio
                    # negative).
                    if short_name == "ev_ebitda":
                        ebitda = info.get("ebitda")
                        if (ebitda is None or isinstance(ebitda, bool)
                                or not isinstance(ebitda, (int, float))
                                or not math.isfinite(ebitda) or ebitda <= 0):
                            continue
                    multiples[short_name] = round(val, 2)
        if len(multiples) < 2:
            return {}, {}
        return multiples, info
    except Exception as exc:
        print(f"[WARN] peers._try_fetch({ticker}) failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return {}, {}


def _aggregate(peer_recs: Dict, field_map: Dict) -> tuple:
    """Median, contributor count and spread per multiple over `peer_recs`.

    Returns `(medians, sample_size, dispersion)`. Dispersion is
    `{min, max, max_to_min_ratio}`; the ratio is None for a single
    observation — a spread over one peer is not a spread, and a 1.0 there
    would read as a perfectly tight cohort.

    Named failure mode for the dispersion block: SPCX 2026-08-11 published
    a `forward_pe` median of 270.72 over a cohort running 23.35 to
    1414.29, and `ps` 9.44 over 2.56 to 243.99. The median alone reads as
    an anchor; the spread says no peer is anywhere near it. NO verdict
    flag accompanies it (a `medians_usable` was proposed and refused):
    usability turns on identity, accounting basis and business
    comparability, none of which this producer can see.

    Only multiples are aggregated. `market_cap` is currency-bearing and is
    deliberately absent from every set here.
    """
    medians: Dict = {}
    sample_size: Dict = {}
    dispersion: Dict = {}
    for field in field_map:
        values = [
            rec["multiples"][field]
            for rec in peer_recs.values()
            if field in rec.get("multiples", {})
        ]
        if not values:
            continue
        ordered = sorted(values)
        n = len(ordered)
        sample_size[field] = n
        if n % 2 == 1:
            medians[field] = ordered[n // 2]
        else:
            medians[field] = round(
                (ordered[n // 2 - 1] + ordered[n // 2]) / 2, 2,
            )
        lo, hi = ordered[0], ordered[-1]
        dispersion[field] = {
            "min": lo,
            "max": hi,
            # _try_fetch admits only 0 < val <= 10_000, so lo is positive
            # and the ratio is always well defined for n >= 2.
            "max_to_min_ratio": round(hi / lo, 2) if n >= 2 else None,
        }
    return medians, sample_size, dispersion


def fetch_peer_multiples(tickers: List[str]) -> Dict:
    """Fetch valuation multiples for a list of peer tickers.

    Args:
        tickers: List of ticker symbols (e.g., ["NVDA", "INTC", "AVGO"]).

    Returns: Dict with per-ticker multiples, median, and metadata.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance not installed", "peers": {}}

    if not tickers:
        return {"error": "No tickers provided", "peers": {}}

    # Fields to extract from yfinance .info
    field_map = {
        "pe": "trailingPE",
        "forward_pe": "forwardPE",
        "ps": "priceToSalesTrailing12Months",
        "pb": "priceToBook",
        "ev_ebitda": "enterpriseToEbitda",
        "ev_revenue": "enterpriseToRevenue",
        "peg": "pegRatio",
    }

    # Exchange suffixes to try when a bare ticker fails (non-US stocks)
    exchange_suffixes = [".L", ".TO", ".HK", ".T", ".KS", ".AX", ".DE", ".PA"]

    peers = {}
    errors = []

    for ticker in tickers:
        multiples, info = _try_fetch(ticker, field_map, yf)
        resolved_ticker = ticker

        # If bare ticker fails, try common exchange suffixes
        if not multiples and "." not in ticker:
            for suffix in exchange_suffixes:
                candidate = ticker + suffix
                multiples, info = _try_fetch(candidate, field_map, yf)
                if multiples:
                    resolved_ticker = candidate
                    break

        if multiples:
            peers[ticker] = {
                "multiples": multiples,
                "market_cap": info.get("marketCap"),
                # HIGH-6 twin (P0-b): record currency faithfully so downstream
                # peer comparison can gate on `currency == "USD"`. DL3a §2
                # invariant 3 — normalize the raw yfinance value (collapses
                # lowercase / padded / unsupported drift to None so the
                # `!= "USD"` gate fails closed correctly).
                "currency": normalize_currency(info.get("currency")),
            }
            if resolved_ticker != ticker:
                peers[ticker]["resolved_as"] = resolved_ticker
        else:
            errors.append(f"{ticker}: no valid multiples returned")

    # DL3b §3.1: median pass restricted to currency == _MEDIANS_BASE_CURRENCY.
    # Non-USD peers remain in `peers` for audit (raw multiples preserved)
    # but are excluded from the median aggregation.
    #
    # Suffix-resolved peers are ALSO excluded from medians: the exchange-
    # suffix retry binds the first foreign listing that shares the symbol
    # with NO identity re-verification (there is no reference name to check
    # against), so a stale/renamed US peer can resolve to an UNRELATED
    # same-symbol foreign issuer. A USD-quoting foreign line (LSE USD
    # duals) would otherwise pass the currency gate and contaminate the
    # peer-band anchor with a different company's multiples. Audit-only.
    def _median_eligible(rec):
        return (rec.get("currency") == _MEDIANS_BASE_CURRENCY
                and "resolved_as" not in rec)

    usd_peers = {t: rec for t, rec in peers.items() if _median_eligible(rec)}
    # medians_excluded_tickers records the peers-dict KEY (requested ticker
    # form, NOT the suffix-resolved form). Order is fetch order via
    # peers.items() iteration. Cx-R1-M7.
    medians_excluded_tickers = [
        t for t, rec in peers.items() if not _median_eligible(rec)
    ]

    # Per-metric contributor count. A median over a single peer is NOT a peer
    # benchmark — e.g. in a pre-profit peer set only the lone profitable name
    # carries a P/E, so medians["pe"] would silently be that one ticker's value
    # presented identically to a robust N=5 median. Recording n lets the
    # consumer discount single-source medians, mirroring the medians_currency /
    # medians_excluded_tickers reliability metadata (DL3b). The producer only
    # reports N honestly; comparability judgment stays with the consumer.
    medians, medians_sample_size, medians_dispersion = _aggregate(
        usd_peers, field_map,
    )

    # Cross-currency RATIO medians (2026-08-28 thesis-layer report). On SNDK
    # the DL3b currency gate dropped all three non-USD peers and left
    # medians_sample_size == 1 for every metric. But every field in
    # field_map is DIMENSIONLESS — a KRW price over KRW earnings carries no
    # currency — so the units argument for excluding them does not hold.
    # Only `market_cap` is currency-bearing, and it is never aggregated
    # here (a cross-currency market-cap median would need FX conversion,
    # and no demonstrated failure asks for one).
    #
    # This is ADDITIVE. `medians` stays USD-only: it is the sanctioned DL3b
    # anchor and CLAUDE.md requires an RFC to change it. The two sets are
    # separately named so the consumer chooses explicitly; the valuation
    # prompt says which is which.
    #
    # `resolved_as` peers stay excluded from BOTH. That gate is an IDENTITY
    # argument, not a currency one: the exchange-suffix retry binds the
    # first foreign listing sharing the symbol with no name re-verification,
    # so the record may be a different company entirely.
    identified_peers = {
        t: rec for t, rec in peers.items() if "resolved_as" not in rec
    }
    (cross_currency_ratio_medians,
     cross_currency_ratio_sample_size,
     cross_currency_ratio_dispersion) = _aggregate(identified_peers, field_map)

    import datetime as _dt

    result = {
        "peers": peers,                                       # unchanged: ALL fetched
        "medians": medians,                                   # NOW: USD-only
        "medians_currency": _MEDIANS_BASE_CURRENCY,           # NEW
        "medians_excluded_tickers": medians_excluded_tickers, # NEW
        "medians_sample_size": medians_sample_size,           # NEW: per-metric n
        "medians_dispersion": medians_dispersion,
        "cross_currency_ratio_medians": cross_currency_ratio_medians,
        "cross_currency_ratio_sample_size": cross_currency_ratio_sample_size,
        "cross_currency_ratio_dispersion": cross_currency_ratio_dispersion,
        "tickers_requested": tickers,
        "tickers_succeeded": list(peers.keys()),
        "data_source": "yfinance",
        # Per-FIELD basis (probe 1G): a single global "TTM" string mislabeled
        # forward_pe (forward consensus) and peg (yfinance pegRatio uses a
        # 5-YEAR expected-growth horizon). The horizon must be visible at the
        # consumer so peer-median PEG is never crossed with a 1-year growth
        # rate (that mismatch produced AMD's $368.7 horizon-inflated anchor).
        "basis": {
            "pe": "TTM",
            "ps": "TTM",
            "pb": "MRQ book value",
            "ev_ebitda": "TTM",
            "ev_revenue": "TTM",
            "forward_pe": "FWD (next-FY consensus EPS)",
            "peg": "yfinance pegRatio — 5y expected growth horizon "
                   "(do NOT combine with a 1y growth rate)",
        },
        # As-of provenance (probe 5C): yfinance denominators (book value,
        # growth estimates) can jump far more than market cap across days;
        # without a stamp the jump is indistinguishable from stale reuse.
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    if errors:
        result["errors"] = errors

    return result


def _main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch peer valuation multiples via yfinance."
    )
    parser.add_argument("--tickers", required=True, nargs="+",
                        help="Space-separated peer ticker symbols")
    parser.add_argument("--output", default=None,
                        help="Output file path (default: stdout)")
    args = parser.parse_args()

    result = fetch_peer_multiples(args.tickers)

    from scripts.cli_utils import write_output
    write_output(result, args.output)
    if args.output:
        succeeded = result.get("tickers_succeeded", [])
        errors = result.get("errors", [])
        print(
            f"peers: {len(succeeded)}/{len(args.tickers)} tickers OK"
            f"{f', {len(errors)} errors' if errors else ''}"
            f" → {args.output}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    _main()
