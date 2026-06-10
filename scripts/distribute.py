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
    python3 -m scripts.distribute doctor       # one-shot env/config/deps/network diagnosis
                                               #   (--deep: +1 FDS +1 FMP authenticated request)

Hard constraints honored: stdlib-only (runs before deps are installed),
cross-platform (`pathlib`/`shutil`/`os.symlink` w/ copy fallback,
`sys.executable`, no `shell=True`), never clobber user config.

Exit codes: 0 success, 1 failure/verification-fail, 2 infrastructure error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.cli_utils import write_text_atomic
from scripts.version_skew import PLACEHOLDER as SKEW_PLACEHOLDER

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
# Cowork thin-plugin packaging (Plan B Task 3b). The plugin manifests are
# TRACKED; the materialized skills/ tree is a derived, gitignored artifact
# (like .agents/skills/) — symlinks in dev, real copies at publish-bake.
PLUGIN_MANIFEST_JSON = ("plugins", "stock-v7-skills", ".claude-plugin", "plugin.json")
PLUGIN_SKILLS_DIR = ("plugins", "stock-v7-skills", "skills")
MARKETPLACE_JSON = (".claude-plugin", "marketplace.json")
VERSION_FILE = "VERSION"
MANIFEST_NAME = ".managed-by-distribute"
# --- Plugin-packaging drift guards (Plan B Task 5) ------------------------
# Canonical resolver-core template; every plugin-shipped skill's Step 0 embeds
# the content BETWEEN the delimiter lines byte-identically (exclusive — the
# embedded start-marker line carries a trailing comment; tails are
# per-consumer and not compared).
RESOLVER_TEMPLATE = ("scripts", "templates", "root_resolver.sh")
_CORE_RE = re.compile(
    r"^# --- resolver-core ---[^\n]*\n(.*?)^# --- end resolver-core ---",
    re.DOTALL | re.MULTILINE,
)
# The 8 plugin-shipped business skills (same list as tests/test_skill_prelude.py).
MONEY_PATH_SKILLS = (
    "score-business", "investment-thesis", "screen-stocks", "research-industry",
    "portfolio", "monitor", "write-report", "generative-ui",
)
SETUP_SKILL = "stock-v7-setup"  # resolver-core identity only (own tail/flow)
# config_gate Preflight tiers (same lists/regex as tests/test_money_path_skills_gated.py
# — distribute.py cannot import pytest files, so the tiny regex is mirrored here;
# the pytest lint stays the independent guard against THIS copy drifting).
PREFLIGHT_BASE_SKILLS = ("score-business", "investment-thesis",
                         "screen-stocks", "research-industry")
PREFLIGHT_PORTFOLIO_SKILLS = ("portfolio", "monitor", "generative-ui")
_GATE_CMD = r'(?:"\$PYBIN"|python3) -m scripts\.config_gate check'
_PREFLIGHT_SEC_RE = re.compile(
    r"^##+\s*Preflight: Money-path config\b.*?(?=^##\s|\Z)", re.DOTALL | re.MULTILINE)
# ONLY fenced ```bash / ```sh blocks count as executable (prose / inline code
# excluded) — same fence convention as tests/test_skill_prelude.py.
_BASH_BLOCK_RE = re.compile(
    r"^[ \t]*```(?:bash|sh)\n(.*?)^[ \t]*```\s*$", re.DOTALL | re.MULTILINE)
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


def _write_json_atomic(obj: object, path: Path) -> None:
    write_text_atomic(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", path)


def write_manifest_versions(root: Path) -> str | None:
    """Lockstep the plugin manifests with the `VERSION` file (Task 3b duty):
    `plugin.json:version` AND `marketplace.json:plugins[].version` are written
    from `VERSION` on every sync — never hand-pasted. Returns the version
    written, or None when this checkout has no plugin manifest / VERSION.
    Writes only on change (keeps tracked files byte-stable when in lockstep)."""
    version_file = root / VERSION_FILE
    plugin_json = root.joinpath(*PLUGIN_MANIFEST_JSON)
    if not (version_file.is_file() and plugin_json.is_file()):
        return None
    version = version_file.read_text(encoding="utf-8").strip()
    plugin = json.loads(plugin_json.read_text(encoding="utf-8"))
    if plugin.get("version") != version:
        plugin["version"] = version  # update version only; key order preserved
        _write_json_atomic(plugin, plugin_json)
    marketplace_json = root.joinpath(*MARKETPLACE_JSON)
    if marketplace_json.is_file():
        market = json.loads(marketplace_json.read_text(encoding="utf-8"))
        changed = False
        for entry in market.get("plugins", []):
            if entry.get("name") == plugin.get("name") and entry.get("version") != version:
                entry["version"] = version
                changed = True
        if changed:
            _write_json_atomic(market, marketplace_json)
    return version


def _bake_expected_min(root: Path, plugin_skills: Path) -> int:
    """Task 6: substitute the version-skew placeholder with the clone's
    `VERSION` in every REAL-COPY materialized SKILL.md (the publish-bake
    path). Symlinked entries share the canonical source file — the
    placeholder stays unexpanded there, which the runtime helper treats as
    "skip the comparison" (a clone-side run is tautologically
    self-consistent). The baked literal is the installed plugin's
    expected-min: the ONE runtime-reachable carrier — after the Step-0 `cd`
    a skill can only read the CLONE's files, so a manifest field would
    compare the clone against itself. Returns the number of files baked."""
    version_file = root / VERSION_FILE
    if not version_file.is_file():
        return 0
    version = version_file.read_text(encoding="utf-8").strip()
    baked = 0
    for d in sorted(plugin_skills.iterdir()):
        if d.is_symlink() or not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.is_file():
            continue
        text = md.read_text(encoding="utf-8")
        if SKEW_PLACEHOLDER in text:
            md.write_text(text.replace(SKEW_PLACEHOLDER, version),
                          encoding="utf-8")
            baked += 1
    return baked


def sync_plugin(root: Path, prefer_copy: bool, *, quiet: bool = False) -> int:
    """Materialize the WHOLE `.claude/skills/*` tree into
    `plugins/stock-v7-skills/skills/` (Cowork plugin skills are auto-discovered
    from `<plugin-root>/skills/<name>/SKILL.md`) + write both manifest versions
    from `VERSION`. No-op when this checkout carries no plugin manifest."""
    if not root.joinpath(*PLUGIN_MANIFEST_JSON).is_file():
        return 0  # not a plugin-packaged checkout — nothing to materialize
    skills = canonical_skill_dirs(root)
    if not skills:
        print("ERROR: no skills found under .claude/skills/", file=sys.stderr)
        return 1

    plugin_skills = root.joinpath(*PLUGIN_SKILLS_DIR)
    # fully managed by us → remove + recreate for idempotency (mirrors .agents/)
    if plugin_skills.exists():
        shutil.rmtree(plugin_skills)
    plugin_skills.mkdir(parents=True)

    modes = set()
    for d in skills:
        modes.add(_link_or_copy(d, plugin_skills / d.name, prefer_copy))
    mode = "copy" if "copy" in modes else "symlink"
    _bake_expected_min(root, plugin_skills)  # copy-mode entries only (Task 6)

    names = [d.name for d in skills]
    manifest = (
        "# Generated by scripts/distribute.py — derived from .claude/skills/; "
        "regenerate via `python3 -m scripts.distribute sync`.\n"
        f"mode: {mode}\n"
        "skills:\n" + "".join(f"  - {n}\n" for n in names)
    )
    write_text_atomic(manifest, plugin_skills / MANIFEST_NAME)

    version = write_manifest_versions(root)
    if not quiet:
        ver = f", manifests at version {version}" if version else ""
        print(f"Generated {Path(*PLUGIN_SKILLS_DIR)}/ ({mode}, {len(names)} "
              f"skills{ver})")
    return 0


def sync(root: Path, hosts: list[str], prefer_copy: bool, *, quiet: bool = False) -> int:
    """(Re)generate the derived skill-discovery layouts: the Cowork plugin
    `plugins/stock-v7-skills/skills/` tree (whenever the tracked plugin
    manifest is present — Cowork is claude-family, so this runs regardless of
    host selection), and the `.agents/skills/` dir + `AGENTS.md` whenever any
    requested host is in the agents family."""
    rc = sync_plugin(root, prefer_copy, quiet=quiet)
    if rc != 0:
        return rc

    needs_agents = any(h in AGENTS_FAMILY for h in hosts)
    if not needs_agents:
        if not quiet:
            print("Only Claude-family hosts selected — canonical layout already "
                  "serves them; no .agents/ adapter to generate.")
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
def _plugin_packaging_problems(root: Path) -> list[str]:
    """Plan B Task 5 drift guards. Active ONLY in a plugin-packaged checkout
    (the tracked plugin.json is the signal, mirroring sync_plugin). Named
    failure modes: manifest-version drift (updates never surface to installed
    plugins), a money-path skill shipping without its Step-0 resolver or
    config_gate Preflight (wrong root / unconfirmed config), bare
    `python3 -m scripts.` bypassing the venv (silent system-python computing
    money-path numbers), and an embedded resolver-core diverging from the
    canonical template (the drift the template exists to prevent)."""
    plugin_json = root.joinpath(*PLUGIN_MANIFEST_JSON)
    if not plugin_json.is_file():
        return []  # not a plugin-packaged checkout — lints inactive
    problems: list[str] = []
    pj_rel = Path(*PLUGIN_MANIFEST_JSON).as_posix()
    mp_rel = Path(*MARKETPLACE_JSON).as_posix()
    fix = "run `python3 -m scripts.distribute sync`"

    # 1. Version lockstep: BOTH manifests' version fields == VERSION.
    version: str | None = None
    version_file = root / VERSION_FILE
    if not version_file.is_file():
        problems.append(f"VERSION file missing but {pj_rel} present")
    else:
        version = version_file.read_text(encoding="utf-8").strip()
    try:
        plugin_version = json.loads(_read(plugin_json)).get("version")
        if version is not None and plugin_version != version:
            problems.append(f"{pj_rel} version {plugin_version!r} != VERSION "
                            f"{version!r} — {fix}")
    except (json.JSONDecodeError, OSError) as e:
        problems.append(f"{pj_rel} unreadable: {e}")
    marketplace_json = root.joinpath(*MARKETPLACE_JSON)
    if not marketplace_json.is_file():
        problems.append(f"{mp_rel} missing but {pj_rel} present")
    else:
        try:
            market = json.loads(_read(marketplace_json))
            for entry in market.get("plugins", []):
                if version is not None and entry.get("version") != version:
                    problems.append(
                        f"{mp_rel} plugins[{entry.get('name')!r}] version "
                        f"{entry.get('version')!r} != VERSION {version!r} — {fix}")
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"{mp_rel} unreadable: {e}")

    # 2. Canonical resolver-core template.
    template = root.joinpath(*RESOLVER_TEMPLATE)
    tpl_rel = Path(*RESOLVER_TEMPLATE).as_posix()
    template_core: str | None = None
    if not template.is_file():
        problems.append(f"{tpl_rel} missing (canonical resolver-core template)")
    else:
        m = _CORE_RE.search(_read(template))
        if m:
            template_core = m.group(1)
        else:
            problems.append(f"{tpl_rel} has no resolver-core delimiter lines")

    # 3. Per-skill lints (skills present in this checkout).
    skills_base = root.joinpath(*CANONICAL_SKILLS_DIR)
    for name in MONEY_PATH_SKILLS + (SETUP_SKILL,):
        md = skills_base / name / "SKILL.md"
        if not md.is_file():
            continue  # materialization coverage is sync/test territory
        rel = f"{Path(*CANONICAL_SKILLS_DIR).as_posix()}/{name}/SKILL.md"
        text = _read(md)

        # 3a. Step-0 resolver-core present + byte-identical to the template
        #     (content BETWEEN the delimiter lines, exclusive).
        m = _CORE_RE.search(text)
        if not m:
            problems.append(f"{rel}: no Step-0 resolver-core block "
                            f"(`# --- resolver-core ---` … `# --- end resolver-core ---`)")
        elif template_core is not None and m.group(1) != template_core:
            problems.append(f"{rel}: embedded resolver-core drifted from {tpl_rel} "
                            f"(must be byte-identical between the delimiter lines)")
        if name == SETUP_SKILL:
            continue  # setup skill: core identity only; its tail/flow differs by design

        # 3b. Venv indirection: no bare `python3 -m scripts.` in executable blocks.
        for i, block in enumerate(_BASH_BLOCK_RE.findall(text)):
            if "python3 -m scripts." in block:
                problems.append(
                    f"{rel}: bash block {i} runs bare `python3 -m scripts.` — "
                    f"must be `\"$PYBIN\" -m scripts.` (venv indirection)")

        # 3c. config_gate Preflight at the right tier, with STOP prose.
        if name in PREFLIGHT_BASE_SKILLS or name in PREFLIGHT_PORTFOLIO_SKILLS:
            sec = _PREFLIGHT_SEC_RE.search(text)
            block = sec.group(0) if sec else ""
            if name in PREFLIGHT_PORTFOLIO_SKILLS:
                ok = bool(re.search(_GATE_CMD + " --portfolio", block))
                tier = "--portfolio tier"
            else:
                ok = bool(re.search(_GATE_CMD + "(?! --portfolio)", block))
                tier = "base tier (no --portfolio)"
            if not (ok and "STOP" in block):
                problems.append(
                    f"{rel}: missing/incorrect 'Preflight: Money-path config' "
                    f"section — needs `config_gate check` at {tier} + STOP prose")
    return problems


def check(root: Path) -> int:
    problems: list[str] = []

    # 0. Plugin-packaging drift guards (no-op outside a plugin-packaged checkout).
    problems.extend(_plugin_packaging_problems(root))

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


def _remove_managed_tree(tree: Path, *, allow_legacy_no_manifest: bool = False) -> bool:
    """Manifest-guarded removal of ONE generated skills tree (shared by
    `.agents/skills/` and `plugins/stock-v7-skills/skills/` — producer-consumer
    rule #3: one implementation). Returns True if the tree existed and was
    (at least partially) removed.

    `allow_legacy_no_manifest` covers ONLY `.agents/skills/`, which predates the
    `.managed-by-distribute` manifest. The plugin tree has always written the
    manifest, so a manifest-less dir there is NOT ours — deleting it would be
    user data loss (codex post-impl regression check R2): skip it instead."""
    if not tree.exists():
        return False
    managed = _manifest_skills(tree / MANIFEST_NAME)
    if managed is None:
        if not allow_legacy_no_manifest:
            print(
                f"distribute uninstall: {tree} has no {MANIFEST_NAME} manifest "
                f"— not generated by sync, leaving it untouched.",
                file=sys.stderr,
            )
            return False
        # no manifest → treat the whole dir as ours (back-compat, .agents only)
        shutil.rmtree(tree)
    else:
        # remove only the entries we created → preserve any user-added skill
        for name in managed:
            target = tree / name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        (tree / MANIFEST_NAME).unlink(missing_ok=True)
        if not any(tree.iterdir()):
            tree.rmdir()
    return True


def uninstall(root: Path) -> int:
    removed = []
    agents_skills = root.joinpath(*AGENTS_SKILLS_DIR)
    if _remove_managed_tree(agents_skills, allow_legacy_no_manifest=True):
        removed.append(str(Path(*AGENTS_SKILLS_DIR)))
        # prune now-empty .agents/
        parent = root.joinpath(AGENTS_SKILLS_DIR[0])
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    # generated plugin skills tree (sync_plugin output, codex post-impl Fix 6) —
    # the TRACKED plugin.json stays; only the derived skills/ tree is ours
    if _remove_managed_tree(root.joinpath(*PLUGIN_SKILLS_DIR)):
        removed.append(str(Path(*PLUGIN_SKILLS_DIR)))
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
                cwd=str(root), capture_output=True, text=True, encoding="utf-8", timeout=120,
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


def _set_setup_block(path: Path, *, config_version: int, confirmed_at: str) -> None:
    """Stamp a top-level `setup:` mapping by text manipulation (no yaml dep; runs pre-pip-install).
    Drops any existing top-level `setup:` block (its 2 indented lines), then appends a fresh one.
    Preserves all other lines/comments. Atomic write."""
    src = _read(path).splitlines()
    out, skip = [], 0
    for ln in src:
        if skip and (ln.startswith(" ") or ln.startswith("\t") or not ln.strip()):
            continue                                          # swallow the old block's indented body
        skip = 0
        if ln.startswith("setup:"):
            skip = 1; continue                                # drop old top-level setup: + its body
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    # confirmed_at MUST be single-quoted: a BARE ISO timestamp (2026-06-09T00:00:00Z) is parsed by
    # PyYAML as a datetime OBJECT, not a str — _require_iso requires a str, so it would fail-close after
    # a successful confirm. The single-quote keeps it a string on read-back. (config_version int is safe.)
    out += ["setup:", f"  config_version: {int(config_version)}", f"  confirmed_at: '{confirmed_at}'"]
    write_text_atomic("\n".join(out) + "\n", path)


def confirm_setup(root, *, home_file=None, confirmed_at=None):
    """VERIFY the user personalized strategy.yaml + provided an API key, THEN stamp the setup:
    confirmation block + write ~/.stock-v7-home. Does NOT write principles (the user owns their
    strategy). Raises config_gate.ConfigError (fail-closed) if still unedited / example missing /
    API key absent — and does NOT stamp or write the home file in that case. Runs post-pip (the
    interactive confirm step), so importing config_gate (lazy yaml) is safe here."""
    import os
    from datetime import datetime, timezone
    from pathlib import Path
    from scripts.config_gate import (CONFIG_VERSION, ConfigError, assert_personalized_and_ready,
                                     assert_portfolio_state_ok)
    from scripts.root_resolve import write_home, find_conflicts, HOME_FILE
    root = Path(root)
    strat = root / "strategy.yaml"
    if not strat.exists():
        raise ConfigError("strategy.yaml not found — run setup to seed it first")
    # SINGLE-ROOT hard-fail (spec §3.2): if other candidate roots ALSO hold a real fund-state, refuse —
    # a real-money tool must not run with two divergent portfolio-states (the CC-clone + Cowork-default
    # case). Candidates = this root + $STOCK_V7_HOME + ~/.stock-v7-home + the default ~/Claude/stock-v7
    # (so the Cowork default is caught even when no env/marker points at it).
    hf = home_file or HOME_FILE
    candidates = [root, Path.home() / "Claude" / "stock-v7"]
    env_home = (os.environ.get("STOCK_V7_HOME") or "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser())
    if hf.exists():
        prev = hf.read_text(encoding="utf-8").strip()
        if prev:
            candidates.append(Path(prev).expanduser())
    conflicts = find_conflicts(candidates=candidates)
    if len(conflicts) > 1:
        raise ConfigError("multiple stock-v7 roots hold a portfolio-state.yaml "
                          f"({', '.join(str(c) for c in conflicts)}) — keep ONE, then re-run setup")
    # Verify BOTH levels (base + portfolio-state) so a successful confirm guarantees EVERY money-path
    # skill passes — no "confirmed but /portfolio still blocked" gap. Raises (no stamp, no home write)
    # if anything is unready.
    assert_personalized_and_ready(root)
    assert_portfolio_state_ok(root)
    stamp = confirmed_at or datetime.now(timezone.utc).isoformat()   # generate, never stamp None
    _set_setup_block(strat, config_version=CONFIG_VERSION, confirmed_at=stamp)
    write_home(root, home_file=hf)


# ---------------------------------------------------------------------------
# doctor — one-shot env / config / deps / network diagnosis (Plan B Task 6)
# ---------------------------------------------------------------------------
# NO recovery automation by design: every ✗ line names the fix (setup /
# bootstrap --confirm / pip install) and the user performs it. Exit 0 when no
# ✗ (⚠ allowed), 1 on any ✗.
_OK, _WARN, _FAIL = "ok", "warn", "fail"
_SYM = {_OK: "✓", _WARN: "⚠", _FAIL: "✗"}
_DOCTOR_DEP_MODULES = ("yfinance", "yaml")  # the requirements.txt runtime deps


def _resolve_pybin(root: Path) -> Path | None:
    """The interpreter the SKILLs actually use: `$ROOT/.venv` (POSIX
    `bin/python`, Windows `Scripts/python.exe`). None when no venv exists —
    the caller falls back to `sys.executable` WITH a note (system-python OK ≠
    the skill venv OK, so the fallback is always surfaced)."""
    for rel in (Path("bin") / "python", Path("Scripts") / "python.exe"):
        cand = root / ".venv" / rel
        if cand.exists():
            return cand
    return None


def _interpreter_version(pybin: str) -> tuple[int, int, int] | None:
    """(major, minor, micro) of `pybin` — probed via subprocess so the VENV
    interpreter's version is checked, not the one running doctor. None when
    the interpreter cannot be executed."""
    try:
        r = subprocess.run(
            [str(pybin), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        parts = tuple(int(x) for x in r.stdout.strip().split("."))
    except ValueError:
        return None
    return parts[:3] if len(parts) >= 3 else None


def _interpreter_imports_ok(pybin: str, modules: tuple[str, ...]) -> tuple[bool, str]:
    """Import `modules` INSIDE `pybin` (`[pybin, "-c", "import …"]`) — never
    the interpreter running doctor. Returns (ok, last stderr line on failure)."""
    try:
        r = subprocess.run(
            [str(pybin), "-c", "import " + ", ".join(modules)],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    if r.returncode == 0:
        return True, ""
    lines = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
    return False, (lines[-1] if lines else f"exit code {r.returncode}")


def _probe_provider_host(host: str) -> tuple[bool, str]:
    """Default-tier NON-SPENDING reachability probe: DNS resolve + ONE
    un-authenticated HTTPS GET to https://<host>/ through the DL1 `http_get`
    primitive (SSRF/redirect-hardened; the policy carries NO auth_fn, so no
    API key is ever sent). ANY HTTP status — 401/404/429/5xx — counts as
    reachable: only the network path is under test, not request semantics."""
    import socket
    try:
        socket.getaddrinfo(host, 443)
    except OSError as e:
        return False, f"DNS resolution failed ({e})"
    from scripts.sources.common import (HttpError, HttpPolicy,
                                        RetryExhaustedError, http_get)
    policy = HttpPolicy(timeout_s=10.0, max_retries=1,
                        allowed_host_suffixes=frozenset({host}),
                        allowed_schemes=frozenset({"https"}))
    try:
        resp = http_get(f"https://{host}/", policy=policy)
        return True, f"HTTPS reachable (HTTP {resp.status})"
    except RetryExhaustedError as e:
        return True, f"HTTPS reachable (HTTP {e.status})"  # server answered
    except (HttpError, OSError, ValueError) as e:
        return False, f"HTTPS request failed ({type(e).__name__})"


def _classify_deep_response(status: int, key_name: str) -> tuple[str, str]:
    if 200 <= status < 300:
        return _OK, f"authenticated (HTTP {status})"
    if status in (401, 402, 403):
        return _FAIL, f"key rejected (HTTP {status}) — check {key_name} in .env"
    if status == 429:
        return _WARN, ("rate-limited (HTTP 429) — quota gate reached; "
                       "key shape accepted, retry later")
    return _WARN, f"unexpected HTTP {status}"


def _deep_probe(url: str, policy, key_name: str) -> tuple[str, str]:
    """ONE authenticated request (minimal quota: max_retries=1, so a 429/5xx
    cannot silently burn extra quota on backoff retries). Classification is by
    status ONLY — details never echo the URL or exception text (the FMP key
    rides in the query string; a leaked transport error could carry it)."""
    from scripts.sources.common import HttpError, RetryExhaustedError, http_get
    try:
        resp = http_get(url, policy=policy)
    except RetryExhaustedError as e:
        return _classify_deep_response(e.status, key_name)
    except (HttpError, OSError, ValueError) as e:
        return _FAIL, f"request failed ({type(e).__name__})"
    return _classify_deep_response(resp.status, key_name)


def _deep_probe_fds() -> tuple[str, str]:
    """1 financialdatasets.ai request (company facts — the lightest endpoint).
    Auth via FD_API_POLICY's own X-API-KEY auth_fn (env/.env, never logged)."""
    from dataclasses import replace
    from scripts.constants import BASE_URL
    from scripts.sources.common import FD_API_POLICY
    return _deep_probe(f"{BASE_URL}/company/facts?ticker=AAPL",
                       replace(FD_API_POLICY, max_retries=1),
                       "FINANCIAL_DATASETS_API_KEY")


def _deep_probe_fmp(key: str) -> tuple[str, str]:
    """1 FMP request (profile — the canonical minimal-quota auth smoke)."""
    from dataclasses import replace
    from scripts.constants import FMP_BASE_URL
    from scripts.sources.common import FMP_POLICY
    return _deep_probe(f"{FMP_BASE_URL}/profile/AAPL?apikey={key}",
                       replace(FMP_POLICY, max_retries=1), "FMP_API_KEY")


def doctor(root: Path, *, deep: bool = False) -> int:
    """One-shot diagnosis: single-root consistency (the SAME graded
    config_gate guard the runtime uses, both surfaces), venv interpreter,
    Python ≥3.10 + deps via $PYBIN, config readiness (config_version stamp +
    personalization + portfolio-state floor via config_gate's own asserts),
    key shape, and provider reachability. `--deep` adds ONE minimal-quota
    authenticated request per configured provider (cost printed first).
    Reports + points at fixes only — never mutates anything."""
    import contextlib
    import io
    from urllib.parse import urlsplit

    from scripts import config_gate as CG

    counts = {_OK: 0, _WARN: 0, _FAIL: 0}

    def emit(status: str, label: str, detail: str = "") -> None:
        counts[status] += 1
        print(f" {_SYM[status]} {label}" + (f" — {detail}" if detail else ""))

    print(f"stock-v7 doctor — root: {root}")

    # 1. Single-root consistency: BOTH graded surfaces through the runtime's
    #    own guard (config_gate.single_root_guard) — one truth table, so
    #    doctor and the runtime gate can never disagree (Plan B 4c).
    for portfolio, label in (
            (False, "single-root guard (base surface: analysis/discovery skills)"),
            (True, "single-root guard (portfolio surface: /portfolio, /monitor)")):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                rc = CG.single_root_guard(root, portfolio=portfolio)
        except Exception as e:  # guard crash = cannot verify → fail-closed
            emit(_FAIL, label, f"guard crashed ({type(e).__name__}: {e})")
            continue
        msg = " ".join(buf.getvalue().split())
        if rc != 0:
            emit(_FAIL, label, msg or "blocked")
        elif msg:
            emit(_WARN, label, msg)
        else:
            emit(_OK, label)

    # 2. Venv interpreter ($PYBIN) — the skills run `$ROOT/.venv`, so deps are
    #    checked THERE; system-python OK ≠ the Cowork skill's venv OK.
    pybin = _resolve_pybin(root)
    if pybin is not None:
        emit(_OK, "venv interpreter", str(pybin))
        eff = str(pybin)
    else:
        eff = sys.executable
        if CG._is_cowork_root(root):  # the runtime's own Cowork detector (one implementation)
            emit(_WARN, "venv interpreter",
                 f"no .venv in this Cowork mount — run /stock-v7-setup to create it "
                 f"(skills fall back to system python3; checking {eff})")
        else:
            emit(_OK, "venv interpreter",
                 f"no .venv — using the running interpreter {eff} "
                 f"(system install is supported outside Cowork)")

    # 3. Python ≥3.10 — of the EFFECTIVE interpreter, probed via subprocess.
    ver = _interpreter_version(eff)
    if ver is None:
        emit(_FAIL, "python >= 3.10",
             f"could not execute {eff} — recreate the venv (/stock-v7-setup) or fix PATH")
    elif ver >= (3, 10):
        emit(_OK, "python >= 3.10", f"{'.'.join(map(str, ver))} ({eff})")
    else:
        emit(_FAIL, "python >= 3.10",
             f"found {'.'.join(map(str, ver))} at {eff} — install Python >=3.10, "
             f"then recreate the venv / reinstall deps")

    # 4. Runtime deps — imported INSIDE $PYBIN.
    deps_ok, deps_err = _interpreter_imports_ok(eff, _DOCTOR_DEP_MODULES)
    deps_label = f"deps ({', '.join(_DOCTOR_DEP_MODULES)}) via {eff}"
    if deps_ok:
        emit(_OK, deps_label)
    else:
        emit(_FAIL, deps_label,
             f"{deps_err} — fix: {eff} -m pip install -r requirements.txt")

    # 5. Config readiness — config_gate's OWN asserts (config_version stamp,
    #    personalized mandate.edge, required keys; portfolio-state floor).
    base_label = ("config (strategy.yaml: setup.config_version stamp + "
                  "personalized + required keys)")
    try:
        CG.assert_money_path_ready(root)
        emit(_OK, base_label)
    except ValueError as e:        # ConfigError(ValueError) — message names the fix
        emit(_FAIL, base_label, str(e))
    except Exception as e:         # unparseable/import trouble = cannot verify → fail-closed
        emit(_FAIL, base_label, f"check crashed ({type(e).__name__}: {e})")
    try:
        CG.assert_portfolio_state_ok(root)
        emit(_OK, "portfolio-state.yaml structural floor")
    except ValueError as e:
        emit(_FAIL, "portfolio-state.yaml structural floor", str(e))
    except Exception as e:
        emit(_FAIL, "portfolio-state.yaml structural floor",
             f"check crashed ({type(e).__name__}: {e})")

    # 6. Key shape — the runtime's own resolution (env > .env, placeholder-
    #    rejecting `_key_available`), per key for actionable lines.
    fds_key = fmp_key = ""
    try:
        fds_key = CG._key_available(root, "FINANCIAL_DATASETS_API_KEY")
        fmp_key = CG._key_available(root, "FMP_API_KEY")
        finnhub_key = CG._key_available(root, "FINNHUB_API_KEY")
    except OSError as e:
        emit(_FAIL, "API keys", f".env unreadable ({e})")
        finnhub_key = ""
    else:
        for name, val, required in (
                ("FINANCIAL_DATASETS_API_KEY", fds_key, True),
                ("FMP_API_KEY", fmp_key, True),
                ("FINNHUB_API_KEY", finnhub_key, False)):
            if val:
                emit(_OK, f"API key {name}", "set (non-placeholder)")
            elif required:
                emit(_FAIL, f"API key {name}",
                     f"missing or placeholder ('{ENV_SENTINEL}') — edit .env, "
                     f"then run /stock-v7-setup (bootstrap --confirm)")
            else:
                emit(_WARN, f"API key {name}",
                     "not set (optional — news fallback disabled)")

    # 7. Network, default tier (non-spending: no API key is sent).
    from scripts.constants import BASE_URL, FMP_BASE_URL
    for base in (BASE_URL, FMP_BASE_URL):
        host = urlsplit(base).hostname or base
        ok, detail = _probe_provider_host(host)
        emit(_OK if ok else _FAIL, f"network {host}",
             detail + ("" if ok else " — check connectivity / DNS / proxy"))

    # 8. Deep tier (opt-in): ONE authenticated request per CONFIGURED
    #    provider. Quota cost is printed BEFORE any spending call.
    if deep:
        configured = [n for n, k in (("api.financialdatasets.ai", fds_key),
                                     ("financialmodelingprep.com", fmp_key)) if k]
        print(f" deep probe: consuming 1 authenticated request per configured "
              f"provider ({', '.join(configured) if configured else 'none configured'})")
        if fds_key:
            status, detail = _deep_probe_fds()
            emit(status, "deep auth probe api.financialdatasets.ai (1 request)", detail)
        else:
            emit(_WARN, "deep auth probe api.financialdatasets.ai",
                 "skipped — no key configured")
        if fmp_key:
            status, detail = _deep_probe_fmp(fmp_key)
            emit(status, "deep auth probe financialmodelingprep.com (1 request)", detail)
        else:
            emit(_WARN, "deep auth probe financialmodelingprep.com",
                 "skipped — no key configured")

    print(f"doctor: {counts[_OK]} ok, {counts[_WARN]} warn, {counts[_FAIL]} fail")
    return 1 if counts[_FAIL] else 0


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
    b_meg = b.add_mutually_exclusive_group()
    b_meg.add_argument("--yes", action="store_true", help="Non-interactive (copy examples, skip prompts).")
    b_meg.add_argument("--confirm", action="store_true",
                       help="Verify personalized config + stamp setup block + write home marker.")
    b.add_argument("--copy", action="store_true", help="Copy skills instead of symlinking.")

    s = sub.add_parser("sync", help="(Re)generate per-agent adapters.")
    s.add_argument("--host", help="Comma-separated hosts (default: auto-detect).")
    s.add_argument("--all", action="store_true", help="All known hosts.")
    s.add_argument("--copy", action="store_true", help="Copy instead of symlink.")

    sub.add_parser("check", help="Verify adapters (CI-safe, exit≠0 on problem).")
    sub.add_parser("uninstall", help="Remove generated adapters (keeps user config).")

    doc = sub.add_parser(
        "doctor",
        help="One-shot env/config/deps/network diagnosis (reports + names "
             "fixes; never mutates).")
    doc.add_argument(
        "--deep", action="store_true",
        help="Also make ONE minimal authenticated API request per configured "
             "provider — consumes 1 financialdatasets.ai request + 1 FMP "
             "request of your quota (cost is printed before the calls).")

    args = parser.parse_args(argv)
    root = repo_root()

    if args.command == "bootstrap":
        rc = bootstrap(root, yes=args.yes, prefer_copy=args.copy)
        if rc != 0:
            # bootstrap itself failed (e.g. Python version preflight) — do NOT confirm/stamp on top of a
            # failed setup (that would mark money-path ready over a broken environment). Fail-closed.
            return rc
        if args.confirm:
            # Post-bootstrap confirm: verify personalized config + stamp + write home.
            # Import lazily to preserve zero-dep module top.
            from scripts.config_gate import ConfigError
            try:
                confirm_setup(root)
                print("\nSetup confirmed. All money-path skills are ready.")
            except ConfigError as e:
                print(f"\nsetup blocked: {e}", file=sys.stderr)
                return 1
            return 0
        if not args.yes:
            # Seed-only run: remind the user of the next step.
            print(
                "\nEdit strategy.yaml (mandate.edge is required; principles optional — defaults supported), "
                "fill portfolio-state.yaml, put your API keys in .env, "
                "then run: distribute bootstrap --confirm"
            )
        return rc
    if args.command == "sync":
        return sync(root, _parse_hosts(args.host, args.all), args.copy)
    if args.command == "check":
        return check(root)
    if args.command == "uninstall":
        return uninstall(root)
    if args.command == "doctor":
        return doctor(root, deep=args.deep)
    return 2


if __name__ == "__main__":
    sys.exit(main())
