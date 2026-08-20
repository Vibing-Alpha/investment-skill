# scripts/track_record/completeness.py
"""Effective completeness, computed at READ time over the whole archive.

A verdict computed when a pull is archived goes stale: archive a short pull
first and it has no partner, is stored `complete`, and stays complete after
a fuller pull arrives and proves it short — while coverage keeps trusting
the stored value. Two operators pulling in a different order then hold
different permanent facts, and one renders a low count that looks entirely
plausible.

So the stored `completeness` is only what the agent alone could see, and
this module answers the other question whenever anyone asks it, against the
archive as it stands.

NON-RECURSIVE by construction, in two places that need separate
arguments:

* `effective_verdict` asks a partner for the ids it holds, never for that
  partner's own verdict. Two mutually deficient pulls therefore cannot
  form a cycle, and no answer depends on traversal order.
* `uncorroborated_stretches` DOES ask each partner for a verdict, and
  asks `effective_verdict` for it rather than `effective`. That is the whole
  reason the two are separate functions: `effective` calls
  `uncorroborated_stretches` to elaborate one reason string, so a
  partner verdict taken from `effective` would close the loop
  `effective -> uncorroborated_stretches -> effective` and two
  overlapping corroborating pulls would recurse until the interpreter
  gave up. `effective_verdict` never calls either, so the call graph stays a
  DAG. Do not "simplify" that call to `effective`.
"""

from __future__ import annotations

from scripts.track_record.archive import Envelope, ParseFailure, _covered_range, canonical_bytes
from scripts.track_record.intervals import overlaps, uncovered
from scripts.track_record.summary import _TRADES_TOOL, _normalize_fill_row, _parse_canonical


UNCORROBORATED = "no other pull held any of this pull's in-window executions"
# ROW-based (see the note on `uncorroborated_stretches` below for why this is
# a deliberately different question from that function's WINDOW-based one).


class ReductionCache:
    """One CALL's memo of the reductions this module repeats.

    `_corroboration_note` asks for one verdict per candidate, and then per
    candidate `uncorroborated_stretches` asks for one verdict per partner:
    n^2 verdicts over the same n envelopes, each re-encoding every response
    and re-normalizing every row. That is a CUBE over an APPEND-ONLY
    archive that grows by a pull per session — measured 26.6s at 30 daily
    `DAYS_30` pulls — and it runs AFTER `summary.md` is on disk, so a run
    slow enough to abandon leaves the operator a plausible-looking report
    and no disclosure. That is precisely the failure the disclosure exists
    to prevent, so the cost class is the correctness problem here.

    Four memos. Three are pure functions of ONE envelope and are keyed by
    that envelope's identity; the fourth is the verdict:

    * `canonical_bytes(e.response)` — per envelope
    * `_covered_range(e.tool, e.args, e.pulled_at)` — per envelope
    * `_ids(e, window)` — per envelope AND WINDOW
    * `effective_verdict(e, envelopes, tool=)` — per envelope and tool

    **The window is half the `_ids` key and that is the risky part of this
    class.** `_ids` filters rows to the window it is handed, so a key of
    the envelope alone answers one candidate's window from another
    candidate's — silently, with a verdict that is simply a wrong number
    and that no timing measurement can see. `tests/
    test_track_record_completeness.py::test_the_reduction_cache_keys_
    partner_ids_by_window` fails the moment the window leaves the key.

    Keyed by `id(...)`, which is sound ONLY because every object keyed is
    alive for the whole call — `envelopes` holds the partners and the
    `candidate` parameter holds the candidate — so no key can be recycled
    onto a different object mid-call. `Envelope` cannot be the key itself:
    it is a frozen dataclass over a `dict` and an arbitrary response, so
    it is unhashable, which is also why `functools.lru_cache` is not an
    option here.

    Bound to ONE envelope list and checked on every use (`for_list`). The
    archive is append-only and grows BETWEEN calls; a memo that outlived
    its list would answer a newer archive out of a stale reduction, which
    is worse than the slowness it removes. So: never module-level, never
    stored, never returned — build one inside a call and drop it there.
    """

    __slots__ = ("_envelopes", "_len", "blobs", "windows", "row_ids", "verdicts")

    def __init__(self, envelopes: list) -> None:
        self._envelopes = envelopes
        self._len = len(envelopes)
        self.blobs: dict = {}
        self.windows: dict = {}
        self.row_ids: dict = {}
        self.verdicts: dict = {}

    def for_list(self, envelopes: list):
        """This cache, if `envelopes` is the list it was built against AND
        still has the length it had then.

        Fail-closed on anything else. A cache is only correct for the
        snapshot it memoized, and answering a different list from it is
        the same stale-fact failure this module exists to remove. Identity
        alone is not enough: the archive list can be APPENDED to in place
        between cache population and reuse (the identity check passes —
        it is still the same object), and a verdict cached before the
        append is a verdict about a shorter archive than the one it now
        answers for. Length is a cheap, sufficient witness that the list
        changed; it does not need to say how.
        """
        if envelopes is not self._envelopes:
            raise ValueError(
                "a ReductionCache is bound to the envelope list it was built "
                "from; pass that same list, or build a new cache")
        if len(envelopes) != self._len:
            raise ValueError(
                f"a ReductionCache is bound to the envelope list as it stood "
                f"when built ({self._len} envelope(s)); it now has "
                f"{len(envelopes)} — build a new cache instead of reusing "
                f"this one against a list that grew")
        return self

    def canonical(self, envelope) -> bytes:
        """`canonical_bytes(envelope.response)`, computed once."""
        key = id(envelope)
        if key not in self.blobs:
            self.blobs[key] = canonical_bytes(envelope.response)
        return self.blobs[key]

    def window(self, envelope):
        """`_covered_range` for `envelope`, computed once.

        `in`, not `.get(...) is None`: `None` is this function's own
        fail-closed answer for an unrecognised args shape, so a sentinel
        read of `None` as "not cached yet" would recompute it every time
        for exactly the envelopes a big archive holds most of.
        """
        key = id(envelope)
        if key not in self.windows:
            self.windows[key] = _covered_range(
                envelope.tool, envelope.args, envelope.pulled_at)
        return self.windows[key]

    def row_ids_in(self, envelope, bounds):
        """`_ids(envelope, bounds, ...)`, computed once per (envelope, WINDOW).

        `bounds` IS in the key — see the class docstring for why dropping
        it is a wrong-number bug rather than a cache miss. It is a pair of
        aware `datetime`s, so it hashes, and two spellings of one instant
        hash together, which is the semantics `_ids` itself compares by.

        The `path` argument `_ids` takes is not in the key because it is
        not free: every call site passes the envelope's OWN path, so this
        method derives it here rather than accept one that could vary.
        """
        key = (id(envelope), bounds)
        if key not in self.row_ids:
            self.row_ids[key] = _ids(envelope, bounds, envelope.path.as_posix())
        return self.row_ids[key]


def _cache_for(envelopes: list, cache):
    """The caller's cache checked against `envelopes`, or a fresh one.

    Every entry point takes `cache=None` and lands here, so a standalone
    call keeps its old behaviour exactly (a memo that lives and dies
    inside that one call) while a caller running the reduction over a
    whole archive can thread ONE memo through and pay for each reduction
    once.
    """
    return ReductionCache(envelopes) if cache is None else cache.for_list(envelopes)


def _ids(envelope, bounds, path):
    """The in-window `trade_id`s of one envelope, or a reason it cannot say.

    Returns `(ids, None)` or `(None, reason)`. A recognised but EMPTY row
    list yields an empty set — the broker said there were none, which is
    evidence. No row list at all yields a reason: those rows are not zero,
    they are unknown.
    """
    response = envelope.response
    rows = response.get("trades") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        return None, f"{path} carries no trades row list"
    ids = set()
    for row in rows:
        normalized = _normalize_fill_row(row, envelope.path)
        if isinstance(normalized, str):
            return None, f"{path} has a row that cannot be read: {normalized}"
        # Instants, not ISO text. `_canonical_utc` does guarantee a single
        # `...Z` form, so string comparison happens to work today — but it
        # works by a coincidence of field width and offset, and `due`
        # refuses that same shortcut for the same reason.
        # `_parse_canonical` reads exactly the form `_normalize_fill_row`
        # guarantees; it is not a second normalization pass.
        when = _parse_canonical(normalized["trade_time"])
        if bounds[0] <= when < bounds[1]:
            ids.add(normalized["execution_id"])
    return ids, None


def effective_verdict(candidate, envelopes: list, *, tool: str,
                      cache=None) -> tuple[str, str | None]:
    """`effective`'s answer, with step 6's corroborated reason left unwritten.

    Returns `(verdict, reason)` exactly as `effective` does, EXCEPT on the
    one path where the reason names uncorroborated stretches: there it
    returns `("complete", None)` and leaves the naming to `effective`.
    `None` is therefore the caller's signal "ask
    `uncorroborated_stretches`", and nothing else returns it.

    Split out so `uncorroborated_stretches` has a verdict to filter
    partners on that does not call it back — see the module docstring.
    It is PUBLIC because that split gave every caller who wants only the
    verdict (`[0]`) a reason to stop paying for a reason string nobody
    reads: `effective` now costs a whole extra archive reduction per
    partner on its corroborated path, so `coverage_gap` and
    `_corroboration_note` calling `effective(...)[0]` turned a measured
    quadratic into a cubic — worst case (many mutually corroborating
    pulls) 1.1s -> 58s at 60 sessions, on the path that decides whether a
    quarter renders a figure. Same verdict, same arguments; only the
    unread reason is skipped. Reach for `effective` only when the reason
    is going to be shown to someone.

    Precedence, in order — spec 2026-08-19 Section 4:

    1. stored observation is not `complete` -> that stored status
    1b. the candidate is not the trades tool -> the stored status. No
       containment test applies; spec Section 3 scopes the union to one
       tool, and Section 6 leaves these four with the agent.
    2. the candidate's own window cannot be derived -> `unknown`
    3. the candidate's own rows cannot be read -> `unknown`
    4. an overlapping pull holds an in-window id the candidate lacks ->
       `truncated`. Positive proof outranks every uncertainty below it.
    5. a same-tool non-duplicate partner that is underivable, or that the
       AGENT CALLED COMPLETE and whose rows cannot be read -> `unknown`.
       A partner already stored non-complete cannot contribute
       unreadability: it is its own declared uncertainty, and in an
       append-only archive counting it would demote every overlapping
       pull forever
    6. otherwise -> `complete`. If NO overlapping partner ever held a
       matching in-window id, the reason is the bare `UNCORROBORATED`
       constant — a window merely brushing the candidate's is not a
       contribution. Otherwise the reason names any stretch of this pull's
       own window that no complete pull observed (`uncorroborated_stretches`
       gives the exact stretches; `report` calls it directly for that detail
       rather than parsing this reason string). That is spec Section 5's
       residual stated precisely: the newest tail of a rolling pull has no
       earlier observer by construction, and a summarizing harness is
       invisible exactly there.

    `cache` is an optional `ReductionCache` (see its docstring). Passing
    one across a whole archive's worth of candidates is what turns
    `_corroboration_note`'s cube back into a square; passing none keeps
    the old behaviour, one call's memo. It is a pure optimization —
    identical verdict, identical reason.
    """
    cache = _cache_for(envelopes, cache)
    key = (id(candidate), tool)
    # `in`, not a truthiness or `is not None` test: every answer here is a
    # 2-tuple, but a sentinel read would be one more place for a miss to
    # look like a hit.
    if key not in cache.verdicts:
        cache.verdicts[key] = _effective_verdict(
            candidate, envelopes, tool=tool, cache=cache)
    return cache.verdicts[key]


def _effective_verdict(candidate, envelopes: list, *, tool: str,
                       cache) -> tuple[str, str | None]:
    """`effective_verdict`'s body, called once per (candidate, tool) memo miss.

    Split out ONLY so the memo above has somewhere to wrap; the precedence
    and every reason string are documented on `effective_verdict`. It
    never calls `effective_verdict`, `effective`, or
    `uncorroborated_stretches`, so the call graph stays the DAG the module
    docstring requires.
    """
    if candidate.completeness != "complete":
        return candidate.completeness, "the agent recorded it that way"

    # Trades only. Spec 2026-08-19 leaves "completeness for the four
    # non-trades tools, where no containment test applies" with the agent,
    # and Section 3 scopes union coverage to one tool for the same reason.
    # Without this gate every positions / performance / orders pull
    # classifies `unknown` — their responses carry no trades row list, so
    # `_ids` refuses — and `pull` then prints a NOTE on four of the five
    # tools every session. That teaches the operator to ignore the one
    # channel carrying a real truncation warning. Verified by running it.
    if candidate.tool != _TRADES_TOOL:
        return candidate.completeness, (
            "no containment test applies to this tool: its rows are not "
            "executions and its coverage is not a union, so the agent's "
            "observation is the whole answer")

    window = cache.window(candidate)
    if window is None:
        return "unknown", ("this pull's own window cannot be derived from its "
                           "args, so there is nothing to restrict rows to")

    mine, why = cache.row_ids_in(candidate, window)
    if mine is None:
        return "unknown", why

    own_bytes = cache.canonical(candidate)
    uncertain = []
    # A partner's WINDOW overlapping is not the same as it CONTRIBUTING —
    # `uncorroborated_stretches` (below) is a pure window-overlap reduction
    # (Task 11's `report` needs exactly that, over a whole quarter), and
    # using it alone here mislabels a partner whose window merely brushes
    # the candidate's while every one of its OWN rows falls elsewhere as a
    # "partial corroborator". Tracked instead by whether ANY partner's
    # `theirs` (restricted to the candidate's own window, computed above)
    # is non-empty: that is a real matching id, not a coincidence of dates.
    corroborated = False
    for other in envelopes:
        if isinstance(other, ParseFailure):
            uncertain.append(f"{other.path.as_posix()} could not be parsed")
            continue
        if not isinstance(other, Envelope) or other.tool != tool:
            continue
        if other.path == candidate.path:
            continue
        if cache.canonical(other) == own_bytes:
            continue          # duplicate: it cannot corroborate itself
        other_window = cache.window(other)
        if other_window is None:
            # The SAME stored-complete qualifier the unreadable-rows branch
            # below carries, for the same recorded reason: an archived
            # error object -- which the skill instructs `--call-unknown`
            # for -- need not carry args any window can be derived from
            # either, and it is its own already-declared uncertainty, not
            # new uncertainty about THIS pull. Without the qualifier that
            # one file demotes every overlapping pull to `unknown` for the
            # archive's life (nothing is ever deleted), coverage refuses
            # the quarter, and no amount of re-pulling can fix it.
            #
            # The alarm case is kept intact here too: a partner the agent
            # called COMPLETE whose window cannot be derived is a genuine
            # contradiction and still makes the candidate `unknown`.
            if other.completeness == "complete":
                uncertain.append(f"{other.path.as_posix()} has no derivable window")
            continue
        if not overlaps(window, other_window):
            continue          # derivably disjoint: it could not have held a row here
        theirs, why = cache.row_ids_in(other, window)
        if theirs is None:
            # Only a partner the AGENT called COMPLETE can make this
            # candidate uncertain by being unreadable. One already called
            # truncated or unknown — an archived error object, which the
            # skill instructs `--call-unknown` for — is not new uncertainty
            # about THIS pull; it is its own, already declared.
            #
            # Without this qualification the archive poisons itself
            # permanently: one error response archived under the capture
            # rule demotes every overlapping pull to `unknown` forever
            # (nothing is ever deleted), so coverage refuses the quarter
            # and no amount of re-pulling can fix it. FOUND BY RUNNING IT —
            # pull an error object with --call-unknown, then a perfectly
            # good trades pull, and the good one comes back `unknown`.
            #
            # The alarm case is kept intact: a partner the agent called
            # COMPLETE whose rows cannot be read is a genuine contradiction
            # and still makes the candidate `unknown`.
            if other.completeness == "complete":
                uncertain.append(why)
            continue
        if theirs:
            corroborated = True
        missing = theirs - mine
        if missing:
            return "truncated", (
                f"{other.path.as_posix()} holds {len(missing)} in-window "
                f"execution(s) this pull lacks, e.g. {sorted(missing)[0]}")

    if uncertain:
        return "unknown", "; ".join(uncertain[:3])

    # Zero partners ever held a matching in-window id: a different fact
    # from partial corroboration, and `report` discloses the difference —
    # so it gets the bare, exact `UNCORROBORATED` reason rather than a
    # stretch list that would, in this case, just spell out the
    # candidate's own full window.
    if not corroborated:
        return "complete", UNCORROBORATED

    # `None`, not a reason: the per-STRETCH message belongs to `effective`,
    # which owns the one call to `uncorroborated_stretches`. Writing it
    # here would make this function call that one, and that one already
    # calls this one — see the module docstring.
    return "complete", None


def effective(candidate, envelopes: list, *, tool: str,
              cache=None) -> tuple[str, str]:
    """`(verdict, reason)` for `candidate`, given the archive `envelopes`.

    `effective_verdict` above decides; this adds the one reason it deliberately
    leaves unwritten. Precedence and every other reason string are
    documented on `effective_verdict`.

    One `cache` (see `ReductionCache`) serves BOTH calls below — the
    verdict and, on the one path that needs it, the stretch elaboration —
    so the second never re-reduces what the first already did.
    """
    cache = _cache_for(envelopes, cache)
    verdict, reason = effective_verdict(candidate, envelopes, tool=tool, cache=cache)
    if reason is not None:
        return verdict, reason

    # Corroboration is per-STRETCH, not per-pull, for the message BELOW —
    # `corroborated` above already answered the ROW question (did ANY
    # partner hold a matching in-window id) and it is True past this
    # point. Counting partners made the OLD version of this message
    # all-or-nothing: one older pull holding a single execution in the
    # overlapping half marked the WHOLE window corroborated, newest tail
    # included — and the newest tail is precisely the residual spec
    # Section 5 names, the part no earlier pull can have observed. A
    # summarized tail then rendered a low, plausible fill count with the
    # disclosure silent, which is the one failure it exists for. Ask
    # instead which parts of this pull's own window nobody else observed.
    #
    # `uncorroborated_stretches` answers a DIFFERENT question from
    # `corroborated` above (see its own docstring) — its WINDOW-based
    # stretches, not the `UNCORROBORATED` ROW-based constant, are what
    # this message names. Reusing that constant as a literal prefix here
    # would claim "no other pull held ANY execution", which is false the
    # moment this line runs (`corroborated` is already True).
    alone = uncorroborated_stretches(candidate, envelopes, tool=tool, cache=cache)
    if alone:
        named = ", ".join(f"{s.isoformat()}..{e.isoformat()}" for s, e in alone[:2])
        more = "" if len(alone) <= 2 else f" and {len(alone) - 2} more"
        return "complete", (
            f"corroborated in part; no complete pull's own window covered "
            f"{named}{more}")
    return "complete", ("every part of this window was observed by another "
                        "pull; none holds an execution it lacks")


def uncorroborated_stretches(candidate, envelopes: list, *, tool: str,
                             cache=None) -> list:
    """The parts of `candidate`'s own window that no OTHER complete pull of
    `tool` observed — empty when every part of it was seen twice.

    One implementation, two consumers: `effective`'s "corroborated in
    part" reason names these, and `report` (Task 11) intersects them with
    the quarter it is rendering. Deriving them a second time in the CLI is
    the reimplementation `.claude/rules/producer-consumer.md` rule 3
    forbids, and the two copies would drift on exactly the edge this
    exists to catch.

    Only a partner whose EFFECTIVE verdict is `complete` counts as having
    OBSERVED a stretch: a truncated one saw it and did not report all of
    it, which is not corroboration of an absence. Effective, not the
    stored field, because the stored field is exactly what this module
    exists to distrust — a pull archived before a fuller one was stored
    `complete` for want of a partner, and reading that field let a pull
    already PROVED short keep vouching for the stretch its own truncation
    lies under, silencing `report`'s only disclosure there. The verdict
    comes from `effective_verdict`, never `effective`; the module docstring says
    why that distinction is load-bearing and not a style choice.

    Empty for a candidate whose own window cannot be derived — there is no
    window to have a tail of, and step 2 has already made it `unknown`.

    DELIBERATELY a WINDOW question, not a ROW one — do not "unify" this
    with `effective`'s `corroborated` flag, and do not read the paragraph
    above as narrowing it: eligibility decides WHICH partners may observe,
    this decides WHAT observing buys them. An ELIGIBLE partner's DECLARED
    window observes a stretch the moment it overlaps, whether or not that
    partner happened to hold a row inside the overlap: an effectively
    complete pull's report over its own window is trusted whole, zero rows
    included (a recognised empty row list is valid zero-row evidence — see
    `_ids`'s docstring). `effective`'s
    `corroborated` flag asks a narrower, stricter question — did a partner
    hold a row matching one of THIS pull's own ids — because a window that
    merely brushes the candidate's while contributing no matching id
    proves nothing about the candidate (round-1 review finding: a
    `DAYS_30` partner whose only row falls three weeks before a quarter
    must not read as corroborating that quarter). The two questions
    genuinely differ and both are needed: this one for `report`'s
    per-quarter disclosure, the other for `effective`'s bare-vs-partial
    reason.

    `cache` (see `ReductionCache`) is what keeps this affordable: the
    per-partner verdict below re-reduces the whole archive, so a caller
    running this over every candidate pays a CUBE unless it threads one
    memo through. Pure optimization — the stretches are identical either
    way.
    """
    cache = _cache_for(envelopes, cache)
    window = cache.window(candidate)
    if window is None:
        return []
    own_bytes = cache.canonical(candidate)
    observed = []
    for other in envelopes:
        if not isinstance(other, Envelope) or other.tool != tool:
            continue
        if other.path == candidate.path:
            continue
        if cache.canonical(other) == own_bytes:
            continue          # a duplicate cannot observe on its own behalf
        other_window = cache.window(other)
        if other_window is None or not overlaps(window, other_window):
            continue
        # LAST of the four filters, and the order is not arbitrary: this
        # one re-reduces the whole archive per partner while the three
        # above are a path compare, a byte compare and an interval test.
        # Reordering it up front costs a full reduction for every
        # duplicate and every derivably disjoint pull. The filters are
        # independent, so the order changes only the work, never the set.
        if effective_verdict(other, envelopes, tool=tool, cache=cache)[0] != "complete":
            continue
        observed.append(other_window)
    return uncovered(window, observed)
