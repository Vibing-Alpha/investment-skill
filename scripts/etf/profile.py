"""PR.PROFILE — the structural facts an ETF entry decision rests on, and the
verdict `P.ENTRY_ELIGIBILITY` derives from them.

The governing rule is one-way: **quantitative screens may refuse or preserve
uncertainty, and can never grant `pass` by themselves.** A fund with a perfect
allocation vector, no concentration and deep liquidity is still `unknown`
until the owner has approved it. Provider taxonomy is weaker still — it can
add leverage evidence, never advance a ticker toward entry.

Three outcomes, and the middle one carries most of the traffic:

    block    the fund IS something we do not buy (leveraged or inverse)
    unknown  something needed is unavailable, stale, or ambiguous
    pass     owner approval is current and every refusal condition is false

`unknown` is not a soft `pass`. Nothing downstream may enter on it.

CLI:
    python3 -m scripts.etf.profile --ticker T --identity-registry PATH \\
        --market-snapshot PATH --compiled-strategy PATH --output PATH
"""

from __future__ import annotations

import argparse
import datetime
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from scripts.cli_utils import normalize_ticker, write_output
from scripts.etf.leverage_tokens import leverage_scan
from scripts.etf.policy import etf_policy_approval

# --- constants (C.*) -------------------------------------------------------
# Every one is a policy choice informed by measurement; the measurement that
# set it is named where it is used.
ETF_STOCK_FRAC_MIN = 0.9
ETF_CASH_FRAC_MAX = 0.1
ETF_NON_EQUITY_FRAC_MAX = 0.1
ETF_ALLOCATION_BUCKET_MIN = -0.02
ETF_ALLOCATION_BUCKET_MAX = 1.02
ETF_MAX_HOLDING_WEIGHT_REFUSE = 0.5
RENDER_WEIGHT_MIN = 0.0
RENDER_WEIGHT_MAX = 1.0
GATING_WEIGHT_MAX = 1.05
PRICE_MAX_AGE_SESSIONS = 1
# C.PROFILE_MAX_AGE_DAYS is defined as equal to C.APPROVAL_MAX_AGE_DAYS
# ("safety-equivalence"): a profile is the structural half of the same
# judgement the approval is the human half of, so they age together.
from scripts.etf.policy import ETF_APPROVAL_MAX_AGE_DAYS  # noqa: E402
ETF_PROFILE_MAX_AGE_DAYS = ETF_APPROVAL_MAX_AGE_DAYS

_PREFIX = "etf.profile"

# yfinance `funds_data.asset_classes` keys -> the canonical bucket names.
_ALLOCATION_KEYS = {
    "cashPosition": "cash",
    "stockPosition": "stock",
    "bondPosition": "bond",
    "preferredPosition": "preferred",
    "convertiblePosition": "convertible",
    "otherPosition": "other",
}
_NON_EQUITY_BUCKETS = ("bond", "preferred", "convertible", "other")

# Closed vocabularies for the eligibility inputs. Named here rather than
# imported from the schema so this predicate stays a pure function of its
# arguments with no artifact dependency.
_INSTRUMENT_TYPES = frozenset({"etf", "equity", "unknown"})
_APPROVAL_STATUSES = frozenset({"current", "not_listed", "expired", "invalid"})
_LEVERAGE_STATUSES = frozenset({"suspected", "not_suspected", "unknown"})
_VECTOR_STATUSES = frozenset({"valid", "invalid", "missing"})


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


# ---------------------------------------------------------------------------
# asset allocation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllocationResult:
    status: str                       # valid | invalid | missing
    vector: Optional[dict]
    stock_frac: float
    cash_frac: float
    non_equity_frac: float
    allocation_sum_frac: Optional[float]
    reasons: tuple[str, ...] = ()


def build_asset_allocation(raw) -> AllocationResult:
    """Project the provider's allocation vector and judge it.

    Every bucket must be PRESENT, finite, and inside
    [C.ALLOCATION_BUCKET_MIN, C.ALLOCATION_BUCKET_MAX]. Presence is checked
    because a hole is not a zero: GULF was measured returning four `None`
    buckets beside `stock 1.0`, and reading those as zero would report a
    fully-invested equity fund whose composition nobody knows.

    The bucket bound is where the return-stacked family is refused — RSST's
    bond leg measured -1.9106 — while the ordinary tail survives: at zero
    tolerance 35 of 400 ordinary funds refused, 25 of them solely here.
    """
    if raw is None:
        return AllocationResult("missing", None, 0.0, 0.0, 0.0, None,
                                ("provider returned no allocation vector",))
    if not isinstance(raw, dict):
        return AllocationResult("invalid", None, 0.0, 0.0, 0.0, None,
                                (f"allocation is {type(raw).__name__}, "
                                 f"not a mapping",))

    vector: dict[str, float] = {}
    reasons: list[str] = []
    # Read the raw values first, THEN judge them. The sum below is provenance
    # — it records what the provider actually sent — so it must not be
    # computed from the post-bounds survivors, or an out-of-range vector would
    # report a sum it never had.
    raw_values: dict[str, float] = {}
    for provider_key, name in _ALLOCATION_KEYS.items():
        if provider_key not in raw:
            reasons.append(f"bucket {name} absent")
            continue
        value = raw[provider_key]
        if not _finite(value):
            reasons.append(f"bucket {name} is {value!r}, not a finite number")
            continue
        raw_values[name] = float(value)
        if not (ETF_ALLOCATION_BUCKET_MIN <= value <= ETF_ALLOCATION_BUCKET_MAX):
            reasons.append(
                f"bucket {name} is {value}, outside "
                f"[{ETF_ALLOCATION_BUCKET_MIN}, {ETF_ALLOCATION_BUCKET_MAX}]")
            continue
        vector[name] = float(value)

    # Only meaningful when every bucket was readable; a sum over a subset is
    # not the fund's allocation.
    raw_sum = (sum(raw_values.values())
               if len(raw_values) == len(_ALLOCATION_KEYS) else None)

    if reasons:
        return AllocationResult("invalid", None, 0.0, 0.0, 0.0, raw_sum,
                                tuple(reasons))

    stock = vector["stock"]
    cash = vector["cash"]
    non_equity = sum(vector[b] for b in _NON_EQUITY_BUCKETS)
    return AllocationResult("valid", vector, stock, cash, non_equity, raw_sum)


# ---------------------------------------------------------------------------
# top holdings + concentration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HoldingsResult:
    status: str                        # valid | invalid | missing
    top_holdings: Optional[list]
    coverage_pct: Optional[float]
    reasons: tuple[str, ...] = ()


def _usable_rows(rows) -> list:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [r for r in rows if isinstance(r, dict)]


def build_top_holdings(rows) -> HoldingsResult:
    """The RENDERING vector: what a human is shown, and the coverage it adds
    up to. Invalidating it is cheap — it withholds a display, not a decision.
    `max_holding_weight` is derived separately and deliberately survives this.
    """
    if rows is None:
        return HoldingsResult("missing", None, None,
                              ("provider returned no holdings",))
    usable = _usable_rows(rows)
    if not usable:
        return HoldingsResult("missing", None, None,
                              ("provider returned no holdings rows",))

    reasons: list[str] = []
    seen: set[str] = set()
    projected: list[dict] = []
    total = 0.0
    for row in usable:
        symbol = row.get("symbol")
        weight = row.get("weight")
        if not isinstance(symbol, str) or not symbol.strip():
            reasons.append(f"row symbol is {symbol!r}")
            continue
        symbol = symbol.strip()
        if symbol in seen:
            reasons.append(f"duplicate symbol {symbol}")
            continue
        seen.add(symbol)
        if not _finite(weight):
            reasons.append(f"weight for {symbol} is {weight!r}, not finite")
            continue
        if not (RENDER_WEIGHT_MIN <= weight <= RENDER_WEIGHT_MAX):
            reasons.append(
                f"weight for {symbol} is {weight}, outside "
                f"[{RENDER_WEIGHT_MIN}, {RENDER_WEIGHT_MAX}]")
            continue
        total += weight
        # Converted to percent exactly once, here. A second multiplication
        # somewhere downstream is the classic way a 8.7% holding becomes 869%.
        projected.append({"symbol": symbol, "weight_pct": weight * 100.0})

    if reasons:
        return HoldingsResult("invalid", None, None, tuple(reasons))
    if total > 1.0001:
        return HoldingsResult("invalid", None, None,
                              (f"weights sum to {total}, above 1.0001",))
    if len(projected) == 1 and total < 0.50:
        # A lone minor row is a truncated response, not a one-holding fund.
        # EWSC returned exactly one row at 0.000057.
        return HoldingsResult("invalid", None, None,
                              (f"single row covering only {total}",))
    return HoldingsResult("valid", projected, total * 100.0)


def max_holding_weight(rows) -> Optional[float]:
    """The concentration screen's operand — the largest row that is itself
    plausible, judged independently of the rendering vector's status.

    Deliberately NOT coupled to `holdings_vector_status`: coupling them would
    null the concentration screen precisely on the funds whose data is worst,
    and this screen is what refuses a fund-of-funds structure. The gating
    bound (1.05) is looser than the rendering bound (1.0) because real rows
    sit just above 1.0 — RYLD's dominant row measured 1.008985.
    """
    eligible = [r.get("weight") for r in _usable_rows(rows)]
    weights = [float(w) for w in eligible
               if _finite(w) and 0.0 <= w <= GATING_WEIGHT_MAX]
    return max(weights) if weights else None


# ---------------------------------------------------------------------------
# liquidity
# ---------------------------------------------------------------------------

def avg_dollar_volume_usd(*, avg_volume_shares, price_usd,
                          price_as_of, run_date: datetime.date):
    """Average shares times the run-day price, or None.

    None rather than zero on every unusable operand: a zero here reads
    downstream as an illiquid fund rather than an unknown one, and the
    eligibility table refuses those differently.

    A price older than C.PRICE_MAX_AGE_SESSIONS nulls the result — a dollar
    volume computed from a stale price measures a market that has moved.
    Sessions, not calendar days: a Friday close read on Sunday is 0 old.
    """
    from scripts.delta.calendar import trading_days_between
    from scripts.schemas.strategy import parse_canonical_iso_date

    if not (_finite(avg_volume_shares) and avg_volume_shares > 0):
        return None
    if not (_finite(price_usd) and price_usd > 0):
        return None
    as_of = parse_canonical_iso_date(price_as_of)
    if as_of is None:
        return None
    if as_of > run_date:
        # `trading_days_between` returns 0 when end <= start, so a price dated
        # AFTER the run read as zero sessions old — a corrupt or clock-skewed
        # quote priced the liquidity screen as if it were current.
        return None
    if trading_days_between(as_of, run_date) > PRICE_MAX_AGE_SESSIONS:
        return None
    value = float(avg_volume_shares) * float(price_usd)
    return value if math.isfinite(value) and value > 0 else None


# ---------------------------------------------------------------------------
# P.ENTRY_ELIGIBILITY
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EligibilityResult:
    eligibility: str                  # V.ENTRY_ELIGIBILITY
    reasons: tuple[str, ...]          # list<V.REFUSAL_REASON>
    matched_rule: str


def entry_eligibility(*, identity_status: str, approval_status: str,
                      leverage_status: str, asset_allocation_status: str,
                      stock_frac, cash_frac, non_equity_frac,
                      max_holding_weight, avg_dollar_volume_usd
                      ) -> EligibilityResult:
    """The decision table, ordered first match. Order is meaningful: the row
    that fires is the reason the user is shown, so the most specific and most
    actionable finding must come first.

    `leveraged_or_inverse` leads because it is the only row that says what the
    instrument IS rather than what is unknown about it, and it is the only
    `block`.
    """
    # The one-way rule made total. Every row below tests for a NAMED refusal
    # value, so an unrecognized status fell through all of them and reached
    # `pass` — an unreadable status advancing a fund toward entry is the
    # inverse of what these screens are for.
    for name, value, allowed in (
        ("identity_status", identity_status, _INSTRUMENT_TYPES),
        ("approval_status", approval_status, _APPROVAL_STATUSES),
        ("leverage_status", leverage_status, _LEVERAGE_STATUSES),
        ("asset_allocation_status", asset_allocation_status, _VECTOR_STATUSES),
    ):
        if value not in allowed:
            return EligibilityResult("unknown", ("composition_unavailable",),
                                     "D.ELIGIBILITY.UNREADABLE_STATUS")

    if leverage_status == "suspected":
        return EligibilityResult("block", ("leveraged_or_inverse",),
                                 "D.ELIGIBILITY.LEVERAGE")
    if approval_status == "not_listed":
        return EligibilityResult("unknown", ("not_owner_approved",),
                                 "D.ELIGIBILITY.NOT_LISTED")
    if approval_status == "expired":
        return EligibilityResult("unknown", ("owner_approval_expired",),
                                 "D.ELIGIBILITY.APPROVAL_EXPIRED")
    if approval_status == "invalid":
        return EligibilityResult("unknown", ("owner_approval_invalid",),
                                 "D.ELIGIBILITY.APPROVAL_INVALID")
    if asset_allocation_status != "valid":
        return EligibilityResult("unknown", ("composition_unavailable",),
                                 "D.ELIGIBILITY.ALLOCATION_UNAVAILABLE")
    if (not _finite(stock_frac) or stock_frac < ETF_STOCK_FRAC_MIN
            or not _finite(cash_frac) or cash_frac > ETF_CASH_FRAC_MAX
            or not _finite(non_equity_frac)
            or non_equity_frac > ETF_NON_EQUITY_FRAC_MAX):
        return EligibilityResult("unknown", ("composition_out_of_scope",),
                                 "D.ELIGIBILITY.OUT_OF_SCOPE")
    if not _finite(max_holding_weight):
        return EligibilityResult("unknown", ("composition_unavailable",),
                                 "D.ELIGIBILITY.CONCENTRATION_UNAVAILABLE")
    if max_holding_weight >= ETF_MAX_HOLDING_WEIGHT_REFUSE:
        return EligibilityResult("unknown", ("fund_of_funds_structure",),
                                 "D.ELIGIBILITY.DOMINANT_HOLDING")
    if identity_status != "etf":
        return EligibilityResult("unknown", ("identity_unresolved",),
                                 "D.ELIGIBILITY.IDENTITY_UNKNOWN")
    if leverage_status == "unknown":
        return EligibilityResult("unknown", ("leverage_unknown",),
                                 "D.ELIGIBILITY.LEVERAGE_UNKNOWN")
    if not _finite(avg_dollar_volume_usd) or avg_dollar_volume_usd <= 0:
        return EligibilityResult("unknown", ("liquidity_unavailable",),
                                 "D.ELIGIBILITY.LIQUIDITY_UNKNOWN")
    return EligibilityResult("pass", (), "D.ELIGIBILITY.PASS")


# ---------------------------------------------------------------------------
# provider reads (the only two network surfaces; both guarded)
# ---------------------------------------------------------------------------

def _fetch_fmp_info(ticker: str) -> tuple[dict, dict]:
    """`(fields, source_status)` from PR.FMP_ETF_INFO. No direct HTTP here."""
    import os

    from scripts.sources.fmp import fetch_etf_info
    res = fetch_etf_info(ticker, fmp_api_key=os.environ.get("FMP_API_KEY", ""))
    if not res.ok:
        code = getattr(getattr(res.error, "code", None), "value", "upstream_error")
        return {}, {"status": "error", "detail": code}
    items = (res.data or {}).get("items") or []
    if not items:
        return {}, {"status": "missing", "detail": "provider returned no rows"}
    return items[0], {"status": "ok", "detail": None}


def _fetch_yfinance_fund(ticker: str) -> tuple[dict, dict]:
    """One guarded FundsData read — top holdings AND allocation come from the
    SAME read, so they cannot describe two different vintages of the fund.

    Measured on yfinance 1.2.0: `top_holdings` is a DataFrame with `Symbol` as
    the index and `Name` / `Holding Percent` as columns, weights as raw
    fractions. There is NO holdings-date field anywhere on the object, which
    is why `holdings_as_of` is null and does not gate.
    """
    import yfinance as yf

    from scripts.sources.yfinance_guard import validate_yfinance_ticker, yfinance_call

    safe = validate_yfinance_ticker(ticker)
    try:
        tk = yfinance_call(lambda: yf.Ticker(safe))
        info = yfinance_call(lambda: tk.info) or {}
        funds = yfinance_call(lambda: tk.funds_data)
        allocation = yfinance_call(lambda: funds.asset_classes)
        frame = yfinance_call(lambda: funds.top_holdings)
    except Exception as exc:  # noqa: BLE001 — a source error is not a finding
        return {}, {"status": "error", "detail": str(exc)[:200]}

    rows = []
    if frame is not None and getattr(frame, "empty", True) is False:
        for symbol, row in frame.iterrows():
            rows.append({"symbol": str(symbol),
                         "weight": row.get("Holding Percent")})
    return ({"category": info.get("category"),
             "legal_type": info.get("legalType"),
             "allocation": allocation,
             "holdings_rows": rows},
            {"status": "ok", "detail": None})


# ---------------------------------------------------------------------------
# producer
# ---------------------------------------------------------------------------

def _nonempty_str(value) -> Optional[str]:
    """Provider absence is `""` as often as it is null — measured as
    `etfCompany == ""` for SNLN and `website == ""` for four others. An
    `is None` guard would read those holes as present-and-empty."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_or_none(value) -> Optional[float]:
    return float(value) if _finite(value) and value > 0 else None


def _nonneg_or_none(value) -> Optional[float]:
    return float(value) if _finite(value) and value >= 0 else None


def build_profile(*, ticker: str, identity_status: str, policy,
                  price_usd, price_as_of, run_date: datetime.date,
                  fmp_fields: dict, fmp_status: dict,
                  yf_fields: dict, yf_status: dict,
                  retrieved_at: str) -> dict:
    """Assemble A.ETF_PROFILE. Pure given its inputs — the two provider reads
    happen in `main`, so every branch here is testable without a network."""
    fund_name = _nonempty_str(fmp_fields.get("name"))
    description = _nonempty_str(fmp_fields.get("description"))
    asset_class = _nonempty_str(fmp_fields.get("assetClass"))
    category = _nonempty_str(yf_fields.get("category"))
    legal_type = _nonempty_str(yf_fields.get("legal_type"))

    scan = leverage_scan(fund_name=fund_name, description=description,
                         category=category, asset_class=asset_class,
                         legal_type=legal_type)
    approval = etf_policy_approval(policy, ticker, run_date)
    allocation = build_asset_allocation(yf_fields.get("allocation"))
    rows = yf_fields.get("holdings_rows")
    holdings = build_top_holdings(rows)
    concentration = max_holding_weight(rows)

    avg_volume = _positive_or_none(fmp_fields.get("avgVolume"))
    dollar_volume = avg_dollar_volume_usd(
        avg_volume_shares=avg_volume, price_usd=price_usd,
        price_as_of=price_as_of, run_date=run_date)

    eligibility = entry_eligibility(
        identity_status=identity_status,
        approval_status=approval.status,
        leverage_status=scan.status,
        asset_allocation_status=allocation.status,
        stock_frac=allocation.stock_frac,
        cash_frac=allocation.cash_frac,
        non_equity_frac=allocation.non_equity_frac,
        max_holding_weight=concentration,
        avg_dollar_volume_usd=dollar_volume,
    )

    return {
        "ticker": ticker,
        "identity_status": identity_status,
        "approval_status": approval.status,
        "approval_reviewed_on": approval.reviewed_on,
        "approval_age_days": approval.age_days,
        "approval_reasons": list(approval.reasons),
        "leverage_status": scan.status,
        "leverage_evidence": [dict(e) for e in scan.evidence],
        "asset_allocation": allocation.vector,
        "asset_allocation_status": allocation.status,
        "stock_frac": allocation.stock_frac,
        "cash_frac": allocation.cash_frac,
        "non_equity_frac": allocation.non_equity_frac,
        "allocation_sum_frac": allocation.allocation_sum_frac,
        "top_holdings": holdings.top_holdings,
        "holdings_vector_status": holdings.status,
        "coverage_pct": holdings.coverage_pct,
        "max_holding_weight": concentration,
        # The provider exposes no holdings vintage at all (measured on
        # yfinance 1.2.0), so this is null and does NOT gate. Making it a
        # requirement would refuse every fund.
        "holdings_as_of": None,
        "expense_ratio_pct": _nonneg_or_none(fmp_fields.get("expenseRatio")),
        "aum_usd": _nonneg_or_none(fmp_fields.get("assetsUnderManagement")),
        "avg_volume_shares": avg_volume,
        "avg_volume_shares_as_of": retrieved_at if avg_volume is not None else None,
        "price_usd": _positive_or_none(price_usd),
        "price_as_of": price_as_of,
        "avg_dollar_volume_usd": dollar_volume,
        "asset_class": asset_class,
        "category": category,
        "legal_type": legal_type,
        "fund_name": fund_name,
        "description": description,
        "entry_eligibility": eligibility.eligibility,
        "entry_reasons": list(eligibility.reasons),
        "retrieved_at": retrieved_at,
        "field_provenance": {
            "avg_volume_shares": {
                "provider": "fmp", "raw_field": "avgVolume",
                "raw_value": fmp_fields.get("avgVolume"),
                "raw_unit": "shares", "conversion": "none"},
            "price_usd": {
                "provider": "etf_market_snapshot", "raw_field": "ticker_prices",
                "raw_value": price_usd, "raw_unit": "usd",
                "conversion": "none"},
            "asset_allocation": {
                "provider": "yfinance", "raw_field": "funds_data.asset_classes",
                "raw_value": yf_fields.get("allocation"),
                "raw_unit": "fraction", "conversion": "key rename"},
        },
        "source_status": {"fmp": dict(fmp_status), "yfinance": dict(yf_status)},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.etf.profile")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--identity-registry", required=True)
    ap.add_argument("--market-snapshot", required=True)
    ap.add_argument("--compiled-strategy", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--run-date", help="ISO date; defaults to today (ET)")
    args = ap.parse_args(argv)

    try:
        ticker = normalize_ticker(args.ticker)
    except ValueError as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return 2

    from scripts.schemas.etf_market_snapshot import load_etf_market_snapshot
    from scripts.schemas.instrument_registry import load_instrument_registry
    from scripts.schemas.strategy import load_compiled_strategy

    try:
        registry = load_instrument_registry(args.identity_registry)
        snapshot = load_etf_market_snapshot(args.market_snapshot)
        compiled = load_compiled_strategy(args.compiled_strategy)
    except (OSError, ValueError) as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return 1

    if registry.ticker != ticker:
        print(f"{_PREFIX}: registry is for {registry.ticker}, not {ticker}",
              file=sys.stderr)
        return 1

    if args.run_date:
        from scripts.schemas.strategy import parse_canonical_iso_date
        run_date = parse_canonical_iso_date(args.run_date)
        if run_date is None:
            print(f"{_PREFIX}: --run-date must be YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        from scripts.delta.calendar import today_et
        run_date = today_et()

    # The price is read ONLY behind its PASSED status. `price_as_of` lives in
    # the status block; there is no nested `ticker_prices[T].price_as_of`.
    price_usd = (snapshot.price_usd(ticker)
                 if snapshot.price_status(ticker) == "PASSED" else None)
    price_as_of = snapshot.price_as_of(ticker)

    fmp_fields, fmp_status = _fetch_fmp_info(ticker)
    yf_fields, yf_status = _fetch_yfinance_fund(ticker)

    profile = build_profile(
        ticker=ticker, identity_status=registry.instrument_type,
        policy=compiled.etf_policy, price_usd=price_usd,
        price_as_of=price_as_of, run_date=run_date,
        fmp_fields=fmp_fields, fmp_status=fmp_status,
        yf_fields=yf_fields, yf_status=yf_status,
        retrieved_at=datetime.datetime.now(datetime.timezone.utc)
        .isoformat().replace("+00:00", "Z"),
    )

    write_output(profile, args.output)
    print(Path(args.output).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
