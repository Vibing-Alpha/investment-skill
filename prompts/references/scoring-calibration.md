# Scoring Calibration — metric-interpretation rules for the dimension agents

Read this before assigning sub-scores. It carries the rules where a
plausible-but-wrong reading of a number distorts the score.

Its load-bearing rule is **§4 (cyclical normalization)**. A 30-run frozen-data A/B
showed it correctly pulls a cyclical's fundamental score DOWN at a margin peak
(MU ≈ −0.6) and UP at a trough (VSH ≈ +0.45) — i.e. toward through-cycle earning
power, away from whatever the trailing quarter happens to show. That matters most
here because this system feeds cyclical semis (MU, VSH, MRVL, SNDK) into a
momentum-rotation portfolio, where certifying a peak-cycle name as high-quality
invites buying the top.

§1 (leverage basis), §2 (poisoned ratios), and §3 (GAAP/non-GAAP) document real,
recurring traps — but the same A/B found a careful agent usually self-corrects them
unprompted (every baseline run already scored leverage on interest-bearing D/E and
ignored null ratios). Keep them as guardrails for the cases — or weaker runs — that
don't, not as the file's main value.

These are calibration rules (*what score to assign given a correct number*); they
complement — not replace — the data-reading correctness already inline in each
prompt (currency-mixed rows) and the operational data-handling notes in
`.claude/skills/score-business/gotchas.md` (array order, field-name drift,
division-by-zero, percentage units). When a rule below needs the underlying data
detail, that file is the canonical source; this file is canonical for the scoring
action.

## Fundamental dimension

### 1. Leverage basis — score on interest-bearing D/E, not the snapshot label

`metrics_snapshot.debt_to_equity` is computed on a **total-liabilities** basis
(`total_liabilities / shareholders_equity`), not interest-bearing debt. For a
company carrying large non-debt liabilities (deferred tax, pensions, payables,
operating-lease liabilities) the two diverge widely — and anchoring the
**Balance Sheet** sub-score on the total-liabilities figure mis-reads solvency by
roughly a full point.

Real case: VSH had `metrics_snapshot.debt_to_equity` ≈ 1.03 (total-liabilities
basis) but interest-bearing `total_debt / shareholders_equity` ≈ 0.47. VRT
showed the same pattern (snapshot ≈ 2.16 vs interest-bearing ≈ 0.69). A
balance-sheet score built on the snapshot lands ~1 point lower than one built on
the interest-bearing ratio — a recurring, avoidable drift.

**Action:** for solvency / refinancing-risk judgments, score on the
interest-bearing ratio `total_debt / shareholders_equity`, computed from
`balance_sheets[0]` — do not anchor on the `metrics_snapshot.debt_to_equity`
label. State which basis you used in the evidence. Use the total-liabilities
ratio only when that is your explicit intent (e.g. a near-term-claims view), and
say so. The same lag caution applies to `interest_coverage` — recompute from the
latest statement rather than trusting a possibly-stale snapshot.

### 2. Poisoned capital-efficiency ratios — don't score off null / anomalous returns

A single bad upstream record can flip `balance_sheets[0].property_plant_and_equipment`
to a large **negative** value (PP&E is an asset, never legitimately negative) and,
on the same record, null out the provider's `return_on_equity` /
`return_on_assets` / `return_on_invested_capital` / `asset_turnover`. These are
correlated symptoms of one bad record, not cause-and-effect in our pipeline.

**Action:** when you see negative PP&E or null ROE/ROA/ROIC/asset_turnover, do
NOT trust or cite the API-derived capital-efficiency ratios in the
**Profitability / capital-efficiency** sub-score. Compute leverage from
`total_debt / shareholders_equity` (sign-unaffected by the PP&E anomaly). Flag
the anomaly in `red_flags` / evidence — surfacing it as-is is correct; never
fabricate a replacement ratio. A re-run after the feed corrects may close it.

### 3. GAAP vs non-GAAP EPS divergence — score the right earnings base, show both

Companies with heavy intangible amortization (typically post-acquisition, e.g.
AMD after Xilinx) report GAAP EPS far below non-GAAP. Scoring earnings power off
one basis silently — without noting the gap — both mis-scores and strands the
choice for downstream valuation.

**Action:** when GAAP / non-GAAP EPS < ~0.7, include BOTH figures in the
**Profitability** evidence, state which basis your earnings-power score uses and
why, and flag the divergence so downstream P/E comparison can pick a consistent
basis.

### 4. Cyclical peaks — score normalized earning power, not the trailing peak

For cyclical businesses (memory, commodities, industrials, shipping), trailing
margins / ROE at a cycle peak are unsustainably high. Rewarding peak trailing
metrics as if they were structural overstates BQ and reverses next cycle.

**Action:** when margins or returns are at a multi-year high (or a trough) for a
cyclical name, score with explicit cycle context — credit the through-cycle /
normalized earning power, not the trailing peak (and don't over-penalize a trough)
— and say so in the evidence so downstream valuation applies normalized rather than
trailing multiples. **Do not invent a normalized figure**: derive it from the
historical statement rows / filings / peer data and tag it `[Calc: ...]`, or score
qualitatively from the cycle pattern — never fabricate a through-cycle margin
(anti-hallucination still binds; see `.claude/rules/anti-hallucination.md`). This
also informs the **Forward** trajectory read (is the trend secular or just
late-cycle?) and the **Industry** read (secular vs cyclical demand drivers).

## Forward & Industry dimensions

The cyclical-peak rule (Fundamental §4) applies here too: distinguish secular
trajectory from late-cycle tailwinds before scoring forward direction or
industry-growth durability. A demand inflection that is really a cycle peak is
not a durable forward catalyst.

When a non-USD / ADR name forces you to read raw statement rows, the
currency-mixed-rows handling inline in each prompt governs *which numbers are
trustworthy*; this file governs *how to score them once correct*.
