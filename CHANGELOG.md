# Changelog

Release notes for the distributed skill system. Newest first. Managed by
`scripts/release.py`; recipients see the latest entry on update.

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
