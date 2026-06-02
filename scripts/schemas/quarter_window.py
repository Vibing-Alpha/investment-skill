"""Typed contract for cross-statement-aligned TTM quarter windows.

Single SoT for the 4-quarter slice consumed by extract_fcf,
historical_multiples, adr/correct (both valuation + EPS-check), adr/detect
(DL4 §3.2).

Two helpers:
  aligned_quarters(...)              -> "latest trailing 4" window (one)
  iter_aligned_quarter_windows(...)  -> all valid 4-windows, oldest-first
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Iterator, Literal, Mapping, Optional

from scripts.schemas.errors import DataQualityError, SchemaError  # DataQualityError added v9 (Fix A)

_ARTIFACT = "quarter_window"
_FISCAL_PERIOD_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_REPORT_PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SOURCE_TAG_RE = re.compile(r"^\[Calc:[^\]]+\]$")  # cycle-5 fix: helper computes alignment, not sources data


def _is_valid_report_period(value: str) -> bool:
    """report_period must match YYYY-MM-DD shape AND be a real calendar date.
    Post-impl ISS-011 (fresh-loop1): pre-fix `_REPORT_PERIOD_RE.fullmatch`
    accepted shape-only matches like "2024-99-99" / "2024-02-30" which then
    flowed into AlignedQuarter as a valid report_period — downstream
    consumers (date sort, lookback, anchor) silently propagated the
    nonsense date. Combine regex shape + date.fromisoformat strict parse."""
    if not isinstance(value, str) or not _REPORT_PERIOD_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True

# Production vocabulary at fetch time: rows carry either
# `period in {"quarter", "quarterly"}` or `fiscal_period = "YYYY-QN"`.
# Output type is normalized to the single value.
PeriodKind = Literal["quarterly"]

FailureKind = Literal[
    "intersection_lt_4",
    "non_consecutive",
    "unparseable_fiscal_period",
    "missing_required_field",       # load-bearing drop reduced intersection below 4
    "statement_metadata_mismatch",  # fiscal_period or currency disagree at same report_period (invariant 9)
    "duplicate_report_period",      # same report_period appears twice within ONE statement family (invariant 11)
]


@dataclass(frozen=True)
class AlignedQuarter:
    """One row in the aligned 4-tuple. Carries the matched income / cash_flow
    / balance rows so consumers do NOT re-lookup by report_period (invariant 4).

    `__post_init__` enforces invariant 6 (source_tag regex), invariant 10
    (period_kind = "quarterly" only in v1), invariant 4 (rows are Mapping),
    and YYYY-MM-DD / YYYY-QN format for report_period / fiscal_period.
    These checks run on EVERY construction path — including hand-constructed
    test fixtures — so the dataclass cannot be invalid after construction.
    (Cycle-2 fix: spec v2 placed validators inside _build_aligned_quarter
    only, which left direct dataclass construction unvalidated.)"""
    report_period: str          # YYYY-MM-DD (calendar date of fiscal-quarter end)
    fiscal_period: str          # YYYY-QN (canonical after invariant-9 agreement)
    period_kind: PeriodKind     # always "quarterly" in v1
    statement_currency: str     # ALL 3 statements agree (invariant 9); upstream gate enforces USD per invariant 7
    income_row: Mapping[str, object]    # canonical mapping; consumers read income_row["net_income"], etc.
    cash_flow_row: Mapping[str, object]
    balance_row: Mapping[str, object]
    source_tag: str             # default "[Calc: aligned_quarter_window from input rows]" — strict format per invariant 6 (cycle-5 fix)

    def __post_init__(self):
        if self.period_kind != "quarterly":
            raise SchemaError(_ARTIFACT, "period_kind",
                              f"must be 'quarterly', got {self.period_kind!r}")
        if not _is_valid_report_period(self.report_period):
            raise SchemaError(_ARTIFACT, "report_period",
                              f"must be a valid YYYY-MM-DD calendar date, "
                              f"got {self.report_period!r}")
        # Post-impl ISS-023 (fresh-loop2): guard isinstance str BEFORE
        # regex.fullmatch — `re.fullmatch(None)` raises TypeError, which
        # consumers `except SchemaError` chains don't catch. Pre-fix the
        # documentation claimed "cannot be invalid after construction" but
        # a None fiscal_period / source_tag would escape as TypeError.
        if not isinstance(self.fiscal_period, str) or \
                not _FISCAL_PERIOD_RE.fullmatch(self.fiscal_period):
            raise SchemaError(_ARTIFACT, "fiscal_period",
                              f"must be a YYYY-QN string, got "
                              f"{type(self.fiscal_period).__name__}="
                              f"{self.fiscal_period!r}")
        if not isinstance(self.source_tag, str) or \
                not _SOURCE_TAG_RE.fullmatch(self.source_tag):
            raise SchemaError(_ARTIFACT, "source_tag",
                              f"must match {_SOURCE_TAG_RE.pattern}, "
                              f"got {type(self.source_tag).__name__}="
                              f"{self.source_tag!r}")
        for name in ("income_row", "cash_flow_row", "balance_row"):
            row = getattr(self, name)
            if not isinstance(row, Mapping):
                raise SchemaError(_ARTIFACT, name,
                                  f"must be Mapping, got {type(row).__name__}")
        # Post-impl ISS-032 (fresh-loop3): the dataclass is frozen but the
        # Mapping rows it holds were not — a caller passing a `dict` row
        # could mutate `row["currency"]` / `row["report_period"]` after
        # construction, breaking the cross-row invariants that
        # __post_init__ enforces. Snapshot each row through MappingProxyType
        # over a shallow dict copy so external mutation can't propagate
        # into already-constructed AlignedQuarter instances. The
        # `object.__setattr__` form is required because the dataclass is
        # frozen (regular assignment would raise FrozenInstanceError).
        for name in ("income_row", "cash_flow_row", "balance_row"):
            row = getattr(self, name)
            object.__setattr__(self, name, MappingProxyType(dict(row)))
        # fresh-loop2-cycle2 C2A-MED-3: also reject whitespace-only currency
        # (`"   "` is truthy in `not self.statement_currency` so pre-fix
        # passed without `_build_aligned_quarter`'s `.strip().upper()`
        # normalization being applied to direct-constructor instances).
        if (not isinstance(self.statement_currency, str)
                or not self.statement_currency.strip()):
            raise SchemaError(_ARTIFACT, "statement_currency",
                              f"must be non-empty str (whitespace-only "
                              f"rejected), got "
                              f"{self.statement_currency!r}")
        # Cross-row invariant 9 enforcement (post-impl ISS-003): if a row
        # carries report_period / fiscal_period / currency / period, those
        # values MUST match the AlignedQuarter top-level fields. Construction
        # via `_build_aligned_quarter` already enforces these; the post_init
        # check guards direct dataclass construction (test fixtures, future
        # consumers) from producing an inconsistent instance. Missing keys
        # (None / absent) are tolerated — the helper-built path may strip
        # redundant fields from rows.
        for name in ("income_row", "cash_flow_row", "balance_row"):
            row = getattr(self, name)
            row_rp = row.get("report_period")
            if row_rp is not None and row_rp != self.report_period:
                raise SchemaError(_ARTIFACT, name,
                                  f"row report_period {row_rp!r} does not "
                                  f"match AlignedQuarter.report_period "
                                  f"{self.report_period!r}")
            row_fp = row.get("fiscal_period")
            if row_fp is not None and row_fp != self.fiscal_period:
                raise SchemaError(_ARTIFACT, name,
                                  f"row fiscal_period {row_fp!r} does not "
                                  f"match AlignedQuarter.fiscal_period "
                                  f"{self.fiscal_period!r}")
            row_cur = row.get("currency")
            # Post-impl ISS-034 (cycle 9 BLOCKING): normalize via strip().upper()
            # to match `_build_aligned_quarter`'s ISS-032 canonicalization.
            # Pre-fix this `__post_init__` cross-row check compared raw
            # `row_cur` against the canonicalized `self.statement_currency`,
            # so a row with `currency="usd"` would build OK
            # (statement_currency="USD") then immediately fail __post_init__
            # with SchemaError — an escape path consumers' `except
            # InsufficientQuartersError` wouldn't catch.
            # Post-impl ISS-051 (zero-context round 5 LOW): also skip
            # non-string currencies (e.g. currency=0, currency=[]) — they
            # are filtered out at _build_aligned_quarter's currency-set
            # construction (which gates on `isinstance(..., str)`), so the
            # `__post_init__` check must mirror that filter or it raises
            # SchemaError on a row the build path already accepted.
            if isinstance(row_cur, str) and row_cur.strip():
                row_cur_norm = row_cur.strip().upper()
                if row_cur_norm != self.statement_currency:
                    raise SchemaError(_ARTIFACT, name,
                                      f"row currency {row_cur!r} does not "
                                      f"match AlignedQuarter.statement_currency "
                                      f"{self.statement_currency!r}")
            row_period = row.get("period")
            if isinstance(row_period, str) and row_period.strip():
                # Post-impl ISS-010 (fresh-loop1): mirror _is_quarterly_vocab's
                # rule — any explicit non-quarterly period must reject, not
                # just "annual". Pre-fix only "annual" was rejected, so a
                # hand-constructed AlignedQuarter with row period="ttm" or
                # "semiannual" or "yearly" would build OK while
                # _build_period_map's vocabulary check would reject the same
                # row. Three-site (post_init / _is_quarterly_vocab /
                # row_matches_period) parity is the contract.
                pn = row_period.strip().lower()
                if pn not in {"quarter", "quarterly"}:
                    raise SchemaError(_ARTIFACT, name,
                                      f"row period={row_period!r} (normalized "
                                      f"to {pn!r}) incompatible with "
                                      f"AlignedQuarter.period_kind="
                                      f"{self.period_kind!r}; expected one "
                                      f"of 'quarter' / 'quarterly'")


AlignedQuarterWindow = tuple[AlignedQuarter, AlignedQuarter, AlignedQuarter, AlignedQuarter]


_FAILURE_KIND_VALUES = frozenset({
    "intersection_lt_4",
    "non_consecutive",
    "unparseable_fiscal_period",
    "missing_required_field",
    "statement_metadata_mismatch",
    "duplicate_report_period",
})


@dataclass(frozen=True)
class SkippedWindow:
    """Diagnostic record for a window that iter_aligned_quarter_windows
    rejected during sliding iteration. Consumers (historical_multiples)
    collect these alongside valid windows so operators can audit why a
    historical TTM slot is missing. (Cycle-2 fix: spec v2 hand-waved a
    `_diagnose_skipped_windows()` helper that was never defined.)"""
    anchor_report_period: str   # latest report_period in the rejected window
    failure_kind: FailureKind
    detail: str

    def __post_init__(self):
        # Post-impl ISS-010: match AlignedQuarter's strict construction
        # semantics — Literal hints are not runtime-enforced, so guard at
        # __post_init__. Fail-closed on invalid failure_kind / empty string
        # anchor_report_period or detail.
        if not isinstance(self.failure_kind, str) or \
                self.failure_kind not in _FAILURE_KIND_VALUES:
            raise SchemaError(_ARTIFACT, "failure_kind",
                              f"must be one of {sorted(_FAILURE_KIND_VALUES)}, "
                              f"got {self.failure_kind!r}")
        if not _is_valid_report_period(self.anchor_report_period):
            raise SchemaError(_ARTIFACT, "anchor_report_period",
                              f"must be a valid YYYY-MM-DD calendar date, "
                              f"got {self.anchor_report_period!r}")
        if not isinstance(self.detail, str) or not self.detail:
            raise SchemaError(_ARTIFACT, "detail",
                              f"must be non-empty str, got {self.detail!r}")


class InsufficientQuartersError(DataQualityError):
    """Raised by aligned_quarters / iter_aligned_quarter_windows when a window
    cannot be constructed.

    Cycle-9 v11 reviewer fix (HIGH-2): the `super().__init__(_ARTIFACT,
    "aligned_quarters", ...)` 3-arg call below requires the parent class to
    accept the `(artifact, field, message)` shape. v9 introduced
    `DataQualityError(ValueError)` as a bare `pass` class, which silently
    lost the named `.artifact` / `.field` / `.message` attributes on
    `InsufficientQuartersError` instances (they fell through to
    `ValueError.__init__(*args)` and stored as `e.args` tuple only).
    v11 gives `DataQualityError` the SAME `__init__(artifact, field, message)`
    shape as `SchemaError` so the constructor chain works identically. See
    `scripts/schemas/errors.py` v11 manifest row.

    Cycle-8 fix (Fix A — promoted from §11 backlog #1 after first-principles
    re-audit + superpower-reviewer validation): inherits from
    `DataQualityError(ValueError)`, NOT `SchemaError`. SchemaError is reserved
    for I/O / JSON-parse / schema-load failures (file not found, malformed
    JSON, missing required field at LOAD time). InsufficientQuartersError is
    a RUNTIME data-quality failure (artifact loaded fine, content doesn't
    satisfy quarter-count or metadata-equality invariants). Both still
    inherit ValueError so existing `except ValueError` catch chains preserve
    behavior — but new code SHOULD use `except DataQualityError` (or the
    specific subclass) for finer-grained error handling. The cycle-7 §3.2.0.B
    LOCAL-CATCH mandate stays as belt-and-suspenders defense; v9 makes the
    spec accept both `except InsufficientQuartersError` AND
    `except DataQualityError` as valid (the latter is broader but matches
    the design intent better).

    `DataQualityError` is added to `scripts/schemas/errors.py` (+5 LOC).
    Migration risk: zero — DataQualityError is a new ValueError subclass;
    no existing `except SchemaError` block was relying on
    `InsufficientQuartersError` being a SchemaError. (Verified at impl
    time via `git grep -nE 'except SchemaError' scripts/` — see §8
    Done-when grep gate.)"""

    def __init__(
        self,
        *,
        ticker: str,
        available: int,
        failure_kind: FailureKind,
        detail: str,
        dropped_rows: Optional[dict[str, dict[str, int]]] = None,
    ):
        self.ticker = ticker
        self.available = available
        self.required = 4
        self.failure_kind = failure_kind
        # dropped_rows: NESTED counter per cycle-5 HIGH-12 fix —
        # {statement_label: {drop_reason: count}}. statement_label in
        # {"income", "cash_flow", "balance"}; drop_reason in
        # {"missing_report_period", "missing_fiscal_period",
        #  "non_quarterly_period_kind"}. Operator can read per-family
        # counts to distinguish "data shape drift in one statement" from
        # "actually <4 quarters everywhere". Cycle-6 propagation fix:
        # earlier v3 wording was flat dict[str, int] — synced here to
        # match v5 nested algorithm.
        self.dropped_rows: dict[str, dict[str, int]] = {
            k: dict(v) for k, v in (dropped_rows or {}).items()
        }
        super().__init__(
            _ARTIFACT,
            "aligned_quarters",
            f"ticker={ticker} failure_kind={failure_kind} "
            f"available={available} required=4 "
            f"dropped_rows={self.dropped_rows} detail={detail}",
        )


def row_matches_period(
    row: Mapping, target: str, *, accept_missing: bool = False,
) -> bool:
    """Period-match predicate for caller-side pre-filters (post-impl ISS-039
    structural fix). Unifies the period-match semantic across ADR / fcf
    consumers so callers can't drift from `_is_quarterly_vocab`'s rules.

    target="quarterly" — delegates to `_is_quarterly_vocab`:
        - period in {"quarter", "quarterly"} (case-insensitive, strip-ed)
        - OR period missing/None AND fiscal_period matches `^\\d{4}-Q[1-4]$`
        - explicit non-quarterly period (e.g. "annual") rejects up-front

    target="annual":
        - period == "annual" (case-insensitive). If `accept_missing=False`
          (default, strict): None / missing rejects. If `accept_missing=True`:
          None / missing accepted as compatible (pre-DL4 lenient semantic
          for annual cash_flow / balance_sheet rows where providers commonly
          omit the period tag).

    Use cases:
    - Mode detection (`is_annual = ...`): `accept_missing=False` (strict).
    - Income statement filter for annual: `accept_missing=False` (strict —
      consistent with mode detection on the same family).
    - Cash flow / balance sheet filter for annual: `accept_missing=True`
      (lenient — provider tag omission must not drop the row, since the
      annual carve-out's downstream code uses `[:1]` single-row slice).

    Pre-fix the 3 caller sites in scripts/adr/correct.py x2 +
    scripts/adr/detect.py each duplicated a `period in {set}` literal
    filter. Cycle 12 ISS-039 unified them via this helper but defaulted
    annual to strict, dropping legitimate cf/balance rows whose period
    was missing — post-impl ISS-044 (zero-context cycle) regression.
    """
    if not isinstance(row, Mapping):
        return False
    if target == "quarterly":
        return _is_quarterly_vocab(row)
    if target == "annual":
        raw = row.get("period")
        if raw is None:
            if not accept_missing:
                return False
            # Post-impl ISS-052 (zero-context round 6 HIGH): even with
            # accept_missing=True, reject rows whose fiscal_period proves
            # they're quarterly (e.g. "2024-Q4"). Pre-fix ISS-044's
            # lenient missing-period acceptance allowed a quarterly cf
            # row (period=None, fiscal_period="YYYY-QN") to slip into
            # the annual carve-out's `[:1]` single-row slice, consumed
            # as an annual data point → wrong FCF / D&A / SBC math.
            # fresh-loop2 cycle 5 H2 (A-HIGH-3): also require positive
            # annual evidence — when BOTH period is None AND
            # fiscal_period is missing/empty/non-string, the row has
            # zero period evidence. Pre-fix code returned True
            # (affirmative-from-absence). Now require fiscal_period to
            # be a non-empty string that is NOT a quarterly tag, OR
            # explicitly accept the row only when fiscal_period proves
            # annual shape (YYYY / YYYY-FY).
            fp = row.get("fiscal_period")
            if not (isinstance(fp, str) and fp.strip()):
                # No period AND no fiscal_period evidence — absence,
                # not annual. Fail-close.
                return False
            if _FISCAL_PERIOD_RE.match(fp):
                # fiscal_period IS quarterly tag — reject (ISS-052).
                return False
            return True
        return isinstance(raw, str) and raw.strip().lower() == "annual"
    raise ValueError(
        f"row_matches_period: target must be 'quarterly' or 'annual', "
        f"got {target!r}"
    )


def _is_quarterly_vocab(row: Mapping) -> bool:
    """period in {quarter, quarterly} reject-first; else fiscal_period matches YYYY-QN.

    Invariant 10 enforcement (post-impl ISS-004): if `period` is explicitly set
    to a non-quarterly value (e.g. "annual"), reject the row up-front. ONLY
    fall back to the fiscal_period regex when `period` is missing/empty —
    otherwise an annual row whose fiscal_period happens to be "YYYY-QN" (e.g.
    fiscal-year-end Q4) would silently be treated as quarterly and aggregated
    into a TTM with period_kind="quarterly".
    """
    # Post-impl ISS-028 (cycle 5 LOW): row.get("period") returning None
    # was previously stringified to "none" by str(None).strip().lower(),
    # which fell into the `if period: return False` early-reject branch and
    # dropped rows that had a valid fiscal_period="YYYY-QN" but a null
    # period key. None-safe: treat None / missing identically.
    raw_period = row.get("period")
    period = "" if raw_period is None else str(raw_period).strip().lower()  # fail-open-ok: empty default never matches the quarterly set below (fail-CLOSED)
    if period in {"quarter", "quarterly"}:
        return True
    if period:  # explicit non-quarterly period → reject (ISS-004)
        return False
    fp = row.get("fiscal_period", "")  # fail-open-ok: empty default never matches _FISCAL_PERIOD_RE (fail-CLOSED)
    return isinstance(fp, str) and bool(_FISCAL_PERIOD_RE.match(fp))


def _build_period_map(
    rows: list[Mapping],
    counter: dict[str, dict[str, int]],
    *,
    statement_label: str,
    ticker: str,
) -> dict[str, Mapping]:
    """Step 1: filter to quarterly + has report_period + has fiscal_period.
    Tracks dropped-row counts PER STATEMENT FAMILY (cycle-5 fix: spec v3/v4
    used a flat counter shared across all 3 statements, which made the
    `(len(intersection_rps) + total_dropped) >= 4` heuristic for
    `missing_required_field` unsound — drops in one family don't
    contribute to the intersection size in the other two). v6 nests the
    counter as `{statement_label: {drop_reason: int}}` so the
    intersection-floor logic can pre-filter to drops within the load-bearing
    family. Raises on duplicate report_period within a single statement
    family (invariant 11)."""
    # Lazy bucket creation per cycle-7 propagation fix: only create the
    # `counter[statement_label]` sub-dict when this statement family
    # actually has a dropped row. Pre-creating empty buckets (v3 spec used
    # `setdefault`) leaked into `aligned_quarters.dropped_rows` as
    # `{"income": {...drops...}, "cash_flow": {}, "balance": {}}`, but the
    # contract is the nested dict surfaces ONLY load-bearing families.
    def _bump(reason: str) -> None:
        bucket = counter.setdefault(statement_label, {})
        bucket[reason] = bucket.get(reason, 0) + 1

    out: dict[str, Mapping] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            # fresh-loop2 ISS-031: surface non-Mapping rows in
            # dropped_rows so the `load_bearing` heuristic in
            # _classify_intersection_lt_4 can distinguish "all rows
            # were None/garbage" from "intersection truly < 4".
            # Pre-fix the silent continue lost that diagnostic.
            _bump("non_mapping_row")
            continue
        rp = row.get("report_period")
        # Post-impl ISS-011 (fresh-loop1): validate full calendar date, not
        # just the YYYY-MM-DD shape. "2024-99-99" / "2024-02-30" previously
        # passed regex and flowed downstream as valid report_period anchors.
        if not _is_valid_report_period(rp):
            _bump("missing_report_period")
            continue
        if not _is_quarterly_vocab(row):
            _bump("non_quarterly_period_kind")
            continue
        fp = row.get("fiscal_period")
        # fresh-loop2 cycle 3 C3A-MED-2: also reject whitespace-only
        # fiscal_period. Pre-fix `isinstance(fp, str) and fp` accepted
        # `"   "` (truthy string) → passed _build_period_map, later
        # failed `_FISCAL_PERIOD_RE.match("   ")` at _validate_window
        # → classified as `unparseable_fiscal_period` instead of
        # `missing_fiscal_period`. Site asymmetry with aligned_pair's
        # `fp.strip()` gate added in cycle 2 sub-loop.
        if not (isinstance(fp, str) and fp.strip()):
            _bump("missing_fiscal_period")
            continue
        if rp in out:
            raise InsufficientQuartersError(
                ticker=ticker, available=0,
                failure_kind="duplicate_report_period",
                detail=(f"{statement_label} has duplicate report_period={rp}; "
                        f"DL4 v1 fail-closes rather than guess canonical row. "
                        f"Investigate restatement / amended filing / annual-vs-quarterly mixing."),
            )
        # fresh-loop2 cycle 5 M3 (A-MED-2): snapshot caller row at
        # ingestion via MappingProxyType(dict(row)) so subsequent
        # caller mutation cannot drift the period map between
        # _build_period_map return and AlignedQuarter construction.
        # Pre-fix `out[rp] = row` stored the raw reference; with
        # concurrent producers or dynamic Mapping subclasses, the row
        # could change between filtering and aggregation. The
        # AlignedQuarter.__post_init__ snapshot at L120 only covered
        # the final construction step.
        out[rp] = MappingProxyType(dict(row))
    return out


def _validate_consecutive(quarters: list[tuple[int, int]]) -> Optional[str]:
    """Return None if 4 quarters are consecutive (with Q4→next-Y Q1 rollover),
    else a human-readable detail string."""
    for i in range(1, len(quarters)):
        py, pq = quarters[i - 1]
        cy, cq = quarters[i]
        expected = (py, pq + 1) if pq < 4 else (py + 1, 1)
        if (cy, cq) != expected:
            return f"{py}-Q{pq} → {cy}-Q{cq} (expected {expected[0]}-Q{expected[1]})"
    return None


def _build_aligned_quarter(
    *,
    rp: str,
    income_row: Mapping,
    cash_flow_row: Mapping,
    balance_row: Mapping,
    ticker: str,
) -> AlignedQuarter:
    """Apply invariant 9 metadata-equality check + build the dataclass."""
    # fiscal_period: all 3 must agree (when present)
    fps = {r.get("fiscal_period") for r in (income_row, cash_flow_row, balance_row)
           if isinstance(r.get("fiscal_period"), str)}
    if len(fps) != 1:
        raise InsufficientQuartersError(
            ticker=ticker, available=0,
            failure_kind="statement_metadata_mismatch",
            detail=(f"report_period={rp}: fiscal_period disagrees across "
                    f"statements: {sorted(fps)}"),
        )
    fp = next(iter(fps))

    # currency: all present must agree.
    # Post-impl ISS-032 (cycle 8 MEDIUM): normalize via strip().upper() before
    # comparison so "usd" / " USD " / "USD" canonicalize to "USD". Mirrors the
    # upstream gates' str(cur).strip().upper() canonicalization at the schema
    # layer.
    # Post-impl ISS-002 (fresh-loop1): fail-CLOSE when NO row carries a
    # currency, instead of falling back to "UNKNOWN" and letting downstream
    # paths that don't gate on statement_currency (extract_fcf,
    # historical_multiples) silently consume non-USD numbers as if they were
    # USD. Defense-in-depth at the schema boundary — caller's
    # `currency_validation.is_usd` gate is still authoritative, but the
    # schema layer no longer emits a sentinel that pretends to be a valid
    # metadata value.
    currencies = {str(r.get("currency")).strip().upper()
                  for r in (income_row, cash_flow_row, balance_row)
                  if isinstance(r.get("currency"), str) and r.get("currency").strip()}
    if len(currencies) > 1:
        raise InsufficientQuartersError(
            ticker=ticker, available=0,
            failure_kind="statement_metadata_mismatch",
            detail=(f"report_period={rp}: currency disagrees: {sorted(currencies)}"),
        )
    if not currencies:
        raise InsufficientQuartersError(
            ticker=ticker, available=0,
            failure_kind="statement_metadata_mismatch",
            detail=(f"report_period={rp}: no currency field present in any of "
                    f"income / cash_flow / balance rows — cannot establish "
                    f"USD invariant at schema layer"),
        )
    ccy = next(iter(currencies))

    return AlignedQuarter(
        report_period=rp,
        fiscal_period=fp,
        period_kind="quarterly",
        statement_currency=ccy,
        income_row=income_row,
        cash_flow_row=cash_flow_row,
        balance_row=balance_row,
        source_tag="[Calc: aligned_quarter_window from input rows]",  # invariant 6 — helper computes alignment, does not verify producer source
    )


def _validate_window_metadata(
    *,
    rp: str,
    income_row: Mapping,
    cash_flow_row: Mapping,
    balance_row: Mapping,
    ticker: str,
) -> None:
    """Invariant 9 metadata equality check WITHOUT dataclass construction.

    Cycle-5 fix: separates validation from construction so iter helper can
    distinguish unparseable_fiscal_period (yield SkippedWindow) from
    statement_metadata_mismatch (raise InsufficientQuartersError) without
    routing through AlignedQuarter.__post_init__ which raises SchemaError
    on either.

    Raises InsufficientQuartersError if fiscal_period or currency disagrees
    across statements at the same report_period.
    """
    fps = {r.get("fiscal_period") for r in (income_row, cash_flow_row, balance_row)
           if isinstance(r.get("fiscal_period"), str)}
    if len(fps) > 1:
        raise InsufficientQuartersError(
            ticker=ticker, available=0,
            failure_kind="statement_metadata_mismatch",
            detail=f"report_period={rp}: fiscal_period disagrees: {sorted(fps)}",
        )
    # Post-impl ISS-032: normalize via strip().upper() before set comparison
    # (parallel to _build_aligned_quarter canonicalization).
    # Post-impl ISS-002 (fresh-loop1): fail-CLOSE on zero currencies present
    # (parity with _build_aligned_quarter — both validation paths must reject
    # the all-missing-currency case at the schema layer).
    currencies = {str(r.get("currency")).strip().upper()
                  for r in (income_row, cash_flow_row, balance_row)
                  if isinstance(r.get("currency"), str) and r.get("currency").strip()}
    if len(currencies) > 1:
        raise InsufficientQuartersError(
            ticker=ticker, available=0,
            failure_kind="statement_metadata_mismatch",
            detail=f"report_period={rp}: currency disagrees: {sorted(currencies)}",
        )
    if not currencies:
        raise InsufficientQuartersError(
            ticker=ticker, available=0,
            failure_kind="statement_metadata_mismatch",
            detail=(f"report_period={rp}: no currency field present in any of "
                    f"income / cash_flow / balance rows — cannot establish "
                    f"USD invariant at schema layer"),
        )


def aligned_pair(
    income: list[Mapping],
    cash_flow: list[Mapping],
    *,
    ticker: str,
) -> Optional[tuple[Mapping, Mapping]]:
    """Return the latest aligned (income_row, cash_flow_row) pair, or None.

    Structural fix per loop-protocol §pattern-decay (post-impl ISS-062): SBC-
    ratio detection in scripts/adr/detect.py needs precise income↔cash_flow
    quarter pairing but does NOT consume balance_sheet data. The DL4 §3.2
    Fix F migration routed SBC through `aligned_quarters` (3-family
    intersection), which silently dropped the trigger for sparse-balance
    ADRs even when income + cash_flow were precisely paired. The
    over-coupling has been reflagged 4 times by zero-context Codex review
    (rounds 1 / 4 / 6 / 9) — exactly the "pattern-decay 3+ sites" trigger
    that protocol mandates be closed at the helper layer rather than via
    documentation.

    Contract:
    - Scan income[0..N] for the FIRST row (newest-first convention) whose
      report_period appears in cash_flow's report_period set.
    - Require fiscal_period agreement (when both rows carry it).
    - Require currency agreement (when both rows carry it; normalized
      via str(cur).strip().upper(), per ISS-032 canonicalization).
    - Returns (income_row, cash_flow_row) on success.
    - Returns None when no shared report_period exists.
    - Raises InsufficientQuartersError(failure_kind=
      "statement_metadata_mismatch") when fiscal_period or currency
      disagree at the matched report_period (parallel to
      _validate_window_metadata's invariant 9 enforcement).

    Use cases:
    - Signals that need exactly 1 latest aligned pair across exactly 2
      statement families (income + cash_flow). SBC/Revenue is the
      canonical example.
    - For 4-quarter TTM aggregation use `aligned_quarters` (3-family
      intersection). For sliding YoY use `iter_aligned_quarter_windows`.

    NOT a general-purpose helper — limited to the 2-family pair case.
    `ticker` is kw-only (Pattern Z.3 lock).
    """
    if not income or not cash_flow:
        return None
    # Post-impl ISS-022 (fresh-loop2): drop non-quarterly rows before pairing.
    # Pre-fix `aligned_pair` filtered on `isinstance(r, Mapping)` only, so an
    # `income_row = {"period": "annual", "report_period": "2024-12-31", ...}`
    # paired with the same-rp annual cash_flow row would return as a
    # "quarterly pair" — directly bypassing the helper's stated 2-family
    # quarter-pairing contract and the parity invariant with
    # `_is_quarterly_vocab` / `row_matches_period` / `AlignedQuarter.__post_init__`.
    # Also drop rows whose report_period isn't a valid calendar date (ISS-011 parity).
    income_dicts = [
        r for r in income
        if isinstance(r, Mapping)
        and _is_valid_report_period(r.get("report_period"))
        and _is_quarterly_vocab(r)
    ]
    cf_dicts = [
        r for r in cash_flow
        if isinstance(r, Mapping)
        and _is_valid_report_period(r.get("report_period"))
        and _is_quarterly_vocab(r)
    ]
    if not income_dicts or not cf_dicts:
        return None

    # Post-impl ISS-001 (fresh-loop1): parity with _build_period_map's
    # invariant 11 — duplicate report_period within ONE statement family is a
    # data-integrity defect, not a "pick the first" affordance. The 3-family
    # aligned_quarters path raises InsufficientQuartersError; aligned_pair
    # must follow the same contract or restatements / amended filings
    # silently route to whichever row happens to be first in newest-first
    # ordering. Check BOTH income and cash_flow.
    def _check_no_duplicate_rp(rows: list, label: str) -> None:
        seen: set[str] = set()
        for r in rows:
            rp = r.get("report_period")
            if not isinstance(rp, str) or not rp:
                continue
            if rp in seen:
                raise InsufficientQuartersError(
                    ticker=ticker, available=0,
                    failure_kind="duplicate_report_period",
                    detail=(f"aligned_pair: {label} has duplicate "
                            f"report_period={rp}; DL4 fail-closes rather than "
                            f"guess canonical row. Investigate restatement / "
                            f"amended filing / annual-vs-quarterly mixing."),
                )
            seen.add(rp)
    _check_no_duplicate_rp(income_dicts, "income")
    _check_no_duplicate_rp(cf_dicts, "cash_flow")

    cf_by_rp: dict[str, Mapping] = {}
    for cf in cf_dicts:
        rp = cf.get("report_period")
        if isinstance(rp, str) and rp:
            cf_by_rp[rp] = cf  # duplicates already rejected above

    # fresh-loop2-cycle2 C2A-MED-1: enforce "latest" semantics inside the
    # helper. Docstring promised "latest aligned pair" but the pre-fix
    # implementation iterated income_dicts in caller order ("newest-first
    # convention") with no internal sort. A caller passing oldest-first
    # data would silently return the OLDEST pair. Sort by report_period
    # newest-first inside the helper so the contract holds regardless of
    # caller ordering. Date strings are YYYY-MM-DD → lexicographic sort
    # == chronological sort (per ISS-029 verification).
    income_dicts_newest_first = sorted(
        income_dicts,
        key=lambda r: r.get("report_period") or "",
        reverse=True,
    )

    for inc in income_dicts_newest_first:
        rp = inc.get("report_period")
        if not isinstance(rp, str) or not rp:
            continue
        cf = cf_by_rp.get(rp)
        if cf is None:
            continue

        # fresh-loop2-cycle2 C2A-HIGH-1: parity-strict metadata gate.
        # Pre-fix aligned_pair checked fp/currency AGREEMENT only when both
        # rows carried them — but a row that lacked fiscal_period entirely
        # would have been DROPPED by `_build_period_map` in the 3-family
        # aligned_quarters path (L435-438 requires `fp` non-empty). Allowing
        # aligned_pair to emit such rows is an asymmetric safety relaxation
        # in a 2-family path that flows into USD-sensitive ADR detection.
        # Require both rows to carry non-empty fiscal_period; require
        # currency presence on at least one row + agreement when present
        # on both.
        inc_fp = inc.get("fiscal_period")
        cf_fp = cf.get("fiscal_period")
        # fresh-loop2-cycle2 sub-loop R-MED2: also reject whitespace-only
        # fiscal_period for parity with the statement_currency
        # `.strip()` check at AlignedQuarter.__post_init__. Pre-fix
        # `inc_fp = "   "` was truthy → passed the `and inc_fp` guard.
        if not (isinstance(inc_fp, str) and inc_fp.strip()
                and isinstance(cf_fp, str) and cf_fp.strip()):
            raise InsufficientQuartersError(
                ticker=ticker, available=0,
                failure_kind="missing_required_field",
                detail=(
                    f"aligned_pair: report_period={rp} requires "
                    f"non-empty fiscal_period (whitespace rejected) on "
                    f"BOTH rows (income={inc_fp!r} cf={cf_fp!r}); parity "
                    f"with _build_period_map's 3-family fail-close."
                ),
            )
        if inc_fp != cf_fp:
            raise InsufficientQuartersError(
                ticker=ticker, available=0,
                failure_kind="statement_metadata_mismatch",
                detail=(f"aligned_pair: report_period={rp} fiscal_period "
                        f"disagrees: income={inc_fp!r} cf={cf_fp!r}"),
            )
        # Currency: require at least one row to carry it; agree when both.
        inc_cur = inc.get("currency")
        cf_cur = cf.get("currency")
        inc_has_cur = isinstance(inc_cur, str) and inc_cur.strip()
        cf_has_cur = isinstance(cf_cur, str) and cf_cur.strip()
        if not (inc_has_cur or cf_has_cur):
            raise InsufficientQuartersError(
                ticker=ticker, available=0,
                failure_kind="missing_required_field",
                detail=(
                    f"aligned_pair: report_period={rp} requires currency on "
                    f"at least one of (income, cash_flow); USD-sensitivity "
                    f"in ADR detection depends on it."
                ),
            )
        if inc_has_cur and cf_has_cur:
            if inc_cur.strip().upper() != cf_cur.strip().upper():
                raise InsufficientQuartersError(
                    ticker=ticker, available=0,
                    failure_kind="statement_metadata_mismatch",
                    detail=(f"aligned_pair: report_period={rp} currency "
                            f"disagrees: income={inc_cur!r} cf={cf_cur!r}"),
                )
        return (inc, cf)

    return None  # no shared report_period


def aligned_quarters(
    income: list[Mapping],
    cash_flow: list[Mapping],
    balance: list[Mapping],
    *,
    ticker: str,
) -> AlignedQuarterWindow:
    """Return the 4 most-recent consecutive fiscal quarters present in all 3
    statements, oldest-first. Raises InsufficientQuartersError on any failure.

    **Cycle-2 fix — invariant 3 hard-gate:** v2 delegated to
    iter_aligned_quarter_windows and took the LAST yielded window. That was
    wrong: when intersection > 4 AND the latest 4 candidates are
    non-consecutive but an EARLIER sliding window is valid, the iterator
    would yield the older valid window and `windows[-1]` would silently
    return a stale "latest" — directly violating invariant 3. v3 implements
    aligned_quarters directly with strict trailing-4 semantics: it inspects
    ONLY the last 4 entries of the intersection and raises on any failure
    there, regardless of what older windows look like.

    See iter_aligned_quarter_windows for the sliding-window flavor consumed
    by historical_multiples (which DOES want to surface older valid windows).
    """
    dropped: dict[str, dict[str, int]] = {}  # v8 propagation fix per cycle-7: nested per-statement-label
    income_map = _build_period_map(income, dropped,
                                    statement_label="income", ticker=ticker)
    cash_flow_map = _build_period_map(cash_flow, dropped,
                                       statement_label="cash_flow", ticker=ticker)
    balance_map = _build_period_map(balance, dropped,
                                     statement_label="balance", ticker=ticker)
    intersection_rps = sorted(
        set(income_map) & set(cash_flow_map) & set(balance_map)
    )

    if len(intersection_rps) < 4:
        # Per-statement load-bearing check (cycle-5 fix HIGH-12):
        # missing_required_field iff ANY single statement family has
        # enough drops that, hypothetically restoring them, would have
        # pushed the intersection ≥ 4. Cross-statement total is wrong
        # because drops in one family don't change the other two maps.
        load_bearing = False
        for label, by_reason in dropped.items():
            # fresh-loop2 cycle-1 verification fix (MISSED-1): exclude
            # `non_mapping_row` from family_drops in the load_bearing
            # computation. A non-Mapping row (None / str / int) is a
            # degenerate provider output, not a "row present but missing
            # field" — the load_bearing heuristic was designed for
            # missing_fiscal_period / non_quarterly_period_kind /
            # missing_report_period, all of which are valid Mappings
            # lacking expected fields. Counting non-Mapping rows would
            # silently flip failure_kind from `intersection_lt_4` to
            # `missing_required_field` when a feed shipped None rows
            # — behavior change vs pre-cycle-1, and incorrectly
            # implies a recoverable data-quality issue.
            family_drops = sum(
                count for reason, count in by_reason.items()
                if reason != "non_mapping_row"
            )
            family_present = (
                len(income_map) if label == "income"
                else len(cash_flow_map) if label == "cash_flow"
                else len(balance_map)
            )
            # Pre-drop size of THIS family would have been family_present
            # + family_drops. If that >= 4 AND family is currently <4 in
            # the maps (drops are what made the family small), the
            # family is load-bearing.
            if family_drops > 0 and (family_present + family_drops) >= 4 and family_present < 4:
                load_bearing = True
                break
        kind: FailureKind = (
            "missing_required_field" if load_bearing else "intersection_lt_4"
        )
        raise InsufficientQuartersError(
            ticker=ticker,
            available=len(intersection_rps),
            failure_kind=kind,
            detail=f"intersection has {len(intersection_rps)} quarters; need 4",
            dropped_rows=dropped,  # now a nested {statement: {reason: count}}
        )

    # Strict trailing-4: the LATEST window MUST satisfy invariant 3, no
    # fallback to earlier windows. (Cycle-2 fix HIGH-1.)
    slice_rps = intersection_rps[-4:]

    # Parse fiscal_period for the 4 selected quarters
    quarters: list[tuple[int, int]] = []
    for rp in slice_rps:
        fp = income_map[rp].get("fiscal_period", "")  # fail-open-ok: empty default → unparseable_fiscal_period raise below (fail-CLOSED)
        m = _FISCAL_PERIOD_RE.match(fp) if isinstance(fp, str) else None
        if not m:
            raise InsufficientQuartersError(
                ticker=ticker, available=len(intersection_rps),
                failure_kind="unparseable_fiscal_period",
                detail=f"window {slice_rps} has unparseable fiscal_period",
                dropped_rows=dropped,
            )
        quarters.append((int(m.group(1)), int(m.group(2))))

    # Continuity check
    gap_detail = _validate_consecutive(quarters)
    if gap_detail is not None:
        raise InsufficientQuartersError(
            ticker=ticker, available=len(intersection_rps),
            failure_kind="non_consecutive",
            detail=f"trailing-4 window {slice_rps}: {gap_detail}",
            dropped_rows=dropped,
        )

    # Build the 4 AlignedQuarter instances (applies invariant 9; raises on mismatch)
    window: list[AlignedQuarter] = []
    for rp in slice_rps:
        window.append(_build_aligned_quarter(
            rp=rp,
            income_row=income_map[rp],
            cash_flow_row=cash_flow_map[rp],
            balance_row=balance_map[rp],
            ticker=ticker,
        ))
    # Cycle-8 fix (Fix C — promoted from §11 backlog #7): runtime invariant
    # guard that AlignedQuarterWindow is exactly 4 elements. Uses
    # `if/raise` NOT `assert` per superpower reviewer feedback (assert is
    # disabled under python -O, defeating the invariant in optimized
    # deployments).
    if len(window) != 4:
        raise InsufficientQuartersError(
            ticker=ticker, available=len(window),
            failure_kind="intersection_lt_4",
            detail=f"AlignedQuarterWindow length invariant violated: "
                   f"got {len(window)}, expected 4 — implementation bug",
        )
    return tuple(window)  # type: ignore[misc]


WindowYield = tuple[Optional[AlignedQuarterWindow], Optional[SkippedWindow]]


def iter_aligned_quarter_windows(
    income: list[Mapping],
    cash_flow: list[Mapping],
    balance: list[Mapping],
    *,
    ticker: str,
) -> Iterator[WindowYield]:
    """Yield (window, skip) pairs for each candidate 4-quarter slide in the
    intersection, oldest-first. Exactly one of `window` or `skip` is non-None
    per yield.

    **Cycle-2 fix — yield-with-skip API:** spec v2 had iter yield only
    valid windows and reference a phantom `_diagnose_skipped_windows()`
    helper to recover the skipped ones in a second pass. v3 yields skip
    diagnostics inline so consumers see them without re-running the
    algorithm. Eliminates the "One implementation, not two" anti-pattern
    flagged by Codex.

    Consumer pattern (historical_multiples):

        valid_windows: list[AlignedQuarterWindow] = []
        skipped_windows: list[SkippedWindow] = []
        for window, skip in iter_aligned_quarter_windows(income, cash_flow,
                                                          balance, ticker=t):
            if window is not None:
                valid_windows.append(window)
            else:
                skipped_windows.append(skip)

    Behavior:
      - With 4 aligned quarters: yields 1 entry. If continuity passes →
        (window, None). Else → (None, SkippedWindow).
      - With 5 aligned quarters: yields 2 entries.
      - With 8 aligned quarters: yields up to 5 entries.
      - If the FULL intersection has <4 quarters: yields ONE
        `(None, SkippedWindow(failure_kind="intersection_lt_4"))` with
        `anchor_report_period = max(union of all 3 family rps)` or the
        `"1970-01-01"` sentinel when all 3 maps are empty (sentinel is a
        valid ISO date accepted by `_is_valid_report_period` /
        `SkippedWindow.__post_init__`). Consumers can populate
        `skipped_windows` uniformly via the yielded entry. Consumers MAY
        still call aligned_quarters() first for a hard `InsufficientQuartersError`
        gate (raises on the same condition).
      - duplicate_report_period (invariant 11) and statement_metadata_mismatch
        (invariant 9) ALWAYS raise — these are data-integrity defects,
        not "no window here". The iterator raises eagerly at first detection.
      - unparseable_fiscal_period within a slide candidate → SkippedWindow
        yield (not raise). aligned_quarters() raises on this; iter does not,
        because a sliding-window scan may legitimately encounter partial
        provider data on older periods.

    Algorithm:
      1. Build 3 maps via _build_period_map (raises on duplicate_report_period
         per invariant 11; raises on metadata-equality via downstream
         _build_aligned_quarter per invariant 9).
      2. intersection_rps = sorted(set(...))
      3. If len < 4: yield nothing, return.
      4. For each i in 0..len-4 (inclusive):
         a. slice_rps = intersection_rps[i:i+4]
         b. Parse fiscal_period; on failure yield (None, SkippedWindow(unparseable)).
         c. _validate_consecutive; on failure yield (None, SkippedWindow(non_consec)).
         d. _build_aligned_quarter for each rp (raises on metadata mismatch).
         e. yield (window, None).
    """
    dropped: dict[str, dict[str, int]] = {}  # v8 propagation fix per cycle-7: nested per-statement-label
    income_map = _build_period_map(income, dropped,
                                    statement_label="income", ticker=ticker)
    cash_flow_map = _build_period_map(cash_flow, dropped,
                                       statement_label="cash_flow", ticker=ticker)
    balance_map = _build_period_map(balance, dropped,
                                     statement_label="balance", ticker=ticker)

    intersection_rps = sorted(set(income_map) & set(cash_flow_map) & set(balance_map))
    if len(intersection_rps) < 4:
        # Post-impl ISS-033 (fresh-loop3): emit a single diagnostic
        # SkippedWindow so consumers can distinguish "no candidates at
        # all" from "candidates existed but were all skipped". Pre-fix
        # an empty iterator was indistinguishable from "no data, no
        # alignment attempted". FailureKind already includes
        # `intersection_lt_4` (used by aligned_quarters' hard-raise
        # path); the iter path now surfaces it as a soft yield so
        # historical_multiples can populate skipped_windows uniformly.
        # Anchor at the newest available report_period across the 3
        # families (or "1970-01-01" if everything is empty — _is_valid_
        # report_period accepts it and SkippedWindow.__post_init__
        # validates).
        all_rps = (set(income_map) | set(cash_flow_map) | set(balance_map))
        anchor_rp = max(all_rps) if all_rps else "1970-01-01"
        yield (None, SkippedWindow(
            anchor_report_period=anchor_rp,
            failure_kind="intersection_lt_4",
            detail=(
                f"intersection of income+cash_flow+balance report_periods "
                f"has {len(intersection_rps)} entries (need 4); per-family "
                f"sizes: income={len(income_map)} cash_flow="
                f"{len(cash_flow_map)} balance={len(balance_map)}"
            ),
        ))
        return

    for i in range(len(intersection_rps) - 3):
        slice_rps = intersection_rps[i:i + 4]
        anchor_rp = slice_rps[-1]

        # 4b: parse fiscal_period from RAW income_map rows (NO dataclass
        # construction yet — _validate_window_metadata + AlignedQuarter's
        # __post_init__ enforce the format strictly; we need to detect
        # parse-failure here as a "skip" outcome, not a SchemaError raise).
        # Cycle-5 fix: v5 placed _build_aligned_quarter first; but
        # AlignedQuarter.__post_init__ validates fiscal_period regex strictly,
        # so unparseable fiscal_period raised SchemaError BEFORE the
        # iter could yield SkippedWindow(unparseable_fiscal_period).
        # v6 separates validation from construction.
        quarters: list[tuple[int, int]] = []
        unparseable = False
        for rp in slice_rps:
            fp_raw = income_map[rp].get("fiscal_period", "")  # fail-open-ok: empty default → unparseable flag set below (fail-CLOSED, yields SkippedWindow)
            m = _FISCAL_PERIOD_RE.match(fp_raw) if isinstance(fp_raw, str) else None
            if not m:
                unparseable = True
                break
            quarters.append((int(m.group(1)), int(m.group(2))))
        if unparseable:
            yield (None, SkippedWindow(
                anchor_report_period=anchor_rp,
                failure_kind="unparseable_fiscal_period",
                detail=f"window {slice_rps} has unparseable fiscal_period",
            ))
            continue

        # 4c: cross-statement metadata equality (invariant 9). Raises
        # eagerly on mismatch regardless of continuity outcome.
        # _validate_window_metadata is a new helper (NO dataclass construction):
        # for each rp, compare fiscal_period / currency across the 3 rows.
        # Mismatch → raise InsufficientQuartersError(statement_metadata_mismatch).
        for rp in slice_rps:
            _validate_window_metadata(
                rp=rp,
                income_row=income_map[rp],
                cash_flow_row=cash_flow_map[rp],
                balance_row=balance_map[rp],
                ticker=ticker,
            )

        # 4d: continuity
        gap_detail = _validate_consecutive(quarters)
        if gap_detail is not None:
            yield (None, SkippedWindow(
                anchor_report_period=anchor_rp,
                failure_kind="non_consecutive",
                detail=f"window {slice_rps}: {gap_detail}",
            ))
            continue

        # 4e: ALL validation passed → safe to construct AlignedQuarter.
        # __post_init__ checks here are belt-and-suspenders; they'll pass
        # because steps 4b + 4c verified the inputs already.
        window: list[AlignedQuarter] = [
            _build_aligned_quarter(
                rp=rp,
                income_row=income_map[rp],
                cash_flow_row=cash_flow_map[rp],
                balance_row=balance_map[rp],
                ticker=ticker,
            )
            for rp in slice_rps
        ]
        yield (tuple(window), None)  # type: ignore[misc]
