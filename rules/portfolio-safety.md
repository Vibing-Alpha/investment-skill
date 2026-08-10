# Portfolio Safety — Default Principles and Constraint Schema

**Canonical source** for constraint schema + default principles. Thin
adapter with quick-reference summary lives at
`.claude/rules/portfolio-safety.md` (auto-loaded by Claude Code).
Consumers on non-Claude-Code platforms should read this file directly.

## Default Principles

When the user has no `principles:` field in `strategy.yaml`, these
defaults apply. They are stored in English and presented to the user
in `output_language` from strategy.yaml.

### Layer 1 — Risk Floor

- After any proposed trade executes, the portfolio must survive an
  extreme scenario where all limit buys fill and all stop losses trigger.

### Layer 2 — Investment Discipline

- Weak technicals do not disqualify a fundamentally strong company,
  but require a larger margin of safety for entry.
- Within 7 days of earnings, do not chase price — use limit orders
  with a meaningful discount.
- Thesis falsification is sufficient reason to exit — do not wait for
  price confirmation.
- When a hard constraint is breached, use market sell immediately —
  do not use limit sells for better pricing.
- Firm conviction to buy or sell → market order. Want to buy but not
  urgent → limit order.
- Before placing new orders, check for contradicting existing GTC
  orders that should be canceled.

### Layer 3 — Portfolio Management

- When broad market trend deteriorates, raise cash allocation — do not
  mechanically reduce all positions equally.
- When cash significantly exceeds target, deploy proactively by
  capital efficiency ranking.
- Concentrate in sectors where you have a knowledge edge — do not
  diversify for diversification's sake.

## Hard Constraint Schema

Hard constraints are extracted from user principles when they contain
quantifiable limits. The system recognizes these constraint types:

| Key | Type | Range | Meaning |
|-----|------|-------|---------|
| `max_single_position` | float | 0.0-1.0 | Single stock cap as fraction of portfolio (compile auto-coerces 35 → 0.35 per skill Step 2) |
| `max_sector` | float | 0.0-1.0 | Single sector cap as fraction of portfolio (compile auto-coerces; sector lookup not implemented — validate.py fails closed until wired) |
| `min_cash` | float | 0.0-1.0 | Minimum cash as fraction of portfolio (compile auto-coerces 10 → 0.10 per skill Step 2) |
| `max_holdings` | int | 1+ | Maximum number of positions |

All are optional. Only present when the user's principles define them.
The constraint names above are the canonical keys that `scripts/validate.py`
recognizes. The principle compilation step must map user language to these
exact keys.

### Fail-Closed Behavior (validate.py, HIGH-9..14)

`scripts/validate.py` enforces these as belt-and-suspenders on top of
compile-stage coercion:

- **Range guard** (HIGH-10): any fraction key (`max_single_position`,
  `max_sector`, `min_cash`) with a value outside `[0.0, 1.0]` produces
  an `invalid_config` violation — the guard does NOT re-coerce, so
  stale compiled files surface loudly.
- **max_sector stub** (HIGH-9): setting `max_sector` — even with a
  valid decimal — emits `invalid_config` until sector mapping is
  implemented.
- **Order vocabulary** (HIGH-11/12): `action ∈ {buy, sell}`,
  `type ∈ {market, limit, stop}`, `shares > 0` integer. Whitespace,
  case variants, non-int/float shares are rejected strictly via
  `invalid_action` / `invalid_type` / `invalid_shares`.
- **Missing order price** (HIGH-13): an order with no `est_price` /
  `price` and no `ticker_prices` entry emits `missing_price_order`.
- **Missing holding price** (HIGH-14): when any ratio constraint is
  active (`min_cash`, `max_single_position`, `max_sector`), holdings
  without a price emit `missing_price` and fail the validation.

## Stress Test

The stress test runs unconditionally, even with zero hard constraints.
It verifies that the proposed order set does not produce negative cash
under 5 scenarios:

1. **Base** — only market orders execute
2. **All-buy** — proposed market sells execute, and all proposed +
   existing limit buys fill
3. **All-sell** — all proposed + existing limit sells fill
4. **Extreme-down** — proposed market sells execute, all buys fill + all
   stops trigger (uses crashed account value as denominator for
   position-% checks)
5. **Defensive** — all stops trigger, no buys fill

Proposed **market** orders in BOTH directions are part of the
immediately-executed state that every scenario builds on — that is what
"only market orders execute" means at Base, and scenarios 2 and 4 build on
the same state rather than discarding its sell leg. What those scenarios
stress is the **contingent** orders (limit / stop / close-auction), which
may or may not fill. A WORKING (already-resting broker) market sell is
excluded: it may be halted, so it is not available cash.

The same funded state feeds the all-fills policy-floor checks
(`min_cash` / `max_holdings` / `max_single_position`). A breach that
pre-exists with working orders alone stays a WARNING even when the
proposed sale cures it — the resting order can still fill first, and the
warning is compared **per ticker**, so a sold position's pre-existing
breach is not swallowed by a funded breach on some other name.

### Order projection prices

A **market** order is projected against the live quote in BOTH
directions: proceeds at `min(explicit, quote)`, cost at
`max(explicit, quote)`. A self-reported estimate can therefore neither
certify proceeds the market will not pay nor under-cost a purchase. An
uncapped BUY, or a MARKET sell, with **no quote at all** is
unprojectable and fails closed. An UNCAPPED non-market sell keeps its
existing projection (the min over its explicit fields); a CAPPED one is
covered by the ceiling rule below.

A price ceiling exists only when BOTH halves hold: the type is one of
`limit` / `loc` / `stop_limit` / `gtc` (or the legacy broker spellings
`limit_buy` / `limit_sell`), **and** the order STATES a ceiling in
`limit_price` or `price`. Both halves are load-bearing — a `limit` buy carrying only an
`est_price` has no ceiling at all (an estimate is not a commitment), and
a `market` buy carrying a stray `limit_price` must not gain one. Do NOT
read this as "contingent orders are contractual": a `stop` /
`stop_market` buy triggers and then executes AT MARKET, `moc` has no
price protection, and `stop_price` is a TRIGGER, never a ceiling.

A capped order is projected AT its stated contractual bound, never at the
extreme of every explicit field: a BUY at the max of its stated ceilings,
a SELL at the min of its stated floors — plus its `stop_price` when the
type is a stop, because a stop-limit's trigger also bounds the realistic
fill. An `est_price` beyond that bound is not conservative but wrong (the
order cannot fill there): costing a limit-$100 buy at a stale $150
estimate produced false refusals plus a `decisions.md` line reading
"limit $100" beside a $150 cost, and the sell mirror understated proceeds
100x. A stray `stop_price` on a NON-stop type is not part of that order's
contract and is ignored.

Known limitation, unchanged by the above: the projection has no way to
express "this order does not fill". A stop-limit sell whose floor sits
above its trigger is credited at the trigger and its shares removed,
where in reality it would rest unfilled. Fixing that needs a non-fill
concept in the projection, not a price rule.

Sell-side stop projections are unchanged — stops use their own price
field — so the risk-floor scenario keeps its current meaning.

Working (open) orders carry a **legacy broker vocabulary** with no
type-vocab gate: `limit_buy` / `stop_buy` / `limit_sell` / `stop_sell`.
`limit_buy` and `limit_sell` follow the same ceiling rule as their schema
spellings (a broker-synced order usually states its limit in `price`,
which counts) and are never re-priced to the quote; a `stop_buy` is
uncapped — it triggers and then executes at market — so it IS
quote-bounded, like schema `stop` / `stop_market`.

### Input bindings

Every input that determines the output is bound to the artifact that used
it, and the logger refuses to write a record whose inputs have since
drifted. Absent binding (legacy artifact / legacy seal) warns instead of
refusing.

- **Validator artifact** — `state_file_sha256`, `constraints_validated`,
  `orders_validated`, and `ticker_prices_sha256`. Live quotes are
  verdict-determining (they value every holding for the ratio checks and
  price uncapped market orders), so a PASS may not be logged against
  different quotes than it was computed from.
- **Authoring seal** (`.decision_ctx.json`, written before the decisions
  are authored) — `state_file_sha256`, `thesis_generated_at`, and
  `strategy_source_hash`. The last binds WHICH STRATEGY authored the
  decisions: a mid-run principle edit plus a legitimate recompile
  otherwise let a superseded decision be logged and stamped with the new
  `constraints_active.source_hash`. `constraints_validated` cannot cover
  this — a text-only principle edit leaves the four hard-limit values
  untouched.

### Funding boundary

Proposed market-sale proceeds may fund **proposed** buys only. A working
broker buy that is not solvent against **pre-trade** cash raises
`working_buys_unfunded`: a resting order can fill before the sell is even
submitted, so the "submit the sell first" control cannot reach it.

Because a proposed buy need no longer be independently cash-funded, when
an order set depends on proposed market-sale proceeds, ONE sequencing
`execution_note` must accompany that set — attached to any one of its
orders — instructing the user to submit the sell first and wait for a
confirmed fill. Per SET, not per buy: cash is fungible, so "which buy
depends on the proceeds" is indeterminate and repeating the note on every
nominally dependent buy would restate an arbitrary attribution without
adding protection.

## Single-root guard — dual standard (setup HARD, runtime GRADED)

Failure mode guarded: **a money-path run reading the WRONG clone's portfolio**
(two clones with divergent `portfolio-state.yaml` — e.g. a CC-CLI clone plus a
Cowork-default clone, or a copied directory).

Two enforcement points with DIFFERENT strictness — this asymmetry is deliberate:

1. **Setup-time (HARD invariant, unchanged).** `confirm_setup` →
   `root_resolve.find_conflicts`: if MORE THAN ONE candidate root holds a real
   fund-state (repo marker + `portfolio-state.yaml`), setup REFUSES to stamp or
   write `~/.stock-v7-home`. A real-money tool must never *confirm* a world with
   two divergent portfolio-states.

2. **Runtime (GRADED, Plan B Task 4c).** `config_gate.main`'s pre-check guard
   (`_single_root_guard`, keyed on `root_resolve.root_source()`) grades by
   surface instead of always blocking:

   | Condition (no `--root` override) | base check (single-ticker / discovery) | `check --portfolio` |
   |---|---|---|
   | marker absent, running root is a Cowork mount (`/sessions/*/mnt/*`) | proceed (prelude's single-mount fail-closed established the root; Cowork has no persistent `$HOME` marker) | proceed |
   | marker absent, non-Cowork (CC-CLI / persistent) | WARN + proceed | **BLOCK** — "no confirmed single-root — run /stock-v7-setup" |
   | marker == running root | proceed | proceed |
   | marker is a DIFFERENT fund-state | WARN (both absolute paths + "portfolio-level skills still block") + proceed | **BLOCK** |
   | marker elsewhere but NOT a fund-state | proceed (nothing to protect) | proceed |
   | corrupt/relative marker or env (`resolve_root` raises) | WARN + proceed | **BLOCK** |

   Rationale for the downgrade (risk-tier policy): the wrong-clone blast radius
   differs by surface. A portfolio-level read from the wrong clone feeds a real
   trade → fail-closed. A single-ticker analysis/discovery run from the wrong
   clone produces at worst a misplaced per-ticker report → warn loudly, don't
   strand the user. An EMPTY marker behaves as absent (not a phantom block).
   Comparison hardening: `os.path.samefile` when both roots exist, else a
   `os.path.normcase`-folded `resolve()` compare (Windows case-insensitivity /
   git-bash path forms). Cowork detection is a path-shape heuristic by design —
   do NOT add a sentinel-file alternative (anti-ratchet).

   The BASE gate (`assert_money_path_ready` — personalization + keys +
   portfolio-state floor) stays fail-closed on EVERY surface; only the
   cross-clone guard is graded, and a warn never bypasses the base gate.

## Advisory-only execution boundary

This system is **advisory-only**. `/portfolio` produces order *recommendations*; a
human executes them at their broker; the human (with the agent's help) updates
`portfolio-state.yaml`. There is **no code path that submits orders**, and the bundled
IBKR MCP exposes only authentication — no order tool. Two invariants follow:

**1. No false execution attestation.** Orders are RECOMMENDATIONS / proposed orders.
The agent MUST NOT describe a proposed order as submitted, placed, filled, or executed,
and MUST NOT fill `execution_outcomes` / `user_confirmation.status` — those are written
by the user AFTER they act. (Guarded by `prompts/portfolio-decide.md`'s "Advisory-only"
statement + `tests/test_prompt_lint.py::test_portfolio_orders_are_advisory_only`.) Why:
a recommendation mis-reported as "done" makes the user think they hold a position they
don't (or vice-versa) — corrupting cash, weights, and every later decision.

**2. Manual holdings-update protocol.** Editing `portfolio-state.yaml` is the only
holdings mutation, and it is user-confirmed. Before writing it the agent MUST: (a) show
a before/after **diff** of the specific fields changing; (b) get the user to confirm
**that diff** (not a vague "looks good"); (c) keep the prior version (so a wrong edit is
reversible); (d) re-run `python3 -m scripts.config_gate check --portfolio` after writing.
Why: `config_gate` validates portfolio-state *structure* (shares > 0, cash ≥ 0, shapes)
but NOT *correctness* — `100` mistyped as `1000` is structurally valid and would silently
feed every future decision; the diff-confirm + reversibility is the control that fits.

**Future-execution gateway contract (NOT built — spec only, YAGNI).** IF automated broker
execution is ever wired, it MUST NOT be prose in a SKILL — it must be a deterministic
gateway that: reads the broker MCP's **machine-verifiable** `account_type` (paper|live) +
account id + authenticated principal + trading permission; requires the user's LATEST
message to carry an explicit `CONFIRM LIVE <account-id> <draft-hash>` token (not a free-text
"yes" the agent can satisfy on its own); any field missing/unverifiable → **draft only**;
emits `execution_result.json`; the draft is always named `order_draft` and can NEVER be
reported as submitted. At that point — and only then — add a static audit that no order
reaches the broker except through the gateway. Until execution is actually wanted, do NOT
build the gateway, a single-writer module, a holdings-path classifier, or that audit
(anti-ratchet — they would guard failure modes that cannot occur in the advisory system).
