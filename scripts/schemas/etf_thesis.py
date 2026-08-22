"""Typed contract for `A.ETF_THESIS` — a tagged union with two variants.

Path: `reports/{T}/{DATE}/etf_thesis.json`
Producer: `scripts.etf.stamp` (PR.STAMP)

    refusal   the fund was ineligible, or eligible but unanalysable. Carries
              WHY and nothing a model wrote.
    thesis    a model wrote it, and every number in it is bound.

The discriminator is the presence of `refusal_kind`, and it is used for
LOADING only — no consumer branches on it to decide whether entry is
permitted; that is `P.ENTRY_PERMITTED`'s job over the whole bundle.

**Evidence binding is the point of this module.** A model can write a
plausible number as easily as a true one, so every observed claim must name
the artifact field it came from, and the loader re-reads that field from the
hash-bound bytes and compares. A `[Calc:]` claim names its operands the same
way and the loader recomputes the formula. Forward-looking conditions are
different in kind — they describe an observation that has not happened yet —
so they bind by naming the field to WATCH plus an operator and a threshold,
and the threshold must equal the number the prose states.

What this cannot do: prove a provider fact is true. It proves the thesis is
about the data that was actually collected.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from scripts.schemas.errors import SchemaError

_ARTIFACT = "etf_thesis.json"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")

REFUSAL_KINDS = frozenset({"ineligible", "analysis_unavailable"})
MERITS = frozenset({"strong_add", "add", "watch", "pass", "avoid"})
ETF_KINDS = frozenset({"broad", "sector", "thematic", "unknown"})
ENTRY_ELIGIBILITIES = frozenset({"pass", "block", "unknown"})
ANALYSIS_READINESS = frozenset({"ready", "unavailable"})
EVIDENCE_SOURCE_KINDS = frozenset({"API", "Calc"})
EVIDENCE_ARTIFACTS = frozenset({"etf_profile", "etf_market_snapshot"})
CONDITION_OPERATORS = frozenset({"lt", "le", "eq", "ge", "gt"})
TIMING_ASSESSMENTS = frozenset({"favorable", "neutral", "unfavorable", "unknown"})

# V.REFUSAL_REASON — one vocabulary covering eligibility and readiness.
REFUSAL_REASONS = frozenset({
    "leveraged_or_inverse", "not_owner_approved", "owner_approval_expired",
    "owner_approval_invalid", "composition_unavailable",
    "composition_out_of_scope", "fund_of_funds_structure",
    "identity_unresolved", "leverage_unknown", "liquidity_unavailable",
    "analysis_unavailable", "not_evaluated_instrument_refused",
    "price_unavailable", "structure_unavailable", "too_young_for_indicators",
    "indicators_unavailable", "benchmark_unavailable", "rates_unavailable",
    "rates_stale", "treasury_unavailable",
})

# Fields only the model may write. Listed so the refusal variant can forbid
# every one of them in a single check: a refusal carrying model prose would
# let a reader take a recommendation from an artifact that refused to make one.
MODEL_AUTHORED_FIELDS = ("merit_recommendation", "merit_evidence", "kind",
                         "technical_timing", "environment", "entry_conditions",
                         "narrative")

_ABSENT = object()


def _err(field: str, message: str) -> SchemaError:
    return SchemaError(_ARTIFACT, field, message)


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _member(raw: Mapping, key: str, allowed: frozenset[str]) -> str:
    value = raw.get(key)
    if value not in allowed:
        raise _err(key, f"{value!r} is outside {sorted(allowed)}")
    return value


def _nonempty_str(raw: Mapping, key: str, *, field: Optional[str] = None) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _err(field or key, f"expected a non-empty string, got {value!r}")
    return value


def _sha256(raw: Mapping, key: str, *, required: bool) -> Optional[str]:
    value = raw.get(key)
    if value is None:
        if required:
            raise _err(key, "required")
        return None
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise _err(key, f"expected a lowercase sha256 hex digest, got {value!r}")
    return value


def _reason_list(raw: Mapping, key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise _err(key, f"expected a list, got {type(value).__name__}")
    for r in value:
        if r not in REFUSAL_REASONS:
            raise _err(key, f"{r!r} is outside the refusal vocabulary")
    return tuple(value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# model-authored sub-schemas
# ---------------------------------------------------------------------------

def _validate_evidence_ref(ref, field: str) -> dict:
    if not isinstance(ref, dict):
        raise _err(field, f"expected an object, got {type(ref).__name__}")
    kind = ref.get("source_kind")
    if kind not in EVIDENCE_SOURCE_KINDS:
        raise _err(f"{field}.source_kind",
                   f"{kind!r} is outside {sorted(EVIDENCE_SOURCE_KINDS)}")
    artifact = ref.get("artifact")
    if artifact not in EVIDENCE_ARTIFACTS:
        raise _err(f"{field}.artifact",
                   f"{artifact!r} is outside {sorted(EVIDENCE_ARTIFACTS)}")
    _nonempty_str(ref, "field_path", field=f"{field}.field_path")
    if "value" not in ref:
        raise _err(f"{field}.value",
                   "required — an observation with no value is not an "
                   "observation")
    as_of = ref.get("as_of")
    if as_of is not None and (not isinstance(as_of, str)
                              or not _ISO_DATE_RE.match(as_of)):
        raise _err(f"{field}.as_of",
                   f"expected an ISO date or null, got {as_of!r}")
    formula = ref.get("formula")
    if kind == "Calc":
        if not isinstance(formula, str) or not formula.strip():
            raise _err(f"{field}.formula",
                       "a Calc reference must show the formula it computed")
    elif formula is not None and not isinstance(formula, str):
        raise _err(f"{field}.formula", f"expected a string or null, got {formula!r}")
    return ref


def _validate_evidence_list(value, field: str) -> tuple:
    if not isinstance(value, list) or not value:
        raise _err(field, f"expected a non-empty list, got {value!r}")
    return tuple(_validate_evidence_ref(r, f"{field}[{i}]")
                 for i, r in enumerate(value))


def _validate_forward_condition(cond, field: str) -> dict:
    if not isinstance(cond, dict):
        raise _err(field, f"expected an object, got {type(cond).__name__}")
    _nonempty_str(cond, "id", field=f"{field}.id")
    _nonempty_str(cond, "statement", field=f"{field}.statement")
    artifact = cond.get("artifact")
    if artifact not in EVIDENCE_ARTIFACTS:
        raise _err(f"{field}.artifact",
                   f"{artifact!r} is outside {sorted(EVIDENCE_ARTIFACTS)}")
    _nonempty_str(cond, "watch_field_path", field=f"{field}.watch_field_path")
    operator = cond.get("operator")
    if operator not in CONDITION_OPERATORS:
        raise _err(f"{field}.operator",
                   f"{operator!r} is outside {sorted(CONDITION_OPERATORS)}")
    if "threshold" not in cond:
        raise _err(f"{field}.threshold",
                   "required — a condition with no trigger level cannot fire")
    return cond


def _validate_condition_list(value, field: str) -> tuple:
    if not isinstance(value, list) or not value:
        raise _err(field, f"expected a non-empty list, got {value!r}")
    ids = set()
    out = []
    for i, c in enumerate(value):
        cond = _validate_forward_condition(c, f"{field}[{i}]")
        if cond["id"] in ids:
            raise _err(f"{field}[{i}].id", f"duplicate id {cond['id']!r}")
        ids.add(cond["id"])
        out.append(cond)
    return tuple(out)


# ---------------------------------------------------------------------------
# the artifact
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EtfThesis:
    variant: str                        # refusal | thesis
    ticker: str
    as_of: str
    entry_eligibility: str
    entry_reasons: tuple[str, ...]
    analysis_readiness: str
    analysis_reasons: tuple[str, ...]
    profile_sha256: str
    market_snapshot_sha256: Optional[str]
    refusal_kind: Optional[str]
    merit_recommendation: Optional[str]
    kind: Optional[str]
    invalidation_conditions: tuple
    entry_conditions: tuple
    held_exit_context: Optional[dict]
    raw: Mapping[str, Any]


def validate_etf_thesis(raw: Any) -> EtfThesis:
    if not isinstance(raw, dict):
        raise _err("<root>", f"expected object, got {type(raw).__name__}")

    ticker = _nonempty_str(raw, "ticker")
    as_of = raw.get("as_of")
    if not isinstance(as_of, str) or not _ISO_DATE_RE.match(as_of):
        raise _err("as_of", f"expected an ISO date, got {as_of!r}")
    eligibility = _member(raw, "entry_eligibility", ENTRY_ELIGIBILITIES)
    entry_reasons = _reason_list(raw, "entry_reasons")
    readiness = _member(raw, "analysis_readiness", ANALYSIS_READINESS)
    analysis_reasons = _reason_list(raw, "analysis_reasons")
    profile_sha = _sha256(raw, "profile_sha256", required=True)
    meta = raw.get("meta")
    if not isinstance(meta, dict) or not meta:
        raise _err("meta", "expected a non-empty object")

    is_refusal = "refusal_kind" in raw
    market_sha = _sha256(raw, "market_snapshot_sha256", required=False)

    if is_refusal:
        refusal_kind = _member(raw, "refusal_kind", REFUSAL_KINDS)
        present = [f for f in MODEL_AUTHORED_FIELDS if f in raw]
        if present:
            raise _err("<root>",
                       f"refusal variant carries model-authored field(s) "
                       f"{present}; a refusal that also recommends is a "
                       f"recommendation")
        invalidations = ()
        if "invalidation_conditions" in raw:
            invalidations = _validate_condition_list(
                raw["invalidation_conditions"], "invalidation_conditions")

        if refusal_kind == "analysis_unavailable":
            if market_sha is None:
                raise _err("market_snapshot_sha256",
                           "required when refusal_kind is analysis_unavailable "
                           "— the refusal is a claim about a specific snapshot")
        else:  # ineligible
            if market_sha is not None:
                raise _err("market_snapshot_sha256",
                           "forbidden when refusal_kind is ineligible — the "
                           "instrument was refused before any snapshot was "
                           "consulted")
            if readiness != "unavailable":
                raise _err("analysis_readiness",
                           f"must be `unavailable` for an ineligible refusal, "
                           f"got {readiness!r}")
            if analysis_reasons != ("not_evaluated_instrument_refused",):
                raise _err("analysis_reasons",
                           f"must be exactly ['not_evaluated_instrument_refused'] "
                           f"for an ineligible refusal, got "
                           f"{list(analysis_reasons)}")

        held = raw.get("held_exit_context")
        if held is not None:
            if not isinstance(held, dict):
                raise _err("held_exit_context",
                           f"expected an object, got {type(held).__name__}")
            _validate_condition_list(held.get("invalidation_conditions"),
                                     "held_exit_context.invalidation_conditions")
            ts = held.get("conditions_authored_at")
            if not isinstance(ts, str) or not _TIMESTAMP_RE.match(ts):
                raise _err("held_exit_context.conditions_authored_at",
                           f"expected an ISO timestamp, got {ts!r}")
            _sha256(held, "source_thesis_sha256", required=True)

        return EtfThesis(
            variant="refusal", ticker=ticker, as_of=as_of,
            entry_eligibility=eligibility, entry_reasons=entry_reasons,
            analysis_readiness=readiness, analysis_reasons=analysis_reasons,
            profile_sha256=profile_sha, market_snapshot_sha256=market_sha,
            refusal_kind=refusal_kind, merit_recommendation=None, kind=None,
            invalidation_conditions=invalidations, entry_conditions=(),
            held_exit_context=held, raw=raw)

    # --- thesis variant ----------------------------------------------------
    if market_sha is None:
        raise _err("market_snapshot_sha256",
                   "required on the thesis variant — a thesis is an argument "
                   "about specific run-day evidence")
    merit = _member(raw, "merit_recommendation", MERITS)
    kind = _member(raw, "kind", ETF_KINDS)
    _validate_evidence_list(raw.get("merit_evidence"), "merit_evidence")

    timing = raw.get("technical_timing")
    if not isinstance(timing, dict):
        raise _err("technical_timing",
                   f"expected an object, got {type(timing).__name__}")
    if timing.get("assessment") not in TIMING_ASSESSMENTS:
        raise _err("technical_timing.assessment",
                   f"{timing.get('assessment')!r} is outside "
                   f"{sorted(TIMING_ASSESSMENTS)}")
    _validate_evidence_list(timing.get("evidence"), "technical_timing.evidence")

    environment = raw.get("environment")
    if not isinstance(environment, dict):
        raise _err("environment",
                   f"expected an object, got {type(environment).__name__}")
    _nonempty_str(environment, "assessment", field="environment.assessment")
    _validate_evidence_list(environment.get("evidence"), "environment.evidence")

    entry_conditions = _validate_condition_list(raw.get("entry_conditions"),
                                                "entry_conditions")
    invalidations = _validate_condition_list(raw.get("invalidation_conditions"),
                                             "invalidation_conditions")

    return EtfThesis(
        variant="thesis", ticker=ticker, as_of=as_of,
        entry_eligibility=eligibility, entry_reasons=entry_reasons,
        analysis_readiness=readiness, analysis_reasons=analysis_reasons,
        profile_sha256=profile_sha, market_snapshot_sha256=market_sha,
        refusal_kind=None, merit_recommendation=merit, kind=kind,
        invalidation_conditions=invalidations, entry_conditions=entry_conditions,
        held_exit_context=None, raw=raw)


def load_etf_thesis(path: str | Path) -> EtfThesis:
    path = Path(path)
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _err("<file>", f"unreadable: {exc}") from exc
    return validate_etf_thesis(raw)


# ---------------------------------------------------------------------------
# the bundle loader — where evidence binding is actually enforced
# ---------------------------------------------------------------------------

def _resolve(doc: Mapping[str, Any], dotted: str):
    cursor: Any = doc
    for part in dotted.split("."):
        if isinstance(cursor, list):
            try:
                idx = int(part)
            except ValueError:
                return _ABSENT
            if idx >= len(cursor):
                return _ABSENT
            cursor = cursor[idx]
            continue
        if not isinstance(cursor, dict) or part not in cursor:
            return _ABSENT
        cursor = cursor[part]
    return cursor


def _values_equal(a, b) -> bool:
    if _finite(a) and _finite(b):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    return a == b


@dataclass(frozen=True)
class EtfBundle:
    thesis: EtfThesis
    profile: Any
    market_snapshot: Optional[Mapping[str, Any]]


def load_etf_bundle(thesis_path, profile_path, market_path, *,
                    expected_ticker: str, authoring_date=None) -> EtfBundle:
    """Load a thesis together with the artifacts it was written against, and
    refuse the set unless it still hangs together.

    The checks are ordered so a cheap structural failure is reported before an
    expensive recomputation, and every one of them answers "could this thesis
    be about different data than it claims?".

    **The clock is the thesis's own `as_of`, not today.** Readiness is
    recomputed to confirm the stored verdict was correct FOR THE RUN THAT
    WROTE IT; recomputing against the reading date instead would fail every
    thesis older than one session on price staleness, which is a fact about
    the calendar rather than about the artifact. `authoring_date` is optional
    and, when given, must equal `as_of` — a caller that believes a different
    date has a bug worth surfacing loudly rather than absorbing.
    """
    from scripts.etf.readiness import analysis_readiness
    from scripts.schemas.etf_profile import validate_etf_profile

    thesis = load_etf_thesis(thesis_path)
    if thesis.ticker != expected_ticker:
        raise _err("ticker", f"thesis is for {thesis.ticker}, not "
                             f"{expected_ticker}")

    stored_date = datetime.date.fromisoformat(thesis.as_of)
    if authoring_date is not None and authoring_date != stored_date:
        raise _err("as_of",
                   f"caller passed authoring_date {authoring_date}, thesis "
                   f"was authored {thesis.as_of}")
    authoring_date = stored_date

    profile_bytes = Path(profile_path).read_bytes()
    if sha256_bytes(profile_bytes) != thesis.profile_sha256:
        raise _err("profile_sha256",
                   "the profile on disk is not the one this thesis was "
                   "written against")
    profile_doc = json.loads(profile_bytes.decode("utf-8"))
    profile = validate_etf_profile(profile_doc)
    if profile.ticker != expected_ticker:
        raise _err("profile.ticker", f"profile is for {profile.ticker}")
    if profile.entry_eligibility != thesis.entry_eligibility:
        raise _err("entry_eligibility",
                   f"thesis says {thesis.entry_eligibility!r}, profile says "
                   f"{profile.entry_eligibility!r}")

    market_doc = None
    if thesis.market_snapshot_sha256 is not None:
        if market_path is None:
            raise _err("market_snapshot_sha256",
                       "a thesis bound to a snapshot cannot be loaded without "
                       "it")
        market_bytes = Path(market_path).read_bytes()
        if sha256_bytes(market_bytes) != thesis.market_snapshot_sha256:
            raise _err("market_snapshot_sha256",
                       "the market snapshot on disk is not the one this "
                       "thesis was written against")
        market_doc = json.loads(market_bytes.decode("utf-8"))
        recomputed = analysis_readiness(market_doc, ticker=expected_ticker,
                                        authoring_date=authoring_date)
        if recomputed.readiness != thesis.analysis_readiness:
            raise _err("analysis_readiness",
                       f"stored {thesis.analysis_readiness!r} but the bound "
                       f"snapshot recomputes to {recomputed.readiness!r}")

    docs = {"etf_profile": profile_doc, "etf_market_snapshot": market_doc}

    def _check_refs(refs, field: str):
        for i, ref in enumerate(refs):
            fp = f"{field}[{i}]"
            doc = docs.get(ref["artifact"])
            if doc is None:
                raise _err(f"{fp}.artifact",
                           f"cites {ref['artifact']} but no such artifact is "
                           f"bound to this thesis")
            found = _resolve(doc, ref["field_path"])
            if found is _ABSENT:
                raise _err(f"{fp}.field_path",
                           f"{ref['field_path']!r} does not exist in "
                           f"{ref['artifact']}")
            if ref["source_kind"] == "API" and not _values_equal(found, ref["value"]):
                raise _err(f"{fp}.value",
                           f"claims {ref['value']!r} but {ref['artifact']}."
                           f"{ref['field_path']} holds {found!r}")

    def _check_conditions(conditions, field: str):
        for i, cond in enumerate(conditions):
            fp = f"{field}[{i}]"
            doc = docs.get(cond["artifact"])
            if doc is None:
                raise _err(f"{fp}.artifact",
                           f"watches {cond['artifact']} but no such artifact "
                           f"is bound to this thesis")
            # The WATCHED value need not exist yet — the condition describes a
            # future observation. What must resolve is the path's container,
            # so a typo cannot produce a condition that can never fire.
            parent = cond["watch_field_path"].rsplit(".", 1)[0]
            if parent != cond["watch_field_path"]:
                if _resolve(doc, parent) is _ABSENT:
                    raise _err(f"{fp}.watch_field_path",
                               f"{cond['watch_field_path']!r} names nothing in "
                               f"{cond['artifact']}; a condition on a path that "
                               f"cannot resolve can never fire")
            elif cond["watch_field_path"] not in doc:
                raise _err(f"{fp}.watch_field_path",
                           f"{cond['watch_field_path']!r} names nothing in "
                           f"{cond['artifact']}")

    raw = thesis.raw
    if thesis.variant == "thesis":
        _check_refs(raw["merit_evidence"], "merit_evidence")
        _check_refs(raw["technical_timing"]["evidence"],
                    "technical_timing.evidence")
        _check_refs(raw["environment"]["evidence"], "environment.evidence")
        _check_conditions(raw["entry_conditions"], "entry_conditions")
        _check_conditions(raw["invalidation_conditions"],
                          "invalidation_conditions")

    return EtfBundle(thesis=thesis, profile=profile, market_snapshot=market_doc)
