"""Deterministic summary.changelog.md append helper.

Called by the score-business orchestration (and thesis orchestration for
the thesis-side changelog) after the synthesis agent emits a delta
section. Not an agent-written file — pure concat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def append_changelog(
    prior_path: Optional[Path],
    current_path: Path,
    ticker: str,
    delta_section: str,
) -> None:
    """Write the merged changelog to current_path.

    If prior_path is None or doesn't exist, initialize with a title
    header + the first delta section. Otherwise: prior contents + HR +
    new section.
    """
    current_path.parent.mkdir(parents=True, exist_ok=True)

    # Reject symlinks on user-provided paths — a symlink pointing at
    # /etc/passwd or ~/.ssh/id_rsa would be concatenated verbatim into
    # the changelog, which is committed to git. This is the same defense
    # added to screen.py's watchlist loader; the pattern generalizes.
    if prior_path is not None and Path(prior_path).is_symlink():
        raise ValueError(
            f"append_changelog: prior changelog path is a symlink "
            f"({prior_path} → {Path(prior_path).resolve()}). Symlinks "
            f"rejected to prevent exfiltrating sensitive files into the "
            f"committed changelog."
        )
    # Concurrency probe 2026-08-03 F4: a same-day SECOND run must append
    # to TODAY's already-accumulated changelog, not rebuild from the
    # resolver-provided prior (yesterday) — the rebuild silently dropped
    # the earlier same-day run's completed section (no race needed; runs
    # finishing one after another lost history). Base preference:
    # today's current file if present, else the prior.
    base_path = None
    if current_path.exists():
        base_path = current_path
        # Idempotency: the old rebuild-from-prior was naturally retry-safe;
        # accumulate-mode must not double-append when an orchestration step
        # is re-run with the identical section.
        _existing = current_path.read_text(encoding="utf-8")
        if delta_section.rstrip() and delta_section.rstrip() in _existing:
            print(f"append_changelog: identical delta section already "
                  f"present in {current_path} — skipping duplicate append.")
            return
    elif prior_path is not None and Path(prior_path).exists():
        base_path = Path(prior_path)
    if base_path is None:
        body = f"# {ticker} — Changelog\n\n{delta_section.rstrip()}\n"
    else:
        prior_content = base_path.read_text(encoding="utf-8").rstrip()
        body = f"{prior_content}\n\n---\n\n{delta_section.rstrip()}\n"

    # Closing round-6 F1: write_text truncates in place — a crash
    # mid-write left a torn changelog that the accumulate-mode base
    # preference then inherited forever. Temp + os.replace (repo
    # convention).
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(current_path.parent),
                               prefix=".changelog.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, str(current_path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cli():
    import argparse
    import sys
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("--prior", default=None, help="Path to prior changelog (optional)")
    p.add_argument("--current", required=True, help="Destination changelog path")
    p.add_argument("--ticker", required=True)
    p.add_argument("--delta-section", required=True,
                   help="Path to file containing the new delta section markdown")
    args = p.parse_args()

    section_path = Path(args.delta_section)
    if not section_path.exists():
        print(
            f"append_changelog: ERROR — delta section file not found: {section_path}",
            file=sys.stderr,
        )
        sys.exit(2)
    if section_path.is_symlink():
        print(
            f"append_changelog: ERROR — delta section path is a symlink; "
            f"reject to prevent exfil into committed changelog: "
            f"{section_path} → {section_path.resolve()}",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        section = section_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"append_changelog: ERROR — cannot read {section_path}: {e}",
              file=sys.stderr)
        sys.exit(2)

    prior_path = Path(args.prior) if args.prior else None
    try:
        append_changelog(
            prior_path=prior_path,
            current_path=Path(args.current),
            ticker=args.ticker,
            delta_section=section,
        )
    except OSError as e:
        # Guard against unreadable prior / unwritable current (permission
        # issues, disk full, etc.) — report + exit 2 (0=ok/1=failure/2=error).
        print(
            f"append_changelog: ERROR — changelog IO failed "
            f"(prior={prior_path}, current={args.current}): {e}",
            file=sys.stderr,
        )
        sys.exit(2)
    print(args.current)


if __name__ == "__main__":
    _cli()
