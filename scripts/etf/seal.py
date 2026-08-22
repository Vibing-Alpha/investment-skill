"""PR.SEAL_WRITER — bind the manifest and the artifact bytes behind each row.

Written at portfolio Step 5, after the decision is authored and before any
order is validated. Deterministic: it copies hashes the manifest already
carries and adds a timestamp. It makes no judgement and reads no artifact of
its own — everything it seals was already verified when the manifest row was
built, and re-deriving it here would be a second implementation free to
disagree with the first.

CLI:
    python3 -m scripts.etf.seal write --manifest PATH --output PATH
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import sys
from pathlib import Path
from typing import Any

from scripts.cli_utils import write_output
from scripts.schemas.decision_seal import SEAL_SKILL, variant_for_row

_PREFIX = "etf.seal"


def build_seal(manifest, manifest_sha256: str, *, now: str) -> dict:
    """One seal per non-stock row. Stock rows need none: the ETF entry gate is
    what the seal protects, and a stock buy is not gated by it."""
    seals: dict[str, Any] = {}
    for ticker, row in manifest.rows.items():
        variant = variant_for_row(row)
        if variant is None:
            continue
        raw = row.raw
        seal: dict[str, Any] = {"skill": SEAL_SKILL,
                                "manifest_sha256": manifest_sha256}
        if variant in ("etf_thesis", "etf_refusal:analysis_unavailable"):
            seal["artifact_sha256"] = raw["thesis_sha256"]
            seal["profile_sha256"] = raw["profile_sha256"]
            seal["market_snapshot_sha256"] = raw["market_snapshot_sha256"]
            seal["generated_at"] = now
        elif variant == "etf_refusal:ineligible":
            seal["artifact_sha256"] = raw["thesis_sha256"]
            seal["profile_sha256"] = raw["profile_sha256"]
            seal["generated_at"] = now
        elif variant == "etf_unavailable":
            seal["identity_sha256"] = raw["identity_sha256"]
            seal["sealed_at"] = now
        else:  # etf_unresolved
            seal["sealed_at"] = now
        seals[ticker] = seal
    return {"seals": seals}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.etf.seal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--manifest", required=True)
    w.add_argument("--output", required=True)
    args = ap.parse_args(argv)

    from scripts.schemas.decision_seal import validate_decision_seal
    from scripts.schemas.etf_manifest import load_etf_manifest

    manifest_path = Path(args.manifest)
    try:
        manifest = load_etf_manifest(manifest_path)
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        print(f"{_PREFIX}: cannot read --manifest {args.manifest}: {exc}",
              file=sys.stderr)
        return 1

    now = (datetime.datetime.now(datetime.timezone.utc)
           .isoformat().replace("+00:00", "Z"))
    doc = build_seal(manifest, manifest_sha, now=now)
    try:
        validate_decision_seal(doc)
    except ValueError as exc:
        print(f"{_PREFIX}: refusing to write an invalid seal: {exc}",
              file=sys.stderr)
        return 1
    write_output(doc, args.output)
    print(Path(args.output).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
