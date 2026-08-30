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
   turn, forces the workaround). **And where the host reaches the repo through
   a BRIDGE** — the repo is a mounted / remote-device path rather than the
   agent's own filesystem — a *permitted* Write (`.json`, `.html`) is no safer:
   it writes to the subagent's own container, so the file never appears in the
   repo and the gate below fires on an artifact that was written successfully
   somewhere else. So the dispatch prompt must name the WRITE TOOL as well as
   the path — the same bridged Bash tool the orchestrator itself runs in, via
   the heredoc above — for EVERY extension, never leaving the tool to the
   subagent's default (feedback 2026-08-29 monitor ④: the router's
   `action_plan.raw.json` vanished this way, and only a hand-added line in the
   dispatch prompt saved the run). The skill MUST ALSO keep a **hard post-dispatch
   existence gate** (`[ -s "<path>/file.md" ] || { echo "FATAL…" >&2; exit 1; }`)
   — it catches a MISSING / EMPTY deliverable (the common silent-failure mode),
   though not a mid-file delimiter collision (handle that via the unique sentinel).

   **Say WHICH MACHINE the repo is on, in the dispatch prompt itself.** A
   subagent's Read / Write / Bash default to the agent's OWN container, and on
   a bridged host that is not where the repo is — so a subagent following an
   unqualified "write it with the Write tool" writes a file that exists,
   somewhere nobody will look, and the orchestrator's `[ -s ... ]` gate then
   reports "the agent did not write the file". The diagnosis points at the
   wrong cause and a re-dispatch fails the same way (feedback 2026-08-30
   score-business ⑧ / investment-thesis ⑦: all 8 dispatches needed this line
   hand-added; 8/8 succeeded once it was there). Detect it the way the
   orchestrator already knows — if IT reaches the repo through a bridged bash
   tool, so must every agent it dispatches — and add one sentence naming the
   tool and the root, e.g.: *"The repo is on the user's device, not in your
   container: do every read and write through `<the bridged bash tool>`; the
   repo root is `<absolute path>`. Your own Read/Write/Bash address a
   different filesystem and must not be used for these paths."* On a
   single-machine host the sentence is simply omitted.
9. **Artifacts live under `<ROOT>/reports/` — never improvise a fallback.**
   If `allocate-bq-run` / a report write fails (corrupt mount, permissions),
   STOP and surface the error to the user. Do NOT redirect output to `/tmp`,
   `$HOME`, or any path outside the repo: in Cowork those are ephemeral
   (lost at session end) and the delta layer (classify/resolver) can never
   find them again — a silently relocated artifact is worse than a visibly
   failed run (feedback 2026-06-11 #6: a full MU BQ+thesis written to /tmp
   after a mount failure evaporated with the session).
