# Portfolio — Known Failure Patterns

## Delta Era

### Staleness classifier is cheap-read, not a probe
`scripts.delta.portfolio_classify` uses days-since heuristics (BQ ≥14
days → stale_bq, thesis >7 days → stale_thesis). It does NOT run a
real probe or fetch data. The actual probe happens only when the user
approves a cascade — that's where `decide_bq_tier` runs with live
signals.

### Alphabetical sequential order is deliberate
Step 3.5 batch refresh runs cascades in alphabetical ticker order,
sequentially (not parallel). Reasons:
- Predictable log output for debugging
- Easier to interrupt partway without racing state
- API quota is per-second, parallel invocations hit rate limits
Do NOT parallelize without measuring the quota impact.

### `[s] skip stale` note must go into decisions.md
When the user picks `[s]` skip stale, the orchestration MUST record
which tickers were stale + skipped in the decisions log, so audits
can see that decisions were made on stale data.
