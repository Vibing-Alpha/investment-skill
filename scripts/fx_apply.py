"""Shared FX application helper for the 3 currency-gated consumers.

Per-consumer field set is passed in (§3.1.1 subsets). The helper handles:
  - Step 0: YTD-basis pre-check (invariant 19, cheapest gate first)
  - Step 1: Cross-quarter currency uniformity check (invariant 13)
  - Step 2: Non-finite pre-scan with extract_fcf.free_cash_flow carve-out
            (invariant 6; codex cycle-1 + agent feedback)
  - Step 3: FX window fetch + outlier check + typed validation
  - Step 4: In-place row mutation via explicit named-local two-step
            (B4 producer convention)
  - Returns: (ok, window_serialized, fx_failure_reason)
"""

from __future__ import annotations

from scripts.sources.fx_rates import (
    get_fx_window,
    SUPPORTED_FX_CURRENCIES,
    detect_outlier_rates,            # cycle-23 F-23-1 #2: promoted from private
)
from scripts.sources.adapter_result import AdapterResult, ErrorCode  # cycle-23: for _map_fx_fetch_error
from scripts.schemas.fx_window import load_fx_window, FxWindow
from scripts.schemas.errors import SchemaError                       # cycle-23 F-23-1 #4
from scripts.fx_constants import FX_TWO_PATH_CARVE_OUT
# cycle-20 agent extra-finding: _is_finite_number lives in scripts.extract_fcf:18.
# Cross-module import here is intentional (cheaper than relocating to a 3rd
# shared module). If a future cycle adds another consumer of this helper,
# consider relocating to scripts/fx_constants.py or new scripts/shared_math.py.
from scripts.extract_fcf import _is_finite_number
# `FX_TWO_PATH_CARVE_OUT` is defined ONCE in `scripts/fx_constants.py` per
# §3.6.1. The earlier draft duplicated the literal here — codex cycle-3 #2
# flagged this as hygiene. Import only; no local re-definition.


def apply_fx_conversion(
    *,
    income_statements: list[dict],
    cash_flows: list[dict],
    balance_sheets: list[dict],
    detected_currency: str,
    consumer_name: str,                            # "extract_fcf" | "historical_multiples" | "adr_correct" | "adr_eps_check"
    consumer_fields: dict[str, tuple[str, ...]],  # {family_name: (field_names,)}
    ticker: str,
) -> tuple[bool, list[dict] | None, str | None, list[str]]:
    """Returns (ok, window_dict_list, failure_reason, warnings).

    Side effect on failure: leaves statement rows unchanged (no partial mutation).
    Side effect on success: rewrites consumer_fields in place on EVERY row
      whose report_period has FX coverage (cycle-8 F1). Tags row.currency =
      'USD' and stores row._pre_conversion_currency = detected_currency on
      every converted row. Rows whose period is uncovered cause fail-close.

    Ordering (cycle-8 + cycle-15 + cycle-16 revised; F1/F9/F-15-1/F-15-8 applied):
      1. Universe collection (Step 1) — gather all report_periods across 3 families
      2. YTD-basis gate (Step 0) — scan FULL universe (cycle-15 F-15-8 broadened)
      3. YTD absent-basis warning (Step 0') — populate warnings list (no fail)
      4. Cross-currency gate (Step 2) — scan all 3 families × FULL universe (cycle-15 F-15-1 broadened from cash_flows-only-trailing-4)
      5. Non-finite pre-scan (Step 3) — field × quarter sweep over trailing 4 (consumer-relevant TTM heuristic; can be expanded if a future consumer reads older periods)
      6. FX fetch + outlier + load (Steps 4-6) — only if Steps 0-3 pass.
         Fetch covers FULL universe (was: trailing 4)
      7. Mutation (Step 7) — over EVERY universe row with FX coverage. Any
         uncovered period → fail-close (cycle-8 F1 silent-corruption fix)
    """

    # === Step 1 (formerly "Step 0+1"): collect UNIVERSE of report_periods
    # across the 3 statement families (cycle-8 F1 — period-selection
    # silent-corruption fix). The earlier "last_4_periods" picker was
    # wrong: historical_multiples uses up to 8 aligned windows,
    # extract_fcf trailing-null skip selects different rows,
    # adr/correct uses prior_window for EPS YoY. Converting only the
    # last 4 left older rows in local currency but tagged "USD",
    # silently corrupting any consumer that read them. The universe
    # approach converts EVERY row whose period has FX coverage —
    # consumers cannot ever read a row mislabeled USD with local value.
    # Pre-declare warnings list so every early-return can include it (cycle-9 4-tuple).
    warnings_emitted: list[str] = []

    all_periods: set[str] = set()
    for fam in (income_statements, cash_flows, balance_sheets):
        for r in fam:
            rp = r.get("report_period")
            if isinstance(rp, str) and rp:
                all_periods.add(rp)
    all_periods_sorted = sorted(all_periods)
    if not all_periods_sorted:
        return True, None, None, warnings_emitted  # let downstream "no quarters" gate fire
    last_4_universe = set(all_periods_sorted[-4:])  # used by Step 0/2 heuristics

    # === Step 0: YTD-basis pre-check (invariant 19). Scans the FULL
    # universe (cycle-15 F-15-8 broadened from trailing-4). Reason:
    # historical_multiples reads up to 8 TTM windows, adr/correct reads
    # prior_window for EPS growth — YTD-contaminated row at older
    # universe period silently corrupts those reads. Parallel to
    # cycle-9's outlier-check broadening (invariant 11). ===
    for fam_rows in (income_statements, cash_flows, balance_sheets):
        for r in fam_rows:
            rp = r.get("report_period")
            if not isinstance(rp, str) or rp not in all_periods_sorted:
                continue  # exclude rows without period or out-of-universe
            # post-impl loop-3 F4 fix: normalize before compare. Pre-fix
            # strict-equal `== "ytd"` missed `"YTD"` / `"Ytd"` / `" ytd "`
            # — a producer drifting case (currently FD API emits
            # lowercase, but defense-in-depth matches the parity of
            # `_any_explicit_non_usd` cycle-4 fix and `_is_usd_constant`
            # Pattern W AST helper normalization).
            basis_val = r.get("period_value_basis")
            if isinstance(basis_val, str) and basis_val.strip().lower() == "ytd":
                return False, None, "fx_ytd_basis_unsupported", warnings_emitted

    # === Step 0': YTD absent-basis warning (cycle-8 F9 + cycle-15 F-15-8
    # broadened scope). When producer omits period_value_basis entirely,
    # YTD contamination can't be ruled out across the consumer-relevant
    # read range (8-window TTM + prior_window). Emit warning but don't fail
    # (default fail-close would break every current non-USD ADR — no
    # producer ships this attestation today). One universal warning is
    # sufficient (DL3e adds per-family diagnostics). ===
    for fam_rows in (income_statements, cash_flows, balance_sheets):
        family_has_unattested = False
        for r in fam_rows:
            rp = r.get("report_period")
            if not isinstance(rp, str) or rp not in all_periods_sorted:
                continue
            if "period_value_basis" not in r:
                family_has_unattested = True
                break
        if family_has_unattested:
            # cycle-10 #2 fix: emit closed-vocab keyword (FX_WARNING_REASONS
            # member), not a full message. The closed-vocab value passes
            # validate_fx_warning_reason(). Consumer's summary layer maps
            # the keyword to display text.
            if "fx_basis_unattested" not in warnings_emitted:
                warnings_emitted.append("fx_basis_unattested")
            break  # one universal warning is enough

    # === Step 2: Cross-family + cross-quarter currency uniformity over the
    # FULL universe (cycle-15 F-15-1 HIGH fix from cash_flows-only +
    # trailing-4). Catches mixed-family currency (income=EUR, cash_flows=JPY)
    # AND mixed-quarter currency (Q3=JPY, Q2=USD on same family). Single-
    # family scan in earlier draft silently picked one detected_currency
    # and converted other family's rows with wrong FX. ===
    seen: set[str] = set()
    for fam in (income_statements, cash_flows, balance_sheets):
        for r in fam:
            rp = r.get("report_period")
            if not isinstance(rp, str) or rp not in all_periods_sorted:
                continue
            cur = r.get("currency")
            if isinstance(cur, str) and cur.strip():
                seen.add(cur.strip().upper())
    if len(seen) > 1:
        return False, None, "fx_mixed_currency_window", warnings_emitted

    # === Step 2.5: Row-level explicit-currency requirement for money-
    # mutating families (post-impl loop-3 cycle-2 HIGH defense). Pre-fix
    # Step 2 above only validated that EXPLICIT currency tags were
    # uniform — rows with `currency=None` or `currency=""` were silently
    # tolerated. Step 7 below then mutated those rows' consumed-field
    # values by the detected rate AND retagged them USD. If a
    # missing-tag row's value was actually already USD (mixed-source
    # dataset case), it would be re-multiplied by the JPY/EUR/etc. rate
    # — silent corruption.
    #
    # For money-mutating families (consumer_fields[fam] is non-empty
    # tuple), require every row that carries a finite consumed-field
    # value to also carry an explicit currency tag matching
    # detected_currency. Alignment-only families (consumer_fields[fam]
    # is empty tuple) are tolerated as before (the H1 loop-1 fix retags
    # them USD without multiplying any field).
    detected_norm = detected_currency.strip().upper() if isinstance(
        detected_currency, str
    ) else None
    for fam_name, fam_rows in (("income_statements", income_statements),
                                 ("cash_flows", cash_flows),
                                 ("balance_sheets", balance_sheets)):
        fields_to_convert = consumer_fields.get(fam_name, ())
        if not fields_to_convert:
            continue  # alignment-only family — missing tag tolerated
        for r in fam_rows:
            rp = r.get("report_period")
            if not isinstance(rp, str) or rp not in all_periods_sorted:
                continue
            # Row participates in mutation only if at least one
            # consumed-field is finite (matches Step 7 mutation check).
            has_value = any(
                _is_finite_number(r.get(fld)) for fld in fields_to_convert
            )
            if not has_value:
                continue
            cur = r.get("currency")
            if not (isinstance(cur, str) and cur.strip()):
                return (
                    False, None, "fx_mixed_currency_window",
                    warnings_emitted,
                )
            if cur.strip().upper() != detected_norm:
                return (
                    False, None, "fx_mixed_currency_window",
                    warnings_emitted,
                )

    # === Step 3: Non-finite pre-scan with consumer-specific carve-out
    # (invariant 6). Scans the trailing 4 universe periods. ===
    missing: list[str] = []
    for fam_name, fam_rows in (("income_statements", income_statements),
                                 ("cash_flows", cash_flows),
                                 ("balance_sheets", balance_sheets)):
        for fld in consumer_fields.get(fam_name, ()):
            if (consumer_name, fam_name, fld) in FX_TWO_PATH_CARVE_OUT:
                continue  # extract_fcf two-path machine handles this
            for r in fam_rows:
                if r.get("report_period") not in last_4_universe:
                    continue
                v = r.get(fld)
                if v is None:
                    continue  # per-consumer policy
                if not _is_finite_number(v):
                    missing.append(f"{fam_name}.{fld}@{r['report_period']}={v!r}")
    if missing:
        return False, None, "fx_consumer_field_missing", warnings_emitted

    # === Step 4: Fetch FX window for the FULL universe (cycle-8 F1 —
    # was last_4_periods; now all_periods_sorted) ===
    fx_result = get_fx_window(detected_currency, all_periods_sorted, ticker=ticker)
    if not fx_result.ok:
        return False, None, _map_fx_fetch_error(fx_result), warnings_emitted
    # AdapterResult invariant: data MUST be dict; list-returning adapters
    # wrap as {"items": [...]} (adapter_result.py L422). Unwrap here per
    # canonical convention. Spec L148/L812-813/L831 narrative reads as
    # flat list — that text describes the LOGICAL row stream, not the
    # AdapterResult envelope. The wrapper is unconditional.
    fx_rows = fx_result.data["items"]
    rate_by_period = {r["date"]: r["fx_rate_usd_per_local"]
                      for r in fx_rows}

    # === Step 5: Outlier check spans the FULL universe (cycle-9 #1 fix
    # from trailing-4 only). Since Step 7 mutates EVERY universe row,
    # an outlier rate at ANY period contaminates whichever 8-window /
    # prior-window TTM consumer reads that period. Median-based detection
    # tolerates the long history naturally (a single 10× outlier among
    # 30 rates is still detectable; trailing-4-only missed it). ===
    universe_rates = [rate_by_period[p] for p in all_periods_sorted
                      if p in rate_by_period]
    if len(universe_rates) >= 4 and detect_outlier_rates(universe_rates):  # cycle-23 F-23-1 #2: public name
        return False, None, "fx_rate_outlier", warnings_emitted

    # === Step 6: Typed-load validation (invariant 16) ===
    try:
        validated = load_fx_window({
            "currency": detected_currency,
            "source": f"yfinance:{detected_currency}=X",
            "rows": fx_rows,
        })
    except SchemaError:
        return False, None, "fx_source_unavailable", warnings_emitted

    # === Step 6.5: Coverage preflight (cycle-12 F-12-2). Ensure ALL
    # universe periods have FX coverage BEFORE any mutation. Otherwise
    # a late uncovered period would cause partial mutation of earlier
    # rows, breaking the all-or-nothing invariant 6. ===
    missing_coverage = set(all_periods_sorted) - rate_by_period.keys()
    if missing_coverage:
        return False, None, "fx_history_insufficient", warnings_emitted

    # === Step 7: MUTATION over EVERY row (cycle-8 F1). The Step 6.5
    # preflight guarantees every universe period has FX coverage, so
    # the per-row `if rate is None` check below is now a paranoia
    # defensive guard (unreachable in single-threaded Python; kept for
    # robustness against future racy mutations). Producer convention:
    # explicit local_val/usd_val named locals so Pattern AC AST walker
    # sees the transition. ===
    for fam_name, fam_rows in (("income_statements", income_statements),
                                 ("cash_flows", cash_flows),
                                 ("balance_sheets", balance_sheets)):
        for r in fam_rows:
            rp = r.get("report_period")
            if not isinstance(rp, str) or not rp:
                continue  # rows without period are not converted but also not used
            rate = rate_by_period.get(rp)
            if rate is None:
                # period has no FX bar in our window — cannot safely tag USD.
                # Fail-close the entire artifact (consumer-side TTM windows
                # may read this row; partial conversion = silent corruption).
                return False, None, "fx_history_insufficient", warnings_emitted
            for fld in consumer_fields.get(fam_name, ()):
                # cycle-12 F-12-1 HIGH fix: NO carve-out check here.
                # The carve-out applies ONLY to Step 3 pre-scan (allowing
                # non-finite values to pass through extract_fcf's two-path
                # state machine downstream). MUTATION must always convert
                # finite values to USD — leaving them in local while
                # tagging row.currency="USD" is silent corruption.
                # Non-finite values are protected by the `_is_finite_number`
                # guard below (left alone; extract_fcf's two-path handles).
                local_val = r.get(fld)
                if not _is_finite_number(local_val):
                    continue  # None/NaN/Inf — leave for downstream handling
                usd_val = local_val * rate  # fx-conversion-ok: cert in caller
                r[fld] = usd_val
        # Re-tag currency on rows of this family that had FX coverage —
        # iff the caller declared this family in consumer_fields (key-presence
        # test, NOT truthy). Two semantics:
        #   - family declared with money-field tuple → convert those fields
        #     + retag currency="USD" + record _pre_conversion_currency
        #   - family declared with empty tuple () → "alignment-only": no
        #     money mutation, but DO retag currency so downstream
        #     `_build_aligned_quarter` (invariant 9 cross-row currency check)
        #     does not see a stale local-currency tag after FX success.
        #   - family absent from consumer_fields → caller doesn't pass this
        #     family to downstream consumers (or downstream tolerates mixed
        #     currency). Leave the family unchanged.
        # post-impl loop-1 H1 fix: cycle-15 F-15-2 narrowed retag to truthy
        # which left adr_eps_check + extract_fcf's balance_sheets stuck with
        # local-currency tag after FX, breaking aligned_quarters with
        # statement_metadata_mismatch on every non-USD ADR — defeating the
        # whole DL3c value proposition.
        if fam_name not in consumer_fields:
            continue  # caller didn't declare this family — leave untouched
        for r in fam_rows:
            rp = r.get("report_period")
            if isinstance(rp, str) and rp in rate_by_period:
                r["currency"] = "USD"  # fail-open-ok: post-fx-conversion retag (DL3c §3.3.3 Step 7)
                r["_pre_conversion_currency"] = detected_currency

    # 4-tuple return: warnings_emitted (from Step 0') goes to caller for
    # surfacing in result["warnings"]. Cycle-9 fix: helper now consistently
    # returns 4 values on EVERY path.
    return True, _serialize_window(validated, detected_currency), None, warnings_emitted


def _map_fx_fetch_error(fx_result: AdapterResult) -> str:
    """ErrorCode + detail → fx_failure_reason mapping. Closed vocab per
    invariant 18.

    Relocated from §3.2.3 (extract_fcf.py block) in cycle-23. The function
    was only ever called by apply_fx_conversion in this same file —
    co-locating eliminates the cross-module reference that v2.15 left
    unresolved (would have NameError'd at first FX-source-down call).

    post-impl loop-2 fix: the get_fx_window adapter encodes specific
    closed-vocab fail-close reasons in the detail string while reusing
    ErrorCode for transport categorization (PARSE_ERROR for
    "fx_history_insufficient" / "yfinance returned None"; SHAPE_MISMATCH
    for "fx_currency_unrecognized" / "fx_currency_unsupported" /
    "report_period not YYYY-MM-DD"; UPSTREAM_ERROR for "fx_rate_outlier").
    Pre-fix the mapper only switched on code, collapsing every reason
    into `fx_source_unavailable` — the operator-visible diagnostic for a
    very-old report_period that precedes yfinance JPY=X history (real
    TTDKY-style case) said "source unavailable" instead of "history
    insufficient", and a malformed report_period also masked as transport
    outage. Now we inspect `detail` first for any closed-vocab token and
    fall back to code-based mapping only when no token matches.
    """
    err = fx_result.error
    code = err.code if err else None
    detail = (err.detail if err and err.detail else "") or ""

    # Detail-token short-circuit: get_fx_window encodes specific reasons
    # as the detail prefix. Order: most-specific first.
    if detail.startswith("fx_history_insufficient"):
        return "fx_history_insufficient"
    if detail.startswith("fx_rate_outlier"):
        return "fx_rate_outlier"
    if detail.startswith("fx_currency_unrecognized"):
        return "fx_currency_unrecognized"
    if detail.startswith("fx_currency_unsupported"):
        return "fx_currency_unsupported"
    # post-impl loop-2 ISS-026: malformed report_period was previously
    # routed to `fx_source_unavailable`, misleading operators. Note this
    # uses a NEW closed-vocab reason (`fx_period_malformed`) that we add
    # to FX_FAILURE_REASONS in scripts/fx_constants.py.
    if detail.startswith("report_period not YYYY-MM-DD"):
        return "fx_period_malformed"

    # Code-based fallback for non-specific failures.
    if code == ErrorCode.RATE_LIMIT:
        return "fx_source_unavailable"  # transient → unavailable from our POV
    if code == ErrorCode.HTTP_TRANSPORT:
        return "fx_source_unavailable"
    if code == ErrorCode.PARSE_ERROR:
        return "fx_source_unavailable"  # yfinance returned empty/malformed
    if code == ErrorCode.NOT_FOUND:
        return "fx_currency_unsupported"  # yfinance doesn't have this pair
    if code == ErrorCode.UNAUTHORIZED:
        return "fx_source_unavailable"  # paranoia — yfinance has no auth
    # Default → unavailable (safest fail-close)
    return "fx_source_unavailable"


def _serialize_window(validated: FxWindow, detected_currency: str) -> list[dict]:
    """Convert validated FxWindow → list[dict] for cert.window emission.

    Each dict carries the per-row FX rate + provenance for downstream
    audit. Output shape matches §3.1.2 schema example exactly:
        {"currency": "JPY", "date": "2024-03-31",
         "fx_rate_usd_per_local": 0.006631, "source": "yfinance:JPY=X",
         "bar_date": "2024-03-29", "lag_days": 2}
    For usd_native short-circuit rows, `bar_date` and `lag_days` are None
    (Optional fields on FxRateRow per §3.0.2)."""
    return [
        {
            "currency": detected_currency,
            "date": r.date,
            "fx_rate_usd_per_local": r.fx_rate_usd_per_local,
            "source": r.source,
            "bar_date": r.bar_date,
            "lag_days": r.lag_days,
        }
        for r in validated.rows
    ]


def build_cert_block(detected_currency: str, fx_window: list[dict]) -> dict:
    """Returns the cert block + 3 anti-hallucination tags as a dict to
    `.update()` into result. Single source of truth for invariant 14
    emission; eliminates 4-site drift risk."""
    return {
        "currency_conversion": {
            "basis": "usd_converted",
            "source_currency": detected_currency,
            "fx_source": f"yfinance:{detected_currency}=X",
            "window": fx_window,
        },
        "currency_conversion_basis_tag":
            f"[Calc: source=yfinance:{detected_currency}=X; per-quarter report_period FX]",
        "fx_source_tag":
            f"[API: yfinance {detected_currency}=X daily close]",
        "fx_rate_usd_per_local_tag":
            f"[Calc: 1 / yfinance {detected_currency}=X bar close; per report_period]",
    }
