# Unit & scale convention (adapter)

Canonical source: **`rules/units.md`** — READ before touching code that
manipulates currency, percent/decimal scale, or time-window tenor.
This file is a thin adapter auto-loaded by Claude Code so the convention
is visible from turn 1.

## Quick reference

| Axis | Suffix | Example |
|---|---|---|
| Currency | `_usd` / `_local` / `_jpy` / `_eur` | `fcf_per_share_usd = 4.09` |
| Scale | `_pct` (human 35) vs `_decimal` / `_frac` (0.35) | compiled constraints MUST be decimal |
| Tenor | `_5y` / `_10y` / `_2y` | must match source ticker (^FVX=5y, ^TNX=10y) |
| Basis | `_bps` (50 = 0.5%) | spread / yield differential |
| Period | `_annual` / `_quarterly` / `_ttm` | cash flow statements |
| Per-unit | `_per_share` / `_per_adr` | EPS, FCF |

## Hard rules (most frequently violated)

1. `strategy.compiled.yaml` constraints (`max_single_position`, `max_sector`,
   `min_cash`) are ALWAYS decimal [0.0, 1.0]. Raw percent (35) is coerced
   at compile time via `cli_utils.normalize_percent_fraction`.
2. `extract_fcf` and `historical_multiples` 3-state currency gate:
   USD-native (no cert), supported non-USD (FX-convert via
   `scripts.fx_apply.apply_fx_conversion` + emit cert), unsupported
   (fail-close). Post-DL3c, do NOT silently compute
   `price_USD / earnings_local`; conversion is the supported path.
3. Treasury tenor suffix must match source ticker: `^FVX` → `us_5y`,
   NEVER `us_2y` (HIGH-4 historical bug).
4. DL3c — post-FX-converted rows tag `currency = "USD"` AND set internal
   `_pre_conversion_currency` (write-only tag, NEVER read in production —
   Pattern AD HIGH). Money fields keep their canonical names (no
   `_usd` suffix); USD status is conveyed via the row tag + artifact-root
   `currency_conversion` cert OR the absence of cert with
   `_dl3c_version: 1` marker (invariant 7).

## Enforcement

- **Static**: `scripts/audit_fail_open.py` patterns
  - `F: raw-percent-constraint` (constraint values ≥ 20)
  - `I: unit-suffix-mismatch` (`_pct = 0.35` or `_decimal = 50`)
  - `J: tenor-mismatch` (us_2y paired with ^FVX)
  - `AC: fx-conversion-without-cert` (local→usd transition without cert write)
  - `AA: fx-rate-like-literal` (MED — backup heuristic for FX-rate literals)
  - `AD: _pre_conversion_currency reader` outside `scripts/fx_apply.py`
  - `AE: producer-marker-wrap` (4 named producer functions must wrap returns
     via `cli_utils.emit_dl3c_root_marker`)
- **Runtime**: `cli_utils.normalize_percent_fraction`,
  `validate._guard_constraints`, 3-state gate in `extract_fcf` +
  `historical_multiples` + `adr/correct`
- **Escape hatches** (trailing per-line comments):
  - `# unit-ok: <reason>` — unit/scale mismatch
  - `# fx-conversion-ok: <reason>` — Pattern AC (each FX conversion site)
  - `# fx-rate-ok: <reason>` — Pattern AA (FX-rate literal)
  - `# pre-conversion-currency-read-ok: <reason>` — Pattern AD (rare)
  - `# dl3c-marker-ok: <reason>` — Pattern AE (rare)

Full policy + known-united fields table + per-producer contracts at
`rules/units.md`.
