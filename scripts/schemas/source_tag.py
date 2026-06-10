"""Shared source-tag canonical validator.

Enforces the `[KIND: descriptor]` form declared by
`.claude/rules/anti-hallucination.md` — descriptors must carry
real semantic content, not placeholder-theater tokens like
`field_name`. Walks dict and list artifacts recursively; applies
to leaf string values under keys literally named `"source"`.

Imported by:
- scripts.schemas.bq_analysis (load_bq_analysis)
- scripts.schemas.investment_thesis (load_investment_thesis)
- scripts.schemas.industry_analysis (load_industry_analysis)
- scripts.assemble (fresh-dim WebSearch binding gate + marker stamp)
- scripts.thesis.stamp_thesis_meta / stamp_events_meta (marker stamp)
- tests.test_prompt_lint (DL6 linter uses identical constants)
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Mapping

from scripts.schemas.errors import SchemaError


SOURCE_TAG_RE: re.Pattern[str] = re.compile(
    r"\[(API|WebSearch|Filing|Calc)\s*:\s*(\S[^\]]*?)\]"
)

# --- WebSearch host-capability binding (Plan B Task 6) ---------------------
#
# Strict-mode grammar for the WebSearch KIND: every tag must bind the
# claim to outlet + url + access-date —
#     [WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]
# A bare `[WebSearch: outlet]` is a tag SHAPE the model can emit from
# memory; the url/access-date binding is what forces a real search result
# behind the citation (anti-hallucination, money-path).
#
# Compat shape (matches the `_dl3c_version` convention): strict binding
# applies ONLY to artifacts carrying the root marker
# `_websearch_binding_version: 1`, which is stamped DETERMINISTICALLY by
# the producers (scripts/assemble.py on full-tier runs,
# scripts.thesis.stamp_thesis_meta / stamp_events_meta, the
# research-industry SKILL validation step). Legacy artifacts (no marker)
# are validated by the old rule exactly — historical reports keep loading.

WEBSEARCH_BINDING_MARKER: str = "_websearch_binding_version"
WEBSEARCH_BINDING_VERSION: int = 1

# Descriptor grammar (applied to SOURCE_TAG_RE group 2 of a WebSearch tag).
# url: http(s) scheme required, no whitespace; outlet: anything up to the
# first comma (urls with literal commas must be percent-encoded).
# DELIBERATE leniency (codex post-impl, deferred): the url group ACCEPTS
# literal commas — the doc above instructs percent-encoding, but the lazy
# `[^\s\]]+?` backtracks so `, accessed` still anchors deterministically
# and a real comma-bearing url parses rather than failing the bind. Do NOT
# tighten to reject commas: that would fail-close on legitimate urls.
WEBSEARCH_BOUND_RE: re.Pattern[str] = re.compile(
    r"^(?P<outlet>[^,\]]+?),\s*(?P<url>https?://[^\s\]]+?),\s*"
    r"accessed\s+(?P<date>\d{4}-\d{2}-\d{2})$"
)

PLACEHOLDER_DESCRIPTORS: frozenset[str] = frozenset({
    # bare placeholder words
    "field", "field_name", "formula", "source", "value", "name",
    "metric", "example", "tbd", "todo", "description",
    # angle-bracket variants
    "<field>", "<field_name>", "<formula>", "<source>",
    "<value>", "<metric>", "<ticker>",
})


def check_websearch_binding(value: str, *, artifact: str, path: str) -> None:
    """Strict-mode check: every [WebSearch: ...] tag in `value` must be
    bound — `[WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]` with an
    http(s) url and a real calendar date. Non-WebSearch tags and tag-free
    prose are ignored. Raises SchemaError on the first violation.
    """
    for m in SOURCE_TAG_RE.finditer(value):
        if m.group(1) != "WebSearch":
            continue
        descriptor = m.group(2).strip()
        bm = WEBSEARCH_BOUND_RE.match(descriptor)
        if bm is None:
            raise SchemaError(
                artifact, path,
                f"unbound WebSearch tag {m.group(0)!r}: strict binding "
                f"requires [WebSearch: <outlet>, <url>, accessed "
                f"<YYYY-MM-DD>] (url must be http(s), no whitespace)",
            )
        outlet = bm.group("outlet").strip()
        if outlet.casefold() in PLACEHOLDER_DESCRIPTORS:
            raise SchemaError(
                artifact, path,
                f"placeholder-theater outlet {outlet!r} in {m.group(0)!r}",
            )
        date_s = bm.group("date")
        try:
            datetime.date.fromisoformat(date_s)
        except ValueError:
            raise SchemaError(
                artifact, path,
                f"invalid access date {date_s!r} in {m.group(0)!r}",
            ) from None


def websearch_binding_active(
    data: Any, *, artifact: str = "artifact"
) -> bool:
    """Read the root binding marker. Absent → False (legacy rule).
    Exactly `_websearch_binding_version: 1` (int) → True (strict).
    Any other value → SchemaError (fail-closed on typo'd/newer markers;
    mirrors dispatch_dl3c_mode's illegal-version handling).
    """
    if not isinstance(data, Mapping):
        return False
    v = data.get(WEBSEARCH_BINDING_MARKER)
    if v is None:
        return False
    if (isinstance(v, int) and not isinstance(v, bool)
            and v == WEBSEARCH_BINDING_VERSION):
        return True
    raise SchemaError(
        artifact, WEBSEARCH_BINDING_MARKER,
        f"unsupported {WEBSEARCH_BINDING_MARKER} {v!r} "
        f"(supported: {WEBSEARCH_BINDING_VERSION})",
    )


def stamp_websearch_binding(data: Mapping[str, Any]) -> dict:
    """Return a new dict with the binding marker as the FIRST key
    (insertion order = serialization order per PEP 468). Idempotent;
    mirrors cli_utils.emit_dl3c_root_marker. Callers are the
    DETERMINISTIC producers only — the marker is the contract switch
    that turns on strict WebSearch validation at every future load.
    """
    new: dict = {WEBSEARCH_BINDING_MARKER: WEBSEARCH_BINDING_VERSION}
    for k, v in data.items():
        if k != WEBSEARCH_BINDING_MARKER:
            new[k] = v
    return new


def check_source_tag(
    value: str, *, artifact: str, path: str, strict_websearch: bool = False
) -> None:
    """Raise SchemaError on degenerate or placeholder-theater tag.

    Public API: per-artifact loaders can call this directly when they
    need suffix-key scoping (e.g. investment_thesis.calculation_audit
    validates `*_source` keys narrowly).

    `strict_websearch=True` additionally requires every WebSearch tag in
    the value to carry the url + access-date binding (marked artifacts).
    """
    m = SOURCE_TAG_RE.search(value)
    if not m:
        raise SchemaError(
            artifact, path, f"non-canonical source tag: {value!r}"
        )
    descriptor = m.group(2).strip().casefold()
    if descriptor in PLACEHOLDER_DESCRIPTORS:
        raise SchemaError(
            artifact, path,
            f"placeholder-theater descriptor {descriptor!r} in {value!r}",
        )
    if strict_websearch:
        check_websearch_binding(value, artifact=artifact, path=path)


def validate_source_tags(
    obj: Any,
    *,
    artifact: str,
    strict_websearch: bool = False,
    max_depth: int = 64,
    _depth: int = 0,
    _path: str = "<root>",
) -> None:
    """Recursively walk dict and list; check every `"source"` leaf string.

    `artifact`: caller-provided name (e.g. "bq_analysis") for error paths.
    `strict_websearch`: when True (binding-marked artifacts), EVERY string
    leaf — not just `"source"` keys — is scanned for WebSearch tags, each
    of which must carry the url + access-date binding. Citations live in
    evidence/interpretation prose as much as in `source` fields; scanning
    only `source` keys would miss most of them. Lenient mode (legacy
    artifacts) is byte-for-byte the old behavior.
    `max_depth`: defensive circuit-breaker; no real artifact exceeds 10.
    """
    if _depth > max_depth:
        raise SchemaError(
            artifact, _path,
            f"source-tag walker exceeded max_depth={max_depth}",
        )
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            child = f"{_path}.{k}" if _path != "<root>" else k
            if k == "source":
                if not isinstance(v, str):
                    raise SchemaError(
                        artifact, child,
                        f"source-tag value must be str, got {type(v).__name__}",
                    )
                check_source_tag(
                    v, artifact=artifact, path=child,
                    strict_websearch=strict_websearch,
                )
            else:
                validate_source_tags(
                    v, artifact=artifact,
                    strict_websearch=strict_websearch,
                    max_depth=max_depth,
                    _depth=_depth + 1, _path=child,
                )
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            validate_source_tags(
                item, artifact=artifact,
                strict_websearch=strict_websearch,
                max_depth=max_depth,
                _depth=_depth + 1, _path=f"{_path}[{i}]",
            )
    elif strict_websearch and isinstance(obj, str):
        # Strict mode only: prose/evidence string leaves must not carry
        # unbound WebSearch citations. (Lenient mode never inspects them.)
        check_websearch_binding(obj, artifact=artifact, path=_path)
