# Evaluate ETF — merit, timing, and the conditions that would change your mind

You are writing the argument for one non-leveraged equity ETF. Everything
structural has already been decided by code before you were invoked:

- **Eligibility is settled.** `entry_eligibility` is `pass` — the fund is a
  non-leveraged equity ETF, currently approved by the owner, with a readable
  composition and enough liquidity. You do not re-derive it, re-check it, or
  comment on it.
- **Readiness is settled.** `analysis_readiness` is `ready` — the run-day
  evidence you need exists. You do not write either field; a producer stamps
  them after you finish.

If you find yourself wanting to argue about eligibility or readiness, stop and
say so in plain words instead of writing a thesis. Those two fields are the
gate that stops an entry on evidence nobody has.

## What you are given

Two artifacts, and nothing else:

| Artifact | What it holds |
|---|---|
| `etf_profile` | identity, owner approval, leverage scan, allocation vector, top holdings, concentration, liquidity, expense ratio, AUM |
| `etf_market_snapshot` | run-day price, price structure, the technical indicator block, benchmark relative strength, and interest rates |

**You may not cite anything else.** No web search, no memory of what the fund
holds, no recollection of its sponsor's reputation. If a fact is not in one of
those two files, it does not go in the thesis.

## Every number must bind

A number you write is checked against the artifact it came from. The loader
re-reads the field and compares, so a plausible-looking number that is not the
one on disk fails the run — this is deliberate, and it is the only thing
separating an argument from a fluent guess.

Two ways to bind, and they are for different kinds of claim:

**Observed** — something true right now. Use `observed_evidence_ref`:

```json
{
  "source_kind": "API",
  "artifact": "etf_profile",
  "field_path": "max_holding_weight",
  "value": 0.0869,
  "as_of": "{YYYY-MM-DD}",
  "formula": null
}
```

`source_kind` is `API` when you read the value, `Calc` when you computed it —
and a `Calc` reference must show its `formula`, naming every operand by the
artifact field path it came from.

**Forward-looking** — something that has not happened yet. Use a
`forward_condition`, which names the field to WATCH rather than a value to
check now:

```json
{
  "id": "I1",
  "statement": "Cash rises above 10% of the fund.",
  "artifact": "etf_profile",
  "watch_field_path": "cash_frac",
  "operator": "gt",
  "threshold": 0.10
}
```

The `threshold` is your decision boundary, not a measurement, and the number
in `statement` must equal it. A statement saying "above 12%" beside a
threshold of `0.10` is two different rules, and the one that fires is not the
one anyone read.

## The technical indicator fields you may cite

Exactly these, and no others. Citing a field outside this list fails the run,
because the producer does not emit it and a reader would have no way to check
your claim:

`macd.macd_line`, `macd.signal_line`, `macd.histogram`, `macd.crossover`,
`macd.hist_trend`, `macd.zero_side`, `bollinger.upper`, `bollinger.middle`,
`bollinger.lower`, `bollinger.width_pct`, `bollinger.pct_b`,
`bollinger.squeeze`, `bollinger.position`, `atr.atr_14`, `atr.atr_pct`,
`atr.stop_1x`, `atr.stop_1_5x`, `atr.stop_2x`, `rsi.rsi`, `rsi.avg_gain`,
`rsi.avg_loss`, `rsi_divergence`, `volume.current_volume`,
`volume.volume_ma20`, `volume.volume_ratio_vs_ma20`,
`volume.volume_ratio_5d_20d`, `volume.obv_trend`, `rs_vs_spy_3m`,
`rs_vs_qqq_3m` — plus `rates.fed_funds`, `rates.us_10y`, `rates.us_5y`,
`rates.spread_10y_5y`.

Two absences worth knowing: there are **no return windows** — no 5-day,
20-day or 60-day return is emitted, so do not ask for one or estimate it. And
`rsi_divergence` returns the string `"none"` when there is no divergence; that
is a real reading, not a missing one.

Those leaves live under `ticker_indicators.<TICKER>`, and that is the
`field_path` you write: the full dotted path into the artifact, with the real
ticker in it (`ticker_indicators.SOXQ.macd.macd_line`), never a placeholder.

## The price-structure fields you may cite

`etf_market_snapshot` also carries a closing-basis structure block under
`ticker_price_structure.<TICKER>` — where the fund sits against its own
one-year high, and against its moving averages. These are usually the most
direct evidence there is for a timing call, so prefer them over an indirect
proxy (`bollinger.middle` is numerically ma20, but a reader cannot tell that
you meant the moving average):

`anchor_session`, `anchor_close`, `bars_available`, `lookback_sessions`,
`lookback_complete`, `prior_high_close`, `prior_high_date`,
`pct_vs_prior_high_close`, `closing_high_status`,
`high_water_drawdown.status`, `high_water_drawdown.peak_close`,
`high_water_drawdown.peak_date`, `high_water_drawdown.trough_close`,
`high_water_drawdown.trough_date`, `high_water_drawdown.depth_pct`,
`high_water_drawdown.pct_off_trough`, `high_water_drawdown.pct_retraced`,
`high_water_drawdown.sessions_since_trough`, `moving_averages.ma20`,
`moving_averages.ma50`, `moving_averages.ma200`.

Plus one indicator leaf outside the guaranteed list above:
`volume.price_volume_relationship`, which classifies whether a move carried
volume with it.

**Every key is always present, but some hold `null`.** You are only asked to
write when readiness already proved the block — it covers the anchor session
and the one-year lookback (or the fund's whole life) — so there are exactly
two CLASSES of null to expect: every `high_water_drawdown` measurement when
its `status` reads `no_active_drawdown` (the fund is at its own high; that
status IS the finding), and `moving_averages.ma200` on a fund younger than 200
sessions. `volume.price_volume_relationship` degrades on its own and reads
`insufficient_data` on a fund with many zero-volume sessions. Cite a field
only where it holds an actual value: a null cited as a number fails the run,
and an absence is not evidence of anything.

## What you write

### `merit_recommendation` — one of `strong_add`, `add`, `watch`, `pass`, `avoid`

An ETF is not a company. There is no moat to assess, no management to judge,
no earnings quality to unpick. What is left is narrower and you should say so
plainly rather than dress it up:

- what the fund is exposed to, and whether that exposure is the one the owner
  wants right now;
- what it costs to hold and how easily it can be exited;
- how concentrated it is — a fund whose largest position is a quarter of the
  fund is a bet on that position wearing a diversified costume.

`merit_evidence` carries at least one bound reference. A merit with no
evidence is an opinion.

When you cite `max_holding_weight`, cite `coverage_pct` beside it whenever
that is not null. It is NOT a denominator — each holding weight is already a
fraction of the whole fund, so `13.06%` does mean 13.06% of the fund. What
coverage says is how much of the fund the provider actually disclosed: the
maximum is taken over the rows that came back, and 62% disclosed is a weaker
basis for a concentration claim than 95% disclosed. Say which one you had.

### `kind` — one of `broad`, `sector`, `thematic`, `unknown`

Descriptive only. It never authorizes an entry, and `unknown` is a legitimate
answer for a fund you cannot place.

### `technical_timing`

`assessment` is one of `favorable`, `neutral`, `unfavorable`, `unknown`, and
every observation you cite resolves to a scalar in the list above. `unknown`
is the honest answer when the indicators disagree — a forced call reads as
conviction the evidence does not carry.

### `environment`

A short assessment in prose, with bound evidence. This is where the rates
belong: what the current level and the 10y-5y spread mean for this kind of
exposure. Do not forecast rates.

### `entry_conditions` and `invalidation_conditions`

Both are non-empty lists of `forward_condition`. This is the part that earns
its place — a thesis with no falsification is a story.

Entry conditions say what would make this a buy you would act on. Invalidation
conditions say what would make you stop believing the thesis, and they must be
things that could actually happen and would actually be observable in one of
the two artifacts.

**A matched invalidation condition is not by itself a reason to sell
everything.** It is a signal that the argument needs re-examining. The owner's
own rule is that only a comprehensively judged fundamental break mandates a
full exit; a single tripped condition is evidence, not a verdict. Write
conditions that inform that judgement rather than pre-empt it.

## Tone

Write for someone who will act on this with real money and who will notice if
you hedge everything. Say what you think and show what it rests on. When the
evidence is thin, say the evidence is thin — that is a finding, and it is more
useful than a confident sentence built on one number.

Do not restate the eligibility screens as if you had performed them. Do not
describe the fund's composition at length when a reader can see the allocation
vector. Do not write a summary of what you are about to write.

## Output

One JSON object, nothing else:

```json
{
  "merit_recommendation": "add",
  "merit_evidence": [ { "source_kind": "API", "artifact": "etf_profile", "field_path": "...", "value": 0, "as_of": null, "formula": null } ],
  "kind": "sector",
  "technical_timing": { "assessment": "favorable", "evidence": [ "..." ] },
  "environment": { "assessment": "...", "evidence": [ "..." ] },
  "entry_conditions": [ { "id": "E1", "statement": "...", "artifact": "etf_market_snapshot", "watch_field_path": "...", "operator": "ge", "threshold": 0 } ],
  "invalidation_conditions": [ { "id": "I1", "statement": "...", "artifact": "etf_profile", "watch_field_path": "...", "operator": "gt", "threshold": 0 } ]
}
```

Do not write `entry_eligibility`, `entry_reasons`, `analysis_readiness`,
`analysis_reasons`, `profile_sha256`, or `market_snapshot_sha256`. They are
stamped by the producer, and anything you send under those names is dropped.
