"""Deterministic extreme-QoQ anomaly DETECTOR for income statements.

Named failure mode this prevents (P1, /score-business SNDK): the NAND up-cycle
peak quarter (revenue 3,025M → 5,950M = +97% QoQ, gross margin 50.9% → 78.4% =
+27pp) is REAL, but the fundamental scoring agent — handed the raw
02_financial_data.json with NO structured signal telling it whether such an
extreme jump is a genuine cyclical peak or corrupt data — guessed "corrupted",
dropped the quarter, and scored the business on a false premise. No validator
flagged it; the safety net did not catch it; only a human did.

Design (settled after three cold-review rounds). The detector DETECTS extreme
quarters deterministically and SURFACES the available evidence — it deliberately
does NOT emit a binary "this is real" verdict. A threshold script cannot reliably
adjudicate real-cyclical-peak vs corruption across every currency-basis / data-
feed / period edge (each such edge is a way to FALSELY confirm, and a wrong
"keep this as real" feeds a bad number into scoring — the exact money-path harm).
So the script's job is narrow and robust: (1) notice the extreme quarter so the
agent never has to guess in a vacuum, and (2) hand over honest evidence — the
07_earnings revenue cross-check (only ever True/False on a comparable, same-
currency-basis, positive-revenue pair, else null) and YoY context. The scoring
prompt (prompts/score-fundamental.md) tells the agent to weigh that evidence +
through-cycle normalization (scoring-calibration.md §4) and to never reflexively
drop an extreme quarter as corrupt. The flag list is written into
02_financial_data.json (`anomalous_quarters`), the file the fundamental agent
actually reads.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from scripts.sources.common import normalize_currency

# A QoQ revenue move beyond this magnitude is "extreme" — large enough that a
# scoring agent might mistake a real cyclical peak/trough for corrupt data.
QOQ_REVENUE_THRESHOLD_PCT = 50.0
# A gross-margin swing beyond this many percentage points is "extreme".
MARGIN_DELTA_THRESHOLD_PP = 20.0
# Statement revenue vs 07_earnings actual_revenue within this fraction → "match"
# (only ever evaluated on a same-currency-basis, positive-revenue pair).
REVENUE_MATCH_TOLERANCE = 0.05


def _num(v: Any) -> Optional[float]:
    """Coerce to a finite float, else None (never raises).

    Booleans are rejected (NOT coerced to 1.0/0.0) — a bool in a numeric slot is
    drift, and float(True)==1.0 would manufacture a nonsense delta.
    """
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return f


def _margin_pct(row: Dict) -> Optional[float]:
    rev = _num(row.get("revenue"))
    gp = _num(row.get("gross_profit"))
    if rev is None or gp is None or rev <= 0:
        return None
    return gp / rev * 100.0


def _fmt_m(v: float) -> str:
    return f"{v / 1e6:,.0f}M"


def _known_ccy(raw: Any) -> Optional[str]:
    """A currency usable as a comparison BASIS — a supported code only.

    `normalize_currency` passes the repo's "UNKNOWN" sentinel through and maps
    empty/unsupported to None; both mean "not a known basis" for our purpose, so
    two "UNKNOWN"s are NOT treated as the same currency (they'd otherwise produce
    a dishonest numeric match).
    """
    c = normalize_currency(raw)
    return c if c and c != "UNKNOWN" else None


def _revenue_cross_check(
    period: str, cur_rev: Optional[float], row_ccy: Optional[str],
    earn_rev: Optional[float], earn_period: Optional[str], earn_ccy: Optional[str],
) -> Dict:
    """Surface the 07_earnings revenue cross-check as EVIDENCE (no verdict).

    `matches_statement` is True/False ONLY when the two are genuinely comparable
    — same currency basis, this period, both positive. Otherwise it is None and
    the note says why, so the agent weighs it rather than the script blessing a
    basis/feed mismatch as real.
    """
    if earn_rev is None or earn_period != period:
        return {"actual_revenue": None, "matches_statement": None,
                "note": ("07_earnings has no actual_revenue for this period — "
                         "cross-check via YoY / 06_analyst_estimates / filing")}
    if not row_ccy or not earn_ccy:
        return {"actual_revenue": earn_rev, "matches_statement": None,
                "note": (f"07_earnings actual_revenue {_fmt_m(earn_rev)} present but the "
                         f"currency basis is unknown on one side (statement={row_ccy or '?'}, "
                         f"earnings={earn_ccy or '?'}) — compare cautiously")}
    if row_ccy != earn_ccy:
        return {"actual_revenue": earn_rev, "matches_statement": None,
                "note": (f"07_earnings actual_revenue {_fmt_m(earn_rev)} is in {earn_ccy} but "
                         f"the statement is {row_ccy} — different currency basis, not directly "
                         f"comparable")}
    if cur_rev is None or cur_rev <= 0 or earn_rev <= 0:
        return {"actual_revenue": earn_rev, "matches_statement": None,
                "note": (f"07_earnings actual_revenue {_fmt_m(earn_rev)} present but revenue is "
                         f"non-positive — cannot compare as a peak")}
    matches = abs(cur_rev - earn_rev) / earn_rev <= REVENUE_MATCH_TOLERANCE
    rel = "≈" if matches else "≠"
    return {"actual_revenue": earn_rev, "matches_statement": matches,
            "note": (f"07_earnings actual_revenue {_fmt_m(earn_rev)} {rel} statement "
                     f"{_fmt_m(cur_rev)} (both {row_ccy})")}


_NOTE = (
    "Extreme QoQ detected — do NOT reflexively treat this quarter as corrupt. "
    "Weigh the evidence: the earnings cross-check is REVENUE-only (gross margin is "
    "NOT earnings-validated), plus yoy_revenue_pct + 06_analyst_estimates + the "
    "filing. Apply through-cycle normalization (scoring-calibration.md §4); only "
    "exclude the quarter with a stated reason in data_quality_caveats."
)


def detect_anomalous_quarters(
    income_statements: List[Dict],
    earnings_data: Optional[Dict] = None,
    *,
    ticker: Optional[str] = None,
    margin_reliable: bool = True,
) -> List[Dict]:
    """Flag quarters with an extreme QoQ revenue or gross-margin jump and surface
    the evidence for the agent to adjudicate. Returns a list of flag dicts (empty
    when nothing is extreme). Never raises.

    `margin_reliable=False` (mixed-currency-unrepairable statements): skip the
    gross-margin signal entirely. On that path `gross_profit` can be native while
    `revenue` is USD, so `gross_profit/revenue` is a garbage cross-currency ratio
    that would manufacture a fake margin anomaly — the system's own rule says not
    to compute margins from those rows. Revenue QoQ stays (revenue is always in
    the USD master set), so a real revenue peak is still surfaced.
    """
    if not isinstance(income_statements, list):
        return []
    # Quarterly only — annual fallback rows (yfinance) must NOT be compared as
    # QoQ. Rows without an explicit `period` default to quarterly (the FDS path).
    rows = [
        r for r in income_statements
        if isinstance(r, dict) and r.get("report_period")
        and str(r.get("period", "quarterly")).lower() == "quarterly"
    ]
    # De-duplicate by report_period (restatement / API drift can repeat a quarter)
    # keeping the LAST-listed row, so two rows of the same period are never
    # compared as a quarter-over-quarter move. Restatement resolution is downstream.
    by_period: Dict[str, Dict] = {}
    for r in rows:
        by_period[str(r.get("report_period"))] = r
    rows = sorted(by_period.values(), key=lambda r: str(r.get("report_period")))
    if len(rows) < 2:
        return []

    earnings = earnings_data if isinstance(earnings_data, dict) else {}
    earnings_rev = _num(earnings.get("actual_revenue"))
    earnings_period = str(earnings.get("report_period") or "") or None
    earnings_ccy = _known_ccy(earnings.get("currency"))

    flags: List[Dict] = []
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        prev_rev, cur_rev = _num(prev.get("revenue")), _num(cur.get("revenue"))
        # Require a POSITIVE prior base; a drop INTO negative (cur_rev < 0) is
        # still computed and flagged as extreme.
        qoq_pct: Optional[float] = None
        if prev_rev is not None and prev_rev > 0 and cur_rev is not None:
            qoq_pct = (cur_rev - prev_rev) / prev_rev * 100.0

        prev_m, cur_m = (_margin_pct(prev), _margin_pct(cur)) if margin_reliable else (None, None)
        margin_delta: Optional[float] = (
            cur_m - prev_m if prev_m is not None and cur_m is not None else None
        )

        extreme_rev = qoq_pct is not None and abs(qoq_pct) >= QOQ_REVENUE_THRESHOLD_PCT
        extreme_margin = margin_delta is not None and abs(margin_delta) >= MARGIN_DELTA_THRESHOLD_PP
        if not (extreme_rev or extreme_margin):
            continue

        period = str(cur.get("report_period"))

        yoy_pct: Optional[float] = None
        if i >= 4:
            yoy_prev = _num(rows[i - 4].get("revenue"))
            if yoy_prev is not None and yoy_prev > 0 and cur_rev is not None:
                yoy_pct = (cur_rev - yoy_prev) / yoy_prev * 100.0

        row_ccy = _known_ccy(cur.get("currency"))
        flags.append({
            "period": period,
            "qoq_revenue_pct": round(qoq_pct, 1) if qoq_pct is not None else None,
            "margin_delta_pp": round(margin_delta, 1) if margin_delta is not None else None,
            "yoy_revenue_pct": round(yoy_pct, 1) if yoy_pct is not None else None,
            "earnings_revenue_cross_check": _revenue_cross_check(
                period, cur_rev, row_ccy, earnings_rev, earnings_period, earnings_ccy),
            "note": _NOTE,
        })

    return flags
