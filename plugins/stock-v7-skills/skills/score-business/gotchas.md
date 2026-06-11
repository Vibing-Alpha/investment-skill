# Score Business — Known Gotchas

Accumulated failure patterns. Update as new issues emerge.

> **Scoring vs operational split.** Several entries below double as scoring-time
> calibration rules (which number the dimension agent should *score on*). Those
> agents don't read this file — so the scoring action for each now lives in
> `prompts/references/scoring-calibration.md`, which the dimension prompts read
> before scoring. This file stays canonical for the *data detail*; the
> calibration file is canonical for the *scoring action*. The cross-referenced
> entries are tagged `→ Scoring action:` below — keep the two in sync (update
> the action there, the data detail here).

## Environment — Cowork mount quirks (Windows host + Linux sandbox)

In Cowork the project folder is a Windows host directory mounted into the Linux
sandbox via virtiofs/FUSE. That mount layer has three failure modes that **no
`scripts/` change can fix** — root cause is the mount, not the skill. They
affect **every skill that creates `reports/<TICKER>/<DATE>/`**, not just
score-business; this is the canonical write-up, and `scripts/delta/resolver.py`
also prints the phantom recipe inline at the point of failure. (feedback
2026-06-12)

### Phantom directory — `stat`-visible but `mkdir`-fails

**Symptom.** `python3 -m scripts.delta.resolver allocate-bq-run --ticker MU`
dies with `FATAL: cannot create report dir reports/MU/<DATE>: FileNotFoundError`.
Inspect `reports/MU`: `stat` hits the inode, but `ls reports/` doesn't list it,
`cd`/`mkdir -p` → "No such file or directory", `rm`/`rmdir` → "Operation not
permitted". It's a stale virtiofs/FUSE **orphan dentry** — the mount's dentry
cache drifted from the host. The sandbox cannot self-heal it (the same
corruption once hit `reports/monitor`).

**This is not a skill bug, and fail-closed is correct.** Do NOT redirect the
run to `/tmp`/`$HOME` — they are ephemeral in Cowork, so the analysis evaporates
at session end and the delta layer can never find it (rule #9 in
`.claude/rules/skill-architecture.md`). The resolver's exit-2 + refusal to
relocate is the right behavior.

**Verified fix (2026-06-12).** Re-materialise the directory from the Windows
**host** side, bypassing the broken Linux mount: use the harness **Write tool
with a Windows path** to write any small file into the phantom dir, e.g.
`reports\MU\.writetest`. That forces the host to re-create the entry and
refreshes the mount cache; afterwards `mkdir` etc. work normally in the sandbox.
Then re-run the skill.

### Delete blocked

`rm`/`rmdir` on a mount-corrupted entry → "Operation not permitted" (EPERM),
even as owner. Don't script cleanup that assumes deletes succeed — surface the
failure rather than retry-looping. A stuck entry usually clears after the
host-side re-materialise above, or in a fresh session that remounts the folder.

### In-place overwrite can truncate

Overwriting an existing file in place on this mount can truncate the result to
the **old** file's byte length (new content longer than old → silently cut off).
So don't *assume* an in-place rewrite grows a file: for content that can grow —
analysis artifacts, `summary.md`, `portfolio-state.yaml` edits — write to a
**fresh path** (or have the harness Write the file from the host side, which
bypasses the mount path) rather than patching in place, and **verify the byte
length after writing** when it matters. If an artifact looks oddly truncated to
a round or prior length, suspect this mount quirk, not a producer bug.

## Data Handling

### ADR Stocks
ADR per-share metrics are unreliable — full handling defined in
`prompts/score-fundamental.md` §ADR Stock Handling.

### API Array Order — depends on array TYPE, not data source

Order depends on which array you're reading, not whether the data
came from Financial Datasets API or yfinance fallback:

| Array | Order | Access recent |
|-------|-------|---------------|
| `historical.daily` / `historical.weekly` (price bars) | **OLDEST-first** | `[-N:]` |
| `income_statements` / `balance_sheets` / `cash_flows` (financial statements) | **NEWEST-first** | `[0]` for latest, `[:N]` for TTM |
| `segmented_revenues` | varies — check `report_period` |
| `analyst_estimates.estimates` | usually sorted by `period` — check |

**The key gotcha**: the statement arrays are NEWEST-first in both
FDS and yfinance paths. Do NOT use `array[-N:]` for them — that
returns the OLDEST N rows, which will silently break TTM / YoY math.

Always verify by checking `report_period` on `[0]` vs `[-1]` before
computing anything:
```python
rows = data["income_statements"]
assert rows[0]["report_period"] > rows[-1]["report_period"], \
    "expected newest-first for statements"
latest = rows[0]             # most recent quarter
ttm_rows = rows[:4]          # trailing 4 quarters
yoy_comp = rows[4]           # same quarter prior year
```

### Price Data Top-Level Shape (01_price_data.json)
`01_price_data.json` is a **dict**, not a list. Shape:
```
{
  "snapshot": {price, market_cap, week_52_high/low, ...},   # current tick
  "historical": {
    "daily":  [bars...],   # oldest-first, each bar = {time, open, high, low, close, volume}
    "weekly": [bars...],   # oldest-first
    "daily_count": int, "weekly_count": int,
    "sma_20": float, "sma_50": float
  },
  "beta": float
}
```
Access bars as `price_data["historical"]["daily"][-N:]`. Do NOT write
`price_data["historical"][0]` (KeyError — historical is a dict) or
`price_data[0]` (TypeError — top level is dict).

### Financial Data Top-Level Shape (02_financial_data.json)
Dict with keys `metrics_snapshot` / `income_statements` / `balance_sheets`
/ `cash_flows` / `segmented_revenues`. The three `*_statements` values
are **NEWEST-first** lists (consistent with the array-order table above —
verified empirically on FDS + yfinance paths). Use `[0]` for the most
recent quarter, `[:4]` for TTM, `[4]` for the same quarter prior year.
Do NOT use `[-4:]` for TTM — that grabs the OLDEST four rows and silently
breaks TTM / YoY math. Always confirm with the `report_period` assertion
shown in the array-order section above before computing.

### Field Name Drift
API field names vary across versions. Common traps:
- Cash flow: `cash_flows` vs `cash_flow_statements` (check actual response)
- Revenue estimates: `revenue` vs `revenue_estimate`
- Always verify field names against actual API response before using

### Cash-flow rows may be YTD-cumulative; `free_cash_flow` field can be corrupted
On some yfinance-fallback feeds the cash-flow rows are **YTD-cumulative**
(monotonic within a fiscal year, reset each FY), not discrete quarters — a naive
`cash_flows[:4]` TTM then double-counts. Separately, the `free_cash_flow` field
is occasionally corrupted (large negative while OCF is strongly positive and
capex small) → re-derive `FCF = OCF − capex` and cross-check `metrics_snapshot`
FCF/share. `extract_fcf`'s dual `api_fcf` vs `ocf_minus_capex` path already
handles the corrupted-FCF case (and treats trailing-null FCF as a YTD-in-progress
signal) for its OWN output — but it does NOT de-cumulate YTD rows. So for a
by-hand statement read, only de-cumulate (`Qn = YTDn − YTD(n-1)`) when the period
basis is explicit (cite it); otherwise prefer `extract_fcf` / filed quarterly FCF
over slicing raw rows.

### Division by Zero
Guard ALL division operations. Common zero-denominator scenarios:
- `week_52_high - week_52_low` (penny stocks, recent IPOs)
- Revenue in margin calculations (pre-revenue companies)
- Shares outstanding (data gap)

### Percentage Unit Confusion
Some API fields return 0-100 (e.g., `45.2` = 45.2%), others return
decimals (e.g., `0.452`). Check if value > 1 (likely percentage) or
< 1 (likely decimal) before using in calculations.

### Metric Definition Ambiguity — `debt_to_equity` (total-liabilities basis)
> → Scoring action: `prompts/references/scoring-calibration.md` §Fundamental 1.

`metrics_snapshot.debt_to_equity` is computed on a **total-liabilities**
basis, NOT interest-bearing debt. The two diverge widely for companies
carrying large non-debt liabilities (deferred tax, pensions, payables).

VSH (2026-04-04) example:
- `metrics_snapshot.debt_to_equity` = **1.028** ≈ `total_liabilities / shareholders_equity` (2186.7M / 2075.9M = 1.053)
- but `total_debt / shareholders_equity` = **0.474** (983.1M / 2075.9M) — the interest-bearing leverage

The snapshot value is internally consistent (it is not corrupted) — it
just answers a different question. Which basis to score on (and the same
caution for `interest_coverage`) is the scoring action cross-referenced above.

**ADR dual-source debt artifact:** for ADRs, `total_debt` (FDS, FX-precise) and
`current_debt + non_current_debt` (yfinance, often round-to-the-million) can
disagree ~6x. All three ARE in the DL3c converted-field set (FX-handled, not
outside repair scope — repair converts a field when the row classifies it native,
leaves it when already USD); the ~6x gap is nonetheless a dual-SOURCE conflict
(FDS vs yfinance, same currency) that FX repair does not reconcile. Prefer the
FX-precise `total_debt` for EV / leverage.

### Negative / Sign-Flipped `property_plant_and_equipment` (upstream anomaly)
> → Scoring action: `prompts/references/scoring-calibration.md` §Fundamental 2.

Some upstream feeds return `balance_sheets[0].property_plant_and_equipment`
with a flipped sign — a large NEGATIVE value whose magnitude is otherwise
correct. PP&E is an asset and is never legitimately negative.

INTC (2026-05-22) example: `property_plant_and_equipment` = **-$107.9B**
(true net PP&E ≈ +$108B), while `total_assets` (+$205B) was independently
correct. Observed ALONGSIDE this, the API `metrics_snapshot` returned
`return_on_equity` / `return_on_assets` / `return_on_invested_capital` /
`asset_turnover` all **null**. These snapshot ratios are fetched directly
from the provider's `/financial-metrics` endpoint — our pipeline does NOT
recompute them from PP&E, so the nulls are the PROVIDER's (most likely the
same bad source record), not our code reacting to a poisoned denominator.
Treat the negative PP&E and the null ratios as correlated symptoms of one
bad upstream record, not cause-and-effect inside our code.

The sign itself is an UPSTREAM data-source artifact — `scripts/normalize.py`
and the source adapters (`financial_datasets.py`, `yahoo_finance.py`) map
the field through faithfully and do NOT transform the sign (verified). Do
not add a speculative sign-correction: we cannot know the source's intent
(it could be reporting accumulated depreciation as a contra-asset).
Surfacing the provider's null ratio as-is is acceptable (we never fabricate
a value), but the negative PP&E is itself a detectable anomaly — flag it. A
producer-side warning on `property_plant_and_equipment < 0` would align with
the fail-closed philosophy in `.claude/rules/producer-consumer.md` #4; not added
here, tracked as optional hardening.

How to score in the presence of this anomaly is the scoring action
cross-referenced above.

## Scoring Edge Cases

### Pre-Profit Companies
See `prompts/score-fundamental.md` §Profitability for adapted scoring.
See `prompts/references/growth-stock-analysis.md` for full methodology.

### Companies Without Guidance
See `prompts/score-forward.md` §Guidance Quality for handling.

### Multi-Industry Companies
See `prompts/score-industry.md` §Industry Scoping for handling.

### Headline GAAP net income distorted by a DISCRETE one-time item
A single quarter can carry a **disclosed, material, non-recurring** item — a
non-cash goodwill impairment, a one-time discrete tax charge, a
contingent-consideration revaluation, or large non-operating stake income — that
makes the TTM GAAP bottom line and the snapshot `pe_ratio` / ROE derived from it
unrepresentative (same poisoned-ratio class as the negative-PP&E note above, and
distinct from cyclical normalization §Fundamental 4 / amortization §Fundamental 3
in `prompts/references/scoring-calibration.md`). Data action: surface it as a
synthesis caveat — flag the snapshot P/E / ROE / PEG as distorted, don't cite them
as a quality signal — only when the item is filed, quantified, and genuinely
non-recurring (don't back out ordinary operating weakness). Note: the dimension
scoring agents read `scoring-calibration.md`, not this file, and it has no
discrete-one-time normalization rule yet — so this stays a synthesis-level data
caveat, not a scoring action.

## Cross-Dimension

### Fix One, Check All
When correcting any formula, field name, or scoring logic in one prompt,
grep ALL prompt files for the same pattern.

## WebSearch Reliability

### Stale Data
Market share, TAM, penetration rates from WebSearch are often 6-18 months
old. Note publication date, flag data older than 12 months as `[stale]`.

### Current Year Queries
WebSearch queries must use current year context, never hardcoded dates.

### FDS statement lag + API tier/issuer starvation → WebSearch is the cross-check
The FDS financials window can lag the latest reported quarter (a just-reported
quarter may not be in the window yet), so the fundamental dimension may score a
stale window while the company has already reported — surface the timing mismatch
as a synthesis caveat and have forward / industry pull the latest print via
current-year WebSearch. Separately, FDS returns HTTP 402 (tier coverage) or 404
(foreign ADRs) on `news / insider / analyst_estimates / earnings /
segmented_revenues` for whole cohorts; expected, not a fetch bug — fall back to
WebSearch. Tag by SOURCE CHANNEL (`.claude/rules/anti-hallucination.md`): a value
read from a fetched API category file is `[API: <category>]`; a value found only
via web search is `[WebSearch: <source>]`. A failed/empty feed is `unknown`,
never `neutral` (`.claude/rules/producer-consumer.md` #4).

## Downstream Compatibility

### GAAP vs Non-GAAP EPS Divergence
> → Scoring action: `prompts/references/scoring-calibration.md` §Fundamental 3.

Companies with large intangible amortization (e.g., AMD post-Xilinx) show
GAAP EPS far below non-GAAP. When GAAP/non-GAAP < 0.7, downstream valuation
must choose which basis for P/E comparisons, so the divergence needs
surfacing (scoring action cross-referenced above).

### Partial Trading Day Volume
When data is fetched during market hours, the last bar has partial volume
(e.g., 5M vs typical 30M). This distorts volume indicators computed by
`scripts/indicators.py`. The indicators are still correct for the data
given — but downstream technical analysis should check if the last bar's
volume is anomalously low relative to the 20-day average.

### Cyclical Stocks — Peak Metrics Misleading
> → Scoring action: `prompts/references/scoring-calibration.md` §Fundamental 4.

For cyclical companies (memory, commodities, industrials), trailing metrics
at cycle peaks (or troughs) are misleading; downstream valuation should apply
normalized rather than trailing multiples (scoring action cross-referenced above).

## Delta Era

### `run_meta.json` is the delta contract — don't hand-edit
If `run_meta.json` is missing from a prior date dir, the resolver
treats that dir as pre-delta (skipped). Do NOT manually delete it to
"reset" a run — that just forces the next run into full tier without
explaining why. If you want a clean rerun, delete the entire date dir
and rerun the skill.

### Component provenance is informational, not load-bearing
`meta.component_provenance` in `bq_analysis.json` records which dims
were fresh vs copied from prior. Downstream consumers read the
dimension scores themselves, not this field. If it looks stale after
a no_op run, that's expected — the dim score file was copied from
prior, and provenance records that fact.

### `summary.changelog.md` grows unboundedly
No automatic rollup yet. If the changelog exceeds ~2000 lines (roughly
a year of daily runs), consider manually truncating older entries. The
canonical artifact is still `bq_analysis.json`; the changelog is a
human-facing audit trail.

## extract_fcf — dual-path auto-selection (2026-04-19)

`scripts/extract_fcf.py` now emits three additional fields in
`fcf_inputs.json`:

- `fcf_source`: which TTM path was used — `"api_fcf"` |
  `"ocf_minus_capex"` | `null`. Null means no valid TTM was computable.
- `fcf_selection_reason`: one of 7 string enums
  (`low_divergence_default`, `single_path_only`, `ni_sign_anchor`,
  `fallback_min_abs`, `both_opposite_sign_null`, `both_invalid_null`,
  `shares_unavailable`). The first 6 are state-machine terminal states;
  the 7th fires when the state machine picked a valid TTM but Stage 4
  couldn't divide by shares (balance sheet missing or outstanding_shares
  non-positive). Always populated — audit every decision.
- `fcf_divergence_pct`: `|TTM_api − TTM_calc| / max * 100`. `null` on
  single-path or both-invalid cases.

These come with `[Calc:...]` source tags on sibling `*_tag` fields
(anti-hallucination.md compliance, for the new fields only — see
pre-existing source-tag gap below).

### Delta-era policy

`fcf_inputs.json` is **never copied** across runs by the delta layer.
Every investment-thesis run regenerates it (~100 ms, no API cost).
This guarantees consumers always see the new schema and avoids the
"prior run lacks new fields → silent provenance regression" class of
bugs.

If you are adding a new file to `scripts/delta/copy_data.py`'s copy
list, do NOT include `fcf_inputs.json`.

### Pre-existing source-tag gap (out of scope, not a new bug)

The existing fields `fcf_per_share`, `ttm_fcf`, `discount_rate`,
`discount_rate_components.*` predate the anti-hallucination rule
enforcement and do NOT carry `[Calc:...]` tags. The 2026-04-19 dual-
path refactor only tags the three newly-added fields. Retroactive
tagging of the pre-existing fields is a separate task — tracked but
not scheduled.

### Known limitations (accepted trade-offs)

1. **Window selection is biased by `free_cash_flow` null.** Trailing-
   null rows are dropped for both paths — if a row has ocf/capex
   populated but api_fcf null, calc path loses that row too. Why
   accepted: in practice, null fcf signals YTD-in-progress where
   ocf/capex on the same row are typically YTD-contaminated anyway.
   A per-path YTD detector is the rejected Option-A heuristic.

2. **20% divergence threshold is a hard cliff.** A ticker whose paths
   oscillate around ~20% divergence could flip `fcf_source` across
   runs. Why accepted: hysteresis requires cross-run state (violates
   stateless-script convention) or a grey-zone buffer (rejected as
   over-engineering).

3. **"All 4 NI signs equal" is strict for turnaround companies.** A
   company crossing profit/loss within the 4-quarter window sees
   `ni_anchor_usable = False` and falls back to `fallback_min_abs`.
   Why accepted: simpler rule is easier to reason about. Upgrade to a
   quality-weighted soft anchor only if gotchas logs show ≥3 tickers
   being misclassified by this rule.

### extract_fcf — DL4 fail-close on non-consecutive quarters (shipped 2026-05-17)

Post-DL4: when the 4 trailing quarters from the income/cash_flow/balance
intersection are non-consecutive (or any of the 6 `FailureKind` variants
fires), `extract_fcf_inputs` emits:

  - `fcf_per_share = null`
  - `fcf_selection_reason = "insufficient_quarters_for_aligned_window"`
  - structured error entry citing `failure_kind` + `dropped_rows`

The pre-DL4 `_check_quarter_continuity` helper (which emitted a warning
and still computed a TTM from whatever 4 rows were selected) was removed
in favor of the structured fail-close path through
`scripts.schemas.quarter_window.aligned_quarters(*, ticker=)`.

Downstream valuation prompt already gates on `fcf_per_share is None`.
Operators investigating AAOI / CSCO / VELO / VSH should expect this
output. Older 10-Q ingest gap is the typical root cause —
`/score-business` re-run after the missing quarter publishes will close
the gap.
