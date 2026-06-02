"""Reverse DCF — solve for the growth rate implied by the current stock price.

Instead of projecting forward (GIGO-prone), asks:
"What revenue/earnings growth rate does the market currently price in?"

If the implied growth is 5% and you think the company can grow 15%, that's a signal.
If the implied growth is 30% and you think 15% is realistic, that's also a signal.

Uses binary search since DCF value is monotonically increasing in growth rate.
"""

import json
import math
import sys
from typing import Dict, Optional


def _dcf_value(
    base_fcf_per_share: float,
    growth_rate: float,
    discount_rate: float,
    terminal_growth: float,
    projection_years: int = 10,
) -> float:
    """Calculate DCF fair value per share for a given growth rate.

    Args:
        base_fcf_per_share: Current trailing FCF per share.
        growth_rate: Annual growth rate (e.g., 0.15 for 15%).
        discount_rate: WACC / required return (e.g., 0.10 for 10%).
        terminal_growth: Long-term sustainable growth (e.g., 0.025 for 2.5%).
        projection_years: Explicit forecast period.

    Returns: Present value per share.
    """
    if discount_rate <= terminal_growth:
        return float("inf")

    pv_sum = 0.0
    fcf = base_fcf_per_share
    for t in range(1, projection_years + 1):
        fcf *= (1 + growth_rate)
        pv_sum += fcf / (1 + discount_rate) ** t

    # Terminal value (Gordon Growth Model)
    terminal_fcf = fcf * (1 + terminal_growth)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / (1 + discount_rate) ** projection_years

    return pv_sum + pv_terminal


def solve_implied_growth(
    current_price: float,
    base_fcf_per_share: float,
    discount_rate: float = 0.10,
    terminal_growth: float = 0.025,
    projection_years: int = 10,
    tolerance: float = 0.001,
    max_iterations: int = 100,
) -> Dict:
    """Solve for the growth rate implied by the current stock price.

    Uses binary search on the growth rate that makes DCF value = current price.

    Args:
        current_price: Current stock price.
        base_fcf_per_share: Current trailing FCF per share.
        discount_rate: WACC / required return.
        terminal_growth: Long-term sustainable growth rate.
        projection_years: Explicit forecast period (default 10).
        tolerance: Convergence tolerance for price match.
        max_iterations: Maximum binary search iterations.

    Returns: Dict with implied_growth_rate and sensitivity analysis.
    """
    # Second line of defense for the dual-path extract_fcf null-guard.
    # Accepts None (upstream path selected no valid FCF) and 0/negative
    # (upstream produced an invalid number). Returns the same skipped
    # shape as the valuation prompt's first-line guard — one contract
    # regardless of which defense tripped. Reason is split so the
    # operator log tells you WHICH input was bad.
    if current_price <= 0:
        return {
            "implied_growth_rate_pct": None,
            "status": "skipped",
            "reason": "invalid_price_input",
            "source": "[Calc: skipped in reverse_dcf — non-positive price]",
        }
    if base_fcf_per_share is None or base_fcf_per_share <= 0:
        return {
            "implied_growth_rate_pct": None,
            "status": "skipped",
            "reason": "invalid_fcf_input",
            "source": "[Calc: skipped in reverse_dcf — null or non-positive fcf]",
        }

    # Binary search bounds: -20% to +50% growth
    lo, hi = -0.20, 0.50

    # Check bounds
    val_lo = _dcf_value(base_fcf_per_share, lo, discount_rate, terminal_growth, projection_years)
    val_hi = _dcf_value(base_fcf_per_share, hi, discount_rate, terminal_growth, projection_years)

    if current_price < val_lo:
        return {
            "implied_growth_rate_pct": round(lo * 100, 1),
            "note": f"Price below minimum DCF (growth < {lo*100:.0f}%)",
            "discount_rate_used": discount_rate,
            "terminal_growth_used": terminal_growth,
        }
    if current_price > val_hi:
        return {
            "implied_growth_rate_pct": round(hi * 100, 1),
            "note": f"Price above maximum DCF (growth > {hi*100:.0f}%)",
            "discount_rate_used": discount_rate,
            "terminal_growth_used": terminal_growth,
        }

    # Binary search
    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        val_mid = _dcf_value(base_fcf_per_share, mid, discount_rate, terminal_growth, projection_years)

        if abs(val_mid - current_price) < tolerance:
            break
        if val_mid < current_price:
            lo = mid
        else:
            hi = mid

    implied_rate = (lo + hi) / 2

    # Sensitivity: what if WACC is ±1%?
    sensitivity = {}
    for wacc_delta in [-0.01, 0.0, 0.01]:
        adj_dr = discount_rate + wacc_delta
        if adj_dr <= terminal_growth:
            continue
        # Re-solve for this WACC
        s_lo, s_hi = -0.20, 0.50
        for _ in range(max_iterations):
            s_mid = (s_lo + s_hi) / 2
            s_val = _dcf_value(base_fcf_per_share, s_mid, adj_dr, terminal_growth, projection_years)
            if abs(s_val - current_price) < tolerance:
                break
            if s_val < current_price:
                s_lo = s_mid
            else:
                s_hi = s_mid
        wacc_label = f"wacc_{adj_dr*100:.1f}pct"
        sensitivity[wacc_label] = round((s_lo + s_hi) / 2 * 100, 1)

    return {
        "implied_growth_rate_pct": round(implied_rate * 100, 1),
        "discount_rate_used": discount_rate,
        "terminal_growth_used": terminal_growth,
        "projection_years": projection_years,
        "base_fcf_per_share": round(base_fcf_per_share, 2),
        "current_price": round(current_price, 2),
        "sensitivity": sensitivity,
    }


def _main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Reverse DCF: solve for implied growth rate from current stock price."
    )
    parser.add_argument("--price", type=float, required=True,
                        help="Current stock price")
    parser.add_argument("--fcf-per-share", type=float, required=True,
                        help="Trailing FCF per share")
    parser.add_argument("--discount-rate", type=float, default=0.10,
                        help="WACC / discount rate (default: 0.10)")
    parser.add_argument("--terminal-growth", type=float, default=0.025,
                        help="Terminal growth rate (default: 0.025)")
    parser.add_argument("--output", default=None,
                        help="Output file path (default: stdout)")
    args = parser.parse_args()

    result = solve_implied_growth(
        current_price=args.price,
        base_fcf_per_share=args.fcf_per_share,
        discount_rate=args.discount_rate,
        terminal_growth=args.terminal_growth,
    )

    from scripts.cli_utils import write_output
    write_output(result, args.output)
    if args.output:
        rate = result.get("implied_growth_rate_pct")
        print(
            f"reverse_dcf: implied_growth={rate}% (price={args.price}, fcf={args.fcf_per_share}) → {args.output}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    _main()
