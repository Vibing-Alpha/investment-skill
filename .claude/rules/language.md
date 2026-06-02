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

### What This Means in Practice

JSON analysis output:
```json
{
  "interpretation": "Consecutive 4Q acceleration, services mix rising to 28%",
  "thesis": "Durable growth driven by services transition and AI integration"
}
```
Always English. The writing skill translates when generating human-facing reports.
