---
globs: ["scripts/**", "prompts/**", ".claude/skills/**"]
---

## Producer-Consumer Contract Rules

When one component produces data that another consumes, the contract
between them must be explicit and consistent. These rules prevent the
recurring bugs from the v7 portfolio development cycle.

### 1. Field Names Are Contracts

When a spec or schema defines a field name (e.g., `fed_funds`), every
producer and consumer MUST use that exact name. Do not invent synonyms.

Before committing, grep for the field name across all producers and
consumers. If you find a mismatch, fix it before it ships.

Common drift locations: spec → script output → prompt input → SKILL.md
references → test assertions.

### 2. Vocabulary Must Match Across Layers

When a prompt defines an output vocabulary (e.g., actions: buy, add,
hold, reduce, exit), every consuming script MUST handle ALL values in
that vocabulary — not just a subset.

When adding a new value to a prompt's vocabulary, grep all downstream
consumers and update them in the same commit.

### 3. One Implementation, Not Two

When the same logic is needed in multiple places within a file, call
one function. Do not reimplement it as a "lightweight" closure or
inline version. The two implementations WILL diverge silently.

If the original function returns more than you need, call it and
discard what you don't use. The cost of an unused return value is
zero; the cost of a diverged reimplementation is a bug.

### 4. Missing Data Is a Failure, Not Zero

When a required input is missing (e.g., no price for a ticker), the
default behavior must be fail-closed:
- Flag it as a violation or warning
- Do NOT silently substitute zero (which makes the position invisible)
- Do NOT skip the check (which lets violations through)

The principle: unknown data should trigger the most conservative
behavior, not the most permissive.

This applies to sentiment/flow feeds too (insider, institutional, news): a
failed or empty feed is `unknown`, NOT `neutral`/`stable` — it must not tilt
an aggregate (e.g. events `overall_event_bias`) as if absence of signal were a
balanced reading. See `prompts/evaluate-events.md` §3/§5.
