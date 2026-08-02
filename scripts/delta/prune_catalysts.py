"""Drop past catalysts from a calendar list."""

from __future__ import annotations

import datetime
from typing import List


def prune_past(
    catalyst_calendar: List[dict], today: datetime.date
) -> List[dict]:
    """Return a new list: entries with date >= today, sorted ascending by date.
    Entries missing a parseable date are preserved (can't classify as past).
    """
    future: List[dict] = []
    undated: List[dict] = []
    for entry in catalyst_calendar:
        # Fail-open: skip non-dict entries (str/None leaked through
        # malformed events.json) rather than crash the thesis reuse path.
        if not isinstance(entry, dict):
            continue
        raw_date = entry.get("date")
        if not raw_date:
            undated.append(entry)
            continue
        try:
            # Probe 4I: truncate to the date prefix — the same [:10]
            # normalization diff_catalysts_in_window uses. Pre-fix a
            # `2026-04-18T12:00:00Z` datetime raised here, classified the
            # entry "undated", and the PAST catalyst survived pruning.
            d = datetime.date.fromisoformat(raw_date[:10])
        except (ValueError, TypeError):
            undated.append(entry)
            continue
        if d >= today:
            future.append(entry)

    future.sort(key=lambda e: e.get("date", ""))
    # Preserve undated entries at the top (they sort by insertion order)
    return undated + future
