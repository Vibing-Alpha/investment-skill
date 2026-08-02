"""Classifier wrapper: prepare input, invoke LLM via the orchestration
layer, validate output.

The actual LLM call is done by the orchestrating SKILL.md (which spawns
a subagent with `prompts/delta/classify-news.md`). This module provides
the pure-Python pre/post-processing so the orchestration layer is thin.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import List


@dataclass
class ClassifierHealth:
    total_articles: int
    sources_with_content: int
    fetch_timestamp_today: bool


@dataclass
class ClassifierOutput:
    material_count: int
    material_list: List[dict]
    low_signal_count: int
    low_signal_headlines: List[str]
    health: ClassifierHealth

    @property
    def input_healthy(self) -> bool:
        """Two-condition health: fetch is today AND articles exist.

        Relaxed from 3-condition (#6, 2026-04-19 MU smoke): dropped the
        `sources_with_content > 0` requirement. Financial Datasets'
        news API routinely returns valid headlines with empty summary
        bodies, which previously forced every BQ probe to fail-open to
        `partial` forever. The classifier prompt judges materiality on
        headlines alone, so empty summaries do not invalidate its
        output. `sources_with_content` remains in the health dataclass
        for visibility, just not as a gating condition.
        """
        return (
            self.health.fetch_timestamp_today
            and self.health.total_articles > 0
        )


def article_date_usable(published_at) -> bool:
    """True when `published_at` will survive `prepare_classifier_input`'s
    window filter: a non-empty str whose first 10 chars parse as an ISO
    date. ONE implementation, two callers (producer-consumer rule 3): the
    filter below AND fetch.py's news date post-pass. The post-pass once
    judged usability as "any non-blank string", so a `"07/30/2026"` date
    kept the category PASSED while this module silently dropped the
    article from the probe window (twenty-second cold round)."""
    if not isinstance(published_at, str) or not published_at:
        return False
    try:
        datetime.date.fromisoformat(published_at[:10])
    except ValueError:
        return False
    return True


def prepare_classifier_input(
    articles: List[dict], since_date: str
) -> dict:
    """Filter articles to those with `published_at >= since_date`
    (INCLUSIVE) and package for the classifier prompt. Spec §6.3.

    Probe 4E: the window was strict (`>`), but timestamps are truncated to
    dates — an article published later ON the prior run's own date (e.g. a
    23:00Z material contract after a morning run) was deterministically
    dropped by every subsequent run. Inclusive is the conservative fix:
    since_date advances each run, so an article participates in at most 2
    consecutive runs; the worst case is one extra `partial` for a material
    article the prior run already saw — never a silently missed one.
    """
    since_dt = datetime.date.fromisoformat(since_date)
    filtered = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        pub = a.get("published_at")
        if not article_date_usable(pub):
            continue
        pub_dt = datetime.date.fromisoformat(pub[:10])
        if pub_dt >= since_dt:  # inclusive — see probe 4E note above
            filtered.append(a)
    return {"since_date": since_date, "articles": filtered}


def validate_classifier_output(raw: dict) -> ClassifierOutput:
    """Raise ValueError if the LLM output doesn't match the expected shape.
    On shape mismatch the orchestrator should fail-open to tier=partial.
    """
    # A non-dict top-level (valid JSON `[]` / `"error"` / number) is a shape
    # mismatch — raise ValueError (which callers catch to fail-open) rather than
    # let `raw.keys()` below throw AttributeError and crash the probe.
    if not isinstance(raw, dict):
        raise ValueError(
            f"classifier output must be a JSON object, got {type(raw).__name__}"
        )
    required = {
        "material_count", "material_list", "low_signal_count",
        "low_signal_headlines", "classifier_input_health",
    }
    missing = required - set(raw.keys())
    if missing:
        raise ValueError(f"classifier output missing keys: {missing}")

    h = raw["classifier_input_health"]
    for key in ("total_articles", "sources_with_content", "fetch_timestamp_today"):
        if key not in h:
            raise ValueError(f"classifier_input_health missing {key!r}")

    # Strict bool check: LLMs sometimes emit string "false" which bool()
    # coerces to truthy, silently passing Gate 1 when it should fail-open.
    if not isinstance(h["fetch_timestamp_today"], bool):
        raise ValueError(
            f"classifier_input_health.fetch_timestamp_today must be bool, "
            f"got {type(h['fetch_timestamp_today']).__name__}"
        )

    # Second cold round: counts must be REAL integers (a float 0.9 was
    # silently truncated to 0 by int()), and material_count must agree with
    # the list it claims to count — a classifier reporting count=0 beside a
    # non-empty material_list would otherwise steer the tier to no_op while
    # the evidence says otherwise. Cross-field consistency is checked HERE
    # (inside the LLM's own output); it cannot validate the LLM's claims
    # about its INPUT (health echo) — that remains the documented contract.
    for count_key in ("material_count", "low_signal_count"):
        v = raw[count_key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(
                f"{count_key} must be an integer, got {type(v).__name__}"
            )
    if raw["material_count"] != len(raw["material_list"]):
        raise ValueError(
            f"material_count={raw['material_count']} disagrees with "
            f"len(material_list)={len(raw['material_list'])}"
        )

    return ClassifierOutput(
        material_count=int(raw["material_count"]),
        material_list=list(raw["material_list"]),
        low_signal_count=int(raw["low_signal_count"]),
        low_signal_headlines=list(raw["low_signal_headlines"]),
        health=ClassifierHealth(
            total_articles=int(h["total_articles"]),
            sources_with_content=int(h["sources_with_content"]),
            fetch_timestamp_today=h["fetch_timestamp_today"],
        ),
    )


def classify_news(
    articles: List[dict],
    since_date: str,
    llm_runner,  # Callable[[str, dict], dict]
) -> ClassifierOutput:
    """Spec-mandated API (§6.3/§9): pre-process articles, dispatch the
    classifier prompt via `llm_runner(prompt_path, context)`, validate
    the result, return a ClassifierOutput.

    `llm_runner` is dependency-injected: in production it's the agent-
    dispatch callable from the orchestrating SKILL.md; in unit tests
    it's a stub that returns a canned dict. This keeps the wrapper
    testable without mocking the entire Task harness.
    """
    context = prepare_classifier_input(articles, since_date)
    raw = llm_runner("prompts/delta/classify-news.md", context)
    return validate_classifier_output(raw)
