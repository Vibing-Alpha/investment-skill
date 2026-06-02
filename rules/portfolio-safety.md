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
2. **All-buy** — all proposed + existing limit buys fill
3. **All-sell** — all proposed + existing limit sells fill
4. **Extreme-down** — all buys fill + all stops trigger (uses crashed
   account value as denominator for position-% checks)
5. **Defensive** — all stops trigger, no buys fill
