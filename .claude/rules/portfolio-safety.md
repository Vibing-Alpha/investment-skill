# Portfolio safety — constraint schema & default principles (adapter)

Canonical source: **`rules/portfolio-safety.md`** — READ before touching
`scripts/validate.py`, `scripts/portfolio_log.py`, or the `/portfolio`
skill. This file is a thin adapter auto-loaded by Claude Code so the
constraint schema is visible from turn 1.

## Hard constraint schema (cited by every consumer)

| Key | Type | Range | Enforced by |
|---|---|---|---|
| `max_single_position` | decimal | [0.0, 1.0] | `validate._guard_constraints` |
| ~~`max_sector`~~ | — | — | **REFUSED by `compile_strategy` (exit 1)** — sector lookup unimplemented, so every run would fail closed. Delete the line; an older example shipped it and it never enforced anything |
| `min_cash` | decimal | [0.0, 1.0] | `validate._check_cash_floor` |
| `max_holdings` | int | ≥ 1 | `validate._check_max_holdings` |

**Unit rule:** all fraction-typed keys MUST be decimal in
`strategy.compiled.yaml`. Raw-percent input (`35` in user's
`strategy.yaml`) is coerced at compile time via
`cli_utils.normalize_percent_fraction`. `validate.py` is the
belt-and-suspenders layer: any value outside [0, 1] surfaces as
`invalid_config`, it does NOT silently re-normalize.

## Default principles (when user has no `principles:` field)

Three layers (full text at canonical source):
1. **Risk floor** — portfolio survives extreme scenario (all buys fill + all stops trigger — any type, not just limits)
2. **Investment discipline** — weak technicals ≠ disqualify, but raise margin; within the configured earnings window (`orders.earnings_window_days`) no chase; thesis falsification → exit; hard-constraint breach → market sell; conviction → market order
3. **Portfolio management** — rising risk → raise cash; excess cash → deploy by CE rank; concentrate in edge sectors

Read `rules/portfolio-safety.md` before modifying default-principle
resolution or the compile step (`scripts/compile_strategy.py`, invoked by
`.claude/skills/portfolio/SKILL.md` Step 2).

## Enforcement

- **Compile stage**: `scripts/compile_strategy.py` projects `risk:` onto
  `hard_constraints` (refusing unknown keys) and coerces percent → decimal via
  `cli_utils.normalize_percent_fraction`. Re-derives every run — no cache branch
- **Validate stage**: `scripts/validate.py` rejects out-of-range + vocab-
  invalid orders; tuple contract preserved for stress tests
- **Audit stage**: `scripts/audit_fail_open.py` pattern F catches raw-
  percent literals like `max_single_position = 35`
- **Log stage**: `scripts/portfolio_log.py` verifies `source_hash` via the
  shared `schemas.strategy.canonical_policy_hash` — covering `principles` +
  `principle_notes` + `risk` — before writing any decision log

## Single-root guard — dual standard (quick-ref)

Guards: **a money-path run reading the WRONG clone's portfolio.**
- **Setup-time = HARD**: `confirm_setup` → `find_conflicts` refuses to stamp
  when >1 root holds a real fund-state.
- **Runtime = GRADED** (`config_gate._single_root_guard`, keyed on
  `root_resolve.root_source()`): marker absent (non-Cowork) / corrupt marker /
  different-fund-state mismatch → `--portfolio` **BLOCKS**, base check
  **WARNS + proceeds**. Cowork mounts (`/sessions/*/mnt/*`) proceed with no
  marker (prelude single-mount fail-closed owns it). Empty marker = absent.
  Compare roots via `samefile` / normcase-folded resolve, never raw `resolve() !=`.
Full matrix + rationale: `rules/portfolio-safety.md` §"Single-root guard".

## Advisory-only execution boundary (quick-ref)

This system is **advisory-only** — `/portfolio` outputs order RECOMMENDATIONS;
the user executes manually + maintains `portfolio-state.yaml`. No code submits
orders (IBKR MCP = auth only). Three invariants:
- **Never** describe a proposed order as submitted/placed/executed; never fill
  `execution_outcomes` / `user_confirmation.status` — the user does, after acting.
- Editing `portfolio-state.yaml`: show a before/after **diff** → user confirms
  THAT diff (**the automatic `nav_peak` ratchet is exempt from CONFIRMATION —
  #9 puts refresh on the system, so it is DISCLOSED and applied without asking —
  but not exempt from deferral**) →
  **DEFER the write until after the decision log is written** → keep the prior
  version → write → re-run `config_gate check --portfolio` (structure is
  validated, not correctness — a mistyped share count passes; **restore the kept
  version if it fails** — stopping alone would leave a malformed state file to
  poison the next run). The deferral
  covers ALL sanctioned writers (nav_peak ratchet, peak reconfirmation, fill
  update): the validator and the authoring seal both bind a `state_file_sha256`
  and `portfolio_log` refuses against both, so a mid-run write costs the run its
  log — but ONLY for a fill THIS run's own recommendations caused; any other
  (completed before the run, or a working order filling mid-run) is synced
  BEFORE analysis and the run restarted, as is a log that succeeded only with
  the "no seal (legacy flow)" warning,
  or the log durably records recommendations made against stale holdings.
  If logging fails, fix what the message names and retry — **never write state to
  get past a refusal** (the first error cannot prove the file is unchanged; most
  checks precede the hash comparisons). An unrepairable refusal ends the run with
  no log and no write. Combine multiple pending changes into ONE write; if the post-write check fails,
  restore the kept version and stop — the change is UNRESOLVED: a **fill** needs
  syncing before the next analysis (the broker moved, the file did not), while a
  **ratchet/reconfirmation** moved nothing (nothing recovers the ratchet — the
  next run recomputes from its own NAV; the reconfirmation is asked again). A refusal whose remedy requires EDITING state (e.g. absent
  `open_orders`) also ends the run — make that edit as a pre-analysis sync and
  start over. Residual:
  a change lost to that, or to a session ending first, is re-established next run
  — a fill is re-reported (ONLY then — an unreported fill is undetectable, since
  the log holds a proposed order and a fill is never inferred); a missed ratchet is simply not written.
- A proposed buy may be funded by a proposed **market sell** in the same set
  (validator credits it). So when the set depends on those proceeds, ONE
  sequencing `execution_note` must accompany it — on any ONE of its orders —
  telling the user to **submit the sell first and wait for a confirmed fill**.
  Per SET, not per buy (cash is fungible; per-buy attribution is arbitrary).
  Nothing machine-enforces this: it is the sole control on the accepted risk.
  Working broker buys are NOT fundable this way — an unfunded one raises
  `working_buys_unfunded` (it can fill before the sell is even submitted).

Future automated execution (if ever wired) MUST go through a deterministic gateway
(machine-verifiable `account_type` + explicit `CONFIRM LIVE <acct> <hash>`, draft
never reportable as submitted) — NOT built now (anti-ratchet). Full boundary +
gateway contract: `rules/portfolio-safety.md` §"Advisory-only execution boundary".

Full vocabulary + action semantics + 15-rule order logic at
`rules/portfolio-safety.md`.
