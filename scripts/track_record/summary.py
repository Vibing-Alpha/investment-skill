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

See README.md in this package; spec
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
Freeze file: .claude/skills/track-record/references/ibkr-freeze.md
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.track_record.archive import (
    Envelope, ParseFailure, coverage_gap, read_envelopes, write_bytes_new,
    KNOWN_TOOLS, _covered_range, _no_parens, _TRADES_TOOL,
)
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
#
# Task 8 (identity now comes from the fill rows, not a positions snapshot)
# added `company_name` as the fifth member: two sightings of one execution
# naming two different companies is the broker reporting two securities
# under one ticker, and dedup (which keeps the FIRST row) would otherwise
# discard the second sighting — and its name — entirely, silently.
_COMPARED_FIELDS = ("symbol", "side", "size", "price", "company_name")

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
    envelopes: list, list_key: str, gate_name: str, bounds=None
) -> list[tuple[dict, Path, object]] | str:
    """Every row of `response[list_key]` across `envelopes`, paired with the
    archive path it came from — or an `unavailable (...)` reason if any
    envelope failed to parse, or a successfully-parsed envelope's response
    does not carry the expected `list_key` row list.

    Fail-closed (brief `fills_from` interface note): a `ParseFailure`, or a
    malformed response from a pull that CLAIMS to be `complete`, means this
    module cannot know what rows that pull held, and that unknown can never
    be read as "contributed zero rows" — it must blank the whole gate, the
    same way `archive.coverage_gap` blanks on any `ParseFailure` in the
    envelope list.

    The one exception, and the reason the sentence above says "claims to be
    complete": a pull the agent already declared INCOMPLETE, carrying no
    row list, is consistent with that declaration rather than a
    contradiction of it, and contributes no rows instead of blanking the
    gate. Step 2 of the skill archives a call that visibly errored, marked
    exactly that way, so refusing on it made one such pull disable
    `unlinked` permanently. See the branch below.
    """
    rows: list[tuple[dict, Path, object]] = []
    for e in envelopes:
        if isinstance(e, ParseFailure):
            return (
                f"unavailable ({gate_name}: archive contains an unparseable "
                f"file {e.path.as_posix()} — {_no_parens(e.reason)})"
            )
        if not isinstance(e, Envelope):
            continue
        response = e.response
        row_list = response.get(list_key) if isinstance(response, dict) else None
        if not isinstance(row_list, list):
            if e.completeness != "complete":
                # A pull the agent has ALREADY declared incomplete, carrying
                # no row list, is consistent with that declaration rather
                # than a contradiction of it — Step 2 tells the agent to
                # archive a call that visibly errored, marked exactly this
                # way, so `{"error": "session expired"}` is a shape the
                # documented workflow produces.
                #
                # Refusing on it made a successful `pull` permanently
                # disable `unlinked`: that command reads every trades pull
                # with no window, so `_cannot_reach` cannot dismiss one, and
                # the archive is never pruned. The only recovery was
                # deleting a file the archive promises to keep. Its
                # incomplete-pull NOTE already tells the reader that orders
                # such a pull missed cannot appear.
                #
                # A `complete` envelope with no row list still refuses
                # below: there the missing list contradicts the claim.
                continue
            if _cannot_reach(e, bounds):        # rule + rationale: see `_cannot_reach`
                continue
            return (
                f"unavailable ({gate_name}: envelope {e.path.as_posix()} "
                f"response carries no {list_key!r} row list)"
            )
        for row in row_list:
            rows.append((row, e.path, e))
    return rows


def _normalize_fill_row(row: Any, path: Path) -> dict | str:
    """One `fills_from` row normalized to the frozen nine fields, or an
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
    # This is the deduplication key, and it is coerced with `str()` just
    # below. Coercion is safe for a scalar — every distinct int maps to a
    # distinct string, which is why the pinned corpus carries int ids and
    # expects them accepted. It is NOT safe for a container: every list
    # becomes `"[]"` and every empty dict `"{}"`, so two genuinely
    # different fills collapsed into one and the quarter came out short by
    # however many collided. `bool` goes with them — `True` and the string
    # `"True"` are different executions that coerce to the same key.
    # `float` is NOT in the accepted set, and that is the correction to an
    # earlier widening here: "a scalar coerces to a unique key" is true of
    # int and str and false across them — `str(1)` is `"1"` while
    # `str(1.0)` is `"1.0"`, so one execution arriving under both spellings
    # counted as two. D2 froze `trade_id` as a string; a float is type
    # drift, and drift whose meaning cannot be established fails closed.
    if (isinstance(execution_id_raw, (bool, float))
            or not isinstance(execution_id_raw, (str, int))
            or str(execution_id_raw).strip() == ""):
        return (
            f"unavailable (fills: trade_id {execution_id_raw!r} cannot "
            f"identify an execution — D2 froze it as a non-empty string, "
            f"and a value that does not coerce to a UNIQUE key merges two "
            f"executions into one in {path.as_posix()})"
        )

    try:
        trade_time = _canonical_utc(trade_time_raw)
    except (ValueError, TypeError) as exc:
        return (
            f"unavailable (fills: unparseable trade_time {trade_time_raw!r} "
            f"in {path.as_posix()}: {_no_parens(str(exc))})"
        )

    order_id_raw = row.get("order_id")
    # The same coercion hazard as `trade_id` above, at the sibling site the
    # first fix missed: `order_id` groups fills into orders, and `str()`
    # maps the JSON boolean `true` and the string `"True"` to one key, so
    # two distinct orders rendered as one. D2 froze it as an integer.
    # Scalars coerce uniquely and are kept; containers and `bool` are not.
    if order_id_raw is not None and (
            isinstance(order_id_raw, (bool, float))
            or not isinstance(order_id_raw, (str, int))):
        return (
            f"unavailable (fills: order_id {order_id_raw!r} is "
            f"{type(order_id_raw).__name__} — D2 froze it as an integer, and "
            f"a value that does not coerce to a UNIQUE key can merge two "
            f"orders, or split one across two spellings, in {path.as_posix()})"
        )
    # An EMPTY id is no id. `""` passed the type gate, counted as a known
    # order, and printed a blank column in the backlog that `tag` then
    # refused to link — the journal requires a non-empty `order_id` — so
    # the entry could never be tagged OFF the list. Treated as absent, it
    # counts toward `order_unknown`, which is what it is.
    # `symbol` and `side` are the two fields a person READS to decide what
    # a row is, and the only two reaching `unlinked`'s output with no type
    # gate at all: a list printed as `['A']`, a `None` printed as an empty
    # column that looks like a missing field rather than a broken one, and
    # an integer side printed as `1`. They also decide whether a group of
    # fills is one coherent order. The live feed sends strings for both in
    # all 2038 rows.
    for _name, _value in (("symbol", row.get("symbol")), ("side", row.get("side"))):
        if (_value is None or isinstance(_value, bool)
                or not isinstance(_value, str) or _value.strip() == ""):
            return (
                f"unavailable (fills: {_name} {_value!r} is not the "
                f"non-empty string D2 froze — it is "
                f"what a person reads to recognise the trade, and it decides "
                f"whether a group of fills is one order, in {path.as_posix()})"
            )

    # `.strip()` on the STORED value, not only in the emptiness test above.
    # Both of these are KEYS — `execution_id` is half the dedup key and
    # `order_id` is what fills group into orders — and they were judged
    # stripped and stored raw, so `"E1"` and `" E1 "` were two executions
    # where the broker sent one (two fills, one of them phantom), and `"10"`
    # and `" 10 "` two orders. It also aligns the broker side of the
    # `orders_linked` comparison with the journal side, which refuses a
    # padded ref outright (`journal._require_ref_field`): an id that reads
    # as non-empty for the gate and as a different string for the lookup can
    # never come off the tagging backlog. If the validator strips to judge
    # the value, the stored key is the stripped form.
    order_id = None if order_id_raw is None else str(order_id_raw).strip()
    if order_id == "":
        order_id = None

    return {
        "account": _SINGLE_ACCOUNT,
        "execution_id": str(execution_id_raw).strip(),
        "order_id": order_id,
        "symbol": row.get("symbol"),
        "trade_time": trade_time,
        "side": row.get("side"),
        "size": row.get("size"),
        "price": row.get("price"),
        # Contemporaneous identity, from the fill itself. Absent is not an
        # error: 22 of 815 real rows have no name, and recording the ticker
        # alone is honest. PRESENT means a `str` whose strip is non-empty —
        # None, a non-string and a blank all normalize to None, so no
        # consumer has to re-decide what counts.
        "company_name": (raw_company
                         if isinstance(raw_company := row.get("company_name"), str)
                         and not isinstance(raw_company, bool)
                         and raw_company.strip() != ""
                         else None),
    }


def _trade_times_agree(a: str, b: str) -> bool:
    """Two canonical-UTC trade timestamps for ONE execution key, within
    `_TRADE_TIME_TOLERANCE_SECONDS`.

    Both come from `_normalize_fill_row`, so both are canonical `Z` and
    parse. An unparseable value cannot reach here; if one ever did, the
    `ValueError` propagating is the fail-closed direction — this function
    must never answer "agree" on input it did not understand.
    """
    if a == b:
        return True
    try:
        delta = _parse_any_utc(a) - _parse_any_utc(b)
    except (ValueError, TypeError, AttributeError):
        return False        # unreadable at full precision: not shown to agree
    return abs(delta.total_seconds()) <= _TRADE_TIME_TOLERANCE_SECONDS


def _parse_any_utc(value: str):
    """One UTC instant, keeping any fractional seconds the source carried.

    `_canonical_utc` truncates to whole seconds on purpose — right for a
    key and for display, wrong for a tolerance test, which has to measure
    the distance between two sightings rather than between two truncations.
    """
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _compare_snapshot(row: dict, order_id: str | None, trade_time: str,
                      trade_time_raw=None, company_name=None) -> dict[str, Any]:
    """The seven D2/D3/Task-8-compared fields for one raw trade row, in the
    SAME normalized form `fills_from` already computes for `order_id` (`str`
    or `None`), `trade_time` (canonical UTC) and now `company_name` (`str`
    or `None`) — comparing the RAW forms would flag the numeric-vs-string
    `order_id` drift `test_fills_from_coerces_identifiers_to_str` pins as
    NOT a conflict, and (Task 8) would flag a missing key against `""` as
    "differing in company_name" for a difference the normalizer's own
    contract calls no difference. `symbol`/`side`/`size`/`price` are
    compared as-is: D2 only froze identifier coercion, not these."""
    return {
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "size": row.get("size"),
        "price": row.get("price"),
        "trade_time": trade_time,
        # The UNTRUNCATED source value, for the tolerance test
        # only: `_canonical_utc` drops fractional seconds, so two
        # sightings 1.8s apart compared as 1s apart and passed a
        # rule written to catch exactly that. The live feed emits
        # whole seconds in all 2038 rows, so nothing changes today;
        # this stops the truncation from being what decides.
        "trade_time_raw": trade_time_raw if trade_time_raw is not None else trade_time,
        "order_id": order_id,
        # Two sightings of ONE execution naming two companies is the
        # broker reporting two securities under one ticker — the case the
        # identity rule exists for, and dedup would otherwise discard the
        # second sighting entirely.
        #
        # The NORMALIZED value, not `row.get("company_name")`. This task's
        # own contract says a missing key, `None`, a non-string and a blank
        # are all ABSENT — so comparing raw makes `""` and a missing key
        # "differing in company_name", and `fills_from` refuses the entire
        # backlog and every DATA count over a difference this task has just
        # declared is not one. Two PRESENT values are still compared raw:
        # a broker that changed the case changed what it reported.
        # Measured on the real archive: 2038 rows collapse to 815
        # executions — 1223 duplicate sightings — with ZERO disagreements,
        # so this refuses nothing that exists today.
        "company_name": company_name,
    }


def fills_from(envelopes: list, bounds: tuple[datetime, datetime] | None = None) -> list[dict] | str:
    """The seam between the archive and every count. Reads D1's
    `get_account_trades` response shape (`response["trades"]`) and
    normalizes each row to `{account, execution_id, order_id, symbol,
    trade_time, side, size, price, company_name}` — nine fields (plus
    `order_id_latest` on a row that was restated across pulls; see below);
    there is still no stable instrument id on a trade row (D4), but
    `side`/`size`/`price` are no longer dropped (`unlinked` is the only place a
    person actually reads a fill, and without these three there was no way
    to recognize a trade from its printed line). `trade_time` comes out as
    canonical UTC (brief interface note); `account`/`execution_id`/`order_id`
    come out as `str` (`order_id` as `None` when absent) whatever the row
    carried; `side`/`size`/`price` come out RAW, unconverted — nothing here
    reads them as money-path numbers, only display.

    `bounds`, when given, scopes ONLY the execution-conflict fail-close (it
    does not filter the returned rows — `_count_fills` still does that).
    Without it, any two archived rows sharing an execution key and
    disagreeing on content blank the whole result. That is right for
    `unlinked`, whose backlog spans all history. It was wrong for a
    quarterly report: the archive is append-only, so a single restatement
    in 2025 removed all three DATA figures from EVERY later quarter's
    report, forever, for a disagreement about a fill none of those quarters
    counts. With `bounds`, a conflict blanks the result only when it could
    change a number in the window — and it is still reported, in the
    quarter whose figures it actually affects, where re-running shows it.
    Straddling counts as inside: `_trade_times_agree` tolerates one second,
    so a conflicting pair can sit either side of a boundary, and the
    conservative reading is the one that refuses.

    Any row that cannot be normalized — missing execution key, missing or
    unparseable timestamp — blanks the WHOLE result to `unavailable (...)`,
    never a silently dropped row: a dropped row undercounts and the count it
    produces looks entirely normal.

    D3 fail-close: two rows sharing the execution key `(account,
    execution_id)` are compared on `_COMPARED_FIELDS`. The first row seen
    under a key is the one held for comparison — which row is "first" does
    not matter, because a difference refuses either way, and for the two
    fields whose ABSENCE is a recognized state rather than a difference
    (`order_id`, `company_name`) a later present sighting is adopted, so
    archive order cannot decide either of them. A match is an
    agreeing duplicate (the normal case for overlapping archive pulls) and
    is counted once, by key: the repeat is not appended again. A mismatch
    blanks the WHOLE result to `unavailable (execution conflict: ...)`,
    the same bounded shape as every other `fills_from` refusal — nothing on
    disk is touched, and no version chain is built (D3 froze fail-close,
    not "latest wins": no corrected/busted execution was observed).
    """
    raw = _rows_or_reason(envelopes, "trades", "fills", bounds)
    if isinstance(raw, str):
        return raw

    rows: list[dict] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    by_key: dict[tuple[str, str], dict] = {}
    seen_in: dict[tuple[str, str], tuple] = {}
    for row, path, envelope in raw:
        normalized = _normalize_fill_row(row, path)
        if isinstance(normalized, str):
            # Ask about the ROW before asking about its envelope. A YTD pull
            # DOES reach this quarter, so the envelope test says nothing
            # about a February row inside it that is missing its execution
            # id — and that row blanked an otherwise established Q2. If the
            # row carries a readable timestamp placing it outside the
            # window, it cannot be one of this quarter's fills whatever else
            # is wrong with it. A row that cannot be placed in time is still
            # refused: then the unknown really could be this quarter's.
            if bounds is not None:
                try:
                    when = _parse_canonical(_canonical_utc(row.get("trade_time")))
                except (ValueError, TypeError, AttributeError):
                    when = None
                if when is not None:
                    if not (bounds[0] <= when < bounds[1]):
                        continue
                    # Its own stamp says it belongs to THIS quarter, and
                    # that is the answer — the same rule every well-formed
                    # row is attributed by. Falling through to
                    # `_cannot_reach` asked the ENVELOPE instead, so a row
                    # stamped inside Q2 carried by a pull whose args say
                    # 2025Q1 was dismissed, and the quarter rendered a
                    # count that omitted a row it could not account for.
                    # The envelope's window only decides rows that cannot
                    # speak for themselves.
                    return normalized
            # Same rule one level down: this rejects a ROW inside an
            # otherwise well-shaped response, not the response itself.
            if _cannot_reach(envelope, bounds):        # see `_cannot_reach`
                continue
            return normalized

        key = (normalized["account"], normalized["execution_id"])
        snapshot = _compare_snapshot(row, normalized["order_id"],
                                     normalized["trade_time"], row.get("trade_time"),
                                     normalized["company_name"])

        prior = seen.get(key)
        if prior is None:
            seen[key] = snapshot
            seen_in[key] = (envelope, normalized["order_id"])
            by_key[key] = normalized
            rows.append(normalized)
            continue

        # The broker RESTATES `order_id` on settled executions — nine of 792
        # shared executions changed id across two pulls two days apart in the
        # live archive — which is why D3's amendment excludes it from the
        # conflict comparison. The row keeps its FIRST id, so a journal link
        # made from an earlier `unlinked` listing keeps matching. But the
        # order COUNT is a count over that unstable field: with `E1` restated
        # from order 100 to 200 while `E2` also lands on 200, first-seen says
        # two orders and latest-seen says one. Both readings are recorded
        # here because two consumers need different ones: `unlinked` and the
        # journal links key off the FIRST-archived id, so a link written
        # from an earlier listing keeps matching, while `_count_fills`
        # counts on the LATEST — the broker owns this field and restates it,
        # so its newest word is its current truth. That split is the
        # decision; `_count_fills`'s own comment records why the obvious
        # third option, refusing when the two disagree, was tried and
        # removed. This comment said it still did, one function away from
        # the code that does not.
        # Unconditionally, on EVERY later sighting. Guarding this on "differs
        # from the first id" left a stale middle value behind whenever the
        # broker restated an id and then restated it BACK: 100 -> 200 -> 100
        # kept `order_id_latest` at 200, so two executions grouped under one
        # order that the newest pull shows as two. Latest means latest.
        # ALWAYS, including when the broker's newest word is that this
        # execution has no order at all. Two separate bugs lived here:
        # `100` then `null` fell back through `latest or first` and counted
        # a phantom order, and `null` then `100` left the row's own id at
        # None — which `unlinked` skips, so a newly-known order vanished
        # from the tagging backlog with nothing said.
        by_key[key]["order_id_latest"] = normalized["order_id"]
        # The ROW shows the first NON-NULL sighting: that is the id an
        # earlier listing could have shown, and there is no link stability
        # to preserve for an id that was never printed.
        if by_key[key]["order_id"] is None and normalized["order_id"] is not None:
            by_key[key]["order_id"] = normalized["order_id"]
        # `company_name`, the same null -> non-null shape, for the same
        # reason and one worse consequence. The row keeps its FIRST-SEEN
        # name, so a first sighting that simply did not carry the field left
        # the execution `instrument_unresolved` FOREVER while a later
        # archived pull named it — and which pull happened to be archived
        # first then decided a permanent journal fact out of identical
        # evidence (spec F-1), with the name readable in the archive the
        # whole time. Later evidence strengthens the record instead. The
        # carve-out in the comparison loop below rules that present-vs-absent
        # is not a conflict; it does not say which of the two SURVIVES, and
        # this does.
        #
        # The held SNAPSHOT is adopted too, not just the row. Leaving it
        # `None` would keep that carve-out skipping every later comparison,
        # so absent -> `ACME INC` -> `OTHER CO` would write `ACME INC` with
        # the disagreement never reported — a definite name asserted while
        # contradicting evidence sits in the archive, which is worse than
        # the unresolved row this fix replaces. `prior` IS this dict, so the
        # comparison below sees the adopted value. Two PRESENT names that
        # differ still refuse, unchanged.
        if by_key[key]["company_name"] is None and normalized["company_name"] is not None:
            by_key[key]["company_name"] = normalized["company_name"]
            seen[key]["company_name"] = normalized["company_name"]

        account, execution_id = key

        def _in_window() -> bool:
            if bounds is None:
                return True
            start, end = bounds
            return any(
                start <= _parse_canonical(ts) < end
                for ts in (prior["trade_time"], normalized["trade_time"])
            )


        # Two sightings inside ONE response are not a restatement. Rows in
        # a single envelope have no chronological order, so `E1->100`
        # followed by `E1->200` in the same list is not "the broker changed
        # its mind" — it is a response that says two things at once, and
        # which one the count follows would be decided by list position.
        # `E1->100, E1->200, E2->200` rendered `1 orders`; swapping the two
        # E1 rows rendered `2`. Neither is established.
        # Compared against the PREVIOUS row from this same envelope, not
        # against the first-ever snapshot. Comparing to `prior` missed the
        # case the check exists for: with `E1->100` archived earlier, a
        # later response saying `E1->200` then `E1->100` compared each row
        # to the old `100` — the second matched, nothing fired, and the
        # count came out of an internally contradictory response.
        # Two envelopes sharing a `pulled_at` are as unordered as two rows
        # in one response: the collision suffix that separates their
        # filenames records the order they were WRITTEN, and a rename
        # destroys that with nothing left to detect it. So the same rule
        # covers both — same envelope, or same instant.
        last_env, last_order = seen_in.get(key, (None, None))
        same_moment = last_env is not None and (
            last_env is envelope
            or getattr(last_env, "pulled_at", None) == getattr(envelope, "pulled_at", object()))
        # `_in_window()` here too, and its absence was the bug: this was the
        # ONE conflict check in this function not scoped to `bounds`, so a
        # single self-contradicting response from any past quarter — 2024,
        # say — blanked fills, orders and order_unknown in every later
        # quarter, permanently, in an archive that never deletes. That is
        # precisely what the `bounds` parameter was added to prevent, and
        # this check was written above where `_in_window` was defined, which
        # is how it escaped. The definition has moved up so every check in
        # the loop shares it.
        if same_moment and last_order != normalized["order_id"] and _in_window():
            return (
                f"unavailable (execution conflict: account={account}, "
                f"trade_id={execution_id}, appears twice at "
                f"{getattr(envelope, 'pulled_at', '?')} with different order_id "
                f"{last_order!r} and {normalized['order_id']!r} — one "
                f"response cannot say which came later)"
            )
        seen_in[key] = (envelope, normalized["order_id"])

        for field in _COMPARED_FIELDS:
            # RULING (Task 8 review round 1): a PRESENT name against an
            # ABSENT one is NOT a conflict. One archived pull naming the
            # company and another simply not carrying the field is not a
            # disagreement about identity -- one sighting is more
            # informative, not contradictory. Comparing raw here would let
            # absence tilt the aggregate to "conflict", exactly what
            # `.claude/rules/producer-consumer.md` forbids for a missing
            # feed. Only `company_name` gets this carve-out: it is the only
            # _COMPARED_FIELDS member whose absence is a recognized,
            # normalized state (`None`) rather than a validation failure
            # upstream (`symbol`/`side` are required non-empty strings by
            # the time they reach here; `size`/`price` carry no ABSENT
            # concept of their own). Two PRESENT names that differ still
            # refuse below -- this narrows WHEN the field is compared, not
            # what counts as a difference once it is. Do not "tighten" this
            # back into a plain `!=` -- that blanks the entire quarter's
            # DATA section over a field that contributes nothing to those
            # counts, the trap this task exists to avoid.
            #
            # This carve-out decides only WHETHER the two are compared. It
            # does NOT decide which value survives -- that is the null ->
            # non-null adoption above, and without it "not a conflict" meant
            # "the absent first sighting wins", i.e. archive order deciding
            # a permanent fact. Read the two together: absence never
            # refuses, and absence never wins either.
            if field == "company_name" and (
                    prior[field] is None or snapshot[field] is None):
                continue
            if prior[field] != snapshot[field] and _in_window():
                return (
                    f"unavailable (execution conflict: account={account}, "
                    f"trade_id={execution_id}, differing in {field})"
                )
        # Agreeing within the tolerance is not the same as agreeing about
        # WHICH QUARTER owns the execution: one second either side of a
        # boundary puts the two sightings in different quarters, and the
        # count then followed whichever was archived first. Neither quarter
        # establishes it.
        if (bounds is not None
                and prior["trade_time"] != normalized["trade_time"]):
            start, end = bounds
            inside = [start <= _parse_canonical(ts) < end
                      for ts in (prior["trade_time"], normalized["trade_time"])]
            if inside[0] != inside[1]:
                return (
                    f"unavailable (execution conflict: account={account}, "
                    f"trade_id={execution_id}, restated across this quarter's "
                    f"boundary — {prior['trade_time']} and "
                    f"{normalized['trade_time']} fall in different quarters)"
                )
        if (not _trade_times_agree(prior["trade_time_raw"], snapshot["trade_time_raw"])
                and _in_window()):
            return (
                f"unavailable (execution conflict: account={account}, "
                f"trade_id={execution_id}, differing in trade_time)"
            )
        # Agreeing duplicate — held once, by key; not appended again.

    return rows


def known_orders_from(envelopes: list) -> set[tuple[str, str]] | str:
    """Every `(account, order_id)` — `account` is unconditionally the
    frozen `_SINGLE_ACCOUNT` constant, `order_id` coerced to `str` after
    the same type/non-empty gate `fills_from` applies on the broker side
    and `journal._require_ref_field` applies on the journal side — found in
    any successfully parsed `get_account_orders` snapshot, or an
    `unavailable (...)` reason.

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
    for row, path, _envelope in raw:
        if not isinstance(row, dict):
            return f"unavailable (known_orders: non-object order row in {path.as_posix()})"
        order_id_raw = row.get("order_id")
        if order_id_raw is None:
            return (
                f"unavailable (known_orders: row missing order_id "
                f"in {path.as_posix()})"
            )
        # The SAME gate its two counterparts apply, which this sibling did
        # not: an unconditional `str()` accepted a `bool`, a `float` and a
        # list/dict alike. `fills_from` rejects exactly those on the broker
        # side of this comparison — `str(True)` and the string `"True"` are
        # one key for two orders, `str(1)` and `str(1.0)` are two keys for
        # one — and `journal._require_ref_field` rejects them on the
        # journal side, together with an id that is empty or only
        # whitespace, which can never match anything and so never comes off
        # the backlog. Three readers of one identifier disagreeing about
        # what it may be is how a linked order silently reads as unlinked.
        #
        # Currently INERT: `orders_linked` has no live input under the D1
        # freeze, so this aligns a sibling rather than fixing a live bug.
        # It is written now because the day the broker exposes order
        # history is the day it stops being inert.
        if (isinstance(order_id_raw, (bool, float))
                or not isinstance(order_id_raw, (str, int))
                or str(order_id_raw).strip() == ""):
            return (
                f"unavailable (known_orders: order_id {order_id_raw!r} "
                f"cannot identify an order — D2 froze it as an integer, and "
                f"a value that does not coerce to a UNIQUE non-empty key "
                f"can merge two orders, or split one across two spellings, "
                f"in {path.as_posix()})"
            )
        # Stripped, like its two counterparts: the gate three lines up
        # judges `str(order_id_raw).strip()`, so storing the raw form let
        # `" 10 "` count as an order the journal's own `"10"` ref can never
        # match. Same rule at all three sites that turn this field into a
        # key.
        result.add((_SINGLE_ACCOUNT, str(order_id_raw).strip()))
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
        return f"unavailable (journal: {_no_parens(str(exc))})"


def current_order_id(row: dict):
    """The order id the archive's LATEST sighting of one execution carries —
    `None` when that sighting carries no order id at all.

    `fills_from` writes `order_id_latest` only on a row sighted more than
    once, and it writes it even when the newest sighting carried no
    `order_id`. So ABSENT and PRESENT-AND-`None` are two different facts,
    and `row.get("order_id_latest")` cannot tell them apart: absent means
    never restated, and the row's own first-seen `order_id` still stands,
    while present-and-`None` means the newest sighting was seen and carried
    no id.

    That is the whole claim, deliberately. WHY the id is gone is not
    observable from here: a broker taking it back and a response that
    simply omitted the field arrive as the same bytes, and `fills_from`
    normalizes an absent key and an explicit `null` to one `None` on
    purpose. `unlinked`'s NOTE is worded to the same bound.

    One implementation, per `.claude/rules/producer-consumer.md` #3. Both
    consumers of the distinction — `_count_fills` (which counts orders on
    the broker's newest word) and the CLI's `unlinked` (which warns about
    the rows whose newest word disagrees with the id it prints) — call this
    rather than re-deriving the `"in row"` test. The `.get()` form is
    exactly the divergence the rule names: it reads a withdrawal as "never
    restated", and one of the two call sites had already written it that
    way.
    """
    return row["order_id_latest"] if "order_id_latest" in row else row["order_id"]


# One order is one instrument on one side. These are the two fields that
# decide it, named once so the two consumers cannot drift apart.
_ORDER_COHERENCE_FIELDS = ("symbol", "side")


def order_shapes(rows) -> set:
    """The distinct `(symbol, side)` pairs across a group of fills that
    share one order id — one pair when the group really is one order, more
    than one when the broker reused the id for something else.

    One implementation, per `.claude/rules/producer-consumer.md` rule 3.
    Both consumers call this: the CLI's `unlinked` (which refuses to
    aggregate an incoherent group and names it in a NOTE) and
    `_count_fills` (which cannot count such a group as one order). `unlinked`
    had this notion inline and `_count_fills` had none, which is how
    `2 fills -> 1 order` was rendered for two executions that disagreed
    about what they were.

    Distinct from the D3 execution-conflict check in `fills_from`: that one
    compares two sightings of the SAME EXECUTION and asks whether the
    archive contradicts itself. This one compares two DIFFERENT executions
    and asks whether the broker's own id groups them into one decision.
    """
    return {tuple(row.get(field) for field in _ORDER_COHERENCE_FIELDS)
            for row in rows}


def is_one_order(rows) -> bool:
    """Whether every fill in `rows` agrees about symbol and side."""
    return len(order_shapes(rows)) <= 1


def _fmt_order_shapes(rows) -> str:
    """The disagreeing `(symbol, side)` pairs, sorted, for a refusal
    message. Sorted on the `repr`s so a `None` and a string never meet in a
    comparison (`sorted` on mixed types raises), keeping the text
    deterministic whatever the feed sent."""
    return ", ".join(sorted(f"{symbol!r}/{side!r}"
                            for symbol, side in order_shapes(rows)))


def _count_fills(
    fills: list[dict], start: datetime, end: datetime
) -> tuple[int, int | str, int]:
    """`(fills, orders, order_unknown)` for one quarter — `orders` may be an
    `unavailable (...)` reason instead of a number (see the coherence rule
    below); the other two never are, since neither depends on the grouping.

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

    A group whose fills DISAGREE about symbol or side is not an order, and
    `orders` refuses rather than counting it as one. `unlinked` already had
    this notion — an order id the broker reused for a different instrument
    or a different direction — and refused to aggregate such a group, while
    this site had none: two executions sharing an id and disagreeing about
    what they were rendered as `2 fills -> 1 order`, a decision that was
    never made, indistinguishable from a genuine two-fill order. Both sites
    now call `order_shapes` (`.claude/rules/producer-consumer.md` rule 3).
    Refusal rather than disclosure here because this figure has no channel
    to disclose into — `render` prints a number or a reason, and a number
    is a claim that the grouping held. It is bounded to `orders`: `fills`
    and `order_unknown` do not depend on the grouping, so they keep their
    values (constraint 5). Grouping is on the LATEST id, so the coherence
    question is asked of exactly the group the count would have used.
    """
    in_bounds = [
        row
        for row in fills
        if start <= _parse_canonical(row["trade_time"]) < end
    ]

    grouped_rows: dict[tuple[str, str], list[dict]] = {}
    order_unknown = 0
    for row in in_bounds:
        # The latest word decides BOTH questions — whether this execution
        # has an order at all, and which one. Testing the first sighting
        # for null while grouping on the latest let the two disagree.
        grouped = current_order_id(row)
        if grouped is None:
            order_unknown += 1
        else:
            # The LATEST id the archive holds for this execution, not the
            # first. `order_id` is derived and the broker restates it, so its
            # most recent word is its current word — and that is what an
            # order count is a count of. The ROW keeps its first-seen id
            # (`unlinked` prints it, and a journal link made from an earlier
            # listing has to keep matching); only the grouping used here
            # follows the restatement.
            grouped_rows.setdefault((row["account"], grouped), []).append(row)

    # Sorted, so which incoherent group gets named is the archive's own
    # ordering of ids and not dict-insertion order — the same figure must
    # come out of the same archive on every run (determinism layer).
    for key in sorted(grouped_rows):
        rows = grouped_rows[key]
        if is_one_order(rows):
            continue
        account, order_id = key
        return len(in_bounds), (
            f"unavailable (order incoherence: account={account}, "
            f"order_id={order_id}, its {len(rows)} fills disagree about "
            f"symbol/side — {_fmt_order_shapes(rows)} — so the broker "
            f"reused one id for more than one decision and they are not "
            f"one order)"
        ), order_unknown

    # An earlier version compared the first-seen and latest-seen totals and
    # refused when they disagreed. That was both weaker and stranger than it
    # looked: three snapshots can put a contradictory grouping in the middle
    # while the two ENDS agree, so it established a number over an archive
    # that disagrees with itself — and where it did fire, it refused a figure
    # the broker had in fact restated to a definite value. There is no
    # ambiguity to adjudicate here. The broker owns this field, it says so
    # again on every pull, and its latest statement is the answer.
    return len(in_bounds), len(grouped_rows), order_unknown


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


def _pin_reaches_quarter_end(spy_series: dict, bounds) -> bool:
    """Whether the pinned SPY series provably runs to the quarter's LAST
    SESSION — the only condition under which the two market-calendar checks
    in `assemble` are a cross-check at all.

    `calendar_checked` used to be `not isinstance(spy_series, str)`: true
    whenever the pin was READABLE, and it never asked what the pin covered.
    The pin is write-once and never re-fetched, so one taken mid-quarter
    stops where it stopped. If the account series stops there too, the
    "market traded after your last observation" check finds nothing — the
    two short series agree with each other — and that agreement was
    reported as having been cross-checked against a trading calendar, on a
    quarter whose final sessions neither of them covers. A real move on the
    omitted days then renders as `0.0%` drawdown and `+0.00pp` context,
    labelled verified. The label is what makes a short series look checked,
    so the label has to be earned by something other than the endpoint the
    check exists to audit.

    Earned WITHOUT an external calendar, and only in the case that can be:
    every calendar day after the pin's last in-quarter row, up to the
    quarter's exclusive end, must be a WEEKEND day. Weekends are the one
    non-trading class that is a property of the calendar itself — a holiday
    can only ADD a closure, never open a Saturday — so if nothing but
    Saturdays and Sundays remain, the pin's last row IS the quarter's last
    session and the checks below really did consult a market calendar. A
    WEEKDAY in that span is unresolvable from here: it is either a market
    holiday (the pin is complete) or a day the fetch never reached (the pin
    is short), and the pin cannot tell those apart ABOUT ITSELF. That case
    now returns False and the RISK line says so, instead of claiming a
    check it could not make.

    The ordinary complete pin is unaffected: all four quarter ends fall on
    a weekday the market normally trades, so a pin fetched after the
    quarter closed ends exactly on it and nothing remains to inspect. The
    cost is the holiday quarter-end this module already records as a known
    limit (2018-03-31 was a Saturday behind a Good Friday): the label is
    withheld there, which is the honest answer rather than a wrong one.
    """
    end_date = bounds[1].date()
    start_date = bounds[0].date()
    in_quarter = [k for k in spy_series
                  if start_date <= _parse_calendar_date(k) < end_date]
    if not in_quarter:
        # The pin says nothing about this quarter at all, so it is not a
        # calendar for it. `ensure_pinned`'s span check normally refuses
        # such a fetch, but a pin already on disk is read as it is found.
        return False
    day = _parse_calendar_date(max(in_quarter)) + timedelta(days=1)
    while day < end_date:
        if day.weekday() < 5:      # Mon-Fri: could have been a session
            return False
        day += timedelta(days=1)
    return True


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
    # The 14-day reach-back is load-bearing twice over, and only one of the
    # two is obvious. It gives `d0` — the last close BEFORE the quarter —
    # room to exist across a holiday weekend; and for a FIRST quarter it is
    # the only thing that puts the prior year's final close in the pin at
    # all, which is what `_spy_baseline` needs, since the account's own Q1
    # baseline falls on January 1st when the market is shut. Shrink this and
    # Q1's CONTEXT silently stops rendering again.
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


def ensure_pinned(reports_root, quarter: str, fetch=None, required=()) -> Path | str:
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
        return f"unavailable (SPY pin: fetch failed for {quarter}: {_no_parens(str(exc))})"

    if not rows:
        return f"unavailable (SPY pin: fetch returned no rows for {quarter})"

    # Validate BEFORE publishing. The pin is write-once by design — that is
    # what makes a rendered CONTEXT reproducible — so a fetch that returns a
    # NaN, an infinity or an unreadable date used to be written anyway and
    # then failed every later read, with the short-circuit above ensuring it
    # was never re-fetched. One transient bad response, and that quarter's
    # CONTEXT was unavailable until a human deleted the file. A refusal here
    # leaves no pin, so the next run simply tries again.
    lines = ["date,adj_close"]
    checked: list[tuple[str, float]] = []
    for date_str, close in rows:
        try:
            canonical = _canonical_calendar_date(date_str)
        except (ValueError, TypeError):
            return (f"unavailable (SPY pin: fetch returned an unreadable date "
                    f"{date_str!r} for {quarter} — nothing was pinned)")
        if isinstance(close, bool) or not isinstance(close, (int, float)):
            return (f"unavailable (SPY pin: fetch returned a non-numeric close "
                    f"{close!r} at {canonical} for {quarter} — nothing was pinned)")
        try:
            finite = math.isfinite(float(close))
        except (OverflowError, ValueError):
            finite = False
        if not finite or close <= 0:
            return (f"unavailable (SPY pin: fetch returned {close!r} at "
                    f"{canonical} for {quarter}, which is not a usable close "
                    f"— nothing was pinned)")
        # Both forms: the CSV keeps the value as fetched, while the
        # conflict check below compares the NUMBERS. Rebuilding them from
        # the serialized lines compared strings, so `100` and `100.0` — the
        # same close, spelled two ways by one response — were refused as a
        # disagreement.
        checked.append((canonical, float(close)))
        lines.append(f"{canonical},{close}")
    # The CROSS-row check, before publishing — the per-row loop above
    # cannot see two rows carrying different values for one date. Written
    # anyway, that pin failed `read_pinned` on every later read and could
    # never be re-fetched, because the existence short-circuit runs first.
    # Same half-fix as the per-row validation it sits under: validating
    # each row is not validating the file.
    conflict = observations_to_series(checked)
    if isinstance(conflict, str):
        return (f"unavailable (SPY pin: fetch returned a conflicting series "
                f"for {quarter} — {_unwrap_reason(conflict)} — nothing was "
                f"pinned)")

    # And it has to SPAN the quarter, not merely parse. A response holding
    # one January row published happily and then failed every read of a Q2
    # report with "no pinned SPY row for 2026-03-31, 2026-06-30" — the pin
    # is write-once, so a single short response disabled CONTEXT for that
    # quarter until a human deleted the file. The bar is the same one the
    # account series is held to: something before the quarter to measure
    # from, and something within `_MAX_GAP_DAYS` of its last day.
    q_start, q_end = quarter_bounds(quarter)
    observed = sorted(d for d, _ in checked)
    span_problem = None
    if not observed:
        span_problem = "no dated rows"
    else:
        first = _parse_calendar_date(observed[0])
        last_day = (q_end - timedelta(days=1)).date()
        # The last row INSIDE the quarter, not the latest row overall. A
        # response reaching past the quarter — March 31st and July 1st, for
        # a Q2 pin — made `(June 30 - July 1).days` negative, so the check
        # passed while the pin held no Q2 endpoint at all.
        in_quarter = [d for d in observed
                      if q_start.date() <= _parse_calendar_date(d) <= last_day]
        last = _parse_calendar_date(in_quarter[-1]) if in_quarter else first
        before = [d for d in observed if _parse_calendar_date(d) < q_start.date()]
        if not before:
            span_problem = (f"nothing before the quarter to measure from — "
                            f"earliest row is {observed[0]}")
        elif (q_start.date() - _parse_calendar_date(before[-1])).days > _MAX_GAP_DAYS:
            # The SAME proximity rule `_spy_baseline` applies. They used
            # different bars: this one asked only for "some row before the
            # quarter", so a pin whose nearest earlier close was two weeks
            # back was written — and then `_spy_baseline` refused to
            # substitute it, leaving CONTEXT unavailable on a write-once
            # file that no later run re-fetches. Two of my own fixes,
            # disagreeing about the same date.
            span_problem = (f"its last row before the quarter is "
                            f"{before[-1]}, more than {_MAX_GAP_DAYS} days "
                            f"before {q_start.date().isoformat()}")
        elif not in_quarter:
            span_problem = f"no row inside {quarter} at all"
        elif (last_day - last).days > _MAX_GAP_DAYS:
            span_problem = (f"its last row inside the quarter is "
                            f"{in_quarter[-1]}, more than {_MAX_GAP_DAYS} days "
                            f"before {last_day.isoformat()}")
    if span_problem:
        return (f"unavailable (SPY pin: fetch returned a series that cannot "
                f"cover {quarter} — {span_problem} — nothing was pinned)")

    # The EXACT dates this report will ask for, when the caller knows them.
    # The span check above uses a tolerance and `spy_return` does not, so a
    # response ending two days short of the quarter cleared one and failed
    # the other — a pin that reads back perfectly and answers nothing, made
    # permanent by write-once. The caller has already picked the account's
    # endpoints by the time it asks for a pin, so it can simply say which
    # dates matter. Refusing here leaves no file, and the next run is free
    # to fetch a complete one.
    missing = [d for d in required if d is not None and d not in dict(checked)]
    if missing:
        return (f"unavailable (SPY pin: fetch returned no close for "
                f"{', '.join(missing)}, which {quarter}'s figures are measured "
                f"between — nothing was pinned)")

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
        #
        # No "what already exists" anchor computed before the mkdir (Item
        # A, cycle-5 fix wave — same defect and the same fix as the
        # archive writer). That check answered "what did THIS call
        # create", which reads as "nothing" on a RETRY after a failed
        # `_fsync_dir_chain`: the failed attempt's own `mkdir` already put
        # every directory on disk, so the retry's anchor lands on the
        # leaf and never re-syncs the ancestor whose durability failed the
        # first time. `write_bytes_new` now re-syncs the whole chain
        # unconditionally on every call, so a retry re-proves it
        # statelessly instead of trusting what merely exists.
        quarter_dir.mkdir(parents=True, exist_ok=True)
        write_bytes_new(target, data)
    except OSError as exc:
        return (
            f"unavailable (SPY pin: could not write "
            f"{target.as_posix()}: {_no_parens(str(exc))})"
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

    Every close is held to the bar `ensure_pinned` writes under — finite
    and strictly positive — rather than merely parseable as a float. A pin
    is write-once, so a row that could never have been written is a row
    that will be read forever, and the calendar cross-checks downstream
    read any key in this mapping as a session the market traded.

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
        return (f"unavailable (SPY pin: unreadable file {path.as_posix()}: "
                f"{_no_parens(str(exc))})")

    reader = csv.DictReader(text.splitlines())
    try:
        fieldnames = reader.fieldnames or ()
    except csv.Error as exc:
        return (f"unavailable (SPY pin: {path.as_posix()} is not readable CSV: "
                f"{_no_parens(str(exc))})")
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

    if set(fieldnames) != {"date", "adj_close"}:
        # The writer emits exactly two columns. A third one means this file
        # is not the shape the pin is written in, and reading only the two
        # names it recognizes rendered a NUMBER out of a file whose columns
        # it could not account for.
        return (
            f"unavailable (SPY pin: {path.as_posix()} has columns "
            f"{list(fieldnames)!r}, not the date/adj_close pair a pin is "
            "written with)"
        )

    pairs: list[tuple[str, float]] = []
    try:
        rows = list(reader)
    except csv.Error as exc:
        # `_csv.Error: field larger than field limit` — a 140k-character
        # field raised out of the ITERATION, past every check above, out of
        # `read_pinned`, and out of `report`: a traceback and no summary at
        # all, where a malformed benchmark should cost CONTEXT and nothing
        # else.
        return (f"unavailable (SPY pin: {path.as_posix()} is not readable "
                f"CSV: {_no_parens(str(exc))})")
    for row in rows:
        raw_date = row.get("date")
        raw_close = row.get("adj_close")
        if raw_date is None or raw_close is None or None in row:
            # `None in row` is the SURPLUS-field case: `csv.DictReader`
            # files every extra column under the `None` key, so a row with
            # more fields than the header parsed cleanly while its extras
            # went unread.
            return f"unavailable (SPY pin: malformed row in {path.as_posix()}: {row!r})"
        try:
            date_str = _canonical_calendar_date(raw_date)
        except (ValueError, TypeError) as exc:
            return (
                f"unavailable (SPY pin: unparseable date {raw_date!r} "
                f"in {path.as_posix()}: {_no_parens(str(exc))})"
            )
        try:
            close = float(raw_close)
        except (ValueError, TypeError):
            return (
                f"unavailable (SPY pin: unparseable close {raw_close!r} "
                f"in {path.as_posix()})"
            )
        # The SAME bar `ensure_pinned` holds a fetch to, applied on the way
        # back in. The module states a reject-at-write philosophy and then
        # trusted whatever parsed on read, with only `spy_return`'s two
        # ENDPOINTS re-checked — so an interior `nan`, `inf` or negative row
        # in an existing pin came back as a live series key, and the
        # market-calendar checks read that key as proof the market traded
        # that day. The cost is a false "series stops short" or "missing
        # interior session" refusal that blanks an otherwise-valid RISK, and
        # the pin is write-once, so it never re-fetches its way out.
        #
        # Still fail-closed — a pin holding an unusable close is not a pin
        # this tool will render a number from — but refusing HERE refuses
        # for the true reason, naming the row, instead of surfacing as a
        # calendar disagreement somewhere else.
        if not math.isfinite(close) or close <= 0:
            return (
                f"unavailable (SPY pin: {raw_close!r} at {date_str} in "
                f"{path.as_posix()} is not a usable close — adj_close must "
                f"be finite and > 0, the same bar the pin was written under)"
            )
        pairs.append((date_str, close))

    return observations_to_series(pairs)


def _spy_baseline_date(d0: str | None) -> str | None:
    """The date `_spy_baseline` will look for, decided WITHOUT a pinned
    series in hand.

    `ensure_pinned` needs to know which closes a report will ask for before
    the pin exists, and for January 1st the answer is "some earlier close",
    which no exact date can name. Returning None there asks for nothing —
    the span check above already proves the series reaches back before the
    quarter, which is exactly what that case needs.
    """
    if d0 is None:
        return None
    try:
        d = _parse_calendar_date(d0)
    except (ValueError, TypeError):
        return d0
    return None if (d.month, d.day) == (1, 1) else d0


def _spy_baseline(spy_series: dict, d0: str | None) -> str | None:
    """The pinned date to measure SPY from, given the account's `d0`.

    Normally `d0` itself: both series observe the same trading days, and
    pairing different dates is exactly the mismatch `spy_return`'s exact-key
    rule exists to prevent. January 1st is the one date where that rule
    cannot be met — the account's `cps` resets on it and IBKR emits an
    observation there, while the market is closed and no SPY close exists.
    Both series still mean the same instant: the value at the start of the
    year. So for that date, and only that date, the baseline is the last
    pinned close BEFORE it — which the pin already carries, since it fetches
    a window reaching back past the quarter's first day.
    """
    if d0 is None or d0 in spy_series:
        return d0
    try:
        d0_date = _parse_calendar_date(d0)
    except (ValueError, TypeError):
        return d0
    if (d0_date.month, d0_date.day) != (1, 1):
        return d0
    earlier = []
    for key in spy_series:
        try:
            if _parse_calendar_date(key) < d0_date:
                earlier.append(key)
        except (ValueError, TypeError):
            return d0           # a key this cannot read: fail to the exact rule
    if not earlier:
        return d0
    # And it has to be CLOSE. Taking the latest row before January 1st
    # without asking how far back it sits let a pin whose nearest earlier
    # close was December 18th render a Q1 CONTEXT carrying two weeks of
    # December — a wrong number, permanently pinned. The same tolerance the
    # account series is held to: a substitute more than `_MAX_GAP_DAYS`
    # away is not the year's starting value, and returning `d0` here means
    # `spy_return` refuses by name instead.
    best = max(earlier)
    if (d0_date - _parse_calendar_date(best)).days > _MAX_GAP_DAYS:
        return d0
    return best


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
    return _finite_or_reason(v1 / v0 - 1, "SPY return", f"{s0}->{s1}")


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
                f"unparseable file {e.path.as_posix()} — {_no_parens(e.reason)})"
            )

    start, end = bounds
    quarter_start = start.date()
    quarter_end = end.date()

    groups: dict[str, list[tuple[str, float]]] = {}
    pending: dict[str, list] = {}
    reaching: set[str] = set()
    for e in envelopes:
        if not isinstance(e, Envelope):
            continue
        if e.completeness != "complete":
            # The gate `coverage_gap` already applies to the WINDOW,
            # applied to the VALUES. Without it the two combined into
            # something worse than either: a complete pull establishes
            # coverage of the whole quarter while its own series stops on
            # April 1 — alone it refuses, `gap_reason` seeing a series that
            # ends three months early — and a truncated pull fills the
            # remainder, so both figures render and every value past the
            # first day came from an input the archive itself marks as not
            # fully captured.
            #
            # Unlike a trades pull, whose extra rows are real executions
            # that can only make a count more accurate, an incomplete
            # series is missing OBSERVATIONS: its endpoints and its holes
            # are exactly what RISK and CONTEXT are computed from.
            continue
        response = e.response
        try:
            ytd = response["accounts"]["account"]["periods"]["YTD"]
            dates = ytd["dates"]
            cps_values = ytd["cps"]
            baseline_raw = ytd["start_date"]
        except (TypeError, KeyError, IndexError):
            if _cannot_reach(e, bounds):        # rule + rationale: see `_cannot_reach`
                continue
            return (
                "unavailable (observations: envelope "
                f"{e.path.as_posix()} response does not carry "
                "accounts.account.periods.YTD.{dates,cps,start_date})"
            )
        if not isinstance(dates, list) or not isinstance(cps_values, list):
            if _cannot_reach(e, bounds):
                continue
            return (
                "unavailable (observations: envelope "
                f"{e.path.as_posix()} dates/cps are not lists)"
            )
        if len(dates) != len(cps_values):
            if _cannot_reach(e, bounds):
                continue
            return (
                "unavailable (observations: envelope "
                f"{e.path.as_posix()} dates/cps length mismatch: "
                f"{len(dates)} dates vs {len(cps_values)} cps)"
            )
        try:
            baseline = _canonical_calendar_date(baseline_raw)
        except (ValueError, TypeError) as exc:
            if _cannot_reach(e, bounds):
                continue
            return (
                "unavailable (observations: unparseable start_date "
                f"{baseline_raw!r} in {e.path.as_posix()}: {_no_parens(str(exc))})"
            )

        # Disqualify before validating, not after. The `qualifying` loop
        # below already discards every baseline on or after this quarter's
        # start — an envelope with one cannot contribute an observation to
        # this window no matter what its rows hold. Parsing it first meant
        # one malformed row in a pull from two years ago refused THIS
        # quarter's RISK and CONTEXT, permanently and for every quarter
        # after it: the archive is append-only, so the offending envelope
        # never goes away. The rows of a disqualified baseline were already
        # collected and then dropped unread, so skipping here changes no
        # figure — only which errors can reach a report they have no
        # bearing on. A baseline that CANNOT be read is not skipped: the
        # `except` above still refuses, because an envelope that cannot be
        # placed in time cannot be shown to be irrelevant.
        if not _baseline_qualifies(_parse_calendar_date(baseline), quarter_start):
            continue

        # Whether this baseline can qualify at all is decided FIRST, over
        # every envelope, before a single row is validated. It cannot be
        # decided per-envelope: `groups` is keyed by baseline, so an
        # envelope carrying one row can belong to a baseline another
        # envelope qualifies — that is exactly the shape of the planted
        # cross-envelope cps conflict the gate matrix pins, and skipping
        # such an envelope would drop the conflicting row and render a
        # figure. Deferring the validation instead means one malformed
        # `cps` in a 2025 pull stops removing RISK and CONTEXT from every
        # later quarter, permanently, in an archive that never forgets.
        baseline = _normalize_baseline(baseline)
        pending.setdefault(baseline, []).append((dates, cps_values, e))
        if baseline in reaching:
            continue
        if not isinstance(dates, list) or not dates:
            # Nothing to read: an empty series cannot reach the window, and
            # a non-list `dates` already failed the shape gate above.
            continue
        for date_raw in dates:
            try:
                d = _parse_calendar_date(_canonical_calendar_date(date_raw))
            except (ValueError, TypeError):
                reaching.add(baseline)      # unreadable -> cannot rule it out
                break
            if quarter_start <= d < quarter_end:
                reaching.add(baseline)
                break

    for baseline, entries in pending.items():
        # The latest readable observation strictly before the quarter — the
        # only candidate `pick_endpoints` can choose as `d0`. Read
        # leniently and only to bound what must be validated below; an
        # unreadable date leaves it None, which skips nothing.
        d0_floor = None
        for dates, _cps, _e in entries:
            for raw in dates if isinstance(dates, list) else []:
                try:
                    d = _parse_calendar_date(_canonical_calendar_date(raw))
                except (ValueError, TypeError):
                    d0_floor = None
                    break
                if d < quarter_start and (d0_floor is None or d > d0_floor):
                    d0_floor = d
            else:
                continue
            break
        if baseline not in reaching:
            # Recorded so the no-qualifying-baseline diagnostic can still
            # name it; its rows are deliberately never validated.
            groups.setdefault(baseline, [])
            continue
        for dates, cps_values, e in entries:
            rows: list[tuple[str, float]] = []
            for date_raw, cps_raw in zip(dates, cps_values):
                try:
                    date_str = _canonical_calendar_date(date_raw)
                except (ValueError, TypeError) as exc:
                    return (
                        "unavailable (observations: unparseable date "
                        f"{date_raw!r} in {e.path.as_posix()}: {_no_parens(str(exc))})"
                    )
                # A row dated AFTER this quarter cannot reach any figure it
                # produces: `d1` is the last observation before the
                # quarter's end, and the interior gap check runs inside
                # `[d0, d1]`. Validating it anyway meant one malformed Q4
                # observation blanked Q2's RISK and CONTEXT — an earlier
                # quarter whose own endpoints and interior are fully
                # established. The date is parsed FIRST, so a row that
                # cannot be placed in time is still refused above.
                observed = _parse_calendar_date(date_str)
                if observed >= quarter_end:
                    continue
                # And a row before the baseline this quarter will actually
                # use. `d0` is the LAST observation before the quarter, so
                # anything earlier than that cannot reach the TWR ratio, the
                # drawdown window or the interior gap check — while one
                # malformed January value was blanking Q3. `d0_floor` is
                # computed leniently over the whole qualifying baseline
                # first; when it cannot be determined, nothing is skipped.
                if d0_floor is not None and observed < d0_floor:
                    continue
                if isinstance(cps_raw, bool) or not isinstance(cps_raw, (int, float)):
                    return (
                        "unavailable (observations: non-numeric cps "
                        f"{cps_raw!r} at {date_str} in {e.path.as_posix()})"
                    )
                try:
                    cps_value = float(cps_raw)
                except OverflowError:
                    # A JSON number is parsed as a Python int of unbounded
                    # width, so `10**400` clears the isinstance check above
                    # and then raises here. Uncaught, it took the WHOLE
                    # report down — no summary file at all, losing the fill
                    # and journal figures that do not depend on this series.
                    return (
                        "unavailable (observations: cps at "
                        f"{date_str} in {e.path.as_posix()} is too large to "
                        "represent as a float)"
                    )
                rows.append((date_str, cps_value))

            groups.setdefault(baseline, []).extend(rows)

    qualifying: list[str] = []
    for baseline, rows in groups.items():
        if not _baseline_qualifies(_parse_calendar_date(baseline), quarter_start):
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
    earliest, earliest_date = None, None
    for key in series:
        try:
            key_date = _parse_calendar_date(key)
        except (ValueError, TypeError):
            return None, None
        if key_date < start_date and (d0_date is None or key_date > d0_date):
            d0, d0_date = key, key_date
        if key_date < end_date and (d1_date is None or key_date > d1_date):
            d1, d1_date = key, key_date
        if earliest_date is None or key_date < earliest_date:
            earliest, earliest_date = key, key_date

    # The FIRST quarter of a year has no observation before it, by
    # construction: `cps` resets every January 1st (D1), so a YTD series
    # begins ON the quarter's first day. Requiring `d0` strictly earlier
    # therefore made RISK and CONTEXT permanently unavailable for one
    # quarter in four — verified on the live archive, where 2026Q1 rendered
    # `gap check: missing endpoint` against a series that covers it
    # completely, and against a freeze that says YTD "covers any quarter of
    # the current year".
    #
    # Restricted to January 1st, not "any quarter whose series happens to
    # begin on its first day". The reset is what makes that first
    # observation a BASELINE rather than just the earliest point available:
    # a Q3 series that starts on July 1st is a truncated one, and treating
    # its first point as a baseline would compute a return over part of the
    # quarter and label it the quarter's. Pinned by a test that predates
    # this branch and failed the moment the rule was written too broadly.
    if (d0 is None and earliest_date == start_date
            and (start_date.month, start_date.day) == (1, 1)):
        d0 = earliest
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
        return (f"unavailable (gap check: unparseable endpoint: "
                f"{_no_parens(str(exc))})")

    ordering_violations: list[str] = []
    # `>` not `>=`: d0 ON the quarter's first day is the one legal case, and
    # it is the ONLY d0 a first quarter can have. `cps` resets every January
    # 1st, so a YTD series begins on that day — the observation there is the
    # baseline the whole series is measured from, and the ratio through it
    # is the quarter's own return. Rejecting it made RISK and CONTEXT
    # permanently unavailable for one quarter in four, against a freeze that
    # says YTD covers any quarter of the current year. `pick_endpoints` will
    # not hand this back for any other quarter: it requires the series to
    # BEGIN exactly on the quarter's first day.
    if d0_date > start_date:
        ordering_violations.append(
            f"d0 {d0} is after quarter start {start_date.isoformat()}"
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
    return _finite_or_reason(g1 / g0 - 1, "quarterly TWR", f"{d0}->{d1}")


def _finite_or_reason(value: float, what: str, where: str) -> float | str:
    """`value`, or an `unavailable (...)` reason when it is not finite.

    Every operand reaching these ratios is already checked finite, which is
    not the same as the RESULT being finite: a growth factor just above
    zero divided into a large one overflows to `inf` from two perfectly
    ordinary finite floats. It then survived every later check and rendered
    as `+infpp` — a figure, sitting where this report promises a refusal.
    Guard the result, not only the inputs.
    """
    if not math.isfinite(value):
        return (f"unavailable ({what}: the computation overflowed to "
                f"{value} at {where} — the inputs were finite, the result "
                f"is not)")
    return value


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
        return (f"unavailable (quarterly_drawdown: unparseable endpoint: "
                f"{_no_parens(str(exc))})")
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
    return _finite_or_reason((account_twr - spy) * 100, "CONTEXT", "account - SPY")


# --- Task 7: assembly and render (spec §6) -------------------------------

# The three archived tools, in the fixed order `assemble` reads them (and
# in which their envelopes are named in INPUTS) — DATA's trades, then the
# orders snapshot that only ever feeds the compiled-disabled orders_linked
# slot, then the performance pulls RISK/CONTEXT read.
#
# `_TRADES_TOOL` is imported from `archive` (Task 4), not redefined here:
# `archive.coverage_gap`'s union branch needs the same spelling, and having
# it live in two places is the drift the module docstring above used to
# excuse as "close enough together to review by eye".
_ORDERS_TOOL = "get_account_orders"
assert _ORDERS_TOOL in KNOWN_TOOLS  # a rename here that misses archive.py silently orphans the archive
_PERFORMANCE_TOOL = "get_pa_performance_all_periods"
assert _PERFORMANCE_TOOL in KNOWN_TOOLS  # a rename here that misses archive.py silently orphans the archive

_MINUS = "−"  # U+2212 MINUS SIGN — the spec layout's own glyph, never ASCII "-"


def _label(text: str) -> str:
    """Left-justifies a section label (or the empty string, for a
    continuation line) to the fixed 9-column width every section head in
    the spec §6 layout shares (`DATA`, `RISK`, `CONTEXT`, `INPUTS` all pad
    to 9)."""
    return f"{text:<9}"


def _envelopes_or_empty(root, tool: str) -> list:
    """`read_envelopes`, with a DIRECTORY-level failure turned into a single
    unreadable entry for that tool instead of an exception.

    Per-file errors are already `ParseFailure`s; this covers the enclosing
    directory — unreadable permissions, a path replaced by a file, a broken
    mount. That `OSError` escaped `assemble` and `report` refused outright:
    exit 1, no summary written, and the journal-derived counts discarded
    along with it, on a tool whose contract is that one unavailable input
    blanks the figures it feeds and no others. A `ParseFailure` carries the
    right meaning here — coverage for this tool cannot be established —
    without silencing anything.
    """
    try:
        return read_envelopes(root, tool)
    except OSError as exc:
        return [ParseFailure(path=Path(root) / tool,
                             reason=f"archive directory unreadable: {_no_parens(str(exc))}")]


def _normalize_baseline(baseline: str) -> str:
    """One key for the two spellings of a year's reset instant.

    A YTD baseline of `2025-12-31` and one of `2026-01-01` name the same
    moment — the point `cps` measures 2026 from — and both are written in
    the wild: the live account uses the first, the frozen shape's own
    example the second. `_baseline_qualifies` accepts both, which is right
    and by itself made things worse: they grouped under different keys, so
    an archive holding BOTH reported "multiple archived YTD baselines" and
    blanked RISK and CONTEXT over data that agrees with itself. Grouping
    has to agree with qualification.

    December 31st folds forward to the following January 1st; nothing else
    is touched, so a genuinely mid-year baseline stays its own key.
    """
    try:
        d = _parse_calendar_date(baseline)
    except (ValueError, TypeError):
        return baseline
    if (d.month, d.day) == (12, 31):
        return date(d.year + 1, 1, 1).isoformat()
    return baseline


def _baseline_qualifies(baseline_date, quarter_start) -> bool:
    """Whether a YTD baseline can be the one a quarter is measured from.

    Strictly before the quarter, EXCEPT at January 1st: `cps` resets on the
    year's first day, so a baseline stamped with that day is the baseline
    rather than a point inside the period. Both spellings occur — the live
    account writes the prior year's last day, the frozen shape's own example
    writes January 1st — and they mean the same instant.

    One function because this rule lived in two places and diverged: cycle
    14 relaxed the collection pass, cycle 15 found the qualifying pass still
    rejecting exactly what the first had just accepted, and half the
    observed Q1 shapes could report nothing in between. `.claude/rules/
    producer-consumer.md` rule 3 names this shape precisely.
    """
    if baseline_date > quarter_start:
        return False
    if baseline_date == quarter_start:
        return (quarter_start.month, quarter_start.day) == (1, 1)
    return True


def _cannot_reach(envelope, bounds) -> bool:
    """True when this envelope's OWN args prove it cannot contribute to
    `bounds` — the test that decides whether a malformed one is worth
    refusing over.

    Used only on error paths, and only where the envelope has already
    failed to yield usable content. A well-formed envelope is never
    dismissed by it. The archive is append-only, so without this a single
    bad pull from two years ago removed the figures it feeds from every
    later quarter, permanently: four separate gates in `observations_from`
    refused before anything asked whether the pull could matter, and each
    was found on its own review cycle after the previous one was moved.
    An envelope whose window cannot be READ returns False and still
    refuses — then the unknown really could be this quarter's.

    Six call sites carry this rule: the shared row gate, the fill
    normalizer's per-row reject, and four shape gates in
    `observations_from`. They call and do not restate it; each site that
    restated it had drifted from the others by the time the last one was
    added.
    """
    if bounds is None:
        return False
    covered = _covered_range(envelope.tool, envelope.args, envelope.pulled_at)
    if covered is None:
        return False
    return not (covered[0] < bounds[1] and bounds[0] < covered[1])


def _holds_a_row_inside(envelope, bounds) -> bool:
    """True when this envelope carries at least one dated row inside
    `bounds`, whatever its `args` claim.

    Only the two row-bearing shapes are inspected — trades, and the
    performance YTD date series. Orders carry no timestamp, so they are
    decided by their declared window alone. Anything unreadable here
    returns True, which names the envelope: naming is the direction that
    cannot mislead.
    """
    start, end = bounds
    try:
        response = envelope.response
        if envelope.tool == _TRADES_TOOL:
            rows = response.get("trades") or []
            stamps = [r.get("trade_time") for r in rows if isinstance(r, dict)]
        elif envelope.tool == _PERFORMANCE_TOOL:
            stamps = response["accounts"]["account"]["periods"]["YTD"]["dates"]
        else:
            return False
        if not isinstance(stamps, list):
            # Checked INSIDE the guard. The loop below sits outside it, so a
            # `dates` that is an int raised `TypeError: not iterable` all the
            # way out of `report` — no summary written at all, on a tool
            # whose contract is that one bad input costs one figure. The
            # shape gate has to precede the iteration, not follow it.
            return True
    except (AttributeError, TypeError, KeyError):
        return True
    for raw in stamps:
        try:
            when = _canonical_calendar_date(raw)
        except (ValueError, TypeError):
            return True
        if start.date() <= _parse_calendar_date(when) < end.date():
            return True
    return False


def _envelope_inputs(tool: str, envelopes: list, bounds=None) -> list[str]:
    """`<tool>/<basename>` for every archived pull of `tool` that could
    have contributed to `bounds`, plus one counted line for those that
    could not.

    A `ParseFailure` is ALWAYS named individually, whatever `bounds` says
    — that is what constraint 3 protects ("the summary names every input
    it read, never only the ones that parsed"), and a file whose `args`
    could not be read cannot be shown to be out of window anyway.

    What changed and why: this listed every archived pull unconditionally,
    so a quarterly report's INPUTS block grew with the account's lifetime
    rather than with the quarter. Measured at five quarters and 96 files,
    a report went from 26 lines to 70 while its numeric section stayed
    byte-identical — the audit trail crowding out the summary it exists to
    support. A pull whose own window does not intersect the quarter cannot
    have contributed a row to it, so naming it asserted nothing. The count
    stays, because "the tool read N other files" is a fact a reader may
    want, and silently dropping them would be its own small lie.

    `bounds=None` keeps every name — callers with no quarter in hand are
    unchanged.
    """
    named: list[str] = []
    elided = 0
    for e in envelopes:
        if bounds is None or not isinstance(e, Envelope):
            named.append(f"{tool}/{e.path.name}")
            continue
        covered = _covered_range(e.tool, e.args, e.pulled_at)
        # Unrecognized args -> no window -> cannot be shown irrelevant, so
        # it is named, matching `coverage_gap`'s own fail-closed reading.
        # `_holds_a_row_inside` is the second chance and it decides ties:
        # the counts are driven by each ROW's own timestamp, not by its
        # envelope's declared window, so an envelope whose `args` say one
        # quarter while its rows fall in another still contributes — and
        # eliding it printed a report that said the pull does not reach this
        # quarter directly above a figure computed partly from it.
        if (covered is None
                or (covered[0] < bounds[1] and bounds[0] < covered[1])
                or _holds_a_row_inside(e, bounds)):
            named.append(f"{tool}/{e.path.name}")
        else:
            elided += 1
    if elided:
        # Not `{tool}/...`: every other line here IS a path, and a counted
        # line wearing a path's prefix reads as a malformed filename.
        named.append(f"({elided} further {tool} pull{'s' if elided > 1 else ''} "
                     f"archived, whose own window does not reach this quarter)")
    return named


def _plural(value, noun: str) -> str:
    """`noun` or `noun`+s, unless the count is a refusal.

    `1 fills → 1 orders` is the report's own sloppiness showing in the one
    line a person reads every quarter. A refused count keeps the plural:
    the reason has replaced the number, and inflecting a noun to agree with
    an `unavailable (...)` string would be inventing agreement with nothing.
    """
    if not isinstance(value, int) or value == 1:
        return noun if value == 1 else noun + "s"
    return noun + "s"


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
    observed: tuple[str, str] | None = None,
    calendar_checked: bool = True,
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

    # The honest signal-detection horizon, stated where the user reads it
    # rather than only in a design doc they never open. Placed BEFORE any
    # DATA/RISK/CONTEXT head on purpose: grouping it under a section would
    # make its digits indistinguishable from a stale figure on a quarter
    # where that section's own numbers refused.
    #
    # It used to open "~126 episodes/year HERE" — the recording operator's
    # own trade frequency and volatility, printed in every recipient's
    # report as a statement about THEIR account. Wrong for them, and a
    # disclosure about the operator besides. The requirement itself is a
    # property of the test, not of any account, so it stays; what was
    # measured from one book is now either read off this quarter's own
    # established count or not asserted at all.
    need = ("Horizon: detecting a +2%/trade edge needs on the order of 270 "
            "independent episodes,")
    lines.append(need)
    lines.append("even under generous independence assumptions and a "
                 "concentrated equity book's")
    if isinstance(counts.fills, int):
        lines.append(f"volatility. This quarter recorded {counts.fills} "
                     f"{_plural(counts.fills, 'fill')}; compare that to the")
        lines.append("requirement before reading any figure below as skill.")
    else:
        lines.append("volatility. This quarter's own fill count is "
                     "unavailable, so the comparison")
        lines.append("cannot be made here — treat every figure below as far "
                     "short of evidence.")
    lines.append("")

    fills = _fmt_field(counts.fills)
    orders = _fmt_field(counts.orders)
    order_unknown = _fmt_field(counts.order_unknown)
    lines.append(
        f"{_label('DATA')}{fills} {_plural(counts.fills, 'fill')} → "
        f"{orders} {_plural(counts.orders, 'order')} "
        f"(order_unknown {order_unknown})"
        "   [each: <N> | unavailable (reason)]"
    )
    lines.append(
        f"{_label('')}journal: {_fmt_field(counts.orders_linked)} "
        "unique orders linked this quarter;"
    )
    lines.append(
        f"{_label('')}{_label('')}{_fmt_field(counts.thesis_versions)} "  # noqa: E501
        f"{_plural(counts.thesis_versions, 'thesis version')} declared this quarter"
    )

    # The observed endpoints, printed rather than assumed. A series can
    # stop before the quarter's final session — a demonstrated case
    # rendered `0.0%` for a quarter whose real figure was `−50.0%`, and the
    # final session is exactly where a drawdown can appear. Two guards now
    # catch the common causes (coverage refuses a pull taken before that
    # day's close, and the pinned SPY series catches a series that stops
    # short of a day the market traded), but requiring the final calendar
    # day in the series cannot be the rule — a quarter can end on a market
    # holiday, and then no pull could satisfy it. So the window the number
    # was actually computed over is stated regardless: a reader who sees a Q2
    # figure computed through 06-29 can see the last session is missing.
    span = f" {observed[0]}..{observed[1]}" if observed else ""
    # Both market-calendar guards — the series stopping before the
    # quarter's last traded session, and a session missing from the middle
    # — read the pinned SPY series, so an unreadable pin removes both
    # silently, and RISK then reads exactly as it would have if they had
    # run and found nothing. RISK is not refused for that: the drawdown's
    # meaning comes from the account series, and coupling it to a benchmark
    # fetch would mean no network, no drawdown, ever. Saying which of the
    # two a reader is looking at costs one clause.
    #
    # An unreadable pin is not the only way to lose them. A pin that is
    # readable but stops short of the quarter's last session cannot rule
    # out a session after the account's last observation either — it agrees
    # with the very endpoint the check audits — so that case takes the same
    # clause (`_pin_reaches_quarter_end`).
    unchecked = "" if calendar_checked else ", not cross-checked against a trading calendar"
    lines.append(
        f"{_label('RISK')}Quarterly max drawdown {_fmt_risk(risk)}"
        f"   [TWR series{span}, peak seeded at quarter start{unchecked}]"
    )
    lines.append(
        f"{_label('CONTEXT')}Account TWR − SPY: {_fmt_context(context)}"
        f"     [account: MCP TWR series{span} · SPY: pinned CSV]"
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
    trades_envelopes = _envelopes_or_empty(archive_root, _TRADES_TOOL)
    inputs += _envelope_inputs(_TRADES_TOOL, trades_envelopes, bounds)
    trades_gap = coverage_gap(_TRADES_TOOL, trades_envelopes, bounds)
    fills: list[dict] | str = (
        trades_gap if trades_gap is not None else fills_from(trades_envelopes, bounds)
    )

    # --- orders (feeds only the D1-compiled-disabled orders_linked slot) --
    orders_envelopes = _envelopes_or_empty(archive_root, _ORDERS_TOOL)
    inputs += _envelope_inputs(_ORDERS_TOOL, orders_envelopes, bounds)
    orders_gap = coverage_gap(_ORDERS_TOOL, orders_envelopes, bounds)
    known_orders: set[tuple[str, str]] | str = (
        orders_gap if orders_gap is not None else known_orders_from(orders_envelopes)
    )

    # --- performance envelopes, read now so their names join the union of
    # "every archive envelope" before the journal — routing happens below.
    perf_envelopes = _envelopes_or_empty(archive_root, _PERFORMANCE_TOOL)
    inputs += _envelope_inputs(_PERFORMANCE_TOOL, perf_envelopes, bounds)

    # --- JOURNAL -------------------------------------------------------
    fold = fold_or_reason(journal_path)
    # Only if it EXISTS. `_read_events` returns [] for an absent file, so a
    # missing journal folds cleanly and used to be listed as an input that
    # was read — the one line of the INPUTS block asserting something
    # untrue, on a report whose whole premise is that a refusal must never
    # look like a value. `_envelope_inputs` goes the other way and names
    # even files that FAILED to parse, because those really were read.
    # `is_file()`, not `exists()`. A `--journal` that lands on a DIRECTORY
    # exists, is never read, and was listed here as an input that was —
    # the same untrue line the guard below was written to prevent, arriving
    # through a different door. A real file that failed to PARSE stays
    # listed, matching `_envelope_inputs`: it was read, and saying so beside
    # its refusal is the honest pairing.
    if Path(journal_path).is_file():
        inputs.append(Path(journal_path).name)
    elif not isinstance(fold, str):
        # And the count has to say so too. `_read_events` folds an absent
        # file to `[]`, which is right for `open`/`tag` (you cannot have
        # declared anything before the journal exists) and wrong here: it
        # turns "no journal was found" into the rendered figure `0 thesis
        # versions declared this quarter`, which this report's own contract
        # reserves for a count it actually made. A journal that EXISTS and
        # holds no in-quarter declaration is a real zero and still renders
        # as one. The distinction matters because the failure it hides is
        # silent and total: a journal skipped, misplaced by a `--journal`
        # typo, or lost with a machine reads exactly like a quarter in
        # which the user simply declared nothing.
        fold = f"unavailable (no journal at {Path(journal_path).as_posix()})"

    counts = count_quarter(fills, fold, bounds, known_orders)

    # --- RISK / CONTEXT: the performance money-path chain ----------------
    observed: tuple[str, str] | None = None
    # False the moment a figure is rendered without the trading-calendar
    # cross-check having been ESTABLISHED — not merely attempted. Set where
    # the pin is read, so it cannot drift from the condition the checks
    # themselves are under, and computed from what the pin COVERS rather
    # than from whether it parsed (`_pin_reaches_quarter_end`).
    calendar_checked = True
    perf_gap = coverage_gap(_PERFORMANCE_TOOL, perf_envelopes, bounds)
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
                if d0 is not None and d1 is not None:
                    observed = (d0, d1)
                freshness = gap_reason(series, d0, d1, bounds)
                if freshness is not None:
                    risk = freshness
                    context = freshness
                else:
                    risk = quarterly_drawdown(series, d0, d1)
                    account_twr = quarterly_twr(series, d0, d1)

                    # Initialised before the branch: when the pin itself
                    # refuses, `spy_series` is never assigned, and the
                    # traded-day check below read it anyway —
                    # `UnboundLocalError` out of `report`, no summary
                    # written, on the ordinary path where a benchmark fetch
                    # fails. Found by running the six documented steps end
                    # to end rather than by testing the branch that added
                    # the check.
                    spy_series: dict | str = "unavailable (SPY pin: not read)"
                    pin = ensure_pinned(reports_root, quarter, fetch_spy,
                                        required=(_spy_baseline_date(d0), d1))
                    if isinstance(pin, str):
                        spy: float | str = pin
                    else:
                        inputs.append(Path(pin).name)
                        spy_series = read_pinned(pin)
                        if isinstance(spy_series, str):
                            spy = spy_series
                        else:
                            spy = spy_return(spy_series, _spy_baseline(spy_series, d0), d1)
                    context = context_pp(account_twr, spy)

                    # The account series stopping short of the quarter's
                    # last TRADING day — the limit this file has carried as
                    # known since cycle 8, on the grounds that detecting it
                    # needs a market calendar. It does not: the pinned SPY
                    # series IS one. yfinance returns trading days, so a
                    # pinned close dated after `d1` and still inside the
                    # quarter proves the market traded on a day the account
                    # series does not cover — which is exactly the case a
                    # pull taken early on the quarter's final day produces,
                    # and it renders `0.0%` for a session that could have
                    # moved the book by half.
                    #
                    # Only ever ADDS a refusal, and only when the pin is
                    # readable. When it is not, the previous behaviour
                    # stands: the endpoints are printed and the operator can
                    # see the window stop short.
                    # Readable is not enough — see `_pin_reaches_quarter
                    # _end`. A pin that itself stops short of the quarter's
                    # last session cannot audit an account series that
                    # stops short in the same place, and saying it did is
                    # what turned an unexamined `0.0%` into a verified one.
                    # The two checks below still RUN on a short pin (they
                    # can only ever add a refusal, and one found on partial
                    # evidence is still true); only the CLAIM is withheld.
                    calendar_checked = (
                        not isinstance(spy_series, str)
                        and _pin_reaches_quarter_end(spy_series, bounds))
                    if not isinstance(spy_series, str):
                        # No try/except. `read_pinned` refuses the WHOLE pin
                        # on any date it cannot canonicalise, so every key
                        # here is a parseable `YYYY-MM-DD` by contract. The
                        # handler that used to sit here set `last_traded =
                        # None` — the fail-open direction, which would have
                        # skipped this refusal while the report still
                        # claimed the figure WAS cross-checked. Its sibling
                        # below lost the same dead guard two rounds earlier
                        # and this one was missed.
                        last_traded = max(
                            (k for k in spy_series
                             if d1 < k and _parse_calendar_date(k) < bounds[1].date()),
                            default=None)
                        if last_traded is not None:
                            short = (
                                f"unavailable (series stops short: the account's "
                                f"last observation in this quarter is {d1}, but "
                                f"the market traded on {last_traded} — the pull "
                                f"was taken before that session closed, and the "
                                f"quarter's own last move is not in the series)"
                            )
                            risk = short
                            context = short
                        else:
                            # The same market calendar, asked the other
                            # question. The check above asks whether the
                            # market traded AFTER the last observation;
                            # this one asks whether it traded BETWEEN two
                            # of them. A missing interior session makes the
                            # max drawdown unknowable — the trough could be
                            # exactly there — and the interior gap rule
                            # cannot see it, because Thursday to Saturday
                            # is two calendar days.
                            #
                            # Split by WHERE the day falls. Inside the
                            # quarter it corrupts only the path between the
                            # endpoints, so RISK alone blanks — TWR is a
                            # ratio of two endpoints of a cumulative series
                            # and a day between them cannot move it.
                            #
                            # BEFORE the quarter's first day it corrupts an
                            # endpoint itself, and blanks both. `d0` is the
                            # last observation strictly before the quarter;
                            # if the market traded between `d0` and the
                            # quarter and the account series lacks that day,
                            # `d0` is not the real baseline but an earlier
                            # one, and the return measured from it includes
                            # a move that happened in the PRIOR quarter. The
                            # first version of this check blanked RISK only,
                            # reasoning that endpoints were safe — the
                            # missing day can BE the endpoint.
                            #
                            # Measured before it was written: across the
                            # real archive's 164 observations, the account
                            # series was missing ZERO SPY trading days —
                            # it carries market holidays as flat points, a
                            # superset. This costs a real pull nothing.
                            #
                            # Its limit, stated because it is not obvious:
                            # a session missing from BOTH the account
                            # series and the pin is invisible here — the
                            # pin is the only calendar in the tool, so it
                            # cannot audit itself. That needs a second,
                            # independent calendar, which is a real
                            # dependency to take on for a case yfinance
                            # (which returns every trading day it has) is
                            # not known to produce. The check catches the
                            # asymmetric case, which is the one a pull
                            # taken mid-quarter actually creates. It
                            # applies to the quarter's FINAL session too —
                            # a pin that also stops on the 29th validates
                            # an account series that stops on the 29th —
                            # and the obvious dependency-free rule does not
                            # work: "the quarter's last weekday is always a
                            # trading day" is false, refuted by 2026Q1's
                            # own shape in reverse — 2018-03-31 was a
                            # Saturday and the Friday before it was Good
                            # Friday, with the market closed.
                            # No try/except around the comparison. Both
                            # key sets are `str` by their producers'
                            # contracts (`read_pinned` and
                            # `observations_to_series` both return
                            # `dict[str, float]`), and `d0`/`d1` cannot be
                            # None here — `gap_reason` refuses a missing
                            # endpoint before this branch is reached. A
                            # guard that cannot fire but WOULD render the
                            # number if it did is a fail-open default
                            # wearing a seatbelt.
                            skipped = sorted(
                                k for k in spy_series
                                if d0 < k < d1 and k not in series)
                            if skipped:
                                shown = ", ".join(skipped[:3])
                                more = ("" if len(skipped) <= 3
                                        else f" and {len(skipped) - 3} more")
                                one = len(skipped) == 1
                                before = [k for k in skipped
                                          if k < bounds[0].date().isoformat()]
                                if before:
                                    stale = (
                                        "unavailable (baseline not the "
                                        f"quarter's: the market traded on "
                                        f"{', '.join(before[:3])} between the "
                                        f"account's last prior observation "
                                        f"{d0} and this quarter, so {d0} is "
                                        "an earlier baseline than the "
                                        "quarter's own and the return "
                                        "measured from it includes a prior "
                                        "quarter's move)"
                                    )
                                    risk = stale
                                    context = stale
                                else:
                                    risk = (
                                        "unavailable (missing "
                                        f"{'session' if one else 'sessions'}: the "
                                        f"market traded on {shown}{more}, which the "
                                        "account series does not cover, so a trough "
                                        f"on {'that day' if one else 'those days'} "
                                        "would not appear in the drawdown)"
                                    )

    return render(quarter, counts, risk, context, inputs, observed,
                  calendar_checked), inputs
