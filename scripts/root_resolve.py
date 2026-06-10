# scripts/root_resolve.py
"""Resolve the single canonical data root (the clone) + detect divergent fund states (spec §3.2).
NEVER probes cwd or scans $HOME — only explicit candidates. stdlib only."""
from __future__ import annotations
import os
from pathlib import Path

HOME_FILE = Path.home() / ".stock-v7-home"


def _source_and_raw(home_file: Path) -> tuple[str, str]:
    """ONE implementation of the env > marker > absent precedence (shared by `resolve_root` and
    `root_source` — producer-consumer rule §3: no duplicated precedence logic). An empty/whitespace
    env var or marker file counts as unset/absent. Does NOT validate the raw value."""
    env = (os.environ.get("STOCK_V7_HOME") or "").strip()
    if env:
        return "env", env
    if home_file.exists():
        try:
            line = home_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            # permission-denied / non-UTF-8 marker: surface as the SAME ValueError family resolve_root
            # documents for a corrupt marker, so config_gate GRADES it (warn+0 / block+1) instead of
            # crashing the guard with an unhandled traceback (codex post-impl Fix 2)
            raise ValueError(
                f"unreadable ~/.stock-v7-home marker ({home_file}): {type(e).__name__}: {e}") from e
        if line:
            return "marker", line
    return "absent", ""


def root_source(*, home_file: Path = HOME_FILE) -> str:
    """Where `resolve_root`'s answer comes from: "env" | "marker" | "absent". Closes the API gap where
    `resolve_root` silently returns the DEFAULT path on an absent marker — the graded single-root guard
    (config_gate, Plan B 4c) keys its marker-absent grading off this, never off the defaulted path.
    A present-but-CORRUPT (e.g. relative) value still reports "env"/"marker" — the caller reaches
    `resolve_root`'s ValueError and grades it as corrupt, not absent. Same contract for a
    present-but-UNREADABLE marker (non-UTF-8 / permission-denied): report "marker" here, let
    `resolve_root` raise the graded ValueError."""
    try:
        return _source_and_raw(home_file)[0]
    except ValueError:
        return "marker"   # present-but-unreadable: caller reaches resolve_root's ValueError and grades it


def resolve_root(*, home_file: Path = HOME_FILE, default: Path | None = None) -> Path:
    source, raw = _source_and_raw(home_file)
    if source == "env":
        p = Path(raw).expanduser()                    # honor ~
        if not p.is_absolute():
            # the prelude `cd`s here from an ARBITRARY cwd — a relative root would point at different
            # dirs per cwd, breaking single-root. Fail-closed rather than resolve against an accidental cwd.
            raise ValueError(f"STOCK_V7_HOME must be an absolute path (got {raw!r})")
        return p
    if source == "marker":
        p = Path(raw).expanduser()                    # write_home persists absolute, but a hand-edited /
        if not p.is_absolute():                       # corrupted marker could be relative — fail-closed
            raise ValueError(f"~/.stock-v7-home must contain one absolute path (got {raw!r})")
        return p
    return default if default is not None else (Path.home() / "Claude" / "stock-v7")


def write_home(root: Path, *, home_file: Path = HOME_FILE) -> None:
    home_file.parent.mkdir(parents=True, exist_ok=True)
    # ALWAYS persist an absolute path — the per-step prelude `cd`s here from an arbitrary cwd, so a
    # relative root would break (contract: ~/.stock-v7-home holds one absolute path).
    home_file.write_text(str(Path(root).expanduser().resolve()) + "\n", encoding="utf-8")


def _is_fund_state(p: Path) -> bool:
    # a real stock-v7 root holds portfolio-state.yaml AND a repo marker. Accept CLAUDE.md OR .git: a
    # published-tarball / non-git clone has CLAUDE.md but no .git, and must still count.
    return p.is_dir() and (p / "portfolio-state.yaml").is_file() and (
        (p / ".git").exists() or (p / "CLAUDE.md").is_file())


def find_conflicts(*, candidates: list[Path]) -> list[Path]:
    """The candidate roots that hold a real fund state (repo marker AND portfolio-state.yaml),
    deduped by resolved path. The caller hard-fails when len(result) > 1 (spec §3.2)."""
    seen: list[Path] = []
    seen_resolved: set[Path] = set()
    for c in candidates:
        rc = c.resolve()
        if _is_fund_state(c) and rc not in seen_resolved:
            seen.append(c); seen_resolved.add(rc)
    return seen
