"""Stock Analysis System v7 — scripts package.

On import, make stdout/stderr UTF-8 so the CLIs never crash with
UnicodeEncodeError when printing non-ASCII (•, →, ≤, ⚠, …, ✓) on a console with
a legacy code page — notably Windows GBK/cp936, where the default crashes.
`python -m scripts.<x>` imports this package first, so this one place covers
every script on every platform (bootstrap, update, and the skill-invoked
fetch/indicators/portfolio_log/… subprocesses).

Best-effort + idempotent: it ONLY reconfigures a stream whose encoding is not
already UTF-8 (so on Linux/macOS/CI — already UTF-8 — it is a complete no-op,
leaving the test suite and captured streams untouched), and silently leaves any
stream that can't be reconfigured (redirected/captured/old Python).
"""
import sys


def _force_utf8_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if stream is not None and enc != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # captured/redirected/binary/old-Python stream — leave it


_force_utf8_io()
