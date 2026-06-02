---
name: monitor
description: |
  Daily entry-point / triage-router for the stock system. Run it each day: it
  assesses cross-system state (holdings + watchlist) and produces a ranked action
  plan, routing each flagged item to the right existing skill, then triggers the
  selected one(s) on your confirmation. It states facts and routes — it does NOT
  decide or place trades.
  Trigger phrases: "monitor", "/monitor", "daily check", "what should I look at
  today", "what needs attention", "morning check", "any alerts", "run my daily
  monitor", "what's new on my holdings/watchlist".
  Requires portfolio-state.yaml (holdings + watchlist). Reuses prior
  /score-business + /investment-thesis artifacts; does not itself analyze.
  NOT for making portfolio/allocation decisions (it routes to portfolio).
  NOT for discovering new tickers (it routes to screen-stocks).
  NOT for writing reports (use write-report).
user_invocable: true
---

# /monitor — Daily Entry-Point / Triage-Router

Orchestration only. This skill runs the deterministic `scripts.monitor` CLI and the
router prompt, then routes you to the right skill. It owns NO analysis logic and makes
NO trade decisions.

## Step 0: Resolve the run directory

```bash
RUN_DATE=$(python3 -c "from scripts.delta.calendar import today_et; print(today_et().strftime('%Y-%m-%d'))")
REPORT_DIR="reports/monitor/$(echo "$RUN_DATE" | tr -d '-')"
```

## Step 1: Probe (deterministic facts)

```bash
python3 -m scripts.monitor probe --state portfolio-state.yaml \
  --output "$REPORT_DIR/monitor_probe.json" --run-date "$RUN_DATE"
```
If this **exits non-zero** (empty universe — no holdings or watchlist, or missing
`portfolio-state.yaml`), there is nothing to monitor: tell the user and stop. Do not
proceed to Step 2.

## Step 2: Dispatch the router (judgment → raw plan)

First clear any stale raw plan. The date directory is reused on a same-day rerun, so without
this a router that fails to write would leave the existence gate below satisfied by an EARLIER
run's `action_plan.raw.json`, and the pipeline would go on to validate/render stale router
output. Removing it first makes the gate prove THIS run produced a fresh plan:

```bash
rm -f "$REPORT_DIR/action_plan.raw.json"
```

Dispatch a subagent with `prompts/monitor-route.md` as its instructions and the
**concrete resolved** probe path as its sole input. **CRITICAL — a subagent does NOT
inherit this shell's `$REPORT_DIR`** (`.claude/rules/skill-architecture.md` #8): when you
compose the dispatch prompt, SUBSTITUTE the literal resolved paths — e.g.
`reports/monitor/20260601/monitor_probe.json` as the input and instruct it to WRITE
`reports/monitor/20260601/action_plan.raw.json` — never a literal `$REPORT_DIR/...`
string (the subagent's shell would resolve that to `/action_plan.raw.json`). Writing
`.json` is allowed for subagents (only the `.md`-write tool is blocked). The router reads
ONLY the probe (no other files, no WebSearch). After dispatch, HARD existence gate:

```bash
[ -s "$REPORT_DIR/action_plan.raw.json" ] || { echo "FATAL: router produced no action_plan.raw.json" >&2; exit 1; }
```

## Step 3: Validate + stamp (HARD gate)

```bash
python3 -m scripts.monitor validate \
  --raw "$REPORT_DIR/action_plan.raw.json" \
  --probe "$REPORT_DIR/monitor_probe.json" \
  --output "$REPORT_DIR/action_plan.json"
```
If this exits non-zero, the router produced an invalid or boundary-violating plan
(e.g. an order/advice in a reason, a dangling evidence_ref, a `status` field). Do NOT
render or trigger. **Exactly ONE repair attempt:** re-dispatch the SAME router prompt with
the SAME concrete probe path + the validator's stderr appended ("your previous plan failed
validation: <stderr> — fix and re-emit to the same path"), then re-run `validate` once. If
it fails the SECOND time, surface the stderr and STOP (no further retries — escalate to the
user). This bounds the loop and never renders an unvalidated plan.

## Step 4: Render the digest (deterministic)

```bash
python3 -m scripts.monitor render-digest \
  --plan "$REPORT_DIR/action_plan.json" \
  --probe "$REPORT_DIR/monitor_probe.json" \
  --output "$REPORT_DIR/digest.md"
```

## Step 5: Present + choose

Show the user `$REPORT_DIR/digest.md`. Then use AskUserQuestion to ask which item(s) to act
on — options are the plan's items (each shown as `<ticker> — <priority> → <route>`) plus
"none". The plan is a triage; the user decides what to run.

## Step 6: Trigger the selected skill(s)

For each item the user selected, invoke its `route` skill **sequentially** (never in
parallel), passing the ticker where applicable:
- `/investment-thesis <ticker>` · `/portfolio` · `/score-business <ticker>` · `/screen-stocks`

Invoke one, let it complete (it owns its own cascades), then the next. Do NOT trigger
anything the user did not select. `/monitor` itself decides nothing about the trades —
the target skill does.
