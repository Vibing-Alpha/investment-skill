"""Journal layer for the personal track-record capture tool.

`validate_event` validates one `trade-journal.jsonl` event dict against
the frozen schema (spec §4.2): one event in, `None` or `JournalError` out,
no I/O. `load_fold` / `append_event` (Task 4, below) own the whole-file
read/write and the fold constraints that need every line at once — a
`thesis_id` declared exactly once, every reference resolving, the
supersede relation acyclic, at most one successor, and the two prediction
rules. `append_event` validates the COMPLETE resulting fold before
writing a byte; a refusal never touches the file.

See README.md in this package.
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
Freeze file: .claude/skills/track-record/references/ibkr-freeze.md

"""

from __future__ import annotations

import json
import math
import os
import re
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
_IBKR_ID_RE = re.compile(r"ibkr:[0-9]+")
_INSTRUMENT_KEYS = frozenset({"id", "symbol_at_write", "instrument_unresolved",
                              "company_name_at_trade"})
_ORDER_REF_KEYS = frozenset({"account", "order_id"})
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
    if value.strip() == "":
        # `.strip()`, not `== ""`. A thesis whose `intent` is three spaces
        # validated and then printed as `blank-intent -` from `open` — a
        # record of a decision with the decision missing, in the one file
        # that exists because this fact cannot be reconstructed later. The
        # same validator guards prediction propositions and the identifiers.
        raise JournalError(
            text, f"{text} must be non-empty" if value == ""
            else f"{text} must not be only whitespace, got {value!r}")


def _validate_recorded_at(event: dict) -> None:
    # Required on every event type (spec §4.2: "recorded_at is required on
    # every event" — the fold assigns each event to the quarter containing
    # it, so an event with no timestamp cannot be counted).
    _require_present(event, "recorded_at")
    value = event["recorded_at"]
    if isinstance(value, bool) or not isinstance(value, str):
        raise JournalError("recorded_at", f"must be a string, got {type(value).__name__}")
    try:
        parsed = datetime.strptime(value, _RECORDED_AT_FORMAT)
        if parsed.strftime(_RECORDED_AT_FORMAT) != value:
            # `strptime` accepts `2026-8-9T1:2:3Z` for this format, so the
            # check promised a canonical shape and delivered a parse. Fixed
            # for the archive's `pulled_at` four cycles ago and not here —
            # the same validator, in the sibling file, left as it was.
            raise ValueError("not zero-padded")
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


def _validate_supersedes_placement(event: dict) -> None:
    """`supersedes` belongs to `thesis_superseded`, and to nothing else.

    Both declaring events share a key set, so a `thesis_opened` carrying
    `supersedes` validated cleanly and then CLOSED the named thesis in the
    fold — `open` stopped showing it — while the journal recorded the
    successor as merely opened. One mistyped event type, and a thesis is
    retired by a record that does not say it retired anything.
    """
    if event.get("event") == "thesis_opened" and event.get("supersedes") is not None:
        raise JournalError(
            "supersedes",
            "thesis_opened cannot supersede — a thesis that replaces another "
            "is a thesis_superseded event; leave supersedes out, or change "
            "the event type",
        )


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
    if "id" in instrument and inst_id is None:
        # `.get("id")` cannot tell an absent key from an explicit `null`,
        # so a record could claim an id of nothing and keep the key. Same
        # rule as the empty string beside it: absent means the key is not
        # there.
        raise JournalError(
            "instrument",
            "instrument.id is null — leave the key out to record an "
            "unresolved instrument",
        )
    # An EXPLICITLY empty id. `has_id` reads it as absent when choosing the
    # branch, and the record keeps it anyway — a claim of an id of nothing,
    # preserved in a file nothing rewrites. Absent means absent: leave the
    # key out.
    if isinstance(inst_id, str) and inst_id.strip() == "":
        raise JournalError(
            "instrument",
            "instrument.id is empty — leave the key out to record an "
            "unresolved instrument, rather than claiming an id of nothing",
        )
    # A non-string `id` used to read as NO id, so `{"id": 123,
    # "instrument_unresolved": true}` passed the unresolved branch while
    # keeping the bogus value — a record claiming the instrument could not
    # be identified with an identifier sitting right beside the claim, in an
    # append-only file. Present means present, whatever its type.
    if inst_id is not None and (isinstance(inst_id, bool)
                                or not isinstance(inst_id, str)):
        raise JournalError(
            "instrument",
            f"instrument.id is {type(inst_id).__name__}, not a string — "
            "leave the key out entirely to record an unresolved instrument",
        )
    # The frozen shape, not merely "a non-empty string". A resolved id is
    # `ibkr:<contract_id>` looked up from a positions pull, and this journal
    # is append-only: `{"id": "not-an-ibkr-id"}` was accepted, reported
    # success, and left an invented identity in the one file whose whole
    # purpose is holding facts nothing else can reconstruct. Every id in the
    # live journal and in the pinned corpus already has this shape.
    if (isinstance(inst_id, str) and not isinstance(inst_id, bool)
            and inst_id != "" and not _IBKR_ID_RE.fullmatch(inst_id)):
        raise JournalError(
            "instrument",
            f"instrument.id {inst_id!r} is not the frozen form "
            "'ibkr:<contract_id>' — a resolved id is read from a positions "
            "pull, never composed; leave it out and set "
            "instrument_unresolved instead",
        )
    has_id = isinstance(inst_id, str) and not isinstance(inst_id, bool) and inst_id != ""

    # A CLOSED key set, for the same reason the top level has one: a
    # `symbol_at_wirte` appended cleanly, and the symbol it was meant to
    # record was gone — into a key nothing reads, in a file nothing
    # rewrites. Top-level keys were already closed; this nested object was
    # the one place a typo still cost the fact silently.
    unknown = sorted(set(instrument) - _INSTRUMENT_KEYS)
    if unknown:
        raise JournalError(
            "instrument",
            f"unknown key(s) {', '.join(unknown)} — instrument holds only "
            f"{', '.join(sorted(_INSTRUMENT_KEYS))}; a misspelling here "
            "loses the fact it was meant to record",
        )

    symbol = instrument.get("symbol_at_write")
    # `.strip()`, like every other non-empty check here. `{"symbol_at_write":
    # "   ", "instrument_unresolved": true}` validated, leaving a record
    # with neither a stable id nor a symbol — an instrument that cannot be
    # identified at all, in an append-only file.
    has_symbol = (isinstance(symbol, str) and not isinstance(symbol, bool)
                  and symbol.strip() != "")
    # A BOOL, when present. Testing only `is True` made every other value
    # — `"yes"`, `1`, `[]` — behave exactly like `false`, so a malformed
    # marker entered the append-only journal and every later read accepted
    # it. The one field whose whole job is to say whether the identity is
    # trustworthy cannot itself be untrustworthy.
    if ("instrument_unresolved" in instrument
            and not isinstance(instrument["instrument_unresolved"], bool)):
        raise JournalError(
            "instrument.instrument_unresolved",
            f"must be true or false, got "
            f"{type(instrument['instrument_unresolved']).__name__}",
        )
    marked_unresolved = instrument.get("instrument_unresolved") is True

    # NOT required here, though the skill always writes it. `{"id": ...}`
    # alone is pinned legal by `test_the_shapes_that_were_always_legal_stay_
    # legal`, and that pin is load-bearing in a way tightening would break:
    # `load_fold` re-validates EVERY event on every read, so narrowing the
    # schema does not just refuse new events — it makes an existing journal
    # unreadable, and this file is the one thing here that cannot be
    # re-fetched. A reviewer read the skill's resolved-instrument contract
    # as a schema requirement; it is a WRITER contract, and the writer
    # honours it. Reverted after the corpus said so.
    raw_name = instrument.get("company_name_at_trade")
    if "company_name_at_trade" in instrument and (
            not isinstance(raw_name, str) or isinstance(raw_name, bool)
            or raw_name.strip() == ""):
        # Present but unusable. Without this, a blank or wrong-typed name
        # rides along silently whenever another identity shape is also
        # present — and the record IS the product.
        raise JournalError(
            "instrument.company_name_at_trade",
            "company_name_at_trade is present but not a non-empty string, "
            f"got {raw_name!r}",
        )
    has_name = isinstance(raw_name, str) and raw_name.strip() != ""

    if marked_unresolved and (has_id or has_name):
        raise JournalError(
            "instrument.instrument_unresolved",
            "instrument carries an id or company_name_at_trade, so it is "
            "identified -- instrument_unresolved must not be true",
        )

    if has_id:
        if "symbol_at_write" in instrument and not has_symbol:
            raise JournalError(
                "instrument.symbol_at_write",
                "symbol_at_write is present but not a non-empty string, "
                f"got {symbol!r}",
            )
        return

    if has_name:
        if not has_symbol:
            raise JournalError(
                "instrument.symbol_at_write",
                "company_name_at_trade requires a non-empty symbol_at_write",
            )
        return

    if has_symbol and marked_unresolved:
        return
    if has_symbol:
        raise JournalError(
            "instrument.instrument_unresolved",
            "instrument carries symbol_at_write but no instrument.id or "
            "company_name_at_trade -- instrument_unresolved must be true to "
            "record it without one",
        )
    if marked_unresolved:
        raise JournalError(
            "instrument.symbol_at_write",
            "instrument_unresolved is true but symbol_at_write is missing "
            "or empty",
        )
    raise JournalError(
        "instrument.id",
        "instrument must carry a non-empty instrument.id, symbol_at_write "
        "plus company_name_at_trade, or symbol_at_write plus "
        "instrument_unresolved: true",
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
    if value.strip() == "":
        # `.strip()`, like `_require_nonempty_str`. A `supersedes` of three
        # spaces would name a thesis that cannot exist, and the fold would
        # then refuse the whole journal for a dangling reference nobody can
        # see in the file.
        raise JournalError("supersedes", "must not be empty or only whitespace")


def _validate_thesis_fields(event: dict, ev_type: str) -> None:
    _validate_enum(event, "kind", _VALID_KINDS)
    _require_present(event, "intent")
    _require_nonempty_str(event, "intent")
    _validate_supersedes_placement(event)
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
    if value.strip() == "":
        # `.strip()`, for the same reason `order_id` gained it on the
        # summary side: `unlinked` treats an empty order id as no order at
        # all, so a link pointing at a whitespace one can never match
        # anything and never comes off the backlog. Found by enumerating
        # every non-empty check in the package after two of them turned out
        # to disagree.
        raise JournalError(path, f"{path} must not be empty or only whitespace")
    if value != value.strip():
        # Judged stripped, USED raw — the third site of one pattern. The
        # emptiness gate above reads `value.strip()`, but the ref is then
        # matched verbatim against the broker side, where `known_orders_from`
        # and `fills_from` now both store the stripped form. `" 10 "` would
        # pass this validation, look right in the file, and match nothing:
        # the order stays on the backlog forever with no reason given.
        # Refused rather than coerced, per this function's own rule two
        # branches up — a journal is hand-written, and surfacing a typo the
        # operator can fix beats silently repairing it under them.
        raise JournalError(
            path,
            f"{path} must not have leading or trailing whitespace "
            f"({value!r}) — it is matched verbatim against the broker's own "
            f"id, so a padded one can never link")


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
        unknown = sorted(set(ref) - _ORDER_REF_KEYS)
        if unknown:
            # Closed, like every other schema here. `{"account": "U1",
            # "order_id": "1", "orderid": "999"}` recorded BOTH identifiers
            # permanently while consumers read only `order_id` — so the
            # misspelling survived as a fact nothing acts on, and the order
            # it meant to link stayed on the backlog.
            raise JournalError(
                f"order_refs[{idx}]",
                f"unknown key(s) {', '.join(unknown)} — an order ref holds "
                f"only {', '.join(sorted(_ORDER_REF_KEYS))}",
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


def _is_finite_number(value) -> bool:
    """`math.isfinite`, but a JSON number is a Python int of unbounded width
    and `math.isfinite(10**400)` raises `OverflowError` rather than
    answering. That escaped `tag` as a traceback with no `REFUSED:` line —
    the one output shape SKILL.md's stderr protocol gives the agent no way
    to classify. Too large to be a float is not finite.
    """
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _validate_probability_pct(pred: dict) -> None:
    value = pred["probability_pct"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JournalError(
            "prediction.probability_pct",
            f"prediction.probability_pct must be a number, got {type(value).__name__}",
        )
    if not _is_finite_number(value):
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
    unknown = sorted(set(pred) - set(_PREDICTION_FIELDS))
    if unknown:
        # Closed, like the event and instrument levels. A `probability: 90`
        # sitting beside `probability_pct: 30` validated and was written —
        # two contradictory probabilities in an append-only record, one of
        # which no consumer reads. A prediction exists to be falsifiable
        # later; two of them are not.
        raise JournalError(
            "prediction",
            f"unknown key(s) {', '.join(unknown)} — a prediction holds only "
            f"{', '.join(_PREDICTION_FIELDS)}",
        )
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
    # An identifier, not prose. A `thesis_id` carrying a newline appended
    # cleanly and then printed as TWO lines from `open`, whose contract is
    # one line per thesis — so the agent parsing that output saw a thesis
    # that does not exist, and the real one under a truncated id. `open`
    # collapses whitespace in `intent` because intent IS prose the user may
    # paste; an id has no such excuse, and refusing here keeps the bad value
    # out of an append-only file rather than papering over it at every
    # reader.
    if " - " in event["thesis_id"]:
        # ` - ` is `open`'s field separator, and a `thesis_id` containing it
        # made two different theses print the identical line: `a` + `b - c`
        # and `a - b` + `c` both render as `a - b - c`. The agent parses
        # that output to decide what to supersede, so the ambiguity chooses
        # a thesis for it. `intent` may still contain the separator — it is
        # prose, and the reader splits on the FIRST one.
        raise JournalError(
            "thesis_id",
            f"thesis_id {event['thesis_id']!r} contains ' - ', which "
            "separates the id from the intent in `open` output — two "
            "different theses would print the same line",
        )
    if any(c in event["thesis_id"] for c in "\r\n\t"):
        raise JournalError(
            "thesis_id",
            f"thesis_id {event['thesis_id']!r} contains a line break or tab: "
            "it is an identifier and is printed one per line, so a newline "
            "in it would render as two theses",
        )

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
    # `split("\n")`, never `splitlines()`. JSONL's record separator is the
    # newline and nothing else, but `splitlines()` also breaks on \v, \f,
    # \x1c-\x1e, \x85 and — the one that actually happens — U+2028 and
    # U+2029. Those are legal inside a JSON string and `json.dumps` with
    # `ensure_ascii=False` writes them literally, so an intent pasted from a
    # web page could be ACCEPTED by `tag` and then split one stored event
    # into two unparseable halves, making the whole journal unreadable from
    # the next command onward. The writer is right to keep the text
    # readable; the reader was wrong about where a record ends.
    raw_lines = text.split("\n")
    # The index of the last line carrying anything. A truncated JSON object
    # THERE, with every earlier line valid, is the signature of an
    # interrupted append: `append_event`'s recovery undoes a raised
    # `OSError`, but a SIGKILL or power loss during the write runs no
    # handler at all, and the partial line then makes every earlier thesis,
    # prediction and link unreadable.
    #
    # The write path is deliberately not what changes: staging the whole
    # journal and republishing it would make each append rewrite every
    # existing line, trading a failure that damages the last line for one
    # that can damage all of them. The refusal is what changes — this case
    # has a ten-second fix, and saying so is the difference between a
    # recoverable file and one the operator believes is destroyed.
    last_content_line = max(
        (i for i, l in enumerate(raw_lines, start=1) if l.strip()), default=0)

    for line_no, line in enumerate(raw_lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line, object_pairs_hook=_no_duplicate_keys)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            if line_no == last_content_line and not line.endswith("}"):
                raise FoldError(
                    "event",
                    f"line {line_no}: invalid JSON: {exc}. This is the last "
                    f"line and it does not end in '}}', which is what an "
                    f"interrupted write leaves behind — a crash or power "
                    f"loss part-way through appending an event. Every "
                    f"earlier line read fine. Delete this last line and the "
                    f"journal is whole again; the event it was recording "
                    f"was never completed, so re-record it.",
                ) from exc
            # `ValueError` covers `_no_duplicate_keys`, which the hook added
            # last cycle raises — and which nothing caught, so `open` and
            # `unlinked` tracebacked and `report` wrote no summary at all.
            # A refusal is what the whole surrounding path is built to
            # produce; the check was right and its landing was missing.
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


# `_fsync_dir` and `_no_duplicate_keys` are imported from `archive`, not
# copied. They were copied, and the copies had already diverged — archive
# gained a parent-chain walk this one never got — which is the failure
# `.claude/rules/producer-consumer.md` rule 3 names. One implementation.
from scripts.track_record.archive import (  # noqa: E402
    _fsync_dir, _no_duplicate_keys,
)


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
    # The exact file the fold below is validated against. Two `tag` calls
    # racing on one journal each read this same prior state, each validate
    # `prior + candidate` against it, and each append: both exit 0, and
    # every later read then refuses the WHOLE journal — two declarations of
    # one `thesis_id`, two links on one order, two resolutions of one
    # prediction. The operator is told twice that the record was written
    # and can then read none of it.
    #
    # Not a lock, and that distinction is the whole reconciliation with the
    # review record, which rejected journal locking FOUR times on the
    # grounds that this is a single-operator tool and no two runs tag one
    # journal — with the restart condition "if that ever stops being true,
    # this is the first thing to build". No second concurrent writer has
    # been observed, so that condition is still unmet and the rejection of
    # LOCKING stands: a lock file trades this failure for a stale-lock one
    # that needs manual clearing, and an atomic temp-file republish would
    # rewrite every existing line on every append.
    #
    # What is built here is neither. Three lines comparing a size, adding
    # no state to clear, no file to leave behind, and no rewrite of
    # anything already recorded — and on the losing side it refuses having
    # written nothing, which is the outcome the tool produces for every
    # other unestablished input. It is admitted on cost, not on scenario:
    # the prior rejections were about machinery whose own failure modes
    # rivalled the one it prevented, and this has none. Checking, immediately before the write, that the file is
    # still the one the fold saw closes the demonstrated sequence, and the
    # loser refuses having written nothing — the correct outcome, since its
    # event was validated against a file that no longer exists. The
    # residual window is between that check and the write itself, which is
    # a few instructions rather than a whole validation.
    fold_size = path.stat().st_size if path.exists() else None

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
    # Encode BEFORE opening the file. `json.loads` accepts an unpaired
    # surrogate (`"\ud800"`), `json.dumps` re-emits it happily, and only the
    # UTF-8 write fails — after the file has been created. Caught here it is
    # a clean refusal that wrote nothing; caught at the write it is the
    # `WRITE FAILED` message, which would be telling the operator to inspect
    # a partial line that does not exist. Either way it must not be a
    # traceback: that is the one output shape SKILL.md's stderr protocol
    # gives the agent no way to classify.
    try:
        line.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JournalError(
            "event",
            f"contains a character that cannot be written as UTF-8: {exc}. "
            "Nothing was written.",
        ) from exc
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
    # The size BEFORE this append, so a failed write can be undone exactly.
    existed_before = path.exists()
    try:
        size_before = path.stat().st_size if existed_before else 0
    except OSError:
        size_before = None

    now_size = size_before if existed_before else None
    if now_size != fold_size:
        raise JournalError(
            "event",
            f"REFUSED: {path.as_posix()} changed while this event was being "
            f"validated "
            f"({fold_size} bytes when read, {now_size} now) — another "
            f"process appended to it. Nothing was written by this call. Its "
            f"event was checked against a file that no longer exists, and "
            f"appending it now could put a duplicate declaration, link or "
            f"resolution in the journal, which every later read refuses. "
            f"Re-run this command.",
        )

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            # Durable before `tag` reports success. Without this the event
            # sits in the page cache and a crash before writeback loses it,
            # while the operator has been told it was recorded — of the two
            # files here, the journal is the one nothing can re-fetch. Two
            # tags a session; the cost is not a consideration.
            f.flush()
            os.fsync(f.fileno())
        if not existed_before:
            # A file's NAME is not durable until its directory is synced —
            # syncing the contents is not enough on POSIX. The archive
            # writer learned this last cycle and the journal did not, so the
            # FIRST event ever recorded could be reported as written and
            # then vanish with its directory entry.
            _fsync_dir(path.parent)
    except (OSError, UnicodeEncodeError) as exc:
        # Truncate back to where this call found the file. That is NOT the
        # rewriting-existing-lines this module refuses to do: it removes
        # only bytes this call itself just wrote, and restores the file to
        # the exact contents it had a moment earlier. Without it, a write
        # that stops halfway leaves a partial last line, and `load_fold`
        # then refuses the WHOLE journal — so a failed append does not lose
        # one event, it loses every fact recorded before it.
        recovered = False
        # Whether anyone ELSE appended during our failed write. If someone
        # did, truncating back to `size_before` would delete their event,
        # which they have already been told was written. That is a worse
        # outcome than the partial line this recovery exists to remove, so
        # leave the file alone and say so.
        #
        # The question is about CONTENT, and the first version of this
        # asked about SIZE — whether the file had grown by more bytes than
        # our own line holds. That is not the same question, and it misses
        # the case exactly: our write stops after 40 of its 100 bytes while
        # a concurrent writer lands a complete 30-byte event, total growth
        # 70, which is not greater than 100 — so the size test read the
        # file as untouched by anyone else and truncated their event away
        # with our fragment.
        #
        # So compare the bytes. The region after `size_before` is OURS only
        # if it is a STRICT (shorter) PREFIX of the exact line this call
        # tried to write — evidence that the write stopped partway through.
        # A region the SAME LENGTH as our line, byte-identical to it, is not
        # a partial write: it means `write()`/`flush()` put the complete
        # line on disk and the failure this handler is reacting to happened
        # AFTER that, in `os.fsync` or the `with` block's own close — the
        # event is fully formed and `load_fold` would accept it. Treating
        # "identical to" as "a prefix of" (plain `startswith` does, since
        # every bytestring is trivially a prefix of itself) is exactly the
        # earlier defect here: it deleted a real, durable event because its
        # tail happened to equal our own line rather than stop short of it.
        #
        # Fail toward LEAVING THE FILE ALONE: a region we cannot read, one
        # longer than our line, or one the same length as our line (whether
        # identical to it or not — a same-length foreign line is refused
        # already by `validate_event`/`_fold_from_events`/the fold-size
        # check above, but the length check alone is sufficient and does
        # not need to lean on that) all count as foreign. An unremoved
        # partial line makes `load_fold` refuse loudly, which a human can
        # repair; a deleted event — foreign OR our own — is a fact nothing
        # can recover.
        line_bytes = line.encode("utf-8")
        if size_before is None:
            # The pre-append `stat` failed, so there is no boundary to read
            # from and no offset to truncate back to. Nothing is known
            # either way — not a foreign write, and the recovery below is
            # skipped regardless, on `size_before` alone.
            foreign = False
            exact_own_line = False
        else:
            try:
                with open(path, "rb") as f:
                    f.seek(size_before)
                    # One byte MORE than our line: enough to prove the
                    # region is longer than anything this call could have
                    # written, without reading a foreign append of
                    # unbounded size into memory.
                    tail = f.read(len(line_bytes) + 1)
            except FileNotFoundError:
                # The failed append never created the file. There is no
                # region to compare and nothing of anyone's to delete —
                # empty, not unreadable, so this is not a foreign write and
                # the `unlink` arm below still runs.
                tail = b""
            except OSError:
                tail = None
            foreign = (
                tail is None
                or len(tail) >= len(line_bytes)
                or not line_bytes.startswith(tail)
            )
            # A DIFFERENT question from `foreign`, computed over the same
            # `tail`: not "may this region be truncated" (still no —
            # `foreign` above already says no, correctly, and this flag
            # does not change that) but "is what actually happened
            # nameable instead of ambiguous". It is, exactly when `tail` is
            # byte-identical to our own line: `write()`/`flush()` put the
            # COMPLETE event on disk and the failure this handler is
            # reacting to happened strictly AFTER, in `os.fsync` or the
            # `with` block's own close. Nothing here is partial and no
            # other process wrote anything — the two claims the generic
            # message below makes, and both are false in this one case.
            exact_own_line = tail is not None and tail == line_bytes
        if size_before is not None and not foreign:
            try:
                if size_before == 0 and not existed_before:
                    # The journal did not exist when this call started, and
                    # the failed append created it. Truncating to zero would
                    # leave a 0-byte file where there had been none — still
                    # readable, but "nothing happened" would then be false
                    # by one directory entry. Remove it instead.
                    path.unlink()
                else:
                    with open(path, "r+b") as f:
                        f.truncate(size_before)
                recovered = True
            except OSError:
                pass
        if recovered:
            raise JournalError(
                "event",
                f"WRITE FAILED after validation passed: {exc}. The partial "
                f"write was undone and {path.as_posix()} is byte-for-byte what it "
                f"was "
                f"before this call — nothing was appended, and nothing "
                f"already recorded was lost.",
            ) from exc
        if exact_own_line:
            # Its own message, not the generic one below: that one asserts
            # a PARTIAL line and another process's write, and both are
            # false here — `tail` read back byte-identical to the line
            # this call tried to write. Saying so plainly instead of
            # folding this case into the ambiguous ones matters because
            # the generic wording invites the operator to "repair" the
            # last line, and there is nothing wrong with it to repair.
            raise OSError(
                f"WRITE FAILED after validation passed: {exc}. The event's "
                f"own line reached {path.as_posix()} IN FULL before this "
                f"failure — `write()`/`flush()` completed, and the failure "
                f"happened afterward, while making it durable (fsync or "
                f"the file close). The journal is intact: this is not a "
                f"partial line and no other process touched the file. Do "
                f"not edit or truncate the last line, and do not re-run "
                f"this command for the same event — it is already "
                f"recorded, and appending it again would create a "
                f"duplicate every later read refuses."
            ) from exc
        raise OSError(
            f"WRITE FAILED after validation passed: {exc}. "
            f"{path.as_posix()} may now hold a PARTIAL line — this is not a "
            f"clean "
            "refusal: check the last line of the file before appending again."
            + (" The partial write was deliberately NOT undone: the file grew "
               "by more than this call could have written, so another process "
               "appended during it, and truncating would delete an event that "
               "process has already been told was recorded."
               if foreign else "")
        ) from exc
