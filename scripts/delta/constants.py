"""Shared vocabulary for the delta layer.

Centralize string enums that appear across probe, resolver, run_meta,
portfolio_classify, and the SKILL.md shell case statements. A single
typo like "noop" vs "no_op" would otherwise go undetected until runtime.

Shell-level callers (SKILL.md) can't import these — they restate the
values verbatim in bash case blocks — but every Python consumer should
reference these constants rather than string literals.
"""

from __future__ import annotations

# BQ tier vocabulary (spec §6.1 + §6.2).
TIER_FULL = "full"
TIER_PARTIAL = "partial"
TIER_NO_OP = "no_op"
TIER_PROBE = "probe"  # fetch.py intermediate value; non-terminal at assemble
BQ_TIERS = (TIER_FULL, TIER_PARTIAL, TIER_NO_OP)
FETCH_TIERS = (TIER_PROBE, *BQ_TIERS)

# Events reuse decisions (spec §7.2).
DECISION_REUSE = "reuse"
DECISION_RERUN = "rerun"
EVENTS_DECISIONS = (DECISION_REUSE, DECISION_RERUN)

# Skill identifiers consumed by resolver + run_meta write.
SKILL_BQ = "score-business"
SKILL_THESIS = "investment-thesis"
SKILL_INDUSTRY = "research-industry"
SKILLS = (SKILL_BQ, SKILL_THESIS, SKILL_INDUSTRY)

# Industry skill uses 21d safety valve (vs BQ's 14d) — industry-level
# structure changes more slowly than per-stock fundamentals.
INDUSTRY_PARTIAL_TIER_DAYS_SAFETY_VALVE = 21
INDUSTRY_FULL_TIER_DAYS_CEILING = 90

# Portfolio staleness states (spec §8.1).
STATE_FRESH = "fresh"
STATE_STALE_BQ = "stale_bq"
STATE_STALE_THESIS = "stale_thesis"
STATE_BQ_ONLY = "bq_only"
STATE_NONE = "none"
PORTFOLIO_STATES = (
    STATE_FRESH, STATE_STALE_BQ, STATE_STALE_THESIS,
    STATE_BQ_ONLY, STATE_NONE,
)
