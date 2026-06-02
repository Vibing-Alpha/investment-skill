# Unit & Scale Naming Convention

**Canonical source** for the unit/scale convention. Thin adapter with a
quick-reference summary lives at `.claude/rules/units.md` (auto-loaded
by Claude Code). Consumers on non-Claude-Code platforms should read
this file directly.

This codebase has suffered **5 unit-mismatch HIGHs** in one audit cycle:

| Bug | Site | Shape |
|---|---|---|
| HIGH-4 | `macro.py` | `^FVX` is 5Y Treasury but emitted as `us_2y` |
| HIGH-10 | `strategy.compiled.yaml` | `max_single_position: 35` (raw pct) vs validator expecting decimal |
| HIGH-26 | `extract_fcf.py` | `fcf_per_share` in JPY vs `current_price` in USD → 802% yield |
| HIGH-27 | `historical_multiples.py` | `price_usd / earnings_local` → PE off by 560-725× |
| HIGH-27a | `metrics_snapshot.pe` | FD returns 3.18e-08 scale → rounded to 0.0 |

All 5 share one pattern: **two numbers that are valid floats but semantically
incompatible units.** No type checker catches this. No linter catches this.
Unit tests pass because the arithmetic runs without exception.

## Suffix rules

Use these suffixes when the variable name alone would be ambiguous about
units/scale/currency:

### Currency

| Suffix | Meaning | Example |
|---|---|---|
| `_usd` | US dollars (canonical) | `fcf_per_share_usd = 4.09` |
| `_local` | Statement currency (may be non-USD) | `net_income_local = 82_665_000_000` (JPY) |
| `_jpy` / `_eur` / `_gbp` | Specific non-USD currency | `price_jpy = 2220` |

When in doubt, prefer explicit `_usd` / `_local` over the concrete currency
code — it makes the contract with the caller more visible.

### Percent vs decimal

| Suffix | Meaning | Example |
|---|---|---|
| `_pct` | Human percent — `5` means 5% | `max_single_position_pct = 35` |
| `_decimal` | Decimal fraction — `0.05` means 5% | `max_single_position_decimal = 0.35` |
| `_frac` | Alias for `_decimal` | `cash_frac = 0.15` |
| `_bps` | Basis points — `50` means 0.5% | `spread_bps = 25` |

**Hard rule:** the `strategy.compiled.yaml` / `validate.py` contract is
always `_decimal` (0.0–1.0). Raw-percent input is normalized at compile
time via `cli_utils.normalize_percent_fraction`.

### Time / Rates / Tenor

| Suffix | Meaning | Example |
|---|---|---|
| `_days` | Number of days | `freshness_days = 7` |
| `_years` | Number of years | `projection_years = 10` |
| `_2y` / `_5y` / `_10y` | Tenor of a yield curve point | `us_10y`, `us_5y` |
| `_annual` / `_quarterly` | Period of a flow measurement | `fcf_annual`, `fcf_quarterly` |
| `_ttm` | Trailing twelve months (flow) | `fcf_ttm`, `revenue_ttm` |

**Hard rule:** tenor suffix on treasury yields MUST match the underlying
ticker. `^FVX` = 5-Year → `us_5y`. Never use `_2y` on a `^FVX` derivative.

### Per-share / per-unit

| Suffix | Meaning | Example |
|---|---|---|
| `_per_share` | Per ordinary share | `fcf_per_share` |
| `_per_adr` | Per ADR unit (after ADR ratio adjustment) | `eps_per_adr` |
| `_ttm` | Trailing 12 months aggregate | `revenue_ttm` |

### Ratios / multiples (unitless)

No suffix needed on `pe`, `pb`, `ps`, `ev_ebitda` since they are universally
understood to be dimensionless. BUT: if a function accepts either a decimal
yield or a percentage yield as input, use `yield_decimal` vs `yield_pct`.

## Known-united artifact fields

These fields MUST have the stated unit across producer + consumer:

| Field | Unit | Producer | Consumers |
|---|---|---|---|
| `current_price` | USD | fetch / extract_fcf | thesis, reverse_dcf, historical_multiples |
| `market_cap` | USD | fetch (yfinance) | adr/correct, validate (indirectly) |
| `fcf_per_share` | USD per ADR | extract_fcf | reverse_dcf, valuation prompt |
| `outstanding_shares` | ordinary shares (count) | fetch (FD) | historical_multiples, extract_fcf |
| `adr_units` | ADR units (count) | adr/correct | adr/correct internal |
| `adr_ratio` | ordinary shares per ADR | adr/correct | prompts (informational) |
| `us_5y`, `us_10y`, `spread_10y_5y` | percent (e.g. 4.5) | macro | rates_fallback, thesis |
| `max_single_position`, `max_sector`, `min_cash` | decimal [0, 1] | strategy.compiled.yaml | validate, portfolio |

## DL3c post-FX-conversion convention

After `scripts.fx_apply.apply_fx_conversion` rewrites statement rows in
place (cycle-8 F1 + cycle-15 F-15-1/F-15-2):

| Row tag | Meaning | Producer | Reader |
|---|---|---|---|
| `currency: "USD"` | row was retagged after FX conversion | `apply_fx_conversion` Step 7 | downstream USD-only consumers |
| `_pre_conversion_currency` | internal audit-only — original local currency before mutation | `apply_fx_conversion` Step 7 | **NO production reader** (Pattern AD HIGH) |
| `fx_rate_usd_per_local` (cert window) | per-quarter rate emitted at cert level only | `scripts.fx_apply._build_cert_block` | typed loader `scripts.schemas.fx_window` |

**Post-conversion field naming rule:** every money field that flows
through `apply_fx_conversion` Step 7 is converted to USD in place — there
is no separate `_usd`-suffixed variant. The fact of conversion is
recorded in the row's `currency = "USD"` tag PLUS the artifact-root
`currency_conversion` cert block. Downstream consumers should treat the
12 master-list field names (`revenue`, `net_income`, `free_cash_flow`,
etc.) as USD when `currency_conversion.basis == "usd_converted"` (cert
present) OR when the artifact carries `_dl3c_version: 1` with no cert
(USD-native, invariant 7).

**`_pre_conversion_currency` is an internal write-only tag:** any
production code outside `scripts/fx_apply.py` reading this field is a
Pattern AD HIGH finding. The escape `# pre-conversion-currency-read-ok:
<reason>` is reserved for the audit/debug-only path.

## Compile-time / runtime enforcement

Three layers:

1. **Convention (this file)** — developer hygiene; enforced by code review.
2. **`scripts/audit_fail_open.py`** — static regex scan catches the highest-signal
   mismatches (e.g., `_pct` variable assigned a literal < 1.0, `_decimal`
   variable assigned a literal > 1.0, raw integer ≥ 20 assigned to
   `max_single_position`).
3. **Runtime guards** — `cli_utils.normalize_percent_fraction`, `validate._guard_constraints`,
   `extract_fcf` currency check, `historical_multiples` currency check.
   These are the last line — they catch stale compiled files and
   upstream-contaminated API responses.

## Annotation escape

A trailing `# unit-ok: <reason>` comment suppresses the audit check on
that line. Use when the convention would be misleading — e.g., a
mathematical identity where the numeric literal is dimensionless (pi,
e, time constants like 365, etc.).

Never use `# unit-ok` without a specific reason. Bare `# unit-ok:` fails
the audit.
