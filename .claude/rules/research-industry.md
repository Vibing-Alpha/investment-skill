# research-industry — Hard constraints (adapter)

Canonical source: **`rules/research-industry.md`** — READ before modifying
`prompts/research-industry.md`, `.claude/skills/research-industry/SKILL.md`,
or any `scripts/industry/*.py` producer.

This file is the thin auto-loaded adapter so the constraint shape is
visible from turn 1.

## Quick reference

| Axis | Rule | Enforcement |
|---|---|---|
| Ticker universe | US common stock / US-listed ADR / **active** OTC ADR only | Agent judgment + WebSearch verify |
| Excluded states | Delisted, acquired, ADR-terminating, pre-IPO, foreign-only | Agent must verify via current-year WebSearch |
| Source tags | `[KIND: specific descriptor]` — no placeholder theater | `scripts/schemas/source_tag.py` runtime validator |
| Slug | `^[a-z0-9]+(-[a-z0-9]+)*$` | `scripts/industry/normalize_slug.py` |
| TAM dispersion | >2x span across sources → surface as risk | Agent rule (prompt §Quality checks) |
| CAGR dispersion | >3pp span across sources → surface as risk | Agent rule (prompt §Quality checks) |
| Data vintage | framing # past validity window (TAM/CAGR ~18mo, penetration ~12mo, supply-demand ~6mo) → tag vintage + flag stale in risks | Agent rule (rules §3.5 / prompt §Phase 1) |
| TAM USD convention | non-USD source → convert at annual avg FX + tag with `[Calc: ...]` | Agent rule (rules §6) |
| Overextended regime | abs(60d_pct) ≥ 30% → mean-reversion risk required | Agent rule (prompt §Phase 3) |
| OTC ADR risk | P1/P2 OTC → top-level liquidity risk entry required | Agent rule (prompt §Phase 4) |

## When to read the canonical file

Read `rules/research-industry.md` (full ~150 lines) before:
- Modifying the candidate-selection logic in `prompts/research-industry.md`
- Adding a new producer script under `scripts/industry/`
- Changing the JSON schema in `scripts/schemas/industry_analysis.py`
- Diagnosing a failed schema validation that mentions source tags or slug

## Enforcement map

- **Static**: `scripts/audit_fail_open.py` does NOT yet have an industry-
  specific pattern (Phase 2 candidate; tracked in `rules/research-industry.md` §6).
- **Runtime**: `validate_industry_analysis()` in schemas, `normalize_slug.py` /
  `decide_tier.py` / `sector_etf_map.py` in producers.
- **Tests**: `tests/test_schemas_industry_analysis.py` +
  `tests/test_research_industry_orchestration.py`.

Full policy + per-rule rationale at `rules/research-industry.md`.
