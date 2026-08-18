---
globs: ["**/*.md", "**/*.yaml", "**/*.json"]
---

## Language Rules

### All Source Files: English

Everything in the repository is written in English:
- Markdown (prompts/, rules/, SKILL.md, CLAUDE.md)
- YAML configs
- Code and comments
- JSON output (all field names, values, evidence text, interpretation)

### Human-Facing Output: Configurable

Only the final human-readable deliverables use `output_language` from strategy.yaml
(default: zh-CN). This applies to:
- One-page summary markdown (the only markdown output from score-business)
- Advisory briefs
- Any content explicitly generated for human consumption

### Exception: a machine-rendered deliverable is translated at presentation

`reports/track-record/<quarter>/summary.md` is rendered by deterministic
Python and pinned by a golden fixture, unlike every other `summary.md` in
this repo (which an agent writes from a prompt). Its `unavailable (reason)`
strings are also the CLI's `REFUSED:` output and are asserted by tests, so
generating them in `output_language` would put the money-path's refusal
vocabulary behind a translation layer.

For that one artifact the FILE stays English and the SKILL translates when
presenting it — faithfully, keeping every refusal's full reason, and showing
the English alongside. The rule above is unchanged: the user still reads
their own language. Only the layer that produces those words moves.

### What This Means in Practice

JSON analysis output:
```json
{
  "interpretation": "Consecutive 4Q acceleration, services mix rising to 28%",
  "thesis": "Durable growth driven by services transition and AI integration"
}
```
Always English. The writing skill translates when generating human-facing reports.
