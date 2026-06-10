# Portfolio safety — constraint schema & default principles (adapter)

Canonical source: **`rules/portfolio-safety.md`** — READ before touching
`scripts/validate.py`, `scripts/portfolio_log.py`, or the `/portfolio`
skill. This file is a thin adapter auto-loaded by Claude Code so the
constraint schema is visible from turn 1.

## Hard constraint schema (cited by every consumer)

| Key | Type | Range | Enforced by |
|---|---|---|---|
| `max_single_position` | decimal | [0.0, 1.0] | `validate._guard_constraints` |
| `max_sector` | decimal | [0.0, 1.0] | (stub — `invalid_config` until sector mapping lands) |
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
1. **Risk floor** — portfolio survives extreme scenario (all limits fill + all stops trigger)
2. **Investment discipline** — weak technicals ≠ disqualify, but raise margin; within 7d of earnings no chase; thesis falsification → exit; hard-constraint breach → market sell; conviction → market order
3. **Portfolio management** — rising risk → raise cash; excess cash → deploy by CE rank; concentrate in edge sectors

Read `rules/portfolio-safety.md` before modifying default-principle
extraction logic or the compile step in `.claude/skills/portfolio/SKILL.md`.

## Enforcement

- **Compile stage**: `.claude/skills/portfolio/SKILL.md` Step 2 coerces
  percent → decimal via `cli_utils.normalize_percent_fraction`
- **Validate stage**: `scripts/validate.py` rejects out-of-range + vocab-
  invalid orders; tuple contract preserved for stress tests
- **Audit stage**: `scripts/audit_fail_open.py` pattern F catches raw-
  percent literals like `max_single_position = 35`
- **Log stage**: `scripts/portfolio_log.py` verifies `source_hash`
  matches `strategy.yaml` current principles before writing any
  decision log

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
orders (IBKR MCP = auth only). Two invariants:
- **Never** describe a proposed order as submitted/placed/executed; never fill
  `execution_outcomes` / `user_confirmation.status` — the user does, after acting.
- Editing `portfolio-state.yaml`: show a before/after **diff** → user confirms
  THAT diff → keep the prior version → re-run `config_gate check --portfolio`
  (structure is validated, not correctness — a mistyped share count passes).

Future automated execution (if ever wired) MUST go through a deterministic gateway
(machine-verifiable `account_type` + explicit `CONFIRM LIVE <acct> <hash>`, draft
never reportable as submitted) — NOT built now (anti-ratchet). Full boundary +
gateway contract: `rules/portfolio-safety.md` §"Advisory-only execution boundary".

Full vocabulary + action semantics + 15-rule order logic at
`rules/portfolio-safety.md`.
