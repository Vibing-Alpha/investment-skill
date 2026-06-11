"""Portfolio constraint validation — safety gate for /portfolio skill.

Pure functions, no network calls, fully testable.
Checks position limits, cash floor, and runs 5 stress test scenarios
before any portfolio orders are placed.
"""

import math
import sys
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Strict vocabulary + config guard (HIGH-9, 10, 11, 12)
# ---------------------------------------------------------------------------

_VALID_ACTIONS = {"buy", "sell"}
# Order-type vocabulary is the SCHEMA contract (scripts/schemas/decisions.py
# ORDER_TYPES, mirrored by portfolio_log.ORDER_TYPES). Cold review 2026-06-11
# R1 HIGH-1: a locally-narrowed {market, limit, stop} set rejected
# schema-valid orders (stop_limit/moc/loc/gtc/stop_market) at the safety
# gate — producer-consumer rule #2 (handle ALL values in the vocabulary).
from scripts.schemas.decisions import ORDER_TYPES as _VALID_TYPES


def _guard_constraints(
    constraints: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fail-closed guard on constraint config.

    Primary coercion happens at compile time (Task 2.1 — skill reads
    strategy.yaml, coerces via normalize_percent_fraction, writes
    strategy.compiled.yaml in decimal). This guard does NOT re-coerce —
    it flags any stale value outside [0.0, 1.0] as invalid_config so
    stale compiled files surface loudly instead of silent-passing.

    Returns (normalized_constraints, violations). Values that fail the
    guard are set to None in the returned dict so downstream checks
    treat them as absent.
    """
    if not constraints:
        return {}, []
    out = dict(constraints)
    violations: List[Dict[str, Any]] = []
    for key in ("max_single_position", "max_sector", "min_cash"):
        v = out.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            violations.append({
                "constraint": "invalid_config",
                "message": f"{key}={v!r} must be numeric (decimal 0.0-1.0)",
            })
            out[key] = None
            continue
        if not math.isfinite(v) or v < 0 or v > 1.0:
            violations.append({
                "constraint": "invalid_config",
                "message": (
                    f"{key}={v} is invalid. Compiled constraints must be "
                    f"finite decimal fractions in [0.0, 1.0] — recompile "
                    f"strategy.compiled.yaml via /portfolio which runs "
                    f"normalize_percent_fraction."
                ),
            })
            out[key] = None
    return out, violations


def _validate_order_vocab(
    orders: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], set]:
    """Strict vocabulary + shares pre-check (HIGH-11, 12).

    Returns (violations, bad_order_indices). Bad orders should be
    filtered before downstream projection so they don't silently move
    cash/shares.
    """
    violations: List[Dict[str, Any]] = []
    bad_indices: set = set()
    for i, order in enumerate(orders or []):
        order_bad = False
        action = order.get("action")
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            violations.append({
                "constraint": "invalid_action",
                "index": i,
                "message": (
                    f"order[{i}].action={action!r} not in "
                    f"{sorted(_VALID_ACTIONS)}"
                ),
            })
            order_bad = True
        otype = order.get("type")
        if not isinstance(otype, str) or otype not in _VALID_TYPES:
            violations.append({
                "constraint": "invalid_type",
                "index": i,
                "message": (
                    f"order[{i}].type={otype!r} not in {sorted(_VALID_TYPES)}"
                ),
            })
            order_bad = True
        shares = order.get("shares")
        # int|float: fractional shares are schema-valid (real broker feature
        # — see portfolio_log F11); bool is a subclass of int, reject it.
        if isinstance(shares, bool) or not isinstance(shares, (int, float)) or shares <= 0:
            violations.append({
                "constraint": "invalid_shares",
                "index": i,
                "message": (
                    f"order[{i}].shares={shares!r} must be a positive number"
                ),
            })
            order_bad = True
        if order_bad:
            bad_indices.add(i)
    return violations, bad_indices


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _order_price(order: Dict[str, Any], ticker_prices: Dict[str, float]):
    """Projection price for an order — explicit order prices BEFORE the
    current-quote fallback.

    Cold review 2026-06-11 R2 HIGH-1: the schema/prompt/log all carry
    ``limit_price`` (portfolio_log._enrich_orders costs orders with it), but
    the validator projected from est_price/price only — a GTC limit buy
    above the current quote stress-tested at the quote, understating the
    cash its fill consumes. Order of preference: est_price (explicit
    estimate) → limit_price → price → current quote.
    """
    for key in ("est_price", "limit_price", "price"):
        v = order.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return v
    return ticker_prices.get(order.get("ticker", ""), 0)


def _get_shares(holding):
    """Extract share count from holding (supports int or {"shares": N}).

    Returns None when ``holding`` is a dict that lacks a ``shares`` key —
    callers MUST treat None as a missing-data failure (HIGH-14 fix), NOT
    silently substitute 0 (which would make the holding invisible to
    ratio constraints).
    """
    if isinstance(holding, dict):
        if "shares" not in holding:
            return None
        return holding["shares"]
    return holding


def _collect_missing_shares(
    holdings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Surface holdings whose ``shares`` field is missing (HIGH-14).

    The previous ``_get_shares`` default of 0 made such holdings
    invisible to ratio constraints. Each missing entry becomes a
    ``missing_shares`` violation so the portfolio gate fails closed.
    """
    violations: List[Dict[str, Any]] = []
    for ticker, holding in holdings.items():
        # _get_shares returns None for: dict without 'shares', dict with
        # `shares: null`, and a bare `TICKER:` (None holding). All three are
        # the same invisible-position hazard — the original key-presence
        # check missed the null-VALUE forms (whole-project review
        # 2026-06-11, HIGH-14 resurrected via null).
        if _get_shares(holding) is None:
            violations.append({
                "constraint": "missing_shares",
                "ticker": ticker,
                "message": (
                    f"holdings[{ticker!r}] has no usable 'shares' value — "
                    f"cannot evaluate position/ratio constraints "
                    f"(fail-closed, HIGH-14)"
                ),
            })
    return violations


def _calc_account_value(
    holdings: Dict[str, Any],
    ticker_prices: Dict[str, float],
    cash: float,
) -> float:
    """Sum(shares * price) + cash.  Missing prices are skipped (fail-closed elsewhere).

    Missing ``shares`` (dict without the key) contribute 0 here — the
    ``missing_shares`` violation emitted by ``_collect_missing_shares``
    already fails the portfolio gate, so this function intentionally
    does not short-circuit (account_value still useful for stress-test
    partial reporting). The caller is responsible for propagating the
    violation.
    """
    total = cash
    for ticker, holding in holdings.items():
        price = ticker_prices.get(ticker, 0.0)
        shares = _get_shares(holding)
        if shares is None:
            continue
        total += shares * price
    return total


def _calc_position_pct(
    shares: int,
    price: float,
    account_value: float,
) -> float:
    """Position percentage.  Fail-closed: if account_value <= 0 return 1.0."""
    if account_value <= 0:
        return 1.0
    return (shares * price) / account_value


# Order action vocabulary — strict per prompts/portfolio-decide.md:
# orders_proposed[i].action is "buy" | "sell". Decision-level verbs
# (add/reduce/exit) belong in decisions[i].action, not orders — the
# portfolio_log validator (scripts/portfolio_log.py ORDER_ACTIONS)
# enforces the same constraint upstream.
_BUY_ACTIONS = {"buy"}
_SELL_ACTIONS = {"sell"}


def _is_buy(order: Dict[str, Any]) -> bool:
    """Detect buy orders in both formats. CANONICAL side classifier —
    portfolio_log's conflict detection imports these; keep ONE
    implementation (producer-consumer rule #3).

    Proposed orders: {"action": "buy", ...}
    Open orders:     {"type": "limit_buy"} or {"type": "stop_buy"}
    str() coercion: user-editable open orders can carry non-string
    action/type drift — classify from the text, never AttributeError.
    """
    if str(order.get("action") or "").lower() in _BUY_ACTIONS:
        return True
    if "buy" in str(order.get("type") or "").lower():
        return True
    return False


def _is_sell(order: Dict[str, Any]) -> bool:
    """Detect sell orders in both formats (see _is_buy — canonical)."""
    if str(order.get("action") or "").lower() in _SELL_ACTIONS:
        return True
    if "sell" in str(order.get("type") or "").lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Core projection
# ---------------------------------------------------------------------------

def _apply_orders(
    holdings: Dict[str, Any],
    cash: float,
    orders: List[Dict[str, Any]],
    ticker_prices: Dict[str, float],
    missing_price_violations: Optional[List[Dict[str, Any]]] = None,
    oversell_violations: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, int], float]:
    """Project holdings/cash after orders execute.

    - Buy: deduct cost, add shares
    - Sell: cap at owned shares (never sell more than you own), add proceeds
    - Uses order's est_price/price first, falls back to ticker_prices
    - Skip orders with no price (fail-closed)
    - If ``missing_price_violations`` is a list, append a
      ``missing_price_order`` violation dict for every order skipped due
      to a missing price (HIGH-13). Legacy callers that pass ``None`` get
      the old silent-skip behavior — intentional so stress-test callers
      don't double-count violations.

    Returns ``(holdings_dict, projected_cash)`` — contract preserved for
    existing callers (stress tests at lines ~255, 289).
    """
    proj_holdings: Dict[str, int] = {}
    for ticker, holding in holdings.items():
        shares = _get_shares(holding)
        if shares is None:
            # HIGH-14: holding without 'shares' key — skip so we don't
            # silently project 0 into constraint checks. The gate-level
            # `missing_shares` violation (via _collect_missing_shares)
            # already blocks the run.
            continue
        proj_holdings[ticker] = shares

    proj_cash = cash

    for idx, order in enumerate(orders):
        ticker = order.get("ticker", "")
        shares = order.get("shares", 0)  # fail-open-ok: _validate_order_vocab pre-check rejects non-positive int shares before this point (sanitized_orders only)
        price = _order_price(order, ticker_prices)  # fail-open-ok: followed by `if not price or price <= 0` → missing_price_order violation (HIGH-13)

        # Fail-closed: skip orders with no price
        if not price or price <= 0:
            if missing_price_violations is not None:
                missing_price_violations.append({
                    "constraint": "missing_price_order",
                    "ticker": ticker,
                    "index": idx,
                    "message": (
                        f"order[{idx}] ticker={ticker!r} has no est_price / "
                        f"price and no ticker_prices fallback — "
                        f"cannot project (fail-closed)"
                    ),
                })
            continue

        if _is_buy(order):
            cost = shares * price
            proj_cash -= cost
            proj_holdings[ticker] = proj_holdings.get(ticker, 0) + shares
        elif _is_sell(order):
            # Cap at owned shares — and SAY so on the violations pass
            # (whole-project review 2026-06-11 C5): an oversell or a sell
            # of an unheld ticker cannot execute as written; silently
            # projecting the capped fill validated an impossible order.
            # Stress callers pass None (capping is the conservative cash
            # read there; the proposed-order pass owns the verdict).
            owned = proj_holdings.get(ticker, 0)
            actual_sell = min(shares, owned)
            if actual_sell < shares and oversell_violations is not None:
                oversell_violations.append({
                    "constraint": "oversell",
                    "ticker": ticker,
                    "index": idx,
                    "current": owned,
                    "limit": shares,
                    "message": (
                        f"order[{idx}] sells {shares} {ticker} but only "
                        f"{owned} held — the order cannot execute as "
                        f"written (stale portfolio-state.yaml?)"
                    ),
                })
            if actual_sell > 0:
                proceeds = actual_sell * price
                proj_cash += proceeds
                proj_holdings[ticker] = owned - actual_sell
                if proj_holdings[ticker] == 0:
                    del proj_holdings[ticker]

    return proj_holdings, proj_cash


# ---------------------------------------------------------------------------
# Constraint checks
# ---------------------------------------------------------------------------

def _check_position_limits(
    proj_holdings: Dict[str, int],
    account_value: float,
    ticker_prices: Dict[str, float],
    constraints: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Check max_single_position and max_holdings constraints.

    Fail-closed: if a ticker's price is missing/zero, flag as violation.
    max_sector is documented as a future hook (skipped for now).
    Returns structured violation dicts.
    """
    violations: List[Dict[str, Any]] = []
    max_single = constraints.get("max_single_position")
    max_holdings = constraints.get("max_holdings")

    if max_single is not None:
        for ticker, shares in proj_holdings.items():
            price = ticker_prices.get(ticker)
            if not price or price <= 0:
                violations.append({
                    "constraint": "missing_price",
                    "ticker": ticker,
                    "current": None,
                    "limit": max_single,
                    "message": f"{ticker} — missing/zero price, fail-closed",
                })
                continue
            pct = _calc_position_pct(shares, price, account_value)
            if pct > max_single:
                violations.append({
                    "constraint": "max_single_position",
                    "ticker": ticker,
                    "current": round(pct, 4),
                    "limit": max_single,
                    "message": (
                        f"{ticker} would be {pct:.1%} of portfolio, "
                        f"exceeding {max_single:.0%} limit"
                    ),
                })

    if max_holdings is not None:
        count = len(proj_holdings)
        if count > max_holdings:
            violations.append({
                "constraint": "max_holdings",
                "ticker": None,
                "current": count,
                "limit": max_holdings,
                "message": (
                    f"Holdings count {count} exceeds "
                    f"max_holdings {max_holdings}"
                ),
            })

    return violations


def _check_cash_floor(
    proj_cash: float,
    account_value: float,
    constraints: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Check min_cash constraint.  Returns structured violation dicts."""
    violations: List[Dict[str, Any]] = []
    min_cash = constraints.get("min_cash")

    if min_cash is not None and account_value > 0:
        cash_pct = proj_cash / account_value
        if cash_pct < min_cash:
            violations.append({
                "constraint": "min_cash",
                "ticker": None,
                "current": round(cash_pct, 4),
                "limit": min_cash,
                "message": (
                    f"Cash {cash_pct:.1%} below min_cash {min_cash:.0%} "
                    f"(${proj_cash:,.0f} of ${account_value:,.0f})"
                ),
            })

    return violations


# ---------------------------------------------------------------------------
# Stress tests
# ---------------------------------------------------------------------------

def _run_stress_tests(
    holdings: Dict[str, Any],
    cash: float,
    proposed_orders: List[Dict[str, Any]],
    open_orders: List[Dict[str, Any]],
    ticker_prices: Dict[str, float],
    constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """Run 5 stress test scenarios using a cash_after helper.

    Each order uses its own price (est_price or price field), falling back to
    ticker_prices.  No crash multiplier — orders carry their own prices.

    1. base:         proposed market orders only
    2. all_buy:      all proposed buys + open buys
    3. all_sell:     all proposed sells + open sells
    4. extreme_down: all buys + stop sells (stops use their own price field)
    5. defensive:    only stops trigger, no buys
    """
    results = {}

    def _project(order_list):
        """Project holdings and cash after orders. Single implementation."""
        return _apply_orders(holdings, cash, order_list, ticker_prices)

    # --- base: proposed market orders only ---
    market_orders = [o for o in proposed_orders if o.get("type", "").lower() == "market"]
    _, base_cash = _project(market_orders)
    results["base"] = {
        "cash_after": round(base_cash, 2),
        "passed": base_cash >= 0,
    }

    # --- all_buy: all proposed buys + open buys ---
    all_buys = (
        [o for o in proposed_orders if _is_buy(o)]
        + [o for o in open_orders if _is_buy(o)]
    )
    _, all_buy_cash = _project(all_buys)
    results["all_buy"] = {
        "cash_after": round(all_buy_cash, 2),
        "passed": all_buy_cash >= 0,
    }

    # --- all_sell: all proposed sells + open sells ---
    all_sells = (
        [o for o in proposed_orders if _is_sell(o)]
        + [o for o in open_orders if _is_sell(o)]
    )
    _, all_sell_cash = _project(all_sells)
    results["all_sell"] = {
        "cash_after": round(all_sell_cash, 2),
        "passed": all_sell_cash >= 0,
    }

    # --- extreme_down: all buys + stop sells trigger ---
    stops = [
        o for o in open_orders
        if "stop" in o.get("type", "").lower() and _is_sell(o)
    ]
    extreme_orders = all_buys + stops
    extreme_holdings, extreme_cash = _project(extreme_orders)

    # Shrunk denominator check: position % against crashed account value
    extreme_passed = extreme_cash >= 0
    extreme_violations = []

    max_single = constraints.get("max_single_position")
    if max_single is not None and extreme_cash >= 0:
        crashed_value = _calc_account_value(extreme_holdings, ticker_prices, extreme_cash)
        if crashed_value > 0:
            for ticker, shares in extreme_holdings.items():
                p = ticker_prices.get(ticker, 0)
                if p <= 0:
                    continue
                pct = _calc_position_pct(shares, p, crashed_value)
                if pct > max_single:
                    extreme_passed = False
                    extreme_violations.append({
                        "constraint": "max_single_position",
                        "ticker": ticker,
                        "current": round(pct, 4),
                        "limit": max_single,
                        "message": (
                            f"{ticker} at {pct:.1%} exceeds {max_single:.0%} "
                            f"(shrunk denominator)"
                        ),
                    })

    results["extreme_down"] = {
        "cash_after": round(extreme_cash, 2),
        "passed": extreme_passed,
    }
    if extreme_violations:
        results["extreme_down"]["violations"] = extreme_violations

    # --- defensive: only stops trigger, no buys ---
    _, defensive_cash = _project(stops)
    results["defensive"] = {
        "cash_after": round(defensive_cash, 2),
        "passed": defensive_cash >= 0,
    }

    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def validate_portfolio(
    state: Dict[str, Any],
    prices: Dict[str, Any],
    proposed_orders: List[Dict[str, Any]],
    constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate portfolio constraints against proposed orders.

    Args:
        state: {"holdings": {ticker: shares_or_dict}, "cash": float,
                "open_orders": [...] (optional)}
        prices: {"ticker_prices": {ticker: price}}
        proposed_orders: [{"ticker", "action", "shares", "type", "est_price"}, ...]
        constraints: {"max_single_position", "max_holdings", "min_cash", ...}

    Returns:
        {"passed": bool, "violations": list, "stress_test": dict}
    """
    # Present-but-null normalization (whole-project review 2026-06-11):
    # a bare YAML key (`holdings:` / `cash:` / `open_orders:`) loads as None
    # with the key PRESENT, sailing past `.get(key, default)` — the sibling
    # readers (portfolio_log, monitor) already normalize with `or`.
    holdings = state.get("holdings") or {}
    cash = float(state.get("cash") or 0)  # fail-open-ok: $0 cash is a legit state (fully invested)
    open_orders = state.get("open_orders") or []
    # A present-but-null/non-numeric price is a MISSING price — it must take
    # the structured missing-price fail-close path (macro.py emits
    # ticker_prices[t]=None for a failed fetch), never `shares * None`.
    ticker_prices = {
        k: v for k, v in (prices.get("ticker_prices") or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }

    # Pre-check 0: holdings with missing 'shares' key → fail-closed
    # (HIGH-14). Previously `_get_shares` silently returned 0, making
    # such positions invisible to ratio constraints.
    missing_shares_violations = _collect_missing_shares(holdings)

    # Pre-check 1: constraint config validity (HIGH-9 stub, HIGH-10 guard).
    normalized_constraints, config_violations = _guard_constraints(
        constraints or {}
    )

    # HIGH-9: max_sector is documented but has no sector-mapping
    # implementation. If the user set it AND it passed the range guard,
    # still fail-closed — silent-pass hides missing functionality.
    if normalized_constraints.get("max_sector") is not None:
        config_violations.append({
            "constraint": "invalid_config",
            "message": (
                "max_sector is set but sector lookup is not yet "
                "implemented — fail-closed. Remove max_sector from "
                "compiled constraints or implement sector mapping."
            ),
        })
        # Remove so downstream checks don't try to use it.
        normalized_constraints["max_sector"] = None

    # Pre-check 2: strict order vocabulary + positive shares (HIGH-11, 12).
    vocab_violations, bad_order_indices = _validate_order_vocab(
        proposed_orders or []
    )
    sanitized_orders = [
        o for i, o in enumerate(proposed_orders or [])
        if i not in bad_order_indices
    ]

    # Project state after proposed orders.
    # Collect missing-price order violations via side-channel (HIGH-13);
    # _apply_orders still returns (holdings, cash) — tuple contract
    # preserved for the stress-test call sites.
    missing_price_violations: List[Dict[str, Any]] = []
    oversell_violations: List[Dict[str, Any]] = []
    proj_holdings, proj_cash = _apply_orders(
        holdings, cash, sanitized_orders, ticker_prices,
        missing_price_violations=missing_price_violations,
        oversell_violations=oversell_violations,
    )
    proj_account = _calc_account_value(proj_holdings, ticker_prices, proj_cash)

    # HIGH-14: If any ratio-based constraint is active, holdings that
    # lack a price cannot be evaluated — fail-closed before running
    # the ratio checks.
    ratio_active = any(
        normalized_constraints.get(k) is not None
        for k in ("min_cash", "max_single_position", "max_sector")
    )
    holding_price_violations: List[Dict[str, Any]] = []
    if ratio_active:
        active_keys = [
            k for k in ("min_cash", "max_single_position", "max_sector")
            if normalized_constraints.get(k) is not None
        ]
        for ticker in list(proj_holdings.keys()):
            price = ticker_prices.get(ticker)
            if not price or price <= 0:
                # _check_position_limits already emits missing_price when
                # max_single_position is set; only add here for other
                # ratio constraints (e.g. min_cash) to avoid double-report.
                if normalized_constraints.get("max_single_position") is None:
                    holding_price_violations.append({
                        "constraint": "missing_price",
                        "ticker": ticker,
                        "current": None,
                        "limit": None,
                        "message": (
                            f"{ticker} has no price — fail-closed for "
                            f"ratio constraints {active_keys}"
                        ),
                    })

    # Check constraints
    violations: List[Dict[str, Any]] = []
    violations.extend(
        _check_position_limits(
            proj_holdings, proj_account, ticker_prices, normalized_constraints
        )
    )
    violations.extend(
        _check_cash_floor(proj_cash, proj_account, normalized_constraints)
    )

    # Cold review 2026-06-11 R2 HIGH-2 / R3 HIGH-1 / R4 HIGH-1: pre-scan
    # open_orders BEFORE stress projection. A PRESENT broker order the
    # projector cannot price (or classify as buy/sell) is an unknown
    # commitment → VIOLATION (fail-closed, producer-consumer rule #4) —
    # and a non-dict entry would CRASH _is_buy inside the stress run, so
    # only mapping-shaped orders are projected.
    unprojectable_violations: List[Dict[str, Any]] = []
    projectable_open_orders: List[Dict[str, Any]] = []
    for i, o in enumerate(open_orders):
        if not isinstance(o, dict):
            unprojectable_violations.append({
                "constraint": "unprojectable_open_order",
                "index": i,
                "message": (f"open_orders[{i}] is not a mapping — it cannot "
                            f"be projected by any stress scenario"),
            })
            continue
        side_known = _is_buy(o) or _is_sell(o)
        price_ok = _order_price(o, ticker_prices) > 0
        if not side_known or not price_ok:
            reason = ("has no recognizable buy/sell side" if not side_known
                      else "cannot be priced (no est_price/limit_price/price "
                           "and no quote)")
            unprojectable_violations.append({
                "constraint": "unprojectable_open_order",
                "ticker": o.get("ticker"),
                "index": i,
                "message": (
                    f"open_orders[{i}] ({o.get('ticker', '?')}) {reason} — "
                    f"it would be silently absent from stress projections; "
                    f"fix the entry in portfolio-state.yaml."
                ),
            })
            continue
        # A SINGLE open sell larger than the held position cannot execute
        # as written — stale state vs broker (fix-batch review HIGH-1; the
        # proposed-order pass already flags its own oversells). Deliberately
        # per-order, NOT cumulative: a stop-loss + take-profit bracket (OCA)
        # working against the same shares is legitimate broker state, and a
        # cumulative check would false-positive on it (the all_sell stress
        # scenario already caps cumulative fills conservatively).
        o_shares = o.get("shares")
        if (_is_sell(o) and isinstance(o_shares, (int, float))
                and not isinstance(o_shares, bool)):
            owned = _get_shares(holdings.get(o.get("ticker")))
            held_unknown = o.get("ticker") in holdings and owned is None
            if not held_unknown:   # unknown shares → missing_shares already fails the run
                owned_n = (owned if isinstance(owned, (int, float))
                           and not isinstance(owned, bool) else 0)
                if o_shares > owned_n:
                    unprojectable_violations.append({
                        "constraint": "open_order_oversell",
                        "ticker": o.get("ticker"),
                        "index": i,
                        "current": owned_n,
                        "limit": o_shares,
                        "message": (
                            f"open_orders[{i}] sells {o_shares} "
                            f"{o.get('ticker', '?')} but the state holds "
                            f"{owned_n} — the order cannot execute as "
                            f"written (stale portfolio-state.yaml vs broker?)"
                        ),
                    })
        projectable_open_orders.append(o)

    # Run stress tests on sanitized orders so invalid vocab doesn't
    # pollute projections. Use normalized_constraints so stress-test
    # max_single_position check respects the range guard.
    stress_test = _run_stress_tests(
        holdings, cash, sanitized_orders, projectable_open_orders,
        ticker_prices, normalized_constraints,
    )

    # Aggregate. Vocab + config + missing-shares/price violations go
    # first so the caller sees root-cause issues before downstream
    # effects.
    all_violations: List[Dict[str, Any]] = (
        config_violations
        + vocab_violations
        + missing_shares_violations
        + missing_price_violations
        + oversell_violations
        + holding_price_violations
        + violations
    )

    # Overall pass: no violations AND all stress tests pass
    stress_passed = all(s["passed"] for s in stress_test.values())

    # Feedback 2026-06-11 #3c: with no open orders the all_buy/extreme_down/
    # defensive scenarios collapse into each other (field run: all_buy ==
    # extreme_down == 36,209.72, PASS reported on a vacuous stress). Say so
    # instead of silently reporting strong-looking coverage.
    warnings: List[str] = []
    if not open_orders:
        warnings.append(
            "open_orders is empty/absent in portfolio-state.yaml — stress "
            "scenarios all_buy/extreme_down/defensive degenerate to "
            "proposed-orders-only (extreme_down == all_buy, defensive == "
            "current cash). Sync broker open orders into the state file "
            "for meaningful stress coverage."
        )
    all_violations.extend(unprojectable_violations)

    return {
        "passed": len(all_violations) == 0 and stress_passed,
        "violations": all_violations,
        "warnings": warnings,
        "stress_test": stress_test,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main():
    import argparse

    import yaml

    from scripts.cli_utils import read_json, write_output

    parser = argparse.ArgumentParser(
        description="Validate portfolio constraints against proposed orders."
    )
    parser.add_argument(
        "--state", required=True,
        help="Path to portfolio state YAML (holdings + cash + open_orders)",
    )
    parser.add_argument(
        "--prices", required=True,
        help="Path to current prices JSON ({ticker_prices: {ticker: price}})",
    )
    parser.add_argument(
        "--orders", required=True,
        help="Path to proposed orders JSON ([{ticker, action, shares, type}])",
    )
    parser.add_argument(
        "--constraints", required=True,
        help="Path to constraints YAML (max_single_position, min_cash, etc.)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output file path (default: stdout)",
    )
    args = parser.parse_args()

    prefix = "validate"

    # Read inputs
    try:
        with open(args.state, "r", encoding="utf-8") as f:
            state = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"{prefix}: failed to read --state {args.state}: {exc}", file=sys.stderr)
        sys.exit(1)

    prices = read_json(args.prices, "--prices", prefix)

    orders_data = read_json(args.orders, "--orders", prefix)
    # Fail-closed on an unrecognized orders shape (whole-project review
    # 2026-06-11 C4): the old `.get("orders", [])` fallback silently
    # validated ZERO orders for any dict keyed otherwise (e.g. a blob-shaped
    # {"orders_proposed": [...]}), producing a vacuous passed:true artifact.
    if isinstance(orders_data, list):
        orders = orders_data
    elif isinstance(orders_data, dict) and isinstance(orders_data.get("orders"), list):
        orders = orders_data["orders"]
    else:
        print(
            f"{prefix}: --orders must be a JSON array of orders, or an "
            f"object with an 'orders' array; got "
            f"{type(orders_data).__name__}"
            + (f" with keys {sorted(orders_data)}" if isinstance(orders_data, dict) else "")
            + " — refusing to validate an empty order set by accident.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        from scripts.schemas import SchemaError
        from scripts.schemas.strategy import load_compiled_strategy
        compiled = load_compiled_strategy(args.constraints)
    except OSError as exc:
        print(f"{prefix}: failed to read --constraints {args.constraints}: "
              f"{exc}", file=sys.stderr)
        sys.exit(1)
    except (yaml.YAMLError, SchemaError) as exc:
        print(f"{prefix}: invalid --constraints {args.constraints}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    # Flatten via the canonical to_mapping() — single source of truth
    # for the dict shape that validate_portfolio + _guard_constraints expect.
    constraints = compiled.hard_constraints.to_mapping()

    result = validate_portfolio(state, prices, orders, constraints)

    write_output(result, args.output)

    # Summary to stderr
    status = "PASS" if result["passed"] else "FAIL"
    n_violations = len(result["violations"])
    print(
        f"validate: {status} ({n_violations} violations) → "
        f"{args.output or 'stdout'}",
        file=sys.stderr,
    )
    # Cold review 2026-06-11 R4 HIGH-2: a failed validation must FAIL the
    # shell step (script standard: 0=success, 1=failure) — exit 0 on
    # passed:false let an orchestration that only checks exit codes carry
    # rejected orders forward as if validated.
    if not result["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    _main()
