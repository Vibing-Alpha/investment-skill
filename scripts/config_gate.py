# scripts/config_gate.py
"""LIGHT money-path config-confirmation gate (spec §3.5). Fail-CLOSED: a money-path run must not
proceed on unconfirmed / example-default / unverifiable config. stdlib + PyYAML (a runtime dep)."""
from __future__ import annotations
from pathlib import Path

CONFIG_VERSION = 1   # stdlib-safe: importable by zero-dep distribute.py (no yaml at module top)


class ConfigError(ValueError):
    """Config missing / unparseable / unconfirmed / unverifiable — caught via `except ValueError`.
    Follows the project's `<Error>(ValueError)` pattern but is NAMED distinctly from
    `scripts.schemas.errors.SchemaError` (which has a different 3-arg `(artifact, field, message)`
    constructor) to avoid a same-name / different-signature clash — producer-consumer clarity."""


def _load_yaml(path: Path) -> dict:
    import yaml   # lazy: keeps `from scripts.config_gate import CONFIG_VERSION` import-safe without PyYAML
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"{path.name}: not found") from e
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"{path.name}: unparseable ({type(e).__name__}: {e})") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: top level is not a mapping (got {type(data).__name__})")
    return data


def _field(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# `mandate.edge` is the ONE example field used as the "did the user personalize" backstop — it's the
# only consumed field whose shipped-example value is NON-neutral. `principles` is NOT used: the example
# ships it comment-only (`principles:` -> None) and says "Remove this section to use defaults", so
# default-principles is a SUPPORTED mode (checking it would false-block). `scoring.dimension_weights`
# example == code DEFAULT_WEIGHTS (neutral), also excluded.
def normalized_edge(strategy_path: Path):
    """`mandate.edge` normalized the way the consumer (`screen.py`: `str(e).strip().lower()` + drop
    falsy) sees it: a sorted list of non-blank lower-cased sectors, or None if it isn't a list. So a
    case/whitespace-only "edit" of the example, or `[""]`/`["  "]`, is seen as NOT personalized."""
    edge = _field(_load_yaml(strategy_path), "mandate.edge")
    if not isinstance(edge, list):
        return None
    return sorted(s.strip().lower() for s in edge if isinstance(s, str) and s.strip())


def unedited_example_fields(strategy_path: Path) -> list[str]:
    """Returns `["mandate.edge"]` if it's still (NORMALIZED) equal to the shipped example's — so a
    decorative case/whitespace edit does NOT count as personalization — else `[]`. FAIL-CLOSED: if the
    example file is missing/unreadable we cannot verify, so raise (the caller blocks)."""
    example = strategy_path.parent / "strategy.example.yaml"
    if not example.exists():
        raise ConfigError("cannot verify config: strategy.example.yaml is missing (cannot compare)")
    # only `mandate.edge`; compare its CONSUMER-normalized form (not raw bytes)
    return ["mandate.edge"] if normalized_edge(strategy_path) == normalized_edge(example) else []


# append to scripts/config_gate.py
import os
from datetime import datetime

REQUIRED_KEYS = ("FINANCIAL_DATASETS_API_KEY", "FMP_API_KEY")   # CLAUDE.md setup: both required (FINNHUB optional)


def _require_iso(value, field: str) -> None:
    if not isinstance(value, str):
        raise ConfigError(f"strategy.yaml setup.{field} must be an ISO-8601 string (got {type(value).__name__})")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ConfigError(f"strategy.yaml setup.{field} is not a valid timestamp: {value!r}") from e


_PLACEHOLDER_KEYS = frozenset({"", "your_key_here"})   # .env.example sentinel — must NOT pass as a real key


def _clean_key(x: str | None) -> str:
    """Mirror `common._load_env`'s parse EXACTLY — `.strip().strip('"').strip("'")`, NO inline-comment
    stripping (the runtime does NOT strip comments, so a `.env` value like `real # note` is used
    LITERALLY by the runtime and is a broken key). Then map to "" (BLOCK) anything that cannot be a real
    key: the placeholder (or any value containing it), or a value with internal whitespace/`#` (a real
    API key has none, and the runtime would otherwise use it verbatim as a broken key — gate must not
    say ready while the runtime breaks)."""
    x = (x or "").strip().strip('"').strip("'").strip()
    if x in _PLACEHOLDER_KEYS or "your_key_here" in x:
        return ""
    if any(c in x for c in " \t#"):
        return ""
    return x


def _key_available(root: Path, name: str) -> str:
    """Mirror the RUNTIME's env resolution. `common._load_env` uses `os.environ.setdefault`, so a
    PRESENT env var (even ""/placeholder) is what the runtime actually uses — `.env` only fills a key
    that is ABSENT from the environment (verified: scripts/sources/common.py `_load_env`). So:
    if `name` is in the environment, validate THAT value (empty/placeholder → "" → BLOCK, because the
    runtime would use it, NOT `.env`); only if `name` is absent do we read the root `.env`. Rejects
    `your_key_here` + inline comments + quotes. This prevents a gate false-positive where the gate
    falls through an empty env var to a real `.env` key the runtime will never load."""
    if name in os.environ:                       # runtime precedence: env wins (setdefault); .env won't override
        return _clean_key(os.environ[name])
    env_file = root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, val = line.partition("=")
                if k.strip() == name:
                    return _clean_key(val)
    return ""


def assert_personalized_and_ready(root: Path) -> None:
    """The BASE money-path precondition (analysis + discovery skills: /score-business, /investment-thesis,
    /screen-stocks): the user personalized `mandate.edge` and the required API keys are present. Does NOT
    check portfolio-state (irrelevant to single-ticker work — see `assert_portfolio_state_ok`).
    Fail-CLOSED. `confirm_setup` calls this before stamping, so a confirmed config always passes the base."""
    strategy = root / "strategy.yaml"
    unedited = unedited_example_fields(strategy)              # raises (fail-closed) if example unverifiable
    if unedited:
        raise ConfigError(f"strategy.yaml is still the example ({', '.join(unedited)}) — edit it, then run /stock-v7-setup")
    edge = _field(_load_yaml(strategy), "mandate.edge")
    # require a real list of NON-BLANK STRINGS — `["mine", 123]` would make screen emit a "123" theme
    # (it does str(e).strip().lower()); `[]`/`[""]`/non-list is unconfigured.
    if not (isinstance(edge, list) and edge and all(isinstance(x, str) and x.strip() for x in edge)):
        raise ConfigError("strategy.yaml mandate.edge must be a non-empty list of sector strings (no blanks/non-strings) — run /stock-v7-setup")
    missing = [k for k in REQUIRED_KEYS if not _key_available(root, k)]
    if missing:
        raise ConfigError(f"missing required API key(s): {', '.join(missing)} — add to `.env`, then run /stock-v7-setup")


def _is_finite_num(x) -> bool:
    return type(x) in (int, float) and x == x and x not in (float("inf"), float("-inf"))  # excludes bool/NaN/Inf


def _is_pos_num(x) -> bool:
    return _is_finite_num(x) and x > 0


def assert_portfolio_state_ok(root: Path) -> None:
    """The ADDITIONAL precondition for PORTFOLIO-level skills (/portfolio, /monitor). This is the
    STRUCTURAL FLOOR that prevents a consumer CRASH or a SILENTLY-INVISIBLE position — NOT the full
    semantic/constraint validation (that stays in `validate.py:validate_portfolio` at /portfolio time).
    Fail-CLOSED. Not applied to single-ticker analysis/discovery skills."""
    pstate = _load_yaml(root / "portfolio-state.yaml")        # raises ConfigError if missing/unparseable/non-mapping
    holdings = pstate.get("holdings")
    holdings_ok = isinstance(holdings, dict) and all(
        isinstance(k, str) and k.strip() and isinstance(v, dict) and _is_pos_num(v.get("shares"))
        for k, v in holdings.items())
    watchlist = pstate.get("watchlist")
    watchlist_ok = watchlist is None or (isinstance(watchlist, list)
                                         and all(isinstance(t, str) and t.strip() for t in watchlist))
    open_orders = pstate.get("open_orders")
    def _order_ok(o):
        # Price-field vocabulary mirrors validate._order_price (est_price /
        # limit_price / price) — cold review 2026-06-11 R8: the preflight
        # rejected a broker-synced {type: limit, limit_price: X} open order
        # that the validator downstream prices fine.
        return (isinstance(o, dict) and isinstance(o.get("ticker"), str) and o["ticker"].strip()
                and isinstance(o.get("type"), str) and _is_pos_num(o.get("shares"))
                and (_is_pos_num(o.get("price")) or _is_pos_num(o.get("est_price"))
                     or _is_pos_num(o.get("limit_price"))))
    orders_ok = open_orders is None or (isinstance(open_orders, list) and all(_order_ok(o) for o in open_orders))
    cash = pstate.get("cash")
    if not (_is_finite_num(cash) and cash >= 0 and holdings_ok and watchlist_ok and orders_ok):
        raise ConfigError("portfolio-state.yaml malformed — need a finite `cash` >= 0; `holdings` as "
                          "{TICKER: {shares: <positive number>, ...}}; `watchlist` a list[str] if present; "
                          "`open_orders` items with ticker/type/positive shares/positive price — run /stock-v7-setup")


def assert_money_path_ready(root: Path, *, require_portfolio: bool = False) -> None:
    """Fail-CLOSED precondition for a money-path run. The BASE level (analysis/discovery) checks setup +
    personalized `mandate.edge` + keys; `require_portfolio=True` (for /portfolio, /monitor) ALSO validates
    portfolio-state. Raises ConfigError (a ValueError) with a /stock-v7-setup-pointing message."""
    strategy = root / "strategy.yaml"
    setup = _load_yaml(strategy).get("setup")
    if not isinstance(setup, dict) or "config_version" not in setup or not setup.get("confirmed_at"):
        raise ConfigError("strategy.yaml is not confirmed (no `setup.config_version`) — run /stock-v7-setup")
    cv = setup["config_version"]
    if type(cv) is not int:           # reject "1", 1.0, True — bool is an int subclass, so use exact type
        raise ConfigError(f"strategy.yaml setup.config_version must be an int (got {cv!r}) — run /stock-v7-setup")
    if cv > CONFIG_VERSION:
        raise ConfigError("strategy.yaml config_version is newer than this code — `git pull` / update first")
    if cv < CONFIG_VERSION:
        raise ConfigError("strategy.yaml config_version is older than this code — re-run /stock-v7-setup to re-confirm")
    _require_iso(setup["confirmed_at"], "confirmed_at")
    assert_personalized_and_ready(root)                      # base: setup + mandate.edge + keys
    if require_portfolio:
        assert_portfolio_state_ok(root)                      # portfolio-level skills only


import sys


def _default_root() -> Path:
    """The gate's OWN repo — the same root the runtime resolves via `common._find_project_root`
    (also __file__-anchored). Module-level seam so tests can inject a running root (incl. a
    /sessions/*/mnt/* shaped one for the Cowork-detection branch) through the REAL guard path."""
    return Path(__file__).resolve().parent.parent


def _is_cowork_root(root: Path) -> bool:
    """Cowork session mounts live at /sessions/<id>/mnt/<folder>/... (empirically-confirmed layout —
    a path-shape heuristic by design; do NOT add a sentinel-file alternative, anti-ratchet). In Cowork
    there is no persistent $HOME, so no `~/.stock-v7-home` marker can exist — the prelude's
    single-mount fail-closed is what establishes the single root there."""
    parts = Path(root).resolve().parts
    return len(parts) >= 5 and parts[:2] == ("/", "sessions") and parts[3] == "mnt"


def _same_root(a: Path, b: Path) -> bool:
    """Hardened root comparison: `os.path.samefile` when both exist (symlinks/junctions/case),
    else a normcase-folded resolve compare — Windows is case-insensitive and git-bash path forms
    differ, so a raw `resolve() !=` can FALSE-mismatch (spurious block) or FALSE-match (missed
    wrong-clone)."""
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _single_root_guard(root: Path, *, portfolio: bool) -> int:
    """GRADED runtime single-root guard (Plan B 4c): the failure mode is a money-path run reading the
    WRONG clone's portfolio, so portfolio-level checks (--portfolio) BLOCK on any unconfirmed /
    unresolved / mismatched single-root, while single-ticker/discovery checks WARN + proceed (their
    blast radius is a stale per-ticker analysis, not a trade). Setup-time keeps the HARD invariant
    (`confirm_setup` -> `find_conflicts`); only this runtime guard is graded — documented in
    `rules/portfolio-safety.md` §"Single-root guard". Returns 0 = proceed, 1 = block."""
    import scripts.root_resolve as rr

    def _graded(block_msg: str) -> int:
        if portfolio:
            print(f"stock-v7: {block_msg}", file=sys.stderr)
            return 1
        print(f"stock-v7: WARNING: {block_msg} — proceeding (portfolio-level skills still block)",
              file=sys.stderr)
        return 0   # fail-open-ok: graded downgrade (Plan B 4c) — non-portfolio surface only; --portfolio blocks above

    # key the marker-absent grade off root_source(), NEVER off resolve_root's silently-defaulted path
    if rr.root_source(home_file=rr.HOME_FILE) == "absent":
        if _is_cowork_root(root):
            return 0    # Cowork: prelude single-mount fail-closed established the root; no marker to require
        return _graded("no confirmed single-root — run /stock-v7-setup")
    try:
        canonical = rr.resolve_root(home_file=rr.HOME_FILE)
    except ValueError as e:
        # relative/corrupt STOCK_V7_HOME / marker — canonical is UNRESOLVED, treat as broken root config
        return _graded(str(e))
    if not _same_root(canonical, root) and rr._is_fund_state(canonical):
        return _graded(f"confirmed root is {canonical.resolve()}, but you're running from {root.resolve()} "
                       f"— `cd` to the confirmed root, or re-run /stock-v7-setup here")
    return 0


def single_root_guard(root: Path, *, portfolio: bool) -> int:
    """PUBLIC seam for diagnostics (`distribute doctor`, Plan B Task 6): the SAME graded runtime
    guard `check` runs — ONE truth table, so doctor and the runtime gate can never disagree
    (producer-consumer §3: no duplicated implementation). Returns 0 = proceed, 1 = block; the
    warn/block reason goes to stderr exactly as at runtime."""
    return _single_root_guard(root, portfolio=portfolio)


def main(argv: list[str]) -> int:
    """`check [--root PATH]`: by DEFAULT checks the gate's OWN repo — `Path(__file__).resolve().parent.parent`
    — the SAME root the runtime resolves via `common._find_project_root` (also __file__-anchored), so the
    gate and the API layer can NEVER diverge onto different `.env`/config. The Plan-B prelude runs `check`
    with NO `--root` (it has already `cd`'d into the clone, which IS this repo). `--root` is an explicit
    override for tests/tooling only. STRICT parse — missing `--root` value / unknown / extra arg → exit 2;
    `--help` → 0. Exit 0 = ready, 1 = blocked (actionable stderr)."""
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m scripts.config_gate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--root", type=Path, default=None)
    chk.add_argument("--portfolio", action="store_true",     # portfolio-level skills ALSO validate portfolio-state
                     help="also validate portfolio-state.yaml (for /portfolio, /monitor)")
    try:
        args = parser.parse_args(argv)            # argparse raises SystemExit: code 2 on bad args, 0 on --help
    except SystemExit as e:
        return int(e.code or 0)
    root = args.root if args.root is not None else _default_root()
    if args.root is None:        # production path only (explicit --root in tests/tooling skips this guard)
        # GRADED single-root RUNTIME guard: a confirmed canonical root that is a DIFFERENT fund-state /
        # an unresolvable marker / (non-Cowork) no marker at all → --portfolio BLOCKS, base WARNS +
        # proceeds. See _single_root_guard. Plan B's prelude `cd`s into the canonical root so this
        # normally passes silently.
        rc = _single_root_guard(root, portfolio=args.portfolio)
        if rc:
            return rc
    try:
        assert_money_path_ready(root, require_portfolio=args.portfolio)
    except ConfigError as e:
        print(f"stock-v7: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
