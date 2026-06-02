---
globs: [".claude/skills/**"]
---

## Skill Architecture Rules

When creating or modifying skills:

1. SKILL.md is orchestration only — no analysis methodology or scoring criteria
2. Analysis logic lives in `prompts/` and is loaded via Read
3. Hard constraints live in `rules/` and are loaded via Read
4. Deterministic computation lives in `scripts/` and is called via Bash
5. No hardcoded timestamps, prices, dates, or model names in SKILL.md
6. Description field defines trigger conditions (written for the model, not humans)
7. Give Claude judgment space — methodology and constraints, not rigid if-else scripts
8. **Subagents cannot Write `.md` files.** The Claude Code harness blocks the
   Write *tool* for subagent (Task/Agent) `.md` writes with: "Subagents should
   return findings as text, not write report files." (`.json` / `.txt` Write is
   allowed; the guard is on the Write tool for `.md`, so the **Bash tool is
   unaffected**.) When a dispatched subagent is the contracted producer of a
   markdown deliverable (`summary.md`, `thesis_summary.md`, the report `.md`),
   instruct it in the dispatch prompt to **write that file via a Bash heredoc**
   with a **quoted, content-unique delimiter** (UTF-8-safe, no shell expansion):
   `cat > "<resolved-path>/summary.md" <<'SUMMARY_MD_EOF'` … `SUMMARY_MD_EOF`.
   Two non-negotiables:
   - **Delimiter must not collide with content.** Use a sentinel that cannot
     appear as a standalone line in the markdown — NOT a bare `EOF`/`MD`. A
     collision terminates the heredoc early; the file is then non-empty but
     TRUNCATED, and the remaining content is parsed as shell (mis-execution).
     This is prevented ONLY by the unique sentinel — the `-s` gate below does
     NOT catch mid-file collision (the truncated file is non-empty → gate passes).
   - **Use the CONCRETE resolved path**, not a shell variable: the subagent's
     Bash shell does NOT inherit the orchestrator's `$REPORT_DIR`, so a literal
     `cat > "$REPORT_DIR/…"` in the dispatch prompt would write to `/summary.md`.
     Substitute the real path when composing the dispatch prompt.
   Do NOT rely on the Write tool for subagent `.md` output (it fails, wastes a
   turn, forces the workaround). The skill MUST ALSO keep a **hard post-dispatch
   existence gate** (`[ -s "<path>/file.md" ] || { echo "FATAL…" >&2; exit 1; }`)
   — it catches a MISSING / EMPTY deliverable (the common silent-failure mode),
   though not a mid-file delimiter collision (handle that via the unique sentinel).
