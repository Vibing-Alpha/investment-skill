---
name: track-record
description: |
  Capture the broker facts that expire at IBKR and the contemporaneous investment
  intent nothing else can reconstruct — archive trades/orders/performance/positions
  before they roll off the broker's retention window, tag a group of orders with
  the one-line thesis behind them, and render a short quarterly summary of what
  happened (never why it was good or bad, never a recommendation). Use this
  whenever the user wants to log the reasoning behind a trade they just placed,
  group recent fills into a thesis, correct a mis-tagged journal entry, capture
  broker history before it expires, or run the quarterly track-record report.
  Trigger phrases: "track record", "打标", "季度复盘", "tag this thesis",
  "log this trade", "tag these orders", "record my prediction", "capture my
  trades", "archive my broker history", "quarterly summary", "track-record
  report", "what orders are unlinked".
  SCOPE: Interactive Brokers only, via the IBKR MCP server. Needs a
  harness that can call the five tools and put a tool result on disk
  unmodified; it no longer depends on one harness's persist-to-file
  behaviour, because the steady-state pull is ~61KB. Codex, Cursor and
  OpenCode are expected to work and are UNTESTED — say which harness
  you are on when you report a capture. Reads five tools read-only
  (get_account_trades, get_account_orders, get_pa_performance_all_periods,
  get_account_positions, get_account_balances) — it never places, amends or
  cancels an order. No other broker is supported: the frozen decisions this
  skill compiles against (D1-D6) are IBKR's response contract, not a generic
  one. Requires the IBKR MCP server to be connected; the frozen
  schema decisions it compiles against ship alongside at
  references/ibkr-freeze.md.
  NOT for buy/sell/hold recommendations, sizing or order generation (use portfolio).
  NOT for scoring a business or building an investment thesis on valuation/
  technicals/catalysts (use score-business / investment-thesis) — the "thesis"
  this skill records is the user's own one-line intent for a trade group, never a
  generated analysis.
user_invocable: true
---

# Track Record — Broker Capture + Quarterly Summary

Orchestration only. Every coverage gate, count and render decision lives in
`scripts/track_record/{archive,journal,summary}.py`, invoked through the
`due | pull | tag | unlinked | open | report` CLI (`python3 -m
scripts.track_record ...`) — this skill has no analysis methodology of its
own and makes no portfolio decision.

**Two judgments are the agent's own, and no command computes either:**
the `completeness` value each pull is archived under — your
observation of the response, not a verdict about the window: `pull`
stores it unchanged, and what actually gates coverage is derived at
read time from every pull that overlaps (Step 2 states the rule); and
which orders form one thesis (Step 4 — the judgment this skill exists
for). Everything else is the CLI's. The frozen broker contract this
compiles against is `references/ibkr-freeze.md`, next to this file.

This skill has one job in six parts: **read the frozen schema → archive
broker facts → resolve theses and predictions that have run their course →
tag order groups with intent → let the user fix a transcription error →
render the quarter.** Resolve precedes tag because tagging can supersede a
thesis, and a superseded thesis is one `open` no longer shows. Nothing here
evaluates whether the user picked well — that judgment is deliberately out
of scope.

## Before anything else, if this session opened with a thesis

**If the message that started this session already states one** — "tag
these fills, I bought because X" — **run `date -u +%Y-%m-%dT%H:%M:%SZ` NOW,
before the prelude below, and hold what it prints.** That is when the user
said it, and Step 4 records it as `recorded_at`. This one command needs
nothing resolved first, which is why it comes before the prelude: taking
the reading after the prelude, the pull and the resolve step stamps the
thesis with a time the user did not speak at, and on the last night of a
quarter puts it in the wrong quarter.

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read/Edit tools do NOT follow a bash `cd` — they need an absolute path
regardless. So the repo root is resolved exactly ONCE, here.

Run this block first and **CAPTURE the `STOCK_V7_ROOT=...` value it prints**.
Substitute that absolute path for the literal `<captured-abs-ROOT>` in every
later Bash block and every harness Read/Edit path in this skill. If this block
exits non-zero (multiple candidate roots, or no repo found), show its stderr
to the user and **STOP** — run nothing else.

If it prints a `stock-v7: WARNING — version skew` line, relay that warning to the user verbatim and continue — it is advisory only (the installed plugin and the clone are at different versions; it tells the user which half to update), never a stop.

```bash
# --- resolver-core ---   (byte-identical to scripts/templates/root_resolver.sh — Task 5 enforces)
# cwd-or-ancestor: if cwd (or ANY parent) is the repo, USE IT — CC-CLI/Codex/Cursor/OpenCode run from the
# repo (or a subdir), so this is a TRUE no-op (covers subdir runs + multi-worktree dev: always the clone
# you're in). Composite marker = scripts/ + prompts/ + strategy.example.yaml (the last is the
# stock-v7-specific tracked file; tighter than CLAUDE.md/VERSION alone).
ROOT=""; d="$PWD"
while [ "$d" != "/" ]; do                # cwd-or-ancestor; marker = scripts/ + prompts/ + strategy.example.yaml
  if [ -d "$d/scripts" ] && [ -d "$d/prompts" ] && [ -f "$d/strategy.example.yaml" ]; then ROOT="$d"; break; fi
  d=$(dirname "$d")
done
case "${STOCK_V7_HOME:-}" in /*) [ -z "$ROOT" ] && ROOT="$STOCK_V7_HOME";; esac   # env override seam — ABSOLUTE only (relative/~ is ignored, mirroring resolve_root's fail-closed; nothing can set it persistently in Cowork)
if [ -z "$ROOT" ]; then
  # Cowork (ephemeral cwd): glob the clone under USER mounts only (exclude outputs/uploads + dot-folders),
  # verify the composite repo marker (a stray dir merely NAMED stock-v7 must not count — round-11),
  # then realpath-dedup (symlinked mounts → same real dir must NOT count as multiple roots).
  HITS=$(ls -d /sessions/*/mnt/*/stock-v7 2>/dev/null | grep -vE '/mnt/(outputs|uploads|\.[^/]*)(/|$)' \
    | while IFS= read -r h; do (cd "$h" 2>/dev/null && [ -d scripts ] && [ -d prompts ] \
        && [ -f strategy.example.yaml ] && pwd -P); done | sort -u || true)
  if [ "$(printf '%s\n' "$HITS" | grep -c .)" -gt 1 ]; then
    echo "stock-v7: multiple stock-v7 roots in mounts — keep ONE:" >&2; printf '%s\n' "$HITS" >&2; exit 1
  fi
  ROOT=$(printf '%s\n' "$HITS" | head -1)   # the sole hit, or EMPTY — the consumer tail handles empty
fi
# --- end resolver-core ---
# BUSINESS tail (the setup skill replaces everything below the end-marker with its clone/pull tail):
if [ -z "$ROOT" ]; then                                   # CC-CLI marker fallback (rare: not-in-repo + no env)
  ROOT=$(cat "$HOME/.stock-v7-home" 2>/dev/null | tr -d '\r')   # strip CRLF if the marker was hand-edited on Windows
  ROOT="${ROOT:-$HOME/Claude/stock-v7}"
fi
cd "$ROOT" 2>/dev/null || { echo "stock-v7: run the setup skill first" >&2; exit 1; }
printf 'STOCK_V7_ROOT=%s\n' "$PWD"   # Step 0 EMITS the resolved abs root (post-cd $PWD) for the agent to capture
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.version_skew --expected-min "1.15.0" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync
```

Every Bash block below starts with `cd "<captured-abs-ROOT>"` for the same
reason: each one may be its own fresh shell.

## 0. Scope — read this before the freeze file

**The IBKR MCP server must be connected and answering.** This skill only
reads it — it cannot install or authenticate it, and nothing here has another
data source. If those five tools are not available to you, say so and stop;
do not substitute another source.

**Interactive Brokers only, through the IBKR MCP server.** D1-D6 are IBKR's
response contract: the `period` -> UTC-range mapping, `trade_id` as the
execution key, `cps` as a decimal TWR series, `contract_id` living only on
positions rows. None of that is portable to another broker, and a different
broker's MCP would need its own Phase-0 probe before any of this runs.

This skill needs a harness that can call the five IBKR MCP tools and
put a tool result on disk unmodified. It no longer needs one
particular harness's persist-to-file behaviour: the steady-state
`DAYS_30` pull is ~61KB against the ~331KB `YEAR_TO_DATE` pull it
replaces (both MEASURED against the real archive). Codex, Cursor and
OpenCode are expected to work and are UNTESTED — say which harness
you are on when you report a capture, so a failure can be attributed.
**Two pulls stay large and no cadence removes them:** the
quarter-close `LAST_QUARTER` at ~174KB, and a first-ever backfill of
four quarters at ~174KB each. Data older than 90 days is reachable
no other way.

A harness that TRUNCATES an oversized result is caught: the archive
refuses invalid JSON. A harness that SUMMARIZES one into valid JSON
is caught only for rows some overlapping pull also saw — so the
newest tail of the most recent pull, and a first-ever backfill, can
be archived `complete` while short, and a rendered fill count can
then be low and entirely plausible. The mitigation is the cadence,
not a check: the next session's overlapping `DAYS_30` pull covers the
previous tail. If you have reason to think your harness summarizes,
say so before capturing rather than after.

**`report` names this on stderr, and the newest pull's tail is
ALWAYS named** — nothing later has observed it yet, by construction.
On a first-ever run the whole window is named, for every quarter,
because nothing else is archived at all. Re-pulling the same window
does NOT silence it: a byte-identical response is a duplicate and
observes nothing on its own behalf.

**Relay it with the operative conclusion, not just the text.** Say
it in words: *the DATA figures resting on that stretch are
uncorroborated — on a harness that summarizes, that is exactly where
a short response is archived `complete` and renders a low, entirely
plausible count.* Relaying the line without the conclusion is the
failure this NOTE exists to prevent, one step removed.

**Do NOT say "wait until another pull covers it" as though that is
always available.** Whether it clears depends on which stretch it is:
- a RECENT tail — the newest part of the last `DAYS_30` pull — is
  covered by the next session's pull, and the warning goes away on
  its own;
- a stretch in an old quarter, typically a first-ever backfill, is
  reachable only by re-issuing that quarter's own call, and a
  re-issue that returns identical bytes is a DUPLICATE and observes
  nothing. There may be no way to clear it, ever. Saying "do not use
  this number until it is corroborated" would then retire an
  otherwise correct historical count permanently;
- a stretch in the MIDDLE of an archive with pulls on both sides is
  the real signal — something was missed where it should not have
  been.

State which of the three it is, and let the user weigh an
uncorroborated number rather than deciding for them that it is
unusable.

**And say that `summary.md` does not carry this.** The stored file
is deliberately unchanged — the acceptance criterion that proves
this whole change is invisible to the numbers — so the caveat lives
in this session and nowhere else. Anyone rereading that file later
sees the figure without it.

## 1. Read the freeze decisions first

Read `references/ibkr-freeze.md`, next to this file (headings D1–D6), before
doing anything else. It is the compiled record of five
live MCP probes — the args→UTC-range mapping and pull cadence (D1), the
execution key (D2), why a ticker can be reassigned between the trade and the
reading (D4) — which is why Step 4 records the company name at trade time
instead of looking an identity up live — and whether to prompt for a
prediction (D6) — and every step below reads it by heading rather than
re-deriving these from the design doc.

**If that file does not exist, STOP.** Tell the user the Phase-0 schema
freeze gate has not been run yet, and do not guess at cadences or shapes from
the spec alone — nothing in this skill may run ahead of that gate.

## Persistence — this skill writes files and nothing else

**It runs no `git` command, ever, and that is deliberate.** Every step below
writes to disk and stops there: nothing is committed, nothing is pushed,
nothing is uploaded, and no copy is made anywhere.

Say what that does and does not mean, because a user is entitled to the
distinction: no file is *moved* off this machine. But this skill is executed
by a model, and it hands file contents to that model by design — it reads the
journal to find due predictions, shows a corrected line before and after, and
reads the quarterly report back to the user. Those contents reach whatever
service is running you. That is how an agent-driven skill works, not a leak,
but "stays on disk" and "is never seen" are different claims and only the
first one is true here. Anyone who wants the archive built without that can
run the CLI directly; it needs no model at all.

An earlier version of this skill committed each write and pushed it, on the
reasoning that a commit which never leaves the machine is not a backup. That
reasoning is still true — but the action was wrong in two ways that only
appear once someone other than the author runs it:

- **`git push` with no refspec goes to whatever `origin` the clone has.** For
  this repository's author that is a private remote. For anyone who obtained
  the tool from a public one it is the public repository — and the broker
  archive and the intent journal are committable there out of the box. Making
  the backup "work" by repointing `origin` at your own public fork publishes
  your trade history.
- **A local commit forks the clone from its own update channel.** Updates
  arrive by `git pull --ff-only`, which refuses to clobber; a clone carrying
  its own commits needs a manual merge from then on.

So the skill stops taking the decision. **Backing this data up is yours**, and
the skill's job is to make sure you always know what is unprotected:

**After every write, say plainly which files now exist ONLY on this machine.**
Name them. Do not call them backed up, do not suggest a command, and do not
run one — the right destination depends on facts this skill does not have
(whether your remote is private, whether you have another copy elsewhere).

The two things worth stating every time, because losing them is not
recoverable:

- `reports/track-record/raw/` — the broker archive. IBKR's own history rolls
  off; once a window is gone, an unarchived pull cannot be re-taken.
- `trade-journal.jsonl` — the intent. It was never anywhere else.

The benchmark pin (`reports/track-record/<quarter>/SPY.csv`) is the third:
write-once, never re-fetched, and yfinance revises past-quarter history
retroactively, so a lost pin means a later regeneration of that quarter can
differ from the report you actually read.

**One clone is the record. A second clone is a second record, and
nothing reconciles them.** If you capture or tag from a laptop and
also from a Cowork mount, each has its own archive and its own
journal; both will answer `open`, `due`, `unlinked` and `report`
from a plausible but partial history, and neither will say so.
Copying one journal over the other DESTROYS whichever events the
other held — they are append-only files, not a mergeable store, and
two sessions can have allocated the same `thesis_id` for different
decisions. Pick one clone and use only it; if you have already used
two, say so before capturing anything else, because merging them is
a manual reconciliation this skill cannot do for you.

## Reading a command's output — three channels, all of them load-bearing

Every subcommand below speaks on three channels, and each means something
different. Read all three, every time.

- **stdout** is the answer: a path, a list, a report location. An EMPTY
  stdout is an answer too, and on its own it is ambiguous — see the exit
  code.
- **exit code** separates "the answer is nothing" (0) from "there is no
  answer" (non-zero). These print the same empty stdout.
- **stderr** carries two distinct kinds of line, and the difference is the
  whole point:
  - **`REFUSED: ...`** — the command produced no answer. Relay it to the
    user verbatim and stop that step; do not retry with a changed input
    unless the message names what to change. Two of these are not "nothing
    happened", and both are recoverable rather than alarming:
    `tag`'s `WRITE FAILED after validation passed` says so in its own text,
    and a `report` that fails while WRITING the summary may already have
    created that quarter's `SPY.csv` benchmark pin. The pin is written once
    per quarter and reused, and it holds settled historical closes, so a
    leftover one is simply the next run's input — nothing to clean up.
  - **`NOTE: ...`** — the command DID its work and exited 0, and is telling
    you something about the answer that the answer itself cannot show.
    **Relay every `NOTE:` line to the user, in the same turn, before you
    act on the result.** These exist because the tool can see a fact it is
    not entitled to decide on — an empty backlog that means an empty
    archive rather than a finished one, a timestamp that looks remembered
    rather than read from the clock, a journal reference that matches no
    archived order. Each one is evidence for the user's judgment, not a
    verdict this tool is willing to reach on its own, and dropping it
    silently is the same as the tool having stayed quiet.

Never treat a `NOTE:` as a failure, and never suppress one because the
command succeeded — succeeding is precisely when it is emitted.

## 2. Pull — archive today's broker facts

**Take the timestamp BEFORE you issue each call, not after.** `pulled_at`
is not a note about when the file was written — it is what a relative
argument like `LAST_QUARTER` or `DAYS_30` is resolved AGAINST when the
archive reads the pull back. The broker resolved that argument at the
moment of the call; if the response lands after a quarter boundary and the
stamp is taken then, the archive resolves the same word to the NEXT
quarter. A `LAST_QUARTER` pull issued at 23:59:59 on March 31st returns
2025Q4 and, stamped a second later, is read as covering 2026Q1 — which
then reports `0 fills` for a quarter the archive believes it has covered
completely, while the Q4 rows it actually holds count for nothing. The
cadence below tells you to pull right after a quarter closes, so this is
not a remote hour: it is the hour you are told to use.

```bash
date -u +%Y-%m-%dT%H:%M:%SZ          # FIRST — then issue the tool call
```

**CAPTURE what that prints**, the same way the prelude's root is captured,
and substitute it for the literal `<captured-PULLED-AT>` in the `pull`
command you run afterwards. It cannot be carried in a shell variable:
every Bash block here may be its own fresh shell, so a `NOW=` set in one
block is empty in the next — `--pulled-at` would then be passed an empty
string and `pull` would refuse it. Take one reading per tool call; do not
reuse the previous call's.

**One archive root belongs to ONE IBKR login, and nothing enforces it.**
The broker's rows carry no account field (D2), so every execution is
recorded under the synthetic account `U1`. Point this root at a second
account and their `order_id`s share a namespace: two genuinely different
orders that happen to collide merge into one, and tagging either removes
both from the backlog with nothing said. If the operator has more than one
account, each needs its own clone of this repo. Ask before archiving a pull
from a login you have not archived here before.

Per D1's frozen cadence, one pull session covers:

- **`get_account_trades`** — call with `{"period": "DAYS_30"}` every
  session, plus `{"period": "LAST_QUARTER"}` in the session right after a
  quarter closes.

  **The quarter pulls are gap-driven, not calendar-driven.** Before the
  session's `DAYS_30`, ask what the archive already covers and pull the
  quarter windows for any COMPLETED quarter it does not, up to four back —
  that is `get_account_trades`'s whole reach (D1).

  > For each of the last four completed quarters, if the archive holds
  > no complete pull covering it, pull it now with the matching
  > `LAST_QUARTER` / `TWO_QUARTERS_AGO` / `THREE_QUARTERS_AGO` /
  > `FOUR_QUARTERS_AGO` argument.
  >
  > **Ask by READING `<captured-abs-ROOT>/reports/track-record/<YYYYQn>/summary.md`.**
  > A quarter counts as already covered only when that file exists AND its
  > `DATA` line opens with a fill COUNT; an absent file, a `coverage gap`
  > reason, or any other `unavailable` there means pull it. This is a cheap
  > ASK, not a proof: the file says what the archive looked like the last
  > time that quarter's report ran, and completeness is decided at read
  > time, so pulls archived since can change the answer. When it does not
  > say plainly that the quarter has fills, or you cannot tell how old it
  > is, pull. An extra pull costs one broker call and archives a window
  > that corroborates what is already there; the other mistake loses the
  > window for good.
  >
  > **Do not run `report` to ask.** That is Step 6's command and it WRITES:
  > it truncates and rewrites the very `summary.md` you would be reading,
  > and — whenever the archived performance pulls carry the quarter, which
  > does not depend on the trades coverage you are asking about — it can
  > also fetch and pin
  > `reports/track-record/<YYYYQn>/SPY.csv`, the one write-once,
  > irreproducible file this skill creates. Step 6 decides whether to tell
  > the user "the benchmark pin appeared during this run" by reading
  > whether the pin existed BEFORE its own report ran; a pin created here
  > would make that reading say `yes`, and Step 6 would then report a pin
  > this session itself created as an earlier run's.

  A cadence stated as "`LAST_QUARTER` right after a quarter closes" only
  works for someone who runs the tool every quarter. Someone who last used
  it in February and comes back in August is not a first-ever archive and
  is not right after a close, so under the calendar rule the first session
  captures roughly the last month and never asks for Q1 or Q2 — which the
  broker can still answer, and will stop being able to. Positions and
  balances snapshots from those months are already gone; the trades are
  not, until they are.

  **A first-ever archive is then just the extreme case, and pulls FOUR
  quarters, not three:** `LAST_QUARTER`, `TWO_QUARTERS_AGO`,
  `THREE_QUARTERS_AGO` and `FOUR_QUARTERS_AGO`, once. `LAST_QUARTER` is in
  the list even though the cadence above also names it, because the two
  fire on different occasions: the cadence pull happens at the NEXT
  quarter close, which on a first run in mid-2026Q3 is months away — so
  without it the immediately preceding quarter (2026Q2) is the one quarter
  the backfill skips, and it stays a coverage gap that nothing later
  fills. Say "four quarters, ending with the one that just closed" and
  list all four; an instruction that says three and a SCOPE line that says
  four is two agents making two different archives.
- **`get_account_orders`** — call with `{}`. D1 found this reports only live
  working orders with no historical window at all, so it can never confirm a
  link — pull it anyway, every session; it costs nothing and can only loosen
  that limitation later. `completeness` is `complete` unless the call itself
  visibly errored or returned a partial result.
- **`get_pa_performance_all_periods`** — call with `{}`, same session, AND
  **once more at quarter close** — **AFTER the quarter's own FINAL TRADING
  SESSION has closed, not merely once the calendar date has rolled to the
  quarter's last day.** A pull made in the early UTC hours of that day
  (e.g. `2026-12-31T00:01Z`) is still evening of the PRIOR US trading day —
  its `dates`/`cps` series necessarily ends one session short, at the day
  before. Pull any time from that session's close onward — **and the coverage
  check is exact about it: on the quarter's last calendar day the pull must be
  at or after 20:00 UTC for Q1-Q3 and 21:00 UTC for Q4** (16:00 ET, which is
  EDT at the March/June/September ends and EST at December's). Earlier that
  day, the pull is archived but does not count as covering the quarter, and
  RISK/CONTEXT stay unavailable until one taken after the close exists. Any
  time on a LATER day is fine. **For Q1 through Q3 there
  is no deadline** — a `YEAR_TO_DATE` series still carries every observation
  of an earlier quarter of the same year, so a pull taken in July still
  covers Q2 completely, and a missed quarter-close pull is recoverable by
  pulling now. **Q4 is the exception, and it is absolute**: `cps`'s baseline
  resets every January 1st (D1), so once the year rolls, no pull carries a
  single observation from the quarter that just closed. For Q4 the quarter-close pull is
  **the only pull that can ever cover it**; waiting and pulling later, in
  the new year, covers none of it, ever. Do not tell the operator a Q1-Q3
  capture is lost because the quarter has rolled — it is not, and abandoning
  it leaves RISK and CONTEXT unavailable for no reason. `completeness` `complete` under
  the same rule as orders. (The coverage check this feeds is
  exact about the time, not only the date — see the close-hour rule above:
  a pull taken on the quarter's last calendar day before that day's close
  does NOT count as covering the quarter.)
- **`get_account_positions`** and **`get_account_balances`** — call with
  `{}`, at quarter boundaries (the same session as the quarterly trades
  pull). Neither feeds `report`'s numbers; they preserve point-in-time facts
  the broker does not retain, which is the only reason either is pulled —
  Step 4 no longer reads a positions pull for anything; it resolves identity
  entirely from `unlinked`'s own rows. `completeness` `complete` under the
  same rule.

**`completeness` — decide it before you call `pull`, from what the response
itself shows:**
- a response of the shape the tool normally returns, with nothing saying
  otherwise → pass neither flag (archived `complete`)
- something in it visibly says it is partial → `--call-partial` (archived
  `truncated`)
- an error object, or a shape you do not recognise → `--call-unknown`
  (archived `unknown`)
- no bytes came back at all → do not invoke `pull` — there is nothing to
  archive. Tell the user which tool failed and what it said, skip that one
  pull, and carry on with the rest of the session.

> Archiving the error is not the end of it. Say what failed, and ask the
> user whether to retry the call now. A rate limit clears in minutes; the
> WINDOW may not come back. `get_pa_performance_all_periods` is the sharp
> case: `cps`'s baseline resets every January 1st (D1), so an error at the
> Q4-close pull that is not retried before the year turns makes that
> quarter's RISK and CONTEXT permanently unavailable, with no later pull
> able to recover them. "Capture never stops" means a bad completeness
> must not BLOCK the session; it does not mean the failure goes
> unmentioned.

**A negative or `unknown` `completeness` never stops a pull** — archive it
anyway. `report`'s coverage gate is what turns an incomplete archive into a
fail-closed `unavailable`, not a refusal here.

For each tool above, call it exactly as the IBKR MCP exposes it, then get
the **raw, untouched** result onto disk. **The response file must be the
tool's OWN BYTES, and this is the one sentence in the step that cannot be
softened:**

> Write the tool result to the file with whatever your harness offers
> for putting a tool result on disk unmodified — Claude Code's
> persist-to-file for an oversized result, a shell redirect, an
> MCP-client save. **Never reconstruct the JSON by typing out what you
> read.** A regenerated response is still valid JSON, so `pull`
> accepts it and archives it `complete`, and a dropped row or a changed
> digit becomes a permanent fact that nothing can detect — this is F-2
> exactly, the summarizing-harness failure Section 0 names, at the
> largest and least-corroborated capture of the whole tool's life. If your harness gives you no way to put the bytes on
> disk, STOP and say so; a session with no capture is recoverable, a
> session with a retyped capture is not.

**The `--args` value is the other half of that, and nothing compares it
against the bytes.** The archive derives the window this pull will forever
claim to have covered from `--args` alone, never from the rows that came
back — so `DAYS_60` passed for a call you actually made with `DAYS_30`
certifies a month nothing observed as covered, and the fills that pull does
hold then render as a low, entirely plausible count. Copy the args from the
call you issued; if you are unsure which period you asked for, pass the
NARROWER one. An under-claim leaves a coverage gap the next report names
and a re-pull can close; an over-claim is permanent.

Only once that file holds the tool's own bytes, archive it:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.track_record pull --tool <tool> \
  --args '<the args JSON, exactly as issued>' \
  --response "/abs/path/response.json" \
  --pulled-at <captured-PULLED-AT> [--call-partial|--call-unknown]
```

Relay `pull`'s printed path, and the `NOTE:` it may print beside it
(below). Those two are what this session can say about THIS pull: the
archive holds a file under the name every later read looks for, and the
archive's own verdict on that file once the rest of it is taken into
account.

**`unlinked`'s row count is not a third one, and must not be relayed as
if it were.** That command aggregates the WHOLE archive minus every
order the journal has linked. Its count moves when any earlier pull is
archived, when any order is tagged, and when the broker re-keys or
withdraws an order id — none of which is this pull — and a count that
went up says only that the backlog grew, not that these bytes are the
ones that grew it. It cannot be attributed back to the response just
archived, so it establishes nothing about whether that response
survived. Relay it as the state of the backlog, which is what it is and
all it is.

The flag names are `--args` (a JSON string) and `--response` (a path) —
this is the CLI's own interface, not a paraphrase. `--args-json` /
`--response-json` do not exist: argparse would exit 2 and archive
nothing, losing the broker fact the session was for.

`pull` prints the archived path on success. A non-zero exit / `REFUSED:
...` line on stderr means the archive refused the input — surface that to
the user rather than retrying blindly. On success it may also print a
`NOTE:` on stderr — its job here is one sentence: `pull` records what YOU
observed (the flag you passed, or its absence) and separately reports what
the archive now says about the window once every other archived pull is
taken into account; relay that NOTE to the user verbatim, and remember it
describes the ARCHIVE's current view, not what just got stored — the
stored value never changes once written.

**The scratch files this step writes count too.** The response file you
wrote for `pull` holds a complete copy of the raw broker response, and
Step 4's event files hold the user's intent and predictions. They are
written at paths you choose, nothing here removes them, and the
Persistence rule above — name every file that now exists only on this
machine — covers them. Name them when you report what this session
produced, and say they are working copies the operator can delete once
the archive path has been printed.

**After every successful `pull` this session, tell the user which archive
files now exist only on this machine** — name the paths `pull` printed. Not
every one of them is a "gone forever" warning: while IBKR still retains a
window, a local copy lost to disk failure or a wiped machine can simply be
pulled again — Step 0 above already draws this line for what a later
session's own cadence can recover on its own (a recent rolling tail) versus
what it cannot (re-issuing an old quarter's backfill returns identical,
uncorroborating bytes, not new coverage). What genuinely has no second
chance is narrower: once the broker's OWN retention has actually rolled a
window off, no pull from anywhere can reach it again; and the performance
series in particular rebaselines every January 1st, so a quarter's YTD
figures lost before the year turns cannot be reconstructed even though the
account itself still exists. Say which kind a lost file would be, not just
that it is unprotected.


## 3. Resolve — close out theses and predictions that have run their course

**This runs BEFORE tagging, and that order is load-bearing.** Tagging can
supersede a thesis, and `open_theses` subtracts superseded versions — so a
thesis resolved after it has been superseded is a thesis you can no longer
see. Resolve first, tag second.

D6 defaults to prompting for a prediction on every tag group (Step 4), so
something has to ask the reverse question — whether an open thesis or an
outstanding prediction should now be closed out. Without this step D6's
prompt is pure cost: the user answers the hardest question on every group
and nothing ever adjudicates it. `thesis_retired` and `prediction_resolved`
are existing journal events and `count_quarter` already reads them.

On a first-ever run this step finds nothing and falls through immediately.
**It uses `due`, `open` and `tag`.**

**Read the clock FIRST, before anything else in this step**, and capture
what it prints — `due` needs it, and so does the `tag` block at the end:

```bash
cd "<captured-abs-ROOT>"
date -u +%Y-%m-%dT%H:%M:%SZ          # FIRST — capture this, substitute it below
```

1. List the predictions that have come due, and ask the user about each row
   it prints:

   ```bash
   cd "<captured-abs-ROOT>"
   PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
   "$PYBIN" -m scripts.track_record due --as-of <captured-NOW>
   ```

   It prints nothing when nothing is due. Each row is
   `thesis_id,deadline,probability_pct,thesis_open,proposition`, and the
   `thesis_open` column says whether to also ask about retiring the thesis.
   `--as-of` is required and never falls back to the clock: substitute the
   reading captured just above, not a fresh one. **Check the exit code, not
   just the output** — an exit 1 with a `REFUSED:` line leaves stdout empty
   too, exactly as the next item describes for `open`, and it means the
   same thing: relay it verbatim and STOP.

   This is the only surface that raises a matured prediction: nothing else
   in this skill asks about one, and item 3 below deliberately says
   nothing about a thesis whose prediction has not come due. Skip this item
   and the whole step falls through in silence, this session and every
   later one.

2. List what is currently open:

   ```bash
   cd "<captured-abs-ROOT>"
   PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
   "$PYBIN" -m scripts.track_record open
   ```

   This prints `<thesis_id> - <intent>`, one line per open thesis. **Split
   on the FIRST ` - `**: the id cannot contain that separator (`tag`
   refuses one that does), while the intent is prose and may.
   **Check the exit code, not just the output.** Exit 0 with no output
   means there is genuinely no open thesis. Exit 1 with a `REFUSED:` line
   on stderr means the journal itself is unreadable or malformed — its
   stdout is ALSO empty, so the two are indistinguishable by output alone.
   On a refusal, relay it verbatim and **STOP**: do not fall through to
   tagging, which would append to a journal already known to be broken.

3. For each outstanding, past-deadline prediction: **ask the user** whether
   the `proposition` came true — `hit`, `miss`, or `unobservable` (the
   broker/market never gave a clean answer either way). If that prediction's
   thesis is still open, ask in the same turn whether the thesis should be
   retired (`invalidated` it was wrong, `played_out` it fully resolved,
   `abandoned` they moved on) or left open.

   Never infer a result or a reason from price action or any other signal —
   this is the same one-line, user-supplied judgment Step 4 collects, not an
   analysis this skill performs.

   **For an open thesis with no prediction, or one not yet past deadline,
   say nothing.** There is no place to record "asked, and the user declined
   to retire it" — declining writes no event — so a later session cannot
   tell that from "never asked", and the question would return every single
   session forever. A thesis with no prediction never becomes past-deadline
   at all. Retiring one of these is user-initiated: act when they bring it
   up, and otherwise leave it alone.

4. Emit what the user told you — `prediction_resolved` before
   `thesis_retired` when both apply to the same thesis (the prediction
   belongs to the version being retired, so resolve it first).
   **Chain them with `&&`:** these are two independent appends, not a
   transaction. Run unchained, a failed first command still falls through
   to the second, and the shell's final status is the second command's —
   so the block looks successful while the journal holds only half of what
   the user said.

   **Show the user both event JSONs — `prediction_resolved` and
   `thesis_retired` — exactly as you will append them, and wait for a yes.**
   The journal is append-only: after this block the only route back is
   Step 5's in-place correction, which rewrites history and has no undo of
   its own.

   ```bash
   cd "<captured-abs-ROOT>"
   PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
   "$PYBIN" -m scripts.track_record tag --at <captured-NOW> < /absolute/path/to/resolved_event.json \
     && "$PYBIN" -m scripts.track_record tag --at <captured-NOW> < /absolute/path/to/retired_event.json
   ```

   `prediction_resolved` needs `{"event": "prediction_resolved",
   "thesis_id": "<id>", "result": "hit"|"miss"|"unobservable"}`;
   `thesis_retired` needs `{"event": "thesis_retired", "thesis_id": "<id>",
   "reason": "invalidated"|"played_out"|"abandoned"}` — `tag` stamps
   `recorded_at` itself. A refusal means that command wrote nothing —
   with ONE exception, which the refusal names itself: a message containing
   `WRITE FAILED after validation passed` means the append reached the disk
   and stopped partway. **That phrase alone does not tell you the state of
   the file, and the two states need opposite next actions** — read the
   rest of the sentence. `byte-for-byte what it was before this call` means
   the partial write was undone and nothing was lost: fix what the error
   names and retry. `may now hold a PARTIAL line` means it was NOT undone:
   read the file's last LINES — **and show them to the user before you
   touch anything, because one branch below deletes one and those bytes
   are the only record of the attempt** — then decide by **whether your
   event is there at all**, identifying it by `event`, `thesis_id` and
   `recorded_at`. It is usually the last line and need not be: the writer
   deliberately does NOT roll back when the file grew by more than its own
   write could explain, because another process appended during it and
   truncating would delete that process's event — so your line can sit
   above a foreign one. Valid JSON alone does not settle it: the append can fail
   before writing a single byte, and then the last line is the PREVIOUS
   event, perfectly valid, and reading validity as success leaves the
   refused event permanently unwritten while you report it as stored.
   **It IS your event, and complete** — it was written and only its
   durability confirmation failed; keep it, and do NOT re-run, which would
   append a duplicate the fold then refuses. **Then run the SECOND command
   of the chain on its own.** A nonzero exit stopped `&&` from ever
   invoking it, so the event the user asked for last is simply missing —
   and re-running the whole chain cannot fix that, because the duplicate
   first event is refused before the second is reached. This is the one
   case where the two commands are run separately; everywhere else the
   chain is what stops a failed first event from being followed by a
   second that depends on it. **And when it is the SECOND command that
   emitted `WRITE FAILED after validation passed`, this procedure applies
   to IT** — not the general "re-run only the failed one" further down.
   That command exited nonzero, so the shorter rule reads it as a failure,
   while its event may be sitting complete in the journal; re-running is
   then refused as a duplicate and the agent reports a failure for a fact
   that WAS stored. Whichever command emitted the message, read the last
   lines before deciding. **The same applies when the second command
   never ran for any other reason** — the session ended, the turn was
   interrupted — and it matters most here: once `prediction_resolved` is on
   disk the prediction is no longer outstanding, so a later session will
   not raise this thesis again and the retirement the user asked for is
   simply lost. If you cannot run the second command now, say so in the
   same turn and name what is missing; nothing else will. **It is a truncated version of
   your event** — delete YOUR event's bytes, leaving the file ending in a
   newline, and re-run. **Not "that whole line" if something follows it on
   the same line**: another process can append immediately after your
   partial JSON with no separator between them, and the writer deliberately
   leaves that layout alone rather than truncating over a foreign event it
   was told was recorded. Deleting the line would delete theirs too, and
   they have already been told it is stored. Split the line and keep their
   half. **It is an earlier event, untouched** — nothing was
   written; re-run as you would after any other refusal. Relay any
   refusal verbatim either way.
   **`&&` stops the chain, but it does not roll back what already
   succeeded** — if the second command fails, the first one's event IS in
   the journal. Re-run only the failed event, never the whole chain.

   **If you may already have started this pair, do NOT re-run the `&&`
   chain. Run `prediction_resolved` ALONE, and then decide about
   `thesis_retired` from what the journal says afterwards — not from what
   that command printed.**

   **This pair needs a detector that Step 4's pair does not, and applying
   Step 4's rule here retires a thesis the journal cannot back.** There, a
   `thesis_opened` that genuinely FAILED takes `orders_linked` down with
   it: the link names a thesis that is not declared, and the journal
   refuses it, so running the second alone is safe whatever the first
   said. Here BOTH events name a thesis that is already declared, so
   `thesis_retired` succeeds whether or not `prediction_resolved` landed.
   Run it on a session where the resolution genuinely failed, and the
   thesis is retired with the user's answer unrecorded.

   The detector is `due` — a row on stdout, not a reading of any refusal's
   wording:

   ```bash
   cd "<captured-abs-ROOT>"
   PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
   "$PYBIN" -m scripts.track_record due --as-of <captured-NOW>
   ```

   `due` lists a past-deadline prediction for exactly as long as it is
   unresolved, and drops it the moment a `prediction_resolved` for it is on
   disk — retired or not, since it reports the thesis's open state in its
   fourth column rather than filtering on it. So, after running
   `prediction_resolved` alone:

   - **exit 1 with a `REFUSED:` line** → `due` could not read the journal.
     Its stdout is empty on a refusal, and reading that emptiness as "no
     longer listed" is the one misread that would send you on to retire the
     thesis. Relay it and STOP.
   - **this thesis is NO LONGER listed** → the resolution is on disk, either
     because it landed before the interruption or because the command you
     just ran put it there. Run `thesis_retired` alone.
   - **this thesis is STILL listed** → the resolution is NOT on disk, and
     the command you just ran did not put it there either. **Do not run
     `thesis_retired`.** Relay both outputs and STOP. The user's answer to
     the prediction is what is at stake, and retiring first buys nothing:
     it leaves a thesis closed on a judgment the journal does not hold.

   Re-running the whole chain instead of this is the trap: `&&` will not
   reach the second command after the first is refused as a duplicate, so
   the interruption survives the recovery attempt unchanged.

   Relay every output. What this recovery leaves standing, said plainly
   rather than implied away: **it recovers the pair, it does not make
   retiring-without-resolving unrecoverable-proof.** If `thesis_retired`
   does land while the prediction is still unresolved, the loss is bounded
   — `due` goes on listing that prediction, with `no` in the `thesis_open`
   column, so a later session raises it again and it can still be
   resolved. What is gone is the session in which the user answered.

   Never allocate a fresh `thesis_id` to get past a refusal. It is
   append-only: that leaves two declarations for one decision, `open`
   shows both forever, and only Step 5's destructive correction can
   reconcile them.

   Step 3's own interruption is the sharper one: once `prediction_resolved`
   is on disk the prediction is no longer outstanding, so `due` never raises
   that thesis again and the retirement the user asked for is lost with
   nothing pointing at it. `open` still shows the thesis, which reads as an
   ordinary open position.

**After the `tag` command(s) above succeed, say that `trade-journal.jsonl`
has changed and exists only here.** It is the one file with no other source:
the broker can re-answer what was traded, nobody can re-answer why.

If a step above turns out not to be possible with the `due`/`open`/`tag`
combination described here — some shape this skill did not
anticipate — **stop and say so** rather than inventing a workaround.

## 4. Tag — group orders into a thesis

**The instant the user finishes stating a thesis, before you look anything
up, run `date -u +%Y-%m-%dT%H:%M:%SZ` and hold what it prints — and if they
stated it in the message that STARTED this session, that instant was before
the prelude, before the pull, and before the resolve step, so the reading
belongs there and not here.** This step is fourth in the running order;
taking the clock when you arrive at it stamps the thesis with a time the
user did not speak at, by however long steps 0-3 took. Read the clock at the
top of the session in that case, hold it through everything else, and use it
here.

That value is the `--at` for BOTH commands of THAT ONE thesis — not for the
step. If the user names several groups in one session, read the clock again
each time they finish stating the next one: reusing the first reading stamps
later theses with a moment their user had not yet spoken at, and across a
quarter boundary counts them in the wrong quarter. It has to be read when
they speak because everything after that — reading the backlog and
building the event files — takes minutes, and `recorded_at` is when the
user spoke, not when you finished. On the last night of a quarter those
minutes are the difference between recording the thesis in this quarter and
the next. The rule and its reasons are restated where the commands are; the
reading itself cannot wait until then.

List the fills awaiting a decision:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.track_record unlinked
```

This prints
`account,order_id,date,side,size,symbol,price,company_name` — one line per
filled ORDER (not per fill) the journal has not yet linked to a thesis, most
recent first, as proper CSV (a field containing a comma is quoted). The
first two columns (`account,order_id`) are the machine key the `tag` step
keys off (below); the rest exist so a person can actually recognize which
decision an order was — a bare `account,order_id,symbol` line gives no way
to tell one order from another once there are hundreds in a quarter.

**`size` and `price` describe the WHOLE order:** size is every fill summed,
price is their volume-weighted average. `date` and `side` come from the
order's LAST fill — the date is when the decision finished. So a 1,000-share
order that filled in tranches shows 1,000 and its blended price, not the
tail. (A single-fill order prints the broker's own values untouched.)

**`company_name` is reduced across EVERY fill of the order, not read off
one of them.** It is the agreed name when every fill names the same
company; `?MISSING-ON-SOME` when some fills of the order carry a name and
others do not; `?CONFLICTING` when two fills disagree about the name OR
about the symbol (a side-only disagreement is not this — it already has
its own NOTE below, and says nothing about WHICH security this is). Both
sentinels start with `?`, because no real company name does, and both are
also named on stderr with the order and the values. This is the identity
signal the rest of this step uses — see "does this group split?" below.

**Except when the command says otherwise on stderr.** When an order's fills
cannot be summed — a non-numeric size or price, a total of zero, or fills
that disagree about the symbol or the side — the row falls back to the LAST
fill's values and the command still exits 0, because a to-do list should
not be withheld over one unsummable order. It names that order in a `NOTE:`
saying the columns are the last fill and not the whole order. The row
itself is shaped exactly like an ordinary one, so the NOTE is the only
thing that distinguishes them — which is why every NOTE is relayed before
you act on the result.

**Two of those NOTEs are about the ORDER ID, and both change what you may
link.** A row is grouped and printed under the id its executions were FIRST
archived under — that is the id an earlier listing showed and the id a link
keys off — but the broker restates that field afterwards, and then the
printed id is no longer what it says today:

- **"merge executions the archive's latest pulls assign to DIFFERENT
  orders"** — one execution was re-keyed away from its siblings, so the
  row's `size` and `price` sum executions that are no longer one order. Do
  NOT link it as one order. One order cannot be split — `load_fold` gives
  it exactly one owning thesis — so it waits, the same way a
  `?CONFLICTING` order waits: tell the user the id it prints under and the
  ids its executions carry now, and leave it unlinked until the broker's
  own record settles.
- **"carries NO order id at all"** — the newest sighting of one of its
  executions has no order id. The NOTE says that and stops there, and so
  must you: the archive records that the field is absent, not why, and a
  broker taking the id back and a sparse response that omitted it are the
  same bytes here. Either way a ref built on the printed id may never
  match a future pull, so it would sit unmatched while looking tagged.
  Name it — as an absent id, not as a broker decision — and leave it
  unlinked unless the user, told that, still wants it linked. This NOTE
  also fires for an order ALREADY tagged, and that is the quieter case:
  such an order appears in no list at all, so this line is the only thing
  that will ever mention it.

As in Step 3, an empty stdout with exit 1 and a `REFUSED:` line is a broken
archive or journal, not an empty backlog — relay it and STOP.

**Grouping orders into a thesis is the judgment this skill exists for — ask
the user, never infer a grouping from symbols or timestamps.** For each group
the user names as one decision, collect from them:

- `intent` — the thesis, one line
- `kind` — `long` or `hedge`
- whether it supersedes an open thesis, and if so which one. Step 3 already
  printed the open theses via `open`; re-run it if that list is stale.
- a prediction (`proposition`, `deadline` with a timezone, `probability_pct`
  0–100) — per D6, **prompt for this by default**; whatever D6 says, accept
  one whenever the user volunteers it unprompted. All three fields or none.

**Before the user's first tag in a session, tell them once:** `orders_linked`
is journalled and `unlinked` genuinely shrinks as they tag — that half works
— but the **verified count of linked orders is permanently `unavailable`**,
because D1 found the broker reports only live working orders, never a
history to check a link against. This is expected, not a bug in the tool.

**FIRST: does this group split?** The eighth column is computed per ORDER,
so it cannot express this by itself, and it is the rule that can attach an
order to the wrong security. Before appending anything, compare the column
across every row of the group. A thesis carries one `instrument`, so:

> **Two steps, in this order.** They compose, and the order is what makes
> the answer single-valued: a group with two symbols where one of them is
> only partly named has two defensible readings if you do them the other
> way round, and they write different thesis ids into an append-only file.
>
> **(i) Partition by SYMBOL.** `symbol_at_write` is the symbol carried on
> the fills THEMSELVES — `unlinked`'s sixth column, read directly off each
> row of the group, the ticker as of the TRADE, never a fresh lookup of
> today's ticker. (An earlier version of this step preferred a live
> positions-pull reading over the archived fill's own symbol; that
> mechanism is gone — there is no live lookup left to prefer, and using
> one would journal today's identity against a trade made months ago,
> the exact ticker-reassignment error this rule exists to prevent.) One
> thesis per distinct `symbol_at_write`, each linking its own orders, with
> the same intent text naming the other leg. This is forced: a thesis
> carries one `instrument`. Two symbols sharing a company name are still
> two theses — a name is evidence about an instrument, not a claim that
> two tickers are one.
>
> **(ii) Within each partition, decide the name from ITS rows only.** Two
> of the values `unlinked` prints in that column are NOT names, and reading
> them as names is what makes the cases below ambiguous: `?CONFLICTING`
> says that ORDER's own fills named two companies (or traded two symbols),
> and `?MISSING-ON-SOME` says some of its fills named one company and some
> named none. So:
> - any row `?CONFLICTING` → that ORDER leaves the thesis and stays
>   UNLINKED, and the partition's name is decided from the rows that
>   REMAIN. It is not one of "two different names" below, and it does not
>   by itself stop the group: the rest is still tagged, and you tell the
>   user which order and which two values. (The table further down says the
>   same thing; this is where you apply it.)
> - every remaining row carries the same real name → that name;
> - some remaining rows named and some not — blank, or
>   `?MISSING-ON-SOME` → the partition does NOT get the name.
>   Not the name from the rows that have it: writing one observed on some
>   rows and not others asserts a group-wide fact from partial evidence.
>   It takes `{"symbol_at_write": "<sym>", "instrument_unresolved": true}`,
>   and you TELL the user which of its orders carried a name and which did
>   not;
> - two different REAL names under the SAME symbol → STOP and ask. The
>   broker named two companies for one ticker, which is a question, not
>   something to average or downgrade.
>
> Worked example, because this is the case two readings diverge on. Rows
> `(AAA, ACME)`, `(AAA, —)`, `(BBB, BETA)` give exactly TWO theses: AAA
> with `instrument_unresolved: true` (partly named), and BBB with
> `company_name_at_trade: "BETA"`. Not three — "one per exact
> `(symbol, name)` pair" applied first would have split AAA in two and
> given one half a name its sibling orders contradict.
>
> Step (ii) is the same rule `?MISSING-ON-SOME` applies inside one order,
> one level up — and it is the case the column cannot show you, because
> per order each row reads as an ordinary agreed name.

Every row can carry an ordinary agreed name and still be two securities,
and a group can be part-named with every individual row looking fine;
nothing warns in either case, because the column is computed per order.
This comparison is the only thing standing between "I grouped these two
orders" and one of them being permanently recorded under the other's
company.

**A deadline already in the past is caught by `tag`, not by prose** — Step
3's `due` command reads it. Both values are in that command's hands and
the comparison is arithmetic, so a paragraph asking an agent to remember
to compare two timestamps is the wrong layer for exactly the reason this
whole skill exists:

> `tag` NOTEs a prediction whose deadline is at or before its own
> `recorded_at`. Relay that NOTE: it means the proposition was already
> decidable when recorded, which may be what the user meant and may be a
> mistyped year. It does not block the append.

**Order references come from `unlinked`. An order that is not there is
SAID OUT LOUD — not refused.** An earlier draft of this step said to
refuse it; that was wrong, and the codebase had already decided the other
way for a reason this operator relies on:

> `orders_linked` must accept an order the archive has not seen — the
> operator can tag right after placing a trade.
> — `test_a_linked_ref_matching_no_archived_order_is_noted` in this
>   codebase's own test suite, pinning the decision

The check already exists and already says the right thing: `unlinked`
NOTEs any linked ref matching no archived order, in those words, adding
that a typo and a not-yet-pulled order look identical and that the real
one "will silently suppress the real one when it arrives"
(`__main__.py:462-503`). So the skill's job is to route the operator to
that NOTE, not to block them:

> Take `(account, order_id)` from `unlinked`'s first two columns. If the
> user names an order that is NOT in that list, do not just build the
> ref: say which one, and ask which of THREE things it is. Two are
> ordinary — they are tagging a trade placed since the last capture
> (legitimate and common), or the id is mistyped — and the journal
> accepts both, because it checks only that the thesis exists and the key
> is unlinked. The third is the one `unlinked` itself causes: the order is
> ALREADY LINKED to another thesis, and that list subtracts every linked
> order before it prints, so a correctly-typed id for a real order the
> user really does own is missing for that reason alone. That third case
> the journal REFUSES — `order ... is already linked to thesis ...`,
> naming the owner — so say it is possible BEFORE you build the ref, and
> if the refusal comes, take Step 4's `order ... is already linked`
> procedure (check first whether it is a reused id, then let the user
> choose) rather than improvising. The cost of a typo
> is delayed and silent: when the real order is finally pulled,
> `unlinked` drops it as already linked and a genuine fill leaves the
> backlog having never been seen. `unlinked` will NOTE the unmatched ref
> every session until a pull matches it — relay that NOTE, it is the only
> thing that will ever mention it again.

**THEN, for each thesis the split produced:** collect the eighth-column
values of ALL of that thesis's rows — a thesis usually links several
orders, so there is no single value to read off — and reduce them:

```
every row the same real name -> {"symbol_at_write": "<sym>",
                                 "company_name_at_trade": "<that name>"}

any row ?MISSING-ON-SOME,    -> {"symbol_at_write": "<sym>",
 or blank                        "instrument_unresolved": true}

two different real names     -> STOP and ask. Do NOT write unresolved: the
 across the rows                 broker named two companies for one ticker,
                                 which is positive evidence that two
                                 securities traded under it. Recording
                                 "we could not identify this" when what
                                 happened is "the broker said two different
                                 things" destroys the evidence, and the
                                 orders leave the backlog so nothing brings
                                 it back. Split by (symbol, company_name),
                                 or leave the group untagged, as the user
                                 decides.

any row ?CONFLICTING         -> that ORDER is dropped from the thesis and
                                stays UNLINKED. One order cannot be split:
                                `load_fold` gives it exactly one owning
                                thesis. The REST of the group is still
                                tagged. Tell the user which order and which
                                two values; it waits until the broker's own
                                record settles.
```

The reduction is over ROWS, not a lookup on one of them.

**Absence and disagreement end differently, and an earlier draft of this
table collapsed them.** Some rows named and some not is missing evidence:
`instrument_unresolved` says exactly that, and is honest. Two different
real names is CONTRADICTORY evidence, and "unresolved" would be a false
statement about what happened. The same split runs one level down, inside
one order, as `?MISSING-ON-SOME` versus `?CONFLICTING`; this is that rule
applied across the orders of one thesis, so there is one rule to
remember, not two.

**`--at` must come from the clock, never from your own sense of the time —
and read that clock when the USER STATES the thesis, not when you finally
emit the event.** `recorded_at` is defined as when the user actually thought
this; reading the backlog and the event construction in between can take
minutes, and on the last night of a quarter that is the difference between
recording it in this quarter and the next. This is the reading taken at the TOP of
this step, before any of that — pass that same held value to both
commands below, the same way Step 2 captures `pulled_at` before the call
rather than after it. If you did not take it then, say so to the user and
take it now rather than reconstructing one: a stamp minutes late is a fact
about this session, a stamp from memory is a fiction. A stamp you compose from memory can be
hours wrong while looking perfectly ordinary, and a later reader has no way
to tell an invented stamp from a real one.

**`recorded_at` is when the statement was RECORDED — always now, never
backdated.** Tagging a trade from six months ago is the ordinary case, not
an edge one, and the temptation is to stamp it with the trade's date so it
counts in that quarter. Do not: this journal's whole value is that it holds
what someone said at a moment they could not yet know the outcome, and a
backdated line claims a foresight nothing establishes. When the decision was
made is part of the `intent` text, where it reads as a recollection, which
is what it is. The CLI notes a stamp more than a day off, or
one landing in another quarter; it cannot see a plausible one that is simply
wrong.

Emit, in order — `append_event` validates the **whole resulting journal**
before writing a byte, so a validation refusal leaves the file untouched;
relay it to the user verbatim. The one refusal that does NOT mean "nothing
happened" says so in its own text (`WRITE FAILED after validation passed`):
the disk write failed partway. Which of its two states you are in — undone,
or a partial line left behind — is in the rest of that message, and Step 3
gives the procedure for both; do not read the phrase alone as either one. **Chain the two commands with `&&`, for the same reason
as Step 3:** they are two independent appends, and unchained, a failed
`thesis_opened` still falls through to `orders_linked`, whose exit code then
becomes the block's — leaving order links pointing at a thesis that was
never declared, under a block that looked like it succeeded.

**Choose the declaring event BEFORE you build the file**, because the
command below archives whatever you built and there is no way to change it
afterwards. Use `thesis_opened` (allocate a fresh `thesis_id`, e.g. a short
slug you haven't used) when this is a new decision, and `thesis_superseded`
(same shape, plus `"supersedes": "<the prior thesis_id>"`) when the user is
REPLACING a thesis they already have — a changed view of the same position,
not a second position. Getting that wrong is not a formatting mistake: a
replacement written as `thesis_opened` leaves the old thesis open, so the
journal states two concurrent theses where the user stated one succeeding
another, and `open` keeps showing the superseded one forever. The CLI
accepts both shapes and nothing later can tell them apart.

Follow it with one `orders_linked` naming that group's `(account,
order_id)` pairs from `unlinked`'s first two columns.

**The exact shapes.** Build these, do not reconstruct them from memory:
every level of every one of these objects is a CLOSED key set, so a
misspelled key is REFUSED rather than stored — which is the safe
direction, but only if what you send is right the first time. Leave
`recorded_at` out; `tag --at` stamps it, as in Step 3.

A new decision:

```json
{
  "event": "thesis_opened",
  "thesis_id": "<a short slug you have not used>",
  "kind": "long",
  "intent": "<the thesis, one line, in the user's own words>",
  "instrument": {"symbol_at_write": "<SYM>", "instrument_unresolved": true}
}
```

`kind` is `long` or `hedge`, nothing else. `thesis_id` is an identifier,
not prose: non-empty, and it may not contain ` - ` (that is `open`'s field
separator, and two theses would print the same line) or a newline or a tab.
`intent` is non-empty and not just spaces.

A replacement for a thesis the user already holds — the same shape, with
one more REQUIRED key:

```json
{
  "event": "thesis_superseded",
  "thesis_id": "<a NEW slug, for this version>",
  "supersedes": "<the prior thesis_id, taken from `open`>",
  "kind": "long",
  "intent": "<the revised thesis, one line>",
  "instrument": {"symbol_at_write": "<SYM>", "company_name_at_trade": "<NAME>"}
}
```

`supersedes` belongs to `thesis_superseded` alone — the CLI refuses it on a
`thesis_opened` by name.

`instrument` is exactly one of the two shapes the reduction table above
selects between, and the two examples above show both:

```json
{"symbol_at_write": "<SYM>", "company_name_at_trade": "<NAME>"}
{"symbol_at_write": "<SYM>", "instrument_unresolved": true}
```

`instrument_unresolved` is a JSON `true`, never the string `"true"`, and it
may not sit beside a `company_name_at_trade` — its whole job is to say the
instrument could NOT be identified, so a name beside it contradicts it and
is refused.

A prediction, when the user gives one, rides INSIDE the declaring event
above — all three keys, or the whole object left out:

```json
  "prediction": {
    "proposition": "<the falsifiable claim, one line>",
    "deadline": "2026-12-31T17:00:00-05:00",
    "probability_pct": 65
  }
```

`deadline` is any ISO-8601 instant carrying a timezone — an offset like the
one above is fine, a bare date is refused. `probability_pct` is a number
from 0 to 100, not a fraction. A prediction is accepted ONLY on
`thesis_opened` / `thesis_superseded`: on any other event it is refused,
because only a declaring event's prediction is ever read back and `due`
would never raise it again.

And the link:

```json
{
  "event": "orders_linked",
  "thesis_id": "<the SAME thesis_id as the declaring event above>",
  "order_refs": [
    {"account": "U1", "order_id": "123456789"},
    {"account": "U1", "order_id": "123456790"}
  ]
}
```

`order_refs` holds at least one ref and each ref holds `account` and
`order_id` and nothing else — both **strings**, quoted. A bare number is
refused rather than coerced: the broker side of that comparison is a
string, so a numeric id would match nothing, and the order would sit on the
backlog forever while the journal claimed it was tagged.

> **Show the user both event JSONs — the declaring event and the
> `orders_linked` — exactly as you will append them, and wait for a yes.**
> The journal is append-only: after this block, the only route back is
> Step 5's in-place correction, which rewrites history and has no undo of
> its own. Reading them back is seconds; the alternative is a permanent
> line the user never agreed to.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
# <captured-NOW> is the stamp taken when the user STATED THIS GROUP — see
# the per-group rule at the top of this step, not a fresh reading here.
"$PYBIN" -m scripts.track_record tag --at <captured-NOW> < /absolute/path/to/thesis_event.json \
  && "$PYBIN" -m scripts.track_record tag --at <captured-NOW> < /absolute/path/to/orders_linked_event.json
```

If the second command fails, the first one's
event is already written — fix and re-run only the failed one. **"Fix" is
not yours to choose when the refusal is `order ... is already linked`** —
and **check first which of two things it is**, because the obvious reading
can be false: the id may belong to another thesis, or the broker may have
REUSED it for a different instrument, which `unlinked` reports in its own
NOTE about orders holding executions that disagree about symbol or side. If
the symbols differ, telling the user "that order belongs to thesis T" is
wrong — it is a different order wearing the same number, and the remedy is
not to move or drop anything but to record that collision for them.
Otherwise, that order belongs to another thesis, and the three ways forward store
materially different histories. Tell the user which thesis holds it, and
let them pick: drop that order from this group (a smaller group than they
named), move it (a Step 5 correction to the OLD link, with all that step's
warnings), or leave this thesis declared with no orders yet. The last is a
legitimate resting state, not a broken one — but be exact about what it
leaves behind, because the obvious description is wrong: the ALREADY-LINKED
order does NOT return to `unlinked`, since its old link still suppresses it
there. What stays visible is the new thesis in `open` with nothing
attached, and whichever orders of the group were genuinely unlinked. That
already-linked order is findable only through the thesis that owns it. Do NOT
quietly drop that order's reference and retry: that stores a grouping
the user never stated, and nothing later can tell it apart from one they
did.

**If the FIRST command is the one that failed with `WRITE FAILED after
validation passed`, "re-run it" can be the wrong action** — follow Step 3's
procedure for that message, in full: read the last line to see whether it is
the event you tried to append; if it is, the declaration WAS written and
re-running the chain refuses the duplicate before ever reaching the link, so
run the `orders_linked` command ALONE. `&&` stopped it from running at all,
and nothing else will.

**And the resume rule for this step's own pair. It is NOT the same one
Step 3 uses, and the difference is load-bearing.** Here a `thesis_opened`
that genuinely FAILED takes `orders_linked` down with it — the link names a
thesis that is not declared, and the journal refuses it — so running the
second alone is safe whatever the first said, and that refusal is the whole
backstop. Step 3's pair has no such backstop, because both of its events
name a thesis that is already declared; it uses a `due` check instead. Do
not carry either procedure across to the other pair. If the shell dies, the turn is interrupted, or the user
walks away between the two appends, the first event is durable and the
second never happened, and the next session cannot see that from `open` or
`unlinked`.

> **If you may already have started this pair, do NOT re-run the `&&`
> chain. Run the two commands SEPARATELY: the first alone, then the
> second alone — whatever the first said.**
>
> Both outcomes of the first need the second. If it succeeded, the pair
> was never started and the second is still owed. If it was refused as a
> duplicate, the first event was already on disk and the second is what
> is missing. Re-running the chain instead is the trap: `&&` will not
> reach the second command after the first refuses, so the interruption
> survives the recovery attempt unchanged.
>
> Relay both outputs. Do not read the refusal's WORDING to decide what to
> do — the design forbids branching on stderr text, and you do not need
> to here: the answer is the same either way. If the first fails for some
> other reason, the second fails too (it references a thesis that is not
> declared), and both messages go to the user.
>
> Never allocate a fresh `thesis_id` to get past a refusal. It is
> append-only: that leaves two declarations for one decision, `open`
> shows both forever, and only Step 5's destructive correction can
> reconcile them.

The interrupted state PRESENTS as an open thesis with no linked orders:
`open` shows the new thesis and `unlinked` still shows its order, which
reads as an ordinary untagged order beside an ordinary open position.
Nothing else distinguishes it.

**After the `tag` command(s) above succeed, say that `trade-journal.jsonl`
has changed and exists only here** — same as Step 3.

## 5. Correct — fix a transcription error

**Read this whole step before you change a byte.** The edit is
irreversible — there is no correction subcommand and no history — and the
two things that make it safe (showing the user the old text, and giving them
the chance to keep their own copy) both have to happen BEFORE it. Executed
top to bottom, an instruction to edit followed fifty lines later by an
instruction to show the old line destroys the only copy before the operator
gets the chance this step promises them.

There is no correction subcommand. Fix a mistagged line by **editing it in
place** in `<captured-abs-ROOT>/trade-journal.jsonl` (one JSON object per
line, via the Edit tool at that absolute path) — change only the content
field(s) that were wrong. **Never move `recorded_at` to now**: it records
when the statement was made, not when a mistake in transcribing it was
noticed.

The one case where it IS the field to fix is when the timestamp is itself
the transcription error — an agent that substituted a stale captured clock
value, so the line claims a moment the user did not speak at. That is the
same kind of mistake as a mistyped symbol, and it decides which quarter
counts the thesis, so leaving it wrong is not neutrality. Correct it to the
actual statement time, say what it was and what it becomes, and never to a
time chosen to move the thesis into a different quarter. Keep the line valid
JSON.

**One case needs a whole line removed, not a field changed: an
`orders_linked` event whose ONLY reference is the order being moved.**
Emptying `order_refs` is refused — the list must be non-empty — so editing
in place has no legal result, and leaving it keeps the new link refusing as
already owned. Delete that entire line. It is the same destructive act as
any other edit here and takes the same protocol: show the user the exact
line first, make the change, re-run `open`, and restore the line verbatim
if it refuses. Say plainly that a recorded link is being removed, not
amended — the old thesis will afterwards read as having been declared with
no orders ever attached, which is a true statement about the journal and a
false one about what happened.

**If the field is `thesis_id`, every line that REFERENCES it must change in
the same edit.** A `thesis_id` is the join key: `orders_linked`,
`prediction_resolved`, `thesis_retired` and any `supersedes` all name it, and
the fold refuses a reference with no declaration. Renaming the declaration
alone leaves those lines pointing at an id that no longer exists — and since
the old line is already destroyed, `open`, `unlinked` and every future `tag`
then refuse the WHOLE journal, with no undo. Grep the file for the old id to FIND
the lines, then change it only where it is the join key — the `thesis_id`
field and any `supersedes` — never inside `intent` or a prediction's
`proposition`, where the same characters are the user's own prose and
rewriting them silently edits what they said. Change all the key
occurrences together, and re-run `open` before telling the user anything. **If `open` refuses after the edit, restore the exact original
text of EVERY line you touched** — this is a multi-line edit, and restoring
only the declaration leaves the references pointing at an undeclared id
while restoring only a reference does the inverse; either way the journal
stays refused. So show the user every affected line before you start, not
just the one they named: that display is the only copy of all of them.

**Change only the field the user named.**

> "The ticker on that line should be NVDL" still has two readings, and
> `company_name_at_trade` is now the field at risk beside the symbol.
> Ask the user which they mean.
>
> - **The symbol was mistyped for the same instrument.** The name came
>   off those very fills and is still correct. Fix `symbol_at_write`
>   alone and LEAVE the name — replacing a correct contemporaneous fact
>   with `instrument_unresolved` would destroy the one thing this record
>   exists to hold.
> - **The wrong instrument was journaled entirely.** The corrected
>   symbol's orders are different orders, so the name on this line was
>   observed on someone else's fills. Replace BOTH fields, and the
>   identity becomes `{"symbol_at_write": "<corrected>",
>   "instrument_unresolved": true}` — that is what "never observed on
>   these fills" means, and a later session can re-tag from the backlog
>   if the orders are unlinked.
>
>   **"If the orders are unlinked" is a job, not an assumption — this
>   thesis's `orders_linked` lines still name the WRONG instrument's
>   orders, and nothing will ever object.** They are references to REAL
>   orders, so they are structurally valid; `open` reads them as fine,
>   `unlinked` keeps suppressing them as claimed, and the quarterly
>   report goes on attributing another security's fills to this thesis
>   for as long as the journal exists. Editing the identity fields alone
>   makes the line SAY unresolved while the fills under it stay someone
>   else's. So in the SAME edit, before you re-run `open`: take this
>   thesis's wrong-instrument refs out of the journal — delete the whole
>   `orders_linked` line where they are its only refs (`order_refs` may
>   not be empty, so there is no in-place edit for that case), or drop
>   just those refs from a line that also holds refs which stay. That is
>   what returns those orders to `unlinked`, where the thesis they really
>   belong to can claim them. This deletion is Step 5's in-place
>   correction — "there is no correction subcommand", above — and not a
>   general licence to remove journal lines: the five event types carry no
>   unlink event, and a non-null `supersedes` is accepted only on
>   `thesis_superseded`, so there is no append-only way to say this — and
>   leaving it unsaid keeps another security's fills attributed to this
>   thesis.
>
>   This is a multi-line edit, so it takes the multi-line protocol stated
>   under `thesis_id` above: show the user EVERY line you are about to
>   touch first, change them together, re-run `open`, and if it refuses
>   restore every one of them verbatim. Do NOT hand-write refs for the
>   corrected symbol's real orders while you are in here — those go
>   through Step 4's `orders_linked` append, which is what checks the key
>   is unlinked. Until then this thesis reads as declared with no orders
>   attached, which is a legitimate resting state.
>
> **`unlinked` is NOT the place to look the name up in either branch.** It
> drops every journal-linked order before it prints (`__main__.py:566`),
> and the order being corrected is linked by definition — the skill
> already says, in Step 4, that an already-linked order "does NOT
> return to `unlinked`". Reading a DIFFERENT order that happens to share
> the corrected ticker would record a company name observed at another
> time against these fills, which is the ticker-reassignment error this
> whole change exists to prevent.

Nothing validates the `symbol_at_write` / `company_name_at_trade` pair —
the schema accepts any well-formed name beside any non-empty symbol — so
an edit to one of them can leave a line asserting one instrument's
identity under another's ticker, which every later read accepts, with the
original `recorded_at` making it look contemporaneous. After editing, run **`open`** once (`cd
"<captured-abs-ROOT>"` first) — **not `unlinked`**: that one reads the
trades archive first and returns on any archive problem before it ever
looks at the journal, so its `REFUSED:` can be about something else
entirely and the check you just made would not have happened. `open`
fold-validates the whole file and
**REFUSE** with exit 1 on a broken edit (bad JSON, a dangling reference, a
duplicate `thesis_id`), which is what makes them a check.

**Do NOT use `report` for this.** It is not a validator: a fold error is
degraded to an `unavailable` line in the summary, `report` still writes
`summary.md` and still exits 0. Using it here reads as confirmation while
proving nothing — and a broken journal that passes as checked is one you
will keep appending to.

**An in-place correction destroys the previous version, and this skill keeps
no history.** Editing in place is the deliberate design (there is no
correction subcommand, and `recorded_at` deliberately keeps saying when the
user actually thought this) — but it means the corrected line afterwards
reads exactly as if it had been written that way at the time, with nothing
anywhere recording that it was ever different.

So **before you edit, show the user the exact line as it stands now, and
after you edit, show the new one.** That puts both versions in front of them
while they can still object, and leaves the old text in this conversation
rather than nowhere. Say plainly that the file itself now carries only the
new version. If they want a durable record of the change — and for a
correction to a decade-long journal they probably do — that is theirs to
arrange before you edit; the skill will not do it for them, because it does
not know where their copies live.

**If `open` refuses after ANY edit in this step, restore the exact original
text of every line you displayed, re-run `open`, and only then tell the user
what happened.** That is the general rule; the `thesis_id` rename and the
single-reference deletion below say it again because they are the two edits
that touch more than one line. The default of relaying the refusal and
stopping is wrong HERE: the old line is already destroyed, so stopping
leaves the whole journal malformed — every later `open`, `unlinked` and
`tag` refuses, while `report` still exits 0 and renders the journal fields
`unavailable`, which reads like an ordinary quiet quarter.

**Once `open` exits 0 on the edited file** — `open`, for the reason given
above; `unlinked` can refuse over the trades archive without ever reading
your correction — say so, and say
that `trade-journal.jsonl` now differs from every copy made before this
session.

## 6. Report — the quarterly summary

**Two things before the report command below, and one of them overrides a
rule you have already read.** The general rule is to stop a step on
`REFUSED:` — here you do not, but only just: `assemble` can create this
quarter's benchmark pin and then fail while writing `summary.md`, so the
post-run pin reading below happens whether the report succeeded or refused.
Stopping first would leave a new, irreproducible, write-once file on disk
that this session never told the user about.

**That exception covers the pin reading and nothing else.** On a refusal,
take the reading, tell the user about the pin if one appeared, relay the
refusal verbatim, and STOP — do not go on to read `summary.md` or present
it. `summary.md` is written with a plain truncating write, so a failure can
leave it absent, stale, empty or half-written, and the one shaped like a
report is the stale one: last quarter's numbers under this quarter's
heading, presented as this run's result.

**And first of all, before the report runs, take a reading of whether this
quarter's benchmark pin already exists.** The report can create
`reports/track-record/<quarter>/SPY.csv` as a side effect, and afterwards
"the file exists" is the same answer for a pin fetched seconds ago and one
fetched two years ago — so announcing an old pin as this run's work, or
missing that one appeared during this run, are both only avoidable by
looking first. Why that matters enough to be the first thing in this step is
below, under "the most fragile thing this step produces".

**Which quarter?** If the user named one, that one. Otherwise ask,
offering the two that make sense: the quarter that just CLOSED — what
a quarter-close session is for — and the CURRENT open one, a progress
read which will refuse on coverage and should. Do not choose for
them: this step writes a `SPY.csv` pin for whichever quarter it
reaches the benchmark step on, once, and a later run does not rewrite
that pin.

```bash
cd "<captured-abs-ROOT>"
PIN="reports/track-record/<YYYYQn>/SPY.csv"
# `-e`, not `-s`: `ensure_pinned` treats the file as pinned when it EXISTS,
# so a zero-byte pin left by an external mishap makes the report refuse to
# refetch. `-s` would call that "no pin", report a fresh fetch that never
# happened, and leave CONTEXT permanently unavailable with nothing naming
# the file to delete.
[ -e "$PIN" ] && echo "PIN_EXISTED_BEFORE=yes" || echo "PIN_EXISTED_BEFORE=no"
```

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.track_record report --quarter <YYYYQn>
```

`make track-record QUARTER=<YYYYQn>` runs the same thing, but do not use it
here: the Makefile falls back to a bare `python3`, which does not exist on a
Windows box whose interpreter is `.venv/Scripts/python.exe`, and `make`
itself may not be installed. Every other block in this skill probes `PYBIN`
the same three ways; this one is not an exception.

This writes and prints the path to `reports/track-record/<quarter>/summary.md`.
It ALSO fetches and writes the pinned SPY benchmark CSV at
`reports/track-record/<quarter>/SPY.csv` (`ensure_pinned`, the design) — but
only the first time this quarter's report reaches the benchmark step at all,
which requires the performance coverage gate, the observations, the endpoints
and the freshness check to have ALL passed. A run that ends in
`unavailable (coverage gap: ...)` writes a summary and no pin; the pin is
then still un-created the next time, and that is correct, not a missed
backup. Once written it is never re-fetched and never overwritten by any
later run, this one or a future one.

**A freshly created pin is the most fragile thing this step produces.** It is
irreproducible: yfinance revises a past quarter's history retroactively, and
freezing it at first-fetch time is the entire reason the pin exists — so if
it is lost, a later regeneration of this same quarter can silently differ
from the report the user actually read. Unlike the archive and the journal,
nothing upstream creates it; it is a side effect of THIS step. So when one
appeared during this run, **say so, and name the path** — it exists only
here.

Read the same path again after the report has run — **including when the
report REFUSED.** The general rule is to stop a step on `REFUSED:`, and
this is its one exception: `assemble` can create the pin and then fail
writing `summary.md`, so stopping first would leave a new, irreproducible
file on disk that this session never told the user about.

```bash
cd "<captured-abs-ROOT>"
PIN="reports/track-record/<YYYYQn>/SPY.csv"
# `-e` here too. The two readings are compared against each other, so a
# zero-byte pin answering `no` to one and `yes` to the other reports a
# fetch that did not happen — or an existing pin as this run's work.
if [ -e "$PIN" ]; then echo "pin present: $PIN"; else echo "no pin exists for this quarter"; fi
```

**Report what those two readings establish, and not a word past it.** They
establish that the pin APPEARED between them — during this run. They do not
establish that this process fetched it: `ensure_pinned` publishes atomically
and yields to a writer that got there first, so a concurrent session can
create the file this report then reads, and the two readings come out
identically either way. Nothing available here separates the two cases, and
whether another session is open on this repo is not something this one can
check — so the narrower claim is the only one to make, always, not a hedge
kept for when a second session is known about.

- first check `no`, second finds it → say **"the benchmark pin appeared
  during this run"** and name the path. Not "this run fetched it".
- first check `yes` → the pin is an earlier run's, and the figures were read
  from it rather than fetched now.

**If CONTEXT is `unavailable` because the pin lacks a date the report needs,
the pin itself is the problem and it will not heal.** The pin is write-once:
once a `SPY.csv` exists for a quarter it is read and never re-fetched, so a
fetch that returned a short or malformed window stays wrong for that quarter
forever. Nothing retries it. The one way out is to **delete
`reports/track-record/<YYYYQn>/SPY.csv` and run this step again** — say that
to the user rather than letting them re-run a report that cannot change.
(Delete only when CONTEXT names the pin. A pin that is merely OLD is
correct — freezing it is the entire point.)

**Read `<captured-abs-ROOT>/strategy.yaml` for `output_language` (default
zh-CN) and, when it is not `en-US`, present the report in that language —
but do NOT translate the file.** `summary.md` is rendered by deterministic
Python and pinned by a golden fixture, because it is a money-path artifact whose refusals must
never look like values; every other skill's `summary.md` is written by an
agent from a prompt, which is why they can emit the target language
directly and this one cannot. So the FILE stays the machine record, in
English, and you are the translation layer — the same role you play for
every other human-facing deliverable in this repo.

Translate **faithfully, not fluently**: every `unavailable (reason)` keeps
its full reason, and the RISK/CONTEXT caveats keep their force. A softened
refusal is the one failure this whole tool is built to prevent. Where a
reason names a file, a date or a field, leave that token in its original
form. Show the raw English block too, so the user can check you.

Show the user `summary.md`'s contents **as-is** — add no interpretation, no
recommendation, and never write a line committing to anything (a promise
about future trades or behavior is not this tool's to make; the design non-goal
6: "any commitment is written by the user"). The report already states its own
caveats (e.g. RISK/CONTEXT being unadjusted, or `unavailable (coverage gap:
...)` when an input is missing) — relay those verbatim too, don't soften or
explain them further.

**Then tell the user what this session produced and where it lives.** All of
it is on this machine and nowhere else — the skill made no copy. List the
paths, and say which of them cannot be re-created:

```bash
cd "<captured-abs-ROOT>"
# The FULL listing, not a tail. This is the inventory of what exists ONLY
# on this machine — truncating it can silently drop the files THIS run
# just archived, which is exactly the newest, least-backed-up content a
# truncated list would misrepresent as complete.
ls -la reports/track-record/raw/*/ 2>/dev/null
ls -la trade-journal.jsonl reports/track-record/*/SPY.csv 2>/dev/null
```

- The **archive** and the **pin** cannot be re-taken: the broker's window
  rolls off and the benchmark's history is revised.
- The **journal** was never anywhere else at all.
- `summary.md` is the one thing that does NOT matter — it is regenerated from
  the other three on demand.

**Do not offer a command to back these up.** Where they should go depends on
whether the user's remote is private, whether they keep copies elsewhere, and
what they are willing to have leave the machine — none of which this skill
knows. Say what is unprotected and let them decide.
