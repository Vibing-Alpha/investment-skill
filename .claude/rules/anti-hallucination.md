---
globs: ["prompts/**", "reports/**"]
---

## Anti-Hallucination Rules

Every number in analysis output must carry a source tag:

- `[API: field_name]` — from Financial Datasets API or other data API
- `[WebSearch: source_name]` — from web search (include source name)
- `[Filing: 10-K/10-Q]` — from SEC filing
- `[Calc: formula]` — derived by calculation (show the formula)

These four KINDs are the ONLY canonical ones — the loader/assembler
fail-closes on any other (e.g. `[News:]`, `[Insider:]`, `[Estimates:]`,
`[Earnings:]`). Tag by SOURCE CHANNEL, not content type: data pulled from
a fetched API category file is `[API: <category>]`, regardless of what the
content is. So a contract-win or partnership catalyst read from
`03_company_news.json` is `[API: 03_company_news, <outlet/headline>]`
(or `[WebSearch: <outlet>]` if you found it via web search) — NEVER
`[News: ...]`. Likewise insider/estimates/earnings → `[API: 04_insider_data]`
/ `[API: 06_analyst_estimates]` / `[API: 07_earnings]`.

Content without a source tag is invalid. No source = does not exist.

Additional constraints:
- Catalyst dates must come from API or WebSearch, never model memory
- Data older than 7 days requires a freshness warning
- WebSearch queries must use current year, never hardcoded dates
- Tables must include interpretation (not just raw numbers)
- Past events (date < today) must be excluded from catalyst calendars

## Exemption: `run_meta.json`

`run_meta.json` is the delta layer's internal audit/state record,
not an analysis artifact. Its numeric fields (e.g.
`probe.material_news_count`, `probe.days_since_last_full`,
`cost.tokens`) are deterministic aggregates computed by
`scripts.delta.probe` from API outputs and prior state. They do NOT
require `[API:...]` / `[Calc:...]` source tags.

This exemption applies only to `run_meta.json`. The analysis
artifacts (`bq_analysis.json`, `investment_thesis.json`, `events.json`,
etc.) remain fully subject to the anti-hallucination rules above.
