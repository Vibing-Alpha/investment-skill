"""Archive layer for the personal track-record capture tool.

Writes each broker-tool response to disk verbatim, before anything parses
it, then reads the archive back and answers whether a quarter's pulls
actually cover a requested UTC window.

See README.md in this package.
Live-probe decisions this module compiles against (D1/D2): D1 = cadence /
args-to-UTC-range mapping / row path; D1 = no `provenance` field, because
the host only persists OVERSIZED tool results to a file, so a path is not
available for every pull.
Freeze file: .claude/skills/track-record/references/ibkr-freeze.md

This layer is generic over `tool` (a caller-supplied name, never hardcoded
here) — the skill archives five: get_account_trades, get_account_orders,
get_pa_performance_all_periods, get_account_positions and
get_account_balances (SKILL.md Step 2). Only the first three are ever read
back by `summary.py`'s computation; the last two preserve point-in-time
facts the broker does not retain and are never consumed downstream.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.track_record.intervals import intersect, overlaps, uncovered

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

# The union branch below names both, and this module had neither before it.
# `summary.py` and `scripts/track_record/__main__.py` both import this one
# rather than holding their own spelling (the CLI's copy was the third
# literal, unified here per review routing) -- so the three no longer
# drift. `test_the_frozen_tool_names_agree_across_modules` still guards
# the import wiring itself.
_TRADES_TOOL = "get_account_trades"

# The five tools D1 froze, and the ONLY names anything downstream reads.
# `write_envelope` refuses everything else, because the alternative is
# worse than it looks: `get_account_trade` (one character short) archived
# cleanly, printed its path, exited 0, and created a sixth directory no
# consumer opens — so a capture of a quarter the broker is about to drop
# reported success while preserving nothing the tool can use. There is no
# recovering it later; by then the source window has closed. A new tool
# means adding its name here, once, and the refusal says so.
KNOWN_TOOLS = frozenset({
    "get_account_trades",
    "get_account_orders",
    "get_pa_performance_all_periods",
    "get_account_positions",
    "get_account_balances",
})
# `summary.py` used to hold this assert against its own (now-removed)
# duplicate `_TRADES_TOOL` literal; the constant lives here now, so the
# check moves here with it -- a rename that misses the union branch above
# would otherwise silently orphan trades coverage rather than raising.
assert _TRADES_TOOL in KNOWN_TOOLS

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


def _fsync_dir_chain(leaf: Path) -> None:
    """`_fsync_dir` from `leaf` upward through every ancestor that exists,
    so a newly created directory's own NAME is durable in its parent too —
    unconditionally, on EVERY call, with no bound at all.

    Syncing only the leaf makes the archived FILE durable inside a
    directory whose own entry may not be. On a first-ever pull the whole
    chain — `reports/`, `track-record/`, `raw/`, `<tool>/` — is created at
    once, and a crash could discard an unsynced ancestor and take the
    response with it.

    Two bounded versions were tried before this one, and a RETRY after a
    mid-chain durability failure is what broke both — the case neither
    had a test for (Item A, cycle-5 fix wave):

    - Bounding at the archive root (`stop_above=root`, the very first
      version) stopped the walk before reaching `track-record/` and
      `reports/`, which sit ABOVE the archive root and can be just as
      newly created — so those two names were never proven durable even
      on a single, unretried call.
    - Bounding at "the deepest ancestor that already existed right before
      this call's own `mkdir`" (the version this replaces) fixed that for
      one call, by reading filesystem state before creating anything. But
      `mkdir(parents=True)` creates the WHOLE chain up front — the fsync
      failure happens strictly after, one directory at a time — so by the
      time a RETRY runs, every directory the first attempt touched
      already exists, and "what already exists" is read as "there is
      nothing left to create, so nothing left to sync". The retry re-syncs
      only the leaf, reports success, and the ancestor whose sync failed
      the first time is never revisited: a later crash can still discard
      it and everything under it, exactly the loss this function exists
      to prevent.

    There is no filesystem state this function can consult that survives
    a failed attempt and a fresh retry — "does this directory exist"
    conflates "created" with "durable", and that is precisely the
    distinction a retry needs. So this makes NO existence-based bounding
    decision at all: every call walks from `leaf` all the way to the true
    filesystem root, syncing each directory along the way, whether or not
    it happens to already be there. A retry therefore re-proves every
    ancestor `leaf` has, regardless of what an earlier, possibly-failed
    attempt managed to reach — which is what being STATELESS buys here:
    the answer never depends on remembering, or inferring, what a prior
    call did.

    The cost is a few extra `fsync` calls per pull on directories that
    were already durable — `_fsync_dir` on an unchanged directory is fast,
    and the walk itself is short (a handful of path components in every
    realistic deployment of this tool). That is the trade made on
    purpose: a few extra syscalls, in exchange for a retry that can never
    again report success while an ancestor somewhere in the chain was
    never actually synced.
    """
    seen = set()
    for d in [leaf, *leaf.parents]:
        if d in seen or not d.is_dir():
            break
        seen.add(d)
        _fsync_dir(d)
        if d.parent == d:          # filesystem root
            break


def _errnos(*names: str) -> frozenset:
    """The subset of `names` this platform actually defines. `ENOTSUP` and
    `EOPNOTSUPP` are the same value on Linux and different on some BSDs,
    and `ENOSYS` is absent on none of the targets — but building the set by
    lookup rather than by literal keeps this portable without asserting
    which of them exist here."""
    return frozenset(v for v in (getattr(errno, n, None) for n in names)
                     if v is not None)


# "This platform/filesystem cannot offer a directory sync at all" — the ONE
# fault class the best-effort behaviour below is argued for. Windows refuses
# to open a directory for reading (EACCES; EISDIR on some runtimes), and a
# FUSE/virtiofs/exFAT/network volume answers a directory fsync with
# EINVAL/ENOSYS/ENOTSUP. None of these say anything about whether the write
# reached the disk; they say the guarantee is unavailable here.
_DIR_OPEN_UNSUPPORTED = _errnos("EACCES", "EPERM", "EISDIR", "EINVAL",
                                "ENOSYS", "ENOTSUP", "EOPNOTSUPP")
_DIR_SYNC_UNSUPPORTED = _errnos("EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP")


def _fsync_dir(directory: Path) -> None:
    """Make a newly published NAME durable, not just its contents.

    The staged bytes are fsynced before the link, so the data survives a
    crash — but the directory entry that gives it a name does not until the
    DIRECTORY is synced too. `pull` printed an archived path, and a crash a
    moment later could leave the response durable and nameless: for a
    window the broker is about to close, the same as losing it.

    Best-effort for ONE fault class, and only that one: a platform that
    cannot offer the guarantee at all (`_DIR_OPEN_UNSUPPORTED` /
    `_DIR_SYNC_UNSUPPORTED`). A name that is durable in the ordinary case is
    worth more than a refusal on Windows or a FUSE mount.

    Every OTHER `OSError` — `EIO`, `ENOSPC`, `EROFS`, `EDQUOT` — is a real
    durability failure and is RAISED. It used to be swallowed under the
    paragraph above, which argues about platform capability and says
    nothing about a disk that is failing: `write_envelope` then returned a
    path, `pull` printed it, and the operator read a successful capture of a
    window the broker is about to close. Failing loudly is cheap here
    precisely because this store is append-only and nothing is ever
    overwritten — a re-pull allocates the next free `_N` name and every
    reader collapses the duplicate (`fills_from` dedups by execution key),
    so the remedy costs one repeated call and can never corrupt what is
    already archived.

    The raised error preserves the original `errno` and names what is and
    is not known: the payload may ALREADY be on disk under this name (the
    link/replace happened before this call), so `pull`'s `REFUSED:` here
    means "the name is not proven durable", not "nothing was written".
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _DIR_OPEN_UNSUPPORTED:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in _DIR_SYNC_UNSUPPORTED:
            return
        raise OSError(
            exc.errno,
            f"could not make the new directory entry durable "
            f"({exc.strerror or exc}) — the payload may already be on disk "
            f"under this name, but the name is not guaranteed to survive a "
            f"crash. Nothing was removed and nothing was overwritten; this "
            f"archive is append-only and every reader collapses a repeated "
            f"pull, so re-run the pull",
            str(directory),
        ) from exc
    finally:
        os.close(fd)


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
    published = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
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
            # half-written one — and `read_envelopes` skips a zero-byte
            # `.json` for the same reason it skips a `.part` leftover, so
            # the claim is inert rather than a permanent `ParseFailure`.
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
        # OUTSIDE both publish arms. It used to sit inside the `try` whose
        # `except OSError` IS the no-hardlink fallback, so a directory-sync
        # error — now raised rather than swallowed — would have been read as
        # "this filesystem has no hardlinks" and sent an already-published
        # file down the claim-and-replace path, where `open(path, "xb")`
        # raises `FileExistsError` and `write_bytes_new` reports the name as
        # taken. The publish decision and the durability step are two
        # different questions; only the first belongs in that handler.
        # SET BEFORE the durability step, not after: `os.link` / the
        # claim-and-replace arm above have ALREADY published the name by this
        # point, so a raising `_fsync_dir_chain` used to leave `published`
        # False and suppress the leftover NOTE on a path where a `.part`
        # really does survive. The flag answers "was the name published",
        # which is a different question from "is this call about to return
        # True" (pure-fresh probe, 2026-08-29).
        published = True
        _fsync_dir_chain(path.parent)
        return True
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            # The no-hardlink arm publishes with `os.replace(tmp, path)`, which
            # MOVES the stage file — so there is nothing left to unlink and
            # nothing to report. This arm must be caught BEFORE the handler
            # below: `FileNotFoundError` IS an `OSError`, so without it the
            # NOTE fired on every successful publish on a hardlink-less mount,
            # naming a file that does not exist and describing it as sharing an
            # inode "on the hardlink path" — the arm that did not run.
            pass
        except OSError as exc:
            # The publish itself is unaffected — the leftover is a HARDLINK
            # to (or, on the claim-and-replace arm, a sibling of) the file
            # just published, and `read_envelopes` skips `.part` names — so
            # this is never a refusal. But it was swallowed entirely, and on
            # a delete-restricted mount (Cowork FUSE returns EPERM for an
            # existing file) EVERY archive write leaves one: a weekly cron
            # grew the archive to 13 `.json` beside 13 `.part` with nothing
            # anywhere saying so (feedback 2026-08-29). Report it ONLY on
            # the published path: on the name-taken / failed paths a NOTE
            # would assert work the command did not do, which SKILL.md's
            # three-channel contract says a `NOTE:` never means.
            if published:
                print(
                    f"NOTE: the stage file {tmp.name} could not be removed "
                    f"after publishing {path.name} ({exc.strerror or exc}). "
                    f"It holds the same bytes as {path.name} (same inode on "
                    f"the hardlink path), is ignored by every reader, and "
                    f"deleting it loses nothing — this mount refuses "
                    f"unlink, so it needs cleaning up outside this tool.",
                    file=sys.stderr,
                )


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
    if tool not in KNOWN_TOOLS:
        raise ValueError(
            f"tool {tool!r} is not one of the five tools this archive is read "
            f"for ({', '.join(sorted(KNOWN_TOOLS))}) — archiving it would "
            f"succeed and then be invisible to `report` and `unlinked`. "
            f"Check the spelling; if the broker really did add a tool, add "
            f"its name to KNOWN_TOOLS in archive.py."
        )
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
        parsed = datetime.strptime(pulled_at, _CANONICAL_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"pulled_at must be canonical UTC 'Z' form YYYY-MM-DDTHH:MM:SSZ, "
            f"got {pulled_at!r}") from exc
    # `strptime` alone is not the shape check it looks like: it happily
    # accepts `2026-9-30T1:2:3Z` for this format. That value becomes the
    # FILENAME `2026930T123Z.json`, which sorts AFTER a canonical October
    # pull — and read order decides which sighting of a restated `order_id`
    # is the latest, so a September snapshot silently became the newest word
    # and the order count came out wrong. Round-tripping is the actual
    # check: a canonical string is the only one that re-formats to itself.
    if parsed.strftime(_CANONICAL_FORMAT) != pulled_at:
        raise ValueError(
            f"pulled_at must be canonical UTC 'Z' form YYYY-MM-DDTHH:MM:SSZ "
            f"with every field zero-padded, got {pulled_at!r} — an unpadded "
            f"value becomes a filename that sorts out of chronological order")
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
    # No "what already exists" check here (Item A, cycle-5 fix wave): that
    # was this function's own prior version, and it is exactly what made a
    # RETRY after a mid-chain `_fsync_dir_chain` failure unsafe — by the
    # time a retry runs, `mkdir` from the failed attempt has already
    # created every directory in the chain, so "does this exist" reads as
    # "nothing left to do" for an ancestor whose sync never actually
    # succeeded. See `_fsync_dir_chain`'s docstring for the full history.
    # `write_bytes_new` now re-syncs the whole chain unconditionally on
    # every call, so nothing needs computing before this `mkdir`.
    tool_dir.mkdir(parents=True, exist_ok=True)
    stem = _compact_timestamp(pulled_at)

    suffix = 0
    while True:
        name = f"{stem}.json" if suffix == 0 else f"{stem}_{suffix}.json"
        path = tool_dir / name
        if write_bytes_new(path, data):
            return path
        suffix += 1


def _reject_non_finite_json_constant(name: str) -> None:
    """`json.loads`'s `parse_constant` hook: refuses a bare `NaN` /
    `Infinity` / `-Infinity` literal rather than silently parsing it into a
    Python float that passes every field check below and comes back as a
    valid `Envelope`.

    `canonical_bytes` re-encodes with `allow_nan=False` and raises on
    exactly that float — the first read-side caller is
    `scripts.track_record.completeness.effective` — so without this guard
    a hand-edited or corrupted archive file carrying one of these literals
    parses cleanly HERE and only crashes later, as an uncaught
    `ValueError`, the first time something re-encodes the response. Every
    other corruption becomes a `ParseFailure` at this same load boundary;
    this one must too. Raising routes it through the existing
    `except ... ValueError` clause below, the same as `_no_duplicate_keys`.
    """
    raise ValueError(f"non-finite JSON literal {name!r} is not a valid envelope field")


def _no_duplicate_keys(pairs):
    """`json.loads` object hook that refuses a repeated member.

    The WRITER refuses these — `pull` says `duplicate key 'response'` — and
    the reader kept the last one silently, so an archive file holding two
    `response` members rendered figures out of whichever came second. The
    two ends have to agree about what a valid envelope is.
    """
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in one JSON object")
        seen[key] = value
    return seen


def _read_one(path: Path) -> Envelope | ParseFailure | None:
    """One archive file: the `Envelope` it holds, a `ParseFailure` naming
    why it could not be read, or `None` — "this is not an archive file at
    all, skip it", the same answer `read_envelopes` gives a `.part`
    leftover.

    `None` is returned for exactly one shape: a file with NO CONTENT (zero
    bytes, or nothing but whitespace). It is distinguishable from
    corruption by CONSTRUCTION rather than by heuristic — `canonical_bytes`
    always emits a non-empty JSON object, so a published envelope is never
    blank — and it has one known cause: `write_bytes_new`'s no-hardlink
    fallback claims the final name with an empty exclusive create and only
    then replaces it with the payload, so a SIGKILL in that window
    publishes an empty `.json` that `finally` never runs to remove.
    """
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
        data = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_non_finite_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        # `RecursionError` is neither a decode error nor an OSError, and
        # `json.loads` raises it on input nested a few thousand deep. It
        # escaped `read_envelopes` and `report` as a traceback, writing no
        # summary at all — where one unreadable archive file should cost
        # that tool's figures and nothing else. Fixed for the CLI's stdin
        # in an earlier cycle and not here; same call, other side.
        if not text.strip():
            # A blank archive file is not corrupted data — it is a crashed
            # write that published nothing (see this function's docstring).
            # It USED to become a `ParseFailure` saying so, and that told
            # the operator the truth while leaving the damage in place: ANY
            # `ParseFailure` makes `coverage_gap` unavailable for the whole
            # tool in EVERY later quarter, so a single kill in that window
            # cost every future report its trades figures until a human
            # deleted the file by hand — in a store whose first behavioural
            # constraint is that it deletes nothing. The successful retry
            # was sitting beside it as `..._1.json` the whole time.
            #
            # So it is SKIPPED instead, and the advice the old message gave
            # ("deleting it loses no data and restores this tool's
            # coverage") is not lost so much as discharged: there is
            # nothing left to restore. The fail-closed direction is
            # preserved — the file contributes NO coverage, so if its
            # window really was never pulled, `coverage_gap` still refuses
            # that quarter, on its own evidence, rather than poisoning
            # every other one.
            return None
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

    # The READER has to hold the same line as the writer. `write_envelope`
    # refuses a non-canonical `pulled_at`; this checked only that it was a
    # string, so a file placed by hand — or written by an older version, or
    # edited — was read as a valid envelope and its trades counted, while
    # `pull` would have rejected the identical content. The timestamp is
    # also what orders the archive, and an unreadable one has no place in
    # that order at all.
    try:
        parsed_at = datetime.strptime(data["pulled_at"], _CANONICAL_FORMAT)
        canonical = parsed_at.strftime(_CANONICAL_FORMAT) == data["pulled_at"]
    except (ValueError, TypeError):
        canonical = False
    if not canonical:
        return ParseFailure(
            path=path,
            reason=(f"pulled_at {data['pulled_at']!r} is not the canonical "
                    "UTC 'Z' form this archive is written in — `pull` would "
                    "have refused this file"),
        )

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


def _archive_sort_key(path):
    """`(stem, collision index)` for one archive filename.

    `20260818T120000Z_10.json` must follow `..._2.json`, and does not under
    a plain string sort. Anything that does not match the collision shape
    sorts by its whole name at index -1, ahead of its own suffixed
    siblings, which is where the un-suffixed original belongs.
    """
    name = path.name
    if name.endswith(".json"):
        stem = name[: -len(".json")]
        head, sep, tail = stem.rpartition("_")
        if sep and tail.isdigit():
            return (head, int(tail))
        return (stem, -1)
    return (name, -1)


def read_envelopes(root: Path, tool: str) -> list:
    """Read back every archived pull for one tool.

    An unparseable ARCHIVE file is reported as a `ParseFailure`, never
    raised, skipped, or deleted (behavioural constraint 3) — the caller
    decides what to do with it. What counts as an archive file is a
    PUBLISHED `*.json` THAT HOLDS SOMETHING: a leftover `.part` stage file
    from an interrupted `write_bytes_new` is not one (a NAME rule, decided
    here), and neither is a blank file — the zero-byte name-claim its
    no-hardlink fallback leaves on a SIGKILL (a CONTENT rule, decided by
    `_read_one`, which returns `None` for it). Both are skipped, never
    deleted. Reading either would turn an unpublished fragment into a
    `ParseFailure` and poison coverage for this tool in every later quarter
    — the opposite of what constraint 3 exists to protect.

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
        if tool_dir.exists():
            # A FILE sitting where the tool's directory belongs is not an
            # empty archive, and the difference decides what to tell the
            # operator: `unlinked` said "no pull is archived — run the pull
            # step first", and `pull` then failed with `File exists`. The
            # advice could not succeed. Reported as a parse failure, which
            # is what it is: something is there and cannot be read as an
            # archive.
            return [ParseFailure(
                path=tool_dir,
                reason=("a FILE occupies this tool's archive directory — "
                        "nothing can be archived or read under it until "
                        "that path is cleared"),
            )]
        return []
    results: list = []
    # Sorted by (stem, collision index), not lexicographically. Same-second
    # pulls get `<name>.json`, `<name>_1.json`, `<name>_2.json`, ... and a
    # plain sort puts `_10` before `_2` — which matters because "the latest
    # sighting wins" for a restated `order_id` reads this order as
    # chronological. Eleven pulls in one second is not something a
    # human-driven session does, but the ordering is now a correctness
    # assumption rather than a display one, and a numeric key removes it
    # instead of documenting it.
    for p in sorted(tool_dir.iterdir(), key=_archive_sort_key):   # a stable first pass
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
        if parsed is None:
            # An UNPUBLISHED claim wearing the final name — see `_read_one`.
            # Skipped for the same reason and on the same terms as the
            # `.part` leftover above: it is not an archive file, and reading
            # it as one turned a payload that never landed into a permanent
            # `ParseFailure` that gapped this tool's coverage in every later
            # quarter. Never deleted; this archive removes nothing.
            continue
        if isinstance(parsed, Envelope) and parsed.tool != tool:
            parsed = ParseFailure(
                path=p,
                reason=(
                    f"tool mismatch: archived under directory {tool!r} but "
                    f"the envelope's own tool field declares {parsed.tool!r}"
                ),
            )
        results.append(parsed)
    # Re-sorted by each envelope's OWN `pulled_at`, not by its filename.
    # The two normally agree — `write_envelope` derives the name from the
    # stamp — but a rename breaks that silently, and read order decides
    # which sighting of a restated `order_id` counts as the latest. A file
    # renamed `a.json` was read first whatever it said about itself, and
    # the order count followed the rename. Parse failures keep their
    # front of the list, ahead of every readable envelope: a ParseFailure has
    # no `pulled_at`, so its key starts empty. That is where they belong —
    # every consumer checks for them before reading anything else, and they
    # refuse everything downstream anyway — but the previous comment here
    # said they kept their filename position, which is not what the key
    # does. Six ordering assumptions in this file have gone wrong; a comment
    # describing the opposite of the code is how the seventh would.
    return sorted(results, key=lambda e: (
        getattr(e, "pulled_at", "") or "", _archive_sort_key(e.path)))


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


# The rolling `period` values D1 did not freeze but the tool has always
# accepted. Their exact boundary is not settled by the schema's wording
# ("a rolling N-day window ending today (UTC)"), so each is modelled as the
# INTERSECTION of every reading: start at `pulled_at - (N-1) days`, end at
# midnight of the pull's own date. Plus one second on the start, so the
# interval is half-open like every other one in this package.
#
# The direction of every uncertainty here is toward claiming LESS. Spec
# 2026-08-19 Section 2 derives it and records that enumerating readings is
# not a proof — a positive-evidence probe is what would widen it, and
# nothing in this code reads that probe's result.
_ROLLING_DAYS = {"DAYS_7": 7, "DAYS_30": 30, "DAYS_60": 60, "DAYS_90": 90}


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
      names the covered quarter directly, CAPPED at `pulled_at` (a pull
      cannot have observed a quarter's future). Recognized for every tool
      (not just get_account_trades) so the given fixtures, which use this
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

    # EXACTLY the frozen shape, one recognised key and nothing beside it.
    # The rule was already written and argued for `args == {}` on the
    # performance tool, three branches down; these two read only their own
    # key. `{"period": "LAST_QUARTER", "limit": 1}` is what that costs:
    # `limit` caps the response, so the pull holds a truncated view of the
    # quarter while claiming to cover all of it, and one archived row
    # renders as `1 fill` for a quarter that had four hundred. An extra
    # argument means this is not the call D1 froze, and the frozen call is
    # the entire basis for saying what window a pull covers.
    if set(args) - {"quarter"} and set(args) - {"period"}:
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
            # CAPPED AT THE PULL INSTANT, like every other shape here. This
            # branch returned the WHOLE quarter regardless of `pulled_at`,
            # so a pull made in the middle of a quarter certified the rest
            # of it — including its future — as observed. The failure that
            # buys is invisible: coverage_gap finds no hole, and the report
            # renders a low, entirely plausible fill count for a quarter
            # half of which nothing was ever pulled for, with no
            # `unavailable` naming it. `YEAR_TO_DATE` one branch below has
            # always been capped this way; this shape simply was not.
            #
            # `intersect`, not a hand-rolled `min` — `intervals.py` owns
            # interval arithmetic (module docstring: a caller confining one
            # interval to another must not compare endpoints by hand). It
            # also answers the degenerate case correctly: a pull stamped at
            # or before the quarter's start observed none of it and gets
            # `None`, the fail-closed direction, rather than an inverted
            # range a later reader has to know to drop.
            bounds = _quarter_bounds(int(m.group(1)), int(m.group(2)))
            try:
                pulled_dt = _parse_utc(pulled_at)
            except ValueError:
                # fail-open-ok: same as the two branches below — an
                # unparseable pulled_at contributes NO coverage, never
                # coverage of the requested window.
                return None
            return intersect(bounds, (bounds[0], pulled_dt))
        return None

    period = args.get("period")

    # `args == {}` EXACTLY, not merely "no period". D1 froze this call as
    # the empty object, and its coverage is inferred from `pulled_at` rather
    # than from anything the args say — so treating every period-less args
    # shape as that call meant `{"unexpected_arg": true}`, which does not
    # match the issued call at all, was granted full coverage and rendered
    # RISK and CONTEXT as numbers. An envelope whose args are not the frozen
    # ones cannot be shown to cover anything.
    if args == {} and tool == _PERFORMANCE_TOOL:
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

    # `period` reaches here straight out of the agent-supplied envelope,
    # and `write_envelope` checks only that `args` is a dict. A list or a
    # dict is unhashable, so `.get(period)` raised `TypeError` out of
    # `report` — a traceback and no summary at all, on a tool whose whole
    # contract is that an input it cannot read becomes `unavailable
    # (reason)` and every other figure still renders. An unrecognized
    # period already means "contributes no coverage"; an unusable one is
    # the same answer.
    if not isinstance(period, str):
        return None

    midnight = datetime(pulled_dt.year, pulled_dt.month, pulled_dt.day,
                        tzinfo=timezone.utc)
    if period in _ROLLING_DAYS:
        span = timedelta(days=_ROLLING_DAYS[period] - 1)
        return pulled_dt - span + timedelta(seconds=1), midnight
    if period == "MONTH_TO_DATE":
        return datetime(pulled_dt.year, pulled_dt.month, 1,
                        tzinfo=timezone.utc), midnight
    if period == "TODAY":
        return midnight, midnight

    offset = _PERIOD_QUARTER_OFFSET.get(period)
    if offset is None:
        return None
    year, quarter = _quarter_containing(pulled_dt)
    return _quarter_bounds(*_quarter_minus(year, quarter, offset))


def _no_parens(text: str) -> str:
    """`text` with parentheses squared off.

    Every reason this module produces is rendered INSIDE `unavailable (...)`,
    and the report's own "a refused field renders no value" check strips
    parenthesised reasons with a matcher that cannot pair nested ones — an
    inner pair leaves the outer opener stripped and the message's digits
    exposed as though they were a figure. Reasons this module writes are
    paren-free by hand; a `JSONDecodeError`'s text is not (`... (char 1)`),
    and that one arrives from outside.
    """
    return text.replace("(", "[").replace(")", "]")


# The hour, in UTC, at or after which a pull on a quarter's last calendar
# day is past that day's US equity close — PER QUARTER, because the answer
# differs and a single conservative value refuses a legitimate pull.
#
# US DST runs from the 2nd Sunday of March to the 1st Sunday of November,
# fixed by law, so every quarter-end falls on the same side of it every
# year: Mar 31 / Jun 30 / Sep 30 are always EDT (16:00 ET = 20:00 UTC) and
# Dec 31 is always EST (21:00 UTC). A flat 21:00 was the first version and
# it refused a Q1-Q3 pull taken at 20:30 — after that day's close, exactly
# what the skill tells the operator to do — leaving RISK and CONTEXT
# unavailable with a valid pull sitting in the archive.
_US_CLOSE_UTC_HOUR_BY_MONTH = {3: 20, 6: 20, 9: 20, 12: 21}


def _range_covers(tool: str, covered, bounds) -> bool:
    """Whether one pull's own args-derived range covers `bounds`.

    The performance tool is judged on the CALENDAR DAY its range ends on
    rather than the exclusive end INSTANT — see `coverage_gap`'s docstring
    for why that relaxation exists and why it is not extended to the others.

    One function because there were two copies: the gate below, and the
    diagnostic that names a pull which "would be covered ... but is not
    marked complete". A drifted copy there would name a pull that in fact
    covers nothing, or stay silent about one that does — a message about
    coverage that disagrees with the coverage answer beside it. The
    identical shape cost this package a whole quarter's figures two cycles
    ago, in `_baseline_qualifies`.
    """
    if covered is None:
        return False
    start, end = bounds
    c_start, c_end = covered
    if c_start > start:
        return False
    if tool == _PERFORMANCE_TOOL:
        last_day = (end - _ONE_DAY).date()
        if c_end.date() > last_day:
            return True
        # Walk back over Saturdays and Sundays. When a quarter ends on a
        # weekend the final session is the Friday before it, and a pull
        # taken after THAT close — exactly what the skill instructs — was
        # dated before the quarter's last calendar day and given no
        # coverage at all: RISK and CONTEXT unavailable for doing the
        # documented thing. Twelve quarter-ends fall on a weekend between
        # 2026 and 2035 — a fixed window, not "the next ten years", which
        # gives a different count depending on when it is read.
        #
        # No market calendar is needed for this direction. Every day
        # skipped is provably not a trading day, so the day reached is the
        # LATEST the final session can have been; a holiday could move it
        # earlier still, which leaves the bar stricter than necessary and
        # never looser.
        while last_day.weekday() >= 5:            # 5 = Saturday, 6 = Sunday
            last_day -= _ONE_DAY
        if c_end.date() > last_day:
            return True
        # ON the quarter's last calendar day, the pull must also be after
        # that day's US equity close. `2026-12-31T00:01Z` satisfies a
        # date-only reading and is the evening of the PRIOR US session, so
        # its series stops one session short — and when the SPY pin is
        # fetched at the same moment it stops there too, the two validate
        # each other, and the quarter's final session is missing from both
        # figures with nothing to say so. For Q4 that loss is permanent:
        # the YTD baseline resets on January 1.
        #
        # SKILL.md already demands this in prose ("AFTER the quarter's own
        # FINAL TRADING SESSION has closed, not merely once the calendar
        # date has rolled"); the relaxation is what let the code disagree
        # with it. 21:00 UTC is the close in EST (Dec 31) and an hour
        # after it in EDT (Mar/Jun/Sep 30) — the conservative side on
        # purpose: refusing a pull taken in that hour on a Q1/Q2/Q3 close
        # costs a re-pull, accepting one taken before the close costs a
        # wrong figure.
        floor = _US_CLOSE_UTC_HOUR_BY_MONTH.get((end - _ONE_DAY).date().month)
        if floor is None:
            # `bounds` did not come from `quarter_bounds`. Nothing in this
            # package produces such a range, and guessing a close time for
            # an arbitrary month is exactly the invention this gate exists
            # to prevent.
            return False
        return c_end.date() == last_day and c_end.hour >= floor
    return end <= c_end


def coverage_gap(tool: str, envelopes: list, bounds: tuple) -> str | None:
    """`None` when `bounds` (a half-open (start, end) UTC pair) is fully
    covered by at least one `complete` envelope's own args-derived range;
    otherwise a reason starting `unavailable (coverage gap:`.

    `tool` IS read directly by this function's own logic (Task 4): the
    union branch below dispatches on `tool == _TRADES_TOOL`, restricts
    every envelope it counts to `e.tool == tool` (an envelope of some
    OTHER tool contributes nothing, however its own args-derived window
    happens to line up), and threads `tool` through to
    `completeness.effective`. It is a required, tool-FIRST parameter
    regardless of that, because an empty envelope list, or one holding
    only `ParseFailure`s, carries no tool name to infer from -- exactly
    the two cases where the union branch above still needs to know which
    rule to apply. Tool first so a stale two-argument call raises
    `TypeError` instead of silently passing `envelopes` as the tool.

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
    pull whose own date is on or after that last TRADING day, AND at or
    after that day's US equity close if it is the same day, has genuinely
    observed every value the quarter contains, so that is the bar. The
    close-hour half is not decoration: a pull stamped `00:01Z` on the
    quarter's last calendar day satisfies a date-only reading and is the
    evening of the PRIOR session, so its series stops one day short — and
    this docstring described exactly that pull as satisfying the rule until
    `_range_covers` was corrected to refuse it. This
    matters most for Q4: `cps`'s baseline resets every January 1st (D1), so
    a pull taken strictly after the boundary (to satisfy the instant check)
    covers none of the quarter that just closed — there is no way to
    "pull later" and recover it, unlike every other quarter.

    A pull taken EARLY on the quarter's final calendar day satisfies this
    rule while its series stops the day before, so RISK and CONTEXT would
    be wrong by that session's entire move. This was carried as a KNOWN
    LIMIT for six review cycles on the grounds that catching it needs a
    market calendar — requiring the final calendar day's own observation
    refuses any quarter ending on a weekend or holiday, and requiring a
    pull dated after the quarter end is what the Q4 paragraph above rules
    out.

    CLOSED, in `assemble`, without a calendar: the pinned SPY series IS
    one. yfinance returns trading days, so a pinned close dated after the
    account's last in-quarter observation and still inside the quarter
    proves the market traded on a day the account series does not cover,
    and both figures refuse by name. It only ever adds a refusal, and only
    when the pin is readable; when it is not, `render` still prints the
    observed endpoints beside both figures so the short window is visible. Deliberately
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
            # Carry the ParseFailure's own reason through. Without it the
            # report said only "unparseable", which reads the same for a
            # truncated 300KB response and for a zero-byte file left by an
            # interrupted write — and only one of those is safe to delete.
            # No parentheses in the appended text: this whole string renders
            # inside `unavailable (...)` and the report's own
            # refusal-renders-no-value check cannot pair nested ones.
            return (
                "unavailable (coverage gap: archive contains an unparseable "
                f"file {e.path.as_posix()} — {_no_parens(e.reason)} — its args cannot be "
                "read, so coverage of this window cannot be confirmed)"
            )

    if tool == _TRADES_TOOL:
        # The union, for this tool only. `_range_covers`'s performance
        # branch carries a close-hour floor and a weekend walk-back that
        # are not interval arithmetic; merging them would drop both
        # silently, which is why the tool is a parameter and not an
        # inference.
        #
        # LOCAL import, and it is not laziness: `completeness` imports the
        # fill normalizer from `summary`, which imports this module, so a
        # module-level import here is a cycle. Importing inside the one
        # branch that needs it keeps `archive` importable on its own.
        from scripts.track_record.completeness import (
            ReductionCache, effective_verdict)

        # ONE memo for BOTH reductions below. `effective_verdict` re-encodes
        # every response and re-normalizes every row of every partner, and
        # this branch asks it once per envelope and then AGAIN once per
        # envelope for the `would_cover` diagnostic — over an append-only
        # archive that gains a pull per session. Threaded, not global:
        # `ReductionCache` is bound to THIS `envelopes` list and dies with
        # this call, so it can never answer a later archive from a stale
        # reduction. Identical answer, one reduction per envelope.
        cache = ReductionCache(envelopes)

        # `effective_verdict`, not `effective`: this branch reads `[0]`
        # twice per envelope and never shows a reason, and `effective`'s
        # reason costs one more reduction of the whole archive per partner
        # — measured 1.1s -> 58s at 60 sessions on an archive of mutually
        # corroborating pulls. Identical verdict, one less order of growth.
        #
        # EFFECTIVE, not stored. A pull archived before a fuller one was
        # stored `complete` because it had no partner at the time; reading
        # the stored value here would let it keep contributing its window
        # after the fuller pull proves it short, and the count renders low
        # and plausible. The verdict is recomputed against the archive as
        # it stands, so later evidence demotes it automatically.
        windows = [
            rng for e in envelopes
            # `e.tool == tool`: `effective`'s own precedence 1b returns the
            # STORED verdict, unexamined, for any envelope whose tool isn't
            # this one -- so without this clause a foreign-tool envelope
            # stored `complete` (e.g. a performance pull, `args == {}`)
            # would pass the verdict check and then `_covered_range(e.tool,
            # ...)` would derive ITS OWN [Jan 1, pulled_at] window from IT,
            # contributing that window to TRADES coverage. Not reachable
            # through summary.py today (it reads per-tool), but coverage_gap
            # must not depend on a caller never mixing tools.
            if isinstance(e, Envelope) and e.tool == tool
            and effective_verdict(e, envelopes, tool=tool, cache=cache)[0] == "complete"
            and (rng := cache.window(e)) is not None
        ]
        holes = uncovered(bounds, windows)
        if not holes:
            return None
        named = ", ".join(f"{s.isoformat()}..{e.isoformat()}" for s, e in holes[:3])
        more = "" if len(holes) <= 3 else f" and {len(holes) - 3} more"

        # The same diagnostic the single-envelope path below carries, and
        # it is NOT optional: `test_a_coverage_gap_names_the_pull_that_
        # would_have_covered_it` uses get_account_trades, so returning
        # early without it breaks a committed test — and the message it
        # pins is the one that tells an agent which field it got wrong.
        # Widened to the union's terms: a non-complete pull is worth
        # naming when it OVERLAPS a hole, not only when it would have
        # covered the whole window alone.
        #
        # EFFECTIVE, not stored, matching the `windows` filter above (and
        # matching the gate, not the field). A pull STORED `complete` but
        # demoted by a fuller partner used to be invisible here: the old
        # `e.completeness != "complete"` test reads a field that is still
        # "complete" on such a pull, so it was excluded from both the union
        # AND the diagnostic — a bare gap with a file marked complete
        # sitting right over it and no way to tell. Names the effective
        # verdict, distinct from the stored one, when the two disagree.
        # `e.tool == tool` for the same reason as `windows` above.
        would_cover = sorted(
            (f"{e.path.name} marked {e.completeness}"
             if e.completeness != "complete"
             else f"{e.path.name} stored complete but effectively {verdict}")
            for e in envelopes
            if isinstance(e, Envelope) and e.tool == tool
            and (verdict := effective_verdict(e, envelopes, tool=tool,
                                              cache=cache)[0]) != "complete"
            and (r := cache.window(e)) is not None
            and any(overlaps(r, h) for h in holes)
        )
        detail = ""
        if would_cover:
            detail = ("; it would be covered by " + ", ".join(would_cover)
                      + ", but only a pull archived as complete counts")
        return (
            "unavailable (coverage gap: no complete pull covers "
            f"{named}{more}{detail})"
        )

    for e in envelopes:
        if not isinstance(e, Envelope):
            continue
        # Same latent exposure as the union branch above, for the same
        # reason: without this, a foreign-tool envelope's own args-derived
        # window could satisfy `_range_covers` for `bounds` under a rule
        # that was never the requested tool's.
        if e.tool != tool:
            continue
        if e.completeness != "complete":
            continue
        if _range_covers(e.tool, _covered_range(e.tool, e.args, e.pulled_at), bounds):
            return None

    # Name what was in the archive and why it did not count. The bare
    # window told an agent that supplied `completeness: "unknown"` nothing
    # about which field it had supplied — the same message covers "you
    # marked the only covering pull uncertain" and "your args or pulled_at
    # placed a complete pull over a different window", which need opposite
    # remedies.
    # No nested parentheses in this text. The whole reason renders INSIDE
    # `unavailable (...)`, and the report's own "a refused field renders no
    # value" check strips parenthesised reasons with a regex that cannot
    # pair nested ones — an inner pair leaves the outer opener stripped and
    # this message's digits exposed as if they were a figure.
    would_cover = sorted(
        f"{e.path.name} marked {e.completeness}"
        for e in envelopes
        if isinstance(e, Envelope) and e.tool == tool and e.completeness != "complete"
        and _range_covers(e.tool, _covered_range(e.tool, e.args, e.pulled_at), bounds)
    )
    detail = ""
    if would_cover:
        detail = ("; it would be covered by " + ", ".join(would_cover)
                  + ", but only a pull archived as complete counts")
    return (
        "unavailable (coverage gap: no complete pull covers "
        f"{start.isoformat()}..{end.isoformat()}{detail})"
    )
