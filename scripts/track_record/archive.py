"""Archive layer for the personal track-record capture tool.

Writes each broker-tool response to disk verbatim, before anything parses
it, then reads the archive back and answers whether a quarter's pulls
actually cover a requested UTC window.

Design: docs/superpowers/plans/2026-08-16-track-record.md (Task 2).
Live-probe decisions this module compiles against (D1/D2): D1 = cadence /
args-to-UTC-range mapping / row path; D1 = no `provenance` field, because
the host only persists OVERSIZED tool results to a file, so a path is not
available for every pull.
Freeze file: docs/superpowers/plans/2026-08-16-track-record-freeze.md

This layer is generic over `tool` (a caller-supplied name, never hardcoded
here) — the skill archives five: get_account_trades, get_account_orders,
get_pa_performance_all_periods, get_account_positions and
get_account_balances (SKILL.md Step 2). Only the first three are ever read
back by `summary.py`'s computation; the last two preserve point-in-time
facts the broker does not retain and are never consumed downstream.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_VALID_COMPLETENESS = frozenset({"complete", "truncated", "unknown"})

# Canonical UTC "Z" form, the one `pulled_at` spelling SKILL.md Step 2 tells
# the operator to emit (`date -u +%Y-%m-%dT%H:%M:%SZ`). Named here rather
# than inlined at the one use site because the other three modules each name
# their own copy (`journal._RECORDED_AT_FORMAT`,
# `summary._CANONICAL_FORMAT`, `__main__._CANONICAL_FORMAT`) — a fourth
# spelling hidden inside a call is exactly the shape that drifts unnoticed.
_CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# Required top-level keys of an envelope, per the frozen shape
# {tool, args, pulled_at, completeness, response}.
_REQUIRED_FIELDS = ("tool", "args", "pulled_at", "completeness", "response")

# D1 (freeze §"args -> UTC range mapping"): get_account_trades `period` reaches
# back N completed calendar quarters relative to the pull's own `pulled_at`.
# LAST_QUARTER = the quarter immediately before the one containing pulled_at.
_PERIOD_QUARTER_OFFSET = {
    "LAST_QUARTER": 1,
    "TWO_QUARTERS_AGO": 2,
    "THREE_QUARTERS_AGO": 3,
    "FOUR_QUARTERS_AGO": 4,
}

# Test-only placeholder arg shape (brief §"Behavioural constraints" item 4):
# {"quarter": "2026Q3"} names the covered quarter directly, independent of
# pulled_at. Kept alongside the real `period` mapping above so the given
# tests (which use this shape) and real production pulls (which use
# `period`) are both honored by one coverage_gap.
_QUARTER_ARG_RE = re.compile(r"^(\d{4})Q([1-4])$")

# SKILL.md Step 2 calls get_pa_performance_all_periods with `{}` — no
# `period`, no `quarter` key at all (unlike get_account_trades, which always
# carries one of the two). D1 states this tool returns the same range
# get_account_trades's own `YEAR_TO_DATE` does: [Jan 1 of pulled_at's year,
# pulled_at]. Without this, `{}` matches neither shape in `_covered_range`
# below, coverage_gap always reports a gap, and RISK/CONTEXT can never
# render in production (found on whole-branch review, B1).
_PERFORMANCE_TOOL = "get_pa_performance_all_periods"

# One calendar day, used only to
# convert a quarter's EXCLUSIVE end instant (e.g. 2027-01-01T00:00:00Z) to
# its own LAST calendar day (2026-12-31) for the performance-tool coverage
# relaxation in `coverage_gap` below.
_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Envelope:
    """One archived tool pull, read back from disk.

    No `provenance` field: the harness only persists OVERSIZED tool
    results to a file (see module docstring), so a path is not available
    for every pull, and a field present on some envelopes and absent on
    others is worse than no field at all.
    """

    tool: str
    args: dict
    pulled_at: str
    completeness: str
    response: Any
    path: Path


@dataclass(frozen=True)
class ParseFailure:
    """An archive file that could not be read back as a valid envelope.

    Reported, never raised, never skipped, never deleted (brief
    behavioural constraint 3) — the archive is the only record of what
    was pulled, so a corrupt file must stay visible.
    """

    path: Path
    reason: str


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic UTF-8 JSON encoding: sorted keys, compact separators,
    non-ASCII preserved, no trailing newline.

    `allow_nan=False` already rejects nested non-finite values (inf/-inf/
    nan at any depth) by raising ValueError from the encoder itself — no
    separate pre-scan is added.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def write_bytes_new(path: Path, data: bytes) -> bool:
    """Create `path` holding exactly `data`; return False if it already exists.

    Both write-once files in this tool — an archived envelope and the pinned
    benchmark CSV — were written straight to their final path. A write
    interrupted partway (ENOSPC, a quota hit, a killed process; a trades
    response runs past 150KB per D1) therefore left a PARTIAL file at a path
    that is never deleted and never rewritten. For the archive that is
    unrecoverable by retry: the retry allocates the NEXT free name while the
    corrupt original keeps turning into a `ParseFailure`, and one
    `ParseFailure` makes `coverage_gap` unavailable for that tool in every
    later quarter, until a human deletes the file by hand.

    So the payload is written to a same-directory temp file, flushed and
    fsynced, and only then published onto the final name with `os.link` —
    which fails with `FileExistsError` if the name is taken, preserving the
    exclusive-create property the previous `open(path, "xb")` provided. The
    temp file is removed on every RETURNING path, success or failure.

    It is NOT removed when the process dies outright (SIGKILL, power loss) —
    `finally` does not run then, and that is precisely the case atomic
    publishing exists to survive. So the guarantee is narrower than "leaves
    nothing behind", and is stated exactly: **nothing incomplete is ever
    published under an archive name.** A leftover keeps its `.part` suffix,
    and `read_envelopes` reads only published `.json` files — without that
    filter a leftover would become a `ParseFailure` and reintroduce, under a
    new filename, the very permanent-poisoning this function removed.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=path.name + ".", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            # Hardlinks are not universal. A Cowork virtiofs/FUSE mount
            # (SKILL.md's prelude supports Cowork explicitly), an
            # exFAT/network volume or a Windows share returns EPERM/ENOSYS
            # here. Unhandled, that escaped `write_envelope` and made `pull`
            # REFUSE for all five tools — the tool's whole purpose failing
            # closed on a supported platform, where the `open(path, "xb")`
            # this replaced had worked.
            #
            # Fall back to: claim the name with an exclusive create (the
            # same primitive as before, so exclusivity is unchanged), then
            # `os.replace` the finished stage file onto it. The window this
            # loses is narrower than the one it keeps: a SIGKILL between
            # claim and replace leaves a ZERO-BYTE file rather than a
            # half-written one, and `read_envelopes` reads only `.json`, so
            # neither is ever published as a valid envelope.
            try:
                with open(path, "xb"):
                    pass
            except FileExistsError:
                return False
            try:
                os.replace(tmp, path)
            except OSError:
                # Never leave the 0-byte claim behind: it would be an
                # unparseable archive file, and those are never deleted.
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _compact_timestamp(pulled_at: str) -> str:
    """`2026-08-16T09:00:00Z` -> `20260816T090000Z` (freeze file's compact
    convention). Used only to name the archive file; the stored `pulled_at`
    field keeps whatever string the caller passed, verbatim."""
    return pulled_at.replace("-", "").replace(":", "")


def write_envelope(
    root: Path,
    tool: str,
    args: dict,
    pulled_at: str,
    completeness: str,
    response: Any,
) -> Path:
    """Write one archived pull under `root/<tool>/`.

    Never truncates an existing path (behavioural constraint 2): on a
    filename collision, allocates the next free `_N` suffix rather than
    overwrite. The exclusivity is `write_bytes_new`'s `os.link` publish,
    which raises `FileExistsError` on a taken name — so a same-instant race
    between two writers can never silently drop one. (It was `open(path,
    "xb")` until the payload moved behind an atomic publish; the property
    is the same, the mechanism is not, and naming the old one here would
    send a reader looking for code that no longer exists.)

    Four of the five envelope fields are validated here, because the CLI's
    `pull` is the untrusted entry point for all of them (model-produced
    stdin, not yet schema-checked) and because every one of them is a field
    `_read_one` will later demand on the way back in — a writer that accepts
    what its own reader rejects produces a file that is permanently
    unreadable and, since archive files are never deleted, permanently
    poisons `coverage_gap` for that tool:

    - `tool` — non-empty `str` (bool excluded: a subtype of `int`, not of
      `str`) AND a plain path component, since it names the subdirectory
    - `pulled_at` — non-empty `str`; it names the archive filename
    - `args` — an object, exactly as `_read_one` requires
    - `completeness` — one of the three frozen values, checked `isinstance`
      first so an unhashable value is a `ValueError` and not a raw
      `TypeError`

    `response` is deliberately NOT validated: it is the broker payload,
    stored verbatim by design, and this layer takes no position on its shape.
    """
    if isinstance(tool, bool) or not isinstance(tool, str) or tool == "":
        raise ValueError(f"tool must be a non-empty string, got {tool!r}")
    # `tool` becomes a path component (`Path(root) / tool`). Unconstrained,
    # `"../x"` writes a sibling of the archive root and an ABSOLUTE value
    # makes pathlib discard `root` outright — both land artifacts outside
    # `<ROOT>/reports/`, which `.claude/rules/skill-architecture.md` #9
    # forbids: an artifact silently relocated out of the tree is worse than
    # a visibly failed run, because nothing can ever find it again.
    if Path(tool).name != tool or tool in (".", ".."):
        raise ValueError(f"tool must be a plain name, not a path: {tool!r}")
    if "\\" in tool:
        # Checked separately: on POSIX `Path("x\\y").name` is the whole
        # string, so the check above passes — but this archive is read on
        # Windows too, where the same value IS a separator.
        raise ValueError(f"tool must be a plain name, not a path: {tool!r}")
    if isinstance(pulled_at, bool) or not isinstance(pulled_at, str) or pulled_at == "":
        raise ValueError(f"pulled_at must be a non-empty string, got {pulled_at!r}")
    # `pulled_at` becomes a path component too — `_compact_timestamp` strips
    # only "-" and ":", so "/", "\" and ".." survive into the FILENAME, with
    # the same consequence as an escaping `tool`: an absolute value makes
    # pathlib discard the tool directory entirely. Guarded on the COMPACTED
    # form, since that is what is actually joined. (`tool` alone was guarded
    # when this check was added; a guard is only as good as the sites it
    # covers, and these two sit three lines apart.)
    # Format, not just shape. `pulled_at` was checked non-empty and
    # path-safe but never PARSED, so `'not-a-time'` and
    # `'2026-13-45T99:99:99Z'` archived fine, and a bare `'-'` compacted to
    # the empty string and wrote a stem-less hidden `.json`. Anything
    # unparseable also silently contributes NO coverage later, three layers
    # downstream of where the operator could still fix it. SKILL.md Step 2
    # specifies `date -u +%Y-%m-%dT%H:%M:%SZ`; that is the contract, so
    # enforce it here — which also removes the offset/naive spellings whose
    # timezone `_parse_utc` would otherwise have to assume.
    try:
        datetime.strptime(pulled_at, _CANONICAL_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"pulled_at must be canonical UTC 'Z' form YYYY-MM-DDTHH:MM:SSZ, "
            f"got {pulled_at!r}") from exc
    # `args` was the one required field the writer did not check while its
    # own reader demands an object. A non-dict wrote successfully, exited 0,
    # and became a permanent `ParseFailure` — and since `coverage_gap` turns
    # ANY ParseFailure into unavailable for the whole tool, and archive
    # files are never deleted, one such pull disabled every later quarter
    # for that tool until a human deleted the file by hand.
    if not isinstance(args, dict):
        raise ValueError(f"args must be an object, got {type(args).__name__}")
    # `x not in <frozenset>` raises a bare `TypeError: unhashable type` on a
    # list/dict value, before the enum check can name the field — and
    # TypeError is outside `_cmd_pull`'s refusal arm, so model-produced
    # stdin could make `pull` traceback instead of printing REFUSED.
    if not isinstance(completeness, str) or completeness not in _VALID_COMPLETENESS:
        raise ValueError(
            f"invalid completeness {completeness!r}: must be one of "
            f"{sorted(_VALID_COMPLETENESS)}"
        )

    envelope = {
        "tool": tool,
        "args": args,
        "pulled_at": pulled_at,
        "completeness": completeness,
        "response": response,
    }
    data = canonical_bytes(envelope)

    tool_dir = Path(root) / tool
    tool_dir.mkdir(parents=True, exist_ok=True)
    stem = _compact_timestamp(pulled_at)

    suffix = 0
    while True:
        name = f"{stem}.json" if suffix == 0 else f"{stem}_{suffix}.json"
        path = tool_dir / name
        if write_bytes_new(path, data):
            return path
        suffix += 1


def _read_one(path: Path) -> Envelope | ParseFailure:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError subclass, not an OSError — a
        # write killed mid-multibyte (e.g. process killed writing a 300KB
        # envelope) leaves invalid UTF-8 on disk, and this must become a
        # ParseFailure like every other unreadable archive file, never an
        # uncaught exception out of read_envelopes (I3, whole-branch review).
        return ParseFailure(path=path, reason=f"unreadable file: {exc}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParseFailure(path=path, reason=f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return ParseFailure(path=path, reason="archive file is not a JSON object")

    missing = [f for f in _REQUIRED_FIELDS if f not in data]
    if missing:
        return ParseFailure(
            path=path, reason=f"missing envelope field(s): {', '.join(missing)}"
        )

    if not isinstance(data["tool"], str) or not isinstance(data["pulled_at"], str):
        return ParseFailure(path=path, reason="tool/pulled_at must be strings")

    if not isinstance(data["args"], dict):
        return ParseFailure(path=path, reason="args must be an object")

    # `isinstance` first, for the same reason as the writer's copy of this
    # check: membership against a frozenset raises `TypeError: unhashable
    # type` on a list/dict, which would escape `read_envelopes` instead of
    # becoming the ParseFailure every other malformed field produces.
    if (not isinstance(data["completeness"], str)
            or data["completeness"] not in _VALID_COMPLETENESS):
        return ParseFailure(
            path=path, reason=f"invalid completeness: {data['completeness']!r}"
        )

    return Envelope(
        tool=data["tool"],
        args=data["args"],
        pulled_at=data["pulled_at"],
        completeness=data["completeness"],
        response=data["response"],
        path=path,
    )


def read_envelopes(root: Path, tool: str) -> list:
    """Read back every archived pull for one tool.

    An unparseable ARCHIVE file is reported as a `ParseFailure`, never
    raised, skipped, or deleted (behavioural constraint 3) — the caller
    decides what to do with it. What counts as an archive file is a
    PUBLISHED `*.json`: a leftover `.part` stage file from an interrupted
    `write_bytes_new` is not one, and IS skipped. Reading it would turn an
    unpublished fragment into a `ParseFailure` and poison coverage for this
    tool in every later quarter — the opposite of what constraint 3 exists
    to protect.

    Also verifies each successfully parsed envelope's own SELF-DECLARED
    `tool` field agrees with `tool`, the directory it was just read from.
    Before this check, `coverage_gap` picked its
    coverage rule from `e.tool` — the envelope's own claim — with nothing
    upstream ever confirming that claim matched where the file actually
    lives. A `get_account_trades` pull mislabeled (by hand, by a bug, or by
    a misplaced file) as `get_pa_performance_all_periods` would then be
    judged under the performance tool's relaxed calendar-date coverage
    check instead of trades' strict instant-containment check — bypassing
    the exact strictness this tool's real data depends on, independent of
    the January truncation relaxation (F1). A mismatch degrades to a
    `ParseFailure` naming BOTH the directory and the self-declared value —
    the same fail-closed shape (retained, reported, never silently
    dropped) every other unreadable archive file already takes.
    """
    tool_dir = Path(root) / tool
    if not tool_dir.is_dir():
        return []
    results: list = []
    for p in sorted(tool_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix != ".json":
            # An archive file is a PUBLISHED `.json`. `write_bytes_new`
            # stages under `<name>.json.<rand>.part` and unlinks in a
            # `finally`, which does not run on SIGKILL or power loss — the
            # very events atomic publishing exists to survive. Reading a
            # leftover would turn a half-written stage file into a
            # `ParseFailure`, and one ParseFailure makes coverage
            # unavailable for this tool in EVERY later quarter: the exact
            # failure the atomic write removed, back under a new name. A
            # COMPLETE leftover (killed after the write, before the link)
            # is just as wrong — it would parse as a real Envelope and be
            # counted as a second, phantom pull. Skipped, never deleted:
            # this archive removes nothing.
            continue
        parsed = _read_one(p)
        if isinstance(parsed, Envelope) and parsed.tool != tool:
            parsed = ParseFailure(
                path=p,
                reason=(
                    f"tool mismatch: archived under directory {tool!r} but "
                    f"the envelope's own tool field declares {parsed.tool!r}"
                ),
            )
        results.append(parsed)
    return results


def _parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp that may use the `Z` suffix
    (Python's `fromisoformat` only accepts `Z` from 3.11; this repo
    targets 3.10+)."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _quarter_bounds(year: int, quarter: int) -> tuple:
    """Half-open UTC bounds of one calendar quarter, matching the
    convention the caller's own `_bounds` test helper uses."""
    month = quarter * 3 - 2
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if quarter == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 3, 1, tzinfo=timezone.utc)
    return start, end


def _quarter_containing(dt: datetime) -> tuple:
    return dt.year, (dt.month - 1) // 3 + 1


def _quarter_minus(year: int, quarter: int, offset: int) -> tuple:
    """`offset` completed calendar quarters before (year, quarter)."""
    zero_based = (year * 4 + (quarter - 1)) - offset
    return zero_based // 4, zero_based % 4 + 1


def _covered_range(tool: str, args: dict, pulled_at: str):
    """The UTC range one archived pull's `args` actually covers, or None
    if the args shape isn't recognized (fail-closed: an unrecognized pull
    contributes no coverage rather than being guessed at).

    `tool` is threaded through (B1, whole-branch review) so a tool whose
    real production args carry neither `quarter` nor `period` — currently
    only `get_pa_performance_all_periods`, called with `{}` per SKILL.md
    Step 2 — can still be given real coverage instead of being permanently
    gapped. This must NOT be extended to `get_account_orders`: D1 froze
    that tool NOT RETAINED, so its permanent gap is correct (and already
    harmless — `orders_linked` is compiled-disabled regardless).

    Shapes recognized, per brief behavioural constraint 4:
    - `{"quarter": "YYYYQn"}` — the test placeholder for D1's real shape;
      names the covered quarter directly. Recognized for every tool (not
      just get_account_trades) so the given fixtures, which use this
      shape on all three archived tools, keep working unchanged.
    - `{"period": "LAST_QUARTER" | "TWO_QUARTERS_AGO" | ... |
      "YEAR_TO_DATE"}` — the real get_account_trades argument (D1), read
      relative to the pull's own `pulled_at`, never to "now".
    - `{}` (no `quarter`, no `period`) on `get_pa_performance_all_periods`
      — D1: same range as `YEAR_TO_DATE`, [Jan 1 of pulled_at's year,
      pulled_at]. Any other tool with this args shape still gets no
      coverage (fail-closed default preserved).
    """
    if not isinstance(args, dict):
        return None

    quarter_arg = args.get("quarter")
    if "quarter" in args and not isinstance(quarter_arg, str):
        # Present but not a string. Falling through from here reached the
        # `period is None and tool == _PERFORMANCE_TOOL` branch below and
        # granted a full [Jan 1 .. pulled_at] window — coverage awarded on
        # the strength of an args shape this function did not recognise,
        # the exact inverse of "an unrecognised pull contributes no
        # coverage rather than being guessed at".
        return None
    if isinstance(quarter_arg, str):
        m = _QUARTER_ARG_RE.match(quarter_arg)
        if m:
            return _quarter_bounds(int(m.group(1)), int(m.group(2)))
        return None

    period = args.get("period")

    if period is None and tool == _PERFORMANCE_TOOL:
        try:
            pulled_dt = _parse_utc(pulled_at)
        except ValueError:
            # fail-open-ok: see the matching comment below — an unparseable
            # pulled_at can never be silently treated as covering anything.
            return None
        return datetime(pulled_dt.year, 1, 1, tzinfo=timezone.utc), pulled_dt

    if period is None:
        return None

    try:
        pulled_dt = _parse_utc(pulled_at)
    except ValueError:
        # fail-open-ok: returning None here means this envelope contributes
        # NO coverage — the fail-closed direction. coverage_gap only ever
        # narrows on this path; an unparseable pulled_at can never be
        # silently treated as covering the requested window.
        return None

    if period == "YEAR_TO_DATE":
        return datetime(pulled_dt.year, 1, 1, tzinfo=timezone.utc), pulled_dt

    offset = _PERIOD_QUARTER_OFFSET.get(period)
    if offset is None:
        return None
    year, quarter = _quarter_containing(pulled_dt)
    return _quarter_bounds(*_quarter_minus(year, quarter, offset))


def coverage_gap(envelopes: list, bounds: tuple) -> str | None:
    """`None` when `bounds` (a half-open (start, end) UTC pair) is fully
    covered by at least one `complete` envelope's own args-derived range;
    otherwise a reason starting `unavailable (coverage gap:`.

    Reads only the envelope list the caller already holds — never the
    archive root — so the coverage answer and the counted rows come from
    one snapshot (behavioural constraint 4 / brief interface note).

    Any `ParseFailure` present makes the whole answer unavailable
    (behavioural constraint 6): its `args` cannot be read, so there is no
    way to tell whether it would have covered `bounds` or not, and
    absence of that information cannot be treated as coverage.

    For `_PERFORMANCE_TOOL` only, the
    upper bound is judged on the CALENDAR DATE the pull's own range ends on,
    not the exclusive end INSTANT `bounds[1]` names. `_covered_range` gives
    this tool's own end as `pulled_at` itself (its response is a daily
    series read up to the moment it was pulled) — that instant is always
    some hours before the quarter's exclusive end boundary (`bounds[1]` is
    UTC midnight on the first day of the NEXT quarter), so requiring
    `end <= c_end` can never be satisfied by a same-quarter pull, including
    one taken on the quarter's own last calendar day. For a daily series, a
    pull whose own date is on or after that last calendar day has genuinely
    observed every value the quarter contains, so that is the bar. This
    matters most for Q4: `cps`'s baseline resets every January 1st (D1), so
    a pull taken strictly after the boundary (to satisfy the instant check)
    covers none of the quarter that just closed — there is no way to
    "pull later" and recover it, unlike every other quarter. Deliberately
    NOT extended to `_TRADES_TOOL` / `_ORDERS_TOOL`: those tools' `args`
    name an exact, self-contained period range (e.g. `LAST_QUARTER` covers
    precisely the quarter it claims to, verbatim, even across a year
    boundary — see `test_last_quarter_rolls_over_the_year_boundary`), so
    relaxing the instant there would hide a genuine truncation instead of
    working around a daily series' own observation cadence.
    """
    start, end = bounds

    for e in envelopes:
        if isinstance(e, ParseFailure):
            return (
                "unavailable (coverage gap: archive contains an unparseable "
                f"file {e.path.as_posix()} — its args cannot be read, so "
                "coverage of this window cannot be confirmed)"
            )

    for e in envelopes:
        if not isinstance(e, Envelope):
            continue
        if e.completeness != "complete":
            continue
        covered = _covered_range(e.tool, e.args, e.pulled_at)
        if covered is None:
            continue
        c_start, c_end = covered
        if e.tool == _PERFORMANCE_TOOL:
            final_calendar_day = (end - _ONE_DAY).date()
            if c_start <= start and c_end.date() >= final_calendar_day:
                return None
        elif c_start <= start and end <= c_end:
            return None

    return (
        "unavailable (coverage gap: no complete pull covers "
        f"{start.isoformat()}..{end.isoformat()})"
    )
