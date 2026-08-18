"""Counting + money-path layer for the personal track-record capture tool.

Turns the archive (raw broker pulls) and the journal (hand-tagged theses)
into the deterministic per-quarter counts §6.1 defines: fills, orders,
order_unknown, orders_linked, thesis_versions — every number here is an
integer tally or a refusal, never a derived ratio.

Task 6 adds the §6.2 money-path layer to this same file: the pinned SPY
benchmark (`ensure_pinned`/`read_pinned`), the NAV/cps observation
flattener (`observations_from`/`to_cps_decimal`/`observations_to_series`),
the endpoint and gap gates (`pick_endpoints`/`gap_reason`), and the two
figures a reader actually sees — RISK (`quarterly_drawdown`) and CONTEXT
(`quarterly_twr` + `spy_return` via `context_pp`). This is the ONLY
money-path arithmetic in the module; everything above it stays an integer
tally.

Design: docs/superpowers/plans/2026-08-16-track-record.md (Tasks 5-6), spec
§5.1 (aggregation and conflicts), §5.2 (durability — pin exclusivity), §6.1
(counting) and §6.2 (money-path computation).
Freeze decisions this module compiles against (see freeze file, D1/D2):
- D1: `get_account_orders` reports LIVE working orders only — there is no
  historical order snapshot to verify a journal link against. `orders_linked`
  is therefore PERMANENTLY `unavailable (orders_linked disabled: ...)`,
  compiled in below as `_ORDERS_LINKED_DISABLED_REASON` — not a runtime
  flag, and not conditioned on `known_orders`'s own value.
- D2: no `account` field exists on any real broker row (single-account
  login) — the frozen constant `_SINGLE_ACCOUNT` fills that slot
  unconditionally; a row's own `account` key, if one is ever present, is
  never read (schema drift must not silently become a new grouping key).
  The raw execution-key field is `trade_id`, and only `trade_id` — no
  fallback name is accepted. `order_id` arrives as an `int` on trade rows;
  both `fills_from` and `known_orders_from` coerce every identifier to
  `str` before it is ever compared, ordered, or used as a dict/set key.
- D3: fail-close on an execution conflict. Two archived rows sharing
  `(account, trade_id)` and differing in any of D2's compared fields
  (`symbol`, `side`, `size`, `price`, `trade_time`, `order_id` — NOT
  `commission`/`net_amount`/`realized_pnl`, which are derived and may be
  restated without the execution changing) refuse the whole `fills_from`
  result. No corrected or busted execution was observed in either probe
  window, so this builds the refusal, not a version chain. An agreeing
  duplicate — the normal case for overlapping pulls — is not a conflict:
  held once, by key.
- D5: `cps` is DECIMAL (0.48041759 = 48.04%), already the scale every
  formula below needs — `to_cps_decimal` is an identity pass, kept as a
  named gate anyway because it is the one place the scale is asserted.
  Observation dates arrive as compact `YYYYMMDD` (`"20260816"`), never
  `YYYY-MM-DD` — canonicalised on the way in, same discipline as
  `trade_time` above. **`nav` is the raw account balance and is NOT
  flow-adjusted; `cps` is TWR and is** — this account has taken real
  external cash flows, so RISK (`quarterly_drawdown`) is computed from
  `(1 + cps)`, never from `nav`, and `observations_from` does not extract
  `nav` at all. See the freeze file's D5 section for the live reconciliation
  that forced this.
Freeze file: docs/superpowers/plans/2026-08-16-track-record-freeze.md
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.track_record.archive import (
    Envelope, ParseFailure, coverage_gap, read_envelopes, write_bytes_new)
from scripts.track_record.journal import Fold, FoldError, load_fold

# D2 froze ABSENT: no real broker row (trade or order) carries an `account`
# field at all. This is not a field the code chooses to ignore — the IBKR MCP
# wire format has nowhere to put one: none of the five tools takes an account
# parameter, and the only response with an `accounts` structure keys it by the
# literal singular string `"account"` with no identifier inside it.
#
# So `U1` is the ARCHIVE's namespace for "the one account this connection
# reaches", not a stand-in for a real broker id. Every producer in this module
# fills the slot with it, full stop.
#
# What this costs, stated the right way round: the tool cannot tell two
# accounts apart, so an archive root that ever spans more than one of them
# merges their orders under one key. That is the supported boundary, and it is
# recorded as a KNOWN ISSUE under D2 in the freeze file — read that before
# widening this. (An earlier version of this comment justified the constant as
# protection against a stray `account` key "silently becoming the grouping
# key". That has the direction backwards: what the unconditional substitution
# actually discards is a REAL account value, and no code gate can recover the
# case that matters, because the rows carry no account either way.)
_SINGLE_ACCOUNT = "U1"

# D1 froze NOT RETAINED: `get_account_orders` reports only currently-working
# orders, never a historical snapshot. No `orders_linked` event can ever be
# verified against one, so the count is permanently disabled — compiled in
# here, not a runtime flag threaded through `known_orders`. Every other
# count in `Counts` is unaffected (brief constraint 6).
_ORDERS_LINKED_DISABLED_REASON = (
    "unavailable (orders_linked disabled: D1 froze NOT RETAINED — "
    "get_account_orders reports only working orders, so no link can ever "
    "be verified against a historical snapshot)"
)

_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")

# D2/D3 froze exactly these six as "differing in content": symbol, side,
# size, price, trade_time, order_id. `commission`, `net_amount` and
# `realized_pnl` are deliberately excluded — they are derived, and a broker
# may restate them without the execution itself having changed.
# D2/D3 froze SIX fields as "differing in content". Live session 2026-08-18
# amended that to four-plus-a-tolerance, on evidence D3 explicitly lacked
# ("Not observed": the probe's two overlapping pulls agreed on all 431 shared
# trade_ids). Two pulls two days apart disagreed on 9 of 792:
#
#   - 7 distinct `order_id`s were replaced (the old ids gone from the broker
#     entirely) while symbol/side/size/price/trade_time/commission/net_amount
#     were byte-identical. D2 excluded commission/net_amount/realized_pnl as
#     "derived and may be restated without the execution having changed" —
#     that criterion describes `order_id` too. It is the broker's attribution
#     handle, not a property of the execution.
#   - one execution's `trade_time` moved 14:46:58Z -> 14:46:57Z, everything
#     else identical.
#
# Treating either as a conflict blanked the WHOLE fills result, which blocked
# `unlinked` and every DATA count outright — fail-close working exactly as
# designed, on a difference that was not a difference.
_COMPARED_FIELDS = ("symbol", "side", "size", "price")

# `trade_time` is compared SEPARATELY, with a tolerance, rather than dropped:
# it is the execution fact that separates two same-symbol same-price fills,
# and losing it would give away the ability to catch an execution genuinely
# restated to a different moment. One second is exactly the restatement
# observed; anything beyond it still fails closed.
_TRADE_TIME_TOLERANCE_SECONDS = 1

# Canonical UTC "Z" form, shared by `trade_time` (post fills_from) and
# journal `recorded_at` (validated at journal.py's write boundary already).
_CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# spec §6.1: "thesis versions ... by declaring event (thesis_opened and
# thesis_superseded)" — every other event type (orders_linked,
# thesis_retired, prediction_resolved) is a fact ABOUT an already-declared
# version, not a new one.
_DECLARING_EVENT_TYPES = frozenset({"thesis_opened", "thesis_superseded"})

# --- Task 6: benchmark pin and money-path computation (spec §5.2, §6.2) ---

# Calendar-date-only canonical form (no time component) — every date this
# section handles (SPY pin dates, NAV/cps observation dates, quarter
# endpoints) is a trading DAY, never an instant, unlike `_CANONICAL_FORMAT`
# above which is `trade_time`'s canonical UTC instant.
_CANONICAL_DATE_FORMAT = "%Y-%m-%d"

# D5's real wire format for a performance-envelope observation date:
# "20260816" — compact YYYYMMDD, no separators, no time component.
_COMPACT_DATE_RE = re.compile(r"^\d{8}$")

# spec §6.2 / brief constraint 8: "> 5 calendar days" — stated as a name so
# a fixture carrying a month-sized gap does not pass under a silently wrong
# threshold.
#
# Rationale for 5, not some other number: it brackets
# the two failure directions a bare gap-day-count cannot otherwise
# distinguish. LOWER than 5 rejects a legitimate market closure — a
# Thanksgiving Thursday plus its adjoining weekend, or a Christmas/New
# Year's run, can chain up to 4 consecutive non-trading calendar days with
# every trading day around it genuinely observed. HIGHER than 5 admits a
# series that is actually missing real trading-day observations — most
# dangerously right at a quarter's own edges (`d0`/`d1`), which is exactly
# where a stale or one-session-short pull (see F2) needs to be caught, not
# waved through. 5 is the tightest bound that clears ordinary closures
# without opening that second door.
_MAX_GAP_DAYS = 5

_UNAVAILABLE_PREFIX = "unavailable ("


@dataclass(frozen=True)
class Counts:
    """One quarter's §6.1 tallies. Every field is `int | str` — a `str` is
    always an `unavailable (...)` reason, never a placeholder number. A
    plain `0` therefore always means a confirmed, counted zero, never
    "could not be computed"."""

    fills: int | str
    orders: int | str
    order_unknown: int | str
    orders_linked: int | str
    thesis_versions: int | str


def quarter_bounds(quarter: str) -> tuple[datetime, datetime]:
    """Half-open UTC `[start, end)` bounds for `"YYYYQn"`. Called once by
    `assemble` and threaded through as `bounds` to every windowed gate
    (`count_quarter`, and later `pick_endpoints`/`gap_reason`) — so all of
    them agree on what one quarter is, rather than each parsing the string
    itself."""
    m = _QUARTER_RE.match(quarter)
    if not m:
        raise ValueError(f"invalid quarter {quarter!r}: expected form 'YYYYQn'")
    year, q = int(m.group(1)), int(m.group(2))
    month = q * 3 - 2
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if q == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 3, 1, tzinfo=timezone.utc)
    return start, end


def _canonical_utc(value: Any) -> str:
    """Parses a broker timestamp to an instant and re-renders it as
    canonical UTC `Z` (`YYYY-MM-DDTHH:MM:SSZ`). Raises `ValueError` /
    `TypeError` on anything that does not resolve to a tz-aware instant —
    a timestamp that cannot be parsed is a refusal, not a guess (brief
    `fills_from` interface note)."""
    if not isinstance(value, str):
        raise TypeError(f"trade_time must be a string, got {type(value).__name__}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"trade_time {value!r} has no timezone")
    return dt.astimezone(timezone.utc).strftime(_CANONICAL_FORMAT)


def _parse_canonical(value: str) -> datetime:
    """Parses an already-canonical UTC `Z` timestamp (post `fills_from` /
    post `journal.validate_event`) back to a tz-aware instant for bounds
    comparison. Not a second normalization pass — `fills_from` and the
    journal's own write-time validation already own that; this only reads
    the form they guarantee."""
    return datetime.strptime(value, _CANONICAL_FORMAT).replace(tzinfo=timezone.utc)


def _rows_or_reason(
    envelopes: list, list_key: str, gate_name: str
) -> list[tuple[dict, Path]] | str:
    """Every row of `response[list_key]` across `envelopes`, paired with the
    archive path it came from — or an `unavailable (...)` reason if any
    envelope failed to parse, or a successfully-parsed envelope's response
    does not carry the expected `list_key` row list.

    Fail-closed (brief `fills_from` interface note): a `ParseFailure` or a
    malformed response means this module cannot know what rows that pull
    held, and that unknown can never be read as "contributed zero rows" —
    it must blank the whole gate, the same way `archive.coverage_gap`
    blanks on any `ParseFailure` in the envelope list.
    """
    rows: list[tuple[dict, Path]] = []
    for e in envelopes:
        if isinstance(e, ParseFailure):
            return (
                f"unavailable ({gate_name}: archive contains an unparseable "
                f"file {e.path.as_posix()} — {e.reason})"
            )
        if not isinstance(e, Envelope):
            continue
        response = e.response
        row_list = response.get(list_key) if isinstance(response, dict) else None
        if not isinstance(row_list, list):
            return (
                f"unavailable ({gate_name}: envelope {e.path.as_posix()} "
                f"response carries no {list_key!r} row list)"
            )
        for row in row_list:
            rows.append((row, e.path))
    return rows


def _normalize_fill_row(row: Any, path: Path) -> dict | str:
    """One `fills_from` row normalized to the frozen eight fields, or an
    `unavailable (...)` reason. D2's row-level execution-key field is
    `trade_id`, and only `trade_id` — no fallback name is accepted, since a
    real archive row never carries anything else there. `order_id` is
    `None` when the row carries none — never a dropped row, never a
    synthesized key.

    `side`/`size`/`price` are carried through RAW (whatever the row
    carries, including `None` if absent) — the same D2-confirmed fields
    `_compare_snapshot` already reads for the conflict check below, just no
    longer dropped before they reach a caller. `unlinked` is the only consumer, and it exists to let a human
    recognize which decision an order was — printing only
    `account,order_id,symbol` gave no way to do that across a real
    quarter's hundreds of orders. Carrying these three costs nothing
    upstream (`count_quarter` reads only `account`/`order_id`/`trade_time`
    and ignores unknown keys), and stops `unlinked` from discarding data the
    archive already holds."""
    if not isinstance(row, dict):
        return f"unavailable (fills: non-object trade row in {path.as_posix()})"

    execution_id_raw = row.get("trade_id")
    trade_time_raw = row.get("trade_time")
    if execution_id_raw is None or trade_time_raw is None:
        return (
            "unavailable (fills: row missing execution id or trade_time "
            f"in {path.as_posix()})"
        )

    try:
        trade_time = _canonical_utc(trade_time_raw)
    except (ValueError, TypeError) as exc:
        return (
            f"unavailable (fills: unparseable trade_time {trade_time_raw!r} "
            f"in {path.as_posix()}: {exc})"
        )

    order_id_raw = row.get("order_id")
    order_id = None if order_id_raw is None else str(order_id_raw)

    return {
        "account": _SINGLE_ACCOUNT,
        "execution_id": str(execution_id_raw),
        "order_id": order_id,
        "symbol": row.get("symbol"),
        "trade_time": trade_time,
        "side": row.get("side"),
        "size": row.get("size"),
        "price": row.get("price"),
    }


def _trade_times_agree(a: str, b: str) -> bool:
    """Two canonical-UTC trade timestamps for ONE execution key, within
    `_TRADE_TIME_TOLERANCE_SECONDS`.

    Both come from `_normalize_fill_row`, so both are canonical `Z` and
    parse. An unparseable value cannot reach here; if one ever did, the
    `ValueError` propagating is the fail-closed direction — this function
    must never answer "agree" on input it did not understand.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    if a == b:
        return True
    delta = datetime.strptime(a, fmt) - datetime.strptime(b, fmt)
    return abs(delta.total_seconds()) <= _TRADE_TIME_TOLERANCE_SECONDS


def _compare_snapshot(row: dict, order_id: str | None, trade_time: str) -> dict[str, Any]:
    """The six D2/D3-compared fields for one raw trade row, in the SAME
    normalized form `fills_from` already computes for `order_id` (`str` or
    `None`) and `trade_time` (canonical UTC) — comparing the RAW forms would
    flag the numeric-vs-string `order_id` drift
    `test_fills_from_coerces_identifiers_to_str` pins as NOT a conflict.
    `symbol`/`side`/`size`/`price` are compared as-is: D2 only froze
    identifier coercion, not these."""
    return {
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "size": row.get("size"),
        "price": row.get("price"),
        "trade_time": trade_time,
        "order_id": order_id,
    }


def fills_from(envelopes: list) -> list[dict] | str:
    """The seam between the archive and every count. Reads D1's
    `get_account_trades` response shape (`response["trades"]`) and
    normalizes each row to `{account, execution_id, order_id, symbol,
    trade_time, side, size, price}` — eight fields; there is still no
    stable instrument id on a trade row (D4), but `side`/`size`/`price` are
    no longer dropped (`unlinked` is the only place a
    person actually reads a fill, and without these three there was no way
    to recognize a trade from its printed line). `trade_time` comes out as
    canonical UTC (brief interface note); `account`/`execution_id`/`order_id`
    come out as `str` (`order_id` as `None` when absent) whatever the row
    carried; `side`/`size`/`price` come out RAW, unconverted — nothing here
    reads them as money-path numbers, only display.

    Any row that cannot be normalized — missing execution key, missing or
    unparseable timestamp — blanks the WHOLE result to `unavailable (...)`,
    never a silently dropped row: a dropped row undercounts and the count it
    produces looks entirely normal.

    D3 fail-close: two rows sharing the execution key `(account,
    execution_id)` are compared on `_COMPARED_FIELDS`. The first row seen
    under a key is the one held for comparison — which row is "first" does
    not matter, because a difference refuses either way. A match is an
    agreeing duplicate (the normal case for overlapping archive pulls) and
    is counted once, by key: the repeat is not appended again. A mismatch
    blanks the WHOLE result to `unavailable (execution conflict: ...)`,
    the same bounded shape as every other `fills_from` refusal — nothing on
    disk is touched, and no version chain is built (D3 froze fail-close,
    not "latest wins": no corrected/busted execution was observed).
    """
    raw = _rows_or_reason(envelopes, "trades", "fills")
    if isinstance(raw, str):
        return raw

    rows: list[dict] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row, path in raw:
        normalized = _normalize_fill_row(row, path)
        if isinstance(normalized, str):
            return normalized

        key = (normalized["account"], normalized["execution_id"])
        snapshot = _compare_snapshot(row, normalized["order_id"], normalized["trade_time"])

        prior = seen.get(key)
        if prior is None:
            seen[key] = snapshot
            rows.append(normalized)
            continue

        account, execution_id = key
        for field in _COMPARED_FIELDS:
            if prior[field] != snapshot[field]:
                return (
                    f"unavailable (execution conflict: account={account}, "
                    f"trade_id={execution_id}, differing in {field})"
                )
        if not _trade_times_agree(prior["trade_time"], snapshot["trade_time"]):
            return (
                f"unavailable (execution conflict: account={account}, "
                f"trade_id={execution_id}, differing in trade_time)"
            )
        # Agreeing duplicate — held once, by key; not appended again.

    return rows


def known_orders_from(envelopes: list) -> set[tuple[str, str]] | str:
    """Every `(account, order_id)` — `account` is unconditionally the
    frozen `_SINGLE_ACCOUNT` constant, `order_id` coerced to `str`, via the
    same substitution `fills_from` applies — found in any successfully
    parsed `get_account_orders` snapshot, or an `unavailable (...)` reason.

    Named (not a private helper) because it groups rows in its own right —
    a later task's gate list is exhaustive over every grouping this module
    performs. Under D1 = NOT RETAINED this has no live input in practice
    (`get_account_orders` reports only working orders, normally none), so
    the returned set is normally empty; built anyway so it starts answering
    the day the broker exposes order history.
    """
    raw = _rows_or_reason(envelopes, "orders", "known_orders")
    if isinstance(raw, str):
        return raw

    result: set[tuple[str, str]] = set()
    for row, path in raw:
        if not isinstance(row, dict):
            return f"unavailable (known_orders: non-object order row in {path.as_posix()})"
        order_id_raw = row.get("order_id")
        if order_id_raw is None:
            return (
                f"unavailable (known_orders: row missing order_id "
                f"in {path.as_posix()})"
            )
        result.add((_SINGLE_ACCOUNT, str(order_id_raw)))
    return result


def fold_or_reason(path) -> Fold | str:
    """Loads the journal fold at `path` (`journal.load_fold`), or converts
    a refusal into the `unavailable (...)` reason `count_quarter` renders
    for the two journal-derived counts. Three refusal shapes are caught:
    `FoldError` (Task 4's pinned schema/topology contract), `OSError`
    (`load_fold` -> `_read_events` -> `Path.read_text()`, which raises
    e.g. `IsADirectoryError` for a malformed path — a directory where the
    journal file should be, or a permissions error), and `UnicodeDecodeError`
    (same call, on a journal saved with invalid UTF-8 — a ValueError
    subclass, not an OSError, so it needs its own arm; this user writes
    zh-CN `intent` text by hand (typed at the tag step, edited in place at
    the correction step), so a stray non-UTF-8
    byte from a text editor is a real path here, not a corner case, I3
    whole-branch review) — all three degrade to a string rather than
    crash, since a broker/journal-adjacent refusal surfacing as an
    uncaught exception is exactly what this function exists to prevent."""
    try:
        return load_fold(path)
    except FoldError as exc:
        return f"unavailable ({exc.field}: {exc.message})"
    except (OSError, UnicodeDecodeError) as exc:
        return f"unavailable (journal: {exc})"


def _count_fills(
    fills: list[dict], start: datetime, end: datetime
) -> tuple[int, int, int]:
    """`(fills, orders, order_unknown)` for one quarter.

    PRECONDITION: `fills` is already unique by the execution key
    `(account, execution_id)` — this function does not dedup. `fills_from`
    is the sole producer of this argument (grep confirms no other call
    site) and it already guarantees the uniqueness: it IS the seam between
    the archive and every count, so collapsing two archived copies of one
    execution into one row is normalization, and normalization is its job,
    not this one's. Splitting that dedup across two functions would also
    split the D3 conflict check away from it — agreeing duplicates collapse
    and disagreeing ones refuse are two halves of one decision about the
    same key, and `.claude/rules/producer-consumer.md` rule 3 is explicit
    that the two halves WILL diverge if reimplemented in a second place.
    (Counting-only dedup used to live here too, defensively; removed
    because it was dead code — unreachable given the precondition, and
    untested, since no fixture in this file repeats an execution id.)

    Orders group on `(account, order_id)` (constraint 1 — `order_id` alone
    would merge two accounts' orders). A fill with no `order_id` counts
    toward `order_unknown`, never synthesized onto a neighbouring order
    (constraint 2).
    """
    in_bounds = [
        row
        for row in fills
        if start <= _parse_canonical(row["trade_time"]) < end
    ]

    order_keys: set[tuple[str, str]] = set()
    order_unknown = 0
    for row in in_bounds:
        if row["order_id"] is None:
            order_unknown += 1
        else:
            order_keys.add((row["account"], row["order_id"]))

    return len(in_bounds), len(order_keys), order_unknown


def _count_thesis_versions(fold: Fold, start: datetime, end: datetime) -> int:
    """spec §6.1: a thesis VERSION — not a thesis — is counted once, by the
    quarter containing its declaring event (`thesis_opened` or
    `thesis_superseded`). Reads `fold.events` directly (every event, in
    file order) rather than `fold.declared` (thesis_id -> ONE declaring
    event, collapsed): the routing here needs every declaring event's own
    `recorded_at`, and a non-declaring event sharing a `thesis_id` with a
    declared one must not be counted."""
    return sum(
        1
        for event in fold.events
        if event.get("event") in _DECLARING_EVENT_TYPES
        and start <= _parse_canonical(event["recorded_at"]) < end
    )


def count_quarter(
    fills: list[dict] | str,
    fold: Fold | str,
    bounds: tuple[datetime, datetime],
    known_orders: set[tuple[str, str]] | str,
) -> Counts:
    """§6.1's five counts for one quarter, given `bounds` from a single
    `quarter_bounds` call (never recomputed here).

    PRECONDITION: when `fills` is a `list`, it is expected unique by
    `(account, execution_id)` — `fills_from` guarantees this (it is the
    only producer of this argument) and `_count_fills` does not re-dedup.

    `fills`, `fold` and `known_orders` may each be an `unavailable (...)`
    string instead of their real value, and each blanks exactly the fields
    that depend on it — propagation is bounded (constraint 5):
    - `fills` unavailable -> `fills`, `orders`, `order_unknown` all blank
      (every fill-derived count; constraint 7).
    - `fold` unavailable -> `thesis_versions` blanks.
    - `orders_linked` is permanently disabled by the D1 freeze (constraint
      6), compiled in below — `known_orders`'s own value (a real set, an
      empty set, or its own `unavailable (...)` reason) never changes this;
      the parameter is accepted and validated for shape so a future
      re-enablement only has to remove the override, not the plumbing.
    """
    start, end = bounds

    if isinstance(fills, str):
        fills_n: int | str = fills
        orders_n: int | str = fills
        order_unknown_n: int | str = fills
    else:
        fills_n, orders_n, order_unknown_n = _count_fills(fills, start, end)

    if isinstance(fold, str):
        thesis_versions_n: int | str = fold
    else:
        thesis_versions_n = _count_thesis_versions(fold, start, end)

    # `known_orders` is intentionally unread while orders_linked is
    # compiled disabled (see module docstring / constraint 6 above).
    del known_orders

    return Counts(
        fills=fills_n,
        orders=orders_n,
        order_unknown=order_unknown_n,
        orders_linked=_ORDERS_LINKED_DISABLED_REASON,
        thesis_versions=thesis_versions_n,
    )


# --- Task 6: benchmark pin (§5.2) ---------------------------------------


def _unwrap_reason(value: str) -> str:
    """Strips one `unavailable (...)` shell if `value` already has one,
    else returns it unchanged. Used only when one gate's refusal is
    embedded inside another's (`context_pp`), so the reader sees a single
    `unavailable (...)` wrapper naming which side failed, not a nested
    `unavailable (... unavailable (...) ...)`.
    """
    if value.startswith(_UNAVAILABLE_PREFIX) and value.endswith(")"):
        return value[len(_UNAVAILABLE_PREFIX):-1]
    return value


def _canonical_calendar_date(value: Any) -> str:
    """One date, canonicalised to `YYYY-MM-DD`, converted from whatever
    spelling the source uses. Three spellings are recognized:
    - D5's real compact wire format, `"20260816"` (`YYYYMMDD`, no
      separators) — the performance envelope's observation dates.
    - An already-canonical `"YYYY-MM-DD"`, or any other instant
      `datetime.fromisoformat` accepts: a naive `"YYYY-MM-DD HH:MM:SS"`
      (what yfinance actually writes to the pinned CSV — see
      `read_pinned`), a `T`-separated timestamp, or an offset/`Z`-suffixed
      instant. An offset-bearing instant is converted to its UTC calendar
      date first (dispatch note 3: an offset can shift which UTC day an
      instant falls on) — a naive one is read as the calendar date it
      already names, since there is no timezone to convert through.

    Raises `ValueError`/`TypeError` on anything that does not resolve —
    a date that cannot be parsed is a refusal, never a guess (mirrors
    `_canonical_utc` for `trade_time`; this is the same discipline applied
    a fourth time, per the brief's `read_pinned` interface note).
    """
    if not isinstance(value, str):
        raise TypeError(f"date must be a string, got {type(value).__name__}")
    if _COMPACT_DATE_RE.match(value):
        return datetime.strptime(value, "%Y%m%d").strftime(_CANONICAL_DATE_FORMAT)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date().isoformat()


def _parse_calendar_date(value: str):
    """Parses an already-canonical `YYYY-MM-DD` string (as produced by
    `_canonical_calendar_date` / `observations_to_series` / `read_pinned`
    keys) back to a `date` for bounds/gap arithmetic. Not a second
    normalization pass — the callers of this already guarantee the form."""
    return datetime.strptime(value, _CANONICAL_DATE_FORMAT).date()


def _fetch_spy_from_yfinance(quarter: str) -> list[tuple[str, float]]:
    """The ONE place `yfinance` is imported in this module (global
    constraint: yfinance appears in exactly one private function). Called
    only by `ensure_pinned` on a real cache miss when its caller passes no
    `fetch` override — the CLI's actual path. Every test in this file
    injects its own `fetch` callable instead, so this function is never
    exercised by the test suite; `ensure_pinned` degrades any failure here
    (an exception, an empty/`None` history) to `unavailable (...)` — this
    function itself is free to raise or return nothing.

    Pulls two weeks of padding before the quarter's first day (enough to
    survive a long holiday weekend and still find a close strictly before
    it — `d0`) through the quarter's own end, so both `pick_endpoints`
    dates are guaranteed to land inside the pulled range.

    `auto_adjust=True` is passed EXPLICITLY: the row
    written to the pin under the `adj_close` header must actually be
    dividend/split-adjusted, and this pinned installation's `yfinance==1.2.0`
    happens to default `auto_adjust` to `True` — but a dependency bump that
    silently flips that default would then write UNADJUSTED closes under an
    `adj_close` header, corrupting `CONTEXT` across every dividend/split with
    no error and no failing test (this path has none, by design — see the
    docstring above). Stating the keyword turns an implicit dependency-version
    behavior into an explicit, version-independent contract.
    """
    import yfinance as yf  # noqa: PLC0415 — the sole, deliberately-lazy import site

    from scripts.sources.yfinance_guard import yfinance_call

    start, end = quarter_bounds(quarter)
    fetch_start = (start - timedelta(days=14)).strftime(_CANONICAL_DATE_FORMAT)
    fetch_end = end.strftime(_CANONICAL_DATE_FORMAT)  # yfinance `end` is exclusive

    history = yfinance_call(lambda: yf.Ticker("SPY").history(
        start=fetch_start, end=fetch_end, interval="1d", auto_adjust=True))
    if history is None or history.empty:
        return []

    rows: list[tuple[str, float]] = []
    for index, row in history.iterrows():
        close = row.get("Close")
        if close is None:
            continue
        rows.append((index.strftime(_CANONICAL_DATE_FORMAT), float(close)))
    return rows


def ensure_pinned(reports_root, quarter: str, fetch=None) -> Path | str:
    """Returns the path to `{reports_root}/{quarter}/SPY.csv`, fetching it
    once on a cache miss and never again ONCE ONE EXISTS (spec §5.2 / brief
    behavioural constraint 1): "a pinned CSV is read, never re-fetched and
    never overwritten." The qualifier is load-bearing — a FAILED write
    leaves no pin at all, so the next run refetches; only a pin that
    actually reached disk is permanent. An existing pin short-circuits
    before `fetch` is ever
    called — the point is the file on disk, not an in-memory memoization,
    since a real run is a fresh process every time.

    `fetch` is a zero-arg callable returning `[(date, adj_close), ...]`;
    `None` (the CLI's real path) uses `_fetch_spy_from_yfinance`, the
    module's one private yfinance caller, bound to `quarter`. A fetch that
    RAISES or returns an empty/falsy result degrades to `unavailable (...)`
    — never an exception: CONTEXT is one of six numbers, and losing the
    benchmark must not cost the report the other five (brief interface
    note).

    Race safety is the write itself, not the pre-check: `write_bytes_new`
    stages the bytes in a temp file and publishes with `os.link`, which
    fails on a taken name — never exists-then-write. If a competing writer
    lands the file between this call's own `exists()` check and its
    publish, `write_bytes_new` returns False — the freshly fetched data is
    discarded and the winner already on disk is left untouched. (This was
    a direct `open(target, "xb")` until the write moved behind the atomic
    publish; exclusivity is unchanged, the mechanism is not.) The alternative (last write
    wins) would nondeterministically clobber whichever pull happened to
    finish second, silently changing a "pinned, reproducible" input.
    """
    quarter_dir = Path(reports_root) / quarter
    target = quarter_dir / "SPY.csv"

    if target.exists():
        return target

    fetch_fn = fetch if fetch is not None else (lambda: _fetch_spy_from_yfinance(quarter))

    try:
        rows = fetch_fn()
    except Exception as exc:  # noqa: BLE001 — a failed fetch is a refusal, never an exception (brief interface note)
        return f"unavailable (SPY pin: fetch failed for {quarter}: {exc})"

    if not rows:
        return f"unavailable (SPY pin: fetch returned no rows for {quarter})"

    lines = ["date,adj_close"]
    for date_str, close in rows:
        lines.append(f"{date_str},{close}")
    data = ("\n".join(lines) + "\n").encode("utf-8")

    # `write_bytes_new` publishes atomically and returns False when the name
    # is already taken — a competing writer landed the file between the
    # `exists()` check above and this call, so read the winner (return
    # `target`; the caller reads it via `read_pinned`) and never overwrite.
    #
    # Two things this closes. The write used to go straight to the final
    # path, and the pin is WRITE-ONCE and never re-fetched — so a write
    # interrupted partway left a truncated CSV that permanently broke this
    # quarter's CONTEXT. And it sat outside every `try`, so the OSError
    # escaped `ensure_pinned`, escaped `assemble`, and reached
    # `_cmd_report` — costing the operator every number in the quarter for
    # a failure in the one input the design says may go missing on its own.
    try:
        # mkdir is INSIDE the try too. It was left outside when the write
        # moved in, so the exact escape the paragraph above describes — an
        # OSError reaching `_cmd_report` and costing the quarter its other
        # five numbers — was still reachable through `mkdir` alone (a plain
        # file where the quarter directory should be, a read-only mount).
        quarter_dir.mkdir(parents=True, exist_ok=True)
        write_bytes_new(target, data)
    except OSError as exc:
        return (
            f"unavailable (SPY pin: could not write "
            f"{target.as_posix()}: {exc})"
        )

    return target


def read_pinned(path) -> dict[str, float] | str:
    """Reads one pinned SPY CSV back to a canonical `{YYYY-MM-DD: close}`
    series, or an `unavailable (...)` reason.

    Every date goes through `_canonical_calendar_date` before it is ever
    compared: yfinance writes `"2026-06-30 00:00:00"`, which never equals
    the canonical account endpoint `"2026-06-30"` unless both sides go
    through the same normalization (brief interface note: "both sides of a
    comparison have to be pinned, not one").

    Conflict detection — two rows sharing one canonical date but carrying
    two DIFFERENT closes — is delegated to `observations_to_series`, the
    SAME check NAV/cps observations go through ("exactly as
    observations_to_series does for NAV and cps"), applied to the raw,
    duplicate-retaining row list BEFORE any collapse to a dict (constraint
    4) — collapsing first would silently pick one and destroy the
    evidence that the pin disagrees with itself.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError subclass, not an OSError — the
        # same arm `_read_one` and `fold_or_reason` already carry, and this
        # is the third `read_text` in the package. `assemble` calls this
        # function with no try of its own, so the missing arm did not cost
        # CONTEXT alone: it took the whole quarterly report down, counts
        # included, for a benchmark file the module's own contract says is
        # allowed to go missing.
        return f"unavailable (SPY pin: unreadable file {path.as_posix()}: {exc})"

    reader = csv.DictReader(text.splitlines())
    fieldnames = reader.fieldnames or ()
    if "date" not in fieldnames or "adj_close" not in fieldnames:
        return (
            f"unavailable (SPY pin: {path.as_posix()} missing a "
            "date/adj_close header)"
        )
    # Presence is not enough: `csv.DictReader` maps a REPEATED column name to
    # its LAST occurrence, so `date,adj_close,adj_close` passes the check
    # above and then reads a different column than the one the pin was
    # written with — silently returning a believable WRONG benchmark rather
    # than refusing. Every other failure here degrades to `unavailable`;
    # this was the one path that produced a number instead.
    if list(fieldnames).count("date") != 1 or list(fieldnames).count("adj_close") != 1:
        return (
            f"unavailable (SPY pin: {path.as_posix()} has a duplicate "
            f"date/adj_close header: {list(fieldnames)!r})"
        )

    pairs: list[tuple[str, float]] = []
    for row in reader:
        raw_date = row.get("date")
        raw_close = row.get("adj_close")
        if raw_date is None or raw_close is None:
            return f"unavailable (SPY pin: malformed row in {path.as_posix()}: {row!r})"
        try:
            date_str = _canonical_calendar_date(raw_date)
        except (ValueError, TypeError) as exc:
            return (
                f"unavailable (SPY pin: unparseable date {raw_date!r} "
                f"in {path.as_posix()}: {exc})"
            )
        try:
            close = float(raw_close)
        except (ValueError, TypeError):
            return (
                f"unavailable (SPY pin: unparseable close {raw_close!r} "
                f"in {path.as_posix()})"
            )
        pairs.append((date_str, close))

    return observations_to_series(pairs)


def spy_return(series: dict, s0: str, s1: str) -> float | str:
    """`adj_close[s1] / adj_close[s0] - 1` on the EXACT calendar dates
    `s0`/`s1` — no nearby-date substitution, ever (brief behavioural
    constraint 2). Both dates must be present in `series` AND both closes
    finite and strictly positive; `quarterly_twr` guards its domain this
    way already, and nothing guarded the SPY side until now — a negative
    close would otherwise yield a plausible-looking false return, a zero
    start would raise `ZeroDivisionError` instead of refusing, and a NaN
    would reach CONTEXT silently.
    """
    if s0 not in series or s1 not in series:
        missing = [d for d in (s0, s1) if d not in series]
        return (
            "unavailable (spy_return: no pinned SPY row for "
            f"{', '.join(missing)})"
        )
    v0, v1 = series[s0], series[s1]
    if (
        isinstance(v0, bool) or isinstance(v1, bool)
        or not isinstance(v0, (int, float)) or not isinstance(v1, (int, float))
        or not (math.isfinite(v0) and math.isfinite(v1))
        or v0 <= 0 or v1 <= 0
    ):
        return (
            "unavailable (spy_return: domain violation — adj_close must "
            f"be finite and > 0, got {s0}={v0!r}, {s1}={v1!r})"
        )
    return v1 / v0 - 1


# --- Task 6: NAV/cps observations + endpoints/gaps (§6.2) ---------------


def observations_from(
    envelopes: list, bounds: tuple[datetime, datetime]
) -> list[tuple[str, float]] | str:
    """Flattens ONE archived YTD BASELINE into a `(date, cps)` sequence, or
    an `unavailable (...)` reason. Takes `bounds` (the same half-open UTC
    `[start, end)` pair every other windowed gate in this module takes)
    because selecting the right baseline is itself a windowed decision —
    not a quarter string, so this stays consistent with `count_quarter` /
    `pick_endpoints` / `gap_reason`.

    Reads D1's frozen row path: `response["accounts"]["account"]
    ["periods"]["YTD"]["dates" | "cps"]` are PARALLEL ARRAYS, equal
    length, indexed together — NOT a list of row objects. Only `YTD` is
    read (`cps` is measured from each period's own start, so mixing
    periods corrupts both RISK and CONTEXT). `nav` is not extracted at
    all: D5 found it is the raw account balance and not flow-adjusted,
    while `cps` is TWR, so RISK reads `(1 + cps)` too (see
    `quarterly_drawdown`) and nothing in this module reads `nav`.

    F1 fix: `YTD` alone is not one baseline — it resets every Jan 1, so a
    "YTD" pull archived in one year and one archived the next carry cps
    measured from two DIFFERENT reference points wearing the same period
    name. The real broker response names its own reference point:
    `periods["YTD"]["start_date"]` (e.g. `"20251231"`), read here as the
    GROUPING KEY — never the pull's own `pulled_at` year, which is only a
    proxy for the baseline and breaks the moment an old envelope is
    archived alongside a fresh one. Every envelope's YTD rows are grouped
    by their (canonicalised) `start_date`; envelopes sharing one baseline
    union exactly as before (same baseline, no conflict). Exactly ONE
    group may be returned: its `start_date` must fall strictly before
    `bounds[0]` (the quarter start) AND at least one of its own
    observation dates must fall inside `bounds` (the baseline's data
    actually reaches the quarter being reported) — a stale baseline whose
    data trails off before the quarter starts is excluded rather than
    silently donating a `d0` to a series it does not belong to. Zero
    qualifying groups, or more than one (ambiguous — cannot be
    disambiguated further), is a refusal naming the quarter window and
    every baseline that was archived.

    Each date (and `start_date`) is canonicalised via
    `_canonical_calendar_date` — D5's real compact `"20260816"`, or any
    other spelling that function accepts. Duplicates WITHIN a baseline
    group are KEPT, not collapsed: `observations_to_series` is where a
    conflict is detected, and a flattener that deduplicated first would
    destroy the evidence that two pulls disagree.

    Fail-closed at the row: a `ParseFailure` in `envelopes`, a response
    missing the frozen shape (now including `start_date`), a length
    mismatch between `dates` and `cps`, or a single row malformed at
    `date` or `cps` blanks the WHOLE result — never a silently dropped
    row. A dropped interior row can stay inside the five-day freshness
    limit while changing the drawdown, and a dropped endpoint changes
    TWR — so an unreadable row is a refusal for the whole series, exactly
    like `fills_from`'s "any row that cannot be normalized blanks the
    whole result" rule.
    """
    for e in envelopes:
        if isinstance(e, ParseFailure):
            return (
                "unavailable (observations: archive contains an "
                f"unparseable file {e.path.as_posix()} — {e.reason})"
            )

    start, end = bounds
    quarter_start = start.date()
    quarter_end = end.date()

    groups: dict[str, list[tuple[str, float]]] = {}
    for e in envelopes:
        if not isinstance(e, Envelope):
            continue
        response = e.response
        try:
            ytd = response["accounts"]["account"]["periods"]["YTD"]
            dates = ytd["dates"]
            cps_values = ytd["cps"]
            baseline_raw = ytd["start_date"]
        except (TypeError, KeyError, IndexError):
            return (
                "unavailable (observations: envelope "
                f"{e.path.as_posix()} response does not carry "
                "accounts.account.periods.YTD.{dates,cps,start_date})"
            )
        if not isinstance(dates, list) or not isinstance(cps_values, list):
            return (
                "unavailable (observations: envelope "
                f"{e.path.as_posix()} dates/cps are not lists)"
            )
        if len(dates) != len(cps_values):
            return (
                "unavailable (observations: envelope "
                f"{e.path.as_posix()} dates/cps length mismatch: "
                f"{len(dates)} dates vs {len(cps_values)} cps)"
            )
        try:
            baseline = _canonical_calendar_date(baseline_raw)
        except (ValueError, TypeError) as exc:
            return (
                "unavailable (observations: unparseable start_date "
                f"{baseline_raw!r} in {e.path.as_posix()}: {exc})"
            )

        rows: list[tuple[str, float]] = []
        for date_raw, cps_raw in zip(dates, cps_values):
            try:
                date_str = _canonical_calendar_date(date_raw)
            except (ValueError, TypeError) as exc:
                return (
                    "unavailable (observations: unparseable date "
                    f"{date_raw!r} in {e.path.as_posix()}: {exc})"
                )
            if isinstance(cps_raw, bool) or not isinstance(cps_raw, (int, float)):
                return (
                    "unavailable (observations: non-numeric cps "
                    f"{cps_raw!r} at {date_str} in {e.path.as_posix()})"
                )
            rows.append((date_str, float(cps_raw)))

        groups.setdefault(baseline, []).extend(rows)

    qualifying: list[str] = []
    for baseline, rows in groups.items():
        baseline_date = _parse_calendar_date(baseline)
        if baseline_date >= quarter_start:
            continue
        if any(quarter_start <= _parse_calendar_date(d) < quarter_end for d, _ in rows):
            qualifying.append(baseline)

    window = f"{quarter_start.isoformat()}..{quarter_end.isoformat()}"

    if not qualifying:
        if groups:
            # A legal envelope whose `dates`/`cps` are both EMPTY clears
            # every shape gate above (`len([]) == len([])`) and lands here
            # as a group with no rows. `min()`/`max()` over it raises
            # ValueError, which escapes this function, is caught by
            # `_cmd_report`'s `except ValueError`, and prints
            # `REFUSED: min() iterable argument is empty` — killing the
            # whole report, fills and orders included, with a message that
            # names nothing the operator can act on. The empty group never
            # qualified for anything; it simply has no date range to name.
            # Fixed HERE, in the diagnostic, rather than by refusing the
            # whole call at the first empty envelope: that would let one
            # empty pull suppress a second, genuinely qualifying one.
            parts = []
            for b, rows in groups.items():
                span = (f", {min(d for d, _ in rows)}..{max(d for d, _ in rows)}"
                        if rows else "")
                parts.append(f"{b} ({len(rows)} obs{span})")
            available = "; ".join(parts)
        else:
            available = "no YTD envelopes archived"
        return (
            "unavailable (observations: no archived YTD baseline covers "
            f"{window} — available: {available})"
        )

    if len(qualifying) > 1:
        return (
            "unavailable (observations: multiple archived YTD baselines "
            f"cover {window} — ambiguous, cannot select one: "
            f"{', '.join(sorted(qualifying))})"
        )

    return groups[qualifying[0]]


def to_cps_decimal(observations: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Applies D5's scale ONCE, at the boundary (constraint 3): every
    formula below (`observations_to_series`, `quarterly_twr`,
    `quarterly_drawdown`) reads only what this function returns, and
    never touches a raw `cps` value directly. D5 froze `cps` as DECIMAL
    (`0.48041759` = 48.04%) — already the scale this module needs — so
    this is an IDENTITY pass in that frozen branch.

    It stays a named, tested gate anyway: it is the single place the
    scale is asserted, so a future broker response that switches to
    percent is caught here, not as a silent factor-of-100 error three
    formulas downstream. Writing the formulas to read `cps[…]` directly
    (skipping this call) would reintroduce exactly that error even though
    D5 currently makes the function a no-op.
    """
    return list(observations)


def observations_to_series(observations) -> dict[str, float] | str:
    """Collapses a `(date, value)` sequence — which MAY carry duplicates,
    by construction of `observations_from` and `read_pinned` — to one
    `{date: value}` mapping, or an `unavailable (...)` reason if one date
    carries two DIFFERENT values.

    The conflict check runs BEFORE the collapse (constraint 4): each
    incoming pair is compared against whatever this date already holds in
    the dict being built, never against a finished dict that could no
    longer tell two values apart. An agreeing duplicate — the normal case
    for overlapping archive pulls — collapses silently.

    Shared by `read_pinned` (the SPY pin) and by `assemble`, which calls
    this directly on `to_cps_decimal(observations_from(envelopes, bounds))`
    for the NAV/cps side: one implementation, so the two conflict checks
    can never diverge (`.claude/rules/producer-consumer.md` rule 3).
    """
    result: dict[str, float] = {}
    for date, value in observations:
        if date in result and result[date] != value:
            return (
                f"unavailable (conflict: {date} carries two different "
                f"values: {result[date]!r} and {value!r})"
            )
        result[date] = value
    return result


def pick_endpoints(series: dict, bounds: tuple) -> tuple:
    """`d0` = the LAST date in `series` strictly BEFORE the quarter's
    first day; `d1` = the LAST date on or before the quarter's last
    calendar day (spec §6.2 / constraint 5). `bounds` is half-open
    `[start, end)`, so "on or before the last calendar day" is exactly
    `date < end.date()` — an inclusive `<=` against `end.date()` would
    wrongly admit the FIRST day of the NEXT quarter as `d1`.

    Takes the MAPPING `observations_to_series` (or `read_pinned`)
    returns, not a bare list of dates — the brief is explicit that taking
    a list would leave a later caller extracting the keys between two
    gates, an unowned step.

    Either or both may come back `None` — most commonly `d0`, when the
    account has no history before the quarter (spec §6.2: "if no d0
    exists the line is unavailable — full stop"). `quarterly_twr` and
    `quarterly_drawdown` both check for `None` explicitly rather than let
    a `None` key miss the dict and raise.

    Fail-closed on an unparseable key (fix round 1, unifying the posture
    with `quarterly_drawdown`, which already refused on this): ANY key in
    `series` that cannot be parsed to a date means the series is not what
    this module thinks it is, and the whole function returns `(None,
    None)` rather than silently computing `d0`/`d1` from the parseable
    subset — a `None` `d0` already reads as `unavailable` downstream
    (constraint 5), so this reuses that path rather than widening the
    return type. Every real key comes from `_canonical_calendar_date`,
    which raises rather than emitting a bad one, so this is unreachable
    with the module's own producers today — it exists so a future one
    that doesn't share that guarantee fails closed instead of silently
    dropping a key.
    """
    start, end = bounds
    start_date = start.date()
    end_date = end.date()

    d0, d0_date = None, None
    d1, d1_date = None, None
    for key in series:
        try:
            key_date = _parse_calendar_date(key)
        except (ValueError, TypeError):
            return None, None
        if key_date < start_date and (d0_date is None or key_date > d0_date):
            d0, d0_date = key, key_date
        if key_date < end_date and (d1_date is None or key_date > d1_date):
            d1, d1_date = key, key_date
    return d0, d1


def gap_reason(series: dict, d0: str | None, d1: str | None, bounds: tuple) -> str | None:
    """`None` when the account series has no `> 5 calendar day` gap
    anywhere that would let RISK/CONTEXT silently span a stale window;
    otherwise an `unavailable (gap: ...)` reason naming EVERY edge that
    tripped (constraint 8) — never just the first one found, so no
    precedence between the three checks has to be defined and a fixture
    carrying two gaps has one unambiguous expected reason.

    Three independent checks, all against the SAME `_MAX_GAP_DAYS` = 5
    threshold (spec §6.2 states this explicitly — a month-sized fixture
    gap passes under almost any other wrong threshold):
    - LEFT: `d0` to the quarter's first day.
    - RIGHT: `d1` to the quarter's last calendar day. Essential — if the
      series stops mid-quarter, `d1` becomes that stale date and no
      consecutive-observation rule can see the missing tail on its own.
    - INTERIOR: the widest gap between two consecutive observations
      inside `[d0, d1]` (both endpoints included, so the left/right edges
      above are genuinely independent of this one).

    Takes the mapping `pick_endpoints` reads too, not a bare list — same
    reasoning as `pick_endpoints`'s own docstring.

    Fail-closed on an ordering/bounds violation (fix round 1): an EXPLICIT
    check that `d0` is strictly before the quarter's first day, `d1` is
    before the quarter's end, and `d0` is before `d1` — before any gap
    arithmetic runs. This module's original implementation used `abs()`
    on the left/right day-count differences, which was correct arithmetic
    but MASKED an ordering violation as a large, loudly-refusing positive
    gap. Dropping `abs()` alone (a later fix, to satisfy
    `audit_fail_open`'s Pattern Z.2, which flags `abs((<date> -
    <date>).days)` as the closest-bar-search anti-pattern — an unrelated
    bug class this usage merely resembles structurally) removed that
    masking WITHOUT replacing what it accidentally caught: a caller that
    hands this function `d0 >= d1` (or an endpoint outside `bounds`) now
    gets a NEGATIVE day-count, which never exceeds `_MAX_GAP_DAYS`, so the
    function would silently return `None` — "no gap found" — for an
    ordering violation, sending the reader looking for missing
    observations that are not the actual problem. The check below names
    the real fault instead, and is unreachable with `pick_endpoints`'s own
    contract (which already guarantees this ordering) — it exists for any
    OTHER caller, present or future, that does not.

    Fail-closed on an unparseable series key, same reasoning and same fix
    round as `pick_endpoints`: the interior scan below refuses rather than
    silently skipping a key it cannot parse, matching `quarterly_drawdown`
    (which already refused on this).
    """
    if d0 is None or d1 is None:
        return "unavailable (gap check: missing endpoint)"

    start, end = bounds
    start_date = start.date()
    end_date = end.date()
    quarter_last_day = end_date - timedelta(days=1)

    try:
        d0_date = _parse_calendar_date(d0)
        d1_date = _parse_calendar_date(d1)
    except (ValueError, TypeError) as exc:
        return f"unavailable (gap check: unparseable endpoint: {exc})"

    ordering_violations: list[str] = []
    if d0_date >= start_date:
        ordering_violations.append(
            f"d0 {d0} is not strictly before quarter start {start_date.isoformat()}"
        )
    if d1_date >= end_date:
        ordering_violations.append(
            f"d1 {d1} is not before quarter end {end_date.isoformat()}"
        )
    if d0_date >= d1_date:
        ordering_violations.append(f"d0 {d0} is not before d1 {d1}")
    if ordering_violations:
        return "unavailable (endpoint ordering: " + "; ".join(ordering_violations) + ")"

    problems: list[str] = []

    left_gap = (start_date - d0_date).days
    if left_gap > _MAX_GAP_DAYS:
        problems.append(
            f"left: {left_gap}d between d0 {d0} and quarter start "
            f"{start_date.isoformat()}"
        )

    right_gap = (quarter_last_day - d1_date).days
    if right_gap > _MAX_GAP_DAYS:
        problems.append(
            f"right: {right_gap}d between d1 {d1} and quarter end "
            f"{quarter_last_day.isoformat()}"
        )

    window_dates: list = []
    for key in series:
        try:
            key_date = _parse_calendar_date(key)
        except (ValueError, TypeError):
            return f"unavailable (gap check: unparseable date {key!r} in series)"
        if d0_date <= key_date <= d1_date:
            window_dates.append(key_date)
    window_dates.sort()

    max_interior_gap = 0
    for a, b in zip(window_dates, window_dates[1:]):
        gap = (b - a).days
        if gap > max_interior_gap:
            max_interior_gap = gap
    if max_interior_gap > _MAX_GAP_DAYS:
        problems.append(f"interior: {max_interior_gap}d gap within [{d0}, {d1}]")

    if not problems:
        return None
    return "unavailable (gap: " + "; ".join(problems) + ")"


def quarterly_twr(cps_decimal: dict, d0: str | None, d1: str | None) -> float | str:
    """`(1 + cps_decimal[d1]) / (1 + cps_decimal[d0]) - 1` — a RATIO, not
    a difference (constraint 6). Defined only when both endpoints are
    present in `cps_decimal` and both `1 + cps_decimal[...]` are finite
    and strictly positive: constraining only the denominator would admit
    `cps_decimal[d1] < -1` (a quarterly return below -100%, outside the
    domain of a positive-NAV cumulative series), and leaving either side
    unconstrained divides by zero at `cps_decimal[d0] == -1`.

    Both endpoints are checked for PRESENCE before any lookup (constraint
    9) — a missing endpoint (including `d0 is None`, spec §6.2's "no d0 ->
    unavailable, full stop") returns `unavailable`, never `KeyError`.
    """
    if d0 is None or d1 is None:
        return "unavailable (quarterly_twr: missing endpoint)"
    if d0 not in cps_decimal or d1 not in cps_decimal:
        missing = [d for d in (d0, d1) if d not in cps_decimal]
        return (
            "unavailable (quarterly_twr: endpoint(s) not in series: "
            f"{missing})"
        )

    g0 = 1 + cps_decimal[d0]
    g1 = 1 + cps_decimal[d1]
    if not (math.isfinite(g0) and math.isfinite(g1)) or g0 <= 0 or g1 <= 0:
        return (
            "unavailable (quarterly_twr: domain violation — 1+cps must be "
            f"finite and > 0 at both endpoints, got {d0}={cps_decimal[d0]!r}, "
            f"{d1}={cps_decimal[d1]!r})"
        )
    return g1 / g0 - 1


def quarterly_drawdown(cps_decimal: dict, d0: str | None, d1: str | None) -> float | str:
    """Quarterly max drawdown, run over growth factors `1 + cps_decimal[t]`
    — NOT NAV. D5 found `nav` is the raw account balance and is not
    flow-adjusted, while `cps` is TWR; this account has taken real
    external cash flows, so a NAV-based drawdown would read a deposit as
    a recovery (dispatch note 2 / freeze file D5). The formula shape is
    unchanged from spec §6.2's NAV-shaped original — only the input's
    meaning is: a running peak seeded at `d0`, bounded to `[d0, d1]`.

    Both endpoints must be present in `cps_decimal` (constraint 9,
    `None` or absent alike). The series is then SLICED to `[d0, d1]`
    (inclusive both ends) BEFORE anything else — an observation before
    `d0` must not enter the running peak, and one after `d1` must not
    enter at all, not even to be validated: an out-of-window non-positive
    growth factor must not fail the whole line closed (see
    `test_drawdown_ignores_observations_outside_d0_to_d1`, where a
    growth factor of exactly 0.0 sits just past `d1`). Only after slicing
    is every `1 + cps` in the window checked finite and strictly
    positive — interior points too, not just the two endpoints.

    The peak RUNS: it is `max(1 + cps[u])` over every `u` from `d0` up to
    the current step, re-evaluated at each step — not frozen at `d0`
    (`d0` being the series minimum would otherwise report a POSITIVE
    "drawdown"), and not "the worst single adjacent step" (a two-step
    decline can be worse than either step alone).
    """
    if d0 is None or d1 is None:
        return "unavailable (quarterly_drawdown: missing endpoint)"
    if d0 not in cps_decimal or d1 not in cps_decimal:
        missing = [d for d in (d0, d1) if d not in cps_decimal]
        return (
            "unavailable (quarterly_drawdown: endpoint(s) not in series: "
            f"{missing})"
        )

    try:
        d0_date = _parse_calendar_date(d0)
        d1_date = _parse_calendar_date(d1)
    except (ValueError, TypeError) as exc:
        return f"unavailable (quarterly_drawdown: unparseable endpoint: {exc})"
    if d0_date > d1_date:
        return f"unavailable (quarterly_drawdown: d0 {d0} is after d1 {d1})"

    window: list[tuple[Any, str, float]] = []
    for key, value in cps_decimal.items():
        try:
            key_date = _parse_calendar_date(key)
        except (ValueError, TypeError):
            return f"unavailable (quarterly_drawdown: unparseable date {key!r} in series)"
        if d0_date <= key_date <= d1_date:
            window.append((key_date, key, value))
    window.sort(key=lambda item: item[0])

    # Domain check over the WINDOW only — slicing to [d0, d1] happens
    # BEFORE this validation, so an out-of-window non-positive value can
    # never fail the line closed.
    for _, key, value in window:
        growth = 1 + value
        if not (math.isfinite(growth) and growth > 0):
            return (
                "unavailable (quarterly_drawdown: domain violation at "
                f"{key}={value!r})"
            )

    peak = 1 + window[0][2]
    worst = 0.0
    for _, _key, value in window[1:]:
        growth = 1 + value
        if growth > peak:
            peak = growth
        drawdown = growth / peak - 1
        if drawdown < worst:
            worst = drawdown
    return worst


def context_pp(account_twr: float | str, spy: float | str) -> float | str:
    """`(account_twr - spy) * 100`, in percentage points, at full
    precision — rounding happens only at display (spec §6.2). If either
    input is already an `unavailable (...)` string, that reason
    PROPAGATES, naming which side failed — it never collapses to a bare
    `"unavailable"`. CONTEXT is the only arithmetic between two gate
    outputs (`quarterly_twr` and `spy_return`); without a name for it, that
    subtraction would have to land in `assemble`, in `render`, or in an
    unnamed private helper, and a reader who sees only "unavailable"
    cannot tell a cps conflict from a missing SPY endpoint from a date the
    pin does not carry.
    """
    if isinstance(account_twr, str):
        return (
            "unavailable (CONTEXT: account TWR unavailable — "
            f"{_unwrap_reason(account_twr)})"
        )
    if isinstance(spy, str):
        return f"unavailable (CONTEXT: SPY return unavailable — {_unwrap_reason(spy)})"
    return (account_twr - spy) * 100


# --- Task 7: assembly and render (spec §6) -------------------------------

# The three archived tools, in the fixed order `assemble` reads them (and
# in which their envelopes are named in INPUTS) — DATA's trades, then the
# orders snapshot that only ever feeds the compiled-disabled orders_linked
# slot, then the performance pulls RISK/CONTEXT read.
_TRADES_TOOL = "get_account_trades"
_ORDERS_TOOL = "get_account_orders"
_PERFORMANCE_TOOL = "get_pa_performance_all_periods"

_MINUS = "−"  # U+2212 MINUS SIGN — the spec layout's own glyph, never ASCII "-"


def _label(text: str) -> str:
    """Left-justifies a section label (or the empty string, for a
    continuation line) to the fixed 9-column width every section head in
    the spec §6 layout shares (`DATA`, `RISK`, `CONTEXT`, `INPUTS` all pad
    to 9)."""
    return f"{text:<9}"


def _envelope_inputs(tool: str, envelopes: list) -> list[str]:
    """`<tool>/<basename>` for every archived pull of `tool` — an
    `Envelope` or a `ParseFailure` alike, since a file that failed to
    parse was still read (brief constraint 3: the summary names every
    input it read, never only the ones that parsed)."""
    return [f"{tool}/{e.path.name}" for e in envelopes]


def _fmt_field(value: int | str) -> str:
    """A `Counts` field, or `risk`/`context`/`orders_linked`, as text: the
    reason verbatim when refused, the plain value otherwise. Never
    fabricates a number beside a refusal (`render`'s first of its three
    permitted computations — choosing between a value and its refusal)."""
    return value if isinstance(value, str) else str(value)


def _fmt_risk(risk: float | str) -> str:
    """RISK's decimal drawdown converted to percent and rounded to one
    decimal (`render`'s other two permitted computations), or its refusal
    reason verbatim. The sign glyph is the spec's own U+2212, never ASCII
    `-` — Python's `-0.0`/negative formatting emits ASCII, so the minus
    (when present) is swapped after formatting."""
    if isinstance(risk, str):
        return risk
    text = f"{risk * 100:.1f}%"
    if text.startswith("-"):
        text = _MINUS + text[1:]
    return text


def _fmt_context(context: float | str) -> str:
    """CONTEXT's percentage-point figure, rounded to two decimals with an
    explicit sign (spec layout: `<+X.XXpp>`), or its refusal reason
    verbatim. Same sign-glyph swap as `_fmt_risk`."""
    if isinstance(context, str):
        return context
    text = f"{context:+.2f}pp"
    if text.startswith("-"):
        text = _MINUS + text[1:]
    return text


def render(
    quarter: str,
    counts: Counts,
    risk: float | str,
    context: float | str,
    inputs: list[str],
) -> str:
    """The quarterly summary text (spec §6 layout), given already-computed
    `counts`/`risk`/`context` and the list of input paths `assemble`
    actually read. No windowing, no aggregation, no other arithmetic:
    `render` owns exactly three computations — choosing between a value
    and its refusal, converting the decimal drawdown to percent, and
    rounding (one decimal for RISK, two for CONTEXT) — all three inside
    `_fmt_risk`/`_fmt_context`/`_fmt_field` above, so this function itself
    only ever formats and joins strings.

    Returns text with NO trailing newline (brief Step 4 note): `assemble`
    is compared against a file read via `Path.read_text()`, and the golden
    is generated via `sys.stdout.write` (never `print`, which would add
    one the redirect then bakes into the file).
    """
    lines = [f"{quarter} Track Record", ""]

    # The honest signal-detection horizon
    # (spec §1/§2's own numbers — σ=11.70%, ~126 episodes/year, ~268
    # independent episodes needed for +2%/trade) stated where the user
    # actually reads it, not only in a design doc they never open. Placed
    # BEFORE any DATA/RISK/CONTEXT section head on purpose — it is fixed,
    # account-level prose, never a per-quarter computation, and grouping it
    # under a later section would make its digits indistinguishable from a
    # stale figure on a quarter where that section's own numbers refused.
    lines.append(
        "Horizon: ~126 episodes/year here; detecting a +2%/trade edge needs "
        "~268 independent"
    )
    lines.append(
        "ones — roughly 9 quarters for one estimate, ~17 for a before/after "
        "comparison,"
    )
    lines.append("even under generous independence assumptions.")
    lines.append("")

    fills = _fmt_field(counts.fills)
    orders = _fmt_field(counts.orders)
    order_unknown = _fmt_field(counts.order_unknown)
    lines.append(
        f"{_label('DATA')}{fills} fills → {orders} orders "
        f"(order_unknown {order_unknown})"
        "   [each: <N> | unavailable (reason)]"
    )
    lines.append(
        f"{_label('')}journal: {_fmt_field(counts.orders_linked)} "
        "unique orders linked this quarter;"
    )
    lines.append(
        f"{_label('')}{_label('')}{_fmt_field(counts.thesis_versions)} "
        "thesis versions declared this quarter"
    )

    lines.append(
        f"{_label('RISK')}Quarterly max drawdown {_fmt_risk(risk)}"
        "   [TWR series, peak seeded at quarter start]"
    )
    lines.append(
        f"{_label('CONTEXT')}Account TWR − SPY: {_fmt_context(context)}"
        "     [account: MCP TWR series · SPY: pinned CSV]"
    )
    lines.append(f"{_label('')}Not risk-adjusted and not a skill measure. This book's beta and")
    lines.append(f"{_label('')}concentration dominate this number.")

    lines.append("")
    if inputs:
        lines.append(f"{_label('INPUTS')}{inputs[0]}")
        lines.extend(f"{_label('')}{path}" for path in inputs[1:])
    else:
        lines.append("INPUTS")

    return "\n".join(lines)


def assemble(
    archive_root,
    journal_path,
    reports_root,
    quarter: str,
    fetch_spy=None,
) -> tuple[str, list[str]]:
    """Composes one quarter's summary from the archive, the journal and the
    pinned benchmark, and renders it. Calls each gate at most once per
    applicable input and routes its result — no arithmetic, no date
    comparison, no filtering or grouping lives here; every such step is one
    of the named gates in `scripts/track_record/summary.py`'s module
    docstring / the plan's constraint 0.

    Returns `(rendered_text, inputs)` — `inputs` is every path actually
    read, `<tool>/<basename>` for archive envelopes and the bare filename
    for the journal and the pinned CSV, in the order §6's layout names them:
    every archive envelope, then the journal, then the pinned CSV.
    """
    bounds = quarter_bounds(quarter)
    inputs: list[str] = []

    # --- DATA: trades -----------------------------------------------
    trades_envelopes = read_envelopes(archive_root, _TRADES_TOOL)
    inputs += _envelope_inputs(_TRADES_TOOL, trades_envelopes)
    trades_gap = coverage_gap(trades_envelopes, bounds)
    fills: list[dict] | str = trades_gap if trades_gap is not None else fills_from(trades_envelopes)

    # --- orders (feeds only the D1-compiled-disabled orders_linked slot) --
    orders_envelopes = read_envelopes(archive_root, _ORDERS_TOOL)
    inputs += _envelope_inputs(_ORDERS_TOOL, orders_envelopes)
    orders_gap = coverage_gap(orders_envelopes, bounds)
    known_orders: set[tuple[str, str]] | str = (
        orders_gap if orders_gap is not None else known_orders_from(orders_envelopes)
    )

    # --- performance envelopes, read now so their names join the union of
    # "every archive envelope" before the journal — routing happens below.
    perf_envelopes = read_envelopes(archive_root, _PERFORMANCE_TOOL)
    inputs += _envelope_inputs(_PERFORMANCE_TOOL, perf_envelopes)

    # --- JOURNAL -------------------------------------------------------
    fold = fold_or_reason(journal_path)
    # Only if it EXISTS. `_read_events` returns [] for an absent file, so a
    # missing journal folds cleanly and used to be listed as an input that
    # was read — the one line of the INPUTS block asserting something
    # untrue, on a report whose whole premise is that a refusal must never
    # look like a value. `_envelope_inputs` goes the other way and names
    # even files that FAILED to parse, because those really were read.
    if Path(journal_path).exists():
        inputs.append(Path(journal_path).name)

    counts = count_quarter(fills, fold, bounds, known_orders)

    # --- RISK / CONTEXT: the performance money-path chain ----------------
    perf_gap = coverage_gap(perf_envelopes, bounds)
    if perf_gap is not None:
        risk: float | str = perf_gap
        context: float | str = perf_gap
    else:
        observations = observations_from(perf_envelopes, bounds)
        if isinstance(observations, str):
            risk = observations
            context = observations
        else:
            series = observations_to_series(to_cps_decimal(observations))
            if isinstance(series, str):
                risk = series
                context = series
            else:
                d0, d1 = pick_endpoints(series, bounds)
                freshness = gap_reason(series, d0, d1, bounds)
                if freshness is not None:
                    risk = freshness
                    context = freshness
                else:
                    risk = quarterly_drawdown(series, d0, d1)
                    account_twr = quarterly_twr(series, d0, d1)

                    pin = ensure_pinned(reports_root, quarter, fetch_spy)
                    if isinstance(pin, str):
                        spy: float | str = pin
                    else:
                        inputs.append(Path(pin).name)
                        spy_series = read_pinned(pin)
                        spy = spy_series if isinstance(spy_series, str) else spy_return(
                            spy_series, d0, d1
                        )
                    context = context_pp(account_twr, spy)

    return render(quarter, counts, risk, context, inputs), inputs
