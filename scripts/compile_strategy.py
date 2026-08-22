"""Deterministic compilation of `strategy.yaml` -> `strategy.compiled.yaml`.

Plan: docs/superpowers/plans/2026-08-12-risk-control-completion.md §3.

F4 was: hard constraints were extracted from the *prose* of `principles` by
the model, and `source_hash` covered only `principles`. So a policy value
written under `risk:` was never read, produced a cache hit, and the decision
log kept attesting the superseded policy — a money-path fail-open.

This module is the fix's load-bearing half: **`hard_constraints` is a pure
function of the `risk:` block, executed by code.** A pure function performed by
a model reading a paragraph is not a pure function, and nothing downstream can
be tested against one.

There is ONE path, and it produces a COMPLETE artifact every time. Earlier
revisions did not: they bailed out (exit 2) on an empty `principles`, or
compiled `soft_principles: []` and left a caller to patch the file afterwards.
Both handed part of the policy back to free-form orchestration — the exact
failure layer this module exists to remove — and both left the SHIPPED
configuration (`strategy.example.yaml`, `principles:` comment-only, live
`risk.max_single_position`) with an artifact that misreported its own policy.
`portfolio_log` skips citation-range validation on an empty principle list, so
a decision citing `#99` was accepted into the audit trail.

So the canonical defaults are compiled here too (`_DEFAULT_PRINCIPLES`).

Exit codes:

  0  compiled and written atomically.
  1  the configuration is INVALID and the run must stop. `config_gate` does not
     validate `principles` or `risk:`, so this is the only gate on malformed
     policy.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path

import yaml

from scripts.cli_utils import normalize_percent_fraction, normalize_ticker
from scripts.schemas.strategy import canonical_policy_hash, derive_etf_entry_enabled

EXIT_OK = 0
EXIT_INVALID = 1

# Mirrors scripts.schemas.strategy._KNOWN_CONSTRAINT_KEYS. Kept explicit here
# because this is the *authoring* boundary: an unknown key must be refused
# before it can be silently dropped from the projection, which is F4's exact
# signature (`risk: {min_cahs: 0.05}` normalizes to nothing and the operator's
# intended floor never binds).
_KNOWN_RISK_KEYS = ("max_single_position", "max_sector", "min_cash",
                    "max_holdings")
# `max_holdings` is an integer count, not a fraction — it must not be divided
# by 100 (rules/units.md).
_FRACTION_KEYS = frozenset({"max_single_position", "max_sector", "min_cash"})


# The canonical default principles, applied when `strategy.yaml` has no
# `principles:` — a mode `config_gate.py:41` documents as SUPPORTED.
#
# They live HERE, in code, and `rules/portfolio-safety.md` documents them.
# The reverse split was tried and failed five review rounds running: with the
# text only in prose, the compiler had to emit `soft_principles: []` and hand
# the artifact back to free-form orchestration to repair. That left the SHIPPED
# configuration (`strategy.example.yaml`, `principles:` comment-only) with an
# artifact that misreported its own policy, and `portfolio_log` silently skips
# citation-range validation on an empty list — so a decision citing `#99` was
# accepted into the audit trail.
#
# Keep this list and the rule file's prose in step; the rule file says the code
# is authoritative so a drift is a doc fix, not an enforcement question.
_DEFAULT_PRINCIPLES = (
    # "all buys" / "all stops", NOT "all limit buys" / "all stop losses":
    # validate.py's extreme_down is proposed market sells + EVERY proposed and
    # open buy regardless of type + stop sells. The narrower wording told the
    # decision agent a stop_limit or gtc buy was out of scope when it is not.
    "After any proposed trade executes, the portfolio must survive an "
    "extreme scenario where all buys fill and all stops trigger.",
    "Weak technicals do not disqualify a fundamentally strong company, "
    "but require a larger margin of safety for entry.",
    # No numeral here: the window is configured as orders.earnings_window_days
    # (read straight from strategy.yaml each run, never compiled). A literal
    # "7 days" would instruct the decision agent to ignore a configured 14.
    "Within the configured earnings window (orders.earnings_window_days), "
    "do not chase price — use limit orders with a meaningful discount.",
    "Thesis falsification is sufficient reason to exit — do not wait "
    "for price confirmation.",
    "When a hard constraint is breached, use market sell immediately — "
    "do not use limit sells for better pricing.",
    "Firm conviction to buy or sell → market order. Want to buy but not "
    "urgent → limit order.",
    "Before placing new orders, check for contradicting existing GTC "
    "orders that should be canceled.",
    "When broad market trend deteriorates, raise cash allocation — do "
    "not mechanically reduce all positions equally.",
    "When cash significantly exceeds target, deploy proactively by "
    "capital efficiency ranking.",
    "Concentrate in sectors where you have a knowledge edge — do not "
    "diversify for diversification's sake.",
)


# C.POLICY_VERSION. The contract version of the `etf_policy` block, not a
# release number: a policy written against an older shape must be re-authored
# by the owner rather than reinterpreted by newer code.
ETF_POLICY_VERSION = 2

_KNOWN_ETF_POLICY_KEYS = frozenset({
    "version", "allow_non_leveraged_equity_etfs", "approved_equity_etfs",
    "merit_admission",
})

# V.MERIT, restricted to the values that admit a buy. `watch`/`pass`/`avoid`
# are merit verdicts meaning "do not enter"; admitting on one would let the
# policy authorize entry on its own refusal.
_MERIT_ADMISSION_ALLOWED = ("strong_add", "add")


class _DuplicateKeyLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that refuses duplicate mapping keys.

    PR.COMPILE_STRATEGY: "duplicate-key-rejecting YAML load before mapping
    construction". The stock loader keeps the LAST occurrence and says
    nothing — on `approved_equity_etfs` that silently discards a review the
    owner wrote, and the compiled artifact then looks exactly like a policy
    where the discarded line was never typed.
    """


def _no_duplicate_keys(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:  # unhashable key — construct_mapping will report it
            continue
        if duplicate:
            raise _Invalid(
                f"duplicate key {key!r} in strategy.yaml at line "
                f"{key_node.start_mark.line + 1}. YAML keeps the last one "
                f"silently; the earlier value would vanish from the compiled "
                f"policy with no diagnostic.")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: _no_duplicate_keys(loader, node))


class _Invalid(Exception):
    """Configuration the operator must fix; never routed to the fallback."""


def _project_risk(raw) -> dict:
    """`risk:` -> `hard_constraints`. Pure, total, and fail-closed."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _Invalid(
            f"`risk:` must be a mapping of constraint keys, got "
            f"{type(raw).__name__}. A scalar or list looks configured but is "
            f"inert — every constraint in it would be silently unenforced."
        )

    unknown = sorted(set(raw) - set(_KNOWN_RISK_KEYS))
    if unknown:
        # `cash_target` shipped in strategy.example.yaml for a long time while
        # having NO consumer, so an existing install can carry it. Personal
        # strategy.yaml files are gitignored and never migrated, so a bare
        # "unknown key" would strand that user with no idea what to do.
        hint = ""
        if "cash_target" in unknown:
            hint = (" — `cash_target` was an advertised key with no consumer "
                    "and has been removed; delete the line, nothing enforced "
                    "it")
        raise _Invalid(
            f"unknown key(s) inside `risk:`: {unknown}. Known keys: "
            f"{list(_KNOWN_RISK_KEYS)}. An unrecognised key is dropped from "
            f"the projection, so the constraint you intended would never "
            f"bind{hint}."
        )

    # `max_sector` is a schema-known key whose enforcement does NOT exist:
    # validate.py has no sector mapping and fails closed on it, so compiling it
    # guarantees `invalid_config` on EVERY run. The previously shipped example
    # carried `max_sector: 0.50`, and personal strategy.yaml files are
    # gitignored and never migrated — accepting the key would leave those
    # installs permanently blocked with a remediation ("remove the compiled
    # constraint") that Step 2 undoes on the next run.
    if raw.get("max_sector") is not None:
        raise _Invalid(
            "`risk.max_sector` cannot be enforced: sector mapping is not "
            "implemented, so validate.py fails closed on it and every run "
            "would refuse. Remove the line — it shipped in an older example "
            "and never enforced anything."
        )

    out = {}
    for key in _KNOWN_RISK_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        try:
            out[key] = (normalize_percent_fraction(value)
                        if key in _FRACTION_KEYS else value)
        except ValueError as exc:
            raise _Invalid(f"`risk.{key}`: {exc}") from exc
        if key == "max_holdings" and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise _Invalid(
                f"`risk.max_holdings` must be an integer >= 1, got {value!r}"
            )
    return out


def _check_principles(raw):
    """Returns the effective principle list. Raises if malformed.

    Empty means the operator configured none, which `config_gate.py:41`
    documents as a supported mode — so the CANONICAL DEFAULTS apply, and they
    are returned here rather than left for a caller to patch in. That keeps the
    artifact a complete, self-describing policy on every path.
    """
    if raw is None or raw == []:
        return list(_DEFAULT_PRINCIPLES)
    if not isinstance(raw, list):
        raise _Invalid(
            f"`principles:` must be a list of strings, got "
            f"{type(raw).__name__}. This is decision policy — it is injected "
            f"verbatim into authoring, so a malformed value must stop the run "
            f"rather than fall back to defaults."
        )
    bad = [p for p in raw if not isinstance(p, str)]
    if bad:
        raise _Invalid(
            f"`principles:` must contain only strings; found "
            f"{[type(b).__name__ for b in bad]}"
        )
    return raw


def _check_notes(raw) -> dict:
    """`principle_notes` is a mapping, copied verbatim. No type restriction.

    A string->string restriction was tried and REVERTED: the prior contract
    accepted any mapping and orchestration copied it, so narrowing it here
    breaks existing policies carrying nested notes or numeric metadata for no
    gain against the failure it was aimed at. The hash's type-erasure is
    handled where it actually shows up — the notes comparison in
    portfolio_log._verify_source_hash.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _Invalid(
            f"`principle_notes:` must be a mapping, got {type(raw).__name__}"
        )
    return raw


def _check_etf_policy(raw):
    """Project + validate `etf_policy:`. Returns the compiled block, or None.

    Absent means the owner has not opened the ETF path at all, which is a
    valid, complete policy — stock compilation is untouched and ETF entry
    stays off. Present-but-malformed is not: this block is the only record of
    which funds the owner reviewed, so a value that cannot be read must stop
    the run rather than degrade into "nothing approved", which looks
    identical to a deliberate empty list.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Invalid(
            f"`etf_policy:` must be a mapping, got {type(raw).__name__}")

    unknown = sorted(set(raw.keys()) - _KNOWN_ETF_POLICY_KEYS, key=repr)
    if unknown:
        raise _Invalid(
            f"`etf_policy:` has unknown key(s): {unknown}. Known keys: "
            f"{sorted(_KNOWN_ETF_POLICY_KEYS)}. A typo here compiles to an "
            f"authorization the owner did not write.")

    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _Invalid(
            f"`etf_policy.version:` must be the integer {ETF_POLICY_VERSION}, "
            f"got {version!r}")
    if version != ETF_POLICY_VERSION:
        raise _Invalid(
            f"`etf_policy.version:` is {version}, this build compiles "
            f"{ETF_POLICY_VERSION}. Re-author the block against the current "
            f"contract rather than letting newer code reinterpret it.")

    allow = raw.get("allow_non_leveraged_equity_etfs")
    if not isinstance(allow, bool):
        raise _Invalid(
            f"`etf_policy.allow_non_leveraged_equity_etfs:` must be an "
            f"explicit true or false, got {allow!r}. An absent flag is not a "
            f"false — inferring the safe value would hide the omission.")

    merit = raw.get("merit_admission")
    if not isinstance(merit, list) or not merit:
        raise _Invalid(
            f"`etf_policy.merit_admission:` must be a non-empty list drawn "
            f"from {list(_MERIT_ADMISSION_ALLOWED)}, got {merit!r}")
    bad = [m for m in merit if m not in _MERIT_ADMISSION_ALLOWED]
    if bad:
        raise _Invalid(
            f"`etf_policy.merit_admission:` contains {bad}, outside "
            f"{list(_MERIT_ADMISSION_ALLOWED)}. The other merit verdicts mean "
            f"'do not enter'.")
    if len(set(merit)) != len(merit):
        raise _Invalid(
            f"`etf_policy.merit_admission:` contains duplicates: {merit}")

    approved_raw = raw.get("approved_equity_etfs")
    if approved_raw is None:
        approved_raw = {}
    if not isinstance(approved_raw, dict):
        raise _Invalid(
            f"`etf_policy.approved_equity_etfs:` must be a mapping of ticker "
            f"-> {{reviewed_on: <ISO date>}}, got "
            f"{type(approved_raw).__name__}")

    approved: dict[str, dict] = {}
    today = _dt.date.today()
    for ticker_raw, approval in approved_raw.items():
        try:
            ticker = normalize_ticker(ticker_raw)
        except ValueError as exc:
            raise _Invalid(
                f"`etf_policy.approved_equity_etfs:` key {ticker_raw!r} is not "
                f"a usable ticker: {exc}") from exc
        if ticker in approved:
            # Two spellings project onto one key; keeping either silently
            # discards a review date the owner believes is in force.
            raise _Invalid(
                f"`etf_policy.approved_equity_etfs:` has two keys that both "
                f"canonicalize to {ticker}. Keep one.")
        if not isinstance(approval, dict):
            raise _Invalid(
                f"`etf_policy.approved_equity_etfs.{ticker}:` must be a "
                f"mapping containing exactly `reviewed_on`, got "
                f"{type(approval).__name__}")
        if set(approval.keys()) != {"reviewed_on"}:
            raise _Invalid(
                f"`etf_policy.approved_equity_etfs.{ticker}:` must contain "
                f"exactly `reviewed_on`, got {sorted(map(str, approval))}")
        reviewed_on = approval["reviewed_on"]
        # A YAML date literal arrives as a date object; both spellings are the
        # same authorization, so accept it and emit the canonical string.
        if isinstance(reviewed_on, _dt.date) and not isinstance(reviewed_on, _dt.datetime):
            parsed = reviewed_on
        elif isinstance(reviewed_on, str):
            try:
                parsed = _dt.date.fromisoformat(reviewed_on)
            except ValueError as exc:
                raise _Invalid(
                    f"`etf_policy.approved_equity_etfs.{ticker}.reviewed_on:` "
                    f"is not a valid ISO date (YYYY-MM-DD): {reviewed_on!r}"
                ) from exc
        else:
            raise _Invalid(
                f"`etf_policy.approved_equity_etfs.{ticker}.reviewed_on:` must "
                f"be an ISO date (YYYY-MM-DD), got {reviewed_on!r}")
        if parsed > today:
            raise _Invalid(
                f"`etf_policy.approved_equity_etfs.{ticker}.reviewed_on:` is "
                f"{parsed.isoformat()}, in the future. A review that has not "
                f"happened cannot authorize anything.")
        approved[ticker] = {"reviewed_on": parsed.isoformat()}

    return {
        "version": version,
        "allow_non_leveraged_equity_etfs": allow,
        "approved_equity_etfs": approved,
        "merit_admission": list(merit),
    }


def _write_atomically(path: Path, doc: dict) -> None:
    """os.replace after a same-directory temp write.

    Nothing partial ever lands, and a crash mid-write leaves the previous
    policy intact rather than a truncated file every consumer would refuse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(doc, handle, allow_unicode=True, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def compile_strategy(source: dict) -> dict:
    """Pure projection: source policy -> the compiled artifact's content."""
    # `risk:` is validated FIRST, before the principles branch, and
    # deliberately so. Only `soft_principles` needs the default-principles
    # fallback; `risk:` never does. Checking principles first would let a
    # `risk: {min_cahs: 0.05}` typo escape into free-form orchestration
    # whenever `principles` is null — which is the shipped example's shape,
    # and F4's exact signature.
    hard_constraints = _project_risk(source.get("risk"))
    principles = _check_principles(source.get("principles"))
    notes = _check_notes(source.get("principle_notes"))
    etf_policy = _check_etf_policy(source.get("etf_policy"))
    # F.COMPILED.PRINCIPLES_SOURCE: raw None or [] => default, else explicit.
    # Read from the RAW source, not from `principles` — that variable already
    # holds the substituted defaults, so it is `explicit` on every path.
    raw_principles = source.get("principles")
    principles_source = ("default"
                         if raw_principles is None or raw_principles == []
                         else "explicit")
    return {
        "source_hash": canonical_policy_hash(source),
        "hard_constraints": hard_constraints,
        "soft_principles": list(principles),
        "principle_notes": dict(notes),
        "principles_source": principles_source,
        "etf_policy": etf_policy,
        # One derivation, shared with the loader that re-checks it.
        "etf_entry_enabled": derive_etf_entry_enabled(
            principles_source,
            None if etf_policy is None else _EtfPolicyView(etf_policy)),
    }


class _EtfPolicyView:
    """Adapts the compiled dict to the one attribute `derive_etf_entry_enabled`
    reads, so the compiler and the loader share the derivation rather than
    each spelling it out."""

    def __init__(self, compiled: dict):
        self.allow_non_leveraged_equity_etfs = compiled[
            "allow_non_leveraged_equity_etfs"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile strategy.yaml into strategy.compiled.yaml",
    )
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    strategy_path = Path(args.strategy)
    output_path = Path(args.output)
    # Writing the artifact OVER the source destroys a gitignored personal
    # policy that nothing can restore. resolve() so `./strategy.yaml` and
    # `strategy.yaml` are caught too.
    if strategy_path.resolve() == output_path.resolve():
        print("compile_strategy: --output must not be --strategy; writing the "
              "compiled artifact over the source would destroy it",
              file=sys.stderr)
        return EXIT_INVALID

    try:
        with open(strategy_path, encoding="utf-8") as handle:
            source = yaml.load(handle, Loader=_DuplicateKeyLoader)  # noqa: S506 — SafeLoader subclass
    except _Invalid as exc:
        print(f"compile_strategy: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except (OSError, yaml.YAMLError) as exc:
        print(f"compile_strategy: cannot read {strategy_path}: {exc}",
              file=sys.stderr)
        return EXIT_INVALID

    # Only an EMPTY document becomes {}. `... or {}` swallowed every falsy
    # root — `[]`, `false`, `0`, `""` all compiled successfully into an empty
    # policy with exit 0, silently discarding whatever the operator meant.
    if source is None:
        source = {}
    if not isinstance(source, dict):
        print(f"compile_strategy: {strategy_path} must be a mapping, got "
              f"{type(source).__name__}", file=sys.stderr)
        return EXIT_INVALID

    try:
        compiled = compile_strategy(source)
    except _Invalid as exc:
        print(f"compile_strategy: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except RecursionError:
        # yaml.safe_load accepts recursive aliases; the canonicaliser would
        # blow the stack. A traceback here reads as a tool crash rather than
        # what it is — a configuration the operator has to fix.
        print("compile_strategy: strategy.yaml contains a recursive alias; "
              "policy must be a finite document", file=sys.stderr)
        return EXIT_INVALID

    if not (source.get("principles") or []):
        print(
            f"compile_strategy: `principles:` is null, absent or empty — "
            f"compiled the {len(_DEFAULT_PRINCIPLES)} canonical default "
            f"principles (rules/portfolio-safety.md). Nothing further is "
            f"needed; the artifact is complete.",
            file=sys.stderr,
        )

    _write_atomically(output_path, compiled)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
