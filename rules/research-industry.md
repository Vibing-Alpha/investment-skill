# research-industry — Hard Constraints (canonical)

This file is the **canonical** policy for the `/research-industry` skill.
Platform-agnostic; the Claude Code adapter is at
`.claude/rules/research-industry.md`.

The skill's methodology lives in `prompts/research-industry.md`. This file
captures **rules** — invariants that must hold across runs regardless of
who runs the skill or which agent backend renders the analysis.

---

## 1. Candidate Ticker Universe

### 1.1 Listing requirements

A candidate ticker is acceptable iff ONE of:

| Listing | Examples | Accepted? |
|---|---|---|
| NYSE / NASDAQ / AMEX common stock | NVDA, VSH | ✅ |
| US-listed ADR (sponsored or unsponsored) on NYSE/NASDAQ | TSM, BABA | ✅ |
| **OTC ADR with active trading** | MRAAY, TTDKY | ✅ (but see §3.4) |
| Foreign-only listing (TYO, KRX, TWSE, HKEx, etc.) | 6981.T (Murata native) | ❌ |
| Delisted / acquired / private | KEM (acquired by Yageo 2020) | ❌ |
| ADR program **announced terminating** | KYOCY (terminating 2026-06-04) | ❌ |
| Pre-IPO / SPAC pre-merger | n/a | ❌ |

### 1.2 Exclusion verification

Before including any ticker, the agent must satisfy at least ONE source:

- **WebSearch** confirming current-year (≤ today) trade activity, with
  the source attached as `[WebSearch: <specific descriptor>]` on the
  candidate's `rationale` OR adjacent risk note.
- **Filing reference** to a recent (≤2 years) SEC filing.

When uncertain, **omit the candidate**. Schema accepts 1-12; padding to
a target count is a defect.

### 1.3 Exposure honesty

A candidate's `revenue_exposure_pct` measures how much of the candidate's
revenue comes from the industry being analyzed:

- **Pure-play** (>50%): primary exposure, strongest stock-picking handle
- **Significant** (15-50%): legitimate but acknowledge dilution
- **Token** (<15%): include only if no better candidate exists; mark
  market_position as "niche" and explain in rationale

If exposure is not separately disclosed in filings, mark it as `[Calc: ...]`
with the derivation, OR set both `revenue_exposure_pct` and
`exposure_source` to null. Never invent a number.

---

## 2. Source-Tag Format (hard schema requirement)

Every claim string must contain at least one source tag matching the
canonical regex from `scripts/schemas/source_tag.py:SOURCE_TAG_RE`:

```
\[(API|WebSearch|Filing|Calc)\s*:\s*<descriptor>\]
```

### Fields subject to source-tag check
- `framing.one_line_thesis`
- `framing.key_drivers[*]`
- `candidate_tickers[*].rationale`
- `risks[*]`
- `catalysts[*]`

### Descriptor specificity

The descriptor must be **specific enough that a reader could re-find the
source**. Placeholder descriptors fail the validation linter:

| Form | Verdict | Rationale |
|---|---|---|
| `[WebSearch: IDC AI Semi Forecast Q1 {CURRENT_YEAR}]` | ✅ | Vendor + topic + period |
| `[WebSearch: Bloomberg Q1 capex tracker]` | ✅ | Outlet + topic |
| `[WebSearch: report]` | ❌ | Placeholder theater |
| `[WebSearch: news]` | ❌ | Placeholder theater |
| `[WebSearch: source]` | ❌ | Placeholder theater |

The validator at `scripts/schemas/source_tag.py:PLACEHOLDER_DESCRIPTORS`
maintains the rejection list.

### Source quality tiers (informational, not enforced)

For the agent's judgment when WebSearch results conflict:

- **Tier 1** (authoritative, prefer): IDC, Gartner, McKinsey, S&P Capital
  IQ, primary SEC filings, official company IR statements
- **Tier 2** (legitimate, cite if Tier 1 unavailable): Bloomberg, Reuters,
  WSJ, FT, CNBC, Barron's, AP
- **Tier 3** (last resort, cite with caution): industry blogs, syndicated
  aggregators, SeekingAlpha, MotleyFool

---

## 3. Forecast Dispersion + Regime Edge-Cases

These rules govern how the agent handles known data-quality risks
discovered during real invocations.

### 3.1 TAM forecast dispersion

If the WebSearch returns TAM estimates from 3+ sources and the span is
**>2x** (highest / lowest), the agent MUST:

1. Pick a defensible central value (median of Tier-1/Tier-2 sources)
2. Surface the **dispersion itself** as a risk entry with the explicit
   range, e.g. `"TAM estimates span $13B-$32B across sources, indicating
   scope-definition uncertainty [WebSearch: Mordor 2026 vs Persistence 2025]"`
3. Tag `tam_source` with the chosen central source

### 3.2 CAGR forecast dispersion

Same rule, lower threshold: if forecast CAGR sources span **>3pp**
(percentage points), surface the dispersion as a risk. The skill's
schema currently stores only the mid value — prose handles the range.

### 3.3 Overextended regime sub-state

The mechanical regime classification (5d/20d/60d all positive → tailwind)
breaks down at extremes. When `trend_60d_pct ≥ +30%` or `≤ -30%`:

- Regime stays its base value (tailwind / headwind)
- `regime_rationale` MUST include the magnitude (e.g., "60d +50.7% is
  extreme — mean-reversion risk elevated")
- A mean-reversion or overbought risk entry MUST appear in `risks[]`

### 3.4 OTC ADR liquidity risk escalation

When a P1 or P2 (priority 1 or 2) candidate is an OTC ADR (not
NYSE/NASDAQ common stock):

- The candidate `rationale` should flag this briefly
- A **top-level risk entry** in `risks[]` MUST address OTC liquidity
  generically (bid-ask spread, settlement, margin eligibility for retail
  brokers)
- P3 OTC ADRs only need the candidate-level flag

### 3.5 Data vintage / time-decay

A `{CURRENT_YEAR}` search query does NOT guarantee current-year DATA — research
vendors republish TAM/CAGR forecasts roughly annually, so a fresh search
routinely surfaces a forecast published 2-3 years ago. The framing numbers
(`tam_usd_b`, `cagr_5y_pct`, lifecycle penetration %, supply-demand balance) each
carry a **vintage** — the period the underlying research is as-of — distinct from
when the agent searched.

- The source tag MUST name the data's as-of period, not just the search year:
  `"[WebSearch: IDC AI-Semi TAM, published <YYYY>-Q2]"`. The date names the DATA.
  If the source does not disclose a publication / as-of date, tag it
  `as-of not disclosed` and flag the uncertainty — never infer or invent one
  (anti-hallucination: an unseen date does not exist).
- **Validity windows** (older than these = stale): TAM / CAGR forecasts ~18
  months; market penetration / adoption rates ~12 months; supply-demand balance
  ~6 months. The faster the lifecycle (emerging / growth), the more a stale number
  misleads.
- If the only available figure exceeds its window, still use it, BUT (a) keep the
  vintage in the tag, (b) add a `risks[]` entry naming the staleness + window, and
  (c) prefer the freshest source when vintages differ.

Why a hard rule: a stale forecast passed through `tam_usd_b` looks authoritative
but mis-frames the whole thesis (and the downstream CE / sizing that keys off it).
On the first real invocations the AI-chip TAM had re-rated ~4× in ~18 months — a
forecast >2 years old pulled today is not a current-year level. This complements §3.1/3.2:
dispersion flags "experts disagree now"; vintage flags "this number is from an old
snapshot, the market has moved."

---

## 4. Slug Format

`meta.slug` must match `^[a-z0-9]+(-[a-z0-9]+)*$`.

- Lowercase ASCII letters + digits only
- Hyphen separator, no leading/trailing hyphen, no double-hyphen
- No spaces, no underscores, no Unicode

Normalize via `scripts/industry/normalize_slug.py` (not free-form agent
judgment). The script is the single source of truth for the (raw input
→ canonical slug) mapping.

---

## 5. Output Artifacts

Every successful invocation must produce exactly these four artifacts in
`reports/industry/<slug>/<YYYYMMDD>/`:

| Artifact | Purpose | Schema |
|---|---|---|
| `industry_analysis.json` | Machine output, /score-business consumer | `scripts/schemas/industry_analysis.py` |
| `summary.md` | Human brief, 300-800 words target | Markdown template in prompts/research-industry.md §"Output schema (summary.md)" |
| `summary.changelog.md` | Append-only delta log | Markdown, one section per run |
| `run_meta.json` | Delta audit state | `scripts/delta/run_meta.py:IndustrySection` |

Anything else (data/, .tier_context.json, intermediate files) is a
working artifact that may be cleaned up.

---

## 6. Currency Convention

`industry_analysis.json` is USD-quoted by contract. Specifically:

- **`framing.tam_usd_b`**: USD billions. If the source vendor reports
  in a non-USD currency (e.g., Chinese semiconductor TAM in CNY from
  CCID Consulting, Japanese passive-component TAM in JPY from
  Yano Keizai), convert at the **annual average** FX rate of the
  reported year (not spot — TAM is a year-aggregate, not a point
  estimate). Note the conversion in `framing.tam_source` using the
  `[Calc: ...]` tag pattern:

  Good: `"tam_source": "[WebSearch: CCID China Semi 2026 Report] + [Calc: CNY 2.8T → USD 386B @ 7.25 annual avg 2025]"`

  Bad: `"tam_source": "[WebSearch: CCID China Semi 2026 Report]"` (silently passes a CNY number as USD billions)

- **`framing.cagr_5y_pct`**: dimensionless percent. No conversion needed,
  but if forecasts differ between USD-denominated and local-currency
  perspectives (rare — most CAGRs are real-growth, currency-neutral),
  prefer the USD perspective and note the basis.

- **`candidate_tickers[*].revenue_exposure_pct`**: dimensionless percent.
  No conversion needed.

This rule was added 2026-05-22 after auditing the DL3c chain: bq_analysis
+ investment_thesis preserve currency_conversion certs end-to-end, but
the upstream `/research-industry` was free to silently pass non-USD
numbers through `tam_usd_b`. Now made explicit.

The downstream `/score-business` consumer reads only
`candidate_tickers[*].ticker` (currency-neutral identifiers), so this
rule is local to research-industry's human-facing outputs and prevents
the summary.md table from misrepresenting market size.

## 7. Enforcement Surface

| Check | Where | Type |
|---|---|---|
| JSON schema | `scripts/schemas/industry_analysis.py` | Runtime validator |
| Source-tag format | `scripts/schemas/source_tag.py` | Runtime validator |
| Slug normalization | `scripts/industry/normalize_slug.py` | Producer-side enforcement |
| Tier decision | `scripts/industry/decide_tier.py` | Producer-side enforcement |
| Sector ETF mapping | `scripts/industry/sector_etf_map.py` | Producer-side enforcement |
| Delta state | `scripts/delta/run_meta.py:IndustrySection` | Producer-side enforcement |
| Orchestration parity | `tests/test_research_industry_orchestration.py` | E2E test |

Add a new rule here when an invocation surfaces a recurring authoring
mistake or content-quality gap. Edit the methodology prompt in parallel
so the agent learns the rule before the validator catches it.
