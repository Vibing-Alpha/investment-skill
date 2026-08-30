"""Deterministic STATEMENT-INTEGRITY surfacer for fetched financial data.

Named failure modes this prevents (2026-08-28 Cowork run, 6 tickers, plus
the local corpus of 41 stored reports):

1. **A sign that flips inside one column.** `interest_expense` carried a lone
   negative on BE (-17.9M among positives), RKLB (-581k), SNDK (-40M) and
   GOOG (-438M), and the provider's own `metrics_snapshot.interest_coverage`
   turned RKLB's into **-7.55**. `capital_expenditure` reversed convention
   part-way through SNDK's series (negative for 7 quarters, positive for 3)
   and carried a lone positive on AAOI and P — P's is the LATEST quarter, so
   a TTM free-cash-flow figure built on it is wrong by twice the capex.
2. **A metrics snapshot a quarter behind the statements, with nothing saying
   so.** SNDK's snapshot sat at 2026-01-02 while the statements reached
   2026-04-03; SPCX's described a capital base roughly 4x smaller than the
   post-IPO one. Consumers read `debt_to_equity` / `interest_coverage` /
   `earnings_per_share` straight off it.
3. **A statement basis that changes mid-series.** SPCX mixes two S-1/A rows
   with one 10-Q; across that boundary `deferred_revenue` appears to collapse
   13,236M -> 7,977M while `deposit_liabilities` goes null -> 14,286M. It is
   a field-mapping shift, not a business event.

DESIGN — surface evidence, never a verdict. Same contract as
`scripts/anomaly.py`, settled there over three cold-review rounds and
recorded again in `.claude/skills/score-business/gotchas.md`: a threshold
script cannot reliably tell a genuine economic sign change from a provider
convention change, and each way it could FALSELY confirm one feeds a bad
number into scoring. So this module notices and reports; the agent
adjudicates, per `prompts/score-fundamental.md`. Nothing here mutates a
value, nullifies a field, or emits a "corrupt" flag.

SCOPE — every rule below was measured against the 41 stored reports before
it shipped, and each is narrowed to where it had NO false positives:

- Sign checks run on `interest_expense` and `capital_expenditure` ONLY.
  Those two are economically single-signed within a reporting convention.
  A generic "any numeric column whose signs disagree" scan fires on
  `net_income`, free cash flow and `issuance_or_repayment_of_debt_securities`,
  where a sign change is ordinary news. Measured: 14 of 79 series flagged,
  every one a genuine provider artifact.
- Basis boundaries report REGISTRATION statements among periodic filings,
  duplicate fiscal-period labels, and mixed label formats. Raw form-type
  heterogeneity is NOT reported: 10 of 15 such flags over the corpus are
  just the fiscal-Q4 row arriving from the 10-K, which is routine.
- All-null and all-zero column scans are deliberately ABSENT. Every
  all-zero column in the corpus (`dividends_per_common_share`,
  `preferred_dividends_impact`, `net_income_discontinued_operations`,
  `net_income_non_controlling_interests`, `current_investments`) is
  legitimately zero — a 0/10 true-positive rate. The one demonstrated harm
  from an all-null column (BE's SBC read as "SBC is low") was fixed where it
  happened, in `scripts.adr.detect`, not with a telemetry block.

Written into `02_financial_data.json` as `data_quality`, beside
`anomalous_quarters` — the file the fundamental agent actually reads.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

# Columns whose sign is fixed by reporting convention rather than by the
# business. Each earns its place with a demonstrated failure (see module
# docstring); do not widen this set without another one.
# Fields with a FIXED sign convention within one provider's series, so that
# both signs appearing in one column is evidence of something (a convention
# change, a netting/reversal, or a mapping error). The detector is generic;
# only this list decides what it looks at, and a field missing from it is a
# defect nothing reports — NOW 2026-08-30 carried a -368,000,000 SBC quarter
# among four positive ones (the 10-Q says +655M) and it passed unremarked
# into `growth_stock_mode.sbc_ratio`.
# Fields a PROVIDER TTM ratio divides by, where the column can be populated
# on some quarters and null on others. A sparse denominator produces a ratio
# whose numerator spans 12 months and whose denominator spans 3 — positive,
# correctly signed and ~4x flattering, which is invisible to every other
# check here. NOW 2026-08-30: `interest_expense` on 1 of 5 quarters,
# `metrics_snapshot.interest_coverage` 36.55 against a recomputed 7.6x.
# Add a field here only with a named ratio it corrupts.
_TTM_INPUT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("income_statements", "interest_expense",
     "metrics_snapshot.interest_coverage"),
)
_TTM_WINDOW_QUARTERS = 4

_SIGN_CHECKED_FIELDS: tuple[tuple[str, str], ...] = (
    ("income_statements", "interest_expense"),
    ("cash_flows", "capital_expenditure"),
    ("cash_flows", "share_based_compensation"),
    ("cash_flows", "depreciation_and_amortization"),
)

# Periodic reports. Anything else in `form_type` — S-1, S-1/A, F-1, 424B4,
# 10-12B — is a registration or transition document whose statements can be
# built on a different basis than the periodic series around it.
_PERIODIC_FORM_TYPES = frozenset({
    "10-K", "10-Q", "20-F", "40-F", "6-K", "11-K",
})


def _finite(v: Any) -> Optional[float]:
    """Finite float, else None. Booleans are REJECTED, not coerced: a bool in
    a numeric slot is drift, and `float(True) == 1.0` would manufacture a
    positive sign out of it."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _as_date(v: Any):
    """`datetime.date` from an ISO `report_period`, else None.

    Cold review 2026-08-28: comparing these as raw TEXT let a malformed row
    win a `max()` (`"not-a-date"` sorts after every real date, manufacturing
    a one-quarter snapshot lag) and made unpadded dates sort wrong
    (`2026-2-01` after `2026-10-01`), which scrambles the run-length view of
    signs. A non-string value also reached the sort key through
    `r.get(...) or ""` — an int is truthy — and raised TypeError, breaking
    this module's never-raises contract.
    """
    import datetime as _dt
    if not isinstance(v, str):
        return None
    raw = v.strip()
    # The WHOLE string must be a date or an ISO timestamp. Slicing `[:10]`
    # accepted junk with a date-shaped prefix (`2026-06-30JUNK`,
    # `2026-08-14 trailing`) and then asserted alignment for a malformed
    # snapshot period — the same truncation defect fixed in indicators.py
    # the same day, transplanted here (cold review 2026-08-29).
    for parse in (
        lambda t: _dt.date.fromisoformat(t),
        # Unpadded ISO-like forms (`2026-2-01`) are real dates a provider
        # does emit and fromisoformat rejects.
        lambda t: _dt.datetime.strptime(t, "%Y-%m-%d").date(),
        # Full timestamps.
        lambda t: _dt.datetime.fromisoformat(
            t[:-1] + "+00:00" if t.endswith("Z") else t).date(),
    ):
        try:
            return parse(raw)
        except ValueError:  # fail-open-ok: an unparseable period is reported as unknown, never assumed — every caller treats None as "not comparable" (excluded from the sign series and from the snapshot-lag count)
            continue
    return None


def _dict_rows(payload: Mapping, family: str) -> List[Dict]:
    rows = payload.get(family)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _chronological(rows: List[Dict]) -> List[Dict]:
    """Oldest-first by PARSED date. Producers are not guaranteed to order
    rows, and a run-length view of signs is meaningless in an arbitrary
    order. Rows whose period will not parse sort last, deterministically,
    rather than raising or landing among the real dates."""
    import datetime as _dt
    return sorted(
        rows,
        key=lambda r: (
            _as_date(r.get("report_period")) is None,
            _as_date(r.get("report_period")) or _dt.date.min,
        ),
    )


def _base_form_type(raw: Any) -> Optional[str]:
    """`10-Q/A` -> `10-Q`. An amendment restates the same periodic report."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper()
    if not s:
        return None
    return s.split("/", 1)[0].strip()


def _sign_name(v: float) -> str:
    return "positive" if v > 0 else "negative"


def _detect_sign_inconsistencies(payload: Mapping) -> List[Dict]:
    out: List[Dict] = []
    for family, field in _SIGN_CHECKED_FIELDS:
        rows = _chronological(_dict_rows(payload, family))
        observations = []
        for r in rows:
            # A row that cannot be dated cannot be placed in a run.
            # Sorting such rows to the end fabricated a chronology the
            # prompt then reads as "a stray final period" (cold review
            # 2026-08-29). Excluded here; counted in `undateable_rows`.
            if _as_date(r.get("report_period")) is None:
                continue
            v = _finite(r.get(field))
            # A zero has no sign. It is excluded from the sign read here and
            # is a separate question (SNDK's zero-filled
            # `issuance_or_repayment_of_debt_securities`) this module does
            # not claim to answer.
            if v is None or v == 0:
                continue
            observations.append((r, v))
        if len(observations) < 2:
            continue
        signs = [_sign_name(v) for _, v in observations]
        if len(set(signs)) < 2:
            continue

        runs: List[List[Any]] = []
        for s in signs:
            if runs and runs[-1][0] == s:
                runs[-1][1] += 1
            else:
                runs.append([s, 1])

        positives = signs.count("positive")
        dominant = "positive" if positives * 2 > len(signs) else (
            "negative" if positives * 2 < len(signs) else None
        )
        minority = [
            {
                "report_period": r.get("report_period"),
                "value": v,
                "form_type": r.get("form_type"),
            }
            for (r, v), s in zip(observations, signs)
            if dominant is not None and s != dominant
        ]
        out.append({
            "family": family,
            "field": field,
            "observations": len(observations),
            "sign_runs": runs,
            "dominant_sign": dominant,
            "minority_count": len(minority) if dominant is not None else None,
            "minority_periods": minority,
            "note": (
                f"`{field}` carries both signs within one series. This is "
                f"evidence, NOT a verdict: it can be a provider convention "
                f"change, a netting/reversal, or a mapping error, and which "
                f"one it is cannot be settled from the numbers. A single "
                f"minority period reads as a stray; two long runs read as a "
                f"convention change. Corroborate against the filing before "
                f"using this column, and treat any provider-derived ratio "
                f"built on it (e.g. `metrics_snapshot.interest_coverage`) as "
                f"unknown until you have."
            ),
        })
    return out


def _detect_sparse_ttm_inputs(payload: Mapping) -> List[Dict]:
    """Fields that are populated on SOME of the trailing quarters but not all.

    Reports coverage, never a verdict: a provider TTM ratio built on a
    partly-null column mixes window lengths, and the consumer cannot tell
    that from the ratio's face.

    A column null on EVERY quarter is excluded, on the narrower ground that
    it carries no PARTIAL evidence to mis-weight — not on any claim about
    what the provider builds from it (measured 2026-08-30: no stored artifact
    has a wholly-absent trailing-4Q `interest_expense` beside a finite
    snapshot `interest_coverage`, so the case is unobserved here). If one
    appears, `snapshot_period_alignment` and the sign check still speak; this
    detector answers only "is the column partly populated".

    The window is the last four DATEABLE rows of the family, which is not
    guaranteed to be the window the provider's own ratio used — 8 of 22
    stored snapshots carry a `report_period` different from their latest
    statement. `snapshot_period_alignment` is the field that answers that
    question; this one answers coverage.
    """
    out: List[Dict] = []
    for family, field, ratio in _TTM_INPUT_FIELDS:
        rows = _chronological(_dict_rows(payload, family))
        rows = [r for r in rows if _as_date(r.get("report_period")) is not None]
        window = rows[-_TTM_WINDOW_QUARTERS:]
        if len(window) < _TTM_WINDOW_QUARTERS:
            continue      # not a full trailing window — nothing to compare
        populated = [r for r in window if _finite(r.get(field)) is not None]
        if not populated or len(populated) == len(window):
            continue      # wholly absent, or complete
        out.append({
            "family": family,
            "field": field,
            "window_quarters": len(window),
            "populated_quarters": len(populated),
            "populated_periods": [r.get("report_period") for r in populated],
            "note": (
                f"`{field}` is reported on {len(populated)} of the trailing "
                f"{len(window)} quarters. A provider TTM ratio built on it "
                f"(`{ratio}`) divides a 12-month numerator by a "
                f"{len(populated)}-quarter denominator, which flatters it by "
                f"roughly {len(window) / len(populated):.0f}x. This is "
                f"evidence about COVERAGE, not about the values themselves — "
                f"each reported figure may be perfectly correct. Recompute "
                f"from the quarters that carry the field, say which ones, or "
                f"mark the ratio `unknown`."
            ),
        })
    return out


def _detect_snapshot_alignment(payload: Mapping) -> Dict:
    snapshot = payload.get("metrics_snapshot")
    snapshot_rp_raw = (
        snapshot.get("report_period") if isinstance(snapshot, Mapping) else None
    )
    snapshot_date = _as_date(snapshot_rp_raw)
    snapshot_rp = snapshot_rp_raw if snapshot_date is not None else None

    # PARSED dates only. An unparseable row is not evidence of a newer
    # statement, and letting one win the comparison as text manufactured a
    # lag the fundamental prompt treats as reason to discard the snapshot.
    dated = [
        (_as_date(r.get("report_period")), r.get("report_period"))
        for r in _dict_rows(payload, "income_statements")
    ]
    dated = [(d, raw) for d, raw in dated if d is not None]
    latest = max(dated)[1] if dated else None

    newer: Optional[int] = None
    if snapshot_date is not None and dated:
        newer = sum(1 for d, _ in dated if d > snapshot_date)

    return {
        "snapshot_report_period": snapshot_rp,
        "latest_statement_report_period": latest,
        # None means "not determinable", never "aligned" — reporting 0 for a
        # snapshot with no period would assert an alignment nobody checked
        # (GOOG 2026-08-03 has no snapshot period at all).
        "statements_newer_than_snapshot": newer,
        "note": (
            "`metrics_snapshot` is the provider's own TTM roll-up and can lag "
            "the statements. When `statements_newer_than_snapshot` is above "
            "0, every ratio read off the snapshot (`debt_to_equity`, "
            "`interest_coverage`, `earnings_per_share`, margins) describes an "
            "older capital base — recompute from `income_statements` / "
            "`balance_sheets` rather than quoting it. `null` means the lag "
            "could not be determined, not that it is zero."
        ),
    }


def _detect_basis_boundaries(payload: Mapping) -> List[Dict]:
    out: List[Dict] = []
    for family in ("income_statements", "balance_sheets", "cash_flows"):
        rows = _chronological(_dict_rows(payload, family))
        if not rows:
            continue

        non_periodic = [
            {"report_period": r.get("report_period"),
             "form_type": r.get("form_type")}
            for r in rows
            if (_base_form_type(r.get("form_type")) is not None
                and _base_form_type(r.get("form_type"))
                not in _PERIODIC_FORM_TYPES)
        ]
        if non_periodic and len(non_periodic) != len(rows):
            out.append({
                "kind": "non_periodic_form_type",
                "family": family,
                "rows": non_periodic,
                "note": (
                    "Rows sourced from a registration/transition document sit "
                    "beside periodic filings. Line items can be mapped to "
                    "different labels on either side of that boundary, so a "
                    "field's apparent jump across it may be a mapping shift "
                    "rather than a business event (SPCX: `deferred_revenue` "
                    "13,236M -> 7,977M while `deposit_liabilities` goes null "
                    "-> 14,286M). Verify any cross-boundary trend against the "
                    "filing before scoring it, and do not build a growth rate "
                    "across it without saying you did."
                ),
            })

        labels = [r.get("fiscal_period") for r in rows
                  if isinstance(r.get("fiscal_period"), str)
                  and r.get("fiscal_period")]
        # A duplicate is one label on two rows in the SAME year. A label
        # recurring once per fiscal year is ordinary — SPCX carries `Q1` a
        # year apart, and flagging that discourages a legitimate YoY
        # comparison. An earlier narrowing used `"-" in label` as a proxy
        # for "year-qualified"; it missed a genuine same-year collision on
        # a bare `Q1`, missed `FY2026` / `2026Q1` / `Q1 2026` / `2026/Q1`
        # entirely, and counted `TTM-LTM` as qualified (cold review
        # 2026-08-29). Ask the real question instead, off report_period.
        _by_label: Dict[str, List[int]] = {}
        for r in rows:
            lbl = r.get("fiscal_period")
            d = _as_date(r.get("report_period"))
            if isinstance(lbl, str) and lbl and d is not None:
                _by_label.setdefault(lbl, []).append(d.year)
        duplicates = sorted({
            lbl for lbl, years in _by_label.items()
            if len(years) != len(set(years))
        })
        if duplicates:
            out.append({
                "kind": "duplicate_fiscal_period_label",
                "family": family,
                "labels": duplicates,
                "note": (
                    "Two or more rows carry the same `fiscal_period` label "
                    "while covering different periods. Key any quarter by "
                    "`report_period`, not by this label."
                ),
            })

        # `2026-Q2` vs a bare `Q1`: a year-qualified label and an unqualified
        # one in the same series cannot be ordered or compared as written.
        formats = {("dated" if "-" in lbl else "bare") for lbl in labels}
        if len(formats) > 1:
            out.append({
                "kind": "mixed_fiscal_period_label_format",
                "family": family,
                "formats": sorted(formats),
                "note": (
                    "`fiscal_period` mixes year-qualified labels (`2026-Q2`) "
                    "with bare ones (`Q1`) in one series — the bare labels "
                    "carry no year and must not be sorted or matched as text."
                ),
            })
    return out


def detect_statement_integrity(
    financial_output: Mapping, *, ticker: str,
) -> Dict:
    """Surface statement-integrity evidence for one ticker's fetched
    financials. Never raises on malformed input; never mutates it.

    `ticker` is accepted for parity with the other detectors' call shape and
    for future message text; the result does not depend on it.
    """
    if not isinstance(financial_output, Mapping):
        financial_output = {}
    return {
        # Rows whose `report_period` will not parse, per family. They are
        # excluded from the sign series and from the snapshot-lag count
        # (they cannot be ordered), so say how many rather than dropping
        # them silently.
        "undateable_rows": {
            fam: sum(1 for r in _dict_rows(financial_output, fam)
                     if _as_date(r.get("report_period")) is None)
            for fam in ("income_statements", "balance_sheets", "cash_flows")
        },
        "sign_inconsistencies": _detect_sign_inconsistencies(financial_output),
        "sparse_ttm_inputs": _detect_sparse_ttm_inputs(financial_output),
        "snapshot_period_alignment": _detect_snapshot_alignment(
            financial_output),
        "statement_basis_boundaries": _detect_basis_boundaries(
            financial_output),
    }
