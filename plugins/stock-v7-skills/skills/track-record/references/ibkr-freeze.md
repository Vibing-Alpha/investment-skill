# Track Record — Freeze Decisions (IBKR MCP response contract)

*Ships with the skill. The probe figures these rulings were read off are the
recording operator's account data and are NOT published — the rulings stand on
their own, and each one names what it was read from.*

Recorded from live IBKR MCP probes on 2026-08-16. Later tasks read these by heading.
The probe responses themselves are that operator's own account data and stay on
their machine — nothing below asks you to consult them, and each ruling names
the field or behaviour it was read off so it can be checked against your own
account instead.

Account shape, `portfolio_measure` and base currency for THIS operator are in
the evidence companion (`…-freeze-evidence.md`, never published). The rulings
below hold for any IBKR account with the same shape; a differing one must
re-probe D2's `account` finding and D5's NAV reconciliation.

## D1 — Cadence and coverage

trades cadence: **once per completed quarter, plus once at quarter close.**
`get_account_trades` reaches back four completed calendar quarters
(`LAST_QUARTER` … `FOUR_QUARTERS_AGO`), so a quarterly pull has three quarters of
slack. `YEAR_TO_DATE` additionally covers the current, incomplete quarter.

**Superseded 2026-08-20 — this is no longer the WHOLE cadence `pull` runs.**
The quarterly cadence above wrote a periodic ~331KB payload per session (a
full `YEAR_TO_DATE` response); a later rewrite made the per-session pull a
rolling `DAYS_30` window instead, dropping the steady-state per-session
payload to ~61KB. The finding above —
that `LAST_QUARTER`/`FOUR_QUARTERS_AGO`/`YEAR_TO_DATE` reach back exactly the
distance stated — still holds as a fact about the tool, and those period
values are still both ISSUED and recognized by `archive._covered_range`.

**The calendar-quarter calls were not retired, and reading this note as
though they were is the failure it must not cause.** What changed is WHEN
they fire: gap-driven rather than calendar-driven. `SKILL.md` Step 2 still
calls `LAST_QUARTER` in the session after a quarter closes, still backfills
four quarters on a first-ever archive, and still pulls any of the last four
COMPLETED quarters the archive does not already cover — an operator who was
away from February to August gets Q1 and Q2 from those calls and from
nothing else. What the rolling `DAYS_30` pulls carry is the CURRENT,
incomplete quarter, whose coverage is now assembled as the union of several
of their windows; they cannot reach a quarter that closed before the window
begins. `.claude/skills/track-record/SKILL.md` Step 2 is the operative
cadence.

orders cadence: **NOT RETAINED.** `get_account_orders` is documented as *live*
orders and returned `{"orders": []}` against an account with no working order.
There is no historical-order window at all. Consequence: `orders_linked` is
permanently unavailable, `unlinked` reads order keys off the **trade** rows
(which carry `order_id`), and tagging is unaffected. Keep pulling it anyway —
it costs nothing and a future pull that catches a working order can only loosen
this.

truncation detection: **record-count test.** No flag field exists. The
The probe's `LAST_QUARTER` pull was fully contained in its `YEAR_TO_DATE`
pull, with zero drift across every shared `trade_id` (counts in the evidence
companion). Two overlapping windows that agree on their intersection is the available
test; a shortfall against the wider window marks the narrower pull `truncated`.
Absent a second window to compare against, `unknown`. **(Superseded on the
no-partner branch — read the three amendments below before applying this
sentence; it is the probe's original finding, not the operative rule. This
line, quoted on its own, is what put a contradictory `unknown` ruling into
SKILL.md Step 2.)**

**Amended 2026-08-17.** "Two overlapping windows" above is load-bearing, not
incidental — it was true of the probe (both mid-2026) but is not true of
every session. A session run in **January** pulls `LAST_QUARTER` (resolving
to the PRIOR year's Q4) alongside the same-session `YEAR_TO_DATE` (beginning
the CURRENT year's January 1st): the two windows are disjoint by
construction, not overlapping. "Absent a second window to compare against,
`unknown`" describes only the case where a session pulls just one of the
two — it does not extend to a disjoint pair, where both were pulled but a
row missing from one side of a non-overlapping comparison proves nothing
about either pull. A disjoint comparison must not run at all.

**Amended again, 2026-08-17.** The amendment above
over-corrected. It went on to say a disjoint pair must not downgrade
`LAST_QUARTER` to `truncated` **or** `unknown` either — classifying it
`complete` directly, from its own `args` alone, with no check run against
anything. That traded the false negative it fixed for a false positive: a
genuinely truncated January `LAST_QUARTER` pull now passed as `complete`
unconditionally, because nothing in that session could ever check it.
Demonstrated: a full and a truncated January pull both passed coverage, and
the quarter's DATA line went from a correct `unavailable` to a fabricated
count.

The disjoint session's own `YEAR_TO_DATE` pull was never the only possible
comparison partner — it was just the only one the first amendment looked
for. The real partner for a January `LAST_QUARTER` pull is a **DECEMBER**
`YEAR_TO_DATE` pull: one archived at that quarter's own close, which
**CONTAINS** it (its window reaches back to January 1st of the same year
and forward through the quarter's own last calendar day). The cadence
already takes a quarter-close pull at year-end for the performance tool's
sake (below, this same section) — `get_account_trades` now takes one too,
in the same session: **compare `LAST_QUARTER` against ANY archived
`YEAR_TO_DATE` pull whose window CONTAINS it — reaches from January 1st of
`LAST_QUARTER`'s own year through `LAST_QUARTER`'s own quarter's last
calendar day — this session's, or an earlier one's, not only the most
recent one and not only the same session's.** A pull that merely overlaps
part of the quarter without reaching its end (e.g. one taken mid-quarter)
does NOT qualify as a partner: it can only speak to the rows inside the
slice it reached, and admitting it validates an incomplete archive as if it
had been checked in full — demonstrated live, where an October 15
`YEAR_TO_DATE` pull admitted as partner for a Q4 `LAST_QUARTER` produced
`1 fill → 1 order (order_unknown 0)` against a complete quarter's actual
three fills, two orders and one unknown-order fill. If a CONTAINING pull is
found, run the same record-count test against it, in the direction fixed
above (§"truncation detection" — the wider pull's rows inside the window,
checked against the narrower pull, never the reverse). **If no containing
pull is found, see the amendment below (the first amendment was superseded on this point) —
classify `complete`, not `unknown`.** See
`.claude/skills/track-record/SKILL.md` Step 2 for the operative rule this
now compiles to.

**Amended a third time, 2026-08-17.** The ruling directly
above — "if no archived pull overlaps, the honest classification is
`unknown`, not `complete`" — is now superseded on the no-partner branch
only (the CONTAINS-not-overlaps correction above it stands). Blocking on
`unknown` was protecting against a truncation this comparison, run
correctly, has never once observed (zero drift across every shared row of the
probe's two overlapping windows) while imposing a certain
cost: every genuinely complete first-ever January `LAST_QUARTER` pull — no
partner can possibly exist yet, by construction — rendered all three DATA
values `unavailable`, downgrading every first year this cadence is ever run.
**Absent any containing partner, classify `LAST_QUARTER` `complete`, and
tell the user plainly that no cross-check was possible for this quarter** —
a stated caveat, not a silent one and not a refusal. This is safe only
because the comparison direction bug above is also fixed in the same pass:
before that fix, the record-count test could never catch a truncation
either way, so `unknown` was protecting nothing it could have caught, and
was blocking a real first use to guard against a risk the check itself
could never have detected in the first place.

**Superseded 2026-08-20 — truncation is no longer classified at pull time.**
Every ruling above — the record-count test, its comparison direction, the
CONTAINS-not-overlaps correction, and the no-partner `complete` fallback —
was a WRITE-time procedure: an agent or the skill judged one pull against one
designated partner, and the verdict was final once written. The
CLI-decisions-into-code rewrite moves classification to READ time:
`scripts/track_record/completeness.effective()` recomputes it on every read,
against the WHOLE archive rather than one designated partner — any
overlapping same-tool pull that is itself `complete` and holds an in-window
`trade_id` this pull lacks demotes it to `truncated`, and a pull stored
`complete` before a fuller one existed is demoted automatically the next time
anyone asks, rather than staying `complete` forever the way a write-time
verdict did. The two outcomes this section fought hardest for are preserved
under the new mechanism: a genuine shortfall is still caught (positive proof
from any overlapping partner outranks every uncertainty), and a pull with no
corroborating partner still classifies `complete` rather than blocking on
`unknown` — stated plainly, not silently (the bare `UNCORROBORATED` reason;
see also `report`'s own corroboration NOTE on stderr, which names any stretch
of the quarter only one pull ever observed). What changed is WHEN the check runs
(once at write time vs. every time at read time) and WHICH partners it
compares against (one designated containing pull vs. every overlapping
same-tool pull currently archived) — not that a shortfall goes uncaught.

args → UTC range mapping:
- `LAST_QUARTER` → the most recent **completed** calendar quarter. Verified:
  returned rows spanning exactly the requested completed quarter, first fill to last.
- `TWO_/THREE_/FOUR_QUARTERS_AGO` → successively earlier completed quarters.
- `YEAR_TO_DATE` → `[Jan 1 of the current year, now]`.
- All boundaries are UTC, per the tool's own contract.

row path per tool:
- trades: `response["trades"]` — a list of row objects.
- orders: `response["orders"]` — a list of row objects (observed empty).
- performance: **NOT a list of rows.**
  `response["accounts"]["account"]["periods"][<PERIOD>]` holds three PARALLEL
  ARRAYS — `["dates"]`, `["nav"]`, `["cps"]` — of equal length, indexed together,
  plus two scalars: `["start_nav"]` and `["start_date"]` (e.g. `"20260101"`).
  **`start_date` is not optional trivia — `observations_from` reads it as the
  BASELINE IDENTITY it groups a `YTD` pull's rows on** (an earlier version of this list omitted it, which would leave a reader
  thinking the shape was complete without it). The pull's own `pulled_at`
  year is NOT an acceptable substitute: `cps` resets its baseline every
  January 1st, so an old envelope archived alongside a fresh one would
  collide under one proxy year and reintroduce the cross-year baseline mix
  this design already had to fix once (see this file's other 2026-08-17
  amendment, above). `observations_from` zips the two parallel arrays and
  reads `start_date` as the grouping key; it does not map over row objects.

performance period to read: **`YTD`**, and only `YTD`. `cps` is measured from
each period's own start, so `YTD` and `1Y` report different `cps` for the same
date. TWR between two endpoints is baseline-independent, so either answers
correctly — but mixing them corrupts both RISK and CONTEXT. One period,
throughout. `YTD` (one observation per business day since Jan 1) covers any quarter of
the current year; a quarter outside it is unreachable and reports `unavailable`.

**Amended 2026-08-17.** "Covers any quarter of the current year" undersold
one case: **Q4**. `cps`'s baseline resets every January 1st, so a `YTD` pull
taken before the roll only ever reaches as far as its own `pulled_at`, and a
pull taken after the roll starts a brand-new baseline that carries nothing
from the year just closed. Q4 is reachable ONLY by a pull taken after its own last TRADING
session's close and strictly before the roll — on or after its last
calendar day (2026-12-31) when that day is itself a trading day, and after
the preceding Friday's close when it is not (2028-12-31 is a Sunday) — there
is no way to "pull later" and recover it the way any other quarter can be.
`archive.coverage_gap` now judges this tool's coverage on the CALENDAR DATE
a pull reached, not the exclusive end instant a quarter's bounds name;
`.claude/skills/track-record/SKILL.md` Step 2 now pulls this tool a second
time each quarter for exactly this reason.

**Corrected 2026-08-17.** The sentence originally
continued here: "so a same-day quarter-close pull is sufficient." That is
wrong, and contradicts both `SKILL.md` Step 2 and design.md §4.1, which
already require the pull to happen AFTER the quarter's own final trading
session has closed — not merely once the calendar date has rolled to the
quarter's last day.

**Superseded 2026-08-19 — the code no longer accepts the calendar-date
floor.** This paragraph said the floor was "a tolerance the CODE accepts
(it cannot distinguish a pull taken one minute into the day from one taken
after the close, since both carry the same date)". It can now:
`_range_covers` requires a pull ON the quarter's last TRADING day to be at
or after that day's US equity close — 20:00 UTC for Q1-Q3 (EDT) and 21:00
UTC for Q4 (EST) — and gives no coverage otherwise. It finds that day by
walking back from the quarter's last calendar day over Saturdays and
Sundays only, so a quarter ending on a weekend is covered by the Friday
pull the cadence asks for (twelve of them in the next ten years); a holiday
can move the real session earlier still, which leaves the bar stricter than
necessary and never looser. An agent reading the
old sentence would have told the operator that an early same-day pull
satisfies coverage while the report refuses it. The rest of this paragraph
is why the rule exists, and stands:

a pull landing before the close can satisfy a date-only floor while
still missing the session's own observation, and the resulting number is
not merely "a day off", it can be wrong by tens of points: a pull at
`2026-12-31T00:01Z` (still evening of Dec 30 in US trading time) rendered
RISK and CONTEXT one way; the same quarter with the final session included
rendered a materially different drawdown and spread — a double-digit
swing from a one-session shortfall, because the quarter's own move happened
on that final session. The freshness gate's 5-day gap tolerance governs
whether a gap is DETECTABLE, not whether a number produced just inside it
is close to correct — do not read the calendar-date floor as license to
pull early.

host path persisting the tool result without passing through the model:
**partially found.** The harness writes an oversized tool result to a file and
returns the path instead of the payload; a completed-quarter trades pull (over 150,000
characters) came back that way and was copied verbatim. Small responses still
pass through the model. Because the path is not available for every pull, the
envelope gains **no** `provenance` field — a field present on some envelopes and
absent on others is worse than no field. Revisit only if the harness makes the
path unconditional.

provenance literal, if found: **n/a — not adopted, per the line above.**

## D2 — Execution key

key: **`(account, trade_id)`** — `trade_id` is a string, unique across every
row of both probe windows.

account: **ABSENT** — no account field on any row. This probe was a single-account
login. The account slot is filled with the frozen constant **`U1`**, in
`fills_from` and in `known_orders_from` alike, so the `(account, order_id)` pair
the spec mandates holds everywhere downstream. `U1` is a label for one account,
not a redaction of a real id.

**KNOWN ISSUE — the synthetic `U1` cannot tell two accounts apart, and no
code gate can (open, not fixed; recorded 2026-08-18, found by cold review,
never observed in a real archive).**

*What is true today.* Across every archived trades pull, `account` appears on
**zero** rows. It is not a field the tool declines to read — the wire format
has nowhere to put one: none of the five MCP tools takes an account
parameter, and the only response carrying an `accounts` structure keys it by
the literal singular string `"account"` with no identifier inside. So `U1` is
the ARCHIVE's namespace for "the one account this connection reaches", not a
stand-in for a real broker id.

*The precise failure.* If an archive root ever spans more than one broker
account and two of their `order_id`s collide, the order count merges them.
Worse, and the reason this is written down at all: one `(U1, order_id)`
reference in the journal would suppress BOTH accounts' orders from
`unlinked`, so a genuinely untagged decision disappears from the backlog with
no signal. Counts are a report number; the backlog is what the tool is for.

*Why no code fix.* The failure splits in two and neither half is reachable by
a gate. A single pull mixing two accounts is structurally impossible under
the current wire format — there is no field to disagree in. One archive root
used across two single-account logins IS possible, and is invisible to any
check: both logins' rows carry no `account`, so a cardinality gate sees zero
values and reading a real account finds nothing to read. A gate on "≥2
distinct account values" would have a true-positive no producer can emit —
its only test would be a fixture nothing can generate — while its
false-negative is exactly the mixing shape most likely to occur, and one
false positive would refuse every quarter and the whole backlog permanently,
since archives are append-only. That trade is worse than the risk.

*An asymmetry worth knowing.* If IBKR ever does key `accounts` by a real id,
`observations_from` fails closed on the spot — it reads
`response["accounts"]["account"]["periods"]["YTD"]` and would refuse. RISK and
CONTEXT therefore already have a schema-change tripwire. The DATA path has
none: `_normalize_fill_row` takes fields with `row.get(...)`, so an added
`account` key is silently ignored and the fill and order counts, and
`unlinked`, carry on. **If this ever breaks, the money-path numbers will say
so and the backlog will not.**

*Reopen when* the MCP exposes a trustworthy account field or an
account-selection parameter, a multi-account response is observed, or an
archive root is found to have been reused across logins. Then re-probe D2 and
take the money-path rigor path — including a migration for the journal's
existing `U1` order references, which cannot be back-filled to real accounts
by guessing.

order id field: **`order_id`** — and it is an **`int`** (a 10-digit value).
Coerce to `str`, as the plan requires: the journal is hand-written and its refs
are strings, so an uncoerced `1234567890` never matches `"1234567890"`.

trade timestamp field: **`trade_time`**, already canonical UTC `Z`
(e.g. `2026-06-30T14:07:05Z`). Normalization is still performed, not assumed.

"differing in content" compares: `symbol`, `side`, `size`, `price`, `trade_time`,
`order_id`. Not `commission`, `net_amount` or `realized_pnl` — those are derived
and may be restated without the execution having changed.

**Amended 2026-08-18, first live session — `order_id` moves to the excluded
list and `trade_time` gains a one-second tolerance.** D3 below recorded this
class as "Not observed"; it has now been observed. Two `YEAR_TO_DATE` pulls
two days apart disagreed on a small fraction of shared `trade_id`s (exact
counts and the id pairs are in the evidence companion):

- **8 differed in `order_id` alone.** Seven distinct ids were replaced
  (each old id replaced by one in a distinct higher-numbered block), the old ids
  gone from the broker's response entirely, the new ones all in a
  `55xxxxxxxx` block, every affected trade dated within the preceding week —
  and `symbol`/`side`/`size`/`price`/`trade_time`/`commission`/`net_amount`
  byte-identical. The exclusion criterion this section already states
  ("derived and may be restated without the execution having changed")
  describes `order_id`: it is the broker's attribution handle, not a
  property of the execution.
- **1 also moved `trade_time` by one second**,
  everything else identical.

`trade_time` is NOT excluded — it is the execution fact that separates two
same-symbol, same-price fills, and dropping it would give away the ability to
catch an execution genuinely restated to a different moment. It is compared
with `_TRADE_TIME_TOLERANCE_SECONDS = 1`, exactly the restatement observed;
two seconds still fails closed.

**Consequence recorded, not solved:** `order_id` is the `orders_linked` key
(`(account, order_id)`, §6.1) and it is now known to be unstable, so a
journal link written before a restatement points at an id the broker no
longer reports. `fills_from` keeps the FIRST-archived row, so existing links
keep matching — but a link written from a later pull would key off a
different id for the same execution. `trade_id` was stable across all 9.

**Investigated 2026-08-18 and closed WITHOUT a schema change.** Two things I
asserted when raising it turned out to be wrong, and checking them is what
settled it: a backfilled older pull does NOT flip the retained id (envelopes
sort by `pulled_at`, so retention is deterministic and independent of write
order), and existing links do NOT go stale (the journal and `unlinked` key
off that same earliest-archived row; the COUNTS key off the LATEST one,
because the broker owns this field and restates it, so its newest word is
what a count of distinct orders follows — this sentence read "journal,
`unlinked` and the counts" until a cold review checked it against the
code). The only real loss is
the broker-side referent, and the archive returns it — the journal's
`order_id` joins to its `trade_id`s in any pull, and those `trade_id`s give
the current id in the latest pull. Verified end to end on a real journal:
every reference resolved, including the restated ones. Re-keying to the execution
id would trade the ORDER as the tagging unit (what §6.1 counts) for the
FILL, to solve something already solved. Recorded in the journal event schema instead.

## D3 — Correction representation

**Observed 2026-08-18** (this line read "Not observed" until then, on the
strength of two pulls taken minutes apart). Two pulls two DAYS apart
disagreed on a small fraction of shared `trade_id`s — see D2's amendment above for the
shape and the ruling. The conflict rule below is unchanged in force; what
changed is which fields feed it.

Conflict rule in force: **fail-close.** Two archived rows sharing
`(account, trade_id)` but differing in the field list above are a conflict; both
are retained and every broker count reading them reports `unavailable`. Build
exactly this one — not a version chain — since nothing observed suggests IBKR
restates an execution under its own id.

## D4 — Instrument identity

stable id across trades/orders/positions: **NO.**

- **Positions carry `contract_id`** (int, IBKR's conid) — a 9-digit integer per
  holding. Stable, and independent of the ticker. (The operator's own
  symbol→conid pairs are in the evidence companion; they are holdings.)
- **Trade rows carry no instrument id at all.** Their only instrument-ish field
  is `symbol`. (`order_id` identifies the order, not the security.)
- Orders were empty, so nothing to check there.

same id ON A TRADE ROW: **NONE.**
display symbol on a trade row: **`symbol`.**

Consequences, all of which the plan must absorb:

1. `fills_from` emits **five** fields, not six: `{account, execution_id,
   order_id, symbol, trade_time}`. There is no `instrument_id` on a trade row to
   emit, and inventing one from `symbol` would assert a stability the ticker does
   not have.

   **Amended 2026-08-17.** "Five, not
   six" stands for instrument identity specifically — there is still no
   `instrument_id` on a trade row. It does NOT mean `fills_from` may emit
   only these five, full stop: D2's own execution-conflict check already
   reads `side`/`size`/`price` off the raw row (see D2 above), so the
   archive carries them; `fills_from` just used to drop them before
   returning. `unlinked` is the only consumer, and it exists so a human can
   recognize which decision an order was — three columns
   (`account,order_id,symbol`) gave no way to do that once a quarter runs to
   hundreds of orders. `fills_from` now emits eight: the original five plus
   `side`, `size`, `price` — plus a ninth, `order_id_latest`, on any row
   whose `order_id` the broker restated (D3): the row keeps its FIRST id so
   journal links stay matched, and the counts read the latest. Verified
   against a live run rather than read: nine keys on a restated row.
2. `unlinked` prints `account,order_id,date,side,size,symbol,price`
   (amended alongside point 1, same review) — not the original
   `account,order_id,symbol`. `account,order_id` stay the first two columns
   so the `tag` step still keys off them unchanged.

   **Superseded 2026-08-20.** A later task added an EIGHTH column,
   `company_name`: `account,order_id,date,side,size,symbol,price,company_name`.
   It is reduced across EVERY fill of the order into the agreed name,
   `?MISSING-ON-SOME` (present on some fills, absent on others), or
   `?CONFLICTING` (the broker reported more than one name, or more than one
   symbol, under the same order id) — which is what makes a reassigned
   ticker visible without a separate lookup. `account,order_id` are still the
   first two columns, unchanged.
3. A **thesis** carries a real `instrument.id` — `ibkr:<contract_id>`, looked up
   from `get_account_positions` by symbol at tag time — **whenever the account
   still holds that instrument.**

   **Amended 2026-08-17.** This entry originally read "the `instrument_unresolved`
   fallback is **not** taken: the identity exists". That was wrong, and the error
   was in treating D4 as binary. The identity exists for an instrument the account
   HOLDS; a position closed since the trade is no longer in
   `get_account_positions`, so its `contract_id` is unrecoverable — and a closed
   position is exactly where the intent behind the trade is most worth recording
   and least reconstructable. Refusing that write would lose the half of the
   record this design exists to keep, which §0's rule that capture is never
   stopped forbids.

   So the fallback IS available, **per thesis rather than per freeze**: a thesis
   carries either a non-empty `instrument.id`, or `symbol_at_write` +
   `instrument_unresolved: true`. Never neither, and never a bare
   `symbol_at_write` — an unresolved instrument must say so, or a later reader
   cannot tell a missing id from an unrecorded one.

   Found by a later task's implementer, which stopped rather than write a skill step
   promising a write the validator would refuse.

   **Superseded 2026-08-20 — the positions lookup this point describes is
   gone.** `.claude/skills/track-record/SKILL.md` no longer resolves
   `instrument.id` by looking a symbol up in a fresh `get_account_positions`
   pull at tag time: "Step 4 no longer reads a positions pull for anything;
   it resolves identity entirely from `unlinked`'s own rows." The reason this
   two-step lookup (read the symbol off `unlinked`, then cross-reference a
   separate positions pull) existed is exactly why it was replaced, not why
   it was kept: a ticker can be reassigned, and a positions snapshot only
   ever answers for an instrument the account CURRENTLY holds — the
   closed-position gap this very entry exists to name, two paragraphs above.
   `company_name_at_trade`, read straight off the fill the way
   `symbol_at_write` always was, answers for a closed position too, because
   it is captured from the trade row itself rather than looked up
   afterward. `journal.py`'s schema still accepts a resolved `instrument.id`
   if one is supplied — narrowing it would make old journal entries
   unreadable — but nothing in the skill supplies one any more:
   `symbol_at_write` + `company_name_at_trade`, or `symbol_at_write` +
   `instrument_unresolved: true`, is the live path.
4. `orders_linked` is already permanently unavailable via D1, so the question of
   verifying a link against an instrument does not arise.

## D5 — cps scale and NAV semantics

scale: **decimal.** `cps` values are fractions from the period start
(a decimal such as `0.1234` meaning 12.34%), per the tool's own contract and confirmed against the
`nav` series.

series frequency: **daily**, business days only, with occasional gaps. YTD holds
one observation per business day since Jan 1.

**date format: compact `YYYYMMDD`, no separators** (e.g. `"20260816"`). yfinance
writes `2026-08-16`. Both sides of that comparison are canonicalised to
`YYYY-MM-DD` before any matching, per the plan.

observation fields: date `dates`, nav `nav`, cps `cps` (the three parallel
arrays above).

NAV semantics: **`nav` is the raw account balance and is NOT flow-adjusted.**
`cps` is TWR and is. Reconciling YTD, `nav[i]/start_nav - 1` tracks `cps[i]`
exactly for the first few observations and then diverges permanently once an
external cash flow lands — the implied-vs-reported figures at the divergence
point, and the widening gap after it, are in the evidence companion. The
divergence is the whole finding: it is what rules NAV out
and widening thereafter, once an external cash flow lands.

**RISK is therefore computed from `(1 + cps)`, not from `nav`** — running peak
seeded at `d0`, bounded to `[d0, d1]`, formula shape unchanged. A drawdown taken
from `nav` would read a deposit as a recovery. See the ruling in the SDD ledger.

boundary observation at quarter edges: **yes.** `20260630` and `20260331` are
both present, so `d0` resolves for 2026Q3 and 2026Q2.

## D6 — Prediction prompting

**prompt for it by default.**

The live tagging trial has **not** been run — it needs the user's own three
theses, and nothing structural waits on it: since the schema branch was removed,
`prediction` is optional-and-complete-when-present regardless of what D6 says,
and `prediction_resolved` is always valid. D6 governs only whether the skill asks
unprompted.

Defaulting to prompting is the recoverable direction: a user who does not want
the question turns it off after meeting it, whereas a user never asked never
discovers the field. Revisit after the first real tagging session.

trial notes: **run 2026-08-18.** Three groups tagged in the first real session
(two momentum round-trips and one momentum entry). **Intent was given
every time, in one line. A prediction was given zero times out of three.**

The question this trial was set to answer — whether a position you cannot
state a falsifiable prediction for gets abandoned rather than recorded — came
back **recorded, not abandoned**: every group was journalled with its intent
and no prediction. The prompt did not cost a capture.

But the absences were not reluctance, they were correct. Two of the three
arcs had already closed, so a forward falsifiable claim written afterwards
is not a prediction at all; the third was a momentum entry, and "momentum"
is not the kind of thesis that states itself as "X above Y by date Z". This
is evidence about WHICH THESES CARRY PREDICTIONS, not about whether the user
will answer: a momentum book may simply have few predictable propositions in
it, and a valuation or catalyst thesis would be where the field earns its
keep.

**D6's ruling is unchanged (prompt by default), on its own original
reasoning** — a user who has met the question can decline it, and this
session shows declining is cheap and does not deter the capture. Revisit
only if a later session shows the prompt suppressing tags rather than
producing empty prediction slots.
