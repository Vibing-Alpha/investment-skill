"""CLI for the personal track-record capture tool: `pull | tag | unlinked |
open | due | report`. Every quarterly NUMBER and every money-path gate decision
lives in `archive.py` / `journal.py` / `summary.py` — `pull`, `tag`, `open`
and `report` only parse arguments, read/write the one file each command
touches, and print the one thing that command promises. `_cmd_unlinked` is
the one exception: it groups archived fill rows by `(account, order_id)`
and subtracts the journal's already-linked set — a real computation, owned
here rather than in `summary.py`, because it is CLI-specific presentation
(which orders still need a decision), not one of the six quarterly numbers
`summary.py` assembles.

See README.md in this package (`open`
added at Task 9 review round 1, so the skill's "what's currently open"
question is answered by a tested library function — `journal.open_theses`
— rather than untested set logic embedded in SKILL.md prose
(`.claude/rules/skill-architecture.md` #1/#3)).
Freeze decisions this module compiles against (see freeze file, D1/D2/D4):
`unlinked` reads order keys off the archived **trade** rows via
`fills_from` — D1 froze `get_account_orders` as NOT RETAINED (it reports
only live working orders), so an order snapshot can never supply the list
of filled orders. It prints
`account,order_id,date,side,size,symbol,price,company_name` (eight
columns): there is still no stable instrument id on a trade row (D4), so
the eighth column carries the company name the broker put on the fill —
reduced across every fill of the order into the agreed name,
`?MISSING-ON-SOME`, or `?CONFLICTING` — which is what makes a reassigned
ticker visible. The account/order_id machine key stays first, and
`date`/`side`/`size`/`price` are what let a human actually recognize which
trade a line is, printed most-recent-first. Before this fix `unlinked`
printed only `account,order_id,symbol`, which a person tagging hundreds of
orders a quarter cannot tell apart without cross-referencing the broker by
hand.

Freeze file: .claude/skills/track-record/references/ibkr-freeze.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.track_record.archive import (
    Envelope, write_envelope, read_envelopes, KNOWN_TOOLS, _TRADES_TOOL,
    _no_duplicate_keys,          # one implementation, per producer-consumer rule 3
    _covered_range,              # ditto: the range resolver coverage_gap uses
)
from scripts.track_record.completeness import (
    effective, effective_verdict, ReductionCache)
from scripts.track_record.journal import JournalError, append_event, open_theses
from scripts.track_record.summary import (
    assemble, current_order_id, fills_from, fold_or_reason, is_one_order,
    _SINGLE_ACCOUNT,
)

# `_TRADES_TOOL` used to be a THIRD local literal copy of this string (the
# module docstring at the top of archive.py named this exact line as the
# one review routed here to unify) — now imported from `archive`, same as
# `summary.py` already does, so the archive subdirectory `unlinked` reads
# can never drift from the name `write_envelope` accepts. `archive.py`
# already asserts `_TRADES_TOOL in KNOWN_TOOLS` at its own definition; a
# second assert of the same import here would only restate that.

_DEFAULT_ROOT = "reports/track-record/raw"
_DEFAULT_JOURNAL = "trade-journal.jsonl"
_DEFAULT_REPORTS = "reports/track-record"

# Same canonical UTC "Z" form journal.py's `_RECORDED_AT_FORMAT` /
# summary.py's `_CANONICAL_FORMAT` write — kept as its own literal here (a
# third private module-format constant) rather than reaching into either
# module for it: this one values-format an arbitrary `--at` instant into
# the string journal.py then only VALIDATES, a distinct operation from
# either of theirs.
_CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _canonical_z(value: str) -> str:
    """Parses an ISO-8601 instant (`Z` suffix or an explicit offset) and
    re-renders it as canonical UTC `Z`. Raises `ValueError` on anything
    that does not resolve to a tz-aware instant — an unparseable `--at` is
    a refusal, not a guess."""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"--at {value!r} has no timezone")
    return dt.astimezone(timezone.utc).strftime(_CANONICAL_FORMAT)


def _configure_stdin_utf8() -> None:
    """UTF-8 on stdin, but only when the stream supports it — tests
    substitute a plain `io.StringIO`, which has no `reconfigure`, and a
    bare unconditional call crashes those while looking correct against a
    real terminal (`.claude/rules/development.md`)."""
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")


def _read_stdin_json() -> dict:
    _configure_stdin_utf8()
    # Refuse a repeated member rather than silently keeping the last, which
    # is what `json.loads` does by default: an event carrying `intent` twice
    # would archive the second and lose the first, with nothing said.
    return json.loads(sys.stdin.read(), object_pairs_hook=_no_duplicate_keys)


_STAMP_DRIFT_HOURS = 24


def _note_if_stamp_is_not_now(stamp: str, field: str) -> None:
    """A NOTE on stderr when `stamp` is far from the present instant.

    Both `pull` and `tag` record something that is happening NOW — SKILL.md
    says to take the value from the clock, never from memory — and a model
    supplying a date from memory is not a hypothetical failure. A stamp days
    off is silent and consequential: it decides which quarter a fill or a
    thesis is counted in, and for `pull` it is also what a relative `period`
    like LAST_QUARTER resolves against, so a stale one can move an entire
    response into the wrong window and render `0 fills`.

    A NOTE, not a refusal, and the distinction is the point: this cannot be
    verified, only doubted. The machine clock can be wrong, a session can
    legitimately span midnight, and refusing would block a real capture of
    a window that is about to expire — the one thing this tool exists to
    prevent losing. So it states the discrepancy and leaves the judgment
    where it belongs.
    """
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return          # shape is the writer's business; it refuses on its own
    now = datetime.now(timezone.utc)
    drift = (now - when).total_seconds() / 3600

    # The quarter, not only the hours. What this stamp decides is which
    # quarter counts the event, and a stamp can land in the wrong one while
    # being minutes old: 20 minutes stale on January 1st is a different
    # quarter AND a different year, and well inside the drift threshold —
    # the exact harm the drift NOTE exists to warn about, in the one case
    # it stayed silent for.
    def _q(dt):
        return f"{dt.year}Q{(dt.month - 1) // 3 + 1}"

    if _q(when) != _q(now):
        # Additional to the drift NOTE below, never instead of it: a stamp
        # can be BOTH six years stale and in the wrong quarter, and the two
        # facts have different remedies.
        print(f"NOTE: {field} {stamp} falls in {_q(when)}, but this command "
              f"is running in {_q(now)}, and this stamp is what places the "
              f"record in a quarter. If it came from memory rather than the "
              f"clock, this has just been recorded in the wrong quarter.",
              file=sys.stderr)

    if abs(drift) <= _STAMP_DRIFT_HOURS:
        return
    direction = "in the past" if drift > 0 else "in the FUTURE"
    print(f"NOTE: {field} {stamp} is {abs(drift):.0f}h {direction}, and this "
          f"command records something happening now. If that stamp came from "
          f"memory rather than the clock, it has been archived as fact and "
          f"decides which quarter this is counted in.", file=sys.stderr)


def _cmd_pull(args: argparse.Namespace) -> int:
    flags = [f for f, on in (("--call-partial", args.call_partial),
                             ("--call-unknown", args.call_unknown)) if on]
    if len(flags) > 1:
        print(f"REFUSED: {' and '.join(flags)} cannot both be given — a call is "
              f"visibly partial, or its completeness cannot be established, "
              f"not both", file=sys.stderr)
        return 1
    stored = ("truncated" if args.call_partial
              else "unknown" if args.call_unknown else "complete")

    try:
        # The SAME duplicate-rejecting loader the stdin form used. A
        # repeated member would otherwise resolve implementation-dependently
        # and be archived.
        response = json.loads(Path(args.response).read_text(encoding="utf-8"),
                              object_pairs_hook=_no_duplicate_keys)
        call_args = json.loads(args.args, object_pairs_hook=_no_duplicate_keys)
        if not isinstance(call_args, dict):
            raise ValueError(f"--args must be a JSON object, got "
                             f"{type(call_args).__name__}")
        # `{"quarter": "YYYYQn"}` is a TEST-ONLY placeholder shape, and this
        # is the boundary where it can still be refused. `_covered_range`
        # reads it as coverage of the named quarter FROM ITS FIRST DAY
        # (capped at `pulled_at`, which closed the future direction and left
        # the past one open), and nothing downstream constrains the shape of
        # `--args` — `write_envelope` checks only `isinstance(args, dict)`.
        # So a real `DAYS_30` call archived under `{"quarter": "2026Q3"}`
        # certifies July and August as observed when nothing was ever pulled
        # for them: `coverage_gap` finds no hole and the report renders a
        # low, entirely plausible fill count with no `unavailable` naming
        # it. The shape cannot simply be dropped from `_covered_range` — the
        # given fixtures and 39 archive tests are written on it — so it is
        # refused HERE, where the only caller is a live pull. SKILL.md never
        # emits it (grep: zero occurrences).
        if "quarter" in call_args:
            raise ValueError(
                "--args carries a 'quarter' key, which is a test-only "
                "placeholder: it claims coverage of that whole quarter from "
                "its first day, whatever window the call actually requested, "
                "so a shorter call archived under it certifies months that "
                "were never pulled and the report shows no gap. Pass the "
                "args the call was ISSUED with — for get_account_trades "
                "that is {\"period\": \"<PERIOD>\"} (SKILL.md Step 2, e.g. "
                "LAST_QUARTER / DAYS_30 / YEAR_TO_DATE), and for "
                "get_pa_performance_all_periods it is {}"
            )
    except (json.JSONDecodeError, ValueError, OSError, RecursionError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    # The stamp NOTE comes AFTER the write, not here. SKILL.md's
    # three-channel contract defines `NOTE:` as "the command DID its work
    # and exited 0"; emitting one and then refusing on an archive I/O
    # error prints a NOTE followed by REFUSED and exit 1 — a combination
    # the skill teaches cannot occur, so an operator reading the NOTE
    # would believe the pull was archived.
    try:
        path = write_envelope(Path(args.root), args.tool, call_args,
                              args.pulled_at, stored, response)
    except (json.JSONDecodeError, ValueError, OSError, RecursionError) as exc:
        # The SAME catch tuple the stdin form used. `write_envelope` raises
        # `ValueError`; `RecursionError` is neither that nor `OSError` and
        # escaped as a traceback until it was added — the one output shape
        # the stderr protocol gives the agent no way to classify.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(path.as_posix())

    # Now the archive holds it, so a NOTE is honest: the command DID its
    # work. Moved here from before the write for that reason.
    _note_if_stamp_is_not_now(args.pulled_at, "pulled_at")

    # Capture is never blocked by classification: the verdict is a NOTE and
    # nothing from it is stored. Any uncertainty inside the classifier is
    # its own `unknown`, not a refusal here.
    try:
        envs = read_envelopes(Path(args.root), args.tool)
        mine = [e for e in envs if isinstance(e, Envelope) and e.path == path]
        if mine:
            verdict, reason = effective(mine[0], envs, tool=args.tool)
            if verdict != "complete":
                print(f"NOTE: read against the whole archive this pull is "
                      f"{verdict} — {reason}. The archive stores your own "
                      f"observation ({stored}); this verdict is recomputed "
                      f"every time it is needed and nothing from it is "
                      f"written.", file=sys.stderr)
    except Exception as exc:                   # noqa: BLE001 — see below
        # DELIBERATELY broad, and the only broad catch in this file. The
        # archive write has already succeeded at this point; the spec's
        # rule is that classification can never block capture, and a
        # narrow catch does not deliver that — `_normalize_fill_row` can
        # raise, `canonical_bytes` can raise on a value json cannot
        # serialise, and `json` raises `RecursionError` on deep nesting,
        # none of which are `OSError`. Any of them escaping here would make
        # `pull` exit non-zero AFTER archiving, telling the operator the
        # capture failed when it did not.
        print(f"NOTE: this pull was archived, but it could not be checked "
              f"against the rest of the archive ({type(exc).__name__}: "
              f"{exc}). Treat its completeness as unestablished until a "
              f"later command can read the archive.", file=sys.stderr)
    return 0


def _cmd_tag(args: argparse.Namespace) -> int:
    try:
        event = _read_stdin_json()
        if not isinstance(event, dict):
            raise ValueError(f"stdin event must be a JSON object, got {type(event).__name__}")
        recorded_at = _canonical_z(args.at)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    event = dict(event, recorded_at=recorded_at)
    try:
        append_event(Path(args.journal), event)
    except (JournalError, OSError, UnicodeDecodeError) as exc:
        # `append_event` reads the EXISTING journal first (`_read_events` ->
        # `Path.read_text(encoding="utf-8")`), so it can fail for reasons
        # that are not `JournalError`: a permissions/`IsADirectoryError`
        # `OSError`, or a `UnicodeDecodeError` on a journal saved with a
        # stray non-UTF-8 byte. `UnicodeDecodeError` is a `ValueError`
        # subclass, NOT a `JournalError`, so the narrower arm let it out as
        # a traceback — on the ONE command the operator runs right after
        # hand-editing that file (SKILL.md Step 5), whose `intent` text is
        # zh-CN prose typed in an editor. `summary.fold_or_reason` and
        # `archive._read_one` already catch exactly this pair; this is the
        # third boundary of the same set.
        #
        # This arm ALSO covers the append itself, which is a different
        # animal: a read-phase failure leaves the journal untouched, but an
        # `OSError` during the write can leave a partial line behind. Both
        # are `OSError`, so this handler cannot separate them — instead
        # `append_event` re-raises the write-phase case with a message that
        # says so, and that message is what reaches the operator here. Do
        # not describe this line as "nothing was written" unconditionally;
        # read what the refusal actually says.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    _note_if_stamp_is_not_now(recorded_at, "--at")
    _note_if_deadline_already_passed(event, recorded_at)
    return 0


def _note_if_deadline_already_passed(event: dict, recorded_at: str) -> None:
    """A prediction whose deadline is at or before its own `recorded_at`.

    Stated, never blocked: the operator may mean exactly that — recording
    a call they already know the answer to, deliberately — and only they
    can say. What must not happen is silence.
    """
    prediction = event.get("prediction")
    if not isinstance(prediction, dict):
        return
    raw = prediction.get("deadline")
    if not isinstance(raw, str):
        return                      # validation has already refused it
    try:
        deadline = _canonical_z(raw)
    except ValueError:
        return                      # ditto
    if deadline <= recorded_at:
        print(f"NOTE: this prediction's deadline ({raw}) is at or before its "
              f"own recorded_at ({recorded_at}), so it was already decidable "
              f"when recorded — a record of hindsight, not a prediction. It "
              f"has been appended as given; say so to the user.",
              file=sys.stderr)


def _summable(value) -> bool:
    """True when `value` is a real number this can add without raising.

    `isinstance(x, (int, float))` is not that test: a JSON number parses as
    a Python int of unbounded width, so `10**400` passes it and then
    overflows on the first arithmetic with a float. The existing fallback —
    show the last fill and say the order could not be summed — is the right
    outcome for such a row; it just never got the chance to run.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _cmd_unlinked(args: argparse.Namespace) -> int:
    # Validated BEFORE the archive is read: a malformed cutoff would
    # otherwise compare as a plain string against `trade_time[:10]` and
    # silently keep or drop the wrong half of the backlog. Refuse, never
    # interpret — this command's whole contract is that its list can be
    # trusted to be the backlog.
    since = getattr(args, "since", None)
    since_date = None
    if since is not None:
        try:
            # `date.fromisoformat`, NOT `strptime("%Y-%m-%d")`: strptime accepts
            # a non-zero-padded `2026-8-2`, which then LOSES the lexical
            # comparison against a canonical `2026-08-29` — so the cutoff would
            # hide the entire backlog INCLUDING today's fills while printing a
            # NOTE that says it hid only the older ones. That is the exact
            # failure this validator exists to prevent (codex review 2026-08-29).
            since_date = datetime.strptime(since, "%Y-%m-%d").date()
            if since_date.isoformat() != since:
                raise ValueError("not canonical YYYY-MM-DD")
        except (TypeError, ValueError):
            print(f"REFUSED: --since must be a zero-padded ISO date "
                  f"(YYYY-MM-DD), got {since!r}", file=sys.stderr)
            return 1
    try:
        trades_envelopes = read_envelopes(Path(args.root), _TRADES_TOOL)
    except OSError as exc:
        # Per-FILE read errors are already `ParseFailure`s inside
        # `_read_one`; this arm covers the DIRECTORY-level failure the glob
        # itself can raise (an unreadable archive dir). Without it the only
        # subcommand-level failure that isn't a `REFUSED:` line is the one
        # that means "the archive is unreachable" — the case the operator
        # most needs stated plainly.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    # The NOTEs are deferred until after EVERY refusal this command can
    # make — the fills gate below and the journal gate after it. They all
    # describe the LIST it is about to print, and SKILL.md's own protocol
    # says a `NOTE:` means the command completed. Printed before a refusal
    # they contradicted that protocol and described a list that never
    # appeared: not false, but the kind of noise that teaches a reader to
    # skip NOTE lines, which is the only way these fail.
    fills = fills_from(trades_envelopes)
    if isinstance(fills, str):
        print(f"REFUSED: {fills}", file=sys.stderr)
        return 1
    fold = fold_or_reason(Path(args.journal))
    if isinstance(fold, str):
        print(f"REFUSED: {fold}", file=sys.stderr)
        return 1
    # A NOTE on stdERR, never a gate and never a nonzero exit. This command
    # prints a to-do list, and it deliberately reads every archived pull
    # without filtering on `completeness` — that value is the agent's own
    # self-report (SKILL.md: the CLI records what it is told and does not
    # verify it), so refusing on it would block the backlog on an
    # unverifiable claim. But an operator tagging from a SHORT list has no
    # way to know it is short, and the empty case is worse: no output plus
    # exit 0 reads as "nothing left to tag". So state the fact and let the
    # reader judge it — evidence, not a verdict.
    # The paragraph above says the empty case is the worse one — and then
    # only spoke when a pull was marked incomplete, so the emptiest case of
    # all stayed silent. With no archived trades pull at all there is no
    # backlog to be at the end of: the question has no ground, and silence
    # plus exit 0 is exactly how a skipped or lost Step 2 presents itself.
    if not any(isinstance(e, Envelope) for e in trades_envelopes):
        # Two different situations, and the remedies are opposite ones. The
        # first version said "the archive is empty, run the pull step" for
        # both — which is a false statement AND the wrong instruction when
        # pulls ARE archived and simply cannot be read.
        # The unreadable-archive branch that used to live here is gone,
        # not moved: it could not run. `fills_from` refuses on the FIRST
        # ParseFailure, and the NOTEs were deferred behind that refusal two
        # cycles later — so "every archived pull failed to parse" required
        # reaching a line the command always returns before. Its text even
        # said "see the refusal below", which by then was above. What it
        # would have said, the refusal says by name.
        print(f"NOTE: no {_TRADES_TOOL} pull is archived under "
              f"{Path(args.root).as_posix()}, so there is nothing to build "
              f"a backlog from — this empty list means the archive is "
              f"empty, NOT that every order has been tagged. Run the pull "
              f"step first.", file=sys.stderr)
    incomplete = sorted({e.completeness for e in trades_envelopes
                         if isinstance(e, Envelope) and e.completeness != "complete"})
    if incomplete:
        print(f"NOTE: this list is built from archived pulls, some of which are "
              f"marked {', '.join(incomplete)} — orders they missed cannot appear "
              f"below, and an empty list does not prove there is nothing to tag.",
              file=sys.stderr)



    linked: set[tuple[str, str]] = set()
    for event in fold.events:
        if event.get("event") != "orders_linked":
            continue
        for ref in event["order_refs"]:
            linked.add((ref["account"], ref["order_id"]))

    # One line per ORDER, not per fill (brief constraint 3), and the numbers
    # on that line describe the WHOLE order: size is every fill summed, price
    # is their volume-weighted average. Printing the last fill's own size and
    # price instead — which is what this did — turns a 1,000-share decision
    # that finished with a 25-share tail into "25 @ 130", unrecognizable as
    # the decision it was and off by 40x on the figure a person scans for.
    # This list exists precisely so a human can recognize WHICH DECISION an
    # order was; a real quarter runs to hundreds of orders, and without that
    # tagging degrades from "a quick note" to "forensic broker lookup".
    #
    # The DATE and SIDE stay the latest fill's: the date is when the decision
    # finished, and side cannot meaningfully be aggregated (a single order is
    # one side; a drifted feed that disagrees is not something to average).
    by_key: dict[tuple[str, str], dict] = {}
    for row in fills:
        if row["order_id"] is None:
            continue
        key = (row["account"], row["order_id"])
        agg = by_key.get(key)
        if agg is None:
            agg = by_key[key] = {"latest": row, "size": 0.0, "notional": 0.0,
                                 "summable": True, "fills": 0, "rows": [],
                                 "coherent": True}
        agg["fills"] += 1
        agg["rows"].append(row)
        # Reduced across EVERY fill of the order, not read off one of them:
        # a name present on the first fill and missing on the third is a
        # partial observation, and storing it would assert a group-wide
        # fact from partial evidence.
        agg.setdefault("names", set()).add(row["company_name"])
        if row["trade_time"] > agg["latest"]["trade_time"]:
            agg["latest"] = row
        size, price = row["size"], row["price"]
        # A drifted or missing field must not silently become a wrong total.
        # Fall back to the latest fill's own raw values and say so, rather
        # than sum what cannot be summed.
        # `_summable` rather than a bare isinstance pair: a JSON number is a
        # Python int of unbounded width, so `10**400` is an `int`, is not a
        # `bool`, and then raises `OverflowError` the moment it meets a
        # float — a raw traceback out of the one command a person pipes into
        # `less`, instead of the NOTE this branch exists to print.
        if _summable(size) and _summable(price):
            # `float()` on each operand BEFORE multiplying. Both `10**200`
            # convert to finite floats and pass `_summable` — and then
            # `size * price` stays in Python's unbounded INT domain, so the
            # product is `10**400` and adding it to a float accumulator
            # raises `OverflowError`. In the float domain the same product
            # is `inf`, which the accumulator guard below already catches
            # and turns into the last-fill fallback this was reaching for.
            agg["size"] += float(size)
            agg["notional"] += float(size) * float(price)
        else:
            agg["summable"] = False

    # One order is one instrument on one side. Two executions sharing an
    # `order_id` while disagreeing about either are not an order — and
    # summing them produced `SELL 12 BBB @ 15.83` out of a BUY of AAA and a
    # SELL of BBB: a line describing an order that never existed, printed as
    # a to-do the operator would then tag. Not aggregated, and named in the
    # NOTE below.
    #
    # `summary.is_one_order`, not an inline comparison against
    # `agg["latest"]` — the same rule now decides whether `_count_fills` may
    # count this group as one order, and two spellings of it WILL diverge
    # (`.claude/rules/producer-consumer.md` rule 3). Computed after the loop
    # rather than inside it because the reference the loop compared against
    # (`agg["latest"]`) moves as later fills arrive; the whole group is the
    # honest subject of the question, and no reader of `coherent` runs
    # before this point.
    for agg in by_key.values():
        agg["coherent"] = is_one_order(agg["rows"])

    # Sorted by trade_time descending — most recent first, the order a
    # person actually scans a backlog in, not alphabetically by order id.
    # A journal reference the archive has never seen. `orders_linked`
    # accepts any (account, order_id) pair — it must, since the operator can
    # tag a trade before the next pull archives it — but an INVENTED or
    # mistyped ref is indistinguishable from that legitimate case until the
    # real order arrives, at which point it is subtracted from this backlog
    # and disappears with no warning: a real decision lost silently, which
    # is the one thing this tool exists to prevent. An account other than
    # the archive's own can never match at all. Stated, not enforced — the
    # legitimate case is real and refusing would break it.
    unmatched = sorted(f"{a},{o}" for a, o in linked if (a, o) not in by_key)
    # An account this archive can NEVER emit is a different kind of
    # unmatched: every archived row carries the frozen `_SINGLE_ACCOUNT`
    # constant, so a ref naming anything else is provably dangling rather
    # than merely not-pulled-yet, and no future pull will resolve it. The
    # journal is deliberately NOT narrowed to refuse it — `load_fold`
    # re-validates every event on every READ, so hard-coding today's single
    # account into the schema would make a future multi-account journal
    # unreadable — but the difference is worth naming here, where it is
    # actionable.
    # Count the REFS, name the ACCOUNTS. The count came from the set of
    # distinct account names, so ten references under one foreign account
    # reported "1 linked order ref" — understating, by the size of the
    # group, the one message whose job is to say how much of the journal
    # can never be checked against any pull.
    impossible_refs = [(a, o) for a, o in linked if a != _SINGLE_ACCOUNT]
    impossible = sorted({a for a, _o in impossible_refs})
    if impossible_refs:
        print(f"NOTE: {len(impossible_refs)} linked order ref(s) name an "
              f"account this archive cannot contain — every archived row is "
              f"{_SINGLE_ACCOUNT}, so these can never match any pull, now or "
              f"later. Account(s) named: {', '.join(impossible)}",
              file=sys.stderr)
    if unmatched:
        print(f"NOTE: {len(unmatched)} linked order ref(s) match no archived "
              f"order, so nothing here can be checked against them — a typo "
              f"or an invented ref looks the same as an order not yet pulled, "
              f"and will silently suppress the real one when it arrives: "
              f"{'; '.join(unmatched[:10])}"
              + (f" (+{len(unmatched) - 10} more)" if len(unmatched) > 10 else ""),
              file=sys.stderr)

    # A LINKED group whose executions have since been re-keyed. Grouping
    # here follows each execution's FIRST order_id, deliberately: the user
    # tagged the order as it existed, and every restatement in the live
    # archive moved a WHOLE order — nine of them, including one with three
    # executions that all moved together, and not one execution left behind
    # on an old id. So a re-keyed order stays tagged, correctly.
    #
    # What that reasoning cannot survive is a SPLIT: one execution re-keyed
    # away while its siblings stay. Then the departed execution is a
    # distinct order today, and subtracting it because the id it used to
    # share is linked would drop a trade needing intent, silently — the one
    # outcome this tool exists to prevent. Nothing observed does that, and
    # inventing a rule for it would break the links that DO work. So say it
    # instead: if it ever happens, this line is what shows it.
    # EVERY execution in the group, not just its latest. Checking only
    # `agg["latest"]` meant a split was invisible whenever the execution
    # that moved was not the most recent one — which is the ordinary case,
    # and precisely the scenario this line exists to reveal.
    restated = sorted({
        f"{acct},{oid} -> {current_order_id(row)}"
        for (acct, oid), agg in by_key.items()
        if (acct, oid) in linked
        for row in agg["rows"]
        if current_order_id(row) not in (None, oid)
    })
    if restated:
        print(f"NOTE: {len(restated)} order(s) you have already tagged were "
              f"re-keyed by the broker afterwards. They stay tagged — a "
              f"restatement renumbers an order, it does not place a new one "
              f"— and are NOT listed below. Shown so you can check that each "
              f"is a renumbering and not one execution split away from its "
              f"order: {'; '.join(restated[:10])}"
              + (f" (+{len(restated) - 10} more)" if len(restated) > 10 else ""),
              file=sys.stderr)

    # The SPLIT the NOTE above says "this line is what shows one" — for an
    # order nobody has tagged yet. That line is scoped to `in linked`, so
    # before either half is linked the split was invisible: grouping follows
    # each execution's FIRST id, so two executions that shared order 100 and
    # were then re-keyed apart still print as ONE merged order-100 row, with
    # its size the sum of both, exit 0 and nothing on stderr. The operator
    # links that row and tags the departed execution — today a distinct
    # order — under an id the broker no longer gives it, which is the same
    # "a trade needing intent leaves the backlog silently" the linked NOTE
    # exists to prevent, one step earlier.
    #
    # UNLINKED only: the linked case is the NOTE above, which says the
    # different thing that case needs ("they stay tagged").
    #
    # A whole-order renumbering does NOT fire this — every execution's
    # current id agrees, and linking the printed first id is correct
    # (`fills_from` keeps that id on the row, so the link keeps matching).
    # A NOTE that fired on every re-keyed order is one nobody reads.
    def _shown(value) -> str:
        return "no order" if value is None else str(value)
    split = sorted(
        f"{acct},{oid} -> "
        + ", ".join(sorted(_shown(current_order_id(r)) for r in agg["rows"]))
        for (acct, oid), agg in by_key.items()
        if (acct, oid) not in linked
        and len({current_order_id(r) for r in agg["rows"]}) > 1
    )
    if split:
        print(f"NOTE: {len(split)} order(s) listed below merge executions the "
              f"archive's latest pulls assign to DIFFERENT orders — one "
              f"execution was re-keyed away from its siblings. The row shows "
              f"the id they were first archived under and its columns sum ALL "
              f"of them, so linking it tags an execution the broker no longer "
              f"places on that order. Shown, not withheld: which decision "
              f"each execution was is yours to say. "
              f"{'; '.join(split[:10])}"
              + (f" (+{len(split) - 10} more)" if len(split) > 10 else ""),
              file=sys.stderr)

    # An execution whose NEWEST sighting carries no order id —
    # `order_id_latest` present and `None`, which `fills_from` writes
    # deliberately and separately from "never restated". The row keeps its
    # first-seen id (there is no other id to key off), so an untagged one
    # still prints below as an ordinary linkable line and a tagged one stays
    # silently tagged; neither said anything until this NOTE. Reported for
    # BOTH, because the tagged case is the quieter of the two — the order
    # never appears in any list again.
    #
    # What the NOTE may say is bounded by what the archive can observe: a
    # sighting that carries no order id. WHY it carries none is not
    # observable here — a broker taking the id back and a sparse response
    # that simply omitted the field arrive as the same bytes, and
    # `fills_from` normalizes an absent key and an explicit `null` to one
    # `None` on purpose. An earlier version of this line named a broker
    # action, which is a claim on a cause the tool cannot see; per
    # `.claude/rules/producer-consumer.md` an absence must not be reported
    # as a contradiction.
    #
    # Evidence, not a verdict, like every NOTE here: the absence may be the
    # broker still settling, and refusing would block the whole backlog on
    # it.
    idless = sorted(
        f"{acct},{oid}" + ("" if (acct, oid) not in linked else " [tagged]")
        for (acct, oid), agg in by_key.items()
        if any(current_order_id(r) is None for r in agg["rows"])
    )
    if idless:
        print(f"NOTE: {len(idless)} order(s) hold an execution whose newest "
              f"sighting in the archive carries NO order id at all. Why is "
              f"not established — the archive records that the field is "
              f"absent, not what the broker meant by it. The id named here "
              f"is the one first archived, which is what a row below or an "
              f"existing link still keys off, so it is not what the latest "
              f"pull carries: {'; '.join(idless[:10])}"
              + (f" (+{len(idless) - 10} more)" if len(idless) > 10 else ""),
              file=sys.stderr)

    # An already-tagged order whose group is INCOHERENT — its executions
    # disagree about symbol or side, so the broker reused the id for a
    # different instrument. `coherent` reached only the printing path, and
    # the line below drops every linked key before anything prints: exit 0,
    # empty stdout, empty stderr, and a real trade that never entered the
    # backlog. That is the outcome the re-keying NOTE above calls the one
    # this tool exists to prevent; this is the same sentence for the group
    # it did not cover.
    #
    # Not a gate. Which of the two decisions the existing link belongs to
    # is the operator's to say — the tool cannot know, and refusing would
    # block the whole backlog on it. Evidence, not a verdict.
    reused = sorted(
        f"{acct},{oid} ({', '.join(sorted({str(r['symbol']) for r in agg['rows']}))})"
        for (acct, oid), agg in by_key.items()
        if (acct, oid) in linked and not agg["coherent"]
    )
    if reused:
        print(f"NOTE: {len(reused)} order(s) you have already tagged now hold "
              f"executions that disagree about the symbol or the side — the "
              f"broker reused the id for something else. They are NOT listed "
              f"below, so the newer trade is not in your backlog and needs "
              f"tagging by hand: {'; '.join(reused[:10])}"
              + (f" (+{len(reused) - 10} more)" if len(reused) > 10 else ""),
              file=sys.stderr)

    entries = [(key, agg) for key, agg in by_key.items() if key not in linked]
    entries.sort(key=lambda item: item[1]["latest"]["trade_time"], reverse=True)

    # `--since` (feedback 2026-08-29 track-record B). The backlog is
    # cumulative and only shrinks by tagging, so an operator adopting the
    # tool with an archive going back a year meets it as 555 rows at once —
    # and D6 asks a prediction per tag group, which is not executable at
    # that size. There is deliberately NO `--limit`: a truncated list is
    # indistinguishable from a short one, whereas a DATE the operator chose
    # is a fact they can restate. And the filter always discloses what it
    # hid, for the reason every NOTE in this command exists: a shortened
    # backlog that does not say it was shortened reads as a cleared one.
    # It filters the PRINTED rows only — the diagnostic NOTEs above still
    # describe the whole archive, because a conflict or a split does not
    # stop being true because it is old.
    hidden = 0
    if since_date is not None:
        cutoff = since_date.isoformat()
        # Both sides are now canonical `YYYY-MM-DD` — `trade_time` is
        # normalised to canonical UTC by `fills_from`, and the cutoff was
        # round-tripped through `date` above — so the lexical compare is the
        # date compare.
        kept = [(k, a) for k, a in entries
                if a["latest"]["trade_time"][:10] >= cutoff]
        hidden = len(entries) - len(kept)
        entries = kept
        if hidden:
            print(f"NOTE: {hidden} untagged order(s) finished before "
                  f"{cutoff} and are NOT listed below — `--since` hid them, "
                  f"they are still untagged, and they will keep appearing "
                  f"in an unfiltered run until they are linked or the "
                  f"archive stops covering them.", file=sys.stderr)

    # csv.writer, not an f-string join: a field containing a comma (a symbol
    # like "ACME, INC", or a drifted non-scalar) used to split into an extra
    # column and shift every field after it — a row that still looks
    # well-formed while meaning something else.
    writer = csv.writer(sys.stdout, lineterminator="\n")
    unsummable: list[str] = []
    conflicting: list[str] = []
    partial: list[str] = []
    # This is the one subcommand a person pipes into `head`/`less` — the
    # backlog grows without bound (hundreds of orders a quarter, leaving
    # only when tagged). A BrokenPipeError out of writerow would exit 1
    # with no `REFUSED:` line, the one shape SKILL.md Step 4's rule gives
    # the agent no way to classify. A closed pipe means the reader stopped
    # reading; that is not a failure of this command.
    def _emit(row) -> bool:
        try:
            writer.writerow(row)
            return True
        except BrokenPipeError:
            return False
    for (account, order_id), agg in entries:
        latest = agg["latest"]
        date = latest["trade_time"][:10]   # trade_time is canonical UTC Z
        names = agg["names"]
        present = {n for n in names if n}
        # Two SYMBOLS under one order id is the same fact `?CONFLICTING`
        # already names for the company — the broker reported two
        # securities under one order — so it takes the same sentinel and
        # the same "leave it unlinked" instruction rather than a second
        # vocabulary. The row prints only the LATEST fill's symbol, so
        # without this the operator sees an ordinary-looking line and can
        # tag the whole order under the wrong instrument, permanently.
        # (The existing `reused` NOTE above covers only orders ALREADY
        # linked; an untagged one had nothing.)
        #
        # SYMBOLS, not `agg["coherent"]`. That flag is also false when the
        # fills merely disagree about the SIDE, which says nothing about
        # WHICH security this is — and marking it an identity conflict
        # would leave the order permanently unlinked, losing the user's
        # contemporaneous intent over a disagreement identity never had.
        # A side disagreement already has its own handling: it makes the
        # order unsummable and is named in that NOTE.
        symbols = {str(r["symbol"]) for r in agg["rows"]}
        if len(present) > 1 or len(symbols) > 1:
            company = "?CONFLICTING"
            seen = sorted(present)
            if len(symbols) > 1:
                seen += sorted(symbols)
            conflicting.append(f"{account},{order_id} ({', '.join(seen)})")
        elif present and None in names:
            company = "?MISSING-ON-SOME"
            partial.append(f"{account},{order_id} ({next(iter(present))})")
        else:
            company = next(iter(present), "")
        # The ACCUMULATION, not only its operands. Two finite fills of
        # `1e308` sum to `inf`, and `inf / inf` is `nan` — so a row read
        # `inf,XYZ,nan`, two plausible-looking CSV fields that are neither a
        # size nor a price. `_summable` guarded each addend and nothing
        # guarded the total.
        if not (_summable(agg["size"]) and _summable(agg["notional"])):
            agg["summable"] = False
        if not agg["coherent"]:
            agg["summable"] = False
        # `fills == 1` counts too. The single-fill branch below prints the
        # feed's own values untouched — which for a `null` size and price is
        # two EMPTY columns, under a documented promise that they describe
        # the whole order, with exit 0 and nothing on stderr.
        if agg["fills"] == 1 and not (_summable(latest["size"])
                                      and _summable(latest["price"])):
            unsummable.append(f"{account},{order_id}")
        elif agg["fills"] > 1 and (not agg["summable"] or not agg["size"]):
            # The comment below used to promise this branch would "say so".
            # It did not, and the row it emits is shaped exactly like a
            # correct one — under a SKILL.md that now states these columns
            # are the whole order's. So an order the tool could not sum is
            # named, here, before the row that misrepresents it. The
            # condition mirrors the fallback arm below EXACTLY — the first
            # version guarded only `not summable`, so an order whose fills
            # sum to ZERO (a same-day open and close, both numeric) fell
            # back silently through the other arm.
            unsummable.append(f"{account},{order_id}")
        if agg["fills"] == 1 or not agg["summable"] or not agg["size"]:
            # A single fill IS the whole order, so print the feed's own
            # values untouched — aggregating one row would only reformat it
            # (10 -> 10.0), changing output for the common case to no end.
            # A non-summable or zero total falls back the same way.
            size, price = latest["size"], latest["price"]
        else:
            size = agg["size"]
            if isinstance(size, float) and size.is_integer():
                size = int(size)      # share counts are whole; 1000.0 reads wrong
            # No guard on the division: `notional / size` is a weighted mean
            # price, so its magnitude is bounded by the largest `price` in
            # the order — every one of which `_summable` has already proven
            # finite. I added a check here and could not construct an input
            # that reaches it; the sum check above is what makes that true,
            # and a branch nothing can enter is worse than no branch.
            price = round(agg["notional"] / agg["size"], 10)
        if not _emit([account, order_id, date, latest["side"], size,
                      latest["symbol"], price, company]):
            return 0

    if conflicting:
        print(f"NOTE: {len(conflicting)} order(s) hold fills that disagree about "
              f"the company name or the symbol — the broker reported two "
              f"securities under one order id. Do NOT pick one; one order "
              f"cannot be split, so these stay UNLINKED until the broker's own "
              f"record settles: {'; '.join(conflicting[:10])}", file=sys.stderr)
    if partial:
        print(f"NOTE: {len(partial)} order(s) carry a company name on some fills "
              f"and not others, so the name is not recorded for them: "
              f"{'; '.join(partial[:10])}", file=sys.stderr)
    if unsummable:
        print(f"NOTE: {len(unsummable)} order(s) could not be summed (a fill's "
              f"size or price was non-numeric, the fills sum to zero, or the "
              f"fills disagree about the symbol or the side), so "
              f"their size/price columns show that order's LAST FILL, not the "
              f"whole order: {'; '.join(unsummable)}", file=sys.stderr)
    return 0


def _cmd_due(args: argparse.Namespace) -> int:
    """Outstanding, past-deadline predictions.

    Both sides go to UTC instants before comparison. A deadline may carry
    an offset — `2026-08-19T23:00:00-05:00` READS as earlier than
    `2026-08-20T01:00:00Z` and is three hours later — so comparing the text
    gives the opposite answer from comparing the instants, and the cost of
    that is asking the user to resolve a prediction that has not come due.

    `--as-of` is required and never defaults to the clock: the determinism
    layer forbids a clock read in a computation path, and a default would
    put one here.
    """
    try:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            raise ValueError(f"--as-of {args.as_of!r} has no timezone")
        as_of = as_of.astimezone(timezone.utc)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    fold = fold_or_reason(Path(args.journal))
    if isinstance(fold, str):
        print(f"REFUSED: {fold}", file=sys.stderr)
        return 1

    # ANYWHERE in the file, not "later in the file": the fold resolves
    # references regardless of order, so a resolution written above its
    # declaration already counts.
    resolved = {e["thesis_id"] for e in fold.events
                if e["event"] == "prediction_resolved"}
    still_open = open_theses(fold)

    rows = []
    for tid, decl in fold.declared.items():
        pred = decl.get("prediction")
        if not pred or tid in resolved:
            continue
        try:
            deadline = datetime.fromisoformat(
                pred["deadline"].replace("Z", "+00:00")).astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            print(f"REFUSED: thesis {tid} has an unreadable deadline "
                  f"{pred.get('deadline')!r}: {exc}", file=sys.stderr)
            return 1
        if deadline >= as_of:          # strictly past, not "at"
            continue
        rows.append((deadline, tid, pred))

    writer = csv.writer(sys.stdout, lineterminator="\n")
    for deadline, tid, pred in sorted(rows, key=lambda r: (r[0], r[1])):
        writer.writerow([tid, pred["deadline"], pred.get("probability_pct", ""),
                         "yes" if tid in still_open else "no",
                         pred["proposition"]])
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    fold = fold_or_reason(Path(args.journal))
    if isinstance(fold, str):
        print(f"REFUSED: {fold}", file=sys.stderr)
        return 1

    # "<thesis_id> - <intent>" (a dash, not a comma): intent is free-form
    # user prose and may itself contain a comma, so a CSV-style join here
    # would misparse — unlike unlinked's account/order_id/symbol columns,
    # which are broker-issued ids that never do.
    for thesis_id, event in sorted(open_theses(fold).items()):
        # ONE line per thesis, always. `intent` is free-form prose the user
        # types or PASTES, and a newline in it would emit a second line that
        # SKILL.md Step 3 reads as another thesis id — then Step 4 asks
        # whether the new group supersedes that phantom. The comment below
        # already reasons about comma-safety in this same output; newlines
        # are the same hazard with a worse failure.
        intent = " ".join(str(event.get("intent", "")).split())
        print(f"{thesis_id} - {intent}")
    return 0


def _corroboration_note(root: Path, quarter: str) -> str | None:
    """Which pulls covering `quarter` nothing else in the archive corroborates.

    Read-time, printed, never stored — spec 2026-08-19 Section 5. A
    summarizing harness produces valid JSON, so a short pull with no
    overlapping partner classifies `complete` and renders a low but
    entirely plausible fill count. Nothing detects that; what the operator
    is owed is knowing WHICH figures rest on one uncorroborated
    observation. STDERR, because `summary.md` must stay byte-identical.
    """
    from scripts.track_record.completeness import (
        ReductionCache, effective_verdict, uncorroborated_stretches)
    from scripts.track_record.intervals import intersect, overlaps
    from scripts.track_record.summary import quarter_bounds

    try:
        bounds = quarter_bounds(quarter)
        # A SECOND read of the archive, deliberately left as one. `assemble`
        # has already read its own envelope list by the time this runs, and
        # `coverage_gap`'s docstring states the opposite property for
        # itself on purpose — "reads only the envelope list the caller
        # already holds ... so the coverage answer and the counted rows
        # come from one snapshot". This NOTE does not have that property:
        # a pull archived between `assemble` returning and this line could
        # make the disclosure describe a snapshot the rendered figures were
        # never computed from. Tolerated, not overlooked — it needs a
        # concurrent session writing to this root mid-report, which Step 6
        # of SKILL.md already tells the operator it cannot rule out (the
        # same window it names for the benchmark pin), and the cost is a
        # stderr line naming one stretch too many or too few, never a
        # figure in `summary.md`. Do NOT assume the one-snapshot property
        # holds here: threading `assemble`'s list out to this caller is
        # what would establish it.
        envelopes = read_envelopes(root, _TRADES_TOOL)
    except (ValueError, OSError):
        # The report itself refuses on these, with its own message. A
        # disclosure that raised here would turn a rendered report into a
        # traceback, which is the inverse of this command's contract.
        return None

    # ONE memo for the whole loop, and this is the loop that needed it
    # most. Each candidate below costs a verdict, and then
    # `uncorroborated_stretches` costs a verdict PER PARTNER — n^2 verdicts
    # over n envelopes, each re-encoding every response and re-normalizing
    # every row, i.e. a cube over an archive that only grows. Measured 26.6s
    # at 30 daily `DAYS_30` pulls, and this line runs AFTER `summary.md` is
    # already written, so a run abandoned on the slowness leaves a plausible
    # report and no disclosure — exactly what the disclosure exists to
    # prevent. `ReductionCache` is bound to THIS `envelopes` list and is
    # dropped when this function returns; it is never module state, because
    # the archive can gain a pull between two calls and a survivor would
    # answer the newer one from a stale reduction.
    cache = ReductionCache(envelopes)
    alone = []
    for e in envelopes:
        if not isinstance(e, Envelope):
            continue
        # `cache.window`, which IS `_covered_range` — memoized, not a second
        # implementation of it.
        window = cache.window(e)
        if window is None or not overlaps(window, bounds):
            continue
        # `effective_verdict`, not `effective`: this reads `[0]` and
        # discards the reason, and `effective`'s reason re-reduces the
        # archive once per partner. Same verdict, one order of growth
        # cheaper. `effective` stays imported at module scope for
        # `_cmd_pull`, which does print its reason.
        if effective_verdict(e, envelopes, tool=_TRADES_TOOL,
                             cache=cache)[0] != "complete":
            continue
        # INTERSECTED with the quarter being reported. A YEAR_TO_DATE pull
        # spans every quarter of its year, so its uncorroborated August
        # tail would otherwise be announced on the Q1 report — where no
        # figure depends on that stretch, and a warning nobody can act on
        # is how a channel stops being read. `intervals.intersect` (not a
        # hand-rolled `max(start)`/`min(end)` here) — this module's own
        # docstring names exactly that pattern as the second interval
        # implementation the spec forbids.
        for stretch in uncorroborated_stretches(e, envelopes, tool=_TRADES_TOOL,
                                                cache=cache):
            clipped = intersect(stretch, bounds)
            if clipped is not None:
                lo, hi = clipped
                alone.append(f"{e.pulled_at} -> {lo.isoformat()}..{hi.isoformat()}")
    if not alone:
        return None
    listed = "; ".join(sorted(alone)[:5])
    more = "" if len(alone) <= 5 else f" and {len(alone) - 5} more"
    # `stretch(es)`, not `pull(s)`: `alone` holds one entry per uncovered
    # STRETCH, and one pull can contribute several. Counting them as pulls
    # tells the operator a plausible, false number about how many
    # independent captures stand behind the quarter's figures.
    return (f"NOTE: {len(alone)} stretch(es) of {quarter} were observed by "
            f"only one pull ({listed}{more}). A harness that summarizes "
            "rather than truncates would be invisible there.")


def _cmd_report(args: argparse.Namespace) -> int:
    # --quarter reaches assemble()'s quarter_bounds(quarter) unvalidated —
    # it is normally skill-driven, but it arrives here through a Makefile
    # variable a person types (`make track-record QUARTER=...`), and a typo
    # is the expected way to hit this. Refuse the same way pull/tag do,
    # rather than let a raw ValueError traceback reach the user.
    # The WRITE is inside the same refusal path as `assemble` on purpose:
    # `mkdir`/`write_text` raise `OSError` (a plain file where
    # `reports/<quarter>/` should be, a full disk, a read-only mount), which
    # `except ValueError` does not catch — so a fixable output-path problem
    # reached the operator as a traceback while every other failure in this
    # CLI prints one `REFUSED:` line.
    try:
        rendered, _inputs = assemble(Path(args.root), Path(args.journal), Path(args.reports), args.quarter)
        out_path = Path(args.reports) / args.quarter / "summary.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    except (ValueError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    # AFTER the try/write, deliberately: a disclosure computed before the
    # write and failing there would cost the operator the report itself,
    # and `_corroboration_note` already fails closed to `None` on its own
    # errors — so this can never turn a working report into a refusal.
    note = _corroboration_note(Path(args.root), args.quarter)
    if note:
        print(note, file=sys.stderr)
    print(out_path.as_posix())
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    """List what the archive already holds, per tool: args, pull instant,
    and the recorded completeness. Read-only, and it never runs `report`.

    Why it exists (feedback 2026-08-29 track-record E). SKILL.md Step 2's
    gap question is "read `reports/track-record/<QUARTER>/summary.md`; if it
    is missing, pull" — but that file exists only once `report` has run, and
    the same step forbids running `report` to answer the question. For an
    operator who has never reached a quarter end the ASK therefore answers
    "pull all four quarters" every time, which on a second run the same day
    re-pulls windows that can only be duplicates and observe nothing. The
    archive already knows; nothing exposed it, so the operator hand-rolled a
    walk over `raw/*/*.json` printing exactly these three fields. That walk
    is this command.

    TWO completeness columns, and the difference is the whole point.
    `completeness` is the pulling agent's own self-report at pull time
    (SKILL.md — the CLI records what it is told and does not verify it).
    `effective` is `completeness.effective_verdict` — the SAME function
    `coverage_gap` gates on, which DEMOTES a stored-`complete` pull to
    `truncated` when an overlapping pull holds an in-window execution it
    lacks. Those two disagree in production; `archive.py` carries a
    diagnostic string ("stored complete but effectively …") for exactly that
    state. Deciding "already pulled" on the STORED field skips a quarter
    that is really truncated, and once the broker's retention window closes
    that data is gone — so `effective` is the column to read, and emitting
    the canonical resolver's own answer beats restating its rules in prose
    (producer-consumer §3). Whether a window is ADEQUATELY covered overall
    still stays `report`'s question. A tool with nothing
    archived is stated explicitly rather than omitted, for the same reason
    `unlinked` refuses to let an empty list mean "nothing to do".

    `covers_start` / `covers_end` come from `archive._covered_range` — the
    SAME resolver `coverage_gap` uses, not a second spelling of it
    (producer-consumer §3). They are the reason this command answers the
    question at all: a RELATIVE period names a different quarter depending
    on when it was pulled, so `LAST_QUARTER` pulled in 2026Q3 covers 2026Q2
    while the same string pulled in 2027Q1 covers 2026Q4 — matching the
    argument name alone mis-answers, and asking the reader to redo the
    calendar arithmetic in prose invites them to get it wrong. Blank when
    `_covered_range` does not recognise the args shape, which is its own
    fail-closed contract: an unrecognised pull contributes no coverage
    rather than a guessed window.
    """
    root = Path(args.root)
    tools = ([args.tool] if getattr(args, "tool", None)
             else sorted(KNOWN_TOOLS))
    if getattr(args, "tool", None) and args.tool not in KNOWN_TOOLS:
        print(f"REFUSED: unknown tool {args.tool!r}; known tools are "
              f"{', '.join(sorted(KNOWN_TOOLS))}", file=sys.stderr)
        return 1
    unreadable: list[str] = []
    # Proper CSV, and the `args` column is JSON — so it contains commas and
    # quotes, and `cut -d,` splits it into extra fields. Read this with a CSV
    # parser (`csv.reader`, `python3 -c`), never with `cut`.
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["tool", "args", "pulled_at", "completeness", "effective",
                     "covers_start", "covers_end"])
    for tool in tools:
        try:
            envs = read_envelopes(root, tool)
        except OSError as exc:
            print(f"REFUSED: cannot read the archive under "
                  f"{root.as_posix()}: {exc}", file=sys.stderr)
            return 1
        rows = [e for e in envs if isinstance(e, Envelope)]
        # `ParseFailure` always carries `path: Path` (archive.py), so no
        # defensive getattr chain — a missing attribute would be corruption
        # this command should not paper over.
        unreadable += [e.path.as_posix() for e in envs
                       if not isinstance(e, Envelope)]
        if not rows:
            # "no pull archived" and "no READABLE pull archived" are different
            # answers to the question this command exists for: the first says
            # go pull it, the second says a file is there and cannot be read.
            reason = "no pull archived" if not envs else "no readable pull archived"
            # A row, not prose, so one parser reads the whole output. NOTE the
            # `args` column is JSON and contains commas — parse this as CSV
            # (`csv.reader`, `python3 -c`), never `cut -d,`.
            writer.writerow([tool, "", "", reason, "", "", ""])
            continue
        # ONE memo across every row of this tool, exactly as `coverage_gap`
        # builds it — the reduction is quadratic without it. Bound to THIS
        # envelope list and dies with this call, so it can never answer a
        # later archive from a stale reduction.
        cache = ReductionCache(envs)
        for e in sorted(rows, key=lambda x: x.pulled_at):
            try:
                rng = _covered_range(e.tool, e.args, e.pulled_at)
            except (ValueError, TypeError, OverflowError):
                # Never let one odd row take the listing down: this command
                # exists to be safe to run before deciding whether to pull.
                rng = None
            start, end = ((rng[0].isoformat(), rng[1].isoformat())
                          if rng else ("", ""))
            try:
                eff = effective_verdict(e, envs, tool=tool, cache=cache)[0]
            except Exception:  # noqa: BLE001 — same rule as `pull`'s
                # A reduction that cannot be computed must not take the
                # listing down, but it must never read as `complete`
                # either: `unknown` is the conservative answer, and the
                # SKILL treats anything but `complete` as "pull it".
                eff = "unknown"
            writer.writerow([e.tool, json.dumps(e.args, sort_keys=True,
                                                separators=(",", ":")),
                             e.pulled_at, e.completeness, eff, start, end])
    if unreadable:
        print(f"NOTE: {len(unreadable)} archive file(s) could not be read "
              f"back as an envelope and are NOT reflected above — a window "
              f"they cover may look unpulled here: "
              f"{'; '.join(sorted(unreadable)[:10])}"
              + (f" (+{len(unreadable) - 10} more)" if len(unreadable) > 10
                 else ""), file=sys.stderr)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="track_record", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pull = sub.add_parser("pull", help="Archive one broker response as an envelope")
    p_pull.add_argument("--root", default=_DEFAULT_ROOT)
    p_pull.add_argument("--tool", required=True)
    p_pull.add_argument("--args", required=True, help="the call's args, as JSON")
    p_pull.add_argument("--pulled-at", required=True)
    p_pull.add_argument("--response", required=True,
                        help="file holding the raw response; a call that "
                             "produced no bytes does not invoke pull")
    p_pull.add_argument("--call-partial", action="store_true")
    p_pull.add_argument("--call-unknown", action="store_true")
    p_pull.set_defaults(func=_cmd_pull)

    p_tag = sub.add_parser("tag", help="Append one journal event (read from stdin) stamped at --at")
    p_tag.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_tag.add_argument("--at", required=True)
    p_tag.set_defaults(func=_cmd_tag)

    p_unlinked = sub.add_parser("unlinked", help="List filled orders no orders_linked event references")
    p_unlinked.add_argument("--root", default=_DEFAULT_ROOT)
    p_unlinked.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_unlinked.set_defaults(func=_cmd_unlinked)

    p_unlinked.add_argument("--since", default=None,
                            help="zero-padded ISO date (YYYY-MM-DD); list "
                                 "only orders whose last fill is on or after "
                                 "it. When it hides any, the count is "
                                 "reported — those orders stay untagged")

    p_coverage = sub.add_parser(
        "coverage",
        help="List what the archive already holds per tool (args / pulled_at "
             "/ completeness) — read-only, answers 'was this window already "
             "pulled' without running report")
    p_coverage.add_argument("--root", default=_DEFAULT_ROOT)
    p_coverage.add_argument("--tool", default=None,
                            help="narrow to one tool; default is all five")
    p_coverage.set_defaults(func=_cmd_coverage)

    p_open = sub.add_parser("open", help="List thesis_ids not yet superseded or retired")
    p_open.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_open.set_defaults(func=_cmd_open)

    p_due = sub.add_parser("due", help="List outstanding predictions past their deadline")
    p_due.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_due.add_argument("--as-of", required=True,
                       help="UTC instant to judge against; never defaults to the clock")
    p_due.set_defaults(func=_cmd_due)

    p_report = sub.add_parser("report", help="Assemble and write one quarter's summary.md")
    p_report.add_argument("--root", default=_DEFAULT_ROOT)
    p_report.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_report.add_argument("--reports", default=_DEFAULT_REPORTS)
    p_report.add_argument("--quarter", required=True)
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    """`argv=None` reads `sys.argv[1:]`, matching stdlib `argparse`
    convention. A usage error (missing required flag, unknown subcommand)
    exits 2 via argparse's own `SystemExit` — never caught here, per the
    CLI contract (0 = success, 1 = a refusal this module handled, 2 =
    usage error argparse itself raises)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
