#!/usr/bin/env python3
"""Distribute the v7 skill system to multiple agents + guided first-run setup.

Why this exists
---------------
v7 SKILL.md files are thin *orchestration adapters* whose bodies reference
repo-root-relative paths (`python3 -m scripts.fetch`, `Read prompts/...`,
`rules/units.md`). The real capability is the shared `prompts/` + `rules/` +
`scripts/` monorepo. Two consequences drive this module's design (full
rationale in `docs/superpowers/specs/2026-06-02-skill-distribution-design.md`):

1. A skill is NOT independently packageable (shared `scripts/` core) and only
   works with **cwd = repo root** (every command/path is repo-relative). So
   global install (`~/.claude/skills/`, `~/.codex/skills/`, plugin marketplace)
   breaks paths. The **repo is the distribution unit**.
2. What differs per agent is small: the skill-discovery dir and the
   auto-loaded instructions file. Claude Code / Cowork read `.claude/skills/`
   + `CLAUDE.md` (already canonical). Codex / OpenCode / Cursor read
   `.agents/skills/` + `AGENTS.md`. So distribution = generate that second
   discovery dir + that second instructions file, derived from the canonical
   Claude Code adapter, surfacing the constraint rules + tool-map for the
   non-Claude family.

Subcommands
-----------
    python3 -m scripts.distribute bootstrap   # guided first-import setup
    python3 -m scripts.distribute sync         # (re)generate per-agent adapters
    python3 -m scripts.distribute check        # verify adapters (CI-safe)
    python3 -m scripts.distribute uninstall    # remove generated adapters

Hard constraints honored: stdlib-only (runs before deps are installed),
cross-platform (`pathlib`/`shutil`/`os.symlink` w/ copy fallback,
`sys.executable`, no `shell=True`), never clobber user config.

Exit codes: 0 success, 1 failure/verification-fail, 2 infrastructure error.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.cli_utils import write_text_atomic

# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------
# Two adapter families. The "claude" family reads the canonical layout and
# needs no generation. The "agents" family reads the generated layout.
CLAUDE_FAMILY = ("claude-code", "cowork")
AGENTS_FAMILY = ("codex", "opencode", "cursor")
ALL_HOSTS = CLAUDE_FAMILY + AGENTS_FAMILY

# Detection hints: (executable-on-PATH, home-dir marker). Cowork is a cloud
# surface with no local CLI — it is covered by the canonical Claude layout, so
# it is never auto-detected here (bootstrap notes it separately).
_DETECT = {
    "claude-code": ("claude", "~/.claude"),
    "codex": ("codex", "~/.codex"),
    "opencode": ("opencode", "~/.config/opencode"),
    "cursor": ("cursor", "~/.cursor"),
}

CANONICAL_SKILLS_DIR = (".claude", "skills")
CANONICAL_RULES_DIR = (".claude", "rules")
AGENTS_SKILLS_DIR = (".agents", "skills")
MANIFEST_NAME = ".managed-by-distribute"
AGENTS_MD = "AGENTS.md"
HASH_MARKER = "source_hash:"  # appears in the AGENTS.md DO-NOT-EDIT header

# Tool-name mapping surfaced to non-Claude agents (SKILL.md bodies are written
# in Claude Code tool vocabulary).
_TOOL_MAP = [
    ("Read <file>", "your file-read tool"),
    ("Bash: <cmd>", "your shell / exec tool (run from repo root)"),
    ("Grep <pattern>", "ripgrep (`rg`) via shell"),
    ("Glob <pattern>", "shell glob / `find`"),
    ("Write <file>", "your file-write tool (.md writes: use a shell heredoc)"),
]


# ---------------------------------------------------------------------------
# Repo discovery + skill enumeration
# ---------------------------------------------------------------------------
def repo_root() -> Path:
    """Repo root = the package parent (this file is scripts/distribute.py)."""
    return Path(__file__).resolve().parent.parent


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def canonical_skill_dirs(root: Path) -> list[Path]:
    """Skill dirs under .claude/skills/ that hold a SKILL.md (skip workspaces)."""
    base = root.joinpath(*CANONICAL_SKILLS_DIR)
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if child.name.endswith("-workspace"):  # eval scratch, per .gitignore
            continue
        if (child / "SKILL.md").is_file():
            out.append(child)
    return out


def rules_files(root: Path) -> list[Path]:
    base = root.joinpath(*CANONICAL_RULES_DIR)
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.md"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_frontmatter(skill_md: Path) -> dict[str, str]:
    """Light YAML-frontmatter read: returns top-level scalar/block keys present.

    Only needs to confirm `name` + `description` exist for `check`. Handles the
    `description: |` block-scalar form used by v7 skills.
    """
    text = _read(skill_md)
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    # frontmatter is between the first two '---' fences
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}  # fail-open-ok: malformed frontmatter → {} → check() reports missing name/description (fail-closed downstream)
    keys: dict[str, str] = {}
    for ln in lines[1:end]:
        if ln and not ln[0].isspace() and ":" in ln:
            k, _, v = ln.partition(":")
            keys[k.strip()] = v.strip()
    return keys


# ---------------------------------------------------------------------------
# Source hash (drift detector for AGENTS.md)
# ---------------------------------------------------------------------------
def compute_source_hash(root: Path) -> str:
    """sha256 over the inputs AGENTS.md is derived from: CLAUDE.md, every
    .claude/rules/*.md, and every canonical SKILL.md. A change to any of them
    means AGENTS.md is stale → `check` flags it.
    """
    h = hashlib.sha256()
    parts: list[Path] = []
    claude_md = root / "CLAUDE.md"
    if claude_md.is_file():
        parts.append(claude_md)
    parts.extend(rules_files(root))
    parts.extend(d / "SKILL.md" for d in canonical_skill_dirs(root))
    for p in parts:
        rel = p.relative_to(root).as_posix()  # OS-stable path key
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        # Normalize line endings so the hash is identical on a CRLF (Windows)
        # checkout vs an LF (Linux/macOS) checkout — .gitattributes enforces LF
        # too, but this makes the hash robust even without it.
        content = p.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        h.update(content.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# AGENTS.md generation
# ---------------------------------------------------------------------------
def _skill_summary(skill_md: Path) -> str:
    """First non-empty line of the description block, for the skill index."""
    text = _read(skill_md)
    lines = text.splitlines()
    in_desc = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("description:"):
            after = stripped.split("description:", 1)[1].strip()
            if after and after != "|":
                return after
            in_desc = True
            continue
        if in_desc:
            if stripped and not stripped.endswith(":"):
                return stripped
            if ln and not ln[0].isspace():  # left frontmatter block
                break
    return ""


def render_agents_md(root: Path) -> str:
    src_hash = compute_source_hash(root)
    skills = canonical_skill_dirs(root)
    rules = rules_files(root)

    tool_rows = "\n".join(
        f"| `{claude}` | {other} |" for claude, other in _TOOL_MAP
    )
    rule_lines = "\n".join(
        f"- `{p.relative_to(root).as_posix()}`" for p in rules
    )
    skill_lines = "\n".join(
        f"- **{d.name}** — {_skill_summary(d / 'SKILL.md')}" for d in skills
    )

    return f"""<!-- GENERATED by scripts/distribute.py — DO NOT EDIT.
     Edit CLAUDE.md / .claude/rules/ / .claude/skills/, then run:
         python3 -m scripts.distribute sync
     {HASH_MARKER} {src_hash} -->
# AGENTS.md — Stock Analysis System v7 (cross-agent adapter)

This file is the auto-loaded project guide for agents that read `AGENTS.md`
(Codex, OpenCode, Cursor, …). Claude Code / Cowork read `CLAUDE.md` +
`.claude/rules/` instead — the canonical sources this file is derived from.

## ⚠️ Run from the repo root

Every skill body uses **repo-root-relative** paths (`python3 -m scripts.fetch`,
`Read prompts/score-fundamental.md`, `rules/units.md`). Skills do **not** work
installed globally — start your agent with the working directory set to this
repo. Skills are discovered in `.agents/skills/` (this family) or
`.claude/skills/` (Claude Code / Cowork).

## Tool-name mapping

SKILL.md bodies are written in Claude Code tool vocabulary. Translate:

| Skill says | Use |
|---|---|
{tool_rows}

> **Windows note:** skill commands use `python3`. On native Windows the
> launcher is usually `python` / `py` — run inside WSL, or alias
> `python3=python`. The repo is otherwise OS-portable (LF-normalized via
> `.gitattributes`; symlinks fall back to copies).
>
> **Output language:** human-facing reports honor `output_language` in
> `strategy.yaml` (free-form — `zh-CN`, `en-US`, `ja-JP`, …; JSON output is
> always English).

## ALWAYS read these constraint files first

Claude Code auto-loads `.claude/rules/*.md` every session; your agent does not.
Read these before any analysis or money-path work (they encode demonstrated
real-money failure modes — anti-hallucination tagging, unit/FX scale,
portfolio constraints, producer-consumer contracts):

{rule_lines}

## Skills

Invoke implicitly (describe the task) or explicitly (`/skills` / `$` mention in
Codex; the `Skill` tool in Claude Code). Each lives in its own dir with a
`SKILL.md` manifest.

{skill_lines}

## Full project guide

The canonical, complete project instructions are in **`CLAUDE.md`** — read it
for architecture, data flow, the script catalog, and the engineering policy.
This adapter intentionally does not duplicate it.
"""


# ---------------------------------------------------------------------------
# sync — generate per-agent adapters
# ---------------------------------------------------------------------------
def _link_or_copy(src: Path, dst: Path, prefer_copy: bool) -> str:
    """Materialize dst from src. Returns the mode actually used."""
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    if not prefer_copy:
        # relative symlink: .agents/skills/<name> -> ../../.claude/skills/<name>
        rel = os.path.relpath(src, dst.parent)
        try:
            os.symlink(rel, dst, target_is_directory=True)
            return "symlink"
        except (OSError, NotImplementedError):
            pass  # Windows w/o Developer Mode, or FS without symlink support
    shutil.copytree(src, dst)
    return "copy"


def sync(root: Path, hosts: list[str], prefer_copy: bool, *, quiet: bool = False) -> int:
    """(Re)generate the `.agents/skills/` discovery dir + `AGENTS.md` whenever
    any requested host is in the agents family. Claude-family hosts need no
    generation (canonical layout already serves them)."""
    needs_agents = any(h in AGENTS_FAMILY for h in hosts)
    if not needs_agents:
        if not quiet:
            print("Only Claude-family hosts selected — canonical layout already "
                  "serves them; nothing to generate.")
        return 0

    skills = canonical_skill_dirs(root)
    if not skills:
        print("ERROR: no skills found under .claude/skills/", file=sys.stderr)
        return 1

    agents_skills = root.joinpath(*AGENTS_SKILLS_DIR)
    # .agents/skills is fully managed by us → remove + recreate for idempotency.
    if agents_skills.exists():
        shutil.rmtree(agents_skills)
    agents_skills.mkdir(parents=True)

    modes = set()
    for d in skills:
        modes.add(_link_or_copy(d, agents_skills / d.name, prefer_copy))
    mode = "copy" if "copy" in modes else "symlink"

    names = [d.name for d in skills]
    manifest = (
        "# Generated by scripts/distribute.py — safe to delete via "
        "`python3 -m scripts.distribute uninstall`.\n"
        f"mode: {mode}\n"
        "skills:\n" + "".join(f"  - {n}\n" for n in names)
    )
    write_text_atomic(manifest, agents_skills / MANIFEST_NAME)

    agents_md = root / AGENTS_MD
    write_text_atomic(render_agents_md(root), agents_md)

    if not quiet:
        hosts_str = ", ".join(h for h in hosts if h in AGENTS_FAMILY)
        print(f"Generated .agents/skills/ ({mode}, {len(names)} skills) + "
              f"AGENTS.md for: {hosts_str}")
    return 0


# ---------------------------------------------------------------------------
# check — verification (CI-safe)
# ---------------------------------------------------------------------------
def check(root: Path) -> int:
    problems: list[str] = []

    # 1. Every canonical SKILL.md has name + description (Agent Skills standard).
    for d in canonical_skill_dirs(root):
        fm = parse_frontmatter(d / "SKILL.md")
        for key in ("name", "description"):
            if key not in fm:
                problems.append(f"{d.name}/SKILL.md missing frontmatter '{key}'")

    agents_md = root / AGENTS_MD
    agents_skills = root.joinpath(*AGENTS_SKILLS_DIR)
    # `.agents/skills/` is the materialized adapter. AGENTS.md is a derived,
    # gitignored artifact (like strategy.compiled.yaml) regenerated alongside it,
    # so a fresh clone (no `.agents/`) is a clean "claude-family only" no-op —
    # NOT a failure. Presence of `.agents/skills/` is the signal that the agents
    # family was set up and must be verified.
    generated = agents_skills.exists()

    if generated:
        # 2. AGENTS.md present + not stale.
        if not agents_md.exists():
            problems.append("AGENTS.md missing but .agents/skills/ present")
        else:
            text = _read(agents_md)
            embedded = None
            for ln in text.splitlines():
                if HASH_MARKER in ln:
                    rest = ln.split(HASH_MARKER, 1)[1].split()  # ['<hex>', '-->']
                    embedded = rest[0] if rest else None
                    break
            current = compute_source_hash(root)
            if embedded != current:
                problems.append(
                    "AGENTS.md is stale (source changed since last sync) — run "
                    "`python3 -m scripts.distribute sync`")
        # 3. Every canonical skill resolves in .agents/skills/.
        for d in canonical_skill_dirs(root):
            target = agents_skills / d.name
            if not (target / "SKILL.md").is_file():
                problems.append(f".agents/skills/{d.name} missing or broken link")
    else:
        print("No generated adapters present (Claude-family only). "
              "Run `python3 -m scripts.distribute sync --host codex` to add the "
              "agents family.")

    if problems:
        print("distribute check: FAIL", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("distribute check: OK")
    return 0


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
def _manifest_skills(manifest: Path) -> list[str] | None:
    """Skill names recorded in the manifest, or None if absent/unparseable."""
    if not manifest.is_file():
        return None
    names, in_list = [], False
    for ln in _read(manifest).splitlines():
        if ln.strip() == "skills:":
            in_list = True
            continue
        if in_list and ln.lstrip().startswith("- "):
            names.append(ln.split("- ", 1)[1].strip())
        elif in_list and ln.strip():
            break
    return names


def uninstall(root: Path) -> int:
    removed = []
    agents_skills = root.joinpath(*AGENTS_SKILLS_DIR)
    if agents_skills.exists():
        managed = _manifest_skills(agents_skills / MANIFEST_NAME)
        if managed is None:
            # no manifest → treat the whole dir as ours (back-compat)
            shutil.rmtree(agents_skills)
        else:
            # remove only the entries we created → preserve any user-added skill
            for name in managed:
                target = agents_skills / name
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            (agents_skills / MANIFEST_NAME).unlink(missing_ok=True)
            if not any(agents_skills.iterdir()):
                agents_skills.rmdir()
        removed.append(str(Path(*AGENTS_SKILLS_DIR)))
        # prune now-empty .agents/
        parent = root.joinpath(AGENTS_SKILLS_DIR[0])
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    agents_md = root / AGENTS_MD
    if agents_md.exists():
        agents_md.unlink()
        removed.append(AGENTS_MD)
    if removed:
        print("Removed: " + ", ".join(removed))
        print("User config (.env, strategy.yaml, portfolio-state.yaml) left untouched.")
    else:
        print("Nothing to remove.")
    return 0


# ---------------------------------------------------------------------------
# bootstrap — guided first-import setup
# ---------------------------------------------------------------------------
def detect_agents() -> list[str]:
    found = []
    for host, (exe, home) in _DETECT.items():
        if shutil.which(exe) or _expand(home).exists():
            found.append(host)
    return found


ENV_SENTINEL = "your_key_here"  # the placeholder in .env.example


def _set_env_value(lines: list[str], key: str, value: str | None) -> list[str]:
    """Set KEY=value in an .env line list. A blank/None value OR the
    `your_key_here` sentinel → comment the line out, so the sentinel never
    survives as an active, invalid key (it can arrive via env defaults in
    `--yes` mode, not just from the example file)."""
    if value is not None:
        value = value.strip()
        if not value or value == ENV_SENTINEL:
            value = None
    out = []
    handled = False
    for ln in lines:
        stripped = ln.lstrip("#").lstrip()
        if stripped.startswith(f"{key}="):
            handled = True
            if value:
                out.append(f"{key}={value}")
            elif key == "FINANCIAL_DATASETS_API_KEY":
                out.append(f"# {key}=  # REQUIRED — set before running fetch")
            else:
                out.append(f"# {key}=")
            continue
        out.append(ln)
    if not handled and value:
        out.append(f"{key}={value}")
    return out


def _prompt(msg: str, default: str = "", *, secret: bool = False, yes: bool = False) -> str:
    if yes or not sys.stdin.isatty():
        return default
    if secret:
        import getpass
        val = getpass.getpass(msg)
    else:
        val = input(msg)
    return val.strip() or default


def _copy_if_absent(src: Path, dst: Path) -> bool:
    if dst.exists():
        print(f"  • {dst.name} already exists — left untouched.")
        return False
    shutil.copyfile(src, dst)
    print(f"  • created {dst.name} from {src.name}")
    return True


def bootstrap(root: Path, *, yes: bool, prefer_copy: bool) -> int:
    print("=== Stock Analysis System v7 — first-run setup ===\n")

    # 1. Python version
    if sys.version_info < (3, 10):
        print(f"ERROR: Python ≥3.10 required (found {sys.version.split()[0]}).",
              file=sys.stderr)
        return 2
    print(f"Python {sys.version.split()[0]} OK (≥3.10).")

    # 2. Runtime deps (warn only — keep this script zero-dep)
    missing = [m for m in ("yfinance", "yaml") if not _module_available(m)]
    if missing:
        pretty = "yfinance / PyYAML"
        print(f"  ! runtime deps not importable ({pretty}). Install with:")
        print(f"      {sys.executable} -m pip install -r requirements.txt")
    else:
        print("Runtime deps (yfinance, PyYAML) OK.")

    # 3. .env
    print("\n[1/4] API keys (.env)")
    env_example = root / ".env.example"
    env_path = root / ".env"
    if env_path.exists():
        print("  • .env already exists — left untouched.")
    elif env_example.exists():
        lines = _read(env_example).splitlines()
        print("  Get keys: financialdatasets.ai (required) · "
              "financialmodelingprep.com (FMP, required) · finnhub.io (optional)")
        # Default each key to its current env var: interactive → press-enter
        # reuses an exported key; non-interactive (`--yes`/no-tty) → keys come
        # straight from the environment (CI / automated setup).
        fds = _prompt("  FINANCIAL_DATASETS_API_KEY (required): ", secret=True, yes=yes,
                      default=os.environ.get("FINANCIAL_DATASETS_API_KEY", ""))
        fmp = _prompt("  FMP_API_KEY (required): ", secret=True, yes=yes,
                      default=os.environ.get("FMP_API_KEY", ""))
        fin = _prompt("  FINNHUB_API_KEY (optional, enter to skip): ", secret=True, yes=yes,
                      default=os.environ.get("FINNHUB_API_KEY", ""))
        lines = _set_env_value(lines, "FINANCIAL_DATASETS_API_KEY", fds or None)
        lines = _set_env_value(lines, "FMP_API_KEY", fmp or None)
        lines = _set_env_value(lines, "FINNHUB_API_KEY", fin or None)
        write_text_atomic("\n".join(lines) + "\n", env_path)
        print("  • wrote .env")
        if not fds:
            print("  ! FINANCIAL_DATASETS_API_KEY left empty — set it before /score-business.")
        if not fmp:
            print("  ! FMP_API_KEY left empty — set it before /score-business or /screen-stocks.")

    # 4. strategy.yaml
    print("\n[2/4] Strategy (strategy.yaml)")
    strat_example = root / "strategy.example.yaml"
    strat_path = root / "strategy.yaml"
    if _copy_if_absent(strat_example, strat_path) and not yes:
        # output_language is free-form — it is passed verbatim to the LLM
        # ("write in <output_language>"), so ANY language tag works, not just
        # zh-CN/en-US. JSON analysis output is always English regardless.
        lang = _prompt("  output_language for human-facing reports "
                       "(e.g. zh-CN, en-US, ja-JP, de-DE; default zh-CN): ",
                       default="zh-CN", yes=yes)
        if lang:
            _set_yaml_scalar(strat_path, "output_language", lang)

    # 5. portfolio-state.yaml
    print("\n[3/4] Portfolio state (portfolio-state.yaml)")
    _copy_if_absent(root / "portfolio-state.example.yaml", root / "portfolio-state.yaml")

    # 6. Agent adapters
    print("\n[4/4] Agent adapters")
    detected = detect_agents()
    print(f"  • detected agents: {', '.join(detected) if detected else 'none'}")
    hosts = sorted(set(detected) | {"claude-code"})
    if not any(h in AGENTS_FAMILY for h in hosts):
        # Offer to also generate the agents family so a fresh Codex/OpenCode/
        # Cursor checkout works without re-running.
        ans = _prompt("  also generate Codex/OpenCode/Cursor adapter (.agents/ + "
                      "AGENTS.md)? [Y/n]: ", default="y", yes=yes)
        if ans.lower() != "n":
            hosts = sorted(set(hosts) | set(AGENTS_FAMILY))
    sync(root, hosts, prefer_copy)

    # 7. Sanity check (0-cost loader replay). smoke_test is a dev/test tool —
    # absent in the published end-user repo — so skip cleanly when it's not here.
    if not _module_available("scripts.smoke_test"):
        print("\nSanity check: skipped (dev-only smoke_test not in this build).")
    else:
        print("\nSanity check (typed-loader replay, 0 cost)…")
        try:
            rc = subprocess.run(
                [sys.executable, "-m", "scripts.smoke_test", "--loaders-only"],
                cwd=str(root), capture_output=True, text=True, timeout=120,
            )
            print("  loaders:", "OK" if rc.returncode == 0 else f"non-zero ({rc.returncode})")
        except Exception as e:  # non-fatal — onboarding sanity only
            print(f"  loaders: skipped ({e})")

    print("\n=== Setup complete ===")
    print("Next:")
    print("  • Claude Code / Cowork: open this repo, skills are in .claude/skills/")
    if any(h in AGENTS_FAMILY for h in hosts):
        print("  • Codex / OpenCode / Cursor: start in this repo; AGENTS.md + "
              ".agents/skills/ are ready")
    print("  • Try: /score-business AAPL")
    return 0


def _module_available(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _set_yaml_scalar(path: Path, key: str, value: str) -> None:
    """Set a top-level `key: value` scalar by line substitution (no yaml dep),
    preserving any trailing comment."""
    lines = _read(path).splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}:"):  # top-level only (no leading indent)
            comment = ""
            if "#" in ln:
                comment = "            # " + ln.split("#", 1)[1].strip()
            lines[i] = f"{key}: {value}{comment}"
            break
    write_text_atomic("\n".join(lines) + "\n", path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_hosts(raw: str | None, all_flag: bool) -> list[str]:
    if all_flag:
        return list(ALL_HOSTS)
    if not raw:
        return sorted(set(detect_agents()) | {"claude-code"})
    hosts = []
    for h in raw.split(","):
        h = h.strip()
        if h and h in ALL_HOSTS:
            hosts.append(h)
        elif h:
            print(f"WARNING: unknown host '{h}' (known: {', '.join(ALL_HOSTS)})",
                  file=sys.stderr)
    return hosts or ["claude-code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="distribute",
        description="Distribute the v7 skill system to multiple agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap", help="Guided first-import setup.")
    b.add_argument("--yes", action="store_true", help="Non-interactive (copy examples, skip prompts).")
    b.add_argument("--copy", action="store_true", help="Copy skills instead of symlinking.")

    s = sub.add_parser("sync", help="(Re)generate per-agent adapters.")
    s.add_argument("--host", help="Comma-separated hosts (default: auto-detect).")
    s.add_argument("--all", action="store_true", help="All known hosts.")
    s.add_argument("--copy", action="store_true", help="Copy instead of symlink.")

    sub.add_parser("check", help="Verify adapters (CI-safe, exit≠0 on problem).")
    sub.add_parser("uninstall", help="Remove generated adapters (keeps user config).")

    args = parser.parse_args(argv)
    root = repo_root()

    if args.command == "bootstrap":
        return bootstrap(root, yes=args.yes, prefer_copy=args.copy)
    if args.command == "sync":
        return sync(root, _parse_hosts(args.host, args.all), args.copy)
    if args.command == "check":
        return check(root)
    if args.command == "uninstall":
        return uninstall(root)
    return 2


if __name__ == "__main__":
    sys.exit(main())
