# Investment Thesis — Known Failure Patterns

## Data Issues

### Partial trading day volume
When data is fetched during market hours, the last bar has partial volume
(e.g., 5M vs typical 30M). This distorts volume indicators (volume_ratio,
price_volume_relationship) and can make the technical agent incorrectly
flag "declining volume." Check if the last bar's volume is <50% of the
20-day average — if so, note it as partial-day data.

### Price staleness
BQ data may be days old when thesis is run. For volatile stocks (ATR >5%),
even one day changes ER/CE materially. If price data is >2 trading days
old, warn the user that valuation metrics may be stale.

### Cross-market peer tickers
Peers like 005930.KS (Samsung) or 000660.KS (SK Hynix) use K-IFRS, not
US GAAP. Samsung's P/E is conglomerate-level, not semiconductor-segment.
Direct multiple comparison is misleading — flag the accounting basis
difference in valuation output.

## Valuation Pitfalls

### Cyclical stocks: low trailing P/E trap
Memory, commodity, and cyclical semiconductor stocks show low trailing P/E
at cycle peaks. This is NOT cheapness — it is the market pricing in
earnings mean-reversion. Use normalized/mid-cycle earnings for P/E-based
fair value, not trailing.

### Pre-profit companies
If TTM FCF is negative, reverse DCF cannot run. The valuation agent must
fall back to P/S, EV/Revenue, and scenario-based valuation without DCF
cross-check.

### GAAP vs non-GAAP EPS divergence
Companies with large amortization (e.g., AMD post-Xilinx) show GAAP EPS
far below non-GAAP. If GAAP/non-GAAP ratio is <0.7, flag it and prefer
forward non-GAAP estimates for P/E context.

### Valuation producer fail-close — expected, not a retryable crash
`historical_multiples` and `extract_fcf` legitimately emit `status: error` for
whole classes of issuer (correct DL4/DL3c behavior, NOT a producer bug to retry);
`reverse_dcf` is then **`status: skipped`** — the orchestrator doesn't invoke it
when `fcf_inputs.json:status == error` (§3.3 caller-chain gate), and it self-skips
on invalid/null FCF. The fail-close classes:
- **Non-consecutive quarters** (any non-Dec-FYE filer whose standalone fiscal Q4
  lives only in the 10-K) → `fcf_selection_reason:
  insufficient_quarters_for_aligned_window`. The FMP fallback back-fills THIS
  cohort (and missing-fiscal-Q4) — verify the producer actually ran before
  assuming fail-close. (FMP does NOT fix the next two.)
- **Unknown ADR ratio** → `adr_ratio_correction_required`.
- **Non-USD annual-only statements** → `fx_unsupported_annual_path`.

Action: emit the skipped `reverse_dcf` stub exactly as `prompts/evaluate-valuation.md`
specifies (it gives the exact `reason` per case — `fcf_selection_reason` for the
poor/null-FCF case, the error detail for the currency-error case) — do NOT
fabricate a successful DCF or hand-build the producer artifact. With DCF /
2Y-self-history unavailable, fall back to peers + reconciled `metrics_snapshot` +
forward consensus + scenario — these usually still yield a per-share fair value,
so ER stays computable. The synthesis agent emits `expected_return: null` ONLY
when NO absolute per-share lens survives (all scenario targets null — the
un-anchorable cohort), never merely because DCF/history is unavailable; CE follows
mechanically downstream (no agent fabricates a number; valuation itself does not
emit ER/CE). For an ADR, resolve value via the home-line ticker's dimensionless
multiples × FX × ADR-ratio — never the yfinance `metrics_snapshot` market_cap alone.

### Peer-set hygiene
`prompts/evaluate-valuation.md` carries the peer-multiple rules (USD-only
`medians`, the `n == 1` "not a peer anchor — omit or cite single-source at lower
confidence" rule, cross-market accounting-basis caveat), and `scripts/peers.py`
already drops tickers it can't quote (delisted → silently absent, logged in
`errors`). The one trap NOT in those rules: **exclude the company's own supplier
or customer from the peer median** — a supplier carrying a much higher gross
margin drags the median up and makes the target look falsely cheap. Verify the
surviving peer count is still meaningful (n ≥ 3) after exclusions.

## Technical Analysis

### MA200 approximation
indicators.py computes from 6 months of daily data — not enough for true
MA200. The technical agent approximates from weekly data. Note this as
approximate in the output.

## Alpha Discovery

### Jevons Paradox in efficiency technologies
When a company publishes memory/compute efficiency technology (e.g.,
Google TurboQuant) AND simultaneously increases hardware procurement,
the efficiency gain is being absorbed by demand growth. The market may
overreact to the efficiency announcement — check the publisher's own
procurement behavior as the more reliable signal.

## Delta Era

### `events.json` meta block is Gate 4 load-bearing
If the events agent doesn't emit `meta.output_version == "8.0"`, Gate
4 fails on every rerun and events is never reused. The agent prompt
at `prompts/evaluate-events.md` spells out the exact required shape —
don't drift from it.

### `meta.generated_at` is provenance, NOT in the rewrite allow-list
Every reuse preserves `meta.generated_at` verbatim as the anchor for
"when was this content freshly generated". The `rewrite_stale_date_fields`
helper deliberately excludes it from its allow-list. If you see a
stale-looking `generated_at` on a reused events.json, that's correct
— it's the original generation date, not today's rerun date.

### `reuse_meta.reused_from` must preserve the chain
On each reuse, copy the prior's `reuse_meta.reused_from` verbatim if
present; otherwise normalize prior's `meta.generated_at`. Do NOT reset
to "today" or "prior run's date" — that would shrink the classifier's
since_date window on each reuse and miss material news.

### Anchor extracted ONCE before mutation
`read_prior_events_run_date` must be called BEFORE
`rewrite_stale_date_fields` or any other mutation of the prior events
doc. Step 2 stores the anchor; Step 4 threads it through verbatim.
Re-deriving in Step 4 would fail on chained reuses.
