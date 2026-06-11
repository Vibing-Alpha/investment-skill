# Changelog

Release notes for the distributed skill system. Newest first. Managed by
`scripts/release.py`; recipients see the latest entry on update.

## v1.2.1 — 2026-06-11

- Safety gate hardening: present-but-null state/price data (bare holdings:/cash:/open_orders: keys, null prices from a failed fetch, shares: null) now fails closed with structured violations instead of crashing or silently passing
- Oversell detection: a proposed sell exceeding held shares, or a SINGLE broker open order selling more than held, is now a violation (stop+limit OCA brackets remain legal); an unrecognized --orders file shape is refused instead of validating zero orders
- Decision log: write-time schema self-check — a log that tomorrow's review would reject is refused at write time; order costing uses the same est_price→limit_price→price chain as validation; open-order limit_price renders in the MD
- Preflight: the open_orders key requirement now fails fast at config_gate (was: refused only at the final write step); watchlist-only states (bare holdings:) and int-shorthand holdings (TICKER: 100) are accepted
- BQ staleness clock keys to the last FULL-tier run — frequent no-op/partial runs no longer reset the 90-day re-score ceiling
- Historical multiples: when the newest reported quarter cannot be aligned yet, summary.current is flagged (current_lags_newest_reported + warning) instead of silently presenting an older quarter as current

## v1.2.0 — 2026-06-11

- Price feed: stale Yahoo meta quotes (thin OTC ADRs) now lose to the newer chart bar in the same response; per-ticker price vintage surfaced as price_as_of/stale_meta_quote
- Regime classification: inputs anchored to the last completed ET session (regime_inputs block) — a live pre-market VIX can no longer flip risk_off to risk_on against prior-close indices
- portfolio-state.yaml: open_orders is now a REQUIRED key (write open_orders: [] to attest none) — decisions attach per-ticker open-order snapshots, warn on direction conflicts, and stress scenarios must cover working orders
- portfolio-state.yaml: optional symbol_aliases map (vendor/broker symbol split, e.g. ADR depositary renames) wired into the price fetch for /portfolio and /monitor
- Same-day /portfolio reruns archive the prior decisions pair as decisions.{run_id}.*; review now sees an earlier run today
- Decision-log hardening: refuses missing/failed stress artifacts and non-ET-day output dirs; scripts.validate exits 1 on FAIL; limit_price honored in cash projections
- Report-dir allocation failures fail visibly with do-not-redirect remediation (/tmp is ephemeral in Cowork)
- SKILL prose hardening: no bare $N literals (harness positional-arg substitution defense) + ER-lint [context-only] marker

## v1.1.0 — 2026-06-10

- Cowork thin-plugin packaging: install from this repo's marketplace, then run /stock-v7-setup (clone-launcher: persistent clone + venv in your project folder)
- All 8 skills hardened for Cowork fresh-shell execution (per-step root resolve, state rehydration, venv-aware $PYBIN)
- Money-path config gate: graded single-root guard (portfolio reads block on wrong/unconfirmed clone; single-ticker analysis warns)
- New: distribute doctor (one-shot env/config/deps/network diagnosis), bidirectional plugin-vs-clone version-skew warnings
- Anti-hallucination: WebSearch-sourced claims now bind outlet + URL + access date, validated at load (fresh runs only; old reports unaffected)

## v1.0.15 — 2026-06-08

- docs(macro): drop a stale comment that still claimed both 10Y-2Y and 10Y-5Y spreads are emitted (the 2Y shim was removed in v1.0.13; only spread_10y_5y is emitted). Comment-only, no behavior change.

## v1.0.14 — 2026-06-08

- fix(portfolio): Step 8 decision-log call used an undefined $BLOB after the P4 Write-tool switch — use the literal .decisions_blob.json path so the (non-optional) decision-log write doesn't break

## v1.0.13 — 2026-06-08

- chore(P4): remove the deprecated macro us_2y/spread_10y_2y shim (^FVX is the 5Y → us_5y/spread_10y_5y only); score-industry now requires currently-tradeable peers (skip delisted/acquired); portfolio Step 8 writes its decisions blob via the Write tool (not a fragile heredoc)

## v1.0.12 — 2026-06-07

- P2 hardening + P3: enforce FDS-field classification completeness for the mixed-currency repair (test guard); scoring agent now sanity-checks impossible debt (current_debt/total_debt > total_liabilities) and skips leverage on a violation

## v1.0.11 — 2026-06-07

- feat(score-business): detect extreme-QoQ quarters (rev ≥50% / margin ≥20pp) and surface 07_earnings cross-check evidence into 02_financial_data.json, so the fundamental agent stops mistaking a real cyclical peak for corrupt data and dropping it (P1, SNDK)

## v1.0.10 — 2026-06-04

- feat(update): the auto update-check now also notifies Codex (.codex/hooks.json SessionStart + --emit-hook-json codex) — Claude Code and Codex both get the session-start release notice; Cursor/OpenCode still manual

## v1.0.9 — 2026-06-04

- fix(update): SessionStart auto-check now surfaces a USER-VISIBLE update notice (--emit-hook-json → systemMessage) — a session-start hook's plain stdout reaches only Claude's context, so the release notice was previously invisible to the user

## v1.0.8 — 2026-06-04

- fix(update): throttle only the auto (--quiet) session-start check, never a manual one — a manual 'update check' is now always live + prints its conclusion (was silenced when the SessionStart hook had checked within the hour)

## v1.0.7 — 2026-06-04

- fix(fetch): raise FDS financials limit 8->16 to capture the buried fiscal Q4 (restores FDS-direct financials + unblocks FMP-uncovered small caps/ADRs that were failing the DL4 consecutive-quarter gate)

## v1.0.6 — 2026-06-04

- fix: always load .env so FMP_API_KEY/FINNHUB_API_KEY load even when FINANCIAL_DATASETS_API_KEY is already set in the environment

## v1.0.5 — 2026-06-03

- Fix segmented revenue fetch: financialdatasets.ai retired /financials/segmented-revenues (HTTP 404); migrated to /financials/segments with the new nested response structure. Restores the per-segment revenue breakdown (product / geography / business segment) that analysis was silently missing, plus fail-closed hardening on unusable feeds.

## v1.0.4 — 2026-06-03

- Cross-platform: explicit UTF-8 on all inline + subprocess I/O (Windows cp936); portable mktemp (macOS/BSD)
- Trim dev-only development.md rule from the published product

## v1.0.3 — 2026-06-02

- FMP_API_KEY is now required (was mislabeled optional): financials fallback for foreign ADRs / non-Dec fiscal years + /screen-stocks needs it. Bootstrap prompts for it as required + warns if empty. FINNHUB_API_KEY stays optional.

## v1.0.2 — 2026-06-02

- Fix Windows GBK console crash — UTF-8 stdout/stderr; no more PYTHONUTF8=1 needed

## v1.0.1 — 2026-06-02

- Slimmer distribution: the published repo no longer carries dev/maintenance
  tooling (publish/release/audit/test scripts + `scripts/dev/`) — only the
  runtime, skills, and the `setup` / `update` entry points.

## v1.0.0 — 2026-06-02

First public release of the Stock Analysis System v7 skill set.

- **8 skills** — `score-business`, `investment-thesis`, `portfolio`,
  `screen-stocks`, `monitor`, `research-industry`, `write-report`,
  `generative-ui`.
- **Multi-agent, zero-touch** — works on Claude Code / Cowork (`.claude/skills/`)
  and Codex / Cursor / OpenCode (`.agents/skills/` + `AGENTS.md`) with no
  per-agent setup; run your agent from the repo root.
- **Versioned releases + opt-in updates** — `python3 -m scripts.update check` /
  `apply` (auto-checked on Claude Code session start; never auto-updates).
- Sourced numbers, explicit units/FX, and fail-closed portfolio limits are
  enforced (see `.claude/rules/`). Human output language is configurable
  (`output_language` in `strategy.yaml`); JSON analysis is always English.
