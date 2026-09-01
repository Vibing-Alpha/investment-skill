"""ET trading day helpers for the delta-update layer.

Uses US/Eastern timezone via zoneinfo (stdlib in Python 3.9+).
Trading day = weekday minus NYSE holidays. We don't need a full
holiday calendar for personal use; a static list of major US market
closures covers the common case. If this becomes a problem, swap in
the `pandas_market_calendars` library.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# US market holidays — cover current year + next to prevent silent
# trading-day misclassification at year boundaries. Extend as years
# roll over (it's a ~5-minute maintenance task per year).
_FIXED_HOLIDAYS = {
    # 2026
    datetime.date(2026, 1, 1),    # New Year
    datetime.date(2026, 1, 19),   # MLK
    datetime.date(2026, 2, 16),   # Presidents Day
    datetime.date(2026, 4, 3),    # Good Friday
    datetime.date(2026, 5, 25),   # Memorial Day
    datetime.date(2026, 6, 19),   # Juneteenth
    datetime.date(2026, 7, 3),    # July 4 observed
    datetime.date(2026, 9, 7),    # Labor Day
    datetime.date(2026, 11, 26),  # Thanksgiving
    datetime.date(2026, 12, 25),  # Christmas
    # 2027
    datetime.date(2027, 1, 1),    # New Year
    datetime.date(2027, 1, 18),   # MLK
    datetime.date(2027, 2, 15),   # Presidents Day
    datetime.date(2027, 3, 26),   # Good Friday
    datetime.date(2027, 5, 31),   # Memorial Day
    datetime.date(2027, 6, 18),   # Juneteenth observed (Sat)
    datetime.date(2027, 7, 5),    # July 4 observed (Mon)
    datetime.date(2027, 9, 6),    # Labor Day
    datetime.date(2027, 11, 25),  # Thanksgiving
    datetime.date(2027, 12, 24),  # Christmas observed (Fri — Dec 25 falls Sat)
    # 2028
    datetime.date(2028, 1, 17),   # MLK
    datetime.date(2028, 2, 21),   # Presidents Day
    datetime.date(2028, 4, 14),   # Good Friday
    datetime.date(2028, 5, 29),   # Memorial Day
    datetime.date(2028, 6, 19),   # Juneteenth
    datetime.date(2028, 7, 4),    # July 4
    datetime.date(2028, 9, 4),    # Labor Day
    datetime.date(2028, 11, 23),  # Thanksgiving
    datetime.date(2028, 12, 25),  # Christmas
    # 2029
    datetime.date(2029, 1, 1),    # New Year
    datetime.date(2029, 1, 15),   # MLK
    datetime.date(2029, 2, 19),   # Presidents Day
    datetime.date(2029, 3, 30),   # Good Friday
    datetime.date(2029, 5, 28),   # Memorial Day
    datetime.date(2029, 6, 19),   # Juneteenth
    datetime.date(2029, 7, 4),    # July 4
    datetime.date(2029, 9, 3),    # Labor Day
    datetime.date(2029, 11, 22),  # Thanksgiving
    datetime.date(2029, 12, 25),  # Christmas
    # 2030
    datetime.date(2030, 1, 1),    # New Year
    datetime.date(2030, 1, 21),   # MLK
    datetime.date(2030, 2, 18),   # Presidents Day
    datetime.date(2030, 4, 19),   # Good Friday
    datetime.date(2030, 5, 27),   # Memorial Day
    datetime.date(2030, 6, 19),   # Juneteenth
    datetime.date(2030, 7, 4),    # July 4
    datetime.date(2030, 9, 2),    # Labor Day
    datetime.date(2030, 11, 28),  # Thanksgiving
    datetime.date(2030, 12, 25),  # Christmas
}

# The last year the holiday list explicitly covers. Beyond this,
# `is_trading_day` falls through to the weekday-only check, silently
# returning True for holidays that land on weekdays. Callers can check
# this value to warn / fail instead of producing wrong output.
_HOLIDAY_COVERAGE_LAST_YEAR = 2030
# The table's FIRST year, derived rather than restated so it cannot drift
# from the data. A date below it is as uncovered as one above it: every
# weekday holiday there reads as an open session. `is_trading_day` does not
# refuse such a date — staleness math on a past date is still wanted — but a
# caller that INFERS a missing session from the calendar must not claim one
# outside the covered span (`scripts.indicators`' `missing_interior_sessions`
# named Thanksgiving and Christmas 2025 as dropped bars before this bound
# existed).

# Probe 4G: NYSE half-days close 13:00 ET, not 16:00 — without this table
# a 14:00 invocation on Black Friday anchored to the PRECEDING session and
# the half-day's data landed under the wrong date dir. Sessions: the day
# after Thanksgiving, July 3 (when a weekday and July 4 is a market
# weekday-holiday), Christmas Eve (when a weekday and not itself the
# observed holiday). Maintained alongside _FIXED_HOLIDAYS (same yearly
# ~5-minute task).
_EARLY_CLOSES = {
    # 2026: Jul 3 is the observed July-4 holiday (full close, in
    # _FIXED_HOLIDAYS); Christmas Eve Thu 12-24; Black Friday 11-27.
    datetime.date(2026, 11, 27): 13,
    datetime.date(2026, 12, 24): 13,
    # 2027: Dec 24 is the observed Christmas holiday; July 4 falls Sunday
    # (observed Mon 7-5, no half-day). Only Black Friday.
    datetime.date(2027, 11, 26): 13,
    # 2028
    datetime.date(2028, 7, 3): 13,
    datetime.date(2028, 11, 24): 13,
    # (Dec 24 2028 is a Sunday — no session)
    # 2029
    datetime.date(2029, 7, 3): 13,
    datetime.date(2029, 11, 23): 13,
    datetime.date(2029, 12, 24): 13,
    # 2030
    datetime.date(2030, 7, 3): 13,
    datetime.date(2030, 11, 29): 13,
    datetime.date(2030, 12, 24): 13,
}


HOLIDAY_COVERAGE_FIRST_YEAR = min(d.year for d in _FIXED_HOLIDAYS)
HOLIDAY_COVERAGE_LAST_YEAR = _HOLIDAY_COVERAGE_LAST_YEAR


def holiday_calendar_covers(d: datetime.date) -> bool:
    """Whether the maintained holiday table can speak about this date."""
    return HOLIDAY_COVERAGE_FIRST_YEAR <= d.year <= HOLIDAY_COVERAGE_LAST_YEAR


def _close_hour_et(d: datetime.date) -> int:
    """Market close hour (ET, 24h) for trading day d — 13 on half-days."""
    return _EARLY_CLOSES.get(d, 16)


def session_close_et(d: datetime.date) -> datetime.datetime:
    """The scheduled regular-session close for trading day `d`, as an
    aware ET datetime (16:00, or 13:00 on a known early-close day).

    Public counterpart to `_close_hour_et`, for callers that need to
    compare a DATA timestamp against the close rather than the wall clock
    — `scripts.indicators` uses it to decide whether the trailing daily
    bar in a price artifact is a completed session or a mid-session stub.
    Because it takes the date rather than reading the clock, callers stay
    deterministic (tests/test_determinism.py).

    Raises ValueError on a non-trading day: there is no close to compare
    against, and returning a fabricated 16:00 would silently classify a
    weekend/holiday bar as complete.
    """
    if not is_trading_day(d):
        raise ValueError(f"{d.isoformat()} is not a US trading day")
    return datetime.datetime(
        d.year, d.month, d.day, _close_hour_et(d), 0, 0, tzinfo=ET,
    )


def _now_utc() -> datetime.datetime:
    """Seam for testing."""
    return datetime.datetime.now(datetime.timezone.utc)


def today_et() -> datetime.date:
    """The current ET calendar day at invocation time."""
    return _now_utc().astimezone(ET).date()


_coverage_warned = False


def is_trading_day(d: datetime.date) -> bool:
    """True iff d is a weekday AND not a known US market holiday."""
    # Round-23: beyond the maintained table, weekday holidays silently
    # classify as OPEN sessions (a 2031 MLK Monday read as a trading day
    # → thesis staleness understated → false 'fresh'). The constant
    # existed but nothing checked it — warn loudly (once per process) so
    # the yearly table extension can't be silently forgotten.
    global _coverage_warned
    if d.year > _HOLIDAY_COVERAGE_LAST_YEAR and not _coverage_warned:
        _coverage_warned = True
        import sys
        print(
            f"[WARN] delta.calendar: {d.isoformat()} is beyond the "
            f"holiday-calendar coverage (last maintained year "
            f"{_HOLIDAY_COVERAGE_LAST_YEAR}) — weekday holidays will be "
            f"misclassified as open sessions and staleness checks can "
            f"understate age. Extend _FIXED_HOLIDAYS + _EARLY_CLOSES "
            f"(yearly ~5-minute task).",
            file=sys.stderr,
        )
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    if d in _FIXED_HOLIDAYS:
        return False
    return True


def last_closed_trading_day(now: datetime.datetime | None = None) -> datetime.date:
    """Most recent completed ET trading session.

    Rule: if now is on a trading day after 16:00 ET, today counts; else
    walk back to the most recent trading day.
    """
    now = now or _now_utc()
    # Cold-round finding: a NAIVE datetime is silently interpreted in the
    # host timezone by astimezone(), shifting the selected session on
    # non-ET machines. All internal callers pass aware datetimes (_now_utc);
    # reject naive input loudly rather than mis-anchor.
    if now.tzinfo is None:
        raise ValueError(
            "last_closed_trading_day requires a timezone-aware datetime "
            "(naive input would be read in the HOST timezone, mis-anchoring "
            "the session on non-ET machines)"
        )
    now_et = now.astimezone(ET)
    candidate = now_et.date()

    # Today counts only if it's a trading day and we're past market close
    # (16:00 ET, or 13:00 on half-days — probe 4G).
    if is_trading_day(candidate) and now_et.hour >= _close_hour_et(candidate):
        return candidate

    # Walk back
    d = candidate - datetime.timedelta(days=1)
    while not is_trading_day(d):
        d -= datetime.timedelta(days=1)
    return d


def session_et(now: datetime.datetime | None = None) -> datetime.date:
    """Session anchor for directory allocation and same-day checks.

    Semantics: the trading day whose close is being analyzed right now.
    Pre-market-close (or weekend/holiday): the previous trading day.
    Post-close on a trading day: that day.

    This differs from today_et() (calendar date) and is stable across
    ET midnight for one continuous analysis session — two consecutive
    skill invocations spanning midnight still land in the same date dir.
    """
    return last_closed_trading_day(now)


def days_between_et(a: datetime.date, b: datetime.date) -> int:
    """Calendar-day delta b - a (positive if b > a)."""
    return (b - a).days


def trading_days_between(start: datetime.date, end: datetime.date) -> int:
    """Number of trading sessions in the half-open interval (start, end].

    i.e. how many trading days fall strictly AFTER `start` and on-or-before
    `end`. Returns 0 when `end <= start`. Used for trading-day-aware
    staleness so a Friday close consumed on the weekend (or the day after a
    market holiday) reads as 0 sessions stale rather than N calendar days.

    Holiday awareness is bounded by `_FIXED_HOLIDAYS` coverage: beyond the
    last covered year, a weekday holiday is counted as a session (an
    over-count that errs toward flagging staleness — the conservative
    direction). See the coverage note near `_FIXED_HOLIDAYS`.
    """
    if end <= start:
        return 0
    n = 0
    d = start + datetime.timedelta(days=1)
    while d <= end:
        if is_trading_day(d):
            n += 1
        d += datetime.timedelta(days=1)
    return n
