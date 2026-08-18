"""CLI for the personal track-record capture tool: `pull | tag | unlinked |
open | report`. Every quarterly NUMBER and every money-path gate decision
lives in `archive.py` / `journal.py` / `summary.py` — `pull`, `tag`, `open`
and `report` only parse arguments, read/write the one file each command
touches, and print the one thing that command promises. `_cmd_unlinked` is
the one exception: it groups archived fill rows by `(account, order_id)`
and subtracts the journal's already-linked set — a real computation, owned
here rather than in `summary.py`, because it is CLI-specific presentation
(which orders still need a decision), not one of the six quarterly numbers
`summary.py` assembles.

Design: docs/superpowers/plans/2026-08-16-track-record.md (Task 8; `open`
added at Task 9 review round 1, so the skill's "what's currently open"
question is answered by a tested library function — `journal.open_theses`
— rather than untested set logic embedded in SKILL.md prose
(`.claude/rules/skill-architecture.md` #1/#3)).
Freeze decisions this module compiles against (see freeze file, D1/D2/D4):
`unlinked` reads order keys off the archived **trade** rows via
`fills_from` — D1 froze `get_account_orders` as NOT RETAINED (it reports
only live working orders), so an order snapshot can never supply the list
of filled orders. It prints `account,order_id,date,side,size,symbol,price`
(seven columns): D4 found no stable instrument id on a trade row, only
`symbol`, so identity is still by symbol, not an id — but the account/
order_id machine key stays first, and `date`/`side`/`size`/`price` are what let a human actually recognize which
trade a line is, printed most-recent-first. Before this fix `unlinked`
printed only `account,order_id,symbol`, which a person tagging hundreds of
orders a quarter cannot tell apart without cross-referencing the broker by
hand.

Freeze file: docs/superpowers/plans/2026-08-16-track-record-freeze.md
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.track_record.archive import Envelope, write_envelope, read_envelopes
from scripts.track_record.journal import JournalError, append_event, open_theses
from scripts.track_record.summary import assemble, fills_from, fold_or_reason

# D1's frozen tool name for the archive subdirectory `unlinked` reads —
# matches `summary._TRADES_TOOL`, kept as a local constant here (that name
# is private to summary.py, and the two producer-consumer sides of this
# frozen string are close enough together to review for drift by eye).
_TRADES_TOOL = "get_account_trades"

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
    return json.loads(sys.stdin.read())


def _cmd_pull(args: argparse.Namespace) -> int:
    try:
        envelope = _read_stdin_json()
        if not isinstance(envelope, dict):
            raise ValueError(f"stdin envelope must be a JSON object, got {type(envelope).__name__}")
        missing = [f for f in ("tool", "args", "pulled_at", "completeness", "response") if f not in envelope]
        if missing:
            raise ValueError(f"stdin envelope missing field(s): {', '.join(missing)}")
        path = write_envelope(
            Path(args.root),
            envelope["tool"],
            envelope["args"],
            envelope["pulled_at"],
            envelope["completeness"],
            envelope["response"],
        )
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(path.as_posix())
    return 0


def _cmd_tag(args: argparse.Namespace) -> int:
    try:
        event = _read_stdin_json()
        if not isinstance(event, dict):
            raise ValueError(f"stdin event must be a JSON object, got {type(event).__name__}")
        recorded_at = _canonical_z(args.at)
    except (json.JSONDecodeError, ValueError) as exc:
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
    return 0


def _cmd_unlinked(args: argparse.Namespace) -> int:
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
    # A NOTE on stdERR, never a gate and never a nonzero exit. This command
    # prints a to-do list, and it deliberately reads every archived pull
    # without filtering on `completeness` — that value is the agent's own
    # self-report (SKILL.md: the CLI records what it is told and does not
    # verify it), so refusing on it would block the backlog on an
    # unverifiable claim. But an operator tagging from a SHORT list has no
    # way to know it is short, and the empty case is worse: no output plus
    # exit 0 reads as "nothing left to tag". So state the fact and let the
    # reader judge it — evidence, not a verdict.
    incomplete = sorted({e.completeness for e in trades_envelopes
                         if isinstance(e, Envelope) and e.completeness != "complete"})
    if incomplete:
        print(f"NOTE: this list is built from archived pulls, some of which are "
              f"marked {', '.join(incomplete)} — orders they missed cannot appear "
              f"below, and an empty list does not prove there is nothing to tag.",
              file=sys.stderr)

    fills = fills_from(trades_envelopes)
    if isinstance(fills, str):
        print(f"REFUSED: {fills}", file=sys.stderr)
        return 1

    fold = fold_or_reason(Path(args.journal))
    if isinstance(fold, str):
        print(f"REFUSED: {fold}", file=sys.stderr)
        return 1

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
                                 "summable": True, "fills": 0}
        agg["fills"] += 1
        if row["trade_time"] > agg["latest"]["trade_time"]:
            agg["latest"] = row
        size, price = row["size"], row["price"]
        # A drifted or missing field must not silently become a wrong total.
        # Fall back to the latest fill's own raw values and say so, rather
        # than sum what cannot be summed.
        if (isinstance(size, (int, float)) and not isinstance(size, bool)
                and isinstance(price, (int, float)) and not isinstance(price, bool)):
            agg["size"] += size
            agg["notional"] += size * price
        else:
            agg["summable"] = False

    # Sorted by trade_time descending — most recent first, the order a
    # person actually scans a backlog in, not alphabetically by order id.
    entries = [(key, agg) for key, agg in by_key.items() if key not in linked]
    entries.sort(key=lambda item: item[1]["latest"]["trade_time"], reverse=True)

    # csv.writer, not an f-string join: a field containing a comma (a symbol
    # like "ACME, INC", or a drifted non-scalar) used to split into an extra
    # column and shift every field after it — a row that still looks
    # well-formed while meaning something else.
    writer = csv.writer(sys.stdout, lineterminator="\n")
    unsummable: list[str] = []
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
        if agg["fills"] > 1 and (not agg["summable"] or not agg["size"]):
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
            price = round(agg["notional"] / agg["size"], 10)
        if not _emit([account, order_id, date, latest["side"], size,
                      latest["symbol"], price]):
            return 0

    if unsummable:
        print(f"NOTE: {len(unsummable)} order(s) could not be summed (a fill's "
              f"size or price was non-numeric, or the fills sum to zero), so "
              f"their size/price columns show that order's LAST FILL, not the "
              f"whole order: {'; '.join(unsummable)}", file=sys.stderr)
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
    print(out_path.as_posix())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="track_record", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pull = sub.add_parser("pull", help="Archive one tool-response envelope read from stdin")
    p_pull.add_argument("--root", default=_DEFAULT_ROOT)
    p_pull.set_defaults(func=_cmd_pull)

    p_tag = sub.add_parser("tag", help="Append one journal event (read from stdin) stamped at --at")
    p_tag.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_tag.add_argument("--at", required=True)
    p_tag.set_defaults(func=_cmd_tag)

    p_unlinked = sub.add_parser("unlinked", help="List filled orders no orders_linked event references")
    p_unlinked.add_argument("--root", default=_DEFAULT_ROOT)
    p_unlinked.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_unlinked.set_defaults(func=_cmd_unlinked)

    p_open = sub.add_parser("open", help="List thesis_ids not yet superseded or retired")
    p_open.add_argument("--journal", default=_DEFAULT_JOURNAL)
    p_open.set_defaults(func=_cmd_open)

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
