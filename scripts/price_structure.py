"""Closing-basis price structure + cohort recovery (pure computation).

Why: /portfolio's contracted inputs carried no closing-basis high and no
recovery metric, so the decision layer improvised from an INTRADAY field on
artifacts with inconsistent cutoffs, producing a wrong breakout verdict.

Dependency direction is enforced by the signatures:
    compute_ticker_price_structure(...)  intrinsic, universe-independent
    select_cohort_event(...)             reads SERIES, never drawdown status
    compute_cohort_recovery(event, ...)  consumes an immutable selected event
"""

from __future__ import annotations

import math
from typing import Any, Dict, Final, List, Optional, Sequence, Tuple

HIGH_LOOKBACK_SESSIONS: Final[int] = 252
INDICATOR_SLICE_SESSIONS: Final[int] = 126
EVENT_SEARCH_WINDOW_SESSIONS: Final[int] = 21
CLUSTER_WINDOWS: Final[Tuple[int, ...]] = (20, 50, 200)
MA_WINDOWS: Final[Tuple[int, ...]] = (20, 50, 200)
QUORUM_MIN_ABSOLUTE: Final[int] = 5           # user-confirmed 2026-08-09
QUORUM_MIN_MODAL_COUNT: Final[int] = 3


def _fin_pos(x: Any) -> bool:
    """Finite AND strictly positive — matches scripts/indicators.py:33-45
    (merge_adjusted_closes can pass terminal garbage through)."""
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) and x > 0)


def _normalise(pairs: Sequence[Tuple[str, float]]) -> List[Tuple[str, float]]:
    by_date: Dict[str, float] = {}
    for d, c in pairs:
        if isinstance(d, str) and _fin_pos(c):
            by_date[d] = float(c)
    return sorted(by_date.items())


def _sma(closes: Sequence[float], window: int) -> Optional[float]:
    return round(sum(closes[-window:]) / window, 4) if len(closes) >= window else None


_UNKNOWN_BREAKOUT_HOLD: Final[dict] = {
    "status": "unknown", "breakout_session": None, "breakout_level": None,
    "consecutive_market_sessions_above": None,
    "reclaim_session_volume_ratio": None,
}


def _unknown_ma_hold() -> Dict[str, Any]:
    return {f"ma{w}": {"status": "unknown", "run": None} for w in MA_WINDOWS}


def _unknown_cluster_hold() -> Dict[str, Any]:
    return {"status": "unknown", "cluster_windows": list(CLUSTER_WINDOWS),
            "missing_windows": [], "run": None,
            "reclaim_session_volume_ratio": None}


def _unknown(anchor_session, bars, *, covered=False, anchor_close=None,
             last_close=None, last_session=None, lag=None, off_cal=0,
             trimmed=False, suffix_start=None, first_trade=None):
    return {
        "anchor_session": anchor_session, "anchor_close": anchor_close,
        "last_close": last_close, "last_close_session": last_session,
        "anchor_session_covered": covered, "session_lag": lag,
        "bars_available": bars, "off_calendar_bars": off_cal,
        "interior_gap_trimmed": trimmed, "suffix_start_session": suffix_start,
        "lookback_sessions": HIGH_LOOKBACK_SESSIONS, "lookback_complete": False,
        "inception_proven": False, "first_trade_session": first_trade,
        "prior_high_close": None, "prior_high_date": None,
        "pct_vs_prior_high_close": None, "closing_high_status": "unknown",
        "high_water_drawdown": {
            "status": "unknown", "peak_basis": "last_occurrence_of_lookback_max",
            "lookback_sessions": HIGH_LOOKBACK_SESSIONS,
            "peak_close": None, "peak_date": None, "trough_close": None,
            "trough_date": None, "sessions_since_trough": None,
            "depth_pct": None, "pct_off_trough": None, "pct_retraced": None},
        "moving_averages": {f"ma{w}": None for w in MA_WINDOWS},
        "breakout_hold": dict(_UNKNOWN_BREAKOUT_HOLD),
        "ma_hold": _unknown_ma_hold(),
        "cluster_hold": _unknown_cluster_hold(),
    }


def compute_ticker_price_structure(pairs, *, anchor_session, market_sessions,
                                   volumes=None, first_trade_session=None):
    """Intrinsic closing-basis facts for one ticker, on the gap-free suffix."""
    cal = list(market_sessions)
    cal_set = set(cal)
    raw = _normalise(pairs)
    off_cal = sum(1 for d, _ in raw if d not in cal_set)
    clean = [(d, c) for d, c in raw if d in cal_set]
    if not clean:
        return _unknown(anchor_session, 0, off_cal=off_cal,
                        first_trade=first_trade_session)

    last_session, last_close = clean[-1]
    covered = last_session == anchor_session
    lag = (cal.index(anchor_session) - cal.index(last_session)
           if anchor_session in cal else None)
    if not covered:
        return _unknown(anchor_session, len(clean), last_close=last_close,
                        last_session=last_session, lag=lag, off_cal=off_cal,
                        first_trade=first_trade_session)

    # Gap-free suffix after the last interior gap.
    first_session = clean[0][0]
    span = cal[cal.index(first_session):cal.index(anchor_session) + 1]
    have = {d for d, _ in clean}
    missing = [d for d in span if d not in have]
    if missing:
        cut = missing[-1]
        suffix = [(d, c) for d, c in clean if d > cut]
        trimmed = True
    else:
        suffix, trimmed = clean, False
    suffix_start = suffix[0][0] if suffix else None
    if len(suffix) < 2:
        return _unknown(anchor_session, len(suffix), covered=True,
                        anchor_close=last_close, last_close=last_close,
                        last_session=last_session, lag=lag, off_cal=off_cal,
                        trimmed=trimmed, suffix_start=suffix_start,
                        first_trade=first_trade_session)

    # Provider-chart-series inception: first bar must equal the first market
    # session on/after firstTradeDate, with no interior trim.
    inception = False
    if first_trade_session and not trimmed and cal and first_trade_session >= cal[0]:
        # The calendar must COVER the listing date: if firstTradeDate predates
        # the known calendar, alignment to cal[0] proves nothing.
        idx = next((i for i, d in enumerate(cal) if d >= first_trade_session), None)
        inception = idx is not None and clean[0][0] == cal[idx]

    dates = [d for d, _ in suffix]
    closes = [c for _, c in suffix]
    window = suffix[-(HIGH_LOOKBACK_SESSIONS + 1):]
    wd = [d for d, _ in window]
    wc = [c for _, c in window]
    anchor_close = wc[-1]

    prior = wc[:-1]
    prior_high = max(prior)
    if len(suffix) >= HIGH_LOOKBACK_SESSIONS + 1 or inception:   # == _proven below
        status = ("breakout" if anchor_close > prior_high
                  else "at_prior_high" if anchor_close == prior_high
                  else "below_prior_high")
    else:
        # Truncated available-history prefix: a concrete status here would
        # masquerade as a proven 1-year (or since-inception) high. The
        # numeric fields below stay populated as available-history
        # DIAGNOSTICS; the status is the gate, and it is unknown.
        status = "unknown"

    _proven = len(suffix) >= HIGH_LOOKBACK_SESSIONS + 1 or inception
    if not _proven:
        # Truncated unproven history: the true peak may predate the fetch —
        # a concrete measurement (or a FALSE no_active_drawdown) would
        # masquerade as proven. Fail closed, same rule as closing_high_status.
        hwd = {"status": "unknown",
               "peak_basis": "last_occurrence_of_lookback_max",
               "lookback_sessions": HIGH_LOOKBACK_SESSIONS,
               "peak_close": None, "peak_date": None, "trough_close": None,
               "trough_date": None, "sessions_since_trough": None,
               "depth_pct": None, "pct_off_trough": None, "pct_retraced": None}
    else:
        peak = max(wc)
        pi = len(wc) - 1 - wc[::-1].index(peak)
        tail = wc[pi:]
        trough = min(tail)
        li = pi + tail.index(trough)
    if _proven and trough < peak:
        hwd = {"status": "active_drawdown",
               "peak_basis": "last_occurrence_of_lookback_max",
               "lookback_sessions": HIGH_LOOKBACK_SESSIONS,
               "peak_close": peak, "peak_date": wd[pi],
               "trough_close": trough, "trough_date": wd[li],
               "sessions_since_trough": len(wc) - 1 - li,
               "depth_pct": round((trough / peak - 1) * 100, 4),
               "pct_off_trough": round((anchor_close / trough - 1) * 100, 4),
               "pct_retraced": round((anchor_close - trough) / (peak - trough) * 100, 4)}
    elif _proven:
        hwd = {"status": "no_active_drawdown",
               "peak_basis": "last_occurrence_of_lookback_max",
               "lookback_sessions": HIGH_LOOKBACK_SESSIONS,
               "peak_close": None, "peak_date": None, "trough_close": None,
               "trough_date": None, "sessions_since_trough": None,
               "depth_pct": None, "pct_off_trough": None, "pct_retraced": None}

    # Volumes: non-negative is legitimate (calc_volume accepts zero-volume
    # sessions); _fin_pos would silently drop them. Zero DENOMINATORS are
    # handled at the ratio (-> None).
    vols = {d: v for d, v in (volumes or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0}
    return {
        "anchor_session": anchor_session, "anchor_close": anchor_close,
        "last_close": last_close, "last_close_session": last_session,
        "anchor_session_covered": True, "session_lag": lag,
        "bars_available": len(suffix), "off_calendar_bars": off_cal,
        "interior_gap_trimmed": trimmed, "suffix_start_session": suffix_start,
        "lookback_sessions": HIGH_LOOKBACK_SESSIONS,
        "lookback_complete": len(suffix) >= HIGH_LOOKBACK_SESSIONS + 1,
        "inception_proven": inception, "first_trade_session": first_trade_session,
        "prior_high_close": prior_high,
        # Latest resistance touch: LAST occurrence on repeated equal highs
        # (drawdown already uses last-occurrence; keep the two consistent).
        "prior_high_date": wd[len(prior) - 1 - prior[::-1].index(prior_high)],
        "pct_vs_prior_high_close": round((anchor_close / prior_high - 1) * 100, 4),
        "closing_high_status": status,
        "high_water_drawdown": hwd,
        "moving_averages": {f"ma{w}": _sma(closes, w) for w in MA_WINDOWS},
        "breakout_hold": _breakout_hold(dates, closes, cal, anchor_session,
                                        inception_proven=inception, volumes=vols),
        "ma_hold": _ma_holds(dates, closes, cal, anchor_session),
        "cluster_hold": _cluster_hold(dates, closes, cal, anchor_session,
                                      volumes=vols),
    }


def _contiguous_to_anchor(dates, market_sessions, start, anchor):
    """Every market session from `start` THROUGH THE ANCHOR present."""
    cal = list(market_sessions)
    if start not in cal or anchor not in cal:
        return False
    expected = cal[cal.index(start):cal.index(anchor) + 1]
    return list(dates[-len(expected):]) == expected if len(dates) >= len(expected) else False


def _reclaim_volume_ratio(vols, dates, idx):
    """Reclaim-session volume / SMA20 ENDING ON the reclaim session (inclusive)
    — the volume_ratio_vs_ma20 convention (scripts/indicators.py:783-793)."""
    if not vols or idx < 19:
        return None
    window = [vols.get(d) for d in dates[idx - 19:idx + 1]]
    if any(v is None for v in window):
        return None
    sma = sum(window) / 20.0
    if not (sma > 0 and math.isfinite(sma)):
        return None
    return round(vols[dates[idx]] / sma, 4)


def _breakout_hold(dates, closes, market_sessions, anchor, *,
                   inception_proven=False, volumes=None):
    """Currently surviving breakout episode above its FROZEN level.

    Set-based precedence: eligible survivor -> active (earliest eligible);
    else uneligible survivor -> unavailable (boundary-uncertain); else none.
    'none' is PROVABLE: the observed prefix max is a lower bound of the true
    prior high, so no survivor under the bound means no true survivor.
    """
    n = len(closes)
    if n < 2:
        return dict(_UNKNOWN_BREAKOUT_HOLD)
    eligible_start = frozen = None
    uneligible_survivor = False
    suffix_min = closes[-1]
    for i in range(n - 1, 0, -1):
        suffix_min = min(suffix_min, closes[i])
        ph = max(closes[max(0, i - HIGH_LOOKBACK_SESSIONS):i])
        if closes[i] > ph and suffix_min > ph:
            if i >= HIGH_LOOKBACK_SESSIONS or inception_proven:
                eligible_start, frozen = i, ph      # earliest eligible wins
            else:
                uneligible_survivor = True
    if eligible_start is not None:
        hold_dates = dates[eligible_start:]
        if not _contiguous_to_anchor(hold_dates, market_sessions,
                                     hold_dates[0], anchor):
            return {"status": "unavailable", "breakout_session": dates[eligible_start],
                    "breakout_level": frozen,
                    "consecutive_market_sessions_above": None,
                    "reclaim_session_volume_ratio": None}
        return {"status": "active", "breakout_session": dates[eligible_start],
                "breakout_level": frozen,
                "consecutive_market_sessions_above": len(hold_dates),
                "reclaim_session_volume_ratio": _reclaim_volume_ratio(
                    volumes, dates, eligible_start)}
    if uneligible_survivor:
        return {"status": "unavailable", "breakout_session": None,
                "breakout_level": None, "consecutive_market_sessions_above": None,
                "reclaim_session_volume_ratio": None}
    return {"status": "none", "breakout_session": None, "breakout_level": None,
            "consecutive_market_sessions_above": None,
            "reclaim_session_volume_ratio": None}


def _rolling_ma(closes, i, w):
    return sum(closes[i - w + 1:i + 1]) / w


def _ma_holds(dates, closes, market_sessions, anchor):
    if len(closes) < 2:
        return _unknown_ma_hold()
    out = {}
    for w in MA_WINDOWS:
        key = f"ma{w}"
        if len(closes) < w:
            out[key] = {"status": "unavailable", "run": None}
            continue
        run = 0
        for i in range(len(closes) - 1, w - 2, -1):
            if closes[i] > _rolling_ma(closes, i, w):
                run += 1
            else:
                break
        if run and not _contiguous_to_anchor(dates[len(dates) - run:],
                                             market_sessions,
                                             dates[len(dates) - run], anchor):
            out[key] = {"status": "unavailable", "run": None}
        else:
            out[key] = {"status": "available", "run": run}
    return out


def _cluster_hold(dates, closes, market_sessions, anchor, *, volumes=None):
    if len(closes) < 2:
        return _unknown_cluster_hold()
    missing = [w for w in CLUSTER_WINDOWS if len(closes) < w]
    base = {"cluster_windows": list(CLUSTER_WINDOWS), "missing_windows": missing}
    if missing:
        return {**base, "status": "unavailable", "run": None,
                "reclaim_session_volume_ratio": None}
    longest = max(CLUSTER_WINDOWS)

    def _above_all(i):
        return all(closes[i] > _rolling_ma(closes, i, w) for w in CLUSTER_WINDOWS)

    run = 0
    for i in range(len(closes) - 1, longest - 2, -1):
        if _above_all(i):
            run += 1
        else:
            break
    if run and not _contiguous_to_anchor(dates[len(dates) - run:], market_sessions,
                                         dates[len(dates) - run], anchor):
        return {**base, "status": "unavailable", "run": None,
                "reclaim_session_volume_ratio": None}
    ratio = None
    if run:
        rs = len(closes) - run
        # Reclaim-boundary rule: the session before the run must have
        # computable cluster MAs AND fail the condition, else no reclaim.
        if rs - 1 >= longest - 1 and not _above_all(rs - 1):
            ratio = _reclaim_volume_ratio(volumes, dates, rs)
    return {**base, "status": "available", "run": run,
            "reclaim_session_volume_ratio": ratio}


def _cohort_composite(series_by_ticker, window):
    """Median of each member's close normalised to its first close in `window`.
    Fixed membership: a member must have a bar on EVERY window session."""
    members, norm = [], {}
    for t, pairs in series_by_ticker.items():
        by_date = {d: c for d, c in _normalise(pairs)}
        if all(d in by_date for d in window) and by_date[window[0]] > 0:
            members.append(t)
            norm[t] = {d: by_date[d] / by_date[window[0]] for d in window}
    composite = {}
    for d in window:
        vals = sorted(norm[t][d] for t in members)
        if vals:
            mid = len(vals) // 2
            composite[d] = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return composite, sorted(members)


def _is_composite_local_min(composite, window, date) -> bool:
    """Local minimum with a session on each side; at least one side strict."""
    if date not in composite:
        return False
    i = list(window).index(date)
    if i == 0 or i == len(window) - 1:
        return False
    a, v, b = composite.get(window[i - 1]), composite[date], composite.get(window[i + 1])
    if a is None or b is None:
        return False
    return v <= a and v <= b and (v < a or v < b)


def select_cohort_event(series_by_ticker, *, market_sessions, anchor_session,
                        max_staleness_sessions,
                        search_window_sessions=EVENT_SEARCH_WINDOW_SESSIONS):
    """Select the shared selloff event from SERIES ONLY (never drawdown
    status — survivor bias would erase fully recovered leaders)."""
    cal = list(market_sessions)
    n_uni = len(series_by_ticker)
    # Ceil-half, NOT round(): ties-to-even would give 6 of 13.
    quorum_min = max(QUORUM_MIN_ABSOLUTE, (n_uni + 1) // 2)
    out = {
        "status": "unavailable", "reason": None,
        "selection_basis": "modal_window_minimum_close",
        "search_window_sessions": search_window_sessions,
        "max_staleness_sessions": max_staleness_sessions,
        "quorum_min_members": quorum_min,
        "quorum_min_modal_count": QUORUM_MIN_MODAL_COUNT,
        "anchor_session": anchor_session,
        "universe_members": sorted(series_by_ticker),
        "excluded_members": [], "total_universe": n_uni,
        "eligible_member_count": 0, "eligible_member_share_pct": None,
        "trough_session": None, "peak_session": None, "tied_sessions": [],
        "trough_date_counts": {}, "modal_count": 0, "share_of_universe_pct": None,
        "sessions_since_trough": None, "oldest_session": None,
        "oldest_session_min_count": 0, "oldest_session_min_share_pct": None,
        "modal_is_oldest_session": False, "composite_local_min_confirmed": False,
        "peak_truncated": False,
    }
    if not series_by_ticker or anchor_session not in cal:
        out["reason"] = "no_universe_or_anchor_off_calendar"
        return out
    end = cal.index(anchor_session)
    window = cal[max(0, end - search_window_sessions + 1):end + 1]
    if len(window) < search_window_sessions:
        # v3 accepted any length >= 3, silently reporting a 21-session window
        # it did not have. Enforced now.
        out["reason"] = "calendar_shorter_than_search_window"
        return out
    out["oldest_session"] = window[0]

    minima, excluded = {}, []
    for t, pairs in series_by_ticker.items():
        by_date = {d: c for d, c in _normalise(pairs) if d in window}
        if len(by_date) != len(window):
            excluded.append(t)          # partial window: its observed minimum
            continue                    # is not its window minimum — no vote
        lowest = min(by_date.values())
        minima[t] = min(d for d, c in by_date.items() if c == lowest)
    out["excluded_members"] = sorted(excluded)
    out["eligible_member_count"] = len(minima)
    out["eligible_member_share_pct"] = round(len(minima) / n_uni * 100, 2)

    counts = {}
    for d in minima.values():
        counts[d] = counts.get(d, 0) + 1
    out["trough_date_counts"] = dict(sorted(counts.items()))
    out["oldest_session_min_count"] = counts.get(window[0], 0)
    out["oldest_session_min_share_pct"] = round(
        counts.get(window[0], 0) / n_uni * 100, 2)

    if len(minima) < quorum_min:
        out["reason"] = "insufficient_eligible_members"
        return out
    top = max(counts.values())
    tied = sorted(d for d, c in counts.items() if c == top)
    out["modal_count"] = top
    out["share_of_universe_pct"] = round(top / n_uni * 100, 2)
    if top < QUORUM_MIN_MODAL_COUNT:
        out["reason"] = "insufficient_modal_support"
        return out
    if len(tied) > 1:
        out["status"] = "ambiguous"
        out["tied_sessions"] = tied
        return out

    modal = tied[0]
    out["trough_session"] = modal
    out["sessions_since_trough"] = end - cal.index(modal)
    out["modal_is_oldest_session"] = modal == window[0]
    composite, _members = _cohort_composite(series_by_ticker, window)
    out["composite_local_min_confirmed"] = _is_composite_local_min(
        composite, window, modal)
    # (No separate composite quorum: composite membership uses the identical
    # complete-window criterion as voter eligibility — v3's second check was
    # redundant and is deliberately absent.)

    if out["modal_is_oldest_session"]:
        out["status"] = "window_truncated"
        out["reason"] = "modal_trough_on_oldest_window_session"
        return out
    if not out["composite_local_min_confirmed"]:
        out["reason"] = "composite_not_local_min"
        return out

    pre = [d for d in window[:window.index(modal)] if d in composite]
    out["peak_session"] = max(pre, key=lambda d: composite[d]) if pre else None
    out["peak_truncated"] = out["peak_session"] == window[0] if pre else False
    out["status"] = ("stale" if out["sessions_since_trough"] > max_staleness_sessions
                     else "fresh")
    out["reason"] = None
    return out


_EVENT_USABLE: Final[frozenset] = frozenset({"fresh", "stale"})


def compute_cohort_recovery(event, series_by_ticker, *, market_sessions):
    """Per-member recovery over the SHARED event interval.

    Universe-dependent by design — #4 defines strength relative to the
    collective rebound, so the anchor belongs to the cohort, not the ticker.
    `event` is consumed immutably; nothing may re-derive it from this output.
    """
    out = {**event, "members": {}}
    trough, peak = event.get("trough_session"), event.get("peak_session")
    anchor = event.get("anchor_session")
    if event.get("status") not in _EVENT_USABLE or not trough or not anchor:
        return out
    for t, pairs in series_by_ticker.items():
        by_date = {d: c for d, c in _normalise(pairs)}
        prior = [d for d in by_date if d < trough]
        m = {"status": "available", "reason": None,
             "close_at_cohort_peak": None, "close_at_cohort_trough": None,
             "anchor_close": by_date.get(anchor),
             "pct_since_cohort_trough": None, "pct_vs_cohort_peak": None,
             "last_close_before_cohort_trough": by_date[max(prior)] if prior else None}
        if trough not in by_date:
            m.update(status="unavailable", reason="missing_cohort_trough_close")
        elif anchor not in by_date:
            m.update(status="unavailable", reason="missing_anchor_close")
        else:
            tc, ac = by_date[trough], by_date[anchor]
            m["close_at_cohort_trough"] = tc
            m["pct_since_cohort_trough"] = round((ac / tc - 1) * 100, 4)
            if peak and not event.get("peak_truncated") and peak in by_date:
                pc = by_date[peak]
                m["close_at_cohort_peak"] = pc
                m["pct_vs_cohort_peak"] = round((ac / pc - 1) * 100, 4)
        out["members"][t] = m
    return out
