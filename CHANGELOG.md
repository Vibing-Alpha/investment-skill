# Changelog

Release notes for the distributed skill system. Newest first. Managed by
`scripts/release.py`; recipients see the latest entry on update.

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
