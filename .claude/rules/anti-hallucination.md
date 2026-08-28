---
globs: ["prompts/**", "reports/**"]
---

## Anti-Hallucination Rules

Every number in analysis output must carry a source tag:

- `[API: field_name]` — read from a file in the run directory: a fetched
  data-API category file (`02_financial_data`, `07_earnings`, …) or a
  pipeline-computed artifact (`indicators.json`, `fcf_inputs.json`,
  `reverse_dcf.json`, sub-analysis JSONs) — `[API: <file>, <field>]`
- `[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]` — from web search
  (BOUND form: outlet + actual page url + access date; see below)
- `[Filing: 10-K/10-Q]` — from SEC filing
- `[Calc: formula]` — derived by calculation (show the formula)

These four KINDs are the ONLY canonical ones — the loader/assembler
fail-closes on any other (e.g. `[News:]`, `[Insider:]`, `[Estimates:]`,
`[Earnings:]`). Tag by SOURCE CHANNEL, not content type: data pulled from
a fetched API category file — or read from any pipeline-produced artifact
in the run directory — is `[API: <file>, <field>]`, regardless of what the
content is; `[Calc:]` is reserved for computation the writer performed —
arithmetic or an explicit comparison — with the formula/comparison shown. So a contract-win or partnership catalyst read from
`03_company_news.json` is `[API: 03_company_news, <outlet/headline>]`
(or a bound `[WebSearch: ...]` if you found it via web search) — NEVER
`[News: ...]`. Likewise insider/estimates/earnings → `[API: 04_insider_data]`
/ `[API: 06_analyst_estimates]` / `[API: 07_earnings]`.

**One KIND per bracket — never nest.** Each `[...]` carries exactly one
source KIND and one source. A citation packed inside another kind's brackets
(`[Filing: 10-Q; WebSearch: outlet, url, accessed ...]`,
`[Calc: a / b = c, from WebSearch: ...]`) reports the OUTER kind to the
validator, so the inner citation escapes every binding check. Emit them as
adjacent tags instead: `[Filing: 10-Q] [WebSearch: outlet, url, accessed ...]`.
Marked artifacts fail closed on a nested WebSearch token
(`scripts/schemas/source_tag.py`).

Content without a source tag is invalid. No source = does not exist.

Scope: these tag rules bind the JSON analysis artifacts. Human-readable
renderings of already-tagged JSON (`summary.md`, report markdown) cite the
data without re-emitting tags — the tags live in the JSON they render.

Additional constraints:
- Catalyst dates must come from API or WebSearch, never model memory
- Data older than 7 days requires a freshness warning
- WebSearch queries must use current year, never hardcoded dates
- Tables must include interpretation (not just raw numbers)
- Past events (date < today) must be excluded from catalyst calendars

## WebSearch binding + host preflight

A bare `[WebSearch: outlet]` is a tag SHAPE the model can emit from
memory. Two rules close that hole:

1. **Bound tag form (canonical).** Every WebSearch-sourced claim binds
   outlet + url + access-date:
   `[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]` — the url is the
   actual page consulted (http/https, no whitespace; percent-encode any
   comma in it; publication vintage goes inside the outlet slot, no
   comma), the access date is the run date. Multiple sources → multiple
   tags. Enforcement is deterministic: fresh-run artifacts carry the root
   marker `_websearch_binding_version: 1` (stamped by `scripts/assemble.py`
   full-tier, `scripts.thesis.stamp_thesis_meta` / `stamp_events_meta`,
   and the research-industry SKILL); marked artifacts are
   strict-validated at load by `scripts/schemas/source_tag.py`
   (`check_websearch_binding`) — unbound tags fail-close. Legacy
   (unmarked) artifacts keep the old rule.
2. **Host preflight (fail-closed).** Prompts whose methodology REQUIRES
   current external info (score-forward, score-industry, evaluate-events,
   research-industry) must execute ONE real WebSearch tool call before
   any analysis content. Host lacks the tool / call errors → report
   exactly `cannot complete: host lacks WebSearch` and STOP — never fall
   back to model memory, never emit a `[WebSearch: ...]` tag without a
   real search result behind it.

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
