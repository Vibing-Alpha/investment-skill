"""Portfolio decision log — persist confirmed /portfolio decisions.

Two subcommands:

  write    Merge LLM-authored decision blob with programmatic snapshots
           (portfolio, macro, thesis metadata, stress test) and write
           decisions.json + decisions.md to the output directory.

  review   Scan the most recent prior run's decisions.json and print
           follow-up items whose date has arrived. Used by the portfolio
           skill at Step 0 to cross-check prior flagged catalysts.

The decision log is the durable artifact behind /portfolio — it is the
only file that survives between runs and supports audit/reflection.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional

import yaml


SKILL_VERSION = "portfolio@v1-decision-log"

# Action vocabularies — kept in sync with prompts/portfolio-decide.md schema.
# When adding a value here, grep both decide.md and _render_md action_order.
# Schema parity tests at tests/test_schemas_decisions.py assert these match
# the loader-side frozensets in scripts/schemas/decisions.py.
DECISION_ACTIONS = frozenset({"exit", "reduce", "hold", "add", "buy", "skip"})
ORDER_ACTIONS = frozenset({"sell", "buy"})
# FIX 3 (D2): a skip/buy entry gate must be a technical/principle condition,
# not a valuation verdict (portfolio-decide.md:59-111). WARN-only audit
# friction — flags any skip/buy rationale carrying valuation/ER language so a
# reviewer re-checks the gate. Deliberately NOT suppressed when the rationale
# also mentions a technical term: the target regression (technicals are fine
# but the skip gates on valuation anyway) itself contains technical words, so
# a "has a technical term → suppress" discriminator would silently miss the
# exact case this guards (post-impl review). A regex can't tell gate from
# context; the real prevention is the prompt rule, this is the visible tripwire.
_VALUATION_GATE_RE = re.compile(
    r"(overvalued|valuation|expected\s+return|\bER\b|\bCE\b)", re.IGNORECASE
)
# Per codex review 2026-05-22 (F4): producer-side enum validation was
# missing — orders_proposed[*].type was required but not enum-checked at
# write time. Now mirrored from scripts/schemas/decisions.py:ORDER_TYPES so
# malformed types fail the producer's _validate_blob_shape rather than only
# the consumer's load_decisions.
ORDER_TYPES = frozenset({
    "market", "limit", "stop_limit", "stop", "moc", "loc", "gtc",
    "stop_market",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_yaml(path: pathlib.Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_json(path: pathlib.Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _latest_thesis_refs(ticker: str) -> List[str]:
    """Return newest bq_analysis.json + investment_thesis.json paths.

    Uses the delta resolver with include_today=True — portfolio_log is
    a consumer that wants "what data exists right now", not "what prior
    data to compare against" (which is probe semantics).
    """
    from scripts.delta.resolver import find_latest_prior
    refs: List[str] = []
    for fname in ("investment_thesis.json", "bq_analysis.json"):
        skill = "score-business" if fname == "bq_analysis.json" else "investment-thesis"
        run_dir = find_latest_prior(ticker, skill, include_today=True)
        if run_dir and (run_dir / fname).exists():
            refs.append(str(run_dir / fname))
    return refs


def _extract_thesis_snapshot(
    ticker: str,
) -> tuple[str, Optional[Dict[str, Any]]]:
    """Pull BQ/ER/CE/conviction fields from the latest thesis file.

    Returns (reason, snap) where reason ∈ {"ok", "no_prior", "schema_error"}:
      - "no_prior": no thesis dir / file exists (normal for new tickers)
      - "schema_error": file exists but failed typed load (actionable drift)
      - "ok": snapshot returned with extracted fields

    Fields are read via the typed contracts in `scripts.schemas.*` so
    schema drift surfaces at the load boundary instead of cascading into
    silent `.get()` fallbacks.
    """
    from scripts.delta.resolver import find_latest_prior
    thesis_dir = find_latest_prior(ticker, "investment-thesis", include_today=True)
    if not thesis_dir:
        return ("no_prior", None)
    thesis_path = thesis_dir / "investment_thesis.json"
    if not thesis_path.exists():
        return ("no_prior", None)
    try:
        from scripts.schemas.investment_thesis import load_investment_thesis
        from scripts.schemas import SchemaError
        thesis = load_investment_thesis(thesis_path)
    except (OSError, ValueError, SchemaError) as exc:
        # fail-open-ok: missing/corrupt/non-canonical prior thesis → drop
        # ticker from summary. SchemaError inherits ValueError; listed
        # explicitly for clarity. json.JSONDecodeError is also a ValueError.
        print(f"[WARN] portfolio_log: thesis read failed for {ticker}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return ("schema_error", None)

    # BQ lookup routes through the resolver so `run_meta.bq.completed`
    # + JSON parseability are enforced (never trust a bare file on
    # disk). Constrain bq_date <= thesis_date so a newer BQ isn't
    # spliced into an older-thesis snapshot — keeps snapshot timing
    # self-consistent. Spec §7.4 decline cascade (today's thesis + no
    # same-day BQ) naturally resolves to yesterday's BQ here.
    bq_dir = find_latest_prior(ticker, "score-business", include_today=True)
    bq_path = None
    if bq_dir is not None and bq_dir.name <= thesis_dir.name:
        bq_path = bq_dir / "bq_analysis.json"

    bq_score = None
    bq_doc = None  # F16: keep the loaded BQ doc for cert reconciliation below
    # F18 (codex cycle 4): distinguish "no BQ exists" from "BQ existed but
    # failed typed load". The latter is a SECURITY-relevant state: a
    # fabricated thesis cert must NOT be propagated when its producer-side
    # counterpart can't be verified.
    bq_load_state: str = "no_path"  # no_path | load_failed | loaded
    if bq_path is not None:
        try:
            from scripts.schemas.bq_analysis import load_bq_analysis
            from scripts.schemas import SchemaError
            bq = load_bq_analysis(bq_path)
            bq_score = bq.scores.overall
            bq_doc = bq
            bq_load_state = "loaded"
        except (OSError, ValueError, SchemaError) as exc:
            # fail-open-ok: missing/corrupt/non-canonical prior BQ →
            # bq_score stays None, summary still useful.
            # SchemaError inherits ValueError; listed explicitly for clarity.
            bq_load_state = "load_failed"
            print(f"[WARN] portfolio_log: bq read failed for {ticker}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)

    snap = {
        "bq": bq_score,
        "conviction": thesis.thesis_conviction,
        "er": thesis.expected_return,
        "ce": thesis.capital_efficiency,
        # Price the thesis computed er/ce/max_downside against — lets a reader
        # detect ER/CE staleness when the decision-time price has drifted. For
        # HELD names, compare against portfolio_before.holdings[].price; for
        # watchlist / non-held names (no holdings row) the comparison price is
        # the run's macro.json ticker_prices, plus the prompt's own ER/CE
        # reconciliation step. Omitted from the snapshot entirely (the trailing
        # None-drop, NOT a JSON null) on legacy artifacts where the agent did
        # not emit meta.current_price.
        "thesis_price": thesis.meta.current_price,
        "dominant_signal": thesis.signal_dominant,
        "max_downside": thesis.max_downside,
    }

    # ----- F16 (codex review cycle 3): DL3c cert cross-layer reconciliation -----
    # The DL3c chain producer→consumer is:
    #   extract_fcf / historical_multiples / adr/correct  (producer; authoritative)
    #     → assemble  (propagates to bq_analysis.json)
    #     → evaluate-thesis prompt  (propagates to investment_thesis.json)
    #     → portfolio_log  (THIS layer; persists into decisions.json)
    #
    # Each layer may drop the field by accident. portfolio_log is the
    # last-stop, so we cross-check thesis.currency_conversion against
    # bq.currency_conversion. Policy:
    #   - Both agree on basis → propagate (the common path)
    #   - BQ has usd_converted cert, thesis says usd_native/legacy →
    #     thesis-side drift; trust BQ (producer authority), warn loudly
    #   - Thesis has usd_converted cert, BQ says usd_native/legacy →
    #     suspicious (thesis can't manufacture a cert the producer didn't
    #     emit); FAIL-CLOSE
    #   - Both have certs but different source_currency or fx_source →
    #     FAIL-CLOSE (cross-layer drift)
    snap["dl3c_mode"] = thesis.dl3c_mode

    def _cert_authoritative_or_none():
        """Return (mode, cert_obj_or_None) to persist, or raise via warn+None.

        Authority: bq (producer-side) wins on disagreement that is salvageable;
        true drift (both have different certs) is fail-close via warning +
        emitting bq's view.

        F18 (codex cycle 4): bq_load_state distinguishes:
          - no_path   → no prior BQ exists (legitimate first-thesis state)
          - load_failed → BQ exists but unparseable (security-relevant; can't
                          verify thesis cert against producer authority)
          - loaded    → cross-check is meaningful
        """
        import json as _json
        from dataclasses import asdict as _asdict

        thesis_has_cert = (
            thesis.dl3c_mode == "post_dl3c_usd_converted"
            and thesis.currency_conversion is not None
        )
        bq_has_cert = (
            bq_doc is not None
            and bq_doc.dl3c_mode == "post_dl3c_usd_converted"
            and bq_doc.currency_conversion is not None
        )

        def _normalize_cert(cert):
            """F17: full normalized cert string for byte-equality comparison.
            Includes window rows, dates, rates, lag_days — not just header."""
            return _json.dumps(_asdict(cert), sort_keys=True)

        if bq_load_state == "load_failed":
            # F18: BQ exists but can't be parsed. Cannot verify thesis cert
            # against producer authority — refuse to propagate the cert,
            # but emit thesis.dl3c_mode for diagnostic visibility. This
            # closes the "fabricated cert sneaks through when BQ malformed"
            # attack/error path.
            if thesis_has_cert:
                print(
                    f"[WARN] portfolio_log: thesis claims usd_converted cert "
                    f"for {ticker} but BQ load failed — cannot verify against "
                    f"producer authority. Dropping cert; emitting thesis mode "
                    f"tag only.",
                    file=sys.stderr,
                )
            return (thesis.dl3c_mode, None)

        if bq_load_state == "no_path":
            # Legitimate first-thesis state. No producer to cross-check;
            # trust thesis (downstream sees only thesis anyway).
            return (thesis.dl3c_mode,
                    thesis.currency_conversion if thesis_has_cert else None)

        # bq_load_state == "loaded": cross-check is meaningful
        if thesis_has_cert and bq_has_cert:
            t_norm = _normalize_cert(thesis.currency_conversion)
            b_norm = _normalize_cert(bq_doc.currency_conversion)
            if t_norm == b_norm:
                # F17: full-cert byte-equal (header + window) → genuine agreement
                return (thesis.dl3c_mode, thesis.currency_conversion)
            print(
                f"[WARN] portfolio_log: DL3c cert mismatch for {ticker} — "
                f"thesis vs bq differ in header or window. "
                f"Trusting BQ (producer authority). "
                f"thesis_basis={thesis.currency_conversion.basis}/"
                f"{thesis.currency_conversion.source_currency}/"
                f"{thesis.currency_conversion.fx_source}; "
                f"bq_basis={bq_doc.currency_conversion.basis}/"
                f"{bq_doc.currency_conversion.source_currency}/"
                f"{bq_doc.currency_conversion.fx_source}",
                file=sys.stderr,
            )
            return (bq_doc.dl3c_mode, bq_doc.currency_conversion)

        if bq_has_cert and not thesis_has_cert:
            # Thesis dropped/mislabeled the cert relative to BQ. BQ is the
            # producer-side authority, so we re-introduce it here.
            print(
                f"[WARN] portfolio_log: DL3c cert dropped at thesis layer "
                f"for {ticker} — bq says usd_converted "
                f"({bq_doc.currency_conversion.source_currency}), thesis "
                f"says {thesis.dl3c_mode}. Restoring cert from BQ.",
                file=sys.stderr,
            )
            return ("post_dl3c_usd_converted", bq_doc.currency_conversion)

        if thesis_has_cert and not bq_has_cert:
            # Thesis claims a cert that the producer (BQ) doesn't carry —
            # impossible under the DL3c contract. Refuse to silently
            # propagate; emit bq's view + warn.
            print(
                f"[WARN] portfolio_log: DL3c cert appeared at thesis layer "
                f"for {ticker} without BQ producer support (bq mode={bq_doc.dl3c_mode}). "
                f"Refusing to propagate fabricated cert; using BQ mode.",
                file=sys.stderr,
            )
            return (bq_doc.dl3c_mode, None)

        # Neither has a usd_converted cert. Prefer BQ mode (producer
        # authority) over thesis mode so an exotic BQ mode (e.g. a
        # post_dl3c_failed_fx state in a hand-corrupted but schema-valid
        # BQ) isn't masked by an incorrectly-set thesis mode. assemble.py
        # fails closed on failed_fx so this is defense-in-depth, but cheap.
        # Cycle 5 LOW note.
        return (bq_doc.dl3c_mode, None)

    reconciled_mode, reconciled_cert = _cert_authoritative_or_none()
    snap["dl3c_mode"] = reconciled_mode
    if reconciled_cert is not None:
        # Full cert serialization (F10 round-trip safety). json round-trip
        # converts dataclass tuples to lists for schema loader compatibility.
        import json as _json
        from dataclasses import asdict
        snap["currency_conversion"] = _json.loads(_json.dumps(asdict(reconciled_cert)))

    # drop None-heavy snapshots to keep JSON tidy
    clean = {k: v for k, v in snap.items() if v is not None} or None
    return ("ok", clean)


def _classify_regime(macro: Dict[str, Any]) -> tuple[str, str]:
    """Simple deterministic regime tag from macro.json.

    The interpretation is a short sentence — skill may override with richer
    language, but this gives a stable default.

    Fail-closed: when VIX data or any of the three major indices is
    missing, we cannot conclude `risk_on`. The original short-circuit
    `vix and vix_ma20 and ...` treated None as subdued, which let a
    macro.json with empty `volatility` classify as risk_on (ignoring
    the raise-cash-on-deterioration signal entirely).
    """
    market = macro.get("market", {})
    vol = macro.get("volatility", {})
    indices = ("SPY", "QQQ", "^DJI")
    above_ma50 = 0
    missing_indices: List[str] = []
    for idx in indices:
        m = market.get(idx, {})
        p, ma50 = m.get("price"), m.get("ma50")
        if p is None or ma50 is None:
            missing_indices.append(idx)
            continue
        if p > ma50:
            above_ma50 += 1
    vix = vol.get("vix")
    vix_ma20 = vol.get("vix_ma20")
    vix_unknown = vix is None or vix_ma20 is None
    vix_elevated = (not vix_unknown) and vix > vix_ma20 * 1.2

    if vix_unknown or missing_indices:
        regime = "mixed"
        gaps = list(missing_indices) + (["VIX"] if vix_unknown else [])
        note = (
            f"Macro signals incomplete (missing: {', '.join(gaps)}) — "
            "treat as mixed; evaluate the raise-cash-on-deterioration discipline explicitly."
        )
    elif above_ma50 == 3 and not vix_elevated:
        regime = "risk_on"
        note = "All three major indices above MA50 and VIX subdued — the raise-cash-on-deterioration discipline is not triggered (macro has not deteriorated)."
    elif above_ma50 <= 1 or vix_elevated:
        regime = "risk_off"
        note = "Majority of indices below MA50 or VIX elevated — the raise-cash-on-deterioration discipline should be evaluated explicitly."
    else:
        regime = "mixed"
        note = "Market signals are mixed — heightened attention to individual invalidation triggers warranted."
    return regime, note


def _compute_portfolio_before(
    state: Dict[str, Any], prices: Dict[str, float]
) -> Dict[str, Any]:
    cash = float(state.get("cash", 0))  # fail-open-ok: $0 cash is a legit state (fully invested)
    holdings_raw = state.get("holdings", {}) or {}
    rows: List[Dict[str, Any]] = []
    total_positions = 0.0
    # Track tickers whose price is missing so cmd_write can fail-closed
    # (producer-consumer rule #4 — missing data is a failure, not zero).
    # The old path silently dropped the position from total_equity AND
    # produced a fake pnl_pct = -100% (via `(mv or 0) - cost`).
    missing_prices: List[str] = []
    # HIGH-14 parity: also track holdings whose 'shares' field is missing.
    # Previously these silently became 0 shares → invisible to mv / weight
    # computation, indistinguishable from a sold-out position.
    missing_shares: List[str] = []
    for t, h in holdings_raw.items():
        if isinstance(h, dict):
            if "shares" not in h:
                missing_shares.append(t)
                shares = None
            else:
                shares = h["shares"]
        else:
            shares = h
        cb = h.get("cost_basis") if isinstance(h, dict) else None
        px = prices.get(t)
        if px is None:
            missing_prices.append(t)
            mv = None
        elif shares is None:
            mv = None
        else:
            mv = shares * px
            total_positions += mv
        rows.append({
            "ticker": t, "shares": shares, "cost_basis": cb,
            "price": px,
            "market_value": round(mv, 2) if mv is not None else None,
        })
    total = total_positions + cash
    # weight/pnl pass-2 now that total is known
    for r in rows:
        mv = r.get("market_value")
        cb = r.get("cost_basis")
        shares = r.get("shares", 0)  # fail-open-ok: pass-2 reads row built in pass-1; only used via truthy `and shares` + cost_total guards below
        r["weight_pct"] = (
            round(mv / total * 100, 2)
            if (mv is not None and total > 0)
            else None
        )
        # PnL requires BOTH a current market_value and a cost_basis — any
        # missing piece → None. The original `(mv or 0) - cost` pattern
        # was the source of the fake -100% bug on missing-price holdings.
        if mv is not None and cb is not None and shares:
            cost_total = cb * shares
            r["pnl_pct"] = (
                round((mv - cost_total) / cost_total * 100, 2)
                if cost_total
                else None
            )
        else:
            r["pnl_pct"] = None
    rows.sort(key=lambda r: r.get("weight_pct") or 0, reverse=True)
    return {
        "total_equity": round(total, 2),
        "cash": round(cash, 2),
        "cash_pct": round(cash / total * 100, 2) if total > 0 else None,
        "holdings": rows,
        "watchlist": list(state.get("watchlist") or []),
        "missing_prices": missing_prices,
        "missing_shares": missing_shares,
    }


def _compact_macro(macro: Dict[str, Any]) -> Dict[str, Any]:
    m = macro.get("market", {})
    v = macro.get("volatility", {})
    r = macro.get("rates", {})
    regime, note = _classify_regime(macro)

    def _mk(idx: str) -> Optional[Dict[str, Any]]:
        d = m.get(idx)
        if not d:
            return None
        entry = dict(d)
        p, ma50 = d.get("price"), d.get("ma50")
        if p and ma50:
            entry["vs_ma50_pct"] = round((p - ma50) / ma50 * 100, 2)
        return entry

    vix_obj: Dict[str, Any] = {}
    if v.get("vix") is not None:
        vix_obj["vix"] = v["vix"]
    if v.get("vix_ma20") is not None:
        vix_obj["vix_ma20"] = v["vix_ma20"]
    if vix_obj.get("vix") and vix_obj.get("vix_ma20"):
        vix_obj["vix_vs_ma20_pct"] = round(
            (vix_obj["vix"] - vix_obj["vix_ma20"]) / vix_obj["vix_ma20"] * 100, 2
        )

    return {
        "spy": _mk("SPY"),
        "qqq": _mk("QQQ"),
        "dji": _mk("^DJI"),
        **vix_obj,
        "rates": r or None,
        "regime": regime,
        "regime_interpretation": note,
    }


def _principle_tags(cited_strings: List[str]) -> List[str]:
    """Extract the structured '#N' principle tags from each principle_cited string.

    Per prompts/portfolio-decide.md, `principle_cited` starts with a primary
    `#N` tag and MAY list additional driving principles — the methodology
    (Phase 2.4) explicitly invites citing the principle(S), plural. Multiple
    principles are written as separate clauses split by `;` (the single
    separator the prompt mandates), e.g. "#4 churn; #6 sizing -> larger tranche".
    We credit a `#N` only when it LEADS a clause, so every driving principle is
    recorded while free-prose mentions ("loosely related to #3", "NOT #3
    valuation-alone") are NOT counted — the tag there does not lead its clause.

    Separator is `;` ONLY, deliberately matching the prompt contract. We do NOT
    split on `,`/`+`/`/`/`&`: those are common intra-prose punctuation, so
    splitting on them would over-credit a trailing rejected tag (e.g.
    "#4 churn, #3 considered but rejected" must NOT credit #3). For this
    audit-only field, a silent over-credit (a rejected principle vanishing from
    not_cited) is worse than a noisy under-credit (a `;`-less author surfaces an
    extra entry in not_cited, self-correcting on a glance), so the parser fails
    toward the narrow, prompt-aligned convention.

    History: `re.findall` across the whole string over-credited prose mentions;
    leading-of-string-only under-credited multi-principle decisions (a 2026-05-26
    run cited "#4 ...; #6 ...; #7 ..." and the audit wrongly reported #6/#7 as
    not_cited); a brief 5-separator clause split fixed that but re-opened the
    over-credit hole + diverged from the prompt. Clause-leading on `;` only
    resolves all three (codex review 2026-05-26).
    """
    tags: List[str] = []
    for s in cited_strings:
        if not s:
            continue
        for clause in s.split(";"):
            m = re.match(r"\s*(#\d+)\b", clause)
            if m:
                tags.append(m.group(1))
    # de-dup, preserve order
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _enrich_decisions(
    blob_decisions: List[Dict[str, Any]],
    portfolio: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Fill current_weight_pct, thesis_snapshot, report_refs into each decision.

    Assumes blob has passed `_validate_blob_shape` upstream, so `ticker`
    is present on every entry (validator returns exit 2 otherwise).

    HIGH-15: programmatic fields (current_weight_pct, thesis_snapshot,
    report_refs) are computed from portfolio-state / prior run artifacts.
    A prior `setdefault` path trusted the LLM to preserve them, but the
    LLM could silently fabricate a weight or fake a report reference —
    corrupting the audit trail and constraint math. We now overwrite
    these fields unconditionally with the programmatic source of truth.
    """
    weights = {h["ticker"]: h.get("weight_pct") or 0 for h in portfolio["holdings"]}
    schema_errors: List[str] = []
    out: List[Dict[str, Any]] = []
    for d in blob_decisions:
        merged = dict(d)
        t = merged.get("ticker")
        # HIGH-15: weight is always computed from portfolio-state; no
        # LLM-supplied value can be trusted. Overwrite unconditionally.
        merged["current_weight_pct"] = weights.get(t, 0)
        if t:
            reason, snap = _extract_thesis_snapshot(t)
            if reason == "schema_error":
                schema_errors.append(t)
            if snap:
                # Overwrite (not setdefault) — snapshot is derived from
                # disk artifacts, which are the authoritative source.
                merged["thesis_snapshot"] = snap
            refs = _latest_thesis_refs(t)
            if refs:
                merged["report_refs"] = refs
        out.append(merged)
    if schema_errors:
        # Operator-visible summary: schema failures are actionable drift,
        # not background noise. Per-ticker [WARN] lines already went to
        # stderr; this aggregate makes the remediation obvious.
        print(
            f"[WARN] portfolio_log: {len(schema_errors)} ticker(s) dropped "
            f"from thesis_snapshot due to schema/IO failure: "
            f"{sorted(schema_errors)}. Rerun /score-business or "
            f"/investment-thesis to regenerate, OR delete "
            f"reports/<T>/<olddate>/ if the drift is in a historical "
            f"artifact.",
            file=sys.stderr,
        )
    return out


# ---------------------------------------------------------------------------
# Blob validation (pre-persist, fail-closed)
# ---------------------------------------------------------------------------

def _validate_blob_shape(blob: Dict[str, Any]) -> List[str]:
    """Check the LLM-authored decisions blob for required fields + enum
    vocabularies per prompts/portfolio-decide.md. Returns error strings.

    Called up-front in cmd_write so a malformed blob never reaches the
    enrichment / render / persist pipeline. Replaces the prior path
    where `d["ticker"]` would KeyError mid-pipeline with no diagnostic.

    HIGH-17: additionally enforces rationale + principle_cited on decisions,
    sequence/type/linked_decision on orders, and ticker/event/what_to_watch
    on follow_ups — fields required by prompts/portfolio-decide.md that
    were previously unchecked and could slip through into the log.
    """
    errors: List[str] = []
    # Guard against non-list top-level fields — `blob.get("decisions", []) or []`
    # falls through when the value is truthy-non-list (e.g. int, dict, string),
    # causing TypeError on enumerate(). Fail-closed: record a structural error
    # and treat as empty so the rest of the checks proceed.
    def _as_list(key: str) -> List[Any]:
        v = blob.get(key)
        if v is None or v == []:
            return []
        if not isinstance(v, list):
            errors.append(f"{key}: not a list ({type(v).__name__})")
            return []
        return v

    for i, d in enumerate(_as_list("decisions")):
        if not isinstance(d, dict):
            errors.append(f"decisions[{i}]: not a dict ({type(d).__name__})")
            continue
        if "ticker" not in d:
            errors.append(f"decisions[{i}]: missing 'ticker'")
        if "action" not in d:
            errors.append(f"decisions[{i}] ({d.get('ticker', '?')}): missing 'action'")
        elif d["action"] not in DECISION_ACTIONS:
            errors.append(
                f"decisions[{i}] ({d.get('ticker', '?')}): action={d['action']!r} "
                f"not in {sorted(DECISION_ACTIONS)}"
            )
        # HIGH-17: rationale + principle_cited are the audit-trail
        # primitives (decide.md §"Decision Log Output"). Missing either
        # defeats the point of keeping the log.
        for key in ("rationale", "principle_cited"):
            v = d.get(key)
            if not v:
                errors.append(
                    f"decisions[{i}] ({d.get('ticker', '?')}): missing {key!r}"
                )
            elif not isinstance(v, str):
                # Type-check, not just presence: principle_cited flows into
                # _principle_tags -> str.split(';'). A truthy non-string (number/
                # list) passes a presence check then crashes mid-write with a
                # raw AttributeError — the exact mid-pipeline failure this
                # validator exists to convert into a clean up-front error.
                errors.append(
                    f"decisions[{i}] ({d.get('ticker', '?')}): {key!r} must be a "
                    f"string, got {type(v).__name__}"
                )
        if d.get("action") in ("skip", "buy") and _VALUATION_GATE_RE.search(d.get("rationale") or ""):
            print(
                f"[WARN] portfolio_log: {d.get('ticker', '?')} {d['action']} rationale "
                f"carries a valuation/ER term — per portfolio-decide.md the entry gate is "
                f"technical/principle, not valuation. Confirm the disqualifier is a run-day "
                f"technical condition (RSI/pct_b/volume), not richness.",
                file=sys.stderr,
            )
    for i, o in enumerate(_as_list("orders_proposed")):
        if not isinstance(o, dict):
            errors.append(f"orders_proposed[{i}]: not a dict ({type(o).__name__})")
            continue
        for key in ("ticker", "action", "shares"):
            if key not in o:
                errors.append(f"orders_proposed[{i}]: missing {key!r}")
        # HIGH-17: sequence / type / linked_decision are required per
        # decide.md orders_proposed schema — render + review consumers
        # assume them, and their absence corrupts downstream sort/trace.
        for key in ("sequence", "type", "linked_decision"):
            if key not in o:
                errors.append(
                    f"orders_proposed[{i}] ({o.get('ticker', '?')}): missing {key!r}"
                )
        action = o.get("action")
        if action is not None and action not in ORDER_ACTIONS:
            errors.append(
                f"orders_proposed[{i}] ({o.get('ticker', '?')}): "
                f"action={action!r} not in {sorted(ORDER_ACTIONS)}"
            )
        # F4 (codex 2026-05-22): validate `type` against ORDER_TYPES at the
        # producer boundary too — previously only the schema loader caught
        # drift, so write-time errors propagated all the way to read-time.
        order_type = o.get("type")
        if order_type is not None and order_type not in ORDER_TYPES:
            errors.append(
                f"orders_proposed[{i}] ({o.get('ticker', '?')}): "
                f"type={order_type!r} not in {sorted(ORDER_TYPES)}"
            )
        if "shares" in o:
            shares = o.get("shares")
            # F11 (codex review cycle 2): align with scripts/schemas/decisions.py
            # which accepts float to support fractional shares (real broker
            # feature on Fidelity / IBKR / Robinhood). Previously this producer
            # rejected float, so a schema-valid order could be blocked at write
            # time. Reject bool explicitly (bool is subclass of int in Python).
            if (isinstance(shares, bool) or
                not isinstance(shares, (int, float)) or
                shares <= 0):
                errors.append(
                    f"orders_proposed[{i}] ({o.get('ticker', '?')}): "
                    f"shares={shares!r} must be a positive number "
                    f"(int or float for fractional shares)"
                )
    # HIGH-17: follow_ups schema (decide.md) requires date, ticker, event,
    # what_to_watch. Past-date filtering happens in _sanitize_follow_ups;
    # missing-field checks belong here so malformed entries never reach
    # the sanitizer or the rendered MD table.
    for i, fu in enumerate(_as_list("follow_ups")):
        if not isinstance(fu, dict):
            errors.append(f"follow_ups[{i}]: not a dict ({type(fu).__name__})")
            continue
        for key in ("date", "ticker", "event", "what_to_watch"):
            if not fu.get(key):
                errors.append(
                    f"follow_ups[{i}] ({fu.get('ticker', '?')}): missing {key!r}"
                )

    # Candidate-action scan discipline (decide.md Phase 3 "zero-order
    # discipline"): a run proposing no orders MUST record candidate_scan so a
    # silently-defaulted all-hold/all-skip is visible. WARN-only friction
    # (mirrors the D2 valuation WARN above) — inaction stays a legitimate
    # outcome, but UNREPORTED or hollow-shell inaction does not. Shape check is
    # intentionally shallow (dict + non-empty summary + near_misses-is-list):
    # a deeper validator would re-create the over-gating this guard avoids.
    # Raw orders check (NOT _as_list): a malformed non-list orders_proposed is
    # already flagged above as a shape error and must NOT also be read as
    # "zero orders" here.
    raw_orders = blob.get("orders_proposed")
    zero_orders = raw_orders is None or (isinstance(raw_orders, list) and not raw_orders)
    if zero_orders:
        scan = blob.get("candidate_scan")
        nm = scan.get("near_misses") if isinstance(scan, dict) else None
        scan_ok = (
            isinstance(scan, dict)
            and isinstance(scan.get("summary"), str)
            and scan.get("summary").strip()
            and (nm is None or isinstance(nm, list))
        )
        if not scan_ok:
            print(
                "[WARN] portfolio_log: no orders proposed AND candidate_scan is "
                "missing/malformed — per portfolio-decide.md zero-order "
                "discipline, an all-hold/all-skip run must record candidate_scan "
                "as an object with a non-empty 'summary' (and, if present, a list "
                "'near_misses'). Report the rotation/opportunity scan result so a "
                "silently-defaulted hold-all is visible.",
                file=sys.stderr,
            )

    return errors


def _validate_decision_coverage(
    blob: Dict[str, Any], portfolio_before: Dict[str, Any]
) -> tuple[List[str], List[str]]:
    """Enforce decide.md contract: every holding + watchlist ticker MUST
    appear in decisions[] (including hold/skip entries). Returns
    (missing_tickers, extra_tickers).
    """
    expected = {h["ticker"] for h in portfolio_before.get("holdings", [])}
    expected |= set(portfolio_before.get("watchlist", []) or [])
    covered = {
        d.get("ticker") for d in (blob.get("decisions") or [])
        if isinstance(d, dict) and d.get("ticker")
    }
    missing = sorted(expected - covered)
    extra = sorted(covered - expected)
    return missing, extra


def _sanitize_follow_ups(
    items: List[Dict[str, Any]], today_iso: str
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Drop follow_up entries with past/malformed dates per decide.md
    (`date >= today`). Returns (kept, dropped_reasons).
    """
    try:
        today = dt.date.fromisoformat(today_iso)
    except (ValueError, TypeError):
        return list(items), [f"today_iso unparseable ({today_iso!r}); no filter applied"]
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for fu in items or []:
        if not isinstance(fu, dict):
            dropped.append(f"non-dict entry {fu!r}")
            continue
        date_str = fu.get("date")
        if not date_str:
            dropped.append(f"{fu.get('ticker', '?')}: missing date")
            continue
        try:
            d = dt.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            dropped.append(f"{fu.get('ticker', '?')}: malformed date {date_str!r}")
            continue
        if d < today:
            dropped.append(
                f"{fu.get('ticker', '?')} {date_str}: past-dated "
                f"({(today - d).days}d before today)"
            )
            continue
        kept.append(fu)
    return kept, dropped


# ---------------------------------------------------------------------------
# MD cell escaping
# ---------------------------------------------------------------------------

def _md_cell(x: Any) -> str:
    """Escape a value for safe insertion into a markdown table cell.

    Pipes and newlines break markdown tables. ticker/rationale/event
    strings flow from user config + LLM output; unescaped special chars
    produce malformed renders.
    """
    if x is None:
        return "—"
    s = str(x)
    s = s.replace("\\", "\\\\").replace("|", "\\|")
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return s


def _enrich_orders(
    orders: List[Dict[str, Any]], prices: Dict[str, float]
) -> List[Dict[str, Any]]:
    # Deep-copy each order dict so we never mutate the caller's blob.
    # `list(orders)` in the caller is a shallow copy; without this, the
    # est_* fields would leak back into blob["orders_proposed"][i].
    out: List[Dict[str, Any]] = []
    for src in orders:
        o = dict(src)
        ticker = o.get("ticker")
        px = o.get("limit_price") or (prices.get(ticker) if ticker else None)
        shares = o.get("shares") or 0  # fail-open-ok: guarded by `if px and shares:` truthy check below — shares=0 skips notional computation
        if px and shares:
            notional = round(px * shares, 2)
            o.setdefault("est_execution_price", px)
            if o.get("action") == "sell":
                o["est_proceeds"] = notional
            elif o.get("action") == "buy":
                o["est_cost"] = notional
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# MD rendering
# ---------------------------------------------------------------------------

def _fmt(x, dash="—"):
    return dash if x is None else x


def _render_md(log: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Portfolio 决策记录 · {log['date']}")
    lines.append("")
    lines.append(f"**run_id**: `{log['run_id']}` · **状态**: `{log['status']}`")
    lines.append(f"**相关**: [decisions.json](decisions.json) · [macro.json](macro.json)")
    lines.append("")
    # Portfolio snapshot
    pb = log["portfolio_before"]
    lines.append("## 组合快照(执行前)")
    lines.append("")
    lines.append(f"总权益 **${pb['total_equity']:,.2f}** · 现金 **${pb['cash']:,.2f}** ({pb['cash_pct']}%)")
    lines.append("")
    lines.append("| Ticker | 股数 | 成本 | 现价 | 市值 | 权重 | 盈亏 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for h in pb["holdings"]:
        cb = f"{h['cost_basis']:.2f}" if h.get('cost_basis') else "—"
        px = f"{h['price']:.2f}" if h.get('price') else "—"
        mv = f"{h['market_value']:,.0f}" if h.get('market_value') is not None else "—"
        wt = f"{h['weight_pct']}%" if h.get('weight_pct') is not None else "—"
        pnl = f"{h['pnl_pct']:+.1f}%" if h.get('pnl_pct') is not None else "—"
        lines.append(
            f"| {_md_cell(h.get('ticker'))} | {h.get('shares', 0)} | {cb} | {px} | {mv} | {wt} | {pnl} |"  # fail-open-ok: display-only markdown table cell, 0 is acceptable placeholder
        )
    lines.append("")
    if pb.get("missing_prices"):
        missing = ", ".join(_md_cell(t) for t in pb["missing_prices"])
        lines.append(
            f"> WARNING — missing prices for {missing}; these positions are "
            f"EXCLUDED from total_equity/weights. Refill macro.ticker_prices "
            f"and rerun before acting on this log."
        )
        lines.append("")
    if pb["watchlist"]:
        lines.append(f"**Watchlist**: {', '.join(_md_cell(t) for t in pb['watchlist'])}")
        lines.append("")
    # Macro
    m = log.get("macro") or {}
    lines.append("## 市场环境")
    lines.append("")
    lines.append(f"**Regime**: `{m.get('regime')}` — {m.get('regime_interpretation')}")
    lines.append("")
    rows = [("SPY", m.get("spy")), ("QQQ", m.get("qqq")), ("DJI", m.get("dji"))]
    lines.append("| 指标 | 现值 | MA50 | vs MA50 |")
    lines.append("|---|---:|---:|---:|")
    for name, d in rows:
        if d:
            lines.append(f"| {name} | {d.get('price')} | {d.get('ma50')} | {d.get('vs_ma50_pct')}% |")
    if m.get("vix"):
        lines.append(f"| VIX | {m['vix']} | MA20 {m.get('vix_ma20')} | {m.get('vix_vs_ma20_pct')}% |")
    lines.append("")
    # Constraints
    c = log.get("constraints_active") or {}
    lines.append("## 硬约束")
    lines.append("")
    for k in ("max_single_position", "max_sector", "min_cash", "max_holdings"):
        lines.append(f"- `{k}`: {_fmt(c.get(k))}")
    lines.append(f"- source_hash: `{(c.get('source_hash') or '')[:12]}…`")
    lines.append("")
    # Decisions (already sorted by cmd_write via _sort_decisions; no re-sort)
    lines.append("## 决策一览")
    lines.append("")
    decisions = log["decisions"]
    lines.append("| Ticker | 动作 | 权重: 当前→目标 | 依据原则 | ER / CE | 备注 |")
    lines.append("|---|---|---|---|---|---|")
    for d in decisions:
        cur = d.get("current_weight_pct", 0)  # fail-open-ok: 0% weight is legit (no position in this ticker)
        tgt = d.get("target_weight_pct", cur)
        ts = d.get("thesis_snapshot") or {}
        er = f"{ts.get('er')}%" if ts.get("er") is not None else "—"
        ce = f"{ts.get('ce')}" if ts.get("ce") is not None else "—"
        rationale_raw = d.get("rationale") or ""
        rationale_short = rationale_raw[:80] + ("…" if len(rationale_raw) > 80 else "")
        lines.append(
            f"| {_md_cell(d.get('ticker'))} | **{_md_cell(d.get('action', '?'))}** | "
            f"{cur}% → {tgt}% | {_md_cell(d.get('principle_cited', '—'))} | "
            f"{er} / {ce} | {_md_cell(rationale_short)} |"
        )
    lines.append("")
    lines.append("### 详细 rationale 与失效触发")
    lines.append("")
    for d in decisions:
        lines.append(f"**{d.get('ticker', '?')}** ({d.get('action', '?')}):")
        lines.append(f"- Rationale: {d.get('rationale', '—')}")
        lines.append(f"- 原则: {d.get('principle_cited', '—')}")
        if d.get("invalidation_trigger"):
            lines.append(f"- 失效条件: {d['invalidation_trigger']}")
        if d.get("entry_trigger"):
            lines.append(f"- 入场触发: {d['entry_trigger']}")
        if d.get("watch_priority"):
            lines.append(f"- 关注优先级: {d['watch_priority']}")
        if d.get("data_freshness_warning"):
            lines.append(f"- ⚠ {d['data_freshness_warning']}")
        if d.get("report_refs"):
            refs = ", ".join(f"`{r}`" for r in d["report_refs"])
            lines.append(f"- 报告: {refs}")
        lines.append("")
    # Orders
    orders = log.get("orders_proposed") or []
    if orders:
        lines.append("## 建议订单")
        lines.append("")
        for o in orders:
            px = o.get("limit_price") or o.get("est_execution_price")
            px_str = f"${px}" if px else "Market"
            dur = (o.get("duration") or "gtc").upper()
            action = (o.get("action") or "?").upper()
            lines.append(
                f"**#{o.get('sequence', '?')} {action} {o.get('ticker', '?')} "
                f"{o.get('shares', 0)} @ {o.get('type', '?')} {px_str} {dur}**"  # fail-open-ok: display-only order-summary line
            )
            if o.get("est_proceeds"):
                lines.append(f"- 预估变现 ${o['est_proceeds']:,.0f}")
            if o.get("est_cost"):
                lines.append(f"- 预估成本 ${o['est_cost']:,.0f}")
            if o.get("execution_note"):
                lines.append(f"- 执行说明: {o['execution_note']}")
            lines.append("")
    # Candidate / rotation scan (decide.md Phase 3 zero-order discipline).
    # Stored in decisions.json + WARN-guarded by the logger, but until now
    # invisible in the human log — so a justified hold-all read identically to
    # a silent default-to-inaction. Render whenever present; it is REQUIRED on a
    # zero-order run and most load-bearing exactly then, since it carries the
    # reason no order fired. Fail-safe on a malformed blob (the schema layer
    # only WARNs, so a junk candidate_scan still reaches the renderer).
    scan = log.get("candidate_scan")
    if isinstance(scan, dict):
        lines.append("## 轮换/机会扫描")
        lines.append("")
        summary = scan.get("summary")
        if isinstance(summary, str) and summary.strip():
            # Collapse newlines and label-prefix: the contract says summary is
            # one line, but a malformed blob only WARNs. A stray "\n| x |" line
            # would inject a fake table row, and a leading "#"/">" would render
            # as a heading/quote. The prefix keeps user text off column 0.
            summary_text = summary.strip().replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            lines.append(f"摘要: {summary_text}")
            lines.append("")
        near_misses = scan.get("near_misses")
        # Keep only dicts carrying at least one contract field with real
        # content. A contentless dict ({}, unrelated keys, or empty/whitespace
        # fields) would otherwise render as an all-dash row that falsely
        # signals "a near-miss candidate existed" — worse than omitting it,
        # since this is audit context.
        rows = (
            [
                nm
                for nm in near_misses
                if isinstance(nm, dict)
                and any(str(nm.get(k) or "").strip() for k in ("ticker", "trigger", "waiting_on"))
            ]
            if isinstance(near_misses, list)
            else []
        )
        if rows:
            lines.append("| Ticker | 触发证据 | 受阻于 |")
            lines.append("|---|---|---|")
            for nm in rows:
                lines.append(
                    f"| {_md_cell(nm.get('ticker'))} | {_md_cell(nm.get('trigger'))} | "
                    f"{_md_cell(nm.get('waiting_on'))} |"
                )
            lines.append("")
        else:
            lines.append("近似候选: 无")
            lines.append("")
    # Stress test
    st = log.get("stress_test") or {}
    if st:
        lines.append("## 压力测试")
        lines.append("")
        lines.append("| 场景 | 现金余额 | 通过 |")
        lines.append("|---|---:|:---:|")
        for scen in ("base", "all_buy", "all_sell", "extreme_down", "defensive"):
            r = st.get(scen)
            if r and isinstance(r, dict):
                ok = "✓" if r.get("passed") else "✗"
                cash = r.get("cash_after")
                cash_s = f"${cash:,.0f}" if cash is not None else "—"
                lines.append(f"| {scen} | {cash_s} | {ok} |")
        if st.get("hard_constraint_violations") is not None:
            lines.append("")
            lines.append(f"硬约束违规: **{st['hard_constraint_violations']}**")
        lines.append("")
    # Follow-ups
    fus = log.get("follow_ups") or []
    if fus:
        lines.append("## 未来催化日历(复盘锚)")
        lines.append("")
        lines.append("| 日期 | Ticker | 事件 | 关注点 |")
        lines.append("|---|---|---|---|")
        for fu in sorted(fus, key=lambda x: x.get("date", "")):
            lines.append(
                f"| {_md_cell(fu.get('date'))} | {_md_cell(fu.get('ticker'))} | "
                f"{_md_cell(fu.get('event'))} | {_md_cell(fu.get('what_to_watch'))} |"
            )
        lines.append("")
    # Principle audit
    pa = log.get("principle_audit") or {}
    if pa:
        lines.append("## 本次原则引用审计")
        lines.append("")
        lines.append("> 口径：基于各决策 `principle_cited` 中作为子句主因出现的 `#N`；")
        lines.append("> 「未引用」= 本次未作为决策主因标注，不等于「未被考虑」。")
        lines.append("")
        lines.append(f"- 引用(决策主因): {', '.join(pa.get('cited_this_run', [])) or '无'}")
        lines.append(f"- 未引用(非主因): {', '.join(pa.get('not_cited_this_run', [])) or '无'}")
        if pa.get("interpretation"):
            lines.append("")
            lines.append(pa["interpretation"])
        lines.append("")
    # Notes
    if log.get("notes"):
        lines.append("## 备注")
        lines.append("")
        for n in log["notes"]:
            lines.append(f"- {n}")
        lines.append("")
    # Confirmation placeholders
    lines.append("## 用户确认 & 执行结果")
    lines.append("")
    lines.append(f"**确认状态**: `{log.get('user_confirmation', {}).get('status', 'pending')}`")
    lines.append("")
    lines.append("执行完毕后请在 `decisions.json` 回填:")
    lines.append("- `user_confirmation.status` → accepted/modified/declined")
    lines.append("- `user_confirmation.confirmed_at`")
    lines.append("- `execution_outcomes.orders_filled` + 实际价格")
    lines.append("- `execution_outcomes.reflection`(7-30 天后回看)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source-hash verification (strategy.yaml ↔ strategy.compiled.yaml)
# ---------------------------------------------------------------------------

def _load_compiled_typed(compiled_path: pathlib.Path):
    """Thin wrapper: preserves "missing file → None" contract callers
    depend on. Schema errors propagate.
    """
    if not compiled_path.exists():
        return None
    from scripts.schemas.strategy import load_compiled_strategy
    return load_compiled_strategy(compiled_path)


def _prepare_compiled_context(compiled_path):
    """Load compiled strategy once and hand back
    (doc_or_None, hard_dict, source_hash, principles_list).

    Missing file → returns (None, {...with 4 None constraint keys}, None, [])
    to preserve the legacy "all 4 constraint keys present" shape that
    `constraints_active` consumers downstream expect.
    Invalid file → raises SchemaError (a ValueError subclass). Caller
    (cmd_write) catches and returns 2; this keeps the "one error
    channel" contract `_verify_source_hash` already established with its
    ValueError raise path.
    """
    path = pathlib.Path(compiled_path) if compiled_path else None
    if path is None or not path.exists():
        # Preserve the old "all 4 constraint keys present with None" shape
        # that downstream consumers of `constraints_active` expect.
        from scripts.schemas.strategy import HardConstraints
        return None, HardConstraints().to_mapping(), None, []
    doc = _load_compiled_typed(path)
    assert doc is not None  # path.exists() guard above
    return doc, doc.hard_constraints.to_mapping(), doc.source_hash, \
        list(doc.soft_principles)


def _verify_source_hash(
    strategy_path: pathlib.Path,
    compiled_path: pathlib.Path,
    allow_stale: bool = False,
) -> None:
    """Raise ValueError on hash mismatch unless allow_stale=True.

    Mismatch means the user edited strategy.yaml but did not recompile
    (via the /portfolio skill's compile stage). Writing a decision log
    against stale constraints corrupts the audit trail: `constraints_active`
    in decisions.json would reference principles the user has since
    deleted or changed.

    Hash formula matches SKILL.md §Step 1 (compile stage):
        sha256(json.dumps(principles, ensure_ascii=False).encode()).hexdigest()

    If either file is missing, we skip the check (initial setup, or
    environments without a compiled file). Tests bypass via allow_stale
    where the hash drift is explicit.
    """
    if not strategy_path.exists() or not compiled_path.exists():
        return
    with open(strategy_path, encoding="utf-8") as f:
        src = yaml.safe_load(f) or {}
    principles = src.get("principles", []) or []
    expected = hashlib.sha256(
        json.dumps(principles, ensure_ascii=False).encode()
    ).hexdigest()

    # Load via typed contract. Schema errors are NOT bypassed by
    # allow_stale — that flag is scoped to "stale principles", not
    # "corrupted file". A corrupted compiled file should always surface.
    compiled = _load_compiled_typed(compiled_path)
    if compiled is None:
        return
    got = compiled.source_hash
    if got != expected:
        msg = (
            f"strategy.compiled.yaml source_hash={got!r} does not match "
            f"strategy.yaml current principles hash={expected!r} — "
            f"recompile required. Re-run /portfolio to regenerate, or "
            f"pass --allow-stale-constraints to bypass (not recommended; "
            f"decision log will reference stale principles)."
        )
        if allow_stale:
            print(f"WARNING: {msg}", file=sys.stderr)
            return
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_DECISION_ACTION_ORDER = {"exit": 0, "reduce": 1, "add": 2, "buy": 3, "hold": 4, "skip": 5}


def _sort_decisions(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        decisions,
        key=lambda d: (
            _DECISION_ACTION_ORDER.get(d.get("action"), 99),
            d.get("ticker") or "",
        ),
    )


def _sort_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Secondary key ticker keeps ordering stable when sequence is missing.
    return sorted(
        orders,
        key=lambda o: (
            o.get("sequence") if isinstance(o.get("sequence"), int) else 9_999,
            o.get("ticker") or "",
        ),
    )


def _sort_follow_ups(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda x: (x.get("date") or "", x.get("ticker") or "", x.get("event") or ""),
    )


def cmd_write(args: argparse.Namespace) -> int:
    from scripts.cli_utils import write_pair_atomic
    from scripts.delta.calendar import today_et

    # Validate output_dir format — enforces the SKILL.md contract that
    # decisions live under reports/portfolio/{YYYYMMDD}/ so `cmd_review`
    # (which scans `*/decisions.json`) can locate the run. A flat
    # `reports/portfolio/decisions.json` would be invisible to review
    # forever; fail-closed rather than leak silent data loss.
    out_dir = pathlib.Path(args.output_dir)
    # Must be an 8-digit name AND parseable as a real calendar date —
    # rejects pattern-matching sentinels like "00000000" / "99999999"
    # that otherwise satisfy a bare `\d{8}` fullmatch.
    name = out_dir.name
    name_valid = bool(re.fullmatch(r"\d{8}", name))
    if name_valid:
        try:
            dt.date.fromisoformat(f"{name[:4]}-{name[4:6]}-{name[6:]}")
        except ValueError:
            name_valid = False
    if not name_valid:
        print(
            f"portfolio_log: --output-dir must be a valid YYYYMMDD date "
            f"(got {name!r} from {args.output_dir!r}); required by "
            f"reports/portfolio/{{YYYYMMDD}}/ contract.",
            file=sys.stderr,
        )
        return 2

    # HIGH-16: verify strategy.yaml ↔ strategy.compiled.yaml source_hash
    # before any analysis reads the compiled constraints. A stale
    # compiled file would cause `constraints_active` to misrepresent the
    # user's current principles — exactly the audit-trail corruption
    # the decision log is meant to prevent. --allow-stale-constraints is
    # an explicit escape hatch for emergencies only.
    # Prefer cwd-local strategy.yaml when present (test-injection pattern
    # + normal project-root invocation both land here), fall back to
    # __file__-anchored path for cross-platform cwd-independence.
    _cwd_strategy = pathlib.Path("strategy.yaml")  # fail-open-ok: cwd-first for test-injection
    _root_strategy = pathlib.Path(__file__).resolve().parent.parent / "strategy.yaml"
    _strategy_path = _cwd_strategy if _cwd_strategy.exists() else _root_strategy
    try:
        _verify_source_hash(
            _strategy_path,
            pathlib.Path(args.constraints),
            allow_stale=getattr(args, "allow_stale_constraints", False),
        )
    except (ValueError, yaml.YAMLError, OSError) as exc:
        # ValueError: hash mismatch + SchemaError (ValueError subclass).
        # yaml.YAMLError / OSError: malformed file or unreadable path
        # (e.g. --constraints "" resolves to cwd → IsADirectoryError).
        # All three fail-close via the same return-code channel.
        print(f"portfolio_log write: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    blob = _read_json(pathlib.Path(args.decisions_blob))
    state = _read_yaml(pathlib.Path(args.state))
    macro = _read_json(pathlib.Path(args.macro))
    prices = macro.get("ticker_prices") or {}

    # Shape-validate the blob before any enrichment — replaces the old
    # `d["ticker"]` KeyError path with a structured diagnostic + exit 2.
    shape_errors = _validate_blob_shape(blob)
    if shape_errors:
        print("portfolio_log: decisions blob schema errors:", file=sys.stderr)
        for e in shape_errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    # compute programmatic parts
    portfolio_before = _compute_portfolio_before(state, prices)
    compact_macro = _compact_macro(macro)

    # Fail-closed when any holding lacks a live price — silently dropping
    # a position corrupts total_equity and constraint math.
    if portfolio_before.get("missing_prices"):
        missing = portfolio_before["missing_prices"]
        print(
            f"portfolio_log: ERROR — {len(missing)} holding(s) have no price "
            f"in macro.ticker_prices: {missing}",
            file=sys.stderr,
        )
        print(
            "  Positions without a price are excluded from total_equity and "
            "produce meaningless pnl/weight. Fix macro.json or remove the "
            "position from portfolio-state.yaml, then re-run.",
            file=sys.stderr,
        )
        return 2

    # HIGH-14: fail-closed when any holding lacks a 'shares' field.
    # Silently defaulting to 0 would make the position invisible in the
    # snapshot and in weight/PnL math — indistinguishable from sold-out.
    if portfolio_before.get("missing_shares"):
        missing = portfolio_before["missing_shares"]
        print(
            f"portfolio_log: ERROR — {len(missing)} holding(s) have no "
            f"'shares' field in portfolio-state.yaml: {missing}",
            file=sys.stderr,
        )
        print(
            "  Positions without a share count cannot be valued. Add the "
            "'shares' field (integer) or remove the ticker from "
            "portfolio-state.yaml, then re-run.",
            file=sys.stderr,
        )
        return 2

    # Enforce decide.md contract: every holding + watchlist ticker must
    # have a decisions[] entry. Uncovered tickers are audit holes.
    missing, extra = _validate_decision_coverage(blob, portfolio_before)
    if missing:
        print(
            f"portfolio_log: ERROR — decisions[] missing {len(missing)} required "
            f"ticker(s): {missing}. decide.md: every holding + watchlist ticker "
            f"must appear in decisions[] (including hold/skip).",
            file=sys.stderr,
        )
        return 2
    if extra:
        print(
            f"portfolio_log: WARN — decisions for tickers not in portfolio: "
            f"{extra} (kept but worth verifying)",
            file=sys.stderr,
        )

    from scripts.schemas import SchemaError
    try:
        compiled_doc, hard, src_hash, all_principles_from_doc = \
            _prepare_compiled_context(args.constraints)
    except (SchemaError, yaml.YAMLError, OSError) as exc:
        # SchemaError (ValueError subclass): typed contract violation.
        # yaml.YAMLError: unparseable yaml.
        # OSError: unreadable path (IsADirectoryError if --constraints "",
        # PermissionError, etc.). All fail-close via return code.
        print(f"portfolio_log: strategy.compiled.yaml invalid: "
              f"{type(exc).__name__}: {exc}. "
              "Recompile via /portfolio skill.", file=sys.stderr)
        return 2
    constraints_active = dict(hard)
    constraints_active["source_hash"] = src_hash

    decisions = _enrich_decisions(blob.get("decisions", []), portfolio_before)
    orders = _enrich_orders(blob.get("orders_proposed", []) or [], prices)

    stress = None
    if args.stress_test and pathlib.Path(args.stress_test).exists():
        v = _read_json(pathlib.Path(args.stress_test))
        stress = v.get("stress_test")
        if stress:
            stress["hard_constraint_violations"] = len(v.get("violations", []))

    # Normalize empty/invalid stress ({}, [], "") to None so it is treated
    # uniformly as "absent" by both the warning below and the MD renderer
    # (renderer guards with `if st:` and would otherwise emit an empty table).
    if not stress:
        stress = None

    # Fail loud, not silent: a log with proposed orders but no stress test
    # means the orders were NOT cash-survivability checked in this record.
    # This fires when --stress-test was omitted (e.g. a Step-7 re-run forgot
    # the flag) or the validator output was missing/empty/invalid. Producer-
    # consumer rule #4 — missing safety data is a warning, never a silent skip.
    if orders and stress is None:
        print(
            f"portfolio_log: WARNING — wrote decision log with {len(orders)} "
            "proposed order(s) but NO stress_test. Orders were NOT "
            "stress-checked in this record; re-run with "
            "--stress-test <validate.py --output>.",
            file=sys.stderr,
        )

    # principle audit
    cited_strings = [d.get("principle_cited") for d in decisions if d.get("principle_cited")]
    cited_tags = _principle_tags(cited_strings)
    all_principles = all_principles_from_doc
    # map '#1', '#2' … to principle strings by order
    all_tags = [f"#{i+1}" for i in range(len(all_principles))]
    not_cited = [t for t in all_tags if t not in cited_tags]
    principle_audit = {
        "cited_this_run": cited_tags,
        "not_cited_this_run": not_cited,
    }
    if blob.get("principle_audit_interpretation"):
        principle_audit["interpretation"] = blob["principle_audit_interpretation"]

    # Date: ET-day to match reports/portfolio/{YYYYMMDD}/ dir naming
    # (spec §11). UTC date can drift by one calendar day.
    today = today_et()
    now_utc = dt.datetime.now(dt.timezone.utc)
    date = today.isoformat()
    run_id = now_utc.strftime("%Y%m%dT%H%M%SZ")

    follow_ups_in = blob.get("follow_ups", []) or []
    kept_fus, dropped_fus = _sanitize_follow_ups(follow_ups_in, date)
    if dropped_fus:
        print(
            f"portfolio_log: dropped {len(dropped_fus)} past/malformed follow_up(s):",
            file=sys.stderr,
        )
        for reason in dropped_fus:
            print(f"  - {reason}", file=sys.stderr)

    log = {
        "run_id": run_id,
        "date": date,
        "skill_version": SKILL_VERSION,
        "status": "proposed",
        "portfolio_before": portfolio_before,
        "macro": compact_macro,
        "constraints_active": constraints_active,
        "decisions": _sort_decisions(decisions),
        "orders_proposed": _sort_orders(orders),
        "stress_test": stress,
        "follow_ups": _sort_follow_ups(kept_fus),
        "principle_audit": principle_audit,
        "notes": blob.get("notes", []),
        "candidate_scan": blob.get("candidate_scan"),
        "user_confirmation": {
            "status": "pending",
            "accepted_orders": [],
            "rejected_orders": [],
            "modified_orders": [],
            "decision_notes": None,
            "confirmed_at": None,
        },
        "execution_outcomes": {
            "orders_filled": [],
            "orders_unfilled": [],
            "actual_proceeds": None,
            "actual_cost": None,
            "reflection": None,
        },
    }

    # Render MD BEFORE any persistence — a render exception leaves no
    # partial JSON on disk. Both files then land via atomic tmp+rename
    # through cli_utils helpers (matches run_meta.save convention).
    md_text = _render_md(log)

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "decisions.json"
    md_path = out_dir / "decisions.md"
    # Pair write: stages both tmps first, replaces JSON (canonical) then
    # MD. Per-file atomic; pair is not strictly atomic across an external
    # observer between the two replaces — see write_pair_atomic docstring.
    write_pair_atomic(log, str(json_path), md_text, str(md_path))

    print(f"portfolio_log: wrote {json_path}")
    print(f"portfolio_log: wrote {md_path}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from scripts.delta.calendar import today_et
    # Use ET trading day to match run-dir naming + delta-layer semantics
    # (spec §11). dt.date.today() is local-tz and drifts a day in Asia
    # timezones vs the reports/portfolio/{YYYYMMDD}/ naming convention.
    today = args.today or today_et().isoformat()
    reports_dir = pathlib.Path(args.reports_dir)
    if not reports_dir.exists():
        print(f"portfolio_log: no reports dir {reports_dir}", file=sys.stderr)
        return 0

    # find most recent prior-day decisions.json (exclude today)
    today_flat = today.replace("-", "")
    candidates = sorted(
        [p for p in reports_dir.glob("*/decisions.json") if p.parent.name != today_flat],
        reverse=True,
    )
    if not candidates:
        print("portfolio_log: no prior decisions.json found — first run.")
        return 0

    prior = candidates[0]

    # Schema gate: validate prior decisions.json through the typed loader
    # BEFORE consuming. decisions.json is persisted state that cascades to
    # future runs (review summary, follow-ups, execution outcomes). Schema
    # drift here would silently corrupt review output, defeating the
    # purpose of a typed contract.
    #
    # Per codex review 2026-05-22: original implementation warned and
    # fell through to raw read, which bypassed the gate on the exact
    # drift path it was meant to block. Now fail-close.
    try:
        from scripts.schemas.decisions import load_decisions
        load_decisions(prior)  # raises SchemaError if drifted
    except Exception as e:
        print(
            f"portfolio_log: FATAL — prior decisions.json failed schema "
            f"validation: {e}",
            file=sys.stderr,
        )
        print(
            f"  File: {prior}",
            file=sys.stderr,
        )
        print(
            "  This typically means the schema was updated but the prior log "
            "predates the change. Either:",
            file=sys.stderr,
        )
        print(
            "  (a) hand-fix the prior log to match current schema, or",
            file=sys.stderr,
        )
        print(
            "  (b) move/rename the prior file so resolver finds an older "
            "compatible run.",
            file=sys.stderr,
        )
        return 1

    log = _read_json(prior)
    print(f"portfolio_log: prior run {log.get('run_id')} ({log.get('date')}) — {prior}")
    print(f"  status: {log.get('status')}")
    confirm = (log.get("user_confirmation") or {}).get("status")
    print(f"  user_confirmation: {confirm}")

    # due follow-ups
    due: List[Dict[str, Any]] = []
    for fu in log.get("follow_ups") or []:
        if fu.get("date") and fu["date"] <= today:
            due.append(fu)
    if due:
        print(f"\n  {len(due)} follow-up(s) due (date ≤ {today}):")
        for fu in sorted(due, key=lambda x: x.get("date", "")):
            print(f"    [{fu['date']}] {fu.get('ticker', '—'):<7} {fu.get('event', '')}")
            if fu.get("what_to_watch"):
                print(f"              -> watch: {fu['what_to_watch']}")
    else:
        print("\n  No follow-ups due.")

    # unfilled execution outcomes flag
    outcomes = log.get("execution_outcomes") or {}
    if outcomes.get("reflection") is None and confirm in ("accepted", "modified"):
        print(f"\n  ⚠ Prior run has no reflection recorded. Consider filling")
        print(f"    {prior}:execution_outcomes.reflection before overwriting today.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Portfolio decision log tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="Write decisions.json + decisions.md")
    w.add_argument("--decisions-blob", required=True, help="LLM-authored blob JSON path")
    w.add_argument("--state", required=True, help="portfolio-state.yaml path")
    w.add_argument("--macro", required=True, help="macro.json path")
    w.add_argument("--constraints", default="strategy.compiled.yaml")
    w.add_argument("--stress-test", default=None, help="validate.py output JSON")
    w.add_argument("--output-dir", required=True)
    w.add_argument(
        "--allow-stale-constraints",
        action="store_true",
        help=(
            "Bypass source_hash verification (emergency only — "
            "decisions log will reference stale principles)."
        ),
    )
    w.set_defaults(func=cmd_write)

    r = sub.add_parser("review", help="Review prior run's follow-ups")
    r.add_argument("--reports-dir", default="reports/portfolio")
    r.add_argument("--today", default=None, help="YYYY-MM-DD (default: today)")
    r.set_defaults(func=cmd_review)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
