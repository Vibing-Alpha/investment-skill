"""Shared source-tag canonical validator.

Enforces the `[KIND: descriptor]` form declared by
`.claude/rules/anti-hallucination.md` — descriptors must carry
real semantic content, not placeholder-theater tokens like
`field_name`. Walks dict and list artifacts recursively; applies
to leaf string values under keys literally named `"source"`.

Imported by:
- scripts.schemas.bq_analysis (load_bq_analysis)
- scripts.schemas.investment_thesis (load_investment_thesis)
- tests.test_prompt_lint (DL6 linter uses identical constants)
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from scripts.schemas.errors import SchemaError


SOURCE_TAG_RE: re.Pattern[str] = re.compile(
    r"\[(API|WebSearch|Filing|Calc)\s*:\s*(\S[^\]]*?)\]"
)

PLACEHOLDER_DESCRIPTORS: frozenset[str] = frozenset({
    # bare placeholder words
    "field", "field_name", "formula", "source", "value", "name",
    "metric", "example", "tbd", "todo", "description",
    # angle-bracket variants
    "<field>", "<field_name>", "<formula>", "<source>",
    "<value>", "<metric>", "<ticker>",
})


def check_source_tag(value: str, *, artifact: str, path: str) -> None:
    """Raise SchemaError on degenerate or placeholder-theater tag.

    Public API: per-artifact loaders can call this directly when they
    need suffix-key scoping (e.g. investment_thesis.calculation_audit
    validates `*_source` keys narrowly).
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


def validate_source_tags(
    obj: Any,
    *,
    artifact: str,
    max_depth: int = 64,
    _depth: int = 0,
    _path: str = "<root>",
) -> None:
    """Recursively walk dict and list; check every `"source"` leaf string.

    `artifact`: caller-provided name (e.g. "bq_analysis") for error paths.
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
                check_source_tag(v, artifact=artifact, path=child)
            else:
                validate_source_tags(
                    v, artifact=artifact, max_depth=max_depth,
                    _depth=_depth + 1, _path=child,
                )
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            validate_source_tags(
                item, artifact=artifact, max_depth=max_depth,
                _depth=_depth + 1, _path=f"{_path}[{i}]",
            )
    # primitive leaves (str / int / float / bool / None): nothing to do
