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


def prepare_classifier_input(
    articles: List[dict], since_date: str
) -> dict:
    """Filter articles to those with `published_at > since_date` (strict)
    and package for the classifier prompt. Spec §6.3.
    """
    since_dt = datetime.date.fromisoformat(since_date)
    filtered = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        pub = a.get("published_at")
        # Defensive: skip non-string published_at (int/None/dict etc.)
        # Slicing a non-str raises TypeError; fromisoformat needs str anyway.
        if not isinstance(pub, str) or not pub:
            continue
        try:
            pub_dt = datetime.date.fromisoformat(pub[:10])
        except ValueError:
            continue
        if pub_dt > since_dt:  # strict: exclude since_date itself
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
