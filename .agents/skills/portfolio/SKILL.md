---
name: portfolio
description: |
  Portfolio-level assessment and order generation. Use this skill when the
  user wants to review their entire portfolio, get buy/sell/hold recommendations,
  generate IBKR orders, rebalance, or make allocation decisions.
  Trigger phrases: "portfolio", "review my positions", "what should I do",
  "rebalance", "portfolio check", "generate orders", "position sizing",
  or any request about the portfolio as a whole (not individual stock analysis).
  Requires portfolio-state.yaml with holdings/cash.
  NOT for analyzing a single stock (use score-business).
  NOT for building a thesis on one stock (use investment-thesis).
user_invocable: true
---

# Portfolio — Principle-Based Decision + Orders

Assess portfolio holdings and watchlist against market conditions.
Produce actionable decisions and concrete order recommendations
based on the user's investment principles.

Every run ends by writing a durable **decision log** (`decisions.json` +
`decisions.md` in `reports/portfolio/{YYYYMMDD}/`). The log is what
survives between runs — it's the audit trail, the follow-up calendar,
and the reflection anchor. Treat it as the real output of this skill,
not the conversation.

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here.

Run this block first and **CAPTURE the `STOCK_V7_ROOT=...` value it prints**. Substitute
that absolute path for the literal `<captured-abs-ROOT>` in every later Bash block, every
harness Read path (including `portfolio-state.yaml` / `strategy.yaml`), and every
subagent-dispatch path in this skill. If this block exits non-zero (multiple candidate
roots, or no repo found), show its stderr to the user and **STOP** — run nothing else.

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
"$PYBIN" -m scripts.version_skew --expected-min "__BAKED_AT_SYNC__" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync. Run this line VERBATIM — never substitute a version for the placeholder: unsubstituted it exits 0 silently, while a guessed one prints a real-looking skew WARNING built from nothing
```

> **Single-writer note (concurrency probe 2026-08-03):** all same-day
> portfolio runs share `reports/portfolio/<date>/` (macro.json + scratch
> files) with NO session lock — two concurrent /portfolio sessions can
> log one session's decisions against the other's prices. Do not run
> /portfolio in two sessions at once; sequential same-day reruns are
> fine (the earlier log is archived). Also do NOT edit
> portfolio-state.yaml **or strategy.yaml** while a run is in flight — a
> mid-run policy edit is compiled at Step 2 but only re-compared at Step 8, so
> Step 7 would present orders sized under the pre-edit values. The validator now
> binds its state hash and the logger refuses on mismatch.

## Preflight: Money-path config

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.config_gate check --portfolio
```

If it exits non-zero, STOP and show its stderr to the user (config not confirmed / API
key missing / portfolio-state malformed) — do NOT run any analysis or produce numbers.
Then continue below.

## Step 0: Review Prior Run (Follow-ups + Unresolved Decisions)

Before assembling today's context, look at what the last run flagged.
This closes the loop between "what I said I'd watch" and "what I'm
deciding now".

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.portfolio_log review
```

On a well-formed prior log the script locates the latest-dated prior
`decisions.json` (**including an
earlier run today** — a same-day rerun archives the prior pair as
`decisions.{run_id}.json/.md` in the same dir, so nothing is clobbered) and
prints its PATH — **not** its per-ticker decisions or `orders_proposed` —
along with:
- The run's `status`, plus `user_confirmation.status` (pending/accepted/
  modified/declined)
- Follow-up events whose date has arrived (`date <= today`)
- A warning if no reflection is recorded — but ONLY when confirmation is
  `accepted` or `modified`, so silence here does not mean "nothing pending"

**Gate — open the prior `decisions.json` before Step 1.** When `review` exits 0 and prints a prior-run path, you MUST read the file at
the path it printed. Any NON-ZERO exit means STOP: the review gate did not complete (its schema-validation path returns 1 and prints a
`File:` line to stderr), so you MUST NOT use it as this run's prior-run
evidence — repairing it is a separate action. For every prior
decision whose `action` is neither `hold` nor `skip`, you MUST read its FULL
`principle_cited` (the citation is multi-clause; the decisive one is NOT
always the leading `#N`), its
`rationale` (which may record a user ruling), its
`invalidation_trigger` (the condition under which that call expires or
escalates), and ALL linked `orders_proposed` entries. You MUST also read that run's
`user_confirmation.{accepted_orders,rejected_orders,modified_orders,decision_notes}`
and `execution_outcomes.{orders_filled,orders_unfilled}` too — `review` prints
none of these, so an order the user REJECTED or recorded as FILLED is invisible
and you could re-propose it. Today's decision for each such
ticker MUST explicitly state **carry forward / supersede / drop** and why — in
that ticker's Step 5 decision while it is still in holdings or watchlist,
otherwise in the Step 8 blob's `notes`, since a filled exit or a dropped
watchlist name leaves no per-ticker decision to carry the verdict. This
system never places orders and `open_orders` is hand-synced, so broker
disposition is UNKNOWN unless the user states it; you MUST never infer a fill or a non-fill from its absence there.
Sequencing: the READING above MUST finish before Step 1; the verdict comes
later, when those decisions are authored. (2026-08-10: a
prior `reduce` and a user ruling recorded in its rationale were invisible
because the prior decisions file was never opened; the next run
wrote a contradictory `hold`.)

Read those due follow-ups into your reasoning. For each one:
- Did the flagged catalyst actually hit? (e.g., earnings on the
  expected date — check news / recent price action)
- Did the prior `what_to_watch` condition trigger? If so, the action
  rule associated with it (e.g., "miss → #3 reduce trigger fires")
  should be explicitly addressed in today's decisions.
- If the prior run was never confirmed, note that.

If no prior run exists (first time), the script says so and you proceed
normally.

If the command exits non-zero (typically: the prior `decisions.json` fails
schema validation after a schema change), **STOP**, show its stderr to the
user, and follow any remediation it prints (hand-fix the prior log, or
move/rename it so an older compatible run resolves — an unvalidated
passthrough field can instead exit non-zero with a bare traceback and no
remediation at all) — do not proceed with
an unresolved prior log; it is the audit chain today's run builds on.

## Step 1: Read Portfolio State

Read `<captured-abs-ROOT>/portfolio-state.yaml` (the project root).

⚠ **First, if the user has already reported a fill that completed BEFORE this
run** — sync it into the file now, using the SAME protocol Step 9 uses:
**diff → user confirms → keep the prior file as `.bak` → write → re-run the
Preflight → restore the `.bak` if it fails**, then stop. Do not skip the backup:
a malformed write here is not caught by anything later and would strand every
future run, and "STOP" alone leaves it in place. Everything downstream (macro prices, decisions, sizing, cash
checks) is derived from what you read here, so authoring against known-stale
holdings would present actionable recommendations that are wrong. This is the
ONLY point where a state write precedes the decision log; fills that arise from
THIS run's recommendations are deferred to Step 9 instead.

Extract:
- `holdings`: dict of ticker → {shares, cost_basis}
- `cash`: number
- `watchlist`: list of tickers
- `open_orders`: the broker's working GTC orders. **Required** — Preflight
  has already refused an absent key, so it is present here. But Preflight has
  checked only that the key is PRESENT and that each entry carries a ticker, a
  string `type`, and positive shares and price. It does **not** check the type
  vocabulary, and nothing downstream does either: an open order typed
  `nonsense` passes both Preflight and validation **provided its side is
  inferable some other way** — it passes with `action: buy` and is refused
  without it as `unprojectable_open_order`. So what is enforced is a
  recognizable SIDE, never a type vocabulary — and not even an unambiguous
  side: a hand-sync typo like `{action: buy, type: limit_sell}` matches BOTH
  classifiers, is accepted, and is then treated as a BUY wherever a scenario
  selects it — including `all_sell`, which spends cash on it. Preflight also
  cannot tell
  whether the list is COMPLETE or whether it matches the broker. So the sync
  obligation is unchanged: this must be the broker's full working-order set —
  whenever holdings/cash are synced from the broker, the working orders are
  part of the SAME sync. An absent or partial list is exactly how a working
  full-clear GTC sell goes unseen by the decision engine.
- `symbol_aliases`: optional `{KEY: {vendor: SYM, broker: SYM}}` map when
  the broker and the data vendor disagree on a symbol (ADR depositary
  changes); Step 4 feeds the vendor side to the price fetch

## Step 2: Compile Policy

Hard constraints come from `strategy.yaml`'s **`risk:` block**, projected by
code. They are NOT extracted from the prose of `principles` — that extraction
was the F4 fail-open: a value written under `risk:` was never read, the hash
covered only `principles`, so an edit produced a cache hit and the decision log
kept attesting the superseded policy.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.compile_strategy \
  --strategy strategy.yaml \
  --output   strategy.compiled.yaml
echo "COMPILE_EXIT=$?"
```

**There is no cache-hit branch.** The compiler re-derives and atomically
rewrites on every run, so a hand-edited or stale `strategy.compiled.yaml`
cannot survive into a decision. Do not add one back.

Branch on the printed `COMPILE_EXIT`:

| Exit | Meaning | What to do |
|---|---|---|
| `0` | a fresh `strategy.compiled.yaml` was written | continue below |
| any non-zero | **STOP.** Show the compiler's stderr to the user, run nothing else, and do **not** fall back to a `strategy.compiled.yaml` already on disk — it is a previous run's policy. Do not infer the cause from the number; READ the stderr. The common case is an invalid configuration (a malformed `principles:` or `risk:` block — a setup error, not a state to compile around, and `config_gate` validates neither, so this is the only gate on it), but an unwritable output path exits non-zero with a raw traceback and no amount of editing `strategy.yaml` fixes it, and a bad flag exits from `argparse` before any compiling happens |

**On exit 0, READ `<captured-abs-ROOT>/strategy.compiled.yaml` now.** The
compiler writes it SILENTLY — it prints nothing on a normal run — so nothing
else in this skill puts the compiled policy in front of you. Its
`hard_constraints`, `soft_principles` and `principle_notes` are exactly what
Step 5 items 2–4 inject into the decision; without this read the disclosure
below is impossible and decisions are authored with NO principles and NO notes,
while `portfolio_log` still attests them. (An earlier revision lost this read
when the cache-hit branch that used to carry it was deleted.)

**Then RELAY the compiled constraints to the user, every run, in one line** —
e.g. `本轮生效约束: max_single_position=0.35`. If `hard_constraints` is EMPTY,
say explicitly that **no numeric floors will be enforced this run**, so a lost
constraint is visible rather than silent. This disclosure replaces the old
extraction confirmation: there is no longer a guess to confirm, but the
operator still needs to see what is actually armed.

**Nothing else to do.** The compiler emits a COMPLETE policy on every path:
when `strategy.yaml` has no `principles:`, it compiles the canonical defaults
itself and says so on stderr. Do NOT hand-write or patch
`strategy.compiled.yaml` — earlier revisions had orchestration repair the file
here, which left the shipped configuration misreporting its own policy and
silently disabled citation-range validation in the decision log.

### What the compiler does

- `hard_constraints` is a pure projection of `risk:` — the three ARMABLE
  canonical keys from `rules/portfolio-safety.md` (`max_single_position`,
  `min_cash`, `max_holdings`) and nothing else. An unknown key inside `risk:`
  is REFUSED, not dropped: dropping it is F4's exact signature, where the
  constraint the operator intended never binds. The fourth canonical key,
  `max_sector`, is refused for the opposite reason — sector lookup is
  unimplemented, so `validate.py` fails closed and every run would refuse;
  the compiler says so and tells the operator to delete the line.
- Percent-point input is coerced to decimal via
  `scripts.cli_utils.normalize_percent_fraction`, so `35` becomes `0.35` and a
  human number cannot arm a 3500% cap. Applied only to the three fraction keys
  — `max_holdings` is an integer count and passes through untouched
  (`rules/units.md`).
- `principles` → `soft_principles` and `principle_notes` are copied
  **verbatim**. Do not drop the notes: the position-action principle
  (currently #3) and others reference them via "见附注", and Step 5 injects
  them.
- `source_hash` covers `principles` + `principle_notes` + `risk` — every input
  that can change what is enforced or what authoring is told. Absent and
  present-null normalize alike, so the two spellings of "not configured" do not
  force a spurious recompile.
- The write is atomic (`os.replace`), so a refusal or a crash leaves the
  previous policy intact rather than a truncated file every consumer refuses.


## Step 3: Classify each ticker (delta-era staleness)

Classify all portfolio tickers in one batch call to amortize Python
startup across N tickers (avoids ~200ms × N subprocess fork cost):

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
TICKERS="AAPL,MU,NVDA,..."  # comma-separated holdings + watchlist
"$PYBIN" -m scripts.delta.portfolio_classify --tickers "$TICKERS"
# → {"AAPL": "fresh", "MU": "stale_bq", "NVDA": "bq_only", ...}
```

For single-ticker ad-hoc checks, `--ticker TICKER` still prints the
state as a bare string. Batch mode (`--tickers T1,T2,...`) returns
JSON for easy jq parsing.

Returns one of (spec §8.1, 5-state contract):
- `fresh` — last full-tier BQ <14 ET days old AND a completed thesis within the last 7 ET days (both `run_meta.{bq,thesis}.completed == true`; windows per `classify.py`)
- `stale_bq` — BQ ≥14 ET days old, OR its last run lost important data (partial/full tier required)
- `stale_thesis` — thesis >7 ET days old (events reuse ceiling)
- `bq_only` — has BQ, no thesis
- `none` — no reports

## Step 3.5: Batch refresh plan

If any ticker is stale (not `fresh`/`none`/`bq_only`), present the user
with a batch refresh plan BEFORE running any cascades.

**A fund classifies from its own artifact.** `classify` reads
`etf_thesis.json` first, so an ETF returns only `fresh` or `stale_thesis` —
never `stale_bq` or `bq_only`, because a fund has no BQ layer. A fund that
has never been analysed returns `none`, which the rule above excludes from
the plan; that is deliberate (there is nothing to refresh), and Step 4.5's
manifest will carry it as `etf_unavailable` so the decision states the fact.
Tell the user plainly that `/etf-thesis <ticker>` is what creates it.

```
Portfolio refresh plan:

BQ refresh (N):
  - TICKER  reason

Thesis refresh (N):
  - TICKER1, TICKER2, ...           ~30s, ~10k each

ETF thesis refresh (N):
  - TICKER1, TICKER2, ...

No refresh needed (N):
  - TICKER1, TICKER2, ...

Proceed?  [a] all  [s] skip stale  [c] customize
```

**One BQ bucket, and no invented totals.** `classify` returns a single
`stale_bq` for both causes, so splitting full-tier from partial-tier here
means guessing — and the tier is decided inside `/score-business` by the delta
layer regardless. Costs follow `.claude/rules/anti-hallucination.md`: the
`~30s, ~10k` thesis figure is measured and stays; no BQ estimate and no grand
total, because there is nothing to derive them from. Asked what the refresh
will cost, say it depends on how many tickers need a full BQ, which is not
known until each runs.

- `[a]` all: sequentially cascade `/score-business` then `/investment-thesis`
  per ticker, **in alphabetical ticker order**. Sequential, not parallel —
  predictable log output and easier debugging.
  **A fund takes a different cascade** — `/etf-thesis <ticker>` alone.
  `/score-business` has no business to score and `/investment-thesis` has no
  BQ to build on. Read the identity per ticker from
  `reports/<TICKER>/instrument_type.json` (`instrument_type == "etf"`) — the
  registry any earlier detect wrote, a plain file read, no network. Do NOT
  key this on `classify`'s state: a fund returns `fresh` or `stale_thesis`,
  and so does a stock, so the state EXCLUDES a fund from `stale_bq`/`bq_only`
  but never identifies one. Nor on Step 4.5's manifest — that step has not
  run yet.

  A ticker with no registry entry has never been through a prepass; treat it
  as a stock here. Both stock skills settle identity before fetching anything
  and forward a fund, so the run self-corrects — it costs a wrong line in the
  plan you are showing, which is why the registry read is worth doing. Show
  each cascade as its own line so the user can see which one a ticker gets.
- `[s]` skip stale: proceed with whatever artifacts currently exist on
  disk — stale tickers are NOT dropped, just not refreshed. Use
  `scripts.delta.resolver find-latest-prior --include-today` to locate
  each ticker's latest available BQ + thesis, then read them as below.
  Record the stale state + days-since-last-refresh alongside each
  ticker's row in `decisions.md` so the audit shows decisions were
  made on stale data. Include a prominent "⚠ stale data" note in
  the decision summary.
  **Legacy or loader-invalid artifacts must not lose their degradation
  signal here.** A `stale_bq` conflates age with degradation, and a raw
  read must not resurrect what the typed loader rejects — so for each
  skipped ticker, first run the loader itself:
  `python3 -c "from scripts.schemas.bq_analysis import load_bq_analysis;
  load_bq_analysis(r'<resolved-run-dir>/bq_analysis.json')"`. If it exits
  non-zero (any schema rejection: wrong types, unknown status vocabulary,
  asymmetric fields), OR it loads but the raw meta does not carry the
  full post-change pair — a **non-null string** `validation_status` AND a
  **list** `degraded_categories`, both present (every other combination —
  legacy absence, one field missing, `null` status beside an empty list,
  any non-list value — either predates the gate or is a truncated /
  unknowable record; assemble writes a null status exactly when
  completeness was unknowable, and an empty list there means "could not
  enumerate", not "nothing was lost"), run
  `python3 -m scripts.delta.portfolio_classify --degradation-summary
  "<resolved-run-dir>"` and use ITS two fields as the summary fields
  below, labeled "derived from stored validation". A
  `degraded_categories` of `null` means the stored record is unreadable —
  treat it as degraded (entry/add blocked, say so in the rationale).
  **A thesis outlives its source run here too.** When the selected thesis
  dir is OLDER than the selected BQ dir, its ER/CE/entry logic was
  computed from ITS OWN dir's run, not the clean one beside today's BQ —
  so also run `python3 -m scripts.delta.portfolio_classify
  --degradation-summary "<thesis-run-dir>"`; if that `degraded_categories`
  is non-empty or `null`, attach a `thesis_source_degraded:
  <categories|unreadable>` note beside the ticker's thesis fields. And
  when thesis and BQ share ONE dir, the source may have been overwritten
  under it: run `python3 -m scripts.delta.portfolio_classify
  --thesis-orphaned "<run-dir>"`; `{"orphaned": true}` means a same-day
  re-score replaced the run the thesis was built from and its state is
  unrecoverable — attach `thesis_source_degraded: unreadable`. The
  decision prompt treats the note exactly like a non-empty
  `degraded_categories`.
- `[c]` customize: show toggles; then behave as `[a]` for the selected subset.

No timeout — wait for explicit user choice.

For every ticker (fresh, bq_only, AND stale when `[s]` was chosen),
resolve the latest artifacts via the delta resolver and read them as
below (read each artifact at its absolute
`<captured-abs-ROOT>/reports/...` path). **The integrity checks described
under `[s]` apply to EVERY read here — including artifacts freshly
produced by this run's `[a]` cascade**: run the typed loader per
artifact, and if it rejects, or the raw meta lacks the full post-change
pair (a non-null string `validation_status` AND a list
`degraded_categories`), derive the two fields via
`--degradation-summary` exactly as described there. A just-written
artifact earns no exemption — a truncation that happened during THIS
run's cascade is precisely the one no later classification has seen yet. Tickers classified `none` that
weren't cascaded should be flagged in decisions.md as "no analysis
available".

For tickers with `bq_analysis.json`, read the **summary only**:
- `scores` (overall, fundamental, forward, industry)
- `meta.validation_status` (str|null — the fetch's top-level status; disclosure context)
- `meta.degraded_categories` (list — important/critical data the fetch lost; this is the gate. Key MISSING while `validation_status` is non-null = a corrupt record — treat as degraded, do not open/add)
- `meta.data_freshness` + `meta.freshness_note` (disclosure, NOT a gate — the
  latest financial period behind the score and any staleness/anomaly prose.
  200-day-old fundamentals on a quarterly filer deserve a sentence in the
  rationale; they do not block, because filing cadence varies by issuer and
  the delta clocks own artifact staleness)
- `synthesis.watchlist_recommendation`
- `synthesis.conviction`
- `synthesis.thesis`
- `synthesis.key_strengths` (first 3)
- `synthesis.key_risks` (first 3)
- `synthesis.catalyst_calendar`

For tickers with `investment_thesis.json`, read the **full file** (~10KB).

## Step 4: Fetch Macro Data

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
# Vendor aliases (symbol_aliases in portfolio-state.yaml): built by the SAME
# tested helper /monitor uses — no hand-assembled JSON. "{}" is a valid
# no-op, so the flag is passed unconditionally. A malformed symbol_aliases
# raises -> STOP (fetching the wrong symbol is the failure this closes).
VENDOR_ALIASES=$("$PYBIN" -c "
import json, pathlib
from scripts.monitor import load_vendor_aliases
print(json.dumps(load_vendor_aliases(pathlib.Path('portfolio-state.yaml'))))
") || { echo "FATAL: symbol_aliases in portfolio-state.yaml is malformed — fix it before fetching prices" >&2; exit 1; }
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$ETDAY" ] || { echo "FATAL: could not resolve ETDAY — every path below would be built from an EMPTY variable, and on a root session (Cowork) that writes the run into / with exit 0 instead of failing" >&2; exit 1; }
"$PYBIN" -m scripts.macro \
  --tickers {ALL_TICKERS_SPACE_SEPARATED} \
  --vendor-aliases "$VENDOR_ALIASES" \
  --output "reports/portfolio/$ETDAY/macro.json"
```

Where `{ALL_TICKERS}` = all tickers from holdings + watchlist, substituted
by you per block. The run-day directory is the **ET calendar day**, derived
in-shell as `$ETDAY` inside EVERY block that touches `reports/portfolio/`
(never substituted by you, never carried across blocks — a local-timezone
"today" lands the run in the wrong dir and Step 8 hard-rejects a non-ET
dir; this derives it correctly everywhere).

A non-zero exit means alias construction, CLI validation, output writing, or an
unexpected macro error failed: **STOP** and show the stderr to the user.
Individual market / ticker / rate fetch failures do **NOT** exit non-zero — they
are recorded in `macro.json` as `FAILED` / `PARTIAL` with null or explicitly
qualified data and the command exits 0, so **read the artifact before
proceeding**:

- **Any HELD ticker whose price fails `validate._usable_price` ⇒ STOP.** That
  predicate, in full: an `int` or `float`, **not** a `bool`, finite if a float,
  and `0 < price < 1e15`. It is the predicate the ratio math uses, and it
  deliberately differs from the logger's, which accepts an explicit `0` as a
  legitimate delisted-at-zero quote. Do not author decisions against a partial
  book. Downstream refusals exist but land at Step 8 — **after** the user may
  already have acted on Step 7's recommendations — and the validator's own
  missing-price gate only arms when a ratio constraint is set.
- **Any HELD ticker whose `ticker_price_structure[T].anchor_session_covered`
  is not `true` ⇒ STOP.** The price layer and the structure layer fail
  INDEPENDENTLY: a healthy meta quote with no daily bar on the anchor
  session (observed 2026-08-29, all 17 holdings at once) leaves every
  closing-basis fact `unknown` — `closing_high_status`, the MA/cluster
  holds, `high_water_drawdown`, and the volume leg
  (`latest_session_bar_unusable`) — which is exactly the evidence #2/#3/#4
  need. The price STOP above does NOT cover it; those prices were all legal.
  `chart_statuses.ticker_prices[T].anchor_session_covered` MIRRORS this same
  field — one computation, so the two can never disagree — so read it there
  rather than opening each structure block by hand. `scripts.macro` also
  prints one `[WARN] … does not cover anchor session …` line naming every
  affected symbol. Check it for held tickers BEFORE Step 5; same reason as the
  price STOP — Step 8's refusal lands after the user may already have acted on
  Step 7. **Three different outages land here, and the pair
  `anchor_session_covered` + `last_bar_session` tells them apart:**
  `last_bar_session` EARLIER than `anchor_session` = this ticker's own daily
  series lagged; `last_bar_session` EQUAL to it = the ticker's bars are fine
  and the broad-index MARKET CALENDAR is unproven (fewer than three of
  SPY/QQQ/^DJI returned bars, so no session can be confirmed real and every
  ticker's structure goes `unknown` at once); `null` = this ticker has no
  usable daily close on or before the anchor at all — which can happen with a
  PASSED meta quote (bars present, every close null/non-positive), so do not
  read it as "the fetch failed". Relay which one it is — the remedy differs.
  **`anchor_session_covered: true` is a FLOOR, not a sufficiency proof, and
  this is the one place the cheap read is not enough.** A gap-free suffix of a
  single bar — an interior gap right before the anchor, or a listing too fresh
  to have history — answers `true` with `bars_available: 1` and EVERY
  closing-basis fact still `unknown`: no `closing_high_status`, no
  `prior_high_close`, all three `moving_averages` null. So for a ticker you are
  about to act on, read `closing_high_status` too: `unknown` there means the
  entry evidence #2/#3/#4 need does not exist, whatever the coverage flag says.
  Unknown is never neutral — it cannot support an entry, and it cannot be
  reported as one.
- **A `null` close on ANY `regime_inputs.indices.*` leg your principles
  actually read ⇒ STOP** — check all three (`SPY` / `QQQ` / `^DJI`), not just
  the one your framework happens to name today, and `regime_inputs.vix.close`
  too when a principle reads VIX. `principle_notes.framework` binds regime
  reading to the anchored `regime_inputs` block, and the live `market.*`
  values are what that block exists to exclude — so a null leg means the
  principle that reads it has no lawful input at all.
  To be exact about what this does and does not prevent: the decision log's
  own `_classify_regime` already fails CLOSED on a missing leg (it answers
  `mixed` and says "Macro signals incomplete"), so nothing mis-classifies
  silently. What the STOP buys is that the DECIDE agent is not asked to
  reason about a regime its inputs cannot support, and that you find out
  before Step 7 rather than after the user has acted on it.
- A **watchlist-only** ticker whose `anchor_session_covered` is not `true`
  ⇒ continue the run, but it **cannot carry a `buy` / `add` this run** — the
  same rule as the no-price case below, for the same reason: entry evidence
  it does not have. Say so explicitly rather than letting it reach Step 7 as
  a candidate. `portfolio_log` refuses such an entry at Step 8
  (`blocked_by_data_integrity`), but that refusal lands AFTER the user may
  have acted on your recommendation — which is exactly why the held-ticker
  STOP above exists.
- A **watchlist-only** ticker without a price ⇒ continue the run, but say so,
  and treat ITS indicators and price structure as **unknown** (never neutral).
  It cannot carry a `buy` / `add` this run, whatever the order type — no
  quote means no `ticker_price_structure`, and Step 8 refuses an entry
  without one. Some shapes are stopped earlier, at Step 6, but do not rely
  on that: the ones that get through leave the run unloggable.
  (`portfolio-decide.md` states this as an authoring rule.)
  Regime is NOT affected: `regime_inputs` is built only from the market indices
  and VIX, so a failed ticker quote must not downgrade macro evidence.

Read the output JSON. This provides:
- Broad market trend data (SPY, QQQ, ^DJI with MAs)
- VIX + VIX MA20
- Interest rates
- Current prices for the portfolio tickers whose quote fetch SUCCEEDED — a
  failed one is null, and null is not neutral (see the STOP above)
- `ticker_indicators[TICKER]` — run-day technical indicators (RSI, MACD,
  Bollinger, ATR, volume confirmation, RSI divergence), computed fresh this
  run. Authoritative for #2 entry timing and #3/#4 momentum reads (the thesis
  `entry_favorability` is a possibly-stale cross-reference). `null`, or a leg
  reading `insufficient_data`, means that read is unavailable — treat as unknown.
- `ticker_price_structure[TICKER]` — **closing-basis** price structure at the
  last completed ET session (the anchor): `anchor_session` / `anchor_close` /
  `anchor_session_covered` / `session_lag`, `closing_high_status`
  (`breakout` | `at_prior_high` | `below_prior_high` | `unknown`),
  `prior_high_close` / `prior_high_date` / `pct_vs_prior_high_close`,
  `lookback_complete` / `inception_proven`, `high_water_drawdown`,
  `moving_averages` (`ma20`/`ma50`/`ma200`), `breakout_hold`, `ma_hold`,
  `cluster_hold`. `null` for a ticker whose fetch failed; inside a present
  block an unproven fact is `unknown`/`unavailable` — never `false`, never
  `0`, never imputed. **Independent of the 74-bar `ticker_indicators` gate**:
  a short-history listing can carry a populated structure block beside a
  `null` indicator block, and the reverse. This is the ONLY contracted source
  of closing-basis highs, MA/cluster holds and drawdown —
  `01_price_data.json:snapshot.week_52_high` is INTRADAY and out of contract
  (decide.md forbids it).
- `universe_rebound_structure` — ONE shared cohort selloff/recovery event over
  all requested tickers (a failed ticker stays in the denominator with an
  empty series): `status` (`fresh` | `stale` | `ambiguous` |
  `window_truncated` | `unavailable`) + `reason`, `trough_session` /
  `peak_session` / `sessions_since_trough` / `peak_truncated`, `modal_count` /
  `trough_date_counts` / `search_window_sessions` / `max_staleness_sessions`,
  and `members[TICKER]` = `status` + `pct_since_cohort_trough` /
  `pct_vs_cohort_peak`. The block carries **no detection boolean and no
  threshold** — `status` is the whole gate (only `fresh` authorises the
  recovery lens in decide.md), and a member may read `unavailable` inside a
  `fresh` event.
- `chart_statuses.ticker_prices[T].price_as_of` / `stale_meta_quote` /
  `price_conflict_same_ts` / `anchor_session_covered` / `last_bar_session`
  — per-ticker price vintage + integrity, plus whether that ticker's price
  STRUCTURE is anchored (mirrors `ticker_price_structure[T]`) and the newest
  session its own bar series carries. The last two are also emitted for each
  index under `chart_statuses.market[IDX]` and for
  `chart_statuses.volatility["^VIX"]`, where — those having no structure
  block — `anchor_session_covered` is exactly the condition that makes that
  symbol's `regime_inputs` close non-null. `status: PASSED` describes the meta
  QUOTE only and says nothing about bar coverage — see the STOP above. **Relay
  any `stale_meta_quote: true`, any `price_conflict_same_ts: true` (the
  meta quote and the chart bar disagree at the same timestamp — the price
  used may be contested), or a
  `price_as_of` older than `regime_inputs.anchor_session`, to the user
  together with the limit prices it affects** (thin OTC ADRs lag — a limit
  set off a stale quote does not fill).
- `regime_inputs` — clock-anchored regime block (anchor_session + per-index
  close/ma50/ma200/high_52w/off_52w_high_pct + VIX close/ma20, all at the
  last completed ET session). Two INDEPENDENT consumers read this block (never
  the live values), with distinct vocabularies — do not conflate their labels:
  (a) the decision log's deterministic audit tag (`portfolio_log._classify_regime`
  → risk_on/risk_off/mixed; coarse, generic, identical for every user), and
  (b) the strategy regime layer the user's principles may define (e.g. a
  bull/sideways/bear entry-mode switch), which the decide agent classifies
  per those principles' own criteria.

## Step 4.5: Identity prepass and ETF manifest

Unconditional, and it runs BEFORE decisions are authored. The validator now
requires a manifest row for every buy: without one, nothing downstream can
tell an out-of-universe ticker from a fund, and every buy is blocked. That is
the fix for a reproduced defect — state `{AAPL}` plus a proposed `buy SOXX`
used to validate as `passed=True, violations=[]`.

The prepass covers holdings AND watchlist, because a buy is usually for
something not yet held.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$ETDAY" ] || { echo "FATAL: could not resolve ETDAY — every path below would be built from an EMPTY variable, and on a root session (Cowork) that writes the run into / with exit 0 instead of failing" >&2; exit 1; }
mkdir -p "reports/portfolio/$ETDAY"
VENDOR_ALIASES=$("$PYBIN" -c "
import json, pathlib
from scripts.monitor import load_vendor_aliases
print(json.dumps(load_vendor_aliases(pathlib.Path('portfolio-state.yaml'))))
") || { echo "FATAL: symbol_aliases in portfolio-state.yaml is malformed" >&2; exit 1; }
ALL=$("$PYBIN" -c "
import pathlib, yaml
from scripts.cli_utils import normalize_ticker
state = yaml.safe_load(pathlib.Path('portfolio-state.yaml').read_text(encoding='utf-8')) or {}
seen = {}
for src in ((state.get('holdings') or {}), (state.get('watchlist') or [])):
    for raw in src:
        try:
            seen.setdefault(normalize_ticker(raw), None)
        except ValueError:
            pass
print(','.join(seen))
")
"$PYBIN" -m scripts.etf.detect --tickers "$ALL" --aliases "$VENDOR_ALIASES" \
  --output "reports/portfolio/$ETDAY/.etf_identity.json" --root "$PWD" \
  || { echo "FATAL: identity prepass did not write its map" >&2; exit 1; }
"$PYBIN" -m scripts.etf.manifest build \
  --state portfolio-state.yaml \
  --identity "reports/portfolio/$ETDAY/.etf_identity.json" \
  --output "reports/portfolio/$ETDAY/.etf_manifest.json" \
  --reports-root "$PWD/reports" \
  || { echo "FATAL: manifest not written — every buy would be blocked" >&2; exit 1; }
```

If either command exits non-zero, show its stderr to the user and **STOP** —
run nothing else. A run with no manifest cannot authorize any buy, so
continuing would only produce a decision set the validator will reject
wholesale, with a message about the manifest rather than about the trades.

A ticker whose identity DOES resolve but resolves to `unknown` gets an
`etf_unresolved` row, which blocks every buy in the set — including stock
buys. That is not an error to work around: say so plainly, and the remedy is
to fix the identity source, not to bypass the gate.

## Step 5: Make Decisions

**First, seal the authoring context** (closing round-27): record WHAT
state, thesis and compiled-strategy vintages the decisions are about to
be authored against — the log writer refuses/warns when they drift before Step 8
(an S0-authored rationale beside an S1 snapshot previously persisted
undetected; the validation-time hash only covers validate→log).

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$ETDAY" ] || { echo "FATAL: could not resolve ETDAY — every path below would be built from an EMPTY variable, and on a root session (Cowork) that writes the run into / with exit 0 instead of failing" >&2; exit 1; }
"$PYBIN" -c "
import hashlib, json, pathlib
import yaml
from scripts.delta.resolver import find_latest_prior
state_bytes = pathlib.Path('portfolio-state.yaml').read_bytes()
state = yaml.safe_load(state_bytes.decode('utf-8')) or {}
tickers = sorted(set(list((state.get('holdings') or {}).keys())
                     + list(state.get('watchlist') or [])))
thesis_ga = {}
for t in tickers:
    d = find_latest_prior(t, 'investment-thesis', include_today=True)
    if d is not None and (d / 'investment_thesis.json').exists():
        try:
            m = json.loads((d / 'investment_thesis.json').read_text(encoding='utf-8')).get('meta') or {}
            thesis_ga[t] = m.get('generated_at')
        except ValueError:
            pass
out = pathlib.Path('reports/portfolio/$ETDAY')
out.mkdir(parents=True, exist_ok=True)


# Atomic write (round-34): a torn/truncated seal is treated by the log
# writer as malformed → hard REFUSE, so never leave a partial file behind.
import os
tmp = out / '.decision_ctx.json.tmp'
compiled_p = pathlib.Path('strategy.compiled.yaml')
strategy_sha = None
if compiled_p.exists():
    strategy_sha = (yaml.safe_load(compiled_p.read_text(encoding='utf-8')) or {}).get('source_hash')
tmp.write_text(json.dumps({
    'state_file_sha256': hashlib.sha256(state_bytes).hexdigest(),
    'thesis_generated_at': thesis_ga,
    # Which STRATEGY authored these decisions. Without it a mid-run
    # principle edit + legitimate recompile let a superseded decision be
    # logged and attributed to the NEW strategy hash — the rendered log
    # then showed a sell the user's current principles contradict.
    'strategy_source_hash': strategy_sha,
}, indent=2), encoding='utf-8')
os.replace(tmp, out / '.decision_ctx.json')
print('decision ctx sealed for', len(tickers), 'tickers')
"
```

Read `<captured-abs-ROOT>/prompts/portfolio-decide.md`.

Read `<captured-abs-ROOT>/strategy.yaml` for `output_language` (default: `zh-CN`).
Present all human-facing output (decisions, rationale, order recommendations) in this
language. JSON field names and source tags remain in English.

Assemble the full context and reason through the decision framework:

**Context provided to the decision:**
1. Portfolio state — the FULL `portfolio-state.yaml` content (holdings +
   cash + watchlist + open orders + any additional top-level fields, e.g.
   `nav_peak_usd` / `nav_peak_as_of`, which the NAV-drawdown circuit-breaker
   principle reads; enumerating only the four classic sections would starve
   it). **Every decision on a ticker with an in-flight open order must
   explicitly reconcile it (keep / cancel / supersede) in its rationale** —
   the log writer attaches the order snapshot to the decision and warns on
   direction conflicts (e.g. `hold` beside a full-size open sell).
2. Hard constraints — the compiled `hard_constraints`, a projection of
   `strategy.yaml`'s `risk:` block (NOT extracted from principle prose)
3. Soft principles (numbered #1–#N, from compiled `soft_principles` — injected verbatim)
4. Principle notes (from compiled `principle_notes` — injected verbatim):
   `framework` (总纲: 基本面选股 / 技术面择时 — frames HOW to read #1–#N),
   `fundamental_break_definition` (the ONLY mandatory-exit trigger, cited via "见附注" by the position-action principle, currently #3),
   `conflict_priority`, `leverage_policy`. Do NOT omit — these are load-bearing.
   Items 2–4 all come from the `strategy.compiled.yaml` you read in Step 2; if
   you did not read it, go back and read it before authoring anything.
5. Macro snapshot (from Step 4)
6. Per-ticker data (BQ summary + thesis, from Step 3)
7. Current prices + run-day technical indicators (`ticker_prices` and
   `ticker_indicators` from macro) — the latter govern the #2 entry-timing
   gate and #3/#4 momentum reads, not the thesis's stale `entry_favorability`.
   The two are provided INDEPENDENTLY and neither is guaranteed: a price only
   where that ticker's quote fetch succeeded, indicators only where there was
   ALSO enough history (a `PASSED` fetch with a short series yields `null`
   indicators). Carry each absence through as **unknown** — never as neutral.
8. **Earnings-window soft preference** — `orders.earnings_window_days` from
   `strategy.yaml` (default 7; mark it "defaulted" if the field is absent), and
   each ticker's `next_earnings_date` resolved from the per-ticker thesis/BQ
   `catalyst_calendar` / `events.json` (carry the event's `source`; for `as_of`
   use `events.json:meta.generated_at`, or `unknown` if absent — do NOT carry a
   per-item `as_of`, which the catalyst items do not have). The decision treats
   the window as a *named soft deferral* (portfolio-decide.md Phase 2.5): with a
   KNOWN date it may defer/size-down an otherwise-authorized entry; with an
   UNKNOWN date it MUST NOT defer. It is never a technical gate. (`orders.*` is
   read straight from `strategy.yaml`; it is not in `strategy.compiled.yaml`.)

   **`next_earnings_date` resolution rule** (the `catalyst_calendar` is a
   free-form event list; `events.json` dates carry `date_precision` ∈
   {`confirmed`, `estimated`, `approximate`} — note: NO "exact" value exists):
   pick the earnings-typed event (its `event`/`impact` text denotes an
   earnings/results print, NOT a product/legal/macro catalyst) whose date is the
   **nearest on or after** the run date — a same-day after-close print IS
   in-window. Accept `date_precision` `confirmed` OR `estimated` (both are
   day-level, so the window can compare against them). Accepting `estimated` is
   intentional: the window is a SOFT, cautious deferral (size-down / wait, never a
   hard gate), so erring toward caution near a *probable* print matches the user's
   "no chase within the earnings window" preference — rejecting estimated would
   silently disable the deferral for most names and chase into their earnings.
   Resolve to `unknown` ONLY when no earnings-typed event matches or the sole
   match is `approximate` (not day-precise) → Phase 2.5: do NOT defer, judge on
   run-day technicals. Never fuzzy-infer a date from non-earnings event text.

9. **ETF manifest projection** — the `rows` map from
   `reports/portfolio/$ETDAY/.etf_manifest.json` (Step 4.5). It says, per
   ticker, WHAT the instrument is and what is known about it. Read it before
   authoring anything about a ticker whose row is not `stock`:
   - `row_kind: "stock"` → ordinary equity, everything above applies.
   - `row_kind: "etf_thesis"` → a fund with a usable thesis. `decision_context`
     carries its merit, kind, technical timing, environment, entry and
     invalidation conditions, top holdings and coverage. **Use it as given** —
     eligibility, readiness and merit were computed by code and are not yours
     to re-derive or overrule. An ETF has no BQ, no ER, no CE and no earnings;
     rank it as an allocation-class candidate rather than by CE.
   - `row_kind: "etf_refusal"` → the fund cannot be entered. Say why (the row
     carries `entry_reasons` / `analysis_reasons`). If `decision_context` is
     present the ticker is HELD and those are its exit conditions still in
     force — surface them beside current evidence.
   - `row_kind: "etf_unavailable"` / `"etf_unresolved"` → no buy is possible.
     State the fact and its `reason`; do not reason about the fund's merits
     from its name.

   **A matched invalidation condition is not a mandate.** Apply
   `principle_notes.fundamental_break_definition`: only a comprehensively
   judged fundamental break mandates a full exit, and a single matched
   condition is evidence the argument needs re-examining. Absent such a
   judgement, a held ETF that may not be entered is limited to hold, reduce or
   exit, and a watchlist-only one to skip.

Produce per-ticker decisions with specific order recommendations.

If any block in this step exits non-zero, **STOP** and surface the error. A failed path resolution in particular must not be worked around: the paths below would be built from an empty variable, and a run written outside its dated directory is one the delta layer can never find again.

## Step 5.5: Write the ETF decision seal

After the decision is authored, before any order is validated. This is a
DIFFERENT artifact from the Step 5 authoring seal (`.decision_ctx.json`),
which binds state/thesis/strategy vintages for the LOG — do not merge them or
reuse that filename; `portfolio_log` refuses the run when it finds something
it cannot read there. This seal binds the manifest and the artifact bytes
behind each ETF row, and it is re-verified
at Step 6 and again at Step 8 — so an artifact replaced between authoring and
logging is caught rather than silently logged against.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$ETDAY" ] || { echo "FATAL: could not resolve ETDAY — every path below would be built from an EMPTY variable, and on a root session (Cowork) that writes the run into / with exit 0 instead of failing" >&2; exit 1; }
"$PYBIN" -m scripts.etf.seal write \
  --manifest "reports/portfolio/$ETDAY/.etf_manifest.json" \
  --output "reports/portfolio/$ETDAY/.etf_decision_ctx.json" \
  || { echo "FATAL: decision seal not written — every ETF buy would be blocked" >&2; exit 1; }
```

If it exits non-zero, show its stderr to the user and **STOP** — run nothing
else. Without the seal no ETF buy can be validated, and presenting orders the
validator will reject wastes the user's attention on the wrong problem.

## Step 6: Validate Orders

Structure the proposed orders as a JSON array and write them to the
run-scoped path `reports/portfolio/<ETDAY>/.proposed_orders.json` — NOT a
Python `tempfile` path. Two reasons (both bit before): a `mkstemp` path on
native-Windows git-bash is a backslash string that bash mangles when
substituted into a later block (repo cross-platform rule: bash-consumed
paths never come from Python tempfile), and a system-temp name is not
reconstructable by the later fresh-shell blocks that re-derive their own
paths.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$ETDAY" ] || { echo "FATAL: could not resolve ETDAY — every path below would be built from an EMPTY variable, and on a root session (Cowork) that writes the run into / with exit 0 instead of failing" >&2; exit 1; }
ORDERS_PATH="reports/portfolio/$ETDAY/.proposed_orders.json"
# Same-day-rerun safety: clear the prior order set FIRST. If parsing the
# heredoc fails below, the validate block must fail loudly on a MISSING
# file — never silently validate an earlier run's stale order set.
"$PYBIN" -m scripts.clear_stale "$ORDERS_PATH" || exit 1
"$PYBIN" -c "
import json, sys, pathlib
sys.stdin.reconfigure(encoding='utf-8')
orders = json.loads(sys.stdin.read())
p = pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(orders), encoding='utf-8')
print(p.as_posix())
" "$ORDERS_PATH" <<'ORDERS' || { echo "FATAL: proposed-orders JSON invalid — nothing written; fix the order array and re-run this block" >&2; exit 1; }
[{"ticker":"MU","action":"buy","type":"market","shares":50,"est_price":90.0}]
ORDERS
```

If the write block prints FATAL (invalid order JSON), STOP — fix the
order array and re-run this block before going any further; the file was
deliberately cleared first, so skipping ahead would make validation fail
on a missing file rather than silently validate a stale order set.

Then validate, capturing the stress-test JSON so Step 8 can attach it to
the decision log. Both paths are deterministic and run-scoped (NOT
/tmp/...$$): Step 8 runs in a LATER shell — the conversational Step 7 sits
between validate and the log write, and a re-validation in Step 7 must
overwrite the same paths so Step 8 reads the latest. A `$$`/PID temp name
is lost across that boundary, and `portfolio_log --stress-test` FAILS
(exit 2) on a missing path — fix the path, do not drop the flag to
silence it. The fixed paths are reconstructable per-call, exactly like
`macro.json`.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$ETDAY" ] || { echo "FATAL: could not resolve ETDAY — every path below would be built from an EMPTY variable, and on a root session (Cowork) that writes the run into / with exit 0 instead of failing" >&2; exit 1; }
VALIDATOR_OUTPUT="reports/portfolio/$ETDAY/.validator_output.json"
# Clear any stale artifact FIRST. Step 8 deliberately KEEPS this file on a
# refusal, and `scripts.validate` exits 1 WITHOUT writing when its inputs are
# unreadable — so without this, a run whose validate dies early would send you
# to read the PREVIOUS run's verdict, possibly a stale PASS. `clear_stale`,
# not `rm -f`: a delete-restricted mount (Cowork FUSE) refuses `unlink` on an
# EXISTING file while still allowing writes, so `rm -f` fails on every SECOND
# run of a day — silently leaving the stale verdict if unguarded, and making
# the run unrunnable if guarded. `clear_stale` deletes, else empties the file,
# else REFUSES — and only then is this FATAL. An emptied file is still
# fail-closed downstream, but LOUDLY, not silently: `portfolio_log
# --stress-test` finds the path present and dies parsing it (exit 2), rather
# than reporting it absent. Either way no stale PASS can be attached.
"$PYBIN" -m scripts.clear_stale "$VALIDATOR_OUTPUT" || { echo "FATAL: cannot clear stale validator output: $VALIDATOR_OUTPUT" >&2; exit 1; }

"$PYBIN" -m scripts.validate \
  --state portfolio-state.yaml \
  --prices "reports/portfolio/$ETDAY/macro.json" \
  --orders "reports/portfolio/$ETDAY/.proposed_orders.json" \
  --constraints strategy.compiled.yaml \
  --manifest "reports/portfolio/$ETDAY/.etf_manifest.json" \
  --decision-seal "reports/portfolio/$ETDAY/.etf_decision_ctx.json" \
  --output "$VALIDATOR_OUTPUT"
```

Without `--output`, scripts.validate writes to stdout and the JSON is
lost before Step 8 needs it (codex review 2026-05-22 F7).

**Do NOT read the exit code as a two-way signal.** The artifact can be written
*before* an error path, so a non-zero exit does not prove the validator had no
opinion — and an early input failure exits non-zero having written nothing at
all. Judge the PAIR (exit code + the artifact this run just wrote):

> **Exactly one combination is an accepted validation: exit 0, the artifact this
> run just wrote, readable, with `passed` exactly `true`.** Then continue.
> The one recognised iteration case is a non-zero exit whose freshly written
> artifact reads `passed: false` — the order set genuinely did not clear; use
> the order-adjustment loop below. **Every other combination — including exit 0 with
> a missing, unreadable, malformed or `passed: false` artifact — is a tool
> failure: STOP and show stderr. Do not read violations from it.**

**On an accepted validation:** include stress test results in the output,
AND relay every entry of the artifact's top-level `warnings` array verbatim.
Warnings live OUTSIDE `stress_test`, so a PASS can carry material ones — a
policy floor breached by the user's own resting broker orders surfaces as a
warning, never a violation. Until Step 8 writes the log, this presentation is
the only place the user can see them.

**On the iteration case** (`passed: false` in this run's freshly written
artifact — an expected signal here, NOT a stop-everything error):
- **Read the whole artifact, never one field.** Enumerate the top-level
  `violations` array AND every `stress_test` scenario whose `passed` is
  false; a scenario that names its own `violations` counts those too. Any
  combination occurs, so no single field is the failure: a failed scenario
  is never copied into the top-level array (an empty array beside
  `passed: false` is normal); a scenario failing purely on cash carries no
  `violations` key at all; one failing on a constraint at its crashed
  valuation carries a nested one while its `cash_after` stays positive; and
  `cash_after` is rounded to two decimals, so a real deficit can read
  `-0.0`. Collect every failing element first, then decide the fix.
- Adjust the order set to resolve them.
- Re-run the ORDER-WRITE block above with the revised array FIRST, then
  re-run validation — the validator reads `.proposed_orders.json`, not
  the conversation; skipping the rewrite re-validates the old order set
  and Step 8 would attach its stress results to your revised
  recommendations. Same rule for any Step 7 revision.
- Max 3 attempts. If still failing, present the unresolved violations
  to the user and ask how to proceed. Note: Step 8 will REFUSE to write
  the log while the validator artifact records `passed: false` — drop or
  fix the failing orders before logging.

## Step 7: Present and Iterate

Present the portfolio assessment, decisions, and orders to the user
in the conversation. Follow the output format from `portfolio-decide.md`.

The user may:
- Ask "why" about a specific decision → explain the reasoning
- Request adjustments ("change MU price to 84") → update and re-validate
- Confirm ("looks good") → the orders are RECOMMENDATIONS only; the user executes
  them manually at their broker. You NEVER submit/place orders and NEVER describe
  them as submitted/placed/executed (advisory-only — `rules/portfolio-safety.md`).
  If any buy in the set is funded by a proposed market SELL in the same set, state
  the sequencing requirement **in this turn** — submit the sell first, wait for a
  confirmed fill. The user may execute right here; the decision log that renders
  the `execution_note` is not written until Step 8, so it cannot carry this.
- Report fills + ask you to update holdings → follow the **holdings-update protocol** below
- Ask to analyze missing tickers → run the appropriate skill

**Holdings-update protocol (the ONLY mutation of `portfolio-state.yaml`).** Only when the
user reports actual fills and asks to update positions — never on your own initiative.
**Sole exception — the `nav_peak_usd` ratchet (bookkeeping, not position data):** when the
user's principles define a NAV-peak-anchored rule (e.g. a drawdown circuit breaker) and
run-day NAV (holdings × run-day macro prices + cash) exceeds the stored `nav_peak_usd`,
`nav_peak_usd` + `nav_peak_as_of` are updated in the same run WITHOUT waiting for a
fill report — monotonic increase only, touch no other field, and SHOW the computation
(prices used + arithmetic) in the run output. Like every other writer here the write
itself lands in **Step 9**, after the log; "same run" means this run, not this step.
Without this exception a no-fill run that makes a new NAV
high leaves the stored peak stale and understates every later drawdown — silently
suppressing the circuit breaker. The same exception covers the breaker's
staleness-reconfirmation write: when the principle's fail-closed branch fires
(`nav_peak_usd` OR `nav_peak_as_of` missing, or `nav_peak_as_of` past its
staleness bound) and the user
reconfirms the peak in-run, `nav_peak_as_of` moves to the run date (and
`nav_peak_usd` only UPWARD, if the user supplies a higher corrected value) — a
user-confirmed bookkeeping write, not a fill; without it the stale-peak branch
deadlocks (reconfirmation required but no authorized writer). It too is agreed
here and written in Step 9. Holdings / cash /
open_orders remain strictly below.

⚠ **What decides a fill's handling is WHOSE it is, not when it landed.**
- **Caused by the recommendations you are presenting right now** → defer to
  Step 9, together with the ratchet and any reconfirmation. The log then records
  the pre-fill recommendation the user actually acted on, which is correct.
- **Any other fill** — one that had already completed when the run started, or a
  pre-existing working GTC order that filled mid-run (a normal event, not the
  conceded concurrency gap) — → **sync it as a Step-1 pre-analysis update and
  start the run over.** Its decisions were authored against holdings that are now
  wrong, and deferring it would durably log a stale recommendation.

⚠ **Otherwise NOTHING in this step WRITES the file. Every sanctioned write is
DEFERRED to Step 9, after Step 8 has logged.** Step 5 seals a `state_file_sha256` over
`portfolio-state.yaml` and Step 6 binds the same hash; `portfolio_log write`
re-hashes the file and **REFUSES (exit 2)** against BOTH bindings if it moved in
between. So writing here silently costs the run its decision log — and that is
true of all three writers alike (the ratchet, the reconfirmation write, and the
fill-driven holdings update). **Agree the change now; apply it after the log.**

Deferring costs nothing *within* a run: at a new high the breaker verdict is the
same against the old peak and the new one, a reconfirmation whose result would
change the advice is a new run, and a reported fill MUST be logged after the
recommendation it followed, never before (`portfolio_log` builds
`portfolio_before` from the state it is handed, so writing the fill first would
file a post-fill recommendation in place of the pre-fill one the user acted on).

**Residual, named and accepted.** The pending change lives only in this
conversation, so a session that ends before Step 9 — or a log that cannot be
written at all — loses it. A **fill** is recovered only if the user re-reports
it: nothing detects an unreported one, because the log holds a PROPOSED order
and this system never infers a fill from that. A **ratchet** is simply not
written, so if NAV falls before the next run that high is gone and the peak
stays low. Say so when a write is left pending, so the operator knows it is on
them. No recovery machinery is built for this: the trigger is a session dying
inside one step, which has not happened, and the fix for the demonstrated bug
does not depend on it.

In this step you therefore only AGREE the change:
1. Show a **before/after diff** of the exact fields changing (e.g. `MU shares: 50 → 100`,
   `cash: 12000 → 3000`, **including `open_orders` — the broker's working GTC
   orders are part of the position sync, not an optional extra**) and have the
   user confirm **that diff** — not a vague "looks good". The `nav_peak_usd`
   ratchet is the exception to *confirmation*, not to *disclosure*: SHOW its
   computation (prices used + arithmetic) in the run output.
2. Carry it forward as a **PENDING write**. A run can produce more than one at
   once (a new high AND a reported fill): combine them into **ONE diff covering
   every applicable change**, never one instead of the other. Do not touch the
   file. Step 9 applies it in a single write.

This is a conversation, not a pipeline. Stay responsive to the user's
questions and adjustments.

## Step 8: Write Decision Log

Once the orders are stable (whether or not the user has said "accepted"),
persist the run. This is non-optional — the decision log is what makes
audit and reflection possible across future runs.

First, produce a **decisions blob** — a JSON file containing only the
LLM-authored judgment fields. The script will fill in the deterministic
parts (portfolio snapshot, macro, thesis metadata, stress test, etc.).
See `prompts/portfolio-decide.md` §"Decision Log Output" for the blob
schema. Write it to a run-scoped dotfile in the portfolio run dir
(`<captured-abs-ROOT>/reports/portfolio/<ETDAY>/.decisions_blob.json`, where
`<ETDAY>` is the ET day the earlier blocks derived — reuse the exact dir the
Step 4 macro output landed in; the write step hard-rejects a non-ET dir) —
portable (native Windows has no `/tmp`) and stable across step boundaries.

**Use the Write tool** to create this `.json` file — do NOT write it with a
Bash heredoc. You are the orchestrator (the main loop), not a subagent, so the
Write tool works for you on a `.json` path; the `cat <<'EOF'` heredoc rule in
`.claude/rules/skill-architecture.md` #8 exists ONLY for *subagents* (whose
Write tool is blocked for `.md`). Give the Write tool the ABSOLUTE
`<captured-abs-ROOT>/...` path (the Write tool does not follow the bash `cd`).
The blob carries CJK `notes`, apostrophes, and nested JSON — a heredoc
quotes/escapes those fragilely (and a stray delimiter line truncates it
silently); the Write tool sidesteps all of it. Content shape:

```json
{
  "decisions": [ "... one entry per ticker in holdings + watchlist ..." ],
  "orders_proposed": [ "... sequence-numbered orders ..." ],
  "follow_ups": [ "... future catalysts to watch ..." ],
  "candidate_scan": { },
  "principle_audit_interpretation": "Explain why any principle was not cited",
  "notes": [ "Any structural observations" ]
}
```
(`candidate_scan` is REQUIRED when `orders_proposed` is empty — Phase 3
zero-order discipline.)

Then call the logger. If the write command fails (the `|| { …; exit 1; }`
guard fires), STOP — the refusal reason is on stderr, the validator
output is preserved for the re-run, and NO decision log exists yet:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
ETDAY=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$ETDAY" ] || { echo "FATAL: could not resolve ETDAY — every path below would be built from an EMPTY variable, and on a root session (Cowork) that writes the run into / with exit 0 instead of failing" >&2; exit 1; }
# Round-20 F4: the exit code MUST be checked — a cmd_write refusal
# (blob shape violation, missing rationale/principle) writes NO
# decisions.json, and the unconditional rm below would then delete the
# only constraint/stress evidence while the block exits 0.
"$PYBIN" -m scripts.portfolio_log write \
  --decisions-blob "reports/portfolio/$ETDAY/.decisions_blob.json" \
  --state portfolio-state.yaml \
  --macro "reports/portfolio/$ETDAY/macro.json" \
  --constraints strategy.compiled.yaml \
  --stress-test "reports/portfolio/$ETDAY/.validator_output.json" \
  --output-dir "reports/portfolio/$ETDAY" \
  || { echo "REFUSED: portfolio_log write — decisions.json was NOT written. The validator output is KEPT at reports/portfolio/$ETDAY/.validator_output.json. See 'If the log write refuses' below — fix and re-run this step; do NOT write state to get past it." >&2; exit 1; }

# Clean up the validator output (its content is now in decisions.json).
# $ETDAY is re-derived in THIS block (Step 6's shell variables do not
# survive into this later call). BEST-EFFORT (`|| true`): the log is already
# written by this point, and on a delete-restricted mount an unguarded `rm`
# returns 1 and makes a SUCCEEDED Step 8 read as a failed one (feedback
# 2026-08-29). A leftover here is harmless — Step 6 clears or neutralizes it
# before the next validate.
rm -f "reports/portfolio/$ETDAY/.validator_output.json" || true
```

The script REFUSES (exit 2) when proposed orders or open broker orders
exist but `--stress-test` is absent/missing, and when the artifact records
`passed: false` — run Step 6 first; only an all-hold run with no open
orders may omit the flag.

The script writes `decisions.json` (canonical machine-readable) +
`decisions.md` (hybrid table/narrative for humans). It fills in for
you: `portfolio_before`, `macro` with regime classification,
`constraints_active`, `current_weight_pct` + `thesis_snapshot` +
`report_refs` per decision, `est_cost`/`est_proceeds` per order,
`principle_audit.cited_this_run` + `not_cited_this_run`,
`user_confirmation` placeholders, and `execution_outcomes` placeholders.

Tell the user where the log landed and mention that
`user_confirmation.status` is initialized to `pending`, and the confirmation
and execution-detail fields are left empty/null, for **the user** to update
after they act. Do not offer to update those yourself — they reflect real
execution, not your proposals.

### If the log succeeded but WARNED that the seal was absent

Check this **before** anything else, and regardless of whether a write is
pending — Step 9 is skipped on an all-hold run, so this cannot live there.

If Step 8 printed `no .decision_ctx.json seal ... (legacy flow)` it still exits
0, but it verified only validation→logging, never authoring→validation. Step 5
always writes a seal in this flow, so its absence means something went wrong:
**write nothing, and restart.** ⚠ **The log it just wrote is already on disk and
is what Step 0 will read** — tell the user it is UNVERIFIED for the authoring
window and that the restart's log supersedes it (a same-day rerun archives the
prior pair). Until that rerun completes, the unverified log stands.

### If the log write refuses

**Fix what the message names and re-run this step.** Every refusal states its
own remedy — a malformed blob wants the blob fixed, and a state-hash mismatch (`portfolio-state.yaml changed
...`) means the file moved mid-run, which this skill forbids: that run cannot be
logged at all and restarts from the beginning.

⚠ **A MALFORMED SEAL is a restart too, not a repair — and this OVERRIDES the
remedy that message prints.** `.decision_ctx.json`
being unreadable is detected BEFORE the state hash can be compared, so it
destroys the only evidence of whether the file moved. Rebuilding the seal — or
re-authoring to produce a new one — would stamp the CURRENT state as though it
had been the authoring state, and the logger's own offer to "delete the file and
proceed unsealed" throws the check away entirely. Do neither: **discard the run
and restart from the beginning**, exactly as for a mismatch.

⚠ **If the remedy requires touching `portfolio-state.yaml`, this run is over.**
Some refusals do ask for that — an absent `open_orders` key tells you to write
`open_orders: []` or sync the broker's working orders. Doing it here would move
the file the run is bound to, so: **make that edit as a Step-1 sync and restart
from the beginning**, exactly as a state-hash mismatch does. The current run's
pending change is NOT applied; it is re-established in the fresh run.

⚠ **And never write state merely to get past a refusal.** You cannot tell from
the error whether the file still matches what this run bound — most checks run
BEFORE either hash is compared, so a blob or seal error says nothing about it.
If a refusal cannot be repaired at all, the run ends with **no log and no state
write**: say so plainly and start fresh. The user re-reports the fill there.
The ratchet is recomputed from the fresh run's own NAV — which recovers the high
only if NAV has not fallen meanwhile; if it has, that high is gone (see the
residual note in Step 7).

## Step 9: Deferred state writes

**Skip this step unless Step 7 produced a PENDING write.** This is where the
holdings-update protocol's execution half lives — deliberately after Step 8, so
`portfolio-state.yaml` is byte-identical from the Step-5 seal through the
Step-8 log and neither hash binding can refuse. Do not move it earlier, and do
not write from Step 7.

⚠ **Reached only after Step 8 logged successfully.** A refused log never leads
here — see its section — which is why no re-basing rule is needed: the successful
log proves `portfolio-state.yaml` is still byte-identical to what Step 7 agreed
against.

Apply exactly what Step 7 carried forward, as **one write** — the change the
user confirmed AND the automatic ratchet it disclosed, if both apply (the
ratchet needs no agreement; #9 puts refresh on the system, and pausing for it
would leave `nav_peak_usd` stale-low). No re-derivation, no additions:

1. **Keep the prior file** (e.g. copy to `portfolio-state.yaml.bak`) so a wrong
   edit is reversible.
2. **Write the combined change**, and nothing else. For the `nav_peak_usd`
   ratchet: monotonic increase only, touch no other field.
3. **Re-run the Preflight block** (`"$PYBIN" -m scripts.config_gate check
   --portfolio`, with its `cd`/`PYBIN` prelude) — if it fails, **restore the
   `.bak` from step 1**, then STOP and show stderr. Restoring is the point of
   keeping it: "STOP" alone leaves the bad file in place, and because this step
   runs AFTER the log, a malformed `portfolio-state.yaml` would poison the next
   run's preflight rather than this one's.
   ⚠ **After restoring, the change is UNRESOLVED, not cancelled** — but say which
   part, because the pending write may hold more than one thing and they recover
   differently:
   - **a fill** — the broker moved even though the file did not. Treat it next
     run the way a pre-run fill is treated: **sync it before any analysis.**
   - **the ratchet** — nothing at the broker moved, and nothing recovers it: the
     next run simply recomputes from its own NAV, so if NAV has fallen the high
     is gone (the accepted residual above).
   - **a peak reconfirmation** — nothing at the broker moved either; the next
     run meets the same fail-closed branch and asks again.

`config_gate` validates STRUCTURE, not correctness (a mistyped `1000`-for-`100`
is structurally valid) — the user agreeing the diff in Step 7 is the control
that catches wrong numbers (for the automatic ratchet, the control is the
computation you disclosed there).
