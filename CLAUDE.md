# Stock Analysis System v7

US-stock investment-analysis skills. Three-layer pipeline:
business-quality (BQ) analysis → investment thesis (valuation / technical /
events) → portfolio decisions. Runs on Claude Code, Cowork, Codex, Cursor, and
OpenCode.

> This is the end-user guide. Run your agent **from the repo root** — the skills
> use repo-relative paths (`python3 -m scripts.fetch`, `Read prompts/...`).

## Setup (once)

```bash
python3 -m pip install -r requirements.txt     # yfinance + PyYAML
make setup                                      # or: python3 -m scripts.distribute bootstrap
```

`make setup` guides you through:
- `.env` — two required data-API keys: `FINANCIAL_DATASETS_API_KEY`
  (financialdatasets.ai) and `FMP_API_KEY` (financialmodelingprep.com);
  `FINNHUB_API_KEY` (finnhub.io) optional.
- `strategy.yaml` — your investment preferences (from `strategy.example.yaml`).
- `portfolio-state.yaml` — your holdings + watchlist (from the example).

Your `.env` / `strategy.yaml` / `portfolio-state.yaml` are personal and
gitignored — never shared.

## Commands

| Command | What it does |
|---------|--------------|
| `/score-business TICKER` | Business-quality analysis (fundamental / forward / industry) |
| `/investment-thesis TICKER` | Valuation + technical timing + event catalysts + thesis (needs a prior `/score-business`) |
| `/portfolio` | Whole-portfolio review + buy/sell/hold + IBKR orders (needs `portfolio-state.yaml`) |
| `/screen-stocks` | Discover tickers by price action / sector / watchlist |
| `/research-industry` | Surface 5–12 candidate tickers in a sector |
| `/monitor` | Daily triage: what on your holdings/watchlist needs attention (routes to the right skill; never trades) |
| `/write-report TICKER` | Turn an analysis into a readable Markdown report |
| `/generative-ui` | Turn an analysis into a standalone HTML dashboard |

## How it works

```
/score-business TICKER  → fetch data → indicators → 3 scoring agents → bq_analysis.json
/investment-thesis TICKER (needs bq_analysis.json)
                        → valuation scripts → analysis agents → investment_thesis.json
/portfolio (needs the above per ticker)
                        → macro + constraints → decisions + orders
```

- **Analysis logic** lives in `prompts/` (methodology), **constraints** in
  `rules/` + `.claude/rules/`, **computation** in `scripts/` (Python). Skills in
  `.claude/skills/` (Claude Code / Cowork) and `.agents/skills/` (Codex / Cursor
  / OpenCode) just orchestrate.
- **Outputs** go under `reports/{TICKER}/{YYYYMMDD}/` (gitignored): per-dimension
  scores, `bq_analysis.json`, `investment_thesis.json`, a human `summary.md`, and
  an append-only `summary.changelog.md`. Runs are incremental — a re-run reuses
  prior work and only recomputes what changed.

## What to expect from the output

These constraints are enforced automatically (full detail in `.claude/rules/`,
which your agent loads); they're worth knowing because they shape what you get:

- **Every number is sourced** — analysis carries `[API:…]` / `[WebSearch:…]` /
  `[Filing:…]` / `[Calc:…]` tags. No source = it doesn't appear
  (`.claude/rules/anti-hallucination.md`).
- **Units are explicit** — currency / percent-vs-decimal / per-share, with FX
  handled for non-USD ADRs (`.claude/rules/units.md`).
- **Portfolio decisions respect hard limits** — position / sector / cash floors,
  fail-closed on missing data (`.claude/rules/portfolio-safety.md`).
- **Language** — human-facing reports use `output_language` in `strategy.yaml`
  (any language, e.g. `zh-CN` / `en-US`); the JSON analysis is always English.

## Staying up to date

```bash
python3 -m scripts.update check     # is a newer release out? (also runs on session start)
python3 -m scripts.update apply     # fast-forward to it + show the changelog
```

Updating is opt-in and never overwrites your local edits. See `CHANGELOG.md` for
what each release changed.

**Cowork (plugin install) — an update has TWO halves**, and the skills warn you
when they drift apart (version-skew warning):
1. **The clone** (scripts + prompts in your project folder): `update apply` as
   above, or just ask the agent to update it.
2. **The plugin** (the skill bodies Cowork runs): plugin UI → the marketplace
   entry → enable **auto-update** (first time it prompts you to install the
   **Claude GitHub App** — install it) → refresh the marketplace → press the
   plugin's **Update** button. The button stays greyed out until the
   marketplace itself has refreshed.

## Dependencies

- Python 3.10+, `yfinance` + `PyYAML` (`requirements.txt`). HTTP via stdlib.
