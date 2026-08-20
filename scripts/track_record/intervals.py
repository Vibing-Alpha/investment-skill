"""Half-open `[start, end)` UTC intervals, and nothing else.

Every window in this package — a pull's covered range, a quarter's bounds,
the union a coverage check merges — is half-open and UTC. Keeping the
arithmetic in one module is what makes "do two consecutive pulls abut?" a
question with one answer: `[a, b)` and `[b, c)` join, because the first
excludes `b` and the second includes it.

An interval with `start >= end` covers no instant and is dropped rather
than merged. Treating it as covering something would be an over-claim, and
over-claiming coverage is the single failure the caller cannot tolerate:
it turns a missing pull into a rendered number.
"""

from __future__ import annotations

from datetime import datetime

Interval = tuple[datetime, datetime]


def merge(intervals: list[Interval]) -> list[Interval]:
    """`intervals` sorted, disjoint, and with abutting pairs joined.

    Empty or inverted intervals are dropped. The result is the smallest
    list covering exactly the same instants as the input.
    """
    usable = sorted((s, e) for s, e in intervals if s < e)
    merged: list[Interval] = []
    for start, end in usable:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def overlaps(a: Interval, b: Interval) -> bool:
    """Do `a` and `b` share an instant?

    Half-open, so abutting intervals do NOT overlap: `[x, y)` ends before
    `[y, z)` begins. An empty or inverted interval shares nothing with
    anything.

    It lives here rather than in the caller that needs it because the spec
    requires ONE interval implementation — a classifier comparing endpoints
    by hand is the second one, and the two drift.
    """
    return a[0] < a[1] and b[0] < b[1] and a[0] < b[1] and b[0] < a[1]


def intersect(a: Interval, b: Interval) -> Interval | None:
    """The half-open interval `a` and `b` share, or `None` when they share
    no instant.

    Half-open, so abutment yields `None`, not a zero-width interval:
    `[a,b)` and `[b,c)` are the same boundary `overlaps` draws, and this
    function must agree with it — `intersect(a, b) is not None` and
    `overlaps(a, b)` are the same question asked two ways. An empty or
    inverted `a` or `b` also yields `None`, for the same over-claim reason
    `merge`/`uncovered` drop one: a caller must never mistake "no shared
    instant" for "some interval was returned".

    Exists so a caller confining one interval to another (e.g. a pull's own
    uncorroborated stretch to the quarter being reported) does not hand-roll
    `max(start)`/`min(end)` endpoint comparison outside this module — the
    second implementation the module docstring warns drifts from the first.
    """
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    if lo < hi:
        return (lo, hi)
    return None


def uncovered(bounds: Interval, intervals: list[Interval]) -> list[Interval]:
    """The parts of `bounds` no interval in `intervals` covers.

    Empty exactly when `bounds` is fully covered — so a caller tests
    coverage with `not uncovered(...)` and reports the holes with the same
    call, rather than answering "covered?" and "where is the hole?"
    separately and risking two answers that disagree.
    """
    lo, hi = bounds
    if lo >= hi:
        return []
    holes: list[Interval] = []
    cursor = lo
    for start, end in merge(intervals):
        if end <= cursor:
            continue
        if start >= hi:
            break
        if start > cursor:
            holes.append((cursor, min(start, hi)))
        cursor = max(cursor, end)
        if cursor >= hi:
            return holes
    if cursor < hi:
        holes.append((cursor, hi))
    return holes
