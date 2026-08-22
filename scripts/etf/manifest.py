"""PR.MANIFEST — one row per portfolio ticker, so the validator can tell what
it is being asked to buy.

Before this existed the validator accepted state `{AAPL}` plus a proposed
`buy SOXX` as `passed=True, violations=[]`: nothing in its inputs could tell
whether an out-of-universe ticker was a stock, a fund, or a typo. The manifest
is that missing input, and it covers holdings AND watchlist because a buy is
most often for something not yet held.

Every path here is fail-closed toward `etf_unavailable` / `etf_unresolved`,
both of which block a buy. A row this producer cannot build honestly is a row
that must not authorize anything — and `cli_utils.read_json` is deliberately
NOT used, because it exits the process on a parse failure and this branch has
to emit row-level unavailability instead of taking the whole run down.

CLI:
    python3 -m scripts.etf.manifest build --state PATH --identity PATH \\
        --output PATH [--reports-root PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from scripts.cli_utils import normalize_ticker, write_output

_PREFIX = "etf.manifest"


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_json(path: Path):
    """`(doc, None)` or `(None, reason)`. Never exits — a bad file for ONE
    ticker must not take the whole portfolio down."""
    try:
        return json.loads(path.read_bytes().decode("utf-8")), None
    except FileNotFoundError:
        return None, "artifact_absent"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, "artifact_unloadable"


def _portfolio_tickers(state: Mapping[str, Any]) -> list[str]:
    """Holdings plus watchlist, canonicalized and de-duplicated.

    The watchlist is in scope because a buy is usually for something not yet
    held; a manifest covering only holdings would leave every new position
    unclassified, which is the exact hole this artifact closes.
    """
    seen: dict[str, None] = {}
    holdings = state.get("holdings") if isinstance(state, dict) else None
    watchlist = state.get("watchlist") if isinstance(state, dict) else None
    for source in (holdings or {}, watchlist or []):
        for raw in source:
            try:
                seen.setdefault(normalize_ticker(raw), None)
            except (ValueError, TypeError):
                print(f"{_PREFIX}: skipping unusable ticker {raw!r}",
                      file=sys.stderr)
    return list(seen)


def _identity_row(identity_map, ticker: str):
    rows = getattr(identity_map, "rows", {}) or {}
    return rows.get(ticker)


def build_row(ticker: str, identity, *, reports_root: Path) -> dict:
    """One manifest row. `identity` is the `IdentityRow` or None."""
    if identity is None or identity.instrument_type == "unknown":
        # No identity means nothing downstream can tell a fund from a stock,
        # so this row blocks every buy — including stock buys. That cost is
        # the price of closing the `state {AAPL} + buy SOXX` hole, and it is
        # disclosed rather than engineered away.
        return {
            "ticker": ticker, "row_kind": "etf_unresolved",
            "instrument_type": "unknown", "bundle_status": "absent",
            "resolution_status": (identity.resolution_status
                                  if identity is not None else "refused"),
        }

    if identity.instrument_type == "equity":
        return {"ticker": ticker, "row_kind": "stock",
                "instrument_type": "equity", "bundle_status": "absent"}

    return _etf_row(ticker, identity, reports_root=reports_root)


def _absence_reason(ticker: str, reports_root: Path) -> str:
    """`artifact_absent` only when no `etf_thesis.json` exists at all."""
    ticker_dir = Path(reports_root) / ticker
    if not ticker_dir.is_dir():
        return "artifact_absent"
    for date_dir in sorted(ticker_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        if (date_dir / "etf_thesis.json").is_file():
            return "artifact_unloadable"
    return "artifact_absent"


def _unavailable(ticker: str, identity_sha: str, reason: str,
                 *, bundle_status: str) -> dict:
    return {"ticker": ticker, "row_kind": "etf_unavailable",
            "instrument_type": "etf", "bundle_status": bundle_status,
            "identity_sha256": identity_sha, "reason": reason}


def _etf_row(ticker: str, identity, *, reports_root: Path) -> dict:
    from scripts.delta.constants import SKILL_ETF
    from scripts.delta.resolver import find_latest_prior
    from scripts.delta.run_meta import RunMeta
    from scripts.schemas.etf_thesis import validate_etf_thesis

    registry_path = reports_root / ticker / "instrument_type.json"
    identity_sha = _sha256_file(registry_path)
    if identity_sha is None:
        return _unavailable(ticker, "0" * 64, "artifact_absent",
                            bundle_status="absent")

    # include_today=True: a fund analysed THIS session must appear as a
    # thesis row. The resolver's default excludes today to stop a run being
    # its own prior, which is the opposite of what a manifest needs.
    run_dir = find_latest_prior(ticker, SKILL_ETF, reports_root=reports_root,
                                include_today=True)
    if run_dir is None:
        # The resolver returns None for BOTH "no run" and "the newest run's
        # artifact would not parse" — it validates parseability itself. Those
        # are different instructions to the user: absent means run the skill,
        # unloadable means go look at the file. Distinguish them here rather
        # than reporting the wrong one.
        return _unavailable(ticker, identity_sha,
                            _absence_reason(ticker, reports_root),
                            bundle_status="absent")

    thesis_path = Path(run_dir) / "etf_thesis.json"
    thesis_doc, err = _read_json(thesis_path)
    if err is not None:
        return _unavailable(ticker, identity_sha, err,
                            bundle_status="absent" if err == "artifact_absent"
                            else "unloadable")
    thesis_bytes = thesis_path.read_bytes()
    thesis_sha = hashlib.sha256(thesis_bytes).hexdigest()

    # The run_meta says which bytes this run produced. A mismatch means the
    # file was replaced after the run recorded it — the artifact may be
    # perfectly valid and still not be the one anybody reviewed.
    rm = RunMeta.load_or_none(Path(run_dir) / "run_meta.json")
    if rm is None or rm.etf is None or rm.etf.artifact_sha256 != thesis_sha:
        return _unavailable(ticker, identity_sha,
                            "run_meta_artifact_hash_mismatch",
                            bundle_status="unloadable")

    try:
        thesis = validate_etf_thesis(thesis_doc)
    except ValueError:
        return _unavailable(ticker, identity_sha, "artifact_unloadable",
                            bundle_status="unloadable")

    profile_path = Path(run_dir) / "data" / "etf_profile.json"
    profile_doc, err = _read_json(profile_path)
    if err is not None or _sha256_file(profile_path) != thesis.profile_sha256:
        return _unavailable(ticker, identity_sha, "artifact_unloadable",
                            bundle_status="unloadable")

    market_path = Path(run_dir) / "data" / "etf_market_snapshot.json"
    if thesis.market_snapshot_sha256 is not None:
        if _sha256_file(market_path) != thesis.market_snapshot_sha256:
            return _unavailable(ticker, identity_sha, "artifact_unloadable",
                                bundle_status="unloadable")

    row: dict[str, Any] = {
        "ticker": ticker, "instrument_type": "etf", "bundle_status": "loaded",
        "identity_sha256": identity_sha,
        "thesis_path": thesis_path.as_posix(),
        "thesis_sha256": thesis_sha,
        "profile_sha256": thesis.profile_sha256,
        "entry_eligibility": thesis.entry_eligibility,
        "analysis_readiness": thesis.analysis_readiness,
        # Copied from the profile so the validator's liquidity check reads the
        # same numbers the eligibility screen did, not a fresh fetch that
        # could disagree with the artifact it is gating.
        "avg_volume_shares": profile_doc.get("avg_volume_shares"),
        "avg_volume_shares_as_of": profile_doc.get("avg_volume_shares_as_of"),
        "profile_retrieved_at": profile_doc.get("retrieved_at"),
        "approval_reviewed_on": profile_doc.get("approval_reviewed_on"),
    }
    if thesis.market_snapshot_sha256 is not None:
        row["market_path"] = market_path.as_posix()
        row["market_snapshot_sha256"] = thesis.market_snapshot_sha256

    if thesis.variant == "thesis":
        row["row_kind"] = "etf_thesis"
        row["merit_recommendation"] = thesis.merit_recommendation
        # Projected so the decision prompt reads a fixed context instead of
        # re-deriving one — and re-derived here from the typed bundle, field
        # by field, because `thesis_sha256` proves the FILE is unchanged and
        # says nothing about whether this projection matches it.
        raw = thesis.raw
        row["decision_context"] = {
            "kind": raw["kind"],
            "technical_timing": raw["technical_timing"],
            "environment": raw["environment"],
            "entry_conditions": list(thesis.entry_conditions),
            "invalidation_conditions": list(thesis.invalidation_conditions),
            "merit_evidence": raw["merit_evidence"],
            "top_holdings": profile_doc.get("top_holdings"),
            "coverage_pct": profile_doc.get("coverage_pct"),
        }
    else:
        row["row_kind"] = "etf_refusal"
        row["refusal_kind"] = thesis.refusal_kind
        row["entry_reasons"] = list(thesis.entry_reasons)
        row["analysis_reasons"] = list(thesis.analysis_reasons)
        held = thesis.raw.get("held_exit_context")
        if held:
            # A held position's exit conditions reach the decision prompt even
            # though this run wrote no thesis. Without them the prompt sees a
            # holding with no documented exit.
            row["decision_context"] = {
                "invalidation_conditions": held["invalidation_conditions"],
                "conditions_authored_at": held["conditions_authored_at"],
                "source_thesis_sha256": held["source_thesis_sha256"],
            }
    return row


def build_manifest(state: Mapping[str, Any], identity_map, *,
                   reports_root: Path) -> dict:
    rows = {}
    for ticker in _portfolio_tickers(state):
        rows[ticker] = build_row(ticker, _identity_row(identity_map, ticker),
                                 reports_root=Path(reports_root))
    return {"rows": rows}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.etf.manifest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--state", required=True)
    b.add_argument("--identity", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--reports-root", default="reports")
    args = ap.parse_args(argv)

    import yaml

    from scripts.schemas.etf_manifest import validate_etf_manifest
    from scripts.schemas.instrument_registry import load_identity_map

    try:
        with open(args.state, encoding="utf-8") as fh:
            state = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"{_PREFIX}: cannot read --state {args.state}: {exc}",
              file=sys.stderr)
        return 1
    try:
        identity_map = load_identity_map(args.identity)
    except (OSError, ValueError) as exc:
        print(f"{_PREFIX}: cannot read --identity {args.identity}: {exc}",
              file=sys.stderr)
        return 1

    manifest = build_manifest(state, identity_map,
                              reports_root=Path(args.reports_root))
    try:
        validate_etf_manifest(manifest)
    except ValueError as exc:
        # A manifest this producer cannot validate must not be written: the
        # validator's fail-closed path treats a loader-invalid manifest as a
        # violation for every buy, and a half-written one is indistinguishable
        # from a corrupted one.
        print(f"{_PREFIX}: refusing to write an invalid manifest: {exc}",
              file=sys.stderr)
        return 1

    write_output(manifest, args.output)
    print(Path(args.output).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
