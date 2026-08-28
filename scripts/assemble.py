"""Assemble bq_analysis.json from score files + synthesis.json + validation data.

Handles the mechanical merging that was previously done by the synthesis agent,
reducing agent output from ~50KB to ~10KB and cutting synthesis time by ~70%.

Usage:
    python3 -m scripts.assemble --report-dir reports/AAPL/20260406
"""

import argparse
import datetime
import json
import math
import sys
from datetime import timezone
from pathlib import Path

from scripts.cli_utils import emit_dl3c_root_marker, read_json, write_output
from scripts.constants import CATEGORIES, Status
from scripts.delta.calendar import today_et, last_closed_trading_day

PREFIX = "assemble"
OUTPUT_VERSION = "8.0"

# Fields in score files that are redundant in the dimensions section
STRIP_KEYS = {"dimension", "ticker", "data_freshness", "scoring_calculation"}

DEFAULT_WEIGHTS = {"fundamental": 0.35, "forward": 0.35, "industry": 0.30}
DIMENSIONS = list(DEFAULT_WEIGHTS.keys())

# Which dimension score files are FRESH (agent-rerun) per delta tier —
# mirrors the orchestrator's AGENTS_RUN map in
# .claude/skills/score-business/SKILL.md (full: fundamental,forward,
# industry; partial: forward,industry; no_op: none). Fresh dims are
# strict-gated for WebSearch source binding; reused dims stay lenient.
WEBSEARCH_FRESH_DIMS_BY_TIER = {
    "full": ("fundamental", "forward", "industry"),
    "partial": ("forward", "industry"),
    "no_op": (),
}

# DL3c §3.7.4: scoped set of DL3c-gated artifacts whose `dl3c_mode` must be
# consistent across an assemble run. `peer_multiples.json` is NOT in this
# scope — it's always USD (yfinance, USD-normalized; not a cert consumer
# per §3.7.2) and including it would block every converted-ticker run.
DL3C_GATED_ARTIFACTS = ("fcf_inputs", "historical_multiples", "adr_correction")


def build_meta(
    ticker,
    validation,
    freshness_interpretation,
    analysis_date,
    tier_context,
):
    """Build the meta section.

    tier_context is a dict loaded from --tier-context-json:
    {
      "tier_this_run": "full" | "partial" | "no_op",
      "component_provenance": {
        "dimensions.fundamental": {"source_date": "...", "reason": "..."},
        ...
      }
    }
    """
    if analysis_date is None:
        # ET trading day, NOT UTC date (spec §11). Matters at UTC-midnight
        # boundaries where UTC and ET days differ.
        analysis_date = today_et().isoformat()

    financials = validation.get("categories", {}).get("financials", {})
    latest_period = financials.get("latest_period")

    freshness_note = compute_freshness_note(validation)
    if freshness_interpretation:
        freshness_note = (
            f"{freshness_note}. {freshness_interpretation}"
            if freshness_note else freshness_interpretation
        )

    validation_status, degraded_categories = _summarize_degradation(validation)

    return {
        "ticker": ticker,
        "analysis_date": analysis_date,
        "generated_at": datetime.datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "market_asof_date": last_closed_trading_day().isoformat(),
        "data_freshness": latest_period,
        "freshness_note": freshness_note,
        # Machine-readable companions to freshness_note. The prose note is
        # written by an LLM and read by humans; these two are what
        # /portfolio and /monitor gate on.
        "validation_status": validation_status,
        "degraded_categories": degraded_categories,
        "output_version": OUTPUT_VERSION,
        "tier_this_run": tier_context["tier_this_run"],
        "component_provenance": tier_context["component_provenance"],
    }


# Category statuses that mean the run lost data. Uses the canonical Status
# constants so a vocabulary change here can't silently drift. SKIPPED is
# excluded on purpose (a deliberate `--categories` scope choice, not damage);
# so is ADR_CHECK (informational). CIRCUIT_BREAKER IS included — it means the
# fetch was cut off mid-run, and compute_freshness_note already treats it as
# an anomaly.
_DEGRADED_CATEGORY_STATUSES = frozenset({
    Status.FAILED, Status.PARTIAL, Status.INCOMPLETE,
    Status.WARNING, Status.CIRCUIT_BREAKER,
})
# SKIPPED is excluded UNCONDITIONALLY — a DELIBERATE TRADEOFF proposed and
# rejected in four separate cold reviews, latterly as "tier-aware: SKIPPED
# should gate on terminal full/partial runs". The measured counterexample
# refutes exactly that scoping: MU 20260522 is a stored FULL-tier run with
# `filing` legitimately SKIPPED — an operator --categories scope choice,
# the documented reason SKIPPED is a deliberate decision and not damage —
# so the tier-aware rule turns a real, healthy stored run into a standing
# false veto. Do not re-propose without a mechanism that distinguishes an
# operator scope choice from truncation better than tier labels can.

# The statuses that mean a category is AFFIRMATIVELY fine. The gate check is
# clean-whitelist-shaped (eleventh cold round), not damage-blacklist-shaped:
# a status outside BOTH sets — missing, empty, or unknown vocabulary
# ("CORRUPT", a number, a drifted producer's new word) — is evidence the
# record is broken, and unknown must fail toward degraded. A blacklist read
# every unrecognised value as clean. Measured: all 43 stored runs use only
# canonical statuses, so the inversion moves nothing (15/43 unchanged).
_CLEAN_CATEGORY_STATUSES = frozenset({
    Status.PASSED, Status.SKIPPED, Status.ADR_CHECK,
})

# The only top-level statuses fetch.py's final_status can produce. Anything
# else in a stored validation is corruption; _summarize_degradation clamps it
# to None (UNKNOWN) rather than parroting it into `meta.validation_status`,
# where a non-null string beside an empty list is exactly the pair the
# classifier reads as post-change and explicitly clean.
_CANONICAL_TOP_STATUSES = frozenset({
    Status.PASSED, Status.PARTIAL, Status.FAILED, Status.INCOMPLETE,
})

# Only these tiers gate a decision. The auxiliary tier is the entire
# baseline-noise floor: measured across all 43 stored 00_validation.json
# files, segmented_revenues is degraded on 42, earnings on 42 and
# eps_validation on 40. Gating on those would fire on 43/43 runs — a
# permanent veto on every new entry — while the clean branch stayed 0/43
# dead code.
_GATING_IMPORTANCE = frozenset({"critical", "important"})

# Categories that gate DESPITE an auxiliary tag in CATEGORIES, because a
# scoring dimension depends on them. Auxiliary is a FETCH judgement — "its
# absence must not abort the run" — and that is not the same judgement as
# "a decision may be taken without it".
#
# Both entries feed score-forward's "EPS Expectations (weight: 20)", a fifth
# of the forward score: `06_analyst_estimates` carries the consensus and the
# revision trend, `07_earnings` the "beat/miss history (8 quarters minimum)".
# Losing either one left `degraded_categories: []` while forward still
# returned a full score with that dimension unsourced, and `/portfolio` could
# open a position on it.
#
# The VALUE is the status set that counts as a real loss for that category,
# because the two have different baselines. `analyst_estimates` is PASSED on
# 32/43 stored runs, so any damaged status there is a genuine loss.
#
# `earnings` is PARTIAL on 30/43, and that state does NOT mean "some quarters
# returned, not the full 8" — inspected, it is usually a filing STUB
# (accession_number / filing_url / filing_window) carrying no EPS at all,
# with `press_releases` empty. PARTIAL is therefore the steady state of the
# feed for most tickers, not a per-run loss, and it cannot discriminate one.
# Gating on it takes the corpus from 15/43 to 24/43 and re-creates the
# permanent entry veto this criterion exists to avoid. Only a hard failure —
# nothing came back at all — is a run-level event.
#
# Measured: both entries together leave the corpus at 15/43, unchanged —
# every run whose earnings hard-failed already fires on another category, so
# this costs nothing today and closes the path for a run where it is the
# only loss.
#
# DELIBERATE TRADEOFF — earnings PARTIAL does not gate EVEN WHEN the FMP
# rescue failed for an infrastructure reason (rate limit / auth). Proposed
# in five separate cold reviews and REJECTED each time: the tolerated
# 30/43 steady-state stubs are THEMSELVES zero-usable-EPS, so the failed-
# rescue state differs only by the rescue's failure reason, and gating on
# that hands FMP's free-tier rate limiter an entry veto over data the
# decision layer already treats as unknown-and-tolerated. financials is
# different (its failed rescue DOES gate) because financials-PASSED asserts
# a usable window; earnings-PARTIAL asserts nothing. Do not re-propose
# without new evidence that the decision layer treats the two states
# differently.
_HARD_FAILURE_STATUSES = frozenset({
    Status.FAILED, Status.INCOMPLETE, Status.CIRCUIT_BREAKER,
})
_GATING_EXTRA_CATEGORIES = {
    "analyst_estimates": _DEGRADED_CATEGORY_STATUSES,
    "earnings": _HARD_FAILURE_STATUSES,
}

# A `not_found` means the issuer genuinely has no such data: a foreign
# private issuer files 20-F, so `filing` is permanently not_found. Re-running
# cannot fix it, so gating on it strands those tickers forever.
#
# This is a BLACKLIST of the known structural cause, deliberately NOT a
# whitelist of known-fixable causes. A whitelist of {unauthorized,
# http_status, rate_limited, timeout} was measured and rejected: it drops
# `price: PARTIAL` with error_code None (AMD/ASTS/GLW/LITE, 2026-05-22) —
# a critical category lost with no code attached. Unknown cause must fail
# toward degraded.
#
# `not_found` is not the only structural cause: a foreign OTC ADR outside the
# provider's universe fails with HTTP 400, which lands on `http_status`, and
# re-running reproduces it exactly like a 20-F issuer's absent `filing`
# (measured: SIVEF loses `filing` + `metrics` that way, a held position).
# `http_status` was considered for this set and DELIBERATELY EXCLUDED: it is
# the catch-all for every unmapped status, so it carries 5xx as well as 400 —
# exempting it would exempt genuine provider outages, which is the failure
# mode this whole field exists to catch. Those tickers stay gated for ENTRY,
# with the categories named in the rationale; exits and trims are never
# blocked. An earlier revision cleared them via a cross-date reproducibility
# carve-out in the decision prompt and that was REMOVED: an outage spanning
# two days reproduces identically, so it cleared the gate on exactly the
# data loss this field exists to catch.
_STRUCTURAL_ABSENCE_CODES = frozenset({"not_found"})

# ...and only for categories where structural absence is a real state.
# `not_found` is mapped from ANY HTTP 404 (`sources/adapter_result.py`,
# status == 404 -> ErrorCode.NOT_FOUND), so the code alone does not mean
# "this issuer has no such data" — a provider serving 404 on /price or
# /metrics would have had that critical loss silently exempted. Scoped to
# the categories that can legitimately be absent for an issuer: no SEC
# filing (20-F filers), no news, no analyst coverage, no segment/insider/
# institutional disclosure. Measured: `not_found` appears in the corpus on
# exactly these six and never on price/metrics/financials/historical, so
# the scoping costs nothing today (15/43 firing, unchanged) and closes the
# path for tomorrow.
_STRUCTURAL_ABSENCE_CATEGORIES = frozenset({
    "filing", "news", "analyst_estimates", "earnings",
    "segmented_revenues", "insider", "institutional",
})

# DELIBERATE TRADEOFF — the exemption is NOT scoped by issuer classification
# (is_adr / filing type). "A domestic issuer's filing not_found should gate"
# was proposed in three separate cold reviews and REJECTED each time: a
# domestic issuer with ZERO filings is a fresh listing's genuine steady state
# (S-1 to first 10-K spans quarters), so an is_adr-scoped exemption
# manufactures a months-long entry veto on new listings; measured, all seven
# stored filing/not_found runs are foreign, and domestic provider gaps
# surface as http codes (which are NOT exempt), not as not_found. Likewise
# "an unattempted fallback (attempted: false — no FMP key) should not confirm
# absence" was proposed twice and REJECTED: no-key is the steady OPERATING
# MODE, not a run event — gating on it would degrade analyst/earnings on
# every thin-coverage name for every keyless user, while the asked-and-
# rate-limited case (a genuine run event) already gates. Do not re-propose
# either without new measured evidence against these counterexamples.

# ...and only when the SECOND source agrees the data is absent.
#
# The FMP fallback does not overwrite the category status when it fails: on a
# failed fill it records `fills.<key> = {"filled": False, "reason": ...}` and
# leaves the primary's `not_found` standing (`sources/fmp.py`, the analyst and
# earnings branches). So an FDS 404 whose FMP rescue was rate-limited or
# rejected looks byte-identical to an issuer with genuinely no analyst
# coverage — and the exemption above would clear a real loss of a category
# that carries a fifth of the forward score.
#
# The news path already gets this right and is the reference: when its Finnhub
# fallback fails, `fetch_news_data` re-emits UNAUTHORIZED / RATE_LIMIT /
# UPSTREAM_ERROR, and returns NOT_FOUND only when both sources agree there is
# nothing (`sources/financial_datasets.py`). Fixing FMP the same way is the
# architecturally cleaner change, but it rewrites `category_statuses` that many
# consumers read; this reads the audit record the fallback already writes and
# is contained to the gate.
#
# Reasons that mean the fallback AGREES the data is absent, so the exemption
# stands: the FMP call itself 404'd, or it returned NO ROWS.
#
# `fmp_analyst_insufficient` is in the set and `fmp_earnings_insufficient` is
# NOT, because the two predicates that raise them are not the same claim. The
# analyst one fires when FMP returned no estimate rows — nobody covers this
# issuer, which is genuine absence. The earnings one fires when FMP returned a
# row whose `actual_eps` and `estimated_eps` are both null — an INCOMPLETE
# answer, not a statement that the company has no earnings history. Treating
# it as agreement would clear the gate on a `not_found` that neither source
# ever confirmed.
_FMP_FILL_KEYS = {"analyst_estimates": "analyst", "earnings": "earnings"}
_FALLBACK_AGREES_ABSENT = frozenset({"not_found", "fmp_analyst_insufficient"})


def _fallback_contradicts_absence(validation, name):
    """True when `name`'s second source was asked, failed, and its failure does
    NOT mean "the data does not exist" — so a `not_found` from the primary is
    unconfirmed and must not be exempted as structural.

    Fails toward the exemption (returns False) for legacy artifacts with no
    `fmp_fallback` record and for runs where the fallback was never attempted:
    there the primary's word is all there ever was, which is how all 42
    pre-change artifacts were built.
    """
    key = _FMP_FILL_KEYS.get(name)
    if key is None:
        return False
    fallback = validation.get("fmp_fallback")
    if fallback is None:
        # Legacy artifact (key absent) or a modern run where the fallback was
        # not configured (fetch writes the key as null when FMP was never
        # wanted): the primary's word is all there ever was — exempt stands.
        return False
    if not isinstance(fallback, dict):
        # Present but NOT a dict (twenty-fourth round): fetch only ever
        # writes null or a dict here, so a string/list/number is a corrupt
        # audit record — the fallback's answer is unknowable, which means
        # the primary's not_found is UNCONFIRMED, and merely corrupting the
        # envelope must not flip a failed category from degraded to clean.
        # 0 of 43 stored runs carry the shape (32 key-absent, 9 null, 2 dict).
        return True
    attempted = fallback.get("attempted")
    if attempted is False or attempted is None:
        return False
    if attempted is not True:
        # Non-bool scalar (thirty-seventh round): corruption, not a
        # never-attempted record — the envelope cannot testify.
        return True
    fills = fallback.get("fills")
    fill = fills.get(key) if isinstance(fills, dict) else None
    if not isinstance(fill, dict):
        # Attempted, but this category has no fill record — the fallback did
        # not get far enough to answer. `fetch.py` writes exactly this on a
        # crash: `{"attempted": True, "available": False, "fills": {}}`. An
        # absent answer is not agreement, so the primary's `not_found` stays
        # unconfirmed. (A malformed record lands here too, and for the same
        # reason: nothing readable said the data is genuinely absent.)
        return True
    filled = fill.get("filled")
    if filled is True:
        return False
    if not isinstance(filled, bool):
        # Strict boolean, not truthiness (thirty-seventh round): the string
        # "false" is TRUTHY, so a corrupt scalar flipped a rate-limited
        # rescue into "successfully filled" and restored the exemption —
        # the scalar-level twin of the round-24 corrupt-envelope rule. A
        # non-bool here means the record cannot testify: unconfirmed.
        return True
    return fill.get("reason") not in _FALLBACK_AGREES_ABSENT


def _summarize_degradation(validation):
    """Return `(validation_status, degraded_categories)` for `meta`.

    `validation_status` is the raw top-level status, or None when it is
    absent or unusable — UNKNOWN, which consumers must not read as clean.

    `degraded_categories` lists only losses that a decision should react to:
    a damaged status, on a critical/important category, from a cause other
    than structural absence. See the three module constants above.
    """
    if not isinstance(validation, dict) or not validation:
        return None, []
    status = validation.get("status")
    status = status.strip() if isinstance(status, str) and status.strip() else None
    if status is not None and status not in _CANONICAL_TOP_STATUSES:
        status = None

    # `categories` must be a mapping before .items(). The sibling
    # `compute_freshness_note` already guards this (cited by symbol: a
    # same-file line number rots on every edit above it); without the
    # same guard here a legacy run whose categories is a list/str raises
    # AttributeError straight out of the classifier, which /monitor and
    # /portfolio call on EVERY ticker.
    #
    # The status comes back None here, NOT preserved (tenth cold round):
    # with no readable categories the losses cannot be enumerated, so the
    # empty list means "no evidence", not "no damage" — and preserving the
    # status made the pair (non-null status, empty list), exactly the shape
    # bq_degradation_state reads as post-change and EXPLICITLY CLEAN. A
    # corrupt `{"status": "FAILED", "categories": []}` classified as fresh
    # and dodged the forced re-score. fetch.py always writes a NON-EMPTY
    # categories dict, so an empty one is the same truncation evidence as a
    # missing one (0 of 43 stored runs carry either shape).
    categories = validation.get("categories")
    if not isinstance(categories, dict) or not categories:
        return None, []
    # A categories map missing GATING keys is truncation evidence too
    # (twenty-third round): fetch writes every category on every run,
    # SKIPPED stubs included, so a subset cannot certify anything it does
    # not carry. The STATUS comes back None — the run's completeness is
    # unknowable — while damage visible among the keys that DID survive is
    # still reported (discarding it would hide real losses behind the
    # corruption that exposed them). The same derived key set drives
    # portfolio_classify._stored_validation_shape_ok; keeping the two
    # answers consistent is the point — they diverged for three review
    # rounds. Auxiliary keys (the version-growth axis) stay exempt.
    _gating_keys = {k for k, v in CATEGORIES.items()
                    if isinstance(v, dict)
                    and v.get("importance") in _GATING_IMPORTANCE}
    _gating_keys |= set(_GATING_EXTRA_CATEGORIES)
    if not _gating_keys <= categories.keys():
        status = None
    degraded = []
    for name, entry in categories.items():
        # An unrecognised category name has unknown importance — fail closed
        # by gating on it rather than silently treating it as auxiliary.
        spec = CATEGORIES.get(name)
        gates_by_tier = (spec is None
                         or spec.get("importance") in _GATING_IMPORTANCE)
        extra_statuses = _GATING_EXTRA_CATEGORIES.get(name)
        if not isinstance(entry, dict):
            # A non-dict ENTRY is a broken record — the entry-level twin of
            # the empty-categories hole above (twelfth cold round). Unknown
            # fails toward degraded, but only where a real loss would gate:
            # an auxiliary entry's genuine FAILED never gates either, so its
            # corruption must not become the one auxiliary state that does.
            if gates_by_tier or extra_statuses is not None:
                degraded.append(name)
            continue
        entry_status = entry.get("status")
        known_clean = entry_status in _CLEAN_CATEGORY_STATUSES
        known_damage = entry_status in _DEGRADED_CATEGORY_STATUSES
        if gates_by_tier:
            # Clean-whitelist, not damage-blacklist: known damage AND unknown
            # vocabulary/missing status both gate. A category listed in both
            # maps must never be NARROWED by the extra map.
            if known_clean:
                continue
        elif extra_statuses is not None:
            # The extra map names the KNOWN-damage statuses that count for
            # this category (earnings: hard failures only — PARTIAL is its
            # 30/43 steady state). Unknown vocabulary still gates even here:
            # unknown is not a steady state, it is a broken record.
            if entry_status not in extra_statuses and (known_clean or known_damage):
                continue
        else:
            continue
        # The exemption additionally requires status == FAILED (thirteenth
        # cold round): "the issuer has no such data" is only a coherent
        # claim when the fetch found NOTHING. The filing producer adopts the
        # highest-severity CHILD error as the aggregate code, so a run whose
        # 10-K fetched fine beside a 404'd 10-Q section emits
        # INCOMPLETE+not_found — content exists and a required piece of it
        # was lost, which contradicts structural absence on its face.
        # Measured: every exempted entry in the 43 stored runs is already
        # FAILED+not_found, so the scoping moves nothing.
        if (entry_status == Status.FAILED
                and entry.get("error_code") in _STRUCTURAL_ABSENCE_CODES
                and name in _STRUCTURAL_ABSENCE_CATEGORIES
                and not _fallback_contradicts_absence(validation, name)):
            continue
        degraded.append(name)
    return status, sorted(degraded)


# DELIBERATE TRADEOFF — financial-freshness (days_old) is DISCLOSURE, not a
# gate. "Stale financials should demote the category / enter
# degraded_categories" was proposed in a cold review and REJECTED: filing
# cadence varies by issuer class (a 20-F annual filer's 200-day-old
# statements are its normal state), so any fixed cutoff is a false veto on
# foreign issuers, and a cadence-aware policy is a feature with its own
# design, not a post-pass. The demonstrated stale cases (MRVL/SNOW/CRWD
# 2026-05-22 era, 205-209 days) were the missing-fiscal-Q4 provider burial,
# fixed at its root (fetch limit 16, DL4 gate, FMP rescue, and the
# rescue-failure demotion). Artifact staleness is owned by the delta clocks
# (14-day BQ / 7-day thesis), and the freshness note + data_freshness ARE
# delivered to the decision layer as disclosure via the portfolio skill's
# BQ-summary whitelist. Do not re-propose a cutoff without a cadence-aware
# design.
def compute_freshness_note(validation):
    """Build a freshness note from validation data, or return None if fresh.

    Defensive against upstream None/non-dict values: `validation.get(x, {})`
    returns the literal None if x IS present and explicitly None, which
    breaks subsequent .get() chains with AttributeError. Use `or {}` to
    coerce None to empty dict at each level.
    """
    if not isinstance(validation, dict):
        return None
    parts = []

    # Check financial data age — chain defensively through possible None/wrong-type
    categories = validation.get("categories") or {}
    if not isinstance(categories, dict):
        return None
    eps_val = categories.get("eps_validation") or {}
    if not isinstance(eps_val, dict):
        return None
    fin_freshness = eps_val.get("financial_freshness") or {}
    if not isinstance(fin_freshness, dict):
        fin_freshness = {}
    days_old = fin_freshness.get("days_old")
    latest_period = fin_freshness.get("latest_report_period")

    if isinstance(days_old, (int, float)) and days_old > 30:
        parts.append(
            f"Financial data is {days_old} days old "
            f"(latest period: {latest_period})"
        )

    # Check for circuit breakers — reuse the already-sanitized `categories`
    circuit_breakers = []
    for cat_name, cat_data in categories.items():
        if isinstance(cat_data, dict) and cat_data.get("status") == Status.CIRCUIT_BREAKER:
            circuit_breakers.append(cat_name)
    if circuit_breakers:
        parts.append(f"CIRCUIT BREAKER in: {', '.join(circuit_breakers)}")

    # Check EPS warnings
    eps_consistency = eps_val.get("eps_consistency", {})
    warnings = eps_consistency.get("warnings", [])
    if warnings:
        parts.append(
            "EPS validation WARNING: " + "; ".join(warnings)
        )

    return ". ".join(parts) if parts else None


def _recompute_overall_from_subscores(dim_doc):
    """(status, value): status ∈ {"ok", "absent", "malformed"}.

    "ok" → value = Σ(score×weight)/W for comparison against `overall`.
    "absent" → no sub_scores (legacy shape) — verification skipped.
    "malformed" → sub_scores present but unusable, INCLUDING a weight sum
    far from 100 (probe-2 review round-3: every scoring prompt weights to
    exactly 100, and normalizing an incomplete SUBSET let a dropped
    30%-weight component vanish while the renormalized overall still
    verified — silent analysis loss). Caller excludes the dimension.
    """
    subs = dim_doc.get("sub_scores")
    if subs is None or "sub_scores" not in dim_doc:
        return "absent", None   # legacy shape — key truly missing
    if not isinstance(subs, dict) or not subs:
        # Probe-2 review round-7: a PRESENT-but-empty (or non-dict)
        # sub_scores block is emitted state that lost its rubric — not a
        # legacy shape. Treating it as absent bypassed verification.
        return "malformed", None
    total_w = 0.0
    acc = 0.0
    for entry in subs.values():
        if not isinstance(entry, dict):
            return "malformed", None
        s, w = entry.get("score"), entry.get("weight")
        for v in (s, w):
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v):
                return "malformed", None
        # Probe-2 review round-5: rubric DOMAIN — every prompt scores
        # sub-components 1-10 with positive weights; an out-of-domain
        # value (0, -3, 40) is corrupt output even when the arithmetic
        # self-verifies.
        if not (1.0 <= s <= 10.0) or not (0.0 < w <= 100.0):
            return "malformed", None
        acc += s * w
        total_w += w
    # Weights must cover the whole rubric (prompts sum to 100; small
    # drift tolerated). A large shortfall means a component is MISSING.
    if not (95.0 <= total_w <= 105.0):
        return "malformed", None
    return "ok", acc / total_w


def _identity_mismatch(dim_name, dim_doc, expected_ticker=None):
    """Self-declared identity check for a scores/<dim>.json payload.

    Returns a reason string when the payload contradicts its slot —
    round-12: declared ``dimension`` != filename dim (misrouted agent
    write); round-16: declared ``ticker`` != this run's ticker (a
    structurally valid MSFT payload in an AAPL run's forward.json was
    weighted into AAPL's canonical BQ, then STRIP_KEYS removed the only
    evidence). Returns None when the identity is consistent or absent
    (legacy payloads without the fields stay accepted). ONE
    implementation shared by build_scores (warns + excludes) and
    build_dimensions (drops — the warning already fired).
    """
    if not isinstance(dim_doc, dict):
        return None
    declared_dim = dim_doc.get("dimension")
    if isinstance(declared_dim, str) and declared_dim and \
            declared_dim != dim_name:
        return f"declares dimension={declared_dim!r}"
    declared_ticker = dim_doc.get("ticker")
    if (expected_ticker and isinstance(declared_ticker, str)
            and declared_ticker
            and declared_ticker.upper() != str(expected_ticker).upper()):
        return (f"declares ticker={declared_ticker!r} but this run is for "
                f"{expected_ticker!r}")
    return None


def build_scores(score_files, weights, expected_ticker=None):
    """Compute weighted BQ score from dimension scores.

    ``expected_ticker`` (round-16): when given, a score file whose
    self-declared ``ticker`` differs is excluded (cross-ticker agent
    write) — same gate as the round-12 dimension-identity check.

    Fail-closed on empty input or fully-mismatched dimension names — main()
    guards with a ≥2 dimensions gate but defensive callers may pass junk.
    Returning ZeroDivisionError here would crash the whole assemble pipeline.

    Note (reviewed + declined, codex cold-rounds 1/10): on a FULL-tier run
    the staging strict-validation (load_bq_analysis) requires all three
    score/weight keys, so a renormalized <3-dim result from this helper
    ends in a STRUCTURED staging abort — that is the documented fail-closed
    contract for full tier (operator re-runs the failed dimension agent),
    not a defect. Widening the loader to ≥2 dims would push partial score
    shapes onto every downstream consumer. The renormalize path here stays
    correct for defensive/lighter callers and turns what used to be a raw
    KeyError crash into an audible exclusion.
    """
    scores = {}
    for dim_name, weight in weights.items():
        if dim_name not in score_files:
            print(
                f"{PREFIX}: WARNING — missing dimension '{dim_name}', "
                f"excluded from weighted average",
                file=sys.stderr,
            )
            continue
        # LLM-authored scores/*.json can drift: a missing 'overall' key or a
        # string value would raise KeyError / TypeError out of the pipeline
        # AFTER Step 5 part 1 already mutated 00_validation.json. Treat a
        # malformed dimension exactly like a missing one (warn + exclude) —
        # NaN/Inf still flow to the finite post-condition below, which
        # fail-closes the whole score (documented behavior, unchanged).
        dim_doc = score_files[dim_name]
        # Probe-2 review round-12: verify the artifact's SELF-DECLARED
        # identity before weighting it. A misrouted agent write (a valid
        # industry payload landing in forward.json) previously got
        # weighted as forward AND kept as industry — duplicated analysis
        # changing the canonical BQ. The identity fields are stripped
        # later (STRIP_KEYS), so this is the only place they can gate.
        mismatch = _identity_mismatch(dim_name, dim_doc, expected_ticker)
        if mismatch:
            print(
                f"{PREFIX}: WARNING — scores/{dim_name}.json {mismatch} "
                f"(misrouted agent write); excluded from weighted average — "
                f"re-dispatch the {dim_name} agent",
                file=sys.stderr,
            )
            continue
        overall_val = dim_doc.get("overall") if isinstance(dim_doc, dict) else None
        if isinstance(overall_val, bool) or not isinstance(overall_val, (int, float)):
            print(
                f"{PREFIX}: WARNING — dimension '{dim_name}' has no numeric "
                f"'overall' (got {overall_val!r}), excluded from weighted average",
                file=sys.stderr,
            )
            continue
        # Probe-2 B1: verify the LLM-authored `overall` against its own
        # sub_scores (every scoring prompt defines overall =
        # Σ(score×weight)/100). A plausible-but-wrong dimension total
        # previously flowed straight into the canonical BQ. Mismatch — or
        # a MALFORMED sub_scores block, including an incomplete weight set
        # (round-3: a dropped 30%-weight component renormalized cleanly) —
        # is treated exactly like a malformed dimension (warn + exclude →
        # structured staging abort on full tier — same fail-closed contract
        # as the declined <3-dim widening below). Only a genuinely ABSENT
        # sub_scores block (legacy shape) skips verification.
        sub_status, recomputed = _recompute_overall_from_subscores(dim_doc)
        if sub_status == "malformed":
            print(
                f"{PREFIX}: WARNING — dimension '{dim_name}' has a "
                f"malformed/incomplete sub_scores block (bad values or "
                f"weights not summing to ~100); excluded from weighted "
                f"average — re-dispatch the {dim_name} agent",
                file=sys.stderr,
            )
            continue
        if sub_status == "ok" and abs(recomputed - overall_val) > 0.06:
            print(
                f"{PREFIX}: WARNING — dimension '{dim_name}' overall "
                f"{overall_val} disagrees with its sub_scores (recomputed "
                f"{recomputed:.2f}); excluded from weighted average — "
                f"re-dispatch the {dim_name} agent",
                file=sys.stderr,
            )
            continue
        scores[dim_name] = overall_val

    total_weight = sum(weights[d] for d in scores)
    if total_weight == 0:
        # No matching dimensions between score_files and weights — fail-closed.
        # Caller should have caught this via the main() ≥2 dim gate but this
        # is the defensive second line.
        return {"overall": None, "weights": {}}

    overall = sum(scores[d] * weights[d] for d in scores) / total_weight

    # Post-condition: a finite weighted average. NaN/Inf can sneak in via a
    # score_file with `overall: NaN` (e.g. an upstream agent failure that
    # serialized a poisoned float). Fail-closed mirrors the empty-input
    # branch above — do NOT raise (this helper runs inside the assemble
    # pipeline and a raise would crash the whole run; the caller already
    # has stricter file-level validation downstream via load_bq_analysis).
    # codex review 2026-05-22: DL3c emit_with_numeric_coerce protects the
    # FD adapter boundary but not the downstream weighted-avg sink here.
    if not math.isfinite(overall):
        print(
            f"{PREFIX}: WARNING — weighted overall is non-finite "
            f"({overall!r}); a score_file likely carries NaN/Inf. "
            f"Failing closed with overall=None.",
            file=sys.stderr,
        )
        return {"overall": None, "weights": {}}

    adjusted_weights = (
        {d: round(weights[d] / total_weight, 4) for d in scores}
        if len(scores) < len(weights)
        else dict(weights)
    )

    return {
        "overall": round(overall, 1),
        **scores,
        "weights": adjusted_weights,
    }


def build_dimensions(score_files, expected_ticker=None):
    """Copy score files into dimensions, stripping redundant keys.

    Round-16: payloads failing the identity gate are dropped here too —
    excluding a misrouted/cross-ticker payload from the weighted score
    while copying its full detail into ``dimensions.*`` would still feed
    the wrong company's analysis to /portfolio's dimension reads.
    """
    dimensions = {}
    for dim_name, data in score_files.items():
        if _identity_mismatch(dim_name, data, expected_ticker):
            continue
        dimensions[dim_name] = {
            k: v for k, v in data.items() if k not in STRIP_KEYS
        }
    return dimensions


def parse_weights(weights_str):
    """Parse weights from comma-separated string like '0.35,0.35,0.30'.

    Each weight must be finite (no NaN/Inf) and non-negative — anything
    else poisons the weighted average in build_scores. codex review
    2026-05-22 catch: DL3c emit_with_numeric_coerce protects the FD
    adapter boundary but not this CLI-arg entry point.
    """
    raw_parts = [x.strip() for x in weights_str.split(",")]
    parts = []
    for raw in raw_parts:
        try:
            val = float(raw)
        except ValueError:
            print(
                f"{PREFIX}: --weights value {raw!r} is not a number",
                file=sys.stderr,
            )
            sys.exit(1)
        if not math.isfinite(val):
            print(
                f"{PREFIX}: --weights value {raw!r} is not finite "
                f"(parsed as {val!r}); NaN/Inf weights poison the "
                f"weighted average",
                file=sys.stderr,
            )
            sys.exit(1)
        if val < 0:
            print(
                f"{PREFIX}: --weights value {raw!r} is negative "
                f"({val!r}); weights must be non-negative",
                file=sys.stderr,
            )
            sys.exit(1)
        parts.append(val)
    if len(parts) != 3:
        print(
            f"{PREFIX}: --weights requires 3 comma-separated values, got {len(parts)}",
            file=sys.stderr,
        )
        sys.exit(1)
    return dict(zip(DIMENSIONS, parts))


def _load_strategy_weights(strategy_path=None):
    """Resolve dimension_weights from strategy.yaml.scoring.dimension_weights.

    Precedence (resolved by caller):
        --weights CLI > strategy.yaml > DEFAULT_WEIGHTS

    This helper returns the strategy.yaml value if:
      - file exists and parses as a mapping
      - scoring.dimension_weights is a dict keyed EXACTLY by the three
        dimensions (no missing / extra keys)
      - values are numeric, non-negative, and sum to ~1.0

    ABSENT override intent (no file / no scoring section / no
    dimension_weights key) → DEFAULT_WEIGHTS. PRESENT-but-invalid
    override (parse error, wrong keys, bad values, bad sum) →
    SystemExit (probe-2 round-15 F1: a typo'd override silently scored
    with defaults — e.g. intended 0.8/0.1/0.1 on scores 10/1/1 persists
    4.2 instead of 8.2, and only ephemeral stderr knew).
    """
    try:
        import yaml
    except ImportError:
        return dict(DEFAULT_WEIGHTS)
    # Try cwd first (tests inject strategy.yaml via tmp_path + cwd override);
    # fall back to __file__-anchored. Pure cwd broke when running from
    # /tmp; pure __file__ broke test-injection. Both matter.  # noqa: audit-fail-open
    if strategy_path:
        p = Path(strategy_path)
    else:
        cwd_candidate = Path("strategy.yaml")  # fail-open-ok: cwd-first for test-injection + CLI ergonomics
        root_candidate = Path(__file__).resolve().parent.parent / "strategy.yaml"
        p = cwd_candidate if cwd_candidate.exists() else root_candidate
    if not p.exists():
        # No user intent to override — silent default.
        return dict(DEFAULT_WEIGHTS)
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        # Probe-2 review round-15 F1: an unparseable strategy.yaml may hide
        # an explicit weights override — silently scoring with defaults
        # persists a plausible-but-wrong overall. Fail-closed.
        raise SystemExit(
            f"{PREFIX}: strategy.yaml could not be parsed "
            f"({type(e).__name__}: {e}) — cannot determine whether a "
            f"dimension_weights override exists. Fix the YAML (or remove "
            f"the file to use DEFAULT_WEIGHTS)."
        )
    scoring = data.get("scoring")
    if scoring is None:
        # No scoring section — no override intent, silent default.
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(scoring, dict):
        raise SystemExit(
            f"{PREFIX}: strategy.yaml.scoring is not a mapping "
            f"(got {type(scoring).__name__}) — fix it or remove the "
            f"scoring section to use DEFAULT_WEIGHTS."
        )
    if "dimension_weights" not in scoring:
        # scoring section exists but no dimension_weights key — questionable
        # but could just mean user set other scoring knobs. Log to be audible.
        print(
            f"{PREFIX}: strategy.yaml.scoring exists but lacks 'dimension_weights'; "
            f"falling back to DEFAULT_WEIGHTS",
            file=sys.stderr,
        )
        return dict(DEFAULT_WEIGHTS)
    w = scoring.get("dimension_weights")
    if not isinstance(w, dict):
        raise SystemExit(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights is not a "
            f"mapping (got {type(w).__name__}) — fix it or remove the key "
            f"to use DEFAULT_WEIGHTS."
        )
    if set(w.keys()) != set(DEFAULT_WEIGHTS):
        raise SystemExit(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights invalid "
            f"(keys={sorted(w.keys())} must match {sorted(DEFAULT_WEIGHTS)}) "
            f"— fix the key names or remove the key to use DEFAULT_WEIGHTS."
        )
    # bool is an int subclass: YAML 1.1 `yes`/`no`/`on`/`off` parse as
    # booleans and would pass a bare (int, float) check (0 <= True <= 1),
    # silently producing a 1/0/0 weighting and boolean values in the
    # bq_analysis.json weights field. Reject bool explicitly, matching the
    # repo-wide numeric-boundary convention.
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
        for v in w.values()
    ):
        raise SystemExit(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights invalid "
            f"(values={dict(w)} must be numeric in [0,1]) — fix the values "
            f"or remove the key to use DEFAULT_WEIGHTS."
        )
    if abs(sum(w.values()) - 1.0) >= 0.01:
        raise SystemExit(
            f"{PREFIX}: strategy.yaml.scoring.dimension_weights invalid "
            f"(sum={sum(w.values()):.4f} must be ~1.0) — fix the values "
            f"or remove the key to use DEFAULT_WEIGHTS."
        )
    return {k: float(w[k]) for k in DEFAULT_WEIGHTS}


def _cert_to_dict(cert):
    """Serialize a CurrencyConversion dataclass back to the §3.1.2 JSON shape.

    Mirrors the producer-side cert builder so the propagated block written
    into bq_analysis.json round-trips through load_currency_conversion.
    """
    window_rows = []
    if cert.window is not None:
        for row in cert.window.rows:
            row_out = {
                "currency": row.currency,
                "date": row.date,
                "fx_rate_usd_per_local": row.fx_rate_usd_per_local,
                "source": row.source,
            }
            if row.bar_date is not None:
                row_out["bar_date"] = row.bar_date
            if row.lag_days is not None:
                row_out["lag_days"] = row.lag_days
            window_rows.append(row_out)
    return {
        "basis": cert.basis,
        "source_currency": cert.source_currency,
        "fx_source": cert.fx_source,
        "window": window_rows,
    }


def _load_dl3c_gated_artifacts(report_dir):
    """DL3c §3.7.4: load gated artifacts via typed loaders.

    Returns (loaded_modes, propagated_cert, converted_cert_dicts):
      - loaded_modes: dict[str, str] mapping artifact name (matching
        DL3C_GATED_ARTIFACTS) → dl3c_mode literal. Only artifacts that
        loaded successfully are present; missing files and SchemaError
        loads are excluded (a warning is emitted on stderr for the latter).
      - propagated_cert: dict serialized cert from the FIRST artifact whose
        mode is "post_dl3c_usd_converted", or None if no converted artifact
        was loaded. The mixed-mode raise + cert-divergence raise downstream
        ensure that, when multiple converted artifacts exist, they all
        emit the same cert (so the "first one wins" semantic is safe).
      - converted_cert_dicts: dict[name → serialized cert] for every
        converted gated artifact. Used by _check_mixed_dl3c_modes (post-
        impl loop-1 H4) to detect cert divergence across artifacts.
    """
    # Inline imports keep the typed-loader dependency local to the DL3c
    # propagation step — mirrors the inline `from scripts.schemas.bq_analysis
    # import load_bq_analysis` convention at the bottom of main().
    from scripts.schemas.adr_correction import load_adr_correction
    from scripts.schemas.errors import SchemaError
    from scripts.schemas.fcf_inputs import load_fcf_inputs
    from scripts.schemas.historical_multiples import load_historical_multiples

    artifact_paths = {
        "fcf_inputs": report_dir / "data" / "fcf_inputs.json",
        "historical_multiples": report_dir / "data" / "historical_multiples.json",
        "adr_correction": report_dir / "data" / "adr_correction.json",
    }
    loaders = {
        "fcf_inputs": load_fcf_inputs,
        "historical_multiples": load_historical_multiples,
        "adr_correction": load_adr_correction,
    }

    loaded_modes = {}
    # post-impl loop-1 H4 fix: collect ALL converted certs (not just the
    # first). _check_mixed_dl3c_modes now also verifies that every
    # converted artifact agrees on (source_currency, fx_source, window).
    # Pre-fix `propagated_cert = first_converted_doc_cert` silently dropped
    # later artifacts' certs even when they disagreed — a partial-FX
    # state with diverging FX windows would produce a bq_analysis.json
    # carrying one artifact's view as if it were canonical.
    converted_cert_dicts: dict[str, dict] = {}
    for name in DL3C_GATED_ARTIFACTS:
        path = artifact_paths[name]
        if not path.exists():
            # Missing-file tolerance preserved — some tickers legitimately
            # skip a gated artifact (USD-only tiers don't produce
            # adr_correction.json, smoke tiers may skip historical_multiples).
            continue
        try:
            doc = loaders[name](path)
        except (SchemaError, ValueError) as exc:
            # post-impl loop-2/3 ISS-023 fix: an EXISTING gated artifact
            # that fails typed-load is a partial-write or schema-drift
            # signal — fail-close. Pre-fix this was `log + continue`
            # with the rationale "upstream producer should have
            # fail-closed already". But the FX-failure-path producer
            # envelope emits a valid-shape artifact with no cert + an
            # explicit fx_failure_reason (loop-2 ISS-021 added
            # post_dl3c_failed_fx mode for that case). If a loader
            # raises here, it means we hit a state OUTSIDE that
            # well-formed-failure envelope — a malformed cert, a
            # bad _dl3c_version, a basis=usd_native with cert
            # present, etc. Those are partial-migration signals that
            # MUST surface to the operator rather than getting
            # silently dropped from mixed-mode/cert-divergence checks.
            # Re-flagged by codex fresh-session in 3 consecutive loops
            # (loop-1 cycle-1, loop-2 cycle-1, loop-3 final challenge).
            print(
                f"{PREFIX}: FATAL — failed to load {name}.json for DL3c "
                f"dispatch: {exc}; the artifact exists on disk but its "
                f"DL3c-relevant subset is malformed. Investigate the "
                f"producer pipeline (likely a partial write, schema "
                f"drift, or hand-edit). To bypass for a one-off run, "
                f"delete the artifact (file-missing is the tolerated "
                f"state, not file-malformed).",
                file=sys.stderr,
            )
            sys.exit(1)
        # Frozen-anchor exclusion: fetch.py:write_adr_anchor writes a
        # classification anchor (no cert, not a DL3c artifact) to the
        # adr_correction.json path. It is NOT part of the FX-conversion mode
        # set — counting it as `legacy_pre_dl3c` would make it a phantom
        # non-converted artifact that spuriously trips _check_mixed_dl3c_modes
        # when fcf_inputs/historical_multiples ARE converted (non-USD ADR
        # re-assembled after a thesis run). Treat it as an absent gated
        # artifact (same as the missing-file path above).
        if getattr(doc, "is_frozen_anchor", False):
            continue
        loaded_modes[name] = doc.dl3c_mode
        if doc.dl3c_mode == "post_dl3c_usd_converted":
            converted_cert_dicts[name] = _cert_to_dict(doc.currency_conversion)
    propagated_cert = (
        next(iter(converted_cert_dicts.values()))
        if converted_cert_dicts else None
    )
    return loaded_modes, propagated_cert, converted_cert_dicts


def _check_mixed_dl3c_modes(loaded_modes, converted_cert_dicts=None):
    """DL3c §3.7.4 / cycle-15 F-15-7: raise if gated artifacts disagree.

    Two independent fail-close cases:

    1. **Cross-mode contamination** — at least one converted artifact
       coexists with another artifact in a NON-converted mode
       (post_dl3c_usd_native OR legacy_pre_dl3c). Indicates partial FX
       success or stale legacy artifact mixed with a new converted artifact;
       operator must investigate.

       post-impl loop-1 H3 fix: pre-fix the check only compared
       `post_dl3c_usd_converted` vs `post_dl3c_usd_native`, missing the
       legacy_pre_dl3c case where a converted artifact silently coexists
       with an artifact predating DL3c. Now any non-converted mode
       (native OR legacy) triggers the fail-close when ANY converted
       artifact exists.

    2. **Diverging converted certs** — multiple converted artifacts present
       but their certificates differ on (basis, source_currency, fx_source,
       window). Indicates two FX windows fetched at different times or
       against different currencies; bq_analysis.json must not silently
       propagate one as canonical (post-impl loop-1 H4 fix).
    """
    converted = {n for n, m in loaded_modes.items() if m == "post_dl3c_usd_converted"}
    # post-impl loop-1 cycle-2 ISS-021: `post_dl3c_failed_fx` also counts
    # as non-converted; mixing it with `post_dl3c_usd_converted` is a
    # genuine partial-FX state (one ticker artifact converted while
    # another's FX fetch failed). Pre-fix `failed_fx` was relabeled
    # `usd_native` and slipped through unchecked.
    non_converted = {
        n for n, m in loaded_modes.items()
        if m in (
            "post_dl3c_usd_native", "legacy_pre_dl3c", "post_dl3c_failed_fx",
        )
    }
    failed_fx = {n for n, m in loaded_modes.items() if m == "post_dl3c_failed_fx"}

    # post-impl loop-1 cycle-3 HIGH-1: a fully-failed-FX file set (every
    # gated artifact is post_dl3c_failed_fx) has no converted artifact
    # to trigger the `converted AND non_converted` branch below, so it
    # would slip through the gate and produce a cert-free bq_analysis.json
    # indistinguishable from a true USD-native run. failed_fx ANYWHERE is
    # a non-USD ticker whose FX fetch broke — fail-close so the operator
    # surfaces the issue rather than silently downstream-consuming
    # pre-conversion local-currency data.
    if failed_fx:
        print(
            f"{PREFIX}: FATAL — FX conversion failed for gated artifacts: "
            f"{sorted(failed_fx)}; the underlying data is still in local "
            f"currency. operator must retry FX fetch or resolve the source "
            f"data issue (e.g., add the currency to SUPPORTED_FX_CURRENCIES, "
            f"check yfinance availability)",
            file=sys.stderr,
        )
        sys.exit(1)

    if converted and non_converted:
        print(
            f"{PREFIX}: FATAL — mixed DL3c modes across gated artifacts: "
            f"converted={sorted(converted)}, "
            f"non_converted={sorted(non_converted)}; "
            f"operator must investigate inconsistent FX state",
            file=sys.stderr,
        )
        sys.exit(1)

    # H4: cert-divergence check across multiple converted artifacts.
    if converted_cert_dicts and len(converted_cert_dicts) >= 2:
        baseline_name = next(iter(converted_cert_dicts))
        baseline = converted_cert_dicts[baseline_name]
        for name, cert in converted_cert_dicts.items():
            if cert != baseline:
                print(
                    f"{PREFIX}: FATAL — converted DL3c certs disagree across "
                    f"gated artifacts: {baseline_name}.cert != {name}.cert. "
                    f"Likely a partial FX-state migration (one artifact "
                    f"reran against a different FX window). operator must "
                    f"reconcile or rerun all gated producers in one pass.",
                    file=sys.stderr,
                )
                sys.exit(1)



def _discard_staging(staging_path) -> None:
    """Best-effort removal of the staging file — NEVER raises.

    Cowork's virtiofs mount can refuse deletes (EPERM). A bare
    `staging_path.unlink(missing_ok=True)` inside an `except SchemaError`
    block then raised PermissionError, which REPLACED the SchemaError: the
    user saw an unlink traceback and never the `contract validation failed`
    line naming the real cause (2026-08-28 Cowork run, 2 tickers). Cleanup
    must never outrank the error it is cleaning up after. Same shape as
    `scripts/etf/stamp.py`'s staged-artifact cleanup.
    """
    try:
        staging_path.unlink(missing_ok=True)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Assemble bq_analysis.json from score + synthesis files"
    )
    parser.add_argument(
        "--report-dir",
        required=True,
        help="Report directory (e.g. reports/AAPL/20260406)",
    )
    parser.add_argument(
        "--weights",
        default=None,
        help="Dimension weights as comma-separated values (default: 0.35,0.35,0.30)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Analysis date override (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--tier-context-json",
        required=True,
        help="Path to transient JSON with tier_this_run + component_provenance.",
    )
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    # Weight precedence: CLI --weights > strategy.yaml > DEFAULT_WEIGHTS
    if args.weights:
        weights = parse_weights(args.weights)
    else:
        weights = _load_strategy_weights()

    # Read inputs
    score_files = {}
    for dim_name in DIMENSIONS:
        score_path = report_dir / "scores" / f"{dim_name}.json"
        if score_path.exists():
            _doc = read_json(
                str(score_path), f"scores/{dim_name}.json", PREFIX
            )
            # Root-shape gate BEFORE both consumers: a list/scalar root
            # would pass build_scores' per-field guard but crash
            # build_dimensions at data.items() (codex cold-round 5).
            # Treated exactly like a missing score file (the <2-dims gate
            # below then decides whether the run can proceed).
            if isinstance(_doc, dict):
                score_files[dim_name] = _doc
            else:
                print(
                    f"{PREFIX}: WARNING — scores/{dim_name}.json root is "
                    f"{type(_doc).__name__}, not an object; dimension "
                    f"excluded (treated like a missing score file)",
                    file=sys.stderr,
                )
        else:
            print(
                f"{PREFIX}: WARNING — {score_path} not found, skipping",
                file=sys.stderr,
            )

    if not score_files:
        print(f"{PREFIX}: no score files found, cannot assemble", file=sys.stderr)
        sys.exit(1)

    # HIGH-19: fail closed on <2 dimensions. A valid BQ verdict requires
    # at least 2 of the 3 dimensions per prompts/score-synthesize.md.
    # The prior behavior (single-dim → warn + emit full verdict) produced
    # overall scores that looked authoritative but came from one pillar.
    if len(score_files) < 2:
        available = ", ".join(sorted(score_files)) or "(none)"
        print(
            f"{PREFIX}: only {len(score_files)}/3 dimensions available "
            f"({available}) — insufficient for a BQ verdict (per "
            f"prompts/score-synthesize.md a valid BQ requires \u22652 "
            f"dimensions). Provide more score files or re-run the skill.",
            file=sys.stderr,
        )
        sys.exit(1)

    synthesis_path = report_dir / "synthesis.json"
    synthesis = read_json(str(synthesis_path), "synthesis.json", PREFIX)

    validation_path = report_dir / "data" / "00_validation.json"
    validation = read_json(str(validation_path), "00_validation.json", PREFIX)

    # Use project's standard read_json (fail-closed with stderr + exit 1
    # on missing/malformed). Pre-existence check is redundant — read_json
    # reports missing-file clearly. Note: exit code is 1 not 2; that's the
    # established convention across all scripts (see cli_utils.py).
    tier_context = read_json(
        args.tier_context_json, "--tier-context-json", PREFIX
    )

    # Validate cross-check between 00_validation.tier_decided and tier_context.
    # Spec §6.2: missing or null in EITHER source aborts; "probe" is non-terminal.
    vtier = validation.get("tier_decided")
    ctier = tier_context.get("tier_this_run")
    if vtier is None or ctier is None:
        print(
            f"{PREFIX}: FATAL — tier is missing/null: "
            f"00_validation.tier_decided={vtier!r}, --tier-context-json={ctier!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if vtier == "probe" or ctier == "probe":
        print(
            f"{PREFIX}: FATAL — assembler received non-terminal tier 'probe'; "
            f"orchestrator must upgrade scope before calling assembler",
            file=sys.stderr,
        )
        sys.exit(1)
    # Probe-2 B3: enum membership, not just equality. An unknown-but-equal
    # tier value (e.g. a typo'd "fulll" threaded through .run_state.json)
    # previously passed every check, mapped to ZERO fresh dims in the
    # WebSearch-binding gate, and skipped the binding marker — fresh
    # analysis silently mislabeled + validated legacy-lenient.
    _TERMINAL_TIERS = ("full", "partial", "no_op")
    if ctier not in _TERMINAL_TIERS:
        print(
            f"{PREFIX}: FATAL — unknown tier {ctier!r}; must be one of "
            f"{_TERMINAL_TIERS}",
            file=sys.stderr,
        )
        sys.exit(1)
    if vtier != ctier:
        print(
            f"{PREFIX}: FATAL — tier mismatch: "
            f"00_validation.tier_decided={vtier!r} vs --tier-context-json={ctier!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # WebSearch source-binding gate (Plan B Task 6). The dims FRESH this
    # run were produced by agents under the post-binding prompt contract:
    # every WebSearch citation must bind outlet + url + access-date
    # ([WebSearch: <outlet>, <url>, accessed <YYYY-MM-DD>]). Reused
    # (prior-run, possibly legacy) dims are NOT gated — incremental
    # partial/no_op runs over pre-binding priors keep working. The
    # fresh-dim set per tier mirrors the orchestrator's AGENTS_RUN map
    # (full: 3 dims; partial: forward+industry; no_op: none).
    from scripts.schemas import SchemaError as _SchemaError
    from scripts.schemas.source_tag import validate_source_tags
    for dim_name in WEBSEARCH_FRESH_DIMS_BY_TIER.get(ctier, ()):
        if dim_name not in score_files:
            continue
        try:
            validate_source_tags(
                score_files[dim_name],
                artifact=f"scores/{dim_name}",
                strict_websearch=True,
            )
        except _SchemaError as exc:
            print(
                f"{PREFIX}: FATAL — WebSearch source-binding violation in "
                f"scores/{dim_name}.json (fresh this run, tier={ctier}): "
                f"{exc}. Every [WebSearch:] citation in a fresh dimension "
                f"must be [WebSearch: <outlet>, <url>, accessed "
                f"<YYYY-MM-DD>] backed by a real search.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Determine ticker (from validation or first score file)
    ticker = validation.get("ticker")
    if not ticker:
        first_score = next(iter(score_files.values()))
        ticker = first_score.get("ticker", "UNKNOWN")

    # Extract fields from synthesis that are handled by the script
    freshness_interpretation = synthesis.pop("freshness_interpretation", None)
    synthesis.pop("business_quality", None)  # canonical value is scores.overall

    # DL3c §3.7.4: load gated artifacts, capture propagation cert, enforce
    # mixed-mode consistency BEFORE writing output. Missing artifacts are
    # tolerated (USD-only tickers typically lack adr_correction.json; some
    # tiers skip historical_multiples).
    loaded_modes, propagated_cert, converted_cert_dicts = (
        _load_dl3c_gated_artifacts(report_dir)
    )
    _check_mixed_dl3c_modes(loaded_modes, converted_cert_dicts)

    # Assemble
    result = {
        "meta": build_meta(ticker, validation, freshness_interpretation, args.date, tier_context),
        "scores": build_scores(score_files, weights, expected_ticker=ticker),
        "synthesis": synthesis,
        "dimensions": build_dimensions(score_files, expected_ticker=ticker),
    }

    # DL3c §3.7.4: propagate cert if any gated artifact was usd_converted.
    # Mixed-mode raise above guarantees the captured cert is representative
    # of all converted artifacts in this run.
    if propagated_cert is not None:
        result["currency_conversion"] = propagated_cert

    # Propagate the deterministic mixed-currency marker. fetch.py persists
    # `currency_consistency` onto 02_financial_data.json ONLY on the mixed
    # path (USD-native financials carry no block), so its mere presence is the
    # signal: field-level USD/native mixing that the self-repair did or did not
    # resolve. Recording it here makes the corruption a machine-readable fact in
    # the canonical artifact instead of leaving it to whatever the synthesis LLM
    # happened to write in prose (the prompts/score-*.md currency guards are the
    # SOFT layer; this is the deterministic HARD record consumed by /portfolio
    # and audits). Distinct from the DL3c `currency_conversion` cert above, which
    # records a CLEAN non-USD → USD conversion. This read is additive metadata,
    # so it tolerates a missing/malformed financials file rather than aborting a
    # core assemble that only needs scores + synthesis + validation.
    fin_path = report_dir / "data" / "02_financial_data.json"
    currency_consistency = None
    if fin_path.exists():
        try:
            with fin_path.open("r", encoding="utf-8") as f:
                fin_data = json.load(f)
            if isinstance(fin_data, dict):
                currency_consistency = fin_data.get("currency_consistency")
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"{PREFIX}: WARNING — could not read currency_consistency from "
                f"02_financial_data.json: {e}",
                file=sys.stderr,
            )
    if isinstance(currency_consistency, dict) and currency_consistency.get("status"):
        result["currency_consistency"] = currency_consistency
        note = f"financials currency_consistency: {currency_consistency['status']}"
        existing = result["meta"].get("freshness_note")
        result["meta"]["freshness_note"] = (
            f"{existing}. {note}" if existing else note
        )

    # WebSearch binding marker — CHAIN-MARKED semantics (schema-evolution
    # probe 2026-08-03 F1). FULL tier: all three dims + synthesis are
    # fresh under the post-binding contract → stamp. PARTIAL/NO_OP: the
    # copied dims may predate the binding contract, so stamping is safe
    # only when the PRIOR artifact was itself marked (its content already
    # satisfied strict validation; the fresh regenerated parts must too).
    # Pre-fix, partial/no_op output was NEVER marked — a no-op rebuild of
    # a fully-marked prior (the shipped AMD 20260730→20260731 chain)
    # dropped the earned marker, and freshly regenerated synthesis prose
    # escaped strict WebSearch-binding validation. Unmarked-prior chains
    # stay legacy-lenient (no spurious failures on genuine legacy dims).
    _prior_marked = tier_context.get("prior_websearch_binding_marked") is True
    if ctier == "full" or _prior_marked:
        from scripts.schemas.source_tag import stamp_websearch_binding
        result = stamp_websearch_binding(result)

    # DL3c marker — EVIDENCE-BASED (closing round-14 F1). The old
    # unconditional stamp declared usd_native (marker + no cert) for a
    # BQ run whose gated valuation artifacts DON'T EXIST YET (they are
    # thesis-Step-5 products; fetch's adr_correction is a pre-DL3c stub)
    # — a JPY-statement ADR's first BQ artifact carried a false
    # USD-native currency label. Stamp only when at least one gated
    # artifact actually ran under the DL3c contract; zero post-DL3c
    # evidence → unmarked → dispatch reads legacy_pre_dl3c ("currency
    # lineage undetermined"), which every consumer treats leniently.
    if any(m in ("post_dl3c_usd_native", "post_dl3c_usd_converted",
                 "post_dl3c_failed_fx") for m in loaded_modes.values()):
        result = emit_dl3c_root_marker(result)

    # Write to a staging path, validate it, then atomically promote to the
    # canonical path — so the contract-validation failure mode honors the
    # same invariant the pre-write failures already enforce: a failed
    # assemble must NOT leave a bq_analysis.json at the canonical path (cf.
    # test_single_dimension_fails_closed / DL3c failed_fx), and a failed
    # re-run must not clobber a prior-good artifact. Validating the
    # serialized staging file (not the in-memory dict) still catches JSON
    # round-trip bugs (encoding, float precision); the promote is a rename,
    # so the validated bytes are exactly what lands at the canonical path.
    output_path = report_dir / "bq_analysis.json"
    staging_path = report_dir / ".bq_analysis.staging.json"
    write_output(result, str(staging_path))

    # Produce-time contract validation (fail-closed). Inline import matches
    # existing macro/strategy loader convention.
    from scripts.schemas.bq_analysis import load_bq_analysis
    from scripts.schemas import SchemaError
    try:
        load_bq_analysis(str(staging_path))
    except SchemaError as exc:
        _discard_staging(staging_path)
        print(f"{PREFIX}: fatal: contract validation failed on "
              f"{output_path}: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Unexpected validator failure (not a contract SchemaError): keep
        # the error loud by re-raising, but don't leak the staging file.
        # Mirrors write_output's own temp-cleanup-then-raise pattern.
        _discard_staging(staging_path)
        raise

    staging_path.replace(output_path)
    print(f"{PREFIX}: wrote {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
