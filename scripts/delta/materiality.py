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
    # Historical name, SESSION basis (feedback 2026-08-29 monitor ④): true
    # when the news fetch belongs to the run's current trading session, not
    # to the calendar day. A calendar comparison is false on every
    # non-trading day, which fail-opened the events gate every weekend.
    fetch_timestamp_today: bool
    # None on a legacy classifier output that predates the field. Counts
    # every article the classifier did NOT classify — out of window OR
    # undatable — so the three counts partition `total_articles` exactly.
    excluded_count: int | None = None


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
        `partial` forever. An empty body degrades the classification, it
        does not void it — `source` is a separate input field, so the
        whitelist test is untouched, and the category test falls back to
        the title. `sources_with_content` remains in the health dataclass
        for visibility, just not as a gating condition; `probe_inputs`
        warns on an all-empty batch and names what that can cost.
        """
        return (
            self.health.fetch_timestamp_today
            and self.health.total_articles > 0
            # An output with no `excluded_count` is one whose count partition
            # could not be checked at all. There is no legacy to excuse it —
            # The classifier output is a same-day transient each skill CLEARS
            # before its own dispatch (`.classifier_output.json` for
            # score-business, `.classifier_output.thesis.json` for
            # investment-thesis — two windows, two artifacts), so absence
            # means a FRESH classifier ignored its contract. Reading that as healthy let its
            # `material_count: 0` drive a `no_op` and reuse yesterday's
            # events with the window unverifiable (codex review 2026-08-29).
            # Unhealthy here fails OPEN to a rerun, which is the safe
            # direction; raising instead would churn every stored fixture for
            # the same outcome.
            and self.health.excluded_count is not None
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
    articles: List[dict], since_date: str, session_date: str | None = None,
    fetch_timestamp: str | None = None,
) -> dict:
    """Filter articles to those with `published_at >= since_date`
    (INCLUSIVE) and package for the classifier prompt. Spec §6.3.

    `session_date` is the run's trading-SESSION anchor and is what the
    prompt judges `fetch_timestamp_today` against (feedback 2026-08-29
    monitor ④ — judged against the CALENDAR day it is necessarily false on
    every non-trading day, which fail-opened the events gate on a weekend
    batch with no new article in it). Defaults to the current session; the
    two SKILL dispatch paths pass their run directory's own date, and this
    parameter lets a caller do the same.

    `fetch_timestamp` is the OTHER half of that comparison — the news fetch's
    own instant (`00_validation.json:validated_at`). The classifier cannot
    observe it: `03_company_news.json` carries only `{company, news}`. It is
    passed through verbatim, `None` included, because unknown freshness must
    reach the classifier AS unknown; the prompt then reports `false`, which
    fails toward re-analysis. Both SKILL dispatch paths supply it, so this
    parameter keeps the two paths on ONE contract (producer-consumer §4).

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
    if session_date is None:
        from scripts.delta.calendar import session_et
        session_date = session_et().isoformat()
    return {"since_date": since_date, "session_date": session_date,
            "fetch_timestamp": fetch_timestamp, "articles": filtered}


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
    # Round-19: the list fields must BE lists — `list({})` silently turned
    # a shape-drifted dict into [] (count==len passes at 0), and the health
    # ints accepted bool (int subclass), so `total_articles: true` read as
    # 1 article → input_healthy fabricated → tier no_op / events reuse on
    # schema-invalid classifier output.
    for list_key in ("material_list", "low_signal_headlines"):
        if not isinstance(raw[list_key], list):
            raise ValueError(
                f"{list_key} must be a list, got {type(raw[list_key]).__name__}"
            )
    for health_int_key in ("total_articles", "sources_with_content"):
        hv = h[health_int_key]
        if isinstance(hv, bool) or not isinstance(hv, int):
            raise ValueError(
                f"classifier_input_health.{health_int_key} must be an "
                f"integer, got {type(hv).__name__}"
            )
    if raw["material_count"] != len(raw["material_list"]):
        raise ValueError(
            f"material_count={raw['material_count']} disagrees with "
            f"len(material_list)={len(raw['material_list'])}"
        )
    # `excluded_count` (feedback 2026-08-29 monitor ⑤) closes the scope gap
    # that made the counts unsummable: material/low_signal cover only the
    # classified articles while total_articles covers the whole input, so a
    # correct output read `10 / 0 / 9` and any consumer adding them up was
    # wrong by construction. It is "everything not classified", NOT just
    # "out of window" — the first spelling left an article with an unreadable
    # `published_at` in no bucket at all, so a fully compliant classifier
    # broke the identity and fail-opened the run (codex review 2026-08-29).
    # OPTIONAL — a legacy classifier output that omits it validates exactly as
    # before; when the classifier DOES report it, the fields must reconcile,
    # because an unreconcilable set means a window we cannot reconstruct.
    # Counts cannot be negative. UNCONDITIONAL — this used to sit inside the
    # `excluded_count in h` branch below, so a validator that says it forbids
    # negatives accepted `low_signal_count: -1` whenever the field was absent
    # (adversarial pass, 2026-08-29). Ordered AFTER the type checks above,
    # which is what makes the comparisons safe.
    # `material_count` is deliberately absent: the len(material_list) check
    # above already rejects a negative one, so a branch here could never be
    # entered — and a branch nothing can reach is worse than no branch.
    for neg_key, neg_val in (("low_signal_count", raw["low_signal_count"]),
                             ("total_articles", h["total_articles"]),
                             ("sources_with_content", h["sources_with_content"])):
        if neg_val < 0:
            raise ValueError(f"{neg_key} must not be negative, got {neg_val}")
    if "excluded_count" in h:
        oow = h["excluded_count"]
        if isinstance(oow, bool) or not isinstance(oow, int):
            raise ValueError(
                f"classifier_input_health.excluded_count must be an "
                f"integer, got {type(oow).__name__}"
            )
        if oow < 0:
            raise ValueError(f"excluded_count must not be negative, got {oow}")
        total = raw["material_count"] + raw["low_signal_count"] + oow
        if total != h["total_articles"]:
            raise ValueError(
                f"classifier_input_health.excluded_count does not "
                f"reconcile: material_count={raw['material_count']} + "
                f"low_signal_count={raw['low_signal_count']} + "
                f"excluded_count={oow} = {total}, but "
                f"total_articles={h['total_articles']}"
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
            excluded_count=(int(h["excluded_count"])
                                 if "excluded_count" in h else None),
        ),
    )


def classify_news(
    articles: List[dict],
    since_date: str,
    llm_runner,  # Callable[[str, dict], dict]
    session_date: str | None = None,
    fetch_timestamp: str | None = None,
) -> ClassifierOutput:
    """Spec-mandated API (§6.3/§9): pre-process articles, dispatch the
    classifier prompt via `llm_runner(prompt_path, context)`, validate
    the result, return a ClassifierOutput.

    `llm_runner` is dependency-injected: in production it's the agent-
    dispatch callable from the orchestrating SKILL.md; in unit tests
    it's a stub that returns a canned dict. This keeps the wrapper
    testable without mocking the entire Task harness.
    """
    context = prepare_classifier_input(articles, since_date, session_date,
                                       fetch_timestamp)
    raw = llm_runner("prompts/delta/classify-news.md", context)
    return validate_classifier_output(raw)
