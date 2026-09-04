---
name: etf-thesis
description: |
  Write an investment thesis for a US-listed, non-leveraged EQUITY ETF.
  Use this skill when the user names an ETF and wants to know whether to buy
  it, hold it, or leave it — the ETF counterpart of /investment-thesis.
  Trigger phrases: "thesis for SOXX", "should I buy VOO", "analyze this ETF",
  "看看这个 ETF", "ETF 值得买吗", "是不是该加仓 QQQ", "evaluate SPY as a
  position", or any request to judge a fund rather than a company.
  Runs the identity detector FIRST: a ticker that resolves to an ordinary
  stock is forwarded to /investment-thesis, and one whose identity cannot be
  resolved is refused rather than guessed at.
  Entry requires the owner to have approved the fund in `strategy.yaml`
  (`etf_policy.approved_equity_etfs`). A fund that is not approved still gets
  an artifact — it just cannot be entered.
  NOT for leveraged, inverse, bond, commodity, or currency ETFs: those are
  refused by the eligibility screens, with the reason stated.
  NOT for individual stocks (use investment-thesis).
  NOT for position sizing or order generation (use portfolio).
user_invocable: true
---

# ETF Thesis — non-leveraged equity ETFs as buyable instruments

This skill answers: **is this fund a position worth taking, and what would
change that?**

It does NOT answer "how much" or "what order type" — those belong to
/portfolio. And it never decides eligibility or readiness by judgement: both
are computed by code before the model is invoked, which is what stops an entry
argued from evidence nobody has.

Three outcomes, and only one of them involves writing an argument:

| Outcome | When | Artifact |
|---|---|---|
| forwarded | the ticker is an ordinary stock | none — /investment-thesis takes over |
| refusal | ineligible, or eligible but this run's evidence is not enough | `etf_thesis.json` saying which, and why |
| thesis | eligible and ready | `etf_thesis.json` + `etf_summary.md` |

A refusal is a real deliverable. Show it to the user with its reason — do not
retry, do not soften it, and do not fill the gap from memory.

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here. `<TICKER>` below is likewise substituted by you into EACH block (never
carried as a shell variable across blocks); each block re-runs the idempotent
`allocate-bq-run` (current run dir) / `find-latest-prior` (prior dirs) to re-derive
its dirs; and the COMPUTED cross-step state — the Step-2 canonical events anchor
(which CANNOT be re-derived later: Step 4's reuse path mutates the events doc) plus
the Step-3 events-reuse decision fields — lives in the run-scoped state file
`$REPORT_DIR/.run_state.json`, written by Steps 2/3 and re-read by every later block.

Run this block first and **CAPTURE the `STOCK_V7_ROOT=...` value it prints**. Substitute
that absolute path for the literal `<captured-abs-ROOT>` in every later Bash block, every
harness Read path, and every subagent-dispatch path in this skill. If this block exits
non-zero (multiple candidate roots, or no repo found), show its stderr to the user and
**STOP** — run nothing else.

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
"$PYBIN" -m scripts.version_skew --expected-min "1.21.1" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync. Run this line VERBATIM — never substitute a version for the placeholder: unsubstituted it exits 0 silently, a guessed one prints a real-looking skew WARNING built from nothing, and the clone's OWN VERSION is the worst of the three — it compares equal by construction, so it exits 0 with no output and reads exactly like a clean check (feedback 2026-09-01)
```

> **Single-writer note (concurrency probe 2026-08-03):** run dirs are
> shared per ticker+day with NO session lock — two concurrent sessions
> running this skill for the SAME ticker will interleave score/state
> writes and can persist an artifact assembled from both sessions'
> halves. Do not run this skill for the same ticker in two sessions at
> once; a same-day RERUN after the earlier run finished is fine.

## Preflight: Money-path config

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.config_gate check
```

If it exits non-zero, STOP and show its stderr to the user (config not confirmed /
required API key missing) — do NOT run any analysis or produce numbers. Then continue
below.

## Prerequisites

None from other skills. This skill fetches everything it needs. It DOES need
`strategy.yaml` to carry an `etf_policy` block before any fund can be entered;
without one, every fund resolves to `not_owner_approved` and the run still
produces a usable refusal artifact.

## Step 1 — Identity (runs before anything else)

`WF.FORWARDING`: the detector runs before any fetch, any allocation, any
prerequisite. A misrouted ETF that reaches the stock path produces a
plausible-looking business-quality score for a fund with no business.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.etf.detect --ticker "<TICKER>" --root "$PWD"
```

Read the printed JSON:

- `instrument_type: "etf"` → continue to Step 2.
- `instrument_type: "equity"` → tell the user this is an ordinary stock and
  run **/investment-thesis** instead. Stop here.
- `instrument_type: "unknown"` → the two identity sources disagreed or one was
  unreachable. **Stop.** Show `source_verdicts` to the user. Do not fall
  through to the stock path, and do not guess from the ticker's name — an
  unresolved identity never authorizes anything.

## Step 2 — Run-day market snapshot

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
RUN_DATE=$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y%m%d'))")
[ -n "$RUN_DATE" ] || { echo "FATAL: could not resolve RUN_DATE — every path below would be built from an EMPTY variable, collapsing the dated run directory the delta resolver keys on (a silently relocated artifact, .claude/rules/skill-architecture.md #9)" >&2; exit 1; }
REPORT_DIR="$PWD/reports/<TICKER>/$RUN_DATE"
mkdir -p "$REPORT_DIR/data"
"$PYBIN" -m scripts.macro --tickers "<TICKER>" --output "$REPORT_DIR/data/etf_market_snapshot.json"
printf 'REPORT_DIR=%s\n' "$REPORT_DIR"
```

**CAPTURE the printed `REPORT_DIR`** and substitute it into every later block.
Shell variables do not survive across blocks.

If any block in this step exits non-zero, **STOP** and surface the error. A failed path resolution in particular must not be worked around: the paths below would be built from an empty variable, and a run written outside its dated directory is one the delta layer can never find again.

## Step 3 — Profile and eligibility

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.compile_strategy --strategy strategy.yaml --output strategy.compiled.yaml
# Clear any same-day model artifact HERE, before the eligibility branch below —
# that branch skips Step 4 entirely, so clearing at the top of Step 4 would
# leave a stale `etf_model.json` for Step 5 to pass to the stamp and for Step
# 5.5 to record as an agent that never ran this run. `clear_stale` empties the
# file when a delete-restricted mount refuses the unlink, which is why every
# later test of it is `-s` (non-empty) and never `-f` (exists).
"$PYBIN" -m scripts.clear_stale "<captured-REPORT_DIR>/data/etf_model.json"
"$PYBIN" -m scripts.etf.profile --ticker "<TICKER>" \
  --identity-registry "$PWD/reports/<TICKER>/instrument_type.json" \
  --market-snapshot "<captured-REPORT_DIR>/data/etf_market_snapshot.json" \
  --compiled-strategy "$PWD/strategy.compiled.yaml" \
  --output "<captured-REPORT_DIR>/data/etf_profile.json"
```

Read `entry_eligibility` from the written profile:

- `pass` → continue to Step 4.
- `block` or `unknown` → **skip Step 4 entirely.** Go straight to Step 5; the
  stamp writes the refusal. Do not invoke the model: there is nothing for it
  to argue about, and an argument beside a refusal reads as a recommendation.

The compile step is re-run every time on purpose. `etf_policy` is inside the
policy hash, so an approval the owner edited this morning must take effect
this run — a cached compile would log a decision against a policy already
replaced.

## Step 4 — Write the thesis (only when eligibility is `pass`)

First check readiness, and skip the model if it is not `ready`:

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -c "
import json, sys
from scripts.delta.calendar import today_et
from scripts.etf.readiness import analysis_readiness
with open(r'<captured-REPORT_DIR>/data/etf_market_snapshot.json', encoding='utf-8') as fh:
    snap = json.load(fh)
r = analysis_readiness(snap, ticker='<TICKER>', authoring_date=today_et())
print(json.dumps({'readiness': r.readiness, 'reasons': list(r.reasons)}))
"
```

If `readiness` is not `ready`, skip to Step 5.

Otherwise Read `prompts/evaluate-etf.md` and follow it, with these two files
as your only inputs:

- `<captured-REPORT_DIR>/data/etf_profile.json`
- `<captured-REPORT_DIR>/data/etf_market_snapshot.json`

Write the model's JSON object to `<captured-REPORT_DIR>/data/etf_model.json`.
Every number in it is re-read from those files and compared in Step 5, so an
unbound number fails the run rather than reaching the user.

## Step 5 — Stamp

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
# `find_latest_prior` returns the run DIRECTORY. `--prior-thesis` wants the
# FILE inside it — passing the directory makes the stamp exit 1 with
# "Is a directory", which would kill every second-day run on a ticker that
# already has a thesis, i.e. exactly when a held position's exit conditions
# matter most. A run must not be its own prior — but "its own" is THIS
# run's directory, which Step 2 allocated from `today_et`. The resolver's
# default exclusion instead assumes `session_et`, and on a NON-TRADING day
# those differ: it would drop the real prior (Friday) and keep the run's
# own dir. So the run dir is passed explicitly. Its name is re-derived from
# the captured path rather than from the clock, so a run spanning ET
# midnight still excludes the directory it is actually writing.
# The lookup is NOT allowed to fail quietly: a swallowed error reads as
# `none`, and `none` on a held fund silently reports a re-analysis as the
# fund's first ever (feedback 2026-08-30 monitor ①).
PRIOR=$("$PYBIN" -c "
from pathlib import Path
from scripts.delta.resolver import find_latest_prior
run_dir = Path(r'<captured-REPORT_DIR>').name
if not (len(run_dir) == 8 and run_dir.isdigit()):
    raise SystemExit('REPORT_DIR basename %r is not a YYYYMMDD run dir' % run_dir)
d = find_latest_prior('<TICKER>', 'etf-thesis', exclude_date=run_dir)
p = (Path(d) / 'etf_thesis.json') if d else None
print(p.as_posix() if p and p.is_file() else 'none')
") || { echo "FATAL: prior-thesis lookup failed — see the error above. Do NOT continue with 'none': that would stamp this run as the fund's first analysis and drop every comparison against the prior thesis." >&2; exit 1; }
# Say which prior this run is judged against — `none` here on a fund you
# have analysed before is the visible symptom of a lost comparison.
printf 'prior thesis: %s\n' "$PRIOR"
MODEL_ARG=""
# `-s`, not `-f`: on a delete-restricted mount `clear_stale` leaves a ZERO-BYTE
# file, and passing that to the stamp makes it die parsing an empty model
# instead of writing the valid refusal.
[ -s "<captured-REPORT_DIR>/data/etf_model.json" ] && MODEL_ARG="--model-json <captured-REPORT_DIR>/data/etf_model.json"
"$PYBIN" -m scripts.etf.stamp --ticker "<TICKER>" \
  --profile "<captured-REPORT_DIR>/data/etf_profile.json" \
  --market-snapshot "<captured-REPORT_DIR>/data/etf_market_snapshot.json" \
  --state "$PWD/portfolio-state.yaml" \
  --prior-thesis "$PRIOR" \
  --authoring-date "$("$PYBIN" -c "from scripts.delta.calendar import today_et; print(today_et().isoformat())")" \
  --output-json "<captured-REPORT_DIR>/etf_thesis.json" \
  --output-markdown "<captured-REPORT_DIR>/etf_summary.md" \
  $MODEL_ARG
```

Non-zero exit means the staged artifact failed validation and **nothing was
promoted** — a prior run's thesis is intact. Show the stderr to the user; the
message names the field that did not bind. Do not retry with the number
removed: an unbindable claim is the finding.

```bash
[ -s "<captured-REPORT_DIR>/etf_thesis.json" ] || { echo "FATAL: no artifact written" >&2; exit 1; }
```

If any block in this step exits non-zero, **STOP** and surface the error.

## Step 5.5 — Record the run

Only after the artifact is promoted. `--artifact-sha256` is over the bytes
that actually landed, so a later run that replaces the file is detectable
rather than silently inherited.

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
SHA=$("$PYBIN" -c "
import hashlib
with open(r'<captured-REPORT_DIR>/etf_thesis.json', 'rb') as fh:
    print(hashlib.sha256(fh.read()).hexdigest())
")
# `agents_run` records what RAN, so it is keyed on the artifact the agent
# itself writes — not on a constant. Step 4 is skipped on BOTH an
# `entry_eligibility` of `block`/`unknown` (Step 3) and an `analysis_readiness`
# other than `ready` (Step 4), and the constant logged `evaluate-etf` on every
# one of those refusals (feedback 2026-09-01 ④, still open on 09-02). A field
# only ever written and never read is the one that most needs to be right:
# nothing downstream will ever notice it is wrong.
AGENTS=""
[ -s "<captured-REPORT_DIR>/data/etf_model.json" ] && AGENTS="evaluate-etf"
"$PYBIN" -m scripts.delta.run_meta write   --run-dir "<captured-REPORT_DIR>" --ticker "<TICKER>" --skill etf-thesis   --artifact-sha256 "$SHA" --agents-run "$AGENTS"
```

## Step 6 — Report

Read `<captured-REPORT_DIR>/etf_summary.md` and present it in the user's
`output_language` (from `strategy.yaml`).

For a refusal, lead with the reason in plain words and say what would change
it — `not_owner_approved` needs an entry in `strategy.yaml`;
`owner_approval_expired` needs the owner to re-review the fund;
`too_young_for_indicators` needs the fund to trade longer; `price_unavailable`
is worth one retry. Do not present a refusal as a soft "hold".

For a thesis, lead with the merit and the two condition lists. A matched
invalidation condition is evidence that the argument needs re-examining, not a
verdict — say that when you present them.

## What this skill never does

- Never proposes an order, a size, or a price. /portfolio does that.
- Never writes `entry_eligibility` or `analysis_readiness` by judgement.
- Never treats an unresolved identity as a stock.
- Never writes artifacts outside `reports/`. If a write fails, stop and surface
  the error — a run redirected to `/tmp` evaporates and the delta layer can
  never find it again.
