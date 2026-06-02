"""Shared utilities for source adapters: auth, retry, provenance.

Provides ``make_request`` (full-URL) and ``make_api_request`` (relative
endpoint) for all source adapters in the v7 scripts package.
"""

import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from scripts.constants import BASE_URL

# ---------------------------------------------------------------------------
# DL1 unified HTTP primitives — HttpPolicy, HttpResponse, exceptions, policies
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field, replace
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Cross-adapter scalar guards
# ---------------------------------------------------------------------------

def is_bool_like(v: Any) -> bool:
    """True if *v* is a Python `bool` OR a numpy scalar bool.

    ISS-141 (Loop12 cycle 1): promoted to common.py from yahoo_finance.py
    `_is_bool_like` (loop11 ISS-139). financial_datasets.py and yahoo_finance
    .py both need symmetric numpy-bool defense in their numeric coercion
    helpers — duplicating the helper would invite drift, so it lives here as
    a shared adapter primitive.

    Why this matters: `isinstance(numpy.bool_(True), bool)` is False because
    numpy bool scalars do not subclass Python `bool`. Without this guard,
    `float(np.bool_(True))` returns 1.0 and `int(np.bool_(True))` returns 1,
    silently coercing upstream bool drift into a numeric value (1.0 / 0.0
    in metrics, 1 / 0 in counts).

    Implementation: detect by `(__module__, __name__)` so the project's
    stdlib-only core constraint is preserved (no hard numpy import).
    NumPy historically spelled the class `bool_`; modern numpy (>= 1.20
    deprecation cycle) aliases `bool` (no trailing underscore) to the same
    scalar type. Both names — plus the legacy `bool8` — are accepted.

    Edge cases (Codex-reviewed loop12):
    - `numpy.True_` / `numpy.False_` are instances of the same `numpy.bool_`
      class, so they hit the same branch.
    - `cupy` / `jax.numpy` use different `__module__` strings, so they would
      not be matched. If those backends ever land in the project, extend
      the module check.
    """
    if isinstance(v, bool):
        return True
    cls = type(v)
    mod = getattr(cls, "__module__", "")
    if not mod.startswith("numpy"):
        return False
    return cls.__name__ in ("bool_", "bool", "bool8")


def sanitize_dict_numerics(obj, *, coerce_bool: bool = False):
    """ISS-085 (Loop6 backlog) + ISS-095 (Loop7) + ISS-090 (Loop7 backlog):
    recursively walk container types and coerce NaN / Infinity /
    -Infinity to None.

    ISS-212 (Loop30 cycle 1 fresh-session-17): relocated from
    financial_datasets.py (where it had been a private `_sanitize_dict
    _numerics`) to common.py as a public adapter primitive. Pre-fix
    yahoo_finance.py and scripts/adr/detect.py imported the underscore
    symbol across module boundaries — a hidden cross-adapter contract
    that would silently break if financial_datasets refactored its
    internals. This is the same pattern used for `is_bool_like` (also
    promoted to common.py at ISS-141).

    Container coverage:
      dict/list                 — recurse, preserve type
      tuple                     — recurse, return tuple (rare in JSON
                                  but Python-native shape data may use)
      set/frozenset             — recurse, return same type (rare; JSON
                                  doesn't have sets but Python data
                                  pipelines may)
      Decimal                   — finite-check via .is_finite()
      bool                      — preserve unless coerce_bool=True

    coerce_bool:
      False (default) — preserve bool values (legitimate flags like
        `is_adr`, `is_foreign`). NaN/Inf coerced; bool kept.
      True — also coerce bool to None. Use for analyst/earnings/
        institutional payloads where ALL item fields should be
        str/num/None/nested — bool would be upstream type drift, not
        a legitimate flag. Pre-fix bool contamination silently passed.

    Applied at analyst-estimates / earnings / institutional emit sites
    (coerce_bool=True), and company emit (coerce_bool=False — has
    legitimate is_adr/is_foreign bool fields).
    """
    import math
    import decimal

    if isinstance(obj, dict):
        return {k: sanitize_dict_numerics(v, coerce_bool=coerce_bool)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_dict_numerics(v, coerce_bool=coerce_bool)
                for v in obj]
    if isinstance(obj, tuple):
        return tuple(
            sanitize_dict_numerics(v, coerce_bool=coerce_bool)
            for v in obj
        )
    if isinstance(obj, (set, frozenset)):
        # Sanitize element-wise. Empty result for sets where every
        # element coerces to None (since sets dedupe None entries to
        # a single None entry — match that semantics).
        sanitized = [
            sanitize_dict_numerics(v, coerce_bool=coerce_bool)
            for v in obj
        ]
        return type(obj)(sanitized)
    if is_bool_like(obj):
        # bool is int subclass; preserve unless caller opts into
        # numeric-strict mode.
        # ISS-141 (Loop12 cycle 1): is_bool_like also covers numpy.bool_ —
        # pre-fix `isinstance(obj, bool)` missed numpy scalar bool, leaving
        # it to fall through past Decimal / float checks and be returned
        # as-is. Then AdapterResult JSON-safety would reject it (fail-loud,
        # but defeats this sanitizer's stated contract of "never let
        # non-JSON-serializable types out"). Now coerced symmetrically
        # with Python bool.
        return None if coerce_bool else obj
    if isinstance(obj, decimal.Decimal):
        # Decimal('NaN') / Decimal('Infinity') / Decimal('-Infinity')
        # all return False from is_finite().
        if not obj.is_finite():
            return None
        # ISS-167 (Loop20 cycle 1 fresh-session-7): finite Decimal is
        # NOT JSON-safe — AdapterResult.__post_init__ → _validate_json
        # _safe rejects Decimal at the catch-all (adapter_result.py
        # L368). The sanitizer's stated purpose is "make data envelope
        # -safe"; pre-fix, finite Decimal slipped through and caused
        # ValueError at AdapterResult construction. Coerce to float
        # for JSON-safety; precision loss is acceptable here because
        # the adapter envelope is designed for JSON serialization
        # (other numeric paths already coerce upstream API decimal
        # strings to float via float(str)).
        try:
            return float(obj)
        except (ValueError, OverflowError):
            return None
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def safe_num(v):
    """Coerce non-finite / bool / non-numeric to None.

    ISS-220 SF-D (Loop33 cycle 1): promoted from
    `financial_datasets._safe_num` to `common.safe_num` so adr/detect.py
    + ADR P3 emit path can also use the canonical numeric coercer
    via `emit_with_numeric_coerce`.
    """
    import math
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def coerce_known_numeric_fields(obj, numeric_fields):
    """Walk a single-level dict and coerce values at known numeric keys
    to None when they are not finite-numeric. Keeps non-numeric fields
    (str/dict/list/None) unchanged so the rest of the row survives.

    ISS-158 (Loop18 cycle 1 fresh-session-5): `sanitize_dict_numerics`
    handles NaN/Inf/bool but NOT strings in numeric slots. This fills
    that gap at the per-category emit boundary.

    ISS-220 SF-D (Loop33 cycle 1): promoted from
    `financial_datasets._coerce_known_numeric_fields`.
    """
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)  # shallow copy — caller mutates safely
    for key in numeric_fields:
        if key in out:
            v = out[key]
            if v is None:
                continue
            # safe_num already handles NaN/Inf/bool/non-numeric → None
            out[key] = safe_num(v)
    return out


def emit_with_numeric_coerce(data, *, numeric_fields=None, coerce_bool: bool = True):
    """Single emit-boundary helper: sanitize NaN/Inf/bool then coerce
    known numeric fields to None on string drift.

    Post-Loop22 structural: 5 producers (metrics / analyst /
    institutional / earnings / financials) used to chain
    `sanitize_dict_numerics(...) + coerce_known_numeric_fields(...)`
    by hand. The chain has 2 failure modes a producer author can hit:
      - Forget the second call entirely (string drift slips through —
        this is exactly what every Loop18-21 round found in a new
        producer)
      - Apply coerce only to top-level dict, missing the per-row case
        when data is a list-of-rows (e.g. analyst estimates, holdings)

    Centralizing the chain plus the dict-vs-list dispatch removes the
    failure modes. Producers call:
        return AdapterResult.passed(
            data=emit_with_numeric_coerce(raw, numeric_fields=_X_NUMERIC_FIELDS),
            meta=...,
        )

    For top-level dict-of-lists shapes (e.g. financial_statements
    with income_statements/balance_sheets/cash_flows), the helper
    walks one level deep into list-valued keys.

    ISS-220 SF-D (Loop33 cycle 1): promoted from
    `financial_datasets._emit_with_numeric_coerce` to `common.emit_with_numeric_coerce`
    so adr/detect.py can also route ADR PASSED data through the
    canonical numeric emit boundary (Pattern P3 sym-ext: ADR was
    8th emit site missed by the original Loop22 helper).
    """
    sanitized = sanitize_dict_numerics(data, coerce_bool=coerce_bool)
    if numeric_fields is None:
        return sanitized
    if isinstance(sanitized, dict):
        # Top-level dict — coerce its own numeric keys, AND recurse
        # one level into list-valued keys (financial_statements case).
        sanitized = coerce_known_numeric_fields(sanitized, numeric_fields)
        for key, val in list(sanitized.items()):
            if isinstance(val, list):
                sanitized[key] = [
                    coerce_known_numeric_fields(row, numeric_fields)
                    if isinstance(row, dict) else row
                    for row in val
                ]
    elif isinstance(sanitized, list):
        # List-of-rows — coerce each row.
        sanitized = [
            coerce_known_numeric_fields(row, numeric_fields)
            if isinstance(row, dict) else row
            for row in sanitized
        ]
    return sanitized


# ---------------------------------------------------------------------------
# Currency code validation (DL3a §2 invariant 3)
# ---------------------------------------------------------------------------

SUPPORTED_CURRENCY_CODES = frozenset({
    "USD", "EUR", "JPY", "GBP", "CAD", "AUD", "CHF", "HKD", "CNY", "TWD",
    "KRW", "INR", "SGD", "NZD", "SEK", "NOK", "DKK", "BRL", "MXN", "ZAR",
    "RUB", "TRY", "THB", "MYR", "PHP", "IDR", "ILS", "PLN", "CZK", "HUF",
})


def normalize_currency(raw: Any) -> Optional[str]:
    """Validate and case-normalize a currency code per §2 invariant 3.

    - Non-string input → None
    - "UNKNOWN" sentinel → "UNKNOWN" (passes through)
    - String in SUPPORTED_CURRENCY_CODES after strip+upper → upper form
    - Anything else (empty, whitespace, unsupported ISO, valid ISO outside
      30-code subset such as "AED") → None

    Naming rationale (§2 invariant 3): avoiding "ISO 4217" in the constant
    encodes that this is a curated project-observed subset, not full
    standards compliance.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s == "UNKNOWN":
        return "UNKNOWN"
    u = s.upper()
    if u in SUPPORTED_CURRENCY_CODES:
        return u
    return None


# ---------------------------------------------------------------------------
# §3.5 cross-artifact currency resolver
# ---------------------------------------------------------------------------

def _descend_dotted(obj: Any, path: str) -> Any:
    """Traverse a dotted path through nested dicts/lists.

    Tokens that parse as int are treated as list indices; others as dict keys.
    Returns None on any traversal failure (missing key, index out of range,
    wrong container type) — never raises.
    """
    if obj is None or not path:
        return None
    for token in path.split("."):
        if obj is None:
            return None
        # Try int token (list index)
        try:
            idx = int(token)
            if isinstance(obj, list) and 0 <= idx < len(obj):
                obj = obj[idx]
                continue
            return None
        except ValueError:
            pass
        # String token (dict key)
        if isinstance(obj, dict) and token in obj:
            obj = obj[token]
            continue
        return None
    return obj


def _path_present(obj: Any, path: str) -> bool:
    """Like _descend_dotted but returns True iff the leaf key/index EXISTS,
    regardless of whether the value at that path is None.

    Used by resolve_artifact_currency to distinguish:
      * producer emitted `{"currency": None}` (explicit graceful-degrade)
      * producer never wrote the currency key at all

    The first case is a producer signal that should NOT fall through to disk;
    the second is the cross-artifact inheritance case that SHOULD fall through.
    """
    if obj is None or not path:
        return False
    tokens = path.split(".")
    for token in tokens[:-1]:
        if obj is None:
            return False
        try:
            idx = int(token)
            if isinstance(obj, list) and 0 <= idx < len(obj):
                obj = obj[idx]
                continue
            return False
        except ValueError:
            pass
        if isinstance(obj, dict) and token in obj:
            obj = obj[token]
            continue
        return False
    # Final token — check presence not value
    if obj is None:
        return False
    last = tokens[-1]
    try:
        idx = int(last)
        return isinstance(obj, list) and 0 <= idx < len(obj)
    except ValueError:
        pass
    return isinstance(obj, dict) and last in obj


def resolve_artifact_currency(
    *,
    in_memory: Optional[dict] = None,
    in_memory_path: str = "currency",
    output_dir: Path,
    artifact_name: str,
    artifact_path: str = "currency",
) -> Optional[str]:
    """Currency resolver per §3.5 — in-memory preferred, disk fallback.

    Semantics by case (impl-loop1 cycle 2 fresh-challenge tightened —
    pre-fix a producer that pre-normalized to ``None`` for unsupported ISO
    silently fell through to stale disk currency, defeating fail-close):

    1. ``in_memory is None`` → fall through to disk (caller never had data).
    2. ``in_memory`` has the key at ``in_memory_path`` (even if value is
       ``None``) → commit to the in-memory value: normalize if string;
       return ``None`` if the value is ``None`` / non-string. That ``None``
       is the producer's authoritative "tried but couldn't verify" signal.
    3. ``in_memory`` exists but does NOT have the key at ``in_memory_path``
       → cross-artifact inheritance: fall through to disk (e.g. insider
       inheriting currency from price snapshot).
    """
    # Step 1: in-memory.
    if in_memory is not None:
        if _path_present(in_memory, in_memory_path):
            # Case 2: producer-authoritative — even an explicit None.
            v = _descend_dotted(in_memory, in_memory_path)
            if isinstance(v, str) and v.strip():
                return normalize_currency(v)
            return None
        # Case 3: key absent — fall through to disk.
    # Step 2: disk
    p = Path(output_dir) / artifact_name
    if p.exists():
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        v = _descend_dotted(doc, artifact_path)
        if isinstance(v, str) and v.strip():
            return normalize_currency(v)
    return None


# ---------------------------------------------------------------------------
# Detail scrubbing for AdapterError.detail / log persistence
# ISS-220 SF-D (Loop33 cycle 1): promoted from adapter_result + yahoo_finance
# so cross-module callers (adapter_error_from_exception 4.12 yfinance scrub
# integration) don't need cross-module imports back into adapter_result.py.
# ---------------------------------------------------------------------------

def _scrub_detail(detail: str, redact: tuple[str, ...]) -> str:
    """Replace each non-empty secret in *redact* with [REDACTED] inside
    *detail*. Used by `adapter_error_from_exception` to scrub API keys
    (and other secrets that may be embedded in URLs/messages) before
    they land in `AdapterResult.error.detail`.

    ISS-057 (Loop4): sort redact tuple by length descending to handle
    overlapping secrets correctly. Pre-fix `("abc", "abcdef")` order →
    "abcdef" never matches because "abc" already replaced its prefix,
    leaking "def" suffix. Now: longest-match-first ensures full secret
    is scrubbed regardless of caller's tuple order.

    ISS-112 (Loop8 cycle 2): also redact the URL-encoded variants of
    each secret. Exception strings frequently quote the request URL
    (e.g. `apikey=a%2Fb%3Dc`); we expand each secret to {raw,
    quote, quote_plus} and dedupe.

    ISS-220 SF-D (Loop33 cycle 1): promoted from adapter_result.
    """
    import urllib.parse as _up
    expanded: list[str] = []
    seen: set[str] = set()
    for s in redact:
        if not s:
            continue
        for variant in (s, _up.quote(s, safe=""), _up.quote_plus(s)):
            if variant and variant not in seen:
                seen.add(variant)
                expanded.append(variant)
    # Sort longer variants first so they're matched before shorter
    # prefixes/substrings of themselves. Stable across duplicates.
    ordered = sorted(expanded, key=len, reverse=True)
    for secret in ordered:
        detail = detail.replace(secret, "[REDACTED]")
    return detail


# yfinance-aware scrub patterns (promoted from yahoo_finance._yfinance_safe_msg
# at ISS-220 SF-D / 4.12).
# ISS-220 Loop39 Sec-1 (iter8): cookie/set-cookie split into a
# dedicated pattern that consumes through `\n}"'` (NOT through `;`)
# because cookie headers are semicolon-delimited and the original
# pattern's `[^,;\n}]*` stopped at the first `;` leaving subsequent
# `; key=value` pairs visible. Other auth headers stay on the
# generic non-cookie pattern (existing behavior preserved).
_AUTH_HEADER_PAT = re.compile(
    r'(?i)["\']?\b'
    r'(authorization|crumb'
    r'|x[-_]?api[-_]?key|api[-_]?key'
    r'|access[-_]?token|refresh[-_]?token|session[-_]?token|token'
    r')\b["\']?'
    r'\s*[:=]\s*'
    r'["\']?[^,;\n}]*'
)
_COOKIE_HEADER_PAT = re.compile(
    r'(?i)["\']?\b(cookie|set-cookie)\b["\']?'
    r'\s*[:=]\s*'
    r'["\']?[^\n}]*'  # cookie values are `;`-delimited; consume to line/brace end
)
def _is_sensitive_header_name(name: str) -> bool:
    """Return True if the caller-supplied header name carries credentials
    that MUST be gated to `auth_host_set` (dropped on cross-host redirect).

    ISS-221 iter11 (per superpowers code review 2026-05-09): true single
    source of truth — probes `_AUTH_HEADER_PAT` / `_COOKIE_HEADER_PAT`
    DIRECTLY on a synthetic `name: SENTINEL` header line. If either
    redaction regex fires, the name is sensitive. The iter10 separate
    `_SENSITIVE_HEADER_NAME_PAT` (with `\\A...\\Z` anchors) was a parity
    claim, NOT structural parity — it missed `Proxy-Authorization`
    (substring `\\bauthorization\\b` redacts but full-match anchors do
    not). This formulation is parity-by-construction by definition:
    classification ⇔ redaction-fires.

    Conservative bias preserved: anything the redaction layer would
    scrub from logs is also gated from cross-host replay. False-
    positive = pin to auth host (safe); false-negative = leak cross-
    host (catastrophic).
    """
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    if not stripped:
        return False
    # `_AUTH_HEADER_PAT` uses `\b` which treats `_` as word-char;
    # `x_access_token` therefore lacks a boundary before `access` and
    # the redaction regex misses it. HTTP-wise `_` and `-` are
    # interchangeable separators in header names (yfinance / vendor
    # SDKs sometimes pass `_`); normalize for the parity probe so the
    # gate covers both spellings consistently.
    sentinel = f"{stripped.replace('_', '-')}: SENTINEL"
    return (
        _AUTH_HEADER_PAT.sub("X", sentinel) != sentinel
        or _COOKIE_HEADER_PAT.sub("X", sentinel) != sentinel
    )


_HOME_PATH_PAT = re.compile(r"/home/[^/\s]+/[^\s]+")


def _yfinance_scrub(text: str) -> str:
    """Strip yfinance-specific sensitive substrings from *text*:
      - Auth/cookie/token/crumb header substrings (regex)
      - Local home paths (regex; yfinance cache file leakage)

    Does NOT redact API keys (caller composes with `_scrub_detail`
    for that). Used by `adapter_error_from_exception` rows 7/8
    (YfRateLimitError / YfCallError) to sanitize before persisting
    error.detail (ISS-220 4.12, Loop33 cycle 1).

    ISS-220 SF-D / 4.12: promoted (with rename) from
    `yahoo_finance._yfinance_safe_msg`. The legacy
    `_yfinance_safe_msg` remains in yahoo_finance.py as a thin
    wrapper that composes `_yfinance_scrub` + env-key `_scrub_detail`
    for stderr-print sites that don't go through the canonical
    mapper.
    """
    # ISS-220 Loop39 Sec-1 (iter8): cookie scrub uses dedicated pattern
    # that consumes the full semicolon-delimited cookie string. Run
    # cookie pattern FIRST so subsequent _AUTH_HEADER_PAT (which still
    # stops at `;` for non-cookie headers) can't re-process leftover
    # cookie tail.
    out = _COOKIE_HEADER_PAT.sub(r"\1=[REDACTED]", text)
    out = _AUTH_HEADER_PAT.sub(r"\1=[REDACTED]", out)
    out = _HOME_PATH_PAT.sub("[HOMEPATH]", out)
    return out


class HttpError(Exception):
    """Base for all http_get errors."""


class HttpStatusError(HttpError):
    """Raised only by HttpResponse.raise_for_status() — NOT auto-raised."""

    def __init__(self, status: int, url: str, body: bytes):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} on {url}")


class HttpTransportError(HttpError):
    """DNS/TLS/connection/timeout after all retries exhausted."""


class SsrfBlockedError(HttpError, ValueError):
    """URL scheme or host rejected by SSRF / allowlist check.

    Multiple inheritance: subclass of ValueError for back-compat with legacy
    safe_urlopen callers doing `except ValueError` (the existing safe_urlopen
    raises bare ValueError on blocked hosts/schemes; we preserve that contract
    while also giving new code the HttpError taxonomy).
    """


class ResponseTooLargeError(HttpError):
    """Response body exceeded HttpPolicy.max_response_bytes."""

    def __init__(self, url: str, cap: int, bytes_read: int):
        self.url = url
        self.cap = cap
        self.bytes_read = bytes_read
        super().__init__(f"Response too large: {bytes_read} > cap {cap} on {url}")


class RetryExhaustedError(HttpError):
    """All retries used up with status still in retry_on."""

    def __init__(self, status: int, url: str, attempts: int, body: bytes):
        self.status = status
        self.url = url
        self.attempts = attempts
        self.body = body
        super().__init__(f"HTTP {status} after {attempts} attempts on {url}")


from types import MappingProxyType

@dataclass(frozen=True, eq=False)  # H-C fix: eq=False avoids auto-__hash__ attempting to hash the (unhashable) Mapping field
class HttpPolicy:
    retry_on: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    max_retries: int = 3
    honor_retry_after: bool = True
    retry_after_cap_s: float = 60.0
    backoff_base_s: float = 1.0
    backoff_jitter_s: float = 1.0
    timeout_s: float = 15.0
    max_response_bytes: int = 16 * 1024 * 1024
    max_redirects: int = 5
    allowed_host_suffixes: frozenset[str] = frozenset()
    # Per-hop scheme allowlist. Default {http, https}; SEC policies tighten to
    # {https} only to prevent redirect downgrade attacks (Codex H2 finding).
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    default_headers: Mapping[str, str] = field(default_factory=dict)
    auth_fn: Optional[Callable[[], Mapping[str, str]]] = None
    # ISS-213 (Loop31 cycle 1 fresh-session-18): host-pin for auth headers.
    # `allowed_host_suffixes` was added in Loop22-23 to block cross-origin
    # redirects under SSRF/auth-leak threat models, but its scope is
    # *suffix-based*: any redirect to *.financialdatasets.ai (e.g.
    # `cdn.financialdatasets.ai` taken over via subdomain hijack) would
    # still receive the X-API-KEY header on the redirect hop. `auth_hostnames`
    # narrows further: when set, auth_fn() output is attached ONLY when the
    # current redirect-hop host is in this set. Default empty set + auth_fn
    # set = auto-pin to the original request host at http_get time (computed
    # once per call). Producers that legitimately need broader auth scope
    # (none today) opt-in by setting an explicit frozenset.
    auth_hostnames: frozenset[str] = frozenset()
    # ISS-205 (Loop31 cycle 1 fresh-session-18): per-policy decompressed
    # byte cap. Pre-fix `_decompress_if_needed` used a fixed
    # `min(max(64 MiB, 32× compressed length), 256 MiB)` ceiling
    # regardless of `max_response_bytes`. Adapters with small
    # `max_response_bytes` (e.g. 16 MiB default, 50 MiB SEC filings)
    # had no way to tighten the decompressed cap. None preserves the
    # legacy ratio-based default; explicit int caps decompressed
    # output to that absolute byte count.
    max_decompressed_bytes: int | None = None
    # ISS-220 SF-G (Loop35 cycle 1): default-disable urllib's ambient
    # ProxyHandler. Pre-fix `urllib.request.build_opener(...)` picked
    # up `HTTP_PROXY`/`HTTPS_PROXY` from os.environ → all DL1 traffic
    # could be silently routed through an attacker-controlled proxy,
    # bypassing _check_ssrf / host-pin / DNS-pin. The .env loader
    # (now allowlisted by SF-G) cannot inject proxy vars, but
    # OS-level env is still under attacker control in shared hosts.
    # "disabled" (default) installs `ProxyHandler({})` which forces
    # direct connection. "ambient" preserves urllib's default
    # behavior (env + system config) for opt-in dev/MITM-debug use.
    proxy_strategy: str = "disabled"

    def __post_init__(self):
        # H-B fix: module-level policy singletons (FD_API_POLICY, SEC_POLICY, etc.)
        # are constructed with literal dicts. `frozen=True` blocks attribute
        # REASSIGNMENT but does NOT make the dict contents immutable. Without this,
        # a caller could do SEC_POLICY.default_headers["X"] = "y" and mutate the
        # shared singleton. Wrap in MappingProxyType to make it read-only.
        #
        # M1 (Codex post-impl): unconditional copy-then-wrap. A prior isinstance
        # guard was bypassable by passing MappingProxyType(mutable_dict) — the
        # proxy would be kept as-is, but the caller-held mutable_dict reference
        # could still mutate the underlying storage.
        object.__setattr__(
            self, "default_headers",
            MappingProxyType(dict(self.default_headers)),
        )
        # Post-Loop23 structural invariant: any HttpPolicy that injects
        # auth headers MUST also pin allowed_host_suffixes. Otherwise a
        # 302 from the legitimate provider to an attacker-controlled
        # host would let the attacker observe the auth header on the
        # redirect hop (http_get rebuilds every request with the same
        # merged auth).
        #
        # ISS-173 (Loop22 fresh-session-9) found this on FD_API_POLICY.
        # ISS-176 (Loop23 fresh-session-10) found the same on FMP_POLICY.
        # The textual convention in CLAUDE.md was insufficient — codex
        # didn't read it. Machine-enforce at construction so future
        # auth-bearing policies fail fast at module import time instead
        # of silently leaking under a redirect attack.
        #
        # Note: FMP keeps the API key in the URL query string rather
        # than a header (auth_fn is None), so this check doesn't apply.
        # The check fires only on policies that PUT the secret in a
        # header that http_get would carry across hops.
        if self.auth_fn is not None and not self.allowed_host_suffixes:
            raise ValueError(
                "HttpPolicy invariant: auth_fn is set without "
                "allowed_host_suffixes — cross-origin redirects would "
                "leak the auth header. Pin allowed_host_suffixes to the "
                "provider domain before constructing this policy."
            )
        # ISS-220 SF-E (Loop34 cycle 1): auth headers MUST NOT be sent
        # over plaintext HTTP. Pre-fix `FD_API_POLICY` had `auth_fn` set
        # but `allowed_schemes` defaulted to `frozenset({"http", "https"})`
        # — a same-host redirect from https→http would pass the SSRF
        # host-check + ISS-213 auth_hostnames check and ship X-API-KEY
        # in cleartext. ISS-213 closed the cross-host axis; this closes
        # the cross-scheme axis. Subset check (`"http" not in ...`) so
        # future https-equivalent schemes (e.g., hypothetical https+ws)
        # remain allowed.
        if self.auth_fn is not None and "http" in self.allowed_schemes:
            raise ValueError(
                "HttpPolicy invariant: auth_fn is set with 'http' in "
                "allowed_schemes — a same-host https→http downgrade "
                "redirect would leak the auth header in cleartext. "
                "Restrict allowed_schemes to {'https'} before "
                "constructing this policy."
            )
        # ISS-220 Loop37 fix-of-fix gap of SF-G: validate proxy_strategy
        # value at construction. Pre-fix http_get checked
        # `if policy.proxy_strategy == "disabled":` only — any typo
        # ("disable" / "DISABLED") silently fell through to ambient
        # urllib proxy handling, defeating the SSRF/host-pin contract.
        # Allowlist invariant prevents typos at module-import time.
        if self.proxy_strategy not in ("disabled", "ambient"):
            raise ValueError(
                f"HttpPolicy invariant: proxy_strategy must be one of "
                f"{{'disabled', 'ambient'}}; got {self.proxy_strategy!r}"
            )


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    attempts: int
    elapsed_ms: int

    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        import json as _json
        return _json.loads(self.body.decode("utf-8"))

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise HttpStatusError(self.status, self.url, self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# Named policies (use via http_get(url, policy=<NAME>))

DEFAULT_POLICY = HttpPolicy()

FD_API_POLICY = HttpPolicy(
    # Legacy `make_request` used `_TIMEOUT = 30`. HttpPolicy default is 15.0s,
    # so FD_API_POLICY MUST set this explicitly or callers silently regress to
    # 15s on slow FD endpoints (Codex post-impl HIGH finding).
    timeout_s=30.0,
    # ISS-173 (Loop22 cycle 1 fresh-session-9): pin redirect target to
    # the FD domain. Pre-fix `allowed_host_suffixes=frozenset()` (default)
    # meant a 302 from api.financialdatasets.ai to https://evil.example/...
    # passed the SSRF private-IP check + got the X-API-KEY auth header
    # (re-applied on every redirect hop via `merged.update(auth_fn())`).
    # Pin the suffix so cross-origin redirects fail at _check_ssrf with
    # "Host not in allowed suffixes" before the auth header reaches the
    # attacker. SEC_POLICY / SEC_FILING_POLICY already follow this
    # pattern; FD path was the auth-bearing exception.
    allowed_host_suffixes=frozenset({"financialdatasets.ai"}),
    # ISS-220 SF-E (Loop34 cycle 1): pin HTTPS-only. Pre-fix the
    # default `frozenset({"http", "https"})` allowed a same-host
    # https→http downgrade redirect to ship X-API-KEY in cleartext.
    # SEC/FMP/Yahoo policies already pin HTTPS-only; FD was missed.
    # The new HttpPolicy.__post_init__ invariant fail-fast at module
    # import if this is removed.
    allowed_schemes=frozenset({"https"}),
    default_headers={
        "Accept": "application/json",
        "User-Agent": "FinancialDataAPI-ClaudeSkill/1.0",
    },
    auth_fn=lambda: {"X-API-KEY": _get_api_key()},
    # ISS-205 (Loop31): JSON adapter — cap decompressed at 64 MiB. Real
    # FD JSON responses are kB-MB; 64 MiB is far above realistic but
    # bounds gzip-bomb expansion vs. the pre-fix 256 MiB ceiling.
    max_decompressed_bytes=64 * 1024 * 1024,
)

# SEC-specific retry set (M5 — Codex post-impl): the pre-DL1 SEC code retried
# on ANY HTTPError except 404. The DL1 default {429, 500, 502, 503, 504} misses
# 403 (SEC's rate-limit signal — returned when crawling faster than ~10 req/s)
# and 408 (timeouts). Extend the retry set so behavior matches prior semantics
# for those transient codes.
_SEC_RETRY_ON = frozenset({403, 408, 429, 500, 502, 503, 504})

SEC_POLICY = HttpPolicy(
    timeout_s=30.0,
    retry_on=_SEC_RETRY_ON,
    # HTTPS-only per hop. Mirrors pre-DL1 `_SecOnlyRedirectHandler` which raised
    # URLError on `parsed.scheme not in ("https",)`.
    allowed_schemes=frozenset({"https"}),
    # ISS-063 (Loop4): SEC submissions/EFTS endpoints must enforce
    # sec.gov suffix on every hop. Pre-fix only blocked private IPs;
    # a redirect to any public HTTPS host would have been allowed,
    # weakening the documented "SEC-only" guarantee.
    allowed_host_suffixes=frozenset({"sec.gov"}),
    default_headers={
        "User-Agent": "stock-v7 research vergil@example.com",
        "Accept-Encoding": "gzip, deflate",
    },
    # ISS-205 (Loop31): SEC submissions/EFTS JSON — cap at 64 MiB.
    max_decompressed_bytes=64 * 1024 * 1024,
)

SEC_FILING_POLICY = HttpPolicy(
    timeout_s=60.0,
    retry_on=_SEC_RETRY_ON,
    max_response_bytes=50 * 1024 * 1024,
    allowed_host_suffixes=frozenset({"sec.gov"}),
    # HTTPS-only per hop — prevents https->http downgrade on redirects to
    # sec.gov hosts. Restores pre-DL1 `_SecOnlyRedirectHandler` semantics.
    allowed_schemes=frozenset({"https"}),
    default_headers={
        "User-Agent": "stock-v7 research vergil@example.com",
        "Accept": "text/html",
    },
    # ISS-220 Loop40 Sec-3 (iter9): SEC HTML filings can be 50 MiB wire
    # and decompress to ~ 5x = 250 MiB; pre-fix this policy was the
    # only one of 6 DL1 policies missing max_decompressed_bytes (the
    # other 5 — FD/SEC/Yahoo/FMP/Finnhub — all set it at ISS-205).
    # 200 MiB cap allows realistic large filings while bounding
    # gzip-bomb expansion. fix-of-fix gap of ISS-205.
    max_decompressed_bytes=200 * 1024 * 1024,
)

YAHOO_CHART_POLICY = HttpPolicy(
    # ISS-203 (Loop29 cycle 1 fresh-session-16): pin host suffix +
    # HTTPS-only. Yahoo chart endpoint has no API key (the
    # HttpPolicy auth-suffix invariant therefore doesn't fire), but
    # defense-in-depth: a 302 from query1.finance.yahoo.com to a
    # public non-Yahoo HTTP(S) host returning a fake chart JSON
    # would parse as legitimate market data and contaminate
    # downstream analysis. Pin yahoo.com so cross-origin redirects
    # fail at _check_ssrf instead of silently sourcing chart data
    # from an untrusted origin.
    allowed_host_suffixes=frozenset({"yahoo.com"}),
    allowed_schemes=frozenset({"https"}),
    default_headers={"User-Agent": "Mozilla/5.0 (compatible; stock-v7/1.0)"},
    # ISS-205 (Loop31): Yahoo chart JSON — cap at 32 MiB (chart payloads
    # are smaller than financial-statement JSON).
    max_decompressed_bytes=32 * 1024 * 1024,
)

FMP_POLICY = HttpPolicy(
    timeout_s=30.0,  # Match prior read-timeout from requests.get(..., timeout=(5, 30)).
    # ISS-176 (Loop23 cycle 1 fresh-session-10): pin host suffix and
    # HTTPS-only. FMP API key is in the URL query string, so a 302 to
    # `https://evil.example/...` would let the attacker observe the
    # User-Agent + the (now-rewritten) URL — and even though urljoin
    # drops the original query string, defense-in-depth means we should
    # never follow cross-origin redirects from an auth-bearing endpoint.
    # Mirrors ISS-173 fix on FD_API_POLICY. Symmetric with SEC_POLICY.
    allowed_host_suffixes=frozenset({"financialmodelingprep.com"}),
    allowed_schemes=frozenset({"https"}),
    default_headers={
        "Accept": "application/json",
        "User-Agent": "stock-v7/1.0",
    },
    # ISS-205 (Loop31): FMP JSON — cap at 64 MiB.
    max_decompressed_bytes=64 * 1024 * 1024,
)

# ISS-177 (Loop23 cycle 1 fresh-session-10): Finnhub fallback in
# financial_datasets._fetch_news_finnhub used safe_urlopen +
# DEFAULT_POLICY (no host pin). Add a dedicated policy so the fallback
# can switch from urllib.urlopen → http_get / safe_http_get_json with
# proper host + scheme constraints.
FINNHUB_POLICY = HttpPolicy(
    timeout_s=10.0,
    allowed_host_suffixes=frozenset({"finnhub.io"}),
    allowed_schemes=frozenset({"https"}),
    default_headers={
        "Accept": "application/json",
        "User-Agent": "fetch/1.0",
    },
    # ISS-205 (Loop31): Finnhub news JSON — cap at 16 MiB (news payloads
    # are tens of kB; 16 MiB is generous defense-in-depth).
    max_decompressed_bytes=16 * 1024 * 1024,
)


# ---------------------------------------------------------------------------
# DL1 private helpers
# ---------------------------------------------------------------------------

def _parse_retry_after(raw: str) -> Optional[float]:
    """Parse a Retry-After header value into seconds-from-now.

    Accepts integer seconds ("120") OR RFC 7231 HTTP-date ("Wed, 21 Oct ...
    GMT"). Returns None on any parse failure; may return <=0 for past dates.
    Naive datetimes (no tz) are assumed UTC.
    """
    s = (raw or "").strip()
    if not s:
        return None
    # Try integer seconds first.
    try:
        return float(s)
    except ValueError:
        pass
    # Fall back to HTTP-date.
    try:
        from email.utils import parsedate_to_datetime
        from datetime import timezone
        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from datetime import datetime
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return delta
    except (TypeError, ValueError, OverflowError, IndexError):
        # IndexError: email.utils raises it on some malformed inputs.
        # Note: email.errors.MessageParseError subclasses ValueError in CPython,
        # so the ValueError catch covers it. Kept explicit in spec for clarity.
        return None


def _lowercase_headers(msg) -> Dict[str, str]:
    """Convert http.client.HTTPMessage (or email.message.Message) to a plain
    dict with lowercased ASCII keys. Multi-valued headers collapse, last wins.
    """
    out: Dict[str, str] = {}
    # msg.items() yields (name, value) duplicates for multi-valued headers;
    # iteration order preserves arrival order, so last assignment wins.
    for key, value in msg.items():
        out[key.lower()] = value
    return out


def _decompress_if_needed(
    body: bytes, headers: Dict[str, str], url: str,
    *, max_decompressed_bytes: int | None = None,
) -> bytes:
    """Decompress response body if Content-Encoding indicates gzip/deflate.

    DL1 default SEC_POLICY sends `Accept-Encoding: gzip, deflate`. Some SEC
    endpoints (efts.sec.gov) gzip the response in kind; others (sec.gov Archives
    filings) don't. Callers that do `resp.body.decode()` or `resp.json()` fail
    with UnicodeDecodeError on gzipped bytes. Found via AAPL smoke test
    post-DL1 Cycle 2.

    Byte-cap is enforced on wire bytes BEFORE decompression (the cap protects
    against streaming-read DoS; decompression happens on already-bounded bytes).

    ISS-113 (Loop8 cycle 2): also cap decompressed bytes. A small (e.g.
    100 KB) gzip body that expands to hundreds of MB ("gzip bomb") used
    to slip through because `_read_with_cap` only enforced the cap on
    wire bytes. Now we stream the decompressor and abort when the
    decompressed-byte counter exceeds `max_decompressed_bytes`. Default
    multiplier 32× the wire cap is generous (real-world gzip ratios on
    JSON/HTML rarely exceed 10–15×) but keeps memory bounded.
    """
    encoding = (headers.get("content-encoding") or "").strip().lower()
    if not encoding or encoding == "identity":
        return body
    if max_decompressed_bytes is None:
        # ISS-129 (Loop9 cycle 1): use a fixed-floor + ratio strategy
        # rather than scaling tightly off compressed length. Pre-fix
        # default `min(32 * max(len(body), 1), 256 * 1024 * 1024)` rejected
        # legitimate small high-ratio responses — a 4 KiB whitespace HTML
        # body compressing to ~40 wire bytes would cap at 32 * 40 = 1280
        # bytes and raise ResponseTooLargeError on the legitimate read.
        # Now: max(64 MiB floor, 32× compressed length), capped at 256 MiB.
        # The 64 MiB floor is well above any realistic JSON / HTML SEC /
        # FD response, but still well-bounded for a gzip-bomb attacker.
        max_decompressed_bytes = min(
            max(64 * 1024 * 1024, 32 * max(len(body), 1)),
            256 * 1024 * 1024,
        )
    try:
        if encoding == "gzip":
            import gzip as _gzip
            import io as _io
            with _gzip.GzipFile(fileobj=_io.BytesIO(body)) as gz:
                return _read_decompressed_with_cap(
                    gz, max_decompressed_bytes, encoding, url,
                )
        if encoding == "deflate":
            import zlib as _zlib
            try:
                return _decompress_deflate_capped(
                    body, _zlib.MAX_WBITS, max_decompressed_bytes,
                    encoding, url,
                )
            except _zlib.error:
                return _decompress_deflate_capped(
                    body, -_zlib.MAX_WBITS, max_decompressed_bytes,
                    encoding, url,
                )
    except ResponseTooLargeError:
        raise
    except Exception as exc:
        # Malformed compressed body — treat as transport-level failure.
        raise HttpTransportError(
            f"Failed to decompress {encoding} response from {url}: {exc}"
        ) from exc
    # Unknown encoding (br, zstd, etc.) — pass through. urllib doesn't
    # negotiate brotli/zstd by default so this is unlikely in practice.
    return body


def _read_decompressed_with_cap(
    stream, cap: int, encoding: str, url: str,
) -> bytes:
    """ISS-113 (Loop8 cycle 2): bounded gzip-stream read. Mirrors
    `_read_with_cap` but for an already-decompressing stream.
    """
    chunks: list[bytes] = []
    bytes_read = 0
    chunk_size = 65536
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        bytes_read += len(chunk)
        if bytes_read > cap:
            raise ResponseTooLargeError(url, cap, bytes_read)
        chunks.append(chunk)
    return b"".join(chunks)


def _decompress_deflate_capped(
    body: bytes, wbits: int, cap: int, encoding: str, url: str,
) -> bytes:
    """ISS-113: bounded deflate decompression via incremental decoder.

    ISS-220 4.2 (Loop32 cycle 2): pre-fix `decoder.decompress(input)`
    was called WITHOUT `max_length`, so a single 64 KiB compressed
    input chunk could inflate to a multi-GB Python bytes object
    BEFORE the post-decompress `bytes_read > cap` check fired. Memory
    pressure / OOM possible before we noticed the breach.

    Now: pass `max_length=remaining + 1` so each call produces at most
    `remaining + 1` bytes (the +1 lets us detect "exceeded by 1" before
    the loop). Process `decoder.unconsumed_tail` until exhausted to
    guarantee progress; raise immediately when bytes_read crosses cap.
    """
    import zlib as _zlib
    decoder = _zlib.decompressobj(wbits)
    chunks: list[bytes] = []
    bytes_read = 0
    chunk_size = 65536
    pos = 0
    while pos < len(body):
        # Pull the next compressed chunk.
        compressed_chunk = body[pos:pos + chunk_size]
        pos += chunk_size
        # Decompress; cap each call's output to `remaining + 1` so
        # over-cap is detected after at most 1 extra byte allocated,
        # not after multi-GB explosion.
        remaining = cap - bytes_read
        if remaining <= 0:
            raise ResponseTooLargeError(url, cap, bytes_read + 1)
        chunk = decoder.decompress(compressed_chunk, max_length=remaining + 1)
        if chunk:
            bytes_read += len(chunk)
            if bytes_read > cap:
                raise ResponseTooLargeError(url, cap, bytes_read)
            chunks.append(chunk)
        # Process unconsumed_tail — decompress() returns up to max_length
        # bytes; if the input chunk would have produced more, the tail
        # remains in decoder. Loop until consumed before fetching new
        # input.
        while decoder.unconsumed_tail:
            remaining = cap - bytes_read
            if remaining <= 0:
                raise ResponseTooLargeError(url, cap, bytes_read + 1)
            tail_chunk = decoder.decompress(
                decoder.unconsumed_tail, max_length=remaining + 1,
            )
            if not tail_chunk:
                break  # defensive; shouldn't happen
            bytes_read += len(tail_chunk)
            if bytes_read > cap:
                raise ResponseTooLargeError(url, cap, bytes_read)
            chunks.append(tail_chunk)
    # Final flush — buffered output. Cap-check still applies.
    tail = decoder.flush()
    if tail:
        bytes_read += len(tail)
        if bytes_read > cap:
            raise ResponseTooLargeError(url, cap, bytes_read)
        chunks.append(tail)
    return b"".join(chunks)


def _read_with_cap(stream, cap: int, url: str) -> bytes:
    """Stream bytes with O(cap) memory bound. Raises ResponseTooLargeError if
    total bytes read exceeds cap. Works on both urlopen response objects and
    HTTPError instances (both expose .read(size))."""
    chunks: list[bytes] = []
    bytes_read = 0
    while True:
        chunk = stream.read(65536)  # 64 KiB stride
        if not chunk:
            break
        bytes_read += len(chunk)
        if bytes_read > cap:
            raise ResponseTooLargeError(url, cap, bytes_read)
        chunks.append(chunk)
    return b"".join(chunks)


import random
import time as _time


def _check_ssrf(url: str, policy: HttpPolicy) -> tuple[str, tuple[tuple[int, str], ...]]:
    """Raise SsrfBlockedError if url's host fails scheme / suffix / private checks.

    Check order (C2-H1 — Codex Cycle 2):
      1. scheme (policy.allowed_schemes)
      2. suffix allowlist (if configured) — BEFORE DNS lookup. Refusing unknown
         hosts without a DNS probe is both more secure and avoids pointless
         retries when the host would fail suffix check anyway.
      3. is_private_host — may raise socket.gaierror on unresolvable host;
         the caller (http_get) treats that as a transport error (retry-eligible),
         NOT an SSRF block.

    Scheme check uses policy.allowed_schemes (default {http, https}; SEC
    policies narrow to {https} to prevent https->http redirect downgrade).

    Returns (host, validated_ips) where validated_ips is a tuple of
    (family, ip_str) pairs that passed the public-IP check. http_get
    pins the request resolver to this set so a DNS-rebinding attack
    cannot flip the host's resolution between this check and urlopen
    (ISS-210 Loop31 fresh-session-18). On literal IP hosts the tuple
    is single-element.
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in policy.allowed_schemes:
        raise SsrfBlockedError(
            f"Blocked URL scheme: {scheme} (allowed: {sorted(policy.allowed_schemes)})"
        )
    host = (parsed.hostname or "").lower()
    if policy.allowed_host_suffixes:
        if not any(host == s or host.endswith("." + s) for s in policy.allowed_host_suffixes):
            raise SsrfBlockedError(
                f"Host {host} not in allowed suffixes {sorted(policy.allowed_host_suffixes)}"
            )
    # Public-IP gate + IP collector for DNS-rebinding pin (ISS-220 4.1).
    # is_private_host writes to `resolved` when host is public AND
    # _out_ips is provided. Mock test paths (`patch(is_private_host,
    # return_value=False)`) won't write — fallback below covers them.
    resolved: list[tuple[int, str]] = []
    if is_private_host(host, _out_ips=resolved):
        raise SsrfBlockedError(f"Blocked private/internal host: {host}")
    if not host:
        raise SsrfBlockedError("Blocked: empty hostname")
    # If is_private_host populated `resolved`, we have a pre-validated
    # public-IP set with no second DNS resolve — DNS rebinding window
    # is closed. Otherwise (mock test path), do a single direct resolve
    # to obtain the pin set.
    if not resolved:
        try:
            addr = ipaddress.ip_address(host.strip("[]"))
            family = socket.AF_INET if addr.version == 4 else socket.AF_INET6
            return host, ((family, str(addr)),)
        except ValueError:
            pass
        # Mock-path fallback: assume host is safe (the patch assertion);
        # do a single resolve to get the pin set.
        # ISS-220 Loop37 fix-of-fix gap: do NOT wrap socket.gaierror as
        # HttpTransportError here — http_get's retry loop catches that
        # exception class as fail-fast. socket.gaierror is OSError, which
        # IS in http_get's retry-eligible except chain (line 1170). Let
        # it propagate naturally so transient DNS blips retry properly.
        infos = _ORIGINAL_GETADDRINFO(host, None, 0, socket.SOCK_STREAM)
        for fam, _type, _proto, _canon, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            resolved.append((fam, ip_str))
    if not resolved:
        raise SsrfBlockedError(f"No IPs resolved for host: {host}")
    return host, tuple(resolved)


def _validate_header_safe(name: str, value: str) -> None:
    """Reject header name/value pairs containing control characters that
    would permit CRLF header injection / request smuggling.

    C4-M1 (Codex Cycle 4): urllib.request.Request.add_header does NOT
    validate at add time; it raises bare `ValueError: Invalid header value`
    from the transport layer. That bypasses our `HttpError` taxonomy and
    breaks the exception contract Cycle 3 locked in. Validate ahead of
    time and raise a typed HttpError instead.
    """
    # Header names must be RFC 7230 tokens (no whitespace, no control chars).
    # Keep the check conservative: reject any ASCII control or non-ASCII byte.
    if not name or any(ord(c) < 0x21 or ord(c) == 0x7F for c in name):
        raise HttpError(f"Invalid header name: {name!r}")
    # Header values must not contain CR/LF (request smuggling). urllib's
    # post-hoc validation also rejects other control chars; match that.
    for c in value:
        o = ord(c)
        if o == 0x0A or o == 0x0D or o == 0x00:
            raise HttpError(f"Invalid header value for {name!r}: contains CR/LF/NUL")


def http_get(
    url: str,
    *,
    policy: HttpPolicy = DEFAULT_POLICY,
    headers: Optional[Mapping[str, str]] = None,
) -> HttpResponse:
    """DL1 canonical HTTP GET primitive (4B — FINAL).

    Returns HttpResponse for 2xx / 3xx / non-retryable 4xx / non-retryable 5xx.
    Raises SsrfBlockedError / ResponseTooLargeError / HttpTransportError /
    RetryExhaustedError / HttpError (on invalid header input) on the paths
    documented in the spec.
    """
    # ISS-213 (Loop31 cycle 1 fresh-session-18): build base headers WITHOUT
    # auth and compute the auth-host pin set up-front. auth_fn() output is
    # injected per-hop based on whether the current redirect-hop host is in
    # auth_host_set. Default behavior when auth_fn is set but auth_hostnames
    # is empty: pin to the ORIGINAL request hostname only (strictest).
    base_merged: Dict[str, str] = {}
    base_merged.update(policy.default_headers)
    if headers:
        base_merged.update(headers)
    # C4-M1: validate header contents up front so CRLF injection surfaces
    # as a typed HttpError instead of urllib's bare ValueError. Validate
    # base headers + (lazily) auth headers below before each hop.
    for k, v in base_merged.items():
        _validate_header_safe(k, v)
    # ISS-220 Loop39 Sec-2 (iter8): caller-supplied sensitive headers
    # (Authorization, Cookie, X-API-KEY, *-token) MUST share the same
    # auth-host gate as `policy.auth_fn` output. Pre-fix only auth_fn
    # output was gated; a caller passing `headers={"Authorization":
    # "Bearer ..."}` directly bypassed `auth_host_set` and replayed
    # the credential on every redirect hop. Now: lift caller-supplied
    # sensitive headers out of `base_merged` into the gated
    # `auth_headers` set.
    # ISS-221 Loop41 Sec-1 (iter10 structural): classification delegated
    # to `_is_sensitive_header_name()` (regex predicate at module top)
    # which is the single source of truth shared with redaction. The
    # iter8/iter9 hardcoded set is gone — codex Loop41 found
    # `GitHub-Token` / `Private-Token` / `X-Access-Token` slipped past;
    # extending the list could not catch the next vendor token name.
    # The predicate over-matches token-suffix names by design (false-
    # positive = pin to auth host; false-negative = leak cross-host).
    caller_auth: Dict[str, str] = {}
    for k in list(base_merged.keys()):
        if _is_sensitive_header_name(k):
            caller_auth[k] = base_merged.pop(k)
    auth_headers: Dict[str, str] = dict(caller_auth)
    auth_host_set: frozenset[str] = frozenset()
    if policy.auth_fn is not None:
        for k, v in policy.auth_fn().items():
            _validate_header_safe(k, v)
            auth_headers[k] = v
    if auth_headers:
        if policy.auth_hostnames:
            auth_host_set = policy.auth_hostnames
        else:
            # Default: pin to the original request host. This is the
            # strictest interpretation; broader scope must be explicit.
            initial_host = (urllib.parse.urlparse(url).hostname or "").lower()
            auth_host_set = frozenset({initial_host}) if initial_host else frozenset()

    start_s = _time.monotonic()
    # Tag the most recent failure so retry exhaustion raises the right error type.
    # plan-review fix H3: prior version used parallel last_status + last_error vars;
    # if a status-retry then a transport-retry happened, last_status bled through
    # and RetryExhaustedError fired instead of HttpTransportError.
    last_failure: Optional[tuple] = None  # ("status", (code, body, headers)) OR ("transport", exc)

    for attempt in range(policy.max_retries):
        redirect_count = 0
        current_url = url
        try:
            while True:  # redirect loop, within one attempt
                checked_host, validated_ips = _check_ssrf(current_url, policy)

                class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                        raise urllib.request.HTTPError(newurl, code, msg, hdrs, fp)

                # ISS-220 SF-G (Loop35 cycle 1): disable ambient proxy
                # by default. `ProxyHandler({})` forces direct connection
                # — urllib without it would honor `HTTP_PROXY` env vars
                # and route auth-bearing traffic through an unvetted
                # proxy, bypassing all SSRF / host-pin / DNS-pin
                # defenses. "ambient" mode opts back in for dev/MITM
                # debugging.
                handlers = [_NoRedirectHandler]
                if policy.proxy_strategy == "disabled":
                    handlers.append(urllib.request.ProxyHandler({}))
                # else "ambient": fall through; default opener picks
                # up urllib.request.getproxies() (env + system config).
                opener = urllib.request.build_opener(*handlers)
                req = urllib.request.Request(current_url, method="GET")
                # ISS-213: per-hop merge — base headers always; auth_headers
                # only when current_host is in auth_host_set. Drop auth on
                # redirect to a sibling host (e.g. cdn.<suffix>) even though
                # allowed_host_suffixes accepts it for navigation.
                hop_host = (urllib.parse.urlparse(current_url).hostname or "").lower()
                hop_merged: Dict[str, str] = dict(base_merged)
                if auth_headers and hop_host in auth_host_set:
                    hop_merged.update(auth_headers)
                for k, v in hop_merged.items():
                    req.add_header(k, v)

                # ISS-210 (Loop31 cycle 1 fresh-session-18): pin DNS for
                # this request to the IPs validated by _check_ssrf. Pre-fix
                # urllib resolved the hostname a second time at socket
                # connect, leaving a TOCTOU window between the public-IP
                # validation and the actual connection — DNS rebinding
                # could flip a public-resolving hostname to a private IP
                # during that window. Now urlopen sees only the validated
                # IPs (filtered to AF_INET if FORCE_IPV4 is set). Other
                # hostnames the same call might dereference (rare — only
                # SOCKS proxies / system resolver fallbacks) fall through
                # to the original resolver. The lock serializes pin
                # install/remove with any concurrent http_get on the same
                # process.
                pinned_ips_for_host = validated_ips
                if _is_force_ipv4():
                    pinned_ips_for_host = tuple(
                        (fam, ip) for (fam, ip) in pinned_ips_for_host
                        if fam == socket.AF_INET
                    )
                if not pinned_ips_for_host:
                    # ISS-220 Loop40 Sec-1 (iter9): fail-closed on empty
                    # pin set. Pre-fix fallback `_ipv4_only_getaddrinfo`
                    # did a fresh `_ORIGINAL_GETADDRINFO` resolve at
                    # connect time, reopening the DNS-rebinding TOCTOU
                    # window that ISS-210 (Loop31 4.1) was specifically
                    # built to close. The only way pin set goes empty
                    # is FORCE_IPV4=1 against an IPv6-only host, which
                    # is a config error or DNS-spoof attack — neither
                    # warrants completing the connection.
                    raise SsrfBlockedError(
                        f"FORCE_IPV4=1 left no validated IPv4 pins for "
                        f"{checked_host}; refusing to fall back to "
                        f"unsafer fresh resolve at connect time"
                    )
                pinned_resolver = _make_pinned_getaddrinfo(
                    checked_host, pinned_ips_for_host,
                )
                try:
                    with _GETADDRINFO_LOCK:
                        socket.getaddrinfo = pinned_resolver
                        try:
                            raw_resp = opener.open(req, timeout=policy.timeout_s)
                        finally:
                            socket.getaddrinfo = _ORIGINAL_GETADDRINFO
                except urllib.error.HTTPError as e:
                    # 3xx / 4xx / 5xx
                    try:
                        if 300 <= e.code < 400:
                            location = e.headers.get("Location")
                            if not location:
                                raise HttpTransportError("Redirect with no Location header") from e
                            redirect_count += 1
                            if redirect_count > policy.max_redirects:
                                raise HttpTransportError(
                                    f"too many redirects (>{policy.max_redirects})"
                                ) from e
                            current_url = urllib.parse.urljoin(current_url, location)
                            continue
                        # 4xx / 5xx
                        status = e.code
                        headers_out = _lowercase_headers(e.headers)
                        body = _read_with_cap(e, policy.max_response_bytes, current_url)
                        body = _decompress_if_needed(
                            body, headers_out, current_url,
                            max_decompressed_bytes=policy.max_decompressed_bytes,
                        )
                    finally:
                        try:
                            e.close()
                        except Exception:  # defensive
                            pass
                else:
                    # 2xx happy path
                    with raw_resp as resp_cm:
                        status = resp_cm.status
                        headers_out = _lowercase_headers(resp_cm.headers)
                        body = _read_with_cap(resp_cm, policy.max_response_bytes, current_url)
                        body = _decompress_if_needed(
                            body, headers_out, current_url,
                            max_decompressed_bytes=policy.max_decompressed_bytes,
                        )

                # Status routing
                if status in policy.retry_on:
                    last_failure = ("status", (status, body, headers_out))
                    break  # exit redirect loop, fall to retry branch
                # All other statuses (2xx, 3xx-after-redirect, non-retryable 4xx/5xx) return.
                elapsed_ms = int((_time.monotonic() - start_s) * 1000)
                return HttpResponse(
                    status=status,
                    url=current_url,
                    headers=headers_out,
                    body=body,
                    attempts=attempt + 1,
                    elapsed_ms=elapsed_ms,
                )
        except (SsrfBlockedError, ResponseTooLargeError):
            raise
        except HttpTransportError:
            raise
        except urllib.error.URLError as e:
            last_failure = ("transport", e)
        except TimeoutError as e:
            last_failure = ("transport", e)
        except OSError as e:
            last_failure = ("transport", e)

        # Retry branch — decide sleep duration
        if attempt + 1 >= policy.max_retries:
            break
        sleep_s: Optional[float] = None
        if last_failure is not None and last_failure[0] == "status" and policy.honor_retry_after:
            _, (_, _, hdrs) = last_failure
            hv = hdrs.get("retry-after")
            if hv is not None:
                parsed = _parse_retry_after(hv)
                if parsed is not None and parsed > 0:
                    sleep_s = min(parsed, policy.retry_after_cap_s)
        if sleep_s is None:
            sleep_s = policy.backoff_base_s * (2 ** attempt) + random.uniform(0, policy.backoff_jitter_s)
        _time.sleep(sleep_s)

    # Retry exhausted — dispatch based on the tag of the MOST RECENT attempt's
    # failure, not any earlier attempt's state (plan-review H3 fix).
    if last_failure is None:
        # Defensive; shouldn't reach. All loops must set last_failure or return.
        raise HttpTransportError(f"{policy.max_retries} attempts failed with no recorded error")
    kind, value = last_failure
    if kind == "status":
        status, body, _ = value
        raise RetryExhaustedError(status, url, policy.max_retries, body)
    # transport
    raise HttpTransportError(
        f"{policy.max_retries} attempts failed: {value}"
    ) from value


class _LegacyResponseShim:
    """Mimics urlopen() return object. Body is pre-read bytes; cursor
    semantics approximate http.client.HTTPResponse.read(n): successive calls
    return successive slices, EOF returns b"".
    """

    def __init__(self, resp: HttpResponse):
        self._resp = resp
        self._offset = 0

    def read(self, n: Optional[int] = -1) -> bytes:
        body = self._resp.body
        if self._offset >= len(body):
            return b""
        if n is None or n < 0:
            chunk = body[self._offset:]
            self._offset = len(body)
            return chunk
        chunk = body[self._offset : self._offset + n]
        self._offset += len(chunk)
        return chunk

    def readline(self) -> bytes:
        body = self._resp.body
        if self._offset >= len(body):
            return b""
        idx = body.find(b"\n", self._offset)
        if idx < 0:
            chunk = body[self._offset:]
            self._offset = len(body)
            return chunk
        chunk = body[self._offset : idx + 1]
        self._offset = idx + 1
        return chunk

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    @property
    def status(self) -> int:
        return self._resp.status

    @property
    def headers(self):
        return self._resp.headers

    def getheader(self, name: str, default=None):
        return self._resp.get_header(name, default)

    def geturl(self) -> str:
        return self._resp.url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# ---------------------------------------------------------------------------
# IPv4-only DNS resolution (opt-in via env var)
#
# ISS-039 (Loop2 backlog) — known limitation:
# This works by monkey-patching `socket.getaddrinfo` for the duration of
# the urllib `opener.open()` call. The `_GETADDRINFO_LOCK` serializes
# DL1 http_get calls so two http_get's never race on the monkeypatch
# replacement+restoration. HOWEVER: while http_get holds the lock and
# the global getaddrinfo is replaced, ANY OTHER code in the same process
# that calls `socket.getaddrinfo` directly — yfinance's `requests`-based
# session, third-party libraries with their own HTTP stack, etc. —
# would also see the IPv4-only resolver during that window.
#
# In the current codebase this is safe because:
#   - macro.py's ThreadPoolExecutor pool all routes through
#     `fetch_yahoo_quote_result` → http_get (one DNS path, lock-serialized).
#   - yfinance fallback (`_run_yfinance_fallback_impl`) runs sequentially
#     after the parallel chart fetches complete, never during a held lock.
#
# If a future refactor introduces parallel non-DL1 HTTP (yfinance Session
# spawned from a worker thread + http_get from another thread), this
# monkeypatch may produce surprising IPv4-forced behavior in the non-DL1
# path. The proper fix is migrating to a `requests` Session with a
# custom HTTPConnection that calls AF_INET-only at the socket layer,
# tracked as a separate refactor.
#
# ISS-220 4.X DEFER (Loop36 cycle 1, comment-only): re-confirmed by
# codex Loop36 Security review at HEAD 299cf67 — "MEDIUM concurrency"
# rating (dispatcher WEAK). Stays deferred because:
#   1) Current call graph is single-thread per fetch run (verified
#      by code-architect Loop36 review).
#   2) Stdlib-only constraint (CLAUDE.md non-obvious convention)
#      forbids httpx / requests-with-custom-HTTPConnection.
# When DL3+ refactor lands a non-DL1 concurrent HTTP path, this
# comment block is the breakpoint to revisit.
# ---------------------------------------------------------------------------

_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_GETADDRINFO_LOCK = threading.Lock()


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Resolve using IPv4 only when explicitly requested."""
    return _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)


def _make_pinned_getaddrinfo(pinned_host: str, pinned_ips: tuple[tuple[int, str], ...]):
    """ISS-210 (Loop31 cycle 1 fresh-session-18): build a per-request
    `socket.getaddrinfo` replacement that returns ONLY the IPs that
    `_check_ssrf` validated for `pinned_host`, eliminating the
    DNS-rebinding TOCTOU window between SSRF validation and urlopen.

    Other hostnames passed through this resolver fall back to the
    original (this is the rare path — `urllib.request.urlopen` only
    looks up the request's own hostname under normal flow).
    """
    pinned_host = pinned_host.lower()
    def _resolver(host, port, family=0, type=0, proto=0, flags=0):
        if (host or "").lower() == pinned_host:
            # Build addrinfo tuples mirroring socket.getaddrinfo's shape
            # with the validated IP set. socket.SOCK_STREAM matches
            # urllib's expected type; type/proto args from caller take
            # precedence when caller asks for a specific family.
            results = []
            for fam, ip in pinned_ips:
                if family and family != fam:
                    continue
                # sockaddr shape: (ip, port) for AF_INET, (ip, port, 0, 0) for AF_INET6
                if fam == socket.AF_INET:
                    sockaddr = (ip, port or 0)
                else:
                    sockaddr = (ip, port or 0, 0, 0)
                results.append((fam, type or socket.SOCK_STREAM, proto, "", sockaddr))
            if not results:
                # Caller's family filter excluded everything — defer to
                # ORIGINAL with the requested family (will likely fail).
                return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
            return results
        # Different host — defer to original resolver
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
    return _resolver


def _is_force_ipv4() -> bool:
    """Check FORCE_IPV4 at call time (after .env loading)."""
    return os.environ.get("FINANCIAL_DATASETS_FORCE_IPV4") == "1"


# ---------------------------------------------------------------------------
# .env loading + API key
# ---------------------------------------------------------------------------

def _find_project_root() -> Optional[Path]:
    """Walk up from this file to find the project root (contains CLAUDE.md or .git)."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "CLAUDE.md").is_file() or (current / ".git").exists():
            return current
        current = current.parent
    return None


# ISS-220 SF-G (Loop35 cycle 1): allowlist of env keys that .env may
# inject. Closes the proxy-bypass auxiliary surface — pre-fix .env
# could carry `HTTP_PROXY=http://attacker.example/` which would route
# all DL1 traffic through the attacker's proxy. Now only the 4 known
# project-config keys are loaded; anything else is silently ignored
# (no print to stdout to avoid CLI clutter).
_ENV_KEY_ALLOWLIST: frozenset[str] = frozenset({
    "FINANCIAL_DATASETS_API_KEY",
    "FINNHUB_API_KEY",
    "FMP_API_KEY",
    "FINANCIAL_DATASETS_FORCE_IPV4",
})


def _load_env() -> None:
    """Load .env file from project root (best-effort, cross-platform).

    ISS-220 4.7 (Loop32 cycle 2): only the project-root `.env` is loaded.
    Pre-fix this function fell back to `Path.cwd() / ".env"` when no
    project root was detected — an installed-package context running
    from an untrusted directory could inject arbitrary env vars
    (proxy settings, feature flags, API keys). Pattern matches
    Pattern N (cwd-root-detection) which `audit_fail_open` already
    flags as MED elsewhere; honor the same convention here.

    ISS-220 SF-G (Loop35 cycle 1): only inject keys in
    `_ENV_KEY_ALLOWLIST`. Pre-fix `os.environ.setdefault(key, value)`
    accepted any KEY=VALUE line, which let a project-root .env
    smuggle proxy / feature-flag / debug-toggle vars (the proxy case
    being SSRF-relevant — see SF-G `proxy_strategy` field).
    """
    root = _find_project_root()
    if root is None:
        # No project root → don't probe CWD. Caller's environment must
        # already have the required vars (or fail at API-key resolution
        # later, which is the proper fail-closed signal).
        return
    env_path = root / ".env"
    if not env_path.is_file():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                if key not in _ENV_KEY_ALLOWLIST:
                    continue  # silently skip non-allowlisted keys
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)


if not os.environ.get("FINANCIAL_DATASETS_API_KEY"):
    _load_env()

_TIMEOUT = 30
_MAX_RETRIES = 3
_RETRY_DELAYS = [1, 2, 4]


class MissingApiKeyError(Exception):
    """Auth/config error: API key environment variable not set.

    ISS-164 (Loop20 cycle 1 fresh-session-7): pre-fix `_get_api_key`
    raised RuntimeError, which adapter_error_from_exception's catch-all
    routes to INTERNAL_ERROR — making an auth/config problem look like
    an adapter bug. Now a typed exception that the canonical mapper
    routes to UNAUTHORIZED.
    """


def _get_api_key() -> str:
    """Return the API key or raise MissingApiKeyError."""
    key = os.environ.get("FINANCIAL_DATASETS_API_KEY", "")
    if not key:
        raise MissingApiKeyError(
            "FINANCIAL_DATASETS_API_KEY environment variable not set."
        )
    return key


# ---------------------------------------------------------------------------
# SSRF protection -- shared host validation
# ---------------------------------------------------------------------------

def safe_urlopen(url: str, *, timeout: int = 60, headers: Optional[dict] = None):
    """[LEGACY] Returns _LegacyResponseShim (file-like-ish).

    Thin wrapper around http_get with max_retries=1 preserving single-shot
    semantics. New code: prefer http_get directly.

    Behavior changes from v6 (documented):
    - SsrfBlockedError (subclass of both HttpError and ValueError) replaces
      the bare ValueError previously raised — legacy `except ValueError` at
      callsites continues to work.
    - 4xx/5xx responses are returned as _LegacyResponseShim.status (caller
      can inspect), NOT raised as HTTPError. Grep for `except HTTPError`
      around safe_urlopen callers (Step 1 did this).
    """
    one_shot_policy = replace(
        DEFAULT_POLICY,
        timeout_s=float(timeout),
        max_retries=1,
    )
    resp = http_get(url, policy=one_shot_policy, headers=headers)
    return _LegacyResponseShim(resp)


def is_private_host(hostname: str, _out_ips: Optional[list] = None) -> bool:
    """Return True if hostname resolves to a private/reserved IP range.

    Catches RFC1918, link-local, loopback, and IPv6 equivalents.
    Empty/None -> True (conservative: block unknown).

    *_out_ips* (ISS-220 4.1, Loop32 cycle 2): optional list. When provided
    AND the host validates as public, this function appends
    `(family, ip_str)` tuples for every resolved address. http_get's
    `_check_ssrf` uses this to obtain the validated IP set without
    a second `socket.getaddrinfo` call — eliminating the DNS-rebinding
    TOCTOU window between SSRF validation and urlopen.

    Default None preserves the bool-returning legacy contract for the
    28 test sites that mock this function via
    `patch("scripts.sources.common.is_private_host", return_value=False)`
    — those mocks won't write to _out_ips, so http_get falls back to
    a single direct resolve for the pin set (mock paths assume host
    is safe; production paths get the new defense).
    """
    if not hostname:
        return True
    hostname = hostname.lower().strip("[]")
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return True
    try:
        addr = ipaddress.ip_address(hostname)
        # ISS-220 4.16 (Loop34 cycle 1): combined check — `not is_global`
        # catches CGNAT (100.64.0.0/10) and unspecified (0.0.0.0); but
        # Python stdlib treats multicast (224.0.0.0/4) AS is_global=True
        # (since multicast packets traverse the internet). Multicast
        # remains an SSRF target (loopback multicast / mDNS / link-
        # local multicast) so we add explicit `is_multicast` reject.
        if not addr.is_global or addr.is_multicast:
            return True
        # ISS-220 4.1 (Loop32 cycle 2): write back to caller's IP collector
        # for DNS-rebinding pin (literal-IP shortcut).
        if _out_ips is not None:
            family = socket.AF_INET if addr.version == 4 else socket.AF_INET6
            _out_ips.append((family, str(addr)))
        return False
    except ValueError:
        pass
    # Resolution failure propagates: let the caller (http_get SSRF check)
    # distinguish "unresolvable" (retry-eligible transport failure) from
    # "resolves to a private IP" (permanent SSRF block). Previously this
    # swallowed gaierror and returned True, classifying every transient DNS
    # blip as an SSRF violation with no retry path (C2-H1 — Codex Cycle 2).
    infos = socket.getaddrinfo(hostname, None, 0, socket.SOCK_STREAM)
    has_private = False
    collected: list = []
    for family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
            # ISS-220 4.16 (Loop34 cycle 1): same combined check as
            # literal-IP path — not is_global OR is_multicast.
            if not addr.is_global or addr.is_multicast:
                has_private = True
                # do NOT add non-global / multicast IPs to the pin set
                continue
            collected.append((family, ip_str))
        except ValueError:
            continue
    if has_private:
        return True
    # ISS-220 4.1 (Loop32 cycle 2): publish the resolved public-IP set
    # to the caller's pin collector. _check_ssrf passes a list it owns;
    # http_get uses these IPs to install a per-request pinned resolver
    # for the upcoming urlopen (DNS-rebinding TOCTOU defense).
    if _out_ips is not None:
        _out_ips.extend(collected)
    return False


# ---------------------------------------------------------------------------
# Country classification -- shared across source adapters
# ---------------------------------------------------------------------------

_US_COUNTRY_VALUES = frozenset({
    "US", "USA", "U.S", "U.S.", "U.S.A", "U.S.A.",
    "UNITED STATES", "UNITED STATES OF AMERICA",
})


def is_us_country(country: str) -> bool:
    """Return True if country string represents the United States.

    Case-insensitive exact match. Empty/None -> True (conservative: assume domestic).
    """
    if not country or not country.strip():
        return True
    return country.strip().upper() in _US_COUNTRY_VALUES


# ---------------------------------------------------------------------------
# Core HTTP functions
# ---------------------------------------------------------------------------

def create_ssl_context() -> ssl.SSLContext:
    """Return an SSL context for HTTPS connections."""
    return ssl.create_default_context()


def _safe_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {"detail": body.decode("utf-8", errors="replace")[:500]}


def safe_http_get_json(
    url: str,
    *,
    policy: "HttpPolicy",
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """Adapter-grade JSON-fetch wrapper: status check + JSON parse.

    Returns ``Any`` (not ``Dict[str, Any]``): callers must consult
    `isinstance(result, list/dict)` because some endpoints return
    JSON arrays at the root (e.g. FMP filings_v3 list response —
    fmp.py:114 branches via `isinstance(raw, list)`). ISS-220 Loop40
    Arch-2: pre-fix annotation was `Dict[str, Any]` which lied about
    list-returning endpoints and could mislead future adapter
    authors / static type-checkers.

    Structural fix (post Loop21, addresses Pattern 1+2 root cause):
    8 fresh-session rounds repeatedly found "raw http_get followed by
    .json() with no status check" — ISS-149 (make_request) + ISS-161
    (yahoo) + ISS-165 (finnhub) + ISS-169 (404 magic dict) all the
    same root pattern in different sites. Each round fixed one site;
    the next round found another. This helper centralizes the
    contract so future raw callers can't reintroduce the gap.

    Contract:
      - Calls http_get(url, policy=policy, headers=headers)
      - Lets typed exceptions propagate untouched (RetryExhaustedError,
        HttpTransportError, SsrfBlockedError, ResponseTooLargeError) —
        adapter_error_from_exception has explicit rows for each
      - 4xx/5xx → raises HttpStatusError(status, url, body) — canonical
        mapper routes to NOT_FOUND (404) / UNAUTHORIZED (401/403) /
        RATE_LIMIT (429) / UPSTREAM_ERROR (5xx)
      - 2xx → returns resp.json() (parse error → ValueError →
        canonical mapper routes to PARSE_ERROR)

    audit_fail_open Pattern T enforces use: any `http_get(...)` followed
    by `.json()` without an intervening `resp.status >= 400` check is
    flagged as a structural violation.

    Why a wrapper instead of changing http_get itself: http_get is the
    DL1 transport primitive used by both DL2 adapters AND legacy non-
    adapter callers (e.g. one-off CLI scripts). Some legacy callers
    legitimately want to inspect 4xx responses (e.g. to read the error
    body for diagnostics). The wrapper is opt-in for adapter-grade
    consumers; legacy callers can keep using raw http_get.
    """
    resp = http_get(url, policy=policy, headers=headers)
    if resp.status >= 400:
        raise HttpStatusError(resp.status, url, resp.body)
    parsed = resp.json()
    # ISS-220 4.29 (Loop36 cycle 1): post-parse object-size cap.
    # Pre-fix the wire/decompressed byte cap bounds body bytes, but
    # JSON parses into Python objects whose `sys.getsizeof` total can
    # be 4-10× the wire bytes (small ints / short strings carry
    # per-object header overhead). Deeply nested or many-key objects
    # could allocate hundreds of MB despite a 16 MiB wire cap.
    # Cap at 4× max_decompressed_bytes (or default 256 MiB) — safe
    # multiplier for typical JSON nesting; bombing inputs raise
    # ResponseTooLargeError which the canonical mapper routes to
    # RESPONSE_TOO_LARGE (Row 6).
    #
    # ISS-220 Loop37 Sec-1 KNOWN-limitation: cap fires AFTER `resp.json()`
    # finishes parsing — the inherent limitation of stdlib `json.loads`
    # which has no streaming/depth/object-count bounds. A 16-64 MiB
    # decompressed body has already allocated up to that × parser-overhead
    # before this check runs. True pre-parse defense would require a
    # streaming parser (ijson / non-stdlib). Wire+decompressed cap on
    # `policy.max_response_bytes` establishes the hard upper bound on
    # parse-time peak memory.
    json_cap = (
        (policy.max_decompressed_bytes or 64 * 1024 * 1024) * 4
    )
    _validate_json_object_size(parsed, max_bytes=json_cap, url=url)
    return parsed


def _validate_json_object_size(obj, *, max_bytes: int, url: str) -> None:
    """ISS-220 4.29 (Loop36 cycle 1): post-parse Python object-size cap.

    Iterative `sys.getsizeof` walk with id() guard for cycles. JSON
    parse output cannot contain cycles, but the guard is cheap and
    defensive. Raises `ResponseTooLargeError` when total exceeds
    max_bytes — the typed exception routes through
    `adapter_error_from_exception` Row 6 to RESPONSE_TOO_LARGE.

    ISS-220 Loop37 fix-of-fix gap: pre-fix recursive `_size()` hit
    Python's default 1000-deep RecursionError on legitimately deep
    JSON before reaching the `total > max_bytes` check, surfacing
    as INTERNAL_ERROR. Iterative stack-based traversal handles
    arbitrary depth bounded only by available memory.
    """
    import sys
    seen: set[int] = set()
    total = 0
    stack = [obj]
    while stack:
        o = stack.pop()
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        total += sys.getsizeof(o)
        if total > max_bytes:
            # Bail early — exact total above cap is not needed.
            raise ResponseTooLargeError(url, max_bytes, total)
        if isinstance(o, dict):
            for k, v in o.items():
                stack.append(k)
                stack.append(v)
        elif isinstance(o, (list, tuple, set, frozenset)):
            for x in o:
                stack.append(x)


def make_request(url: str, debug: bool = False) -> Dict[str, Any]:
    """[LEGACY] FD API GET. Returns JSON-decoded dict.

    Thin wrapper around http_get(policy=FD_API_POLICY). DL2 cleans up
    magic-key 404 semantics.

    ISS-149 (Loop16 cycle 1 fresh-session-3): pre-fix swallowed typed
    DL1 exceptions and re-raised as RuntimeError. RetryExhaustedError /
    HttpTransportError became RuntimeError; 401/402/4xx/5xx all became
    RuntimeError with the status embedded in the message string. Then
    every FD adapter wrapping `_make_request` in
    `try: ... except Exception as e: return adapter_error_from_exception(e, ...)`
    saw RuntimeError, which the canonical mapper routes to INTERNAL_ERROR
    (Row 10 catch-all). Net effect: a 429 → INTERNAL_ERROR (wrong code,
    wrong retryable, wrong severity). Fixed by letting typed exceptions
    propagate so the mapper sees them as the appropriate row:
      - RetryExhaustedError(429) → RATE_LIMIT (retryable=True)
      - HttpTransportError → HTTP_TRANSPORT (retryable=True)
      - HttpStatusError(401) → UNAUTHORIZED (retryable=False)
      - HttpStatusError(402) → UNAUTHORIZED (per spec)
      - HttpStatusError(404) → NOT_FOUND (preserved magic-dict for
        legacy 404 path that callers depend on)
      - HttpStatusError(5xx) → UPSTREAM_ERROR (retryable=True)
    """
    # Structural simplification (post Loop21): make_request is now a
    # thin wrapper over safe_http_get_json. Logic for status check +
    # JSON parse + typed-exception propagation lives in the shared
    # helper so future raw callers can't bypass it. See
    # safe_http_get_json docstring for the full contract rationale
    # (consolidates ISS-149 / ISS-161 / ISS-165 / ISS-169 root cause).
    if debug:
        # Pre-call debug print: lazily evaluated once safe_http_get_json
        # returns or raises. We don't have access to attempts/status
        # here without calling http_get directly, so the debug message
        # is now coarser (URL + outcome only). Acceptable trade-off
        # given debug=True is dev-only.
        print(f"[DEBUG] {url} (via safe_http_get_json)", file=sys.stderr)
    return safe_http_get_json(url, policy=FD_API_POLICY)


def make_api_request(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """HTTP GET for a relative endpoint (prepends ``BASE_URL``).

    The endpoint is a path relative to BASE_URL
    (e.g. ``"/financials/income-statements"``).
    params is an optional dict of query-string key/value pairs.
    """
    if params:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{BASE_URL}{endpoint}?{query}" if query else f"{BASE_URL}{endpoint}"
    else:
        url = f"{BASE_URL}{endpoint}"

    return make_request(url)


def retry_with_backoff(
    fn: Callable,
    max_retries: int = 3,
    delays: Optional[List[float]] = None,
) -> Any:
    """Generic retry with exponential backoff.

    Args:
        fn: Callable to execute (no arguments).
        max_retries: Maximum number of attempts.
        delays: List of delay-seconds between retries.
                If None, uses [1, 2, 4] exponential backoff.

    Returns:
        The return value of fn on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    if delays is None:
        delays = [1, 2, 4]
    if max_retries < 1:
        max_retries = 1

    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1 and attempt < len(delays):
                time.sleep(delays[attempt])

    raise last_error  # type: ignore[misc]


def create_provenance(
    source: str,
    fallback_stage: int = 0,
) -> Dict[str, Any]:
    """Create a provenance metadata dict.

    Args:
        source: Identifier for the data source (e.g. "financial-datasets-api").
        fallback_stage: 0 = primary, 1 = first fallback, 2 = second fallback, etc.

    Returns:
        Dict with keys: source, fallback_stage, as_of (ISO timestamp), confidence.
        confidence = 1.0 - 0.1 * fallback_stage (floored at 0.0).
    """
    confidence = max(0.0, 1.0 - 0.1 * fallback_stage)
    return {
        "source": source,
        "fallback_stage": fallback_stage,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "confidence": confidence,
    }
