"""Journal layer for the personal track-record capture tool.

`validate_event` validates one `trade-journal.jsonl` event dict against
the frozen schema (spec §4.2): one event in, `None` or `JournalError` out,
no I/O. `load_fold` / `append_event` (Task 4, below) own the whole-file
read/write and the fold constraints that need every line at once — a
`thesis_id` declared exactly once, every reference resolving, the
supersede relation acyclic, at most one successor, and the two prediction
rules. `append_event` validates the COMPLETE resulting fold before
writing a byte; a refusal never touches the file.

Design: docs/superpowers/plans/2026-08-16-track-record.md (Tasks 3-4).
Freeze decisions this module compiles against:
- D4 (instrument identity) resolved to "a thesis still carries a real
  `instrument.id`" — the stable `contract_id` exists on positions, it
  simply does not reach a trade row (that affects `fills_from`/`unlinked`
  in a later task, not this schema). `orders_linked` is a fully valid,
  unconditionally-enabled event type.
  **Amended by commit 2667d7f2** ("the instrument fallback is per-thesis,
  not per-freeze", found while writing Task 9's skill): the id's
  existence on positions rows is per-INSTRUMENT, not per-account — it is
  recoverable for one the account still holds and unrecoverable for one
  it has since closed. So `instrument.id` is NOT unconditionally
  required: `thesis_opened`/`thesis_superseded` accept EITHER a
  non-empty `instrument.id` OR the pair `symbol_at_write` +
  `instrument_unresolved: true`, never neither and never one without the
  other's marker. See `_validate_instrument` for the exact rule.
- D6 (prediction prompting) governs only whether the skill ASKS for a
  prediction, never the schema: `prediction` is optional-and-complete-
  when-present here regardless of D6, and `prediction_resolved` is always
  a valid event type.
Freeze file: docs/superpowers/plans/2026-08-16-track-record-freeze.md

"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_VALID_EVENTS = frozenset(
    {
        "thesis_opened",
        "thesis_superseded",
        "orders_linked",
        "thesis_retired",
        "prediction_resolved",
    }
)
_VALID_KINDS = frozenset({"long", "hedge"})
_VALID_REASONS = frozenset({"invalidated", "played_out", "abandoned"})
_VALID_RESULTS = frozenset({"hit", "miss", "unobservable"})

# Canonical UTC "Z" form only (spec §4.2: "recorded_at is writer-generated
# and normalized to UTC Z"). strptime's literal "Z" rejects both a naive
# date (no time component) and an explicit offset like "+00:00" — the
# right instant in the wrong, non-string-orderable spelling — without a
# separate regex pass: any mismatch (shape or out-of-range component,
# e.g. month 99) raises ValueError uniformly.
_RECORDED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_PREDICTION_FIELDS = ("proposition", "deadline", "probability_pct")

# Exactly the keys each event type may carry. Not a forward-compatibility
# door: spec §4.2 names `conviction`, `comparison_symbol` and
# `holding_horizon` as a CLOSED set of three "preserved and unused in v1"
# fields, so "unknown" here really does mean "nothing will ever read this".
#
# Why refuse rather than keep: a `prediction` misspelled `predicton`
# appended successfully and then held the user's falsifiable claim in a key
# no consumer reads — the fold does not see it, `prediction_resolved`
# refuses with "has no prediction to resolve", and the skill's resolve step
# never surfaces it. One mistyped letter, and a judgment that cannot be
# reconstructed later is discarded at the moment it is recorded. This is the
# same harm as a prediction riding on a non-declaring event, which is
# already refused.
#
# It also removes the hazard `_fold_from_events` documents below: a caller
# building one event via `dict(SUP, event=...)` used to inherit `SUP`'s
# `supersedes` silently, which is why the broader successor reading had to
# be reverted.
_COMMON_KEYS = frozenset({"event", "recorded_at", "thesis_id"})
_DECLARING_KEYS = _COMMON_KEYS | {
    "kind", "intent", "instrument", "supersedes",
    "prediction", "conviction", "comparison_symbol", "holding_horizon",
}
_ALLOWED_KEYS = {
    "thesis_opened": _DECLARING_KEYS,
    "thesis_superseded": _DECLARING_KEYS,
    "orders_linked": _COMMON_KEYS | {"order_refs"},
    "thesis_retired": _COMMON_KEYS | {"reason"},
    "prediction_resolved": _COMMON_KEYS | {"result"},
}



class JournalError(ValueError):
    """Raised by `validate_event` when an event dict violates the journal
    schema. Always names the offending field (`self.field`) so a caller —
    the tag skill relaying a refusal verbatim to the user — can say
    exactly what to fix without parsing the message text.
    """

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def _require_present(container: dict, field: str) -> None:
    if field not in container:
        raise JournalError(field, "required field is missing")


def _require_nonempty_str(container: dict, field: str, *, label: str | None = None) -> None:
    """Validates `container[field]` as a non-empty `str`. Caller must have
    already established the key is present. `bool` is excluded explicitly:
    it is a subtype of `int`, not of `str`, but a stray `True`/`False` is
    otherwise easy to let through silently.

    `.field` on the raised error is `label` when given, else the bare
    `field` — a dotted path for a nested caller (e.g.
    `label="prediction.proposition"`), a bare name for a top-level one.
    This keeps `.field` a consistent "the path you'd go edit" contract
    (review fix-round 1: it previously mixed bare and dotted forms across
    call sites with no single rule)."""
    text = label or field
    value = container[field]
    if isinstance(value, bool) or not isinstance(value, str):
        raise JournalError(text, f"{text} must be a string, got {type(value).__name__}")
    if value == "":
        raise JournalError(text, f"{text} must be non-empty")


def _validate_recorded_at(event: dict) -> None:
    # Required on every event type (spec §4.2: "recorded_at is required on
    # every event" — the fold assigns each event to the quarter containing
    # it, so an event with no timestamp cannot be counted).
    _require_present(event, "recorded_at")
    value = event["recorded_at"]
    if isinstance(value, bool) or not isinstance(value, str):
        raise JournalError("recorded_at", f"must be a string, got {type(value).__name__}")
    try:
        datetime.strptime(value, _RECORDED_AT_FORMAT)
    except ValueError as exc:
        raise JournalError(
            "recorded_at",
            "must be canonical UTC 'Z' form YYYY-MM-DDTHH:MM:SSZ "
            f"(naive dates and non-Z offsets are rejected), got {value!r}",
        ) from exc


def _validate_enum(event: dict, field: str, valid: frozenset) -> None:
    _require_present(event, field)
    value = event[field]
    # `isinstance` first: `value not in <frozenset>` raises a bare
    # `TypeError: unhashable type` on a list/dict, which breaks this
    # module's stated contract that every defect escapes as a
    # `JournalError` — and TypeError is outside `_cmd_tag`'s refusal arm,
    # so it reached the operator as a traceback instead of `REFUSED:`.
    if not isinstance(value, str) or value not in valid:
        raise JournalError(field, f"must be one of {sorted(valid)}, got {value!r}")


def _validate_instrument(event: dict) -> None:
    # D4 amendment (commit 2667d7f2, "the instrument fallback is per-thesis,
    # not per-freeze"): the original freeze note said instrument.id would be
    # unconditionally required because SOME stable id exists (contract_id,
    # on positions rows) -- but that existence is per-INSTRUMENT, not
    # per-account: an id is recoverable for an instrument the account still
    # holds and unrecoverable for one it has since closed (a closed
    # position no longer appears in get_account_positions). Capture must
    # not stop for that -- it is exactly where the trade's intent is most
    # worth recording and least reconstructable (spec §0: capture never
    # stops). So EITHER shape is accepted per event, never neither:
    #   - a non-empty instrument.id, or
    #   - symbol_at_write (non-empty str) + instrument_unresolved: true
    # A bare symbol_at_write with no marker, or instrument_unresolved with
    # no symbol_at_write, is refused: an unresolved instrument must SAY it
    # is unresolved, or a later reader cannot tell a missing id from an
    # unrecorded one. `.field` is "instrument" for a container-shaped
    # defect (missing / not a dict -- there is no leaf to name yet) and a
    # dotted nested leaf otherwise, matching `.field`'s dotted-path
    # contract (review fix-round 1). Nothing downstream reads
    # instrument.id (orders_linked is already permanently disabled by D1,
    # and thesis_versions counts declarations regardless), so this is a
    # pure schema widening -- every previously-valid event stays valid.
    _require_present(event, "instrument")
    instrument = event["instrument"]
    if not isinstance(instrument, dict):
        raise JournalError(
            "instrument", f"must be an object, got {type(instrument).__name__}"
        )

    inst_id = instrument.get("id")
    has_id = isinstance(inst_id, str) and not isinstance(inst_id, bool) and inst_id != ""

    symbol = instrument.get("symbol_at_write")
    has_symbol = isinstance(symbol, str) and not isinstance(symbol, bool) and symbol != ""
    marked_unresolved = instrument.get("instrument_unresolved") is True

    if has_id:
        # Returning here as soon as the id checked out left the OTHER two
        # fields entirely unvalidated, so a record could assert both "here
        # is a stable id" and "the instrument could not be resolved" at
        # once. Nothing downstream reads `instrument.id` today, so this
        # costs no number — but the record IS the product: this tool exists
        # because contemporaneous facts cannot be reconstructed later, and a
        # self-contradictory one is a fact a future reader cannot use.
        if marked_unresolved:
            raise JournalError(
                "instrument.instrument_unresolved",
                "instrument carries a non-empty instrument.id, so it is "
                "resolved -- instrument_unresolved must not be true",
            )
        if "symbol_at_write" in instrument and not has_symbol:
            raise JournalError(
                "instrument.symbol_at_write",
                "symbol_at_write is present but not a non-empty string, "
                f"got {symbol!r}",
            )
        return

    if has_symbol and marked_unresolved:
        return
    if has_symbol and not marked_unresolved:
        raise JournalError(
            "instrument.instrument_unresolved",
            "instrument carries symbol_at_write but no instrument.id -- "
            "instrument_unresolved must be true to record it without one",
        )
    if marked_unresolved and not has_symbol:
        raise JournalError(
            "instrument.symbol_at_write",
            "instrument.instrument_unresolved is true but symbol_at_write "
            "is missing or empty",
        )
    raise JournalError(
        "instrument.id",
        "instrument must carry a non-empty instrument.id, or "
        "symbol_at_write plus instrument_unresolved: true",
    )


def _validate_supersedes(event: dict, ev_type: str) -> None:
    # Required + non-empty str on thesis_superseded (it declares which
    # version it replaces); optional on thesis_opened (a fresh thesis
    # supersedes nothing — the fixture example carries an explicit
    # `"supersedes": null`) but validated as a non-empty str if given a
    # non-null value, per the general "thesis_id and supersedes are
    # non-empty str" rule.
    present = "supersedes" in event and event["supersedes"] is not None
    if ev_type == "thesis_superseded" and not present:
        raise JournalError("supersedes", "required field is missing")
    if not present:
        return
    value = event["supersedes"]
    if isinstance(value, bool) or not isinstance(value, str):
        raise JournalError("supersedes", f"must be a string, got {type(value).__name__}")
    if value == "":
        raise JournalError("supersedes", "must be non-empty")


def _validate_thesis_fields(event: dict, ev_type: str) -> None:
    _validate_enum(event, "kind", _VALID_KINDS)
    _require_present(event, "intent")
    _require_nonempty_str(event, "intent")
    _validate_instrument(event)
    _validate_supersedes(event, ev_type)


def _require_ref_field(ref: dict, idx: int, field: str) -> None:
    # `.field` carries the index (review fix-round 1): a hand-edited
    # journal is a flat jsonl file, and "order_refs[1].order_id" tells the
    # user which ref to fix without them having to count entries.
    path = f"order_refs[{idx}].{field}"
    if field not in ref:
        raise JournalError(path, f"{path} is required")
    value = ref[field]
    # known_orders_from (a later task) coerces the broker side of this
    # comparison to str; a numeric order_id/account here would fail
    # equality against that str silently, reporting orders_linked as
    # falsely unavailable rather than surfacing the typo. Reject rather
    # than coerce (brief item 7): a journal is hand-written, and a typed
    # id is worth surfacing now.
    if isinstance(value, bool) or not isinstance(value, str):
        raise JournalError(path, f"{path} must be a string, got {type(value).__name__}")
    if value == "":
        raise JournalError(path, f"{path} must be non-empty")


def _validate_order_refs(event: dict) -> None:
    _require_present(event, "order_refs")
    refs = event["order_refs"]
    if not isinstance(refs, list):
        raise JournalError("order_refs", f"must be a list, got {type(refs).__name__}")
    if not refs:
        raise JournalError("order_refs", "must be non-empty")
    for idx, ref in enumerate(refs):
        if not isinstance(ref, dict):
            raise JournalError(
                "order_refs", f"order_refs[{idx}] must be an object, got {type(ref).__name__}"
            )
        _require_ref_field(ref, idx, "account")
        _require_ref_field(ref, idx, "order_id")


def _validate_conviction(event: dict) -> None:
    # Optional on the two DECLARING events (`_ALLOWED_KEYS` refuses it elsewhere, so this range check is unreachable from the other three); when present, an int in 1-5. bool is
    # excluded explicitly — it is a subtype of int in Python, so
    # `conviction: true` would otherwise pass an `isinstance(x, int)` check.
    if "conviction" not in event:
        return
    value = event["conviction"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalError(
            "conviction", f"must be an int in 1-5, got {type(value).__name__}"
        )
    if not 1 <= value <= 5:
        raise JournalError("conviction", f"must be an int in 1-5, got {value}")


def _validate_deadline(pred: dict) -> None:
    value = pred["deadline"]
    if isinstance(value, bool) or not isinstance(value, str):
        raise JournalError(
            "prediction.deadline",
            f"prediction.deadline must be a string, got {type(value).__name__}",
        )
    # Unlike recorded_at, deadline is NOT pinned to the canonical "Z" form
    # (spec §4.2: "a valid instant with a timezone") — the fixture example
    # itself carries "-05:00". Python 3.10's fromisoformat does not accept
    # a trailing "Z" (that lands in 3.11), so normalize it the same way
    # archive.py's _parse_utc does, for 3.10 compatibility.
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise JournalError(
            "prediction.deadline",
            f"prediction.deadline must be a valid ISO-8601 instant, got {value!r}",
        ) from exc
    if dt.tzinfo is None:
        raise JournalError("prediction.deadline", "prediction.deadline must be a tz-aware instant")


def _validate_probability_pct(pred: dict) -> None:
    value = pred["probability_pct"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JournalError(
            "prediction.probability_pct",
            f"prediction.probability_pct must be a number, got {type(value).__name__}",
        )
    if not math.isfinite(value):
        raise JournalError(
            "prediction.probability_pct", "prediction.probability_pct must be finite"
        )
    if not 0 <= value <= 100:
        raise JournalError(
            "prediction.probability_pct",
            f"prediction.probability_pct must be within [0, 100], got {value}",
        )


_PREDICTION_BEARING_EVENTS = frozenset({"thesis_opened", "thesis_superseded"})


def _validate_prediction(event: dict) -> None:
    # Optional on a thesis, complete when present, unconditional on D6
    # (D6 decides only whether the skill prompts for one — never whether
    # the schema accepts one).
    #
    # Restricted to the two DECLARING events, because only a declaring
    # event's prediction is ever read again: `_fold_from_events` stores it
    # as `declared[tid]["prediction"]`, `prediction_resolved` looks for it
    # there, and the skill's resolve step scans declaring events. A
    # prediction written onto any other event used to be accepted, fold
    # cleanly, and then be permanently invisible — unresolvable,
    # unreportable, and unrecoverable except by hand-editing the journal.
    # For a tool whose whole purpose is preserving a judgment that cannot
    # be reconstructed later, silently accepting one into a slot nothing
    # reads is the worst available outcome; refuse it at write time, while
    # the user is still in the room to put it somewhere real.
    if "prediction" in event and event.get("event") not in _PREDICTION_BEARING_EVENTS:
        raise JournalError(
            "prediction",
            "a prediction can only ride on thesis_opened or "
            f"thesis_superseded (the events a fold reads it back from), "
            f"not on {event.get('event')!r}",
        )
    if "prediction" not in event:
        return
    pred = event["prediction"]
    if not isinstance(pred, dict):
        raise JournalError("prediction", f"must be an object, got {type(pred).__name__}")
    for field in _PREDICTION_FIELDS:
        if field not in pred:
            raise JournalError(f"prediction.{field}", f"prediction.{field} is required")
    _require_nonempty_str(pred, "proposition", label="prediction.proposition")
    _validate_deadline(pred)
    _validate_probability_pct(pred)


def validate_event(event: dict) -> None:
    """Validates one journal event dict against the frozen schema (spec
    §4.2). Raises `JournalError` naming the offending field on any
    violation; returns `None` (no exception) when the event is valid.

    Single-event validation only — the whole-file fold constraints
    (thesis_id declared exactly once, references resolve, supersede
    acyclic, at most one successor) are Task 4's `fold`, which calls this
    function per line first.
    """
    if not isinstance(event, dict):
        raise JournalError("event", f"must be an object, got {type(event).__name__}")

    ev_type = event.get("event")
    # `isinstance` first, for the same reason as `_validate_enum`: an
    # unhashable `event` value would raise TypeError here, before this
    # function could name the field.
    if not isinstance(ev_type, str) or ev_type not in _VALID_EVENTS:
        raise JournalError(
            "event", f"unknown event type {ev_type!r}: must be one of {sorted(_VALID_EVENTS)}"
        )

    # `prediction` is deliberately excluded here and left to
    # `_validate_prediction`, which refuses it on a non-declaring event with
    # a message that says WHY (only a declaring event's prediction is ever
    # read back) rather than the generic "no consumer reads this".
    unknown = sorted(set(event) - _ALLOWED_KEYS[ev_type] - {"prediction"})
    if unknown:
        raise JournalError(
            "event",
            f"{ev_type} carries key(s) no consumer reads: "
            f"{', '.join(unknown)} — check for a typo; nothing would ever "
            "read them back",
        )

    _validate_recorded_at(event)

    # thesis_id is required on every event type (spec §4.2 lists it first
    # in each event's required-field set).
    _require_present(event, "thesis_id")
    _require_nonempty_str(event, "thesis_id")

    if ev_type in ("thesis_opened", "thesis_superseded"):
        _validate_thesis_fields(event, ev_type)
    elif ev_type == "orders_linked":
        _validate_order_refs(event)
    elif ev_type == "thesis_retired":
        _validate_enum(event, "reason", _VALID_REASONS)
    elif ev_type == "prediction_resolved":
        _validate_enum(event, "result", _VALID_RESULTS)

    _validate_conviction(event)
    _validate_prediction(event)


# --- Task 4: fold, topology and prediction rules (spec §4.2) ---


class FoldError(JournalError):
    """Raised by `load_fold` / `append_event` when the WHOLE-FILE topology
    is violated: a `thesis_id` declared more than once, a `thesis_id` or
    `supersedes` that resolves to nothing, a supersede cycle, a version
    with two successors, a `prediction_resolved` on a thesis with no
    `prediction`, or a second effective resolution of one prediction.

    Subclass of `JournalError` — same two-arg (`field`, `message`)
    contract, so a caller catching `JournalError` still catches this. A
    plain per-event `JournalError` surfaced while building a fold (a
    malformed field, not a topology defect) is re-wrapped as a
    `FoldError` too (see `_read_events`), so both defect classes escape
    this module uniformly — a later `fold_or_reason` adapter only has to
    catch one type.
    """


@dataclass(frozen=True)
class Fold:
    """The whole-file fold of a journal.

    `events` — every event dict, in file order.
    `declared` — `thesis_id -> declaring event dict` (the `thesis_opened`
    or `thesis_superseded` that declared it).
    `successor` — `superseded thesis_id -> successor thesis_id`, for every
    `thesis_id` that has been superseded.

    Built only by `load_fold` / `append_event` (via `_fold_from_events`),
    which raise `FoldError` rather than construct a `Fold` violating the
    topology constraints — an instance reaching a caller is always
    internally consistent. A `Fold` is a plain frozen dataclass otherwise
    (no checked constructor), so callers (later tasks' fixtures) may build
    one directly, e.g. `Fold([], {}, {})` for an empty fold.
    """

    events: list[dict]
    declared: dict[str, dict]
    successor: dict[str, str]


def _read_events(path) -> list[dict]:
    """Parses every non-blank line of `path` as JSON and schema-validates
    it via `validate_event`. An absent file loads as `[]` — an absent
    journal is an empty fold, the normal first run, not an error.

    Schema-only: this does NOT check cross-event topology (a `thesis_id`
    resolving, at-most-one-successor, ...). That is `_fold_from_events`,
    always run once over the COMPLETE event list — `load_fold` calls it
    over everything on disk, `append_event` calls it over the existing
    lines PLUS the candidate new one, together, never over a prefix on its
    own. A `supersedes` may legally point to a `thesis_id` declared LATER
    in the same file (constraint 3's cycle case proves it: the error must
    say `cycle`, not `resolve` — resolving references in file order and
    rejecting the first forward pointer would reject that case as
    dangling before the rest of the file is ever seen), so validating an
    on-disk prefix's topology in isolation would be wrong even as an
    intermediate step.

    A per-event `JournalError` (a malformed field on an otherwise
    topologically fine line) is re-raised as a `FoldError`, carrying the
    original `.field` and prefixed with the 1-based line number — a
    hand-written journal can mix both defect classes on the same run, and
    a caller must not need to tell them apart to relay a refusal.
    """
    path = Path(path)
    if not path.exists():
        return []
    events: list[dict] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FoldError("event", f"line {line_no}: invalid JSON: {exc}") from exc
        try:
            validate_event(event)
        except JournalError as exc:
            raise FoldError(exc.field, f"line {line_no}: {exc.message}") from exc
        events.append(event)
    return events


def _check_acyclic(successor: dict) -> None:
    """Raises `FoldError` naming `cycle` if the supersede relation
    (`superseded thesis_id -> successor thesis_id`) contains one.

    By the time this runs, `_fold_from_events` has already enforced
    at-most-one-successor while building `successor`, so every node has at
    most one outgoing edge and the graph is a union of chains and cycles.
    A standard 3-color walk from every unvisited node finds any cycle in
    one pass over each edge: a GRAY node reached again while still on the
    CURRENT walk is the cycle, regardless of which line declared it first
    — file order plays no role, which is the point (see `_read_events`).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    for start in successor:
        if color.get(start, WHITE) != WHITE:
            continue
        path_ids: list[str] = []
        node = start
        while True:
            color[node] = GRAY
            path_ids.append(node)
            nxt = successor.get(node)
            if nxt is None or color.get(nxt) == BLACK:
                break
            if color.get(nxt) == GRAY:
                cycle_ids = path_ids[path_ids.index(nxt):] + [nxt]
                raise FoldError(
                    "thesis_id",
                    "supersede cycle detected: " + " -> ".join(cycle_ids),
                )
            node = nxt
        for n in path_ids:
            color[n] = BLACK


def _fold_from_events(events: list[dict]) -> Fold:
    """The topology + prediction pass (spec §4.2 fold constraints 1-6),
    over a COMPLETE event list — never a prefix (see `_read_events`).

    Two passes, not one: pass 1 declares every `thesis_id` (constraint 1 —
    duplicate check), independent of file order. Pass 2 then resolves
    every `thesis_id` reference, and every DECLARING event's `supersedes`,
    against the COMPLETE declared set built by pass 1 (constraint 2),
    tracks at-most-one-successor as it goes (constraint 4), and checks the
    two prediction rules (constraints 5-6). A single combined pass would
    reject a legal forward `supersedes` reference as dangling before ever
    reaching the line that declares it. Acyclicity (constraint 3) is
    checked last, once the full `successor` map exists.

    `supersedes` is resolved only on `thesis_opened`/`thesis_superseded`
    events — the only two the schema defines it on (spec §4.2: it is
    `null` on a fresh `thesis_opened` and required on `thesis_superseded`;
    no other event type carries it). A broader "any event with a non-null
    `supersedes`" reading was tried and reverted: `validate_event` does not
    reject a stray extra key on `orders_linked`/`thesis_retired`/
    `prediction_resolved`, and a future caller building one of those via
    `dict(SUP, event=..., ...)` — the idiom this test file itself uses
    throughout — could inherit `SUP`'s `supersedes` by accident. Reading it
    broadly would then register a phantom edge into `fold.successor`,
    which Task 5 reads as tracking version succession specifically, and
    could collide with a real successor and wrongly refuse a legitimate
    append. The narrow reading can never falsely reject; the broad one
    could — so narrow wins.
    """
    declared: dict[str, dict] = {}
    for event in events:
        if event["event"] in ("thesis_opened", "thesis_superseded"):
            tid = event["thesis_id"]
            if tid in declared:
                raise FoldError("thesis_id", f"thesis_id {tid!r} is already declared")
            declared[tid] = event

    successor: dict[str, str] = {}
    # Two more whole-file facts, both "the record must not say two
    # contradictory things about the same subject". Neither changes a
    # reported number — `orders_linked` is permanently disabled by D1 and
    # `thesis_versions` counts declarations — but this file IS the product:
    # a decade from now it is the only evidence of what the user thought,
    # and a history that answers "which decision was this order part of"
    # twice has not answered it.
    order_owner: dict[tuple, str] = {}     # (account, order_id) -> thesis_id
    retired_by: dict[str, str] = {}        # thesis_id -> the reason it retired with
    resolved = set()
    for event in events:
        tid = event["thesis_id"]
        if tid not in declared:
            raise FoldError(
                "thesis_id", f"thesis_id {tid!r} does not resolve to a declaration"
            )

        if event["event"] in ("thesis_opened", "thesis_superseded"):
            sup = event.get("supersedes")
            if sup is not None:
                if sup not in declared:
                    raise FoldError(
                        "supersedes",
                        f"supersedes {sup!r} does not resolve to a declaration",
                    )
                if sup in successor:
                    raise FoldError(
                        "supersedes",
                        f"thesis_id {sup!r} already has a successor "
                        f"{successor[sup]!r}; cannot also have successor {tid!r}",
                    )
                successor[sup] = tid

        if event["event"] == "orders_linked":
            for ref in event["order_refs"]:
                key = (ref["account"], ref["order_id"])
                if order_owner.get(key) == tid:
                    # Same event (or the same thesis twice). The cross-thesis
                    # message below would name t-1 on BOTH sides and send the
                    # operator hunting for a second event that does not
                    # exist — on a file SKILL.md has them hand-editing.
                    raise FoldError(
                        "order_refs",
                        f"order {key[1]!r} (account {key[0]!r}) is named more "
                        f"than once for thesis {tid!r} — remove the duplicate",
                    )
                if key in order_owner:
                    raise FoldError(
                        "order_refs",
                        f"order {key[1]!r} (account {key[0]!r}) is already "
                        f"linked to thesis {order_owner[key]!r}; it cannot "
                        f"also be linked to {tid!r}",
                    )
                order_owner[key] = tid

        if event["event"] == "thesis_retired":
            if tid in retired_by:
                raise FoldError(
                    "thesis_id",
                    f"thesis_id {tid!r} is already retired "
                    f"({retired_by[tid]!r}); a second retirement would leave "
                    f"two reasons with nothing to say which is true",
                )
            retired_by[tid] = event["reason"]

        if event["event"] == "prediction_resolved":
            if not declared[tid].get("prediction"):
                raise FoldError(
                    "thesis_id", f"thesis_id {tid!r} has no prediction to resolve"
                )
            if tid in resolved:
                raise FoldError(
                    "thesis_id",
                    f"thesis_id {tid!r} prediction already has a resolution: "
                    "a prediction may be resolved at most once",
                )
            resolved.add(tid)

    _check_acyclic(successor)

    return Fold(events=events, declared=declared, successor=successor)


def load_fold(path) -> Fold:
    """Loads and fold-validates a whole `trade-journal.jsonl`.

    An absent file loads as an empty `Fold([], {}, {})` — reporting on a
    quarter before anything has been tagged is the normal first run, not
    an error. Raises `FoldError` on any per-event schema violation or any
    of the six fold constraints (spec §4.2); never returns a `Fold` that
    violates them.
    """
    return _fold_from_events(_read_events(path))


def open_theses(fold: Fold) -> dict[str, dict]:
    """Every declared `thesis_id` that is neither superseded nor retired, as
    `{thesis_id: declaring event dict}` — the set a user picks from when
    asked whether a new tag group supersedes something already open.

    Superseded is already tracked on `fold.successor` (a `thesis_id` with an
    outgoing edge has a successor and is no longer open). Retired is not a
    fold-topology constraint (`_fold_from_events` places no constraint on
    `thesis_retired` beyond `thesis_id` resolving), so it is re-derived once
    here from `fold.events` rather than added to `Fold` itself — this is a
    read-side query over an already-validated fold, not a new invariant the
    fold must enforce.

    Pure function over an already-built `Fold` — no I/O, so a caller with a
    `Fold` in hand (from `load_fold` or the `fold_or_reason` adapter) never
    needs to re-read the file. This is the one implementation: the CLI's
    `open` subcommand and the skill both call it rather than re-deriving
    "declared minus superseded minus retired" a second time
    (`.claude/rules/producer-consumer.md` #3).
    """
    retired = {e["thesis_id"] for e in fold.events if e["event"] == "thesis_retired"}
    return {
        tid: event
        for tid, event in fold.declared.items()
        if tid not in fold.successor and tid not in retired
    }


def append_event(path, event: dict) -> None:
    """Validates `event` against the schema (`validate_event`) AND the
    COMPLETE resulting fold — the file's existing events plus this one,
    together — before writing anything. A VALIDATION refusal leaves the
    file byte-for-byte unchanged: both validation passes run entirely in
    memory, and the file is opened for writing only after they return
    without raising.

    That qualifier is load-bearing. An `OSError` raised by the append
    ITSELF (ENOSPC, EIO) can leave a partial line, and a caller catching
    `OSError` cannot tell the two apart — so the write-phase failure is
    re-raised below with a message that names itself. "A refusal leaves
    the file unchanged", said flatly, would be a promise this function
    cannot keep.

    Only the new event is appended in `"a"` mode — existing lines are
    never re-read into memory and rewritten from known keys, so a
    preserved-but-unused field on an old line survives untouched.

    One byte of the existing file IS inspected first: its last. Appending
    `json.dumps(event) + "\\n"` to a file that does not already end in a
    newline puts two JSON objects on ONE line (`}{`), which makes every
    later `load_fold` fail with `Extra data` — taking the PRIOR event down
    along with this one, after this call has already reported success. A
    file with no trailing newline is exactly what hand-editing produces,
    and SKILL.md's correction step instructs the operator to hand-edit this
    file, so it is the documented workflow rather than a corner case.
    Reading one byte is not the re-read the paragraph above rules out: no
    existing line is parsed, and none is rewritten.
    """
    path = Path(path)
    prior_events = _read_events(path)

    try:
        validate_event(event)
    except JournalError as exc:
        raise FoldError(exc.field, exc.message) from exc

    _fold_from_events(prior_events + [event])

    path.parent.mkdir(parents=True, exist_ok=True)
    separator = ""
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as f:      # binary: the last BYTE, not the last character
            f.seek(-1, 2)                # whence=2 -> relative to end of file
            if f.read(1) != b"\n":
                separator = "\n"
    # `ensure_ascii=False`, matching `archive.canonical_bytes`: `intent` and
    # `proposition` are the user's own prose, written in zh-CN by this
    # operator. Escaped to \uXXXX they are unreadable in the one file
    # SKILL.md tells them to hand-edit — and this file IS the product, the
    # decade-old record of what they thought. Two writers in one package
    # must not hold two encodings.
    line = separator + json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
    # The APPEND itself, alone in its own arm. Everything above this point is
    # read-and-validate, and a failure there really does leave the file
    # byte-for-byte unchanged, as this docstring promises. A failure HERE
    # (ENOSPC, EIO, a quota hit mid-line) does not: the file can keep a
    # partial line, and the next `load_fold` then refuses the whole journal.
    # Both raise `OSError`, so a caller catching that type cannot tell them
    # apart — and `__main__._cmd_tag` catches exactly that, printing one
    # `REFUSED:` line for either. Without this re-raise, a silently corrupted
    # append-only journal is reported to the operator in the identical words
    # as a clean, nothing-happened validation refusal. The message is the
    # distinction; the corruption itself is not preventable here without
    # rewriting existing lines, which this function deliberately never does.
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        raise OSError(
            f"WRITE FAILED after validation passed: {exc}. "
            f"{path} may now hold a PARTIAL line — this is not a clean "
            "refusal: check the last line of the file before appending again."
        ) from exc
