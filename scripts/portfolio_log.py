"""Portfolio decision log — persist confirmed /portfolio decisions.

Two subcommands:

  write    Merge LLM-authored decision blob with programmatic snapshots
           (portfolio, macro, thesis metadata, stress test) and write
           decisions.json + decisions.md to the output directory.

  review   Scan the most recent prior run's decisions.json (including a
           same-day earlier run) and print follow-up items whose date has
           arrived. Used by the portfolio skill at Step 0 to cross-check
           prior flagged catalysts.

The decision log is the durable artifact behind /portfolio — it is the
only file that survives between runs and supports audit/reflection.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
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
# Feedback 2026-06-11 #7: an AUTHOR-DECLARED `[context-only]` marker in the
# rationale downgrades the WARN to INFO — suppression stays auditable (the
# marker is in the logged rationale) without re-attempting clause parsing.
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
    elif (thesis_dir / "bq_analysis.json").exists():
        # Probe-2 review round-12: when the LATEST BQ is newer than the
        # selected thesis, the thesis dir's own paired bq_analysis.json is
        # the matching vintage — previously this state silently dropped
        # the bq field from the durable snapshot (recorded as plain
        # no_path) although the matching BQ sat on disk.
        bq_path = thesis_dir / "bq_analysis.json"

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
        # Probe-2 review round-13: the structured integrity state that
        # GATED the recommendation (portfolio-decide reads
        # degraded_categories / validation_status as the entry/add gate)
        # must survive into the durable record. None values are dropped by
        # the trailing None-strip below.
        "bq_validation_status": (bq_doc.meta.validation_status
                                 if bq_doc is not None else None),
        "bq_degraded_categories": (list(bq_doc.meta.degraded_categories)
                                   if bq_doc is not None else None),
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
    #     emit); WARN loudly + trust BQ's view (the write PROCEEDS — this
    #     is fail-VISIBLE, not fail-close; see _cert_authoritative_or_none)
    #   - Both have certs but different source_currency or fx_source →
    #     WARN loudly (cross-layer drift surfaced) + persist BQ's view
    # NOTE (whole-project review 2026-06-11 C16): an earlier version of
    # this comment said "FAIL-CLOSE" for the last two cases; the code has
    # always warned-and-proceeded with BQ authority. Do not "restore" a
    # refusal here without an RFC — existing runs depend on the
    # warn-and-trust-BQ semantics.
    snap["dl3c_mode"] = thesis.dl3c_mode

    def _cert_authoritative_or_none():
        """Return (mode, cert_obj_or_None) to persist.

        Authority: bq (producer-side) wins on every disagreement; drift is
        surfaced via [WARN] on stderr while the write proceeds with BQ's
        view. There is NO raise/refusal path in this reconciliation.

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

    Clock-anchored inputs (feedback 2026-06-11 #2): when macro.json
    carries `regime_inputs` (post-fix macro.py), classification uses
    ONLY that block — every value anchored to the last completed ET
    session, so a live pre-market VIX can never be compared against
    prior-close indices (the mix that flipped risk_off to risk_on on
    2026-06-11). Legacy macro.json (no block) keeps the old path
    byte-identical for historical artifacts and replay.
    """
    indices = ("SPY", "QQQ", "^DJI")
    above_ma50 = 0
    missing_indices: List[str] = []
    anchor_note = ""
    # Round-20 F2: a numeric 0.0 close (provider missing-value drift) is
    # NOT a price — it read as "subdued VIX" / "index below MA50" and
    # flipped the deterministic regime. Non-finite-positive = missing.
    from scripts.indicators import _fin_pos
    ri = macro.get("regime_inputs")
    if isinstance(ri, dict):
        ri_indices = ri.get("indices") or {}
        for idx in indices:
            m = ri_indices.get(idx) or {}
            p, ma50 = m.get("close"), m.get("ma50")
            if not _fin_pos(p) or not _fin_pos(ma50):
                missing_indices.append(idx)
                continue
            if p > ma50:
                above_ma50 += 1
        ri_vix = ri.get("vix") or {}
        vix, vix_ma20 = ri_vix.get("close"), ri_vix.get("ma20")
        if ri.get("anchor_session"):
            anchor_note = f" (inputs anchored to {ri['anchor_session']} close)"
    else:
        market = macro.get("market", {})
        vol = macro.get("volatility", {})
        for idx in indices:
            m = market.get(idx, {})
            p, ma50 = m.get("price"), m.get("ma50")
            if not _fin_pos(p) or not _fin_pos(ma50):
                missing_indices.append(idx)
                continue
            if p > ma50:
                above_ma50 += 1
        vix = vol.get("vix")
        vix_ma20 = vol.get("vix_ma20")
    vix_unknown = not _fin_pos(vix) or not _fin_pos(vix_ma20)
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
    return regime, note + anchor_note


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
            # Key-presence alone missed `shares: null` (key present, value
            # None) — the same invisible-position hazard via a different
            # spelling (whole-project review 2026-06-11 C2).
            if h.get("shares") is None:
                missing_shares.append(t)
                shares = None
            else:
                shares = h["shares"]
        elif h is None:
            # bare `TICKER:` holding — no usable share count
            missing_shares.append(t)
            shares = None
        else:
            shares = h
        cb = h.get("cost_basis") if isinstance(h, dict) else None
        px = prices.get(t)
        # Round-21 F1: negative/NaN/Inf/bool prices are provider bugs, not
        # prices — a -1.0 quote produced market_value=-10 / pnl=-102% with
        # missing_prices=[]. Explicit 0 stays legitimate (delisted-at-zero,
        # test_h1_zero_price_legitimate_preserved).
        if (px is not None
                and (isinstance(px, bool)
                     or not isinstance(px, (int, float))
                     or not math.isfinite(px) or px < 0)):
            px = None
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

    out = {
        "spy": _mk("SPY"),
        "qqq": _mk("QQQ"),
        "dji": _mk("^DJI"),
        **vix_obj,
        "rates": r or None,
        "regime": regime,
        "regime_interpretation": note,
    }
    # Pass the anchored regime inputs through so decisions.json explains
    # its own regime classification (feedback 2026-06-11 #2).
    if isinstance(macro.get("regime_inputs"), dict):
        out["regime_inputs"] = macro["regime_inputs"]
    # rates_status travels WITH the rates it qualifies — a stale-disk
    # PARTIAL (live fetch failed, cache beyond the freshness bound) or
    # FAILED must be visible in the durable decision log, not dropped at
    # compaction (codex cold-round 9; same lesson as historical_multiples'
    # status downgrade: a signal without a reader is not a fix).
    if isinstance(macro.get("rates_status"), dict):
        out["rates_status"] = macro["rates_status"]
    # Persist what a rationale may cite, so the durable log can verify its own
    # claims. Pre-fix, every RSI/volume number in decisions.json was
    # uncheckable because ticker_indicators was dropped at compaction.
    for _k in ("ticker_price_structure", "universe_rebound_structure",
               "ticker_indicators"):
        if isinstance(macro.get(_k), dict):
            out[_k] = macro[_k]
    return out


def _principle_audit_split(decisions, all_principles) -> tuple:
    """The `(cited, not_cited)` split the decision log persists.

    ONE implementation, per `.claude/rules/producer-consumer.md` #3: `cmd_write`
    persists this and `cmd_audit` previews it, and a preview that can drift
    from the write it previews is worse than no preview — the author would
    read the green as clearance. Callers pass whichever decision list they
    hold; `_enrich_decisions` copies `principle_cited` verbatim and preserves
    order, so the enriched and raw lists give the same answer.
    """
    cited = _principle_tags(
        [d.get("principle_cited") for d in decisions
         if isinstance(d, dict) and d.get("principle_cited")])
    all_tags = [f"#{i + 1}" for i in range(len(all_principles))]
    return cited, [t for t in all_tags if t not in cited]


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

def _emit_blob_advisories(blob: Dict[str, Any]) -> None:
    """Print the blob's ADVISORY lines — findings that inform but never refuse.

    Separate from `_validate_blob_shape` because that function is a pure error
    COLLECTOR and `cmd_write` calls it twice: once up front on shape, once more
    on principle-index range after strategy.compiled.yaml is loaded. With the
    prints inside it, every advisory doubled — the field caught the
    `[context-only]` INFO twice and went looking for a second ADBE decision
    (feedback 2026-09-02 portfolio (3)); the zero-order `candidate_scan` WARN
    doubled with it. De-duplicating one message would have left the next call
    site free to reintroduce the whole class, so the emission moved out instead.

    Call ONCE per run, after the shape pass has already refused a malformed
    blob — these read fields a shape error would have flagged.
    """
    decisions = blob.get("decisions")
    for d in decisions if isinstance(decisions, list) else []:
        if not isinstance(d, dict):
            continue
        rationale_text = d.get("rationale") if isinstance(d.get("rationale"), str) else ""
        if d.get("action") in ("skip", "buy") and _VALUATION_GATE_RE.search(rationale_text):
            if "[context-only]" in rationale_text.lower():
                # Feedback 2026-06-11 #7: author-declared context citation
                # (decide.md documents the marker). Keep it VISIBLE but stop
                # crying wolf — the marker sits in the logged rationale, so
                # every suppression is auditable. NOT a regex clause-
                # discriminator (tried and removed as unsound).
                print(
                    f"[INFO] portfolio_log: {d.get('ticker', '?')} {d.get('action')} "
                    f"rationale carries a valuation/ER term marked [context-only] — "
                    f"lint suppressed.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[WARN] portfolio_log: {d.get('ticker', '?')} {d.get('action')} rationale "
                    f"carries a valuation/ER term — per portfolio-decide.md the entry gate is "
                    f"technical/principle, not valuation. Confirm the disqualifier is a run-day "
                    f"technical condition (RSI/pct_b/volume), not richness; if the ER mention "
                    f"is background context only, append [context-only] to that clause.",
                    file=sys.stderr,
                )

    # Zero-order discipline. Raw orders check (NOT _as_list): a malformed
    # non-list orders_proposed is a shape ERROR and must NOT also be read here
    # as "zero orders". Shape check is intentionally shallow (dict + non-empty
    # summary + near_misses-is-list): a deeper validator would re-create the
    # over-gating this guard avoids.
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


def _validate_blob_shape(
    blob: Dict[str, Any],
    soft_principles: Optional[List[str]] = None,
) -> List[str]:
    """Check the LLM-authored decisions blob for required fields + enum
    vocabularies per prompts/portfolio-decide.md. Returns error strings.

    Called up-front in cmd_write so a malformed blob never reaches the
    enrichment / render / persist pipeline. Replaces the prior path
    where `d["ticker"]` would KeyError mid-pipeline with no diagnostic.

    HIGH-17: additionally enforces rationale + principle_cited on decisions,
    sequence/type/linked_decision on orders, and ticker/event/what_to_watch
    on follow_ups — fields required by prompts/portfolio-decide.md that
    were previously unchecked and could slip through into the log.

    Closing round-38: when `soft_principles` (the compiled principle list)
    is supplied, every clause-leading `#N` tag in principle_cited must be
    within [1, len(soft_principles)] — an LLM indexing typo like "#99"
    otherwise renders in decisions.md as an authoritative citation and
    lands in the persisted cited_this_run audit. cmd_write runs this
    second pass right after loading strategy.compiled.yaml.
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
    # Probe-2 review round-9a: ONE decision per ticker. The decide.md
    # contract is one entry per ticker; conflicting duplicates (exit AND
    # hold for the same position) previously persisted into the canonical
    # log and downstream review state.
    _seen_decision_tickers: Dict[str, str] = {}
    for i, d in enumerate(_as_list("decisions")):
        if not isinstance(d, dict):
            continue
        t = d.get("ticker")
        if not isinstance(t, str):
            continue
        if t in _seen_decision_tickers:
            errors.append(
                f"decisions[{i}]: duplicate decision for {t!r} "
                f"(earlier action={_seen_decision_tickers[t]!r}, this "
                f"action={d.get('action')!r}) — one decision per ticker"
            )
        else:
            _seen_decision_tickers[t] = str(d.get("action"))

    # Probe-2 review round-9b: bind each order's DIRECTION to its linked
    # decision's action. An `exit` decision beside a linked BUY order
    # previously persisted (and validate.py sees only orders, never
    # decisions). Mapping: buy/add decisions may link buy orders;
    # reduce/exit decisions may link sell orders; hold/skip decisions may
    # link no orders at all.
    _DECISION_TO_ORDER_ACTIONS = {
        "buy": {"buy"}, "add": {"buy"},
        "reduce": {"sell"}, "exit": {"sell"},
        "hold": set(), "skip": set(),
    }
    _decision_action_by_ticker = {
        d.get("ticker"): d.get("action")
        for d in _as_list("decisions") if isinstance(d, dict)
    }

    from scripts.schemas.decisions import MAX_ORDER_SHARES
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
        # round-9b direction binding (see map above the loop);
        # round-10: the linked TICKER must equal the ORDER's ticker (a
        # cross-ticker link laundered a contradicting order past the
        # direction gate), and the bare-ticker link form ("AMD", no
        # action suffix) resolves through the same direction map instead
        # of bypassing it.
        linked = o.get("linked_decision")
        if isinstance(linked, str) and linked:
            if "." in linked:
                l_ticker, _, l_action = linked.rpartition(".")
            else:
                l_ticker, l_action = linked, None
            o_ticker = o.get("ticker")
            if isinstance(o_ticker, str) and l_ticker != o_ticker:
                errors.append(
                    f"orders_proposed[{i}] ({o_ticker}): "
                    f"linked_decision={linked!r} references a DIFFERENT "
                    f"ticker ({l_ticker!r}) — an order must link its own "
                    f"ticker's decision"
                )
            d_action = _decision_action_by_ticker.get(l_ticker)
            allowed = _DECISION_TO_ORDER_ACTIONS.get(str(d_action))
            if (d_action is not None and l_action is not None
                    and l_action != d_action):
                errors.append(
                    f"orders_proposed[{i}] ({o.get('ticker', '?')}): "
                    f"linked_decision={linked!r} action suffix "
                    f"{l_action!r} does not match the {l_ticker} "
                    f"decision's action {d_action!r}"
                )
            if (allowed is not None and action in ORDER_ACTIONS
                    and action not in allowed):
                errors.append(
                    f"orders_proposed[{i}] ({o.get('ticker', '?')}): "
                    f"order action {action!r} contradicts linked decision "
                    f"{l_ticker}.{d_action} (allowed order actions: "
                    f"{sorted(allowed) or 'none — hold/skip links no orders'})"
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
                (isinstance(shares, float) and not math.isfinite(shares)) or
                shares <= 0 or shares >= MAX_ORDER_SHARES):
                errors.append(
                    f"orders_proposed[{i}] ({o.get('ticker', '?')}): "
                    f"shares={shares!r} must be a positive finite number "
                    f"below {MAX_ORDER_SHARES:.0e} "
                    f"(int or float for fractional shares)"
                )
        # PRESENT-but-invalid price fields fail the write gate (codex
        # cold-round 8): an est_price of inf would pass into decisions.json
        # while the validator costs the order at the finite quote — a
        # contradictory audit record. Absent/null keys stay legal.
        for pkey in ("est_price", "limit_price", "stop_price", "price"):
            pval = o.get(pkey)
            if pkey in o and pval is not None:
                if (isinstance(pval, bool) or
                    not isinstance(pval, (int, float)) or
                    (isinstance(pval, float) and not math.isfinite(pval)) or
                    pval <= 0 or pval >= MAX_ORDER_SHARES):
                    errors.append(
                        f"orders_proposed[{i}] ({o.get('ticker', '?')}): "
                        f"{pkey}={pval!r} must be a positive finite number "
                        f"below {MAX_ORDER_SHARES:.0e} when present"
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
    # Closing round-38: principle-index range check (second pass — needs the
    # compiled principle list, which cmd_write loads AFTER the up-front shape
    # call). Tags are extracted by the SAME clause-leading parser the audit
    # uses (_principle_tags), so what gets validated is exactly what gets
    # credited/rendered.
    #
    # An EMPTY compiled list SKIPS the check — there is nothing to bind to
    # (legacy artifacts, and default-principles configs); the audit section
    # already reports zero valid tags for those.
    #
    # A refusal was tried here and REVERTED. The compiler now emits
    # `soft_principles: []` for a null/absent/[] `principles` source, and
    # SKILL.md Step 2 is required to fill it with the resolved canonical
    # defaults — so an empty list at this point does mean the log cannot
    # attest which policy authored the decisions. But turning the skip into a
    # refusal blocks every LEGACY artifact too, and ~50 existing tests encode
    # "empty soft_principles is a legitimate state". The Step 2 requirement is
    # the weaker sufficient layer; this would be a second one that breaks
    # working configurations to enforce it.
    if soft_principles:
        valid_n = len(soft_principles)
        for i, d in enumerate(_as_list("decisions")):
            if not isinstance(d, dict):
                continue
            v = d.get("principle_cited")
            if not isinstance(v, str):
                continue
            for tag in _principle_tags([v]):
                try:
                    n = int(tag.lstrip("#"))
                except ValueError:
                    continue
                if not (1 <= n <= valid_n):
                    errors.append(
                        f"decisions[{i}] ({d.get('ticker', '?')}): "
                        f"principle_cited tag {tag} is out of range — "
                        f"strategy.compiled.yaml defines #1..#{valid_n}"
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


def _validate_decision_evidence(blob, macro, *, constraints):
    """Fact-vs-fact cross-checks; returns (errors, warnings). Errors refuse
    the write; warnings print and never block. Everything deliberately NOT
    checked is listed in the plan's §Honest enforcement boundary."""
    import math as _math
    from scripts.schemas.decisions import (
        CLOSING_HIGH_STATUS_VALUES, LENS_VALUES, PATH_STATUS_VALUES)

    def _finite(x):
        return (isinstance(x, (int, float)) and not isinstance(x, bool)
                and _math.isfinite(x))

    errs, warns = [], []
    tps = macro.get("ticker_price_structure")
    tps = tps if isinstance(tps, dict) else None
    urs = macro.get("universe_rebound_structure")
    urs = urs if isinstance(urs, dict) else {}
    members = urs.get("members")
    members = members if isinstance(members, dict) else {}
    ind = macro.get("ticker_indicators")
    ind = ind if isinstance(ind, dict) else {}

    for d in blob.get("decisions") or []:
        t = d.get("ticker")
        tag = f"decisions[{t}]"
        entry = d.get("action") in ("buy", "add")
        blk = (tps or {}).get(t)
        blk = blk if isinstance(blk, dict) else None
        truth = (blk or {}).get("closing_high_status")
        structure_ok = blk is not None and truth not in (None, "unknown")

        lens = d.get("lens_used")
        if lens is not None and (not isinstance(lens, str) or lens not in LENS_VALUES):
            errs.append(f"{tag}.lens_used {lens!r} not in {sorted(LENS_VALUES)}")
        if lens == "cohort_recovery":
            mem = members.get(t)
            mem = mem if isinstance(mem, dict) else {}
            if urs.get("status") != "fresh":
                errs.append(f"{tag}.lens_used=cohort_recovery but event status is "
                            f"{urs.get('status')!r} (only 'fresh' authorises it)")
            if mem.get("status") != "available" or \
                    not _finite(mem.get("pct_since_cohort_trough")):
                errs.append(f"{tag}.lens_used=cohort_recovery but members[{t}] is "
                            f"not an available finite record")
        elif lens in ("rs_vs_spy", "rs_vs_qqq"):
            key = "rs_vs_spy_3m" if lens == "rs_vs_spy" else "rs_vs_qqq_3m"
            row = ind.get(t)
            val = row.get(key) if isinstance(row, dict) else None
            if not _finite(val):
                errs.append(f"{tag}.lens_used={lens} but ticker_indicators[{t}]."
                            f"{key} is {val!r} (not a finite number)")

        for key in ("breakout_path_status", "repair_path_status"):
            v = d.get(key)
            if v is not None and (not isinstance(v, str) or v not in PATH_STATUS_VALUES):
                errs.append(f"{tag}.{key} {v!r} not in {sorted(PATH_STATUS_VALUES)}")
            if entry and v is None:
                errs.append(f"{tag}.{key} is required on action={d.get('action')!r}")
        _bps, _rps = d.get("breakout_path_status"), d.get("repair_path_status")
        if entry and isinstance(_bps, str) and _bps in PATH_STATUS_VALUES \
                and isinstance(_rps, str) and _rps in PATH_STATUS_VALUES \
                and "qualified" not in (_bps, _rps):
            errs.append(f"{tag}: entry requires at least one path status "
                        f"'qualified' — both are non-qualified, which is "
                        f"internally contradictory")

        obs = d.get("observed_closing_high_status")
        if obs == "unknown":
            errs.append(f"{tag}.observed_closing_high_status='unknown' is "
                        f"meaningless transcription — omit the field instead")
        if obs is not None and (not isinstance(obs, str) or obs not in CLOSING_HIGH_STATUS_VALUES):
            errs.append(f"{tag}.observed_closing_high_status {obs!r} not in "
                        f"{sorted(CLOSING_HIGH_STATUS_VALUES)}")

        if entry and blk is None:
            errs.append(f"{tag}: action={d.get('action')!r} is "
                        f"blocked_by_data_integrity — ticker_price_structure is "
                        f"missing for {t}")
            continue
        if entry and blk.get("anchor_session_covered") is not True:
            errs.append(f"{tag}: action={d.get('action')!r} is "
                        f"blocked_by_data_integrity — anchor session not covered "
                        f"for {t} (stale/halted series)")
            continue
        # PATH-SENSITIVE integrity: the strategy's two entry paths are
        # parallel, and repair explicitly does not require a new high — an
        # incomplete breakout-high history must not block a repair entry.
        # The producer emits a concrete closing_high_status ONLY under a
        # complete lookback or proven inception, so the fact-check below
        # subsumes the old lookback/inception conjunct.
        if entry and d.get("breakout_path_status") == "qualified" \
                and truth != "breakout":
            errs.append(f"{tag}: breakout_path_status='qualified' contradicts "
                        f"macro closing_high_status={truth!r} (a breakout claim "
                        f"requires a proven closing-basis breakout)")
        if entry and obs is None and truth not in (None, "unknown"):
            errs.append(f"{tag}.observed_closing_high_status is required on "
                        f"entries when the macro status is concrete")
        if obs is None and truth == "breakout":
            warns.append(f"{tag}: macro reports closing_high_status='breakout' but "
                         f"the decision does not transcribe "
                         f"observed_closing_high_status (prompt requires it; not "
                         f"blocking)")
        if obs is not None:
            if not structure_ok:
                errs.append(f"{tag} cites price structure but none exists for {t}")
                continue
            if obs != truth:
                errs.append(f"{tag}.observed_closing_high_status={obs!r} does not "
                            f"match macro closing_high_status={truth!r}")
            if blk.get("anchor_session_covered") is not True:
                errs.append(f"{tag} cites price structure but "
                            f"anchor_session_covered is not true (stale/halted)")
    return errs, warns


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
    from scripts.validate import _order_price

    out: List[Dict[str, Any]] = []
    for src in orders:
        o = dict(src)
        ticker = o.get("ticker")
        # Canonical price extraction shared with the validator — the old
        # limit_price-or-quote chain costed an est_price-only order at the
        # live quote while scripts.validate stress-projected it at est_price,
        # so the audit record and the validation it attests to disagreed on
        # cash impact (whole-project review 2026-06-11 C15a). Since
        # 2026-08-09 the selection is direction-aware AND quote-bounded for
        # uncapped orders, so real prices MUST be passed here (never
        # display_only): a stale market-sell estimate would otherwise be
        # persisted as est_execution_price while the validator used the quote.
        px = _order_price(o, prices if ticker else {}) or None
        shares = o.get("shares") or 0  # fail-open-ok: guarded by `if px and shares:` truthy check below — shares=0 skips notional computation
        if px and shares:
            notional = round(px * shares, 2)
            # Closing round-25: ASSIGN, never setdefault — enrichment owns
            # this field. A plausible LLM-supplied est_execution_price in
            # the blob previously survived and the md rendered "@ market
            # \$90" while the safety math used the \$100 quote.
            o["est_execution_price"] = px
            if o.get("action") == "sell":
                o["est_proceeds"] = notional
            elif o.get("action") == "buy":
                o["est_cost"] = notional
        else:
            # no computable price → no enrichment claims survive either
            for _k in ("est_execution_price", "est_cost", "est_proceeds"):
                o.pop(_k, None)
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# MD rendering
# ---------------------------------------------------------------------------

def _fmt(x, dash="—"):
    return dash if x is None else x


def _render_md(log: Dict[str, Any],
               ticker_prices: Optional[Dict[str, Any]] = None) -> str:
    # 2026-08-10: the renderer had NO price access, so the working-order
    # line could not agree with the validator's projection. Optional +
    # defaulting to {} keeps every existing caller working; with no map
    # the display_only fallback below reproduces the previous output.
    ticker_prices = ticker_prices or {}
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
    _cp = pb.get('cash_pct')
    lines.append(f"总权益 **${pb['total_equity']:,.2f}** · 现金 **${pb['cash']:,.2f}**"
                 + (f" ({_cp}%)" if _cp is not None else " (—)"))
    lines.append("")
    lines.append("| Ticker | 股数 | 成本 | 现价 | 市值 | 权重 | 盈亏 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for h in pb["holdings"]:
        # Closing round-4: explicit 0 is a REAL value for both fields
        # (zero-basis position; delisted-at-zero price) — only None is
        # missing. Truthiness rendered a legitimate 0 basis as "—".
        cb = f"{h['cost_basis']:.2f}" if h.get('cost_basis') is not None else "—"
        px = f"{h['price']:.2f}" if h.get('price') is not None else "—"
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
    # Display probe 2026-08-03 F3: the header previously said MA50/vs MA50
    # while the VIX row carried MA20 data beneath it — a human read the
    # VIX regime comparison under the wrong moving-average tenor. Neutral
    # header + per-row tenor labels.
    lines.append("| 指标 | 现值 | 均线 | vs 均线 |")
    lines.append("|---|---:|---:|---:|")
    for name, d in rows:
        if d:
            # Closing round-8: a failed index feed persisted
            # `| SPY | None | MA50 None | None% |` — None is missing
            # data, render as '—' (the regime already classified it as
            # incomplete; the table must not dress it as a number).
            _p = d.get('price')
            _ma = d.get('ma50')
            _vs = d.get('vs_ma50_pct')
            lines.append(
                f"| {name} | {_p if _p is not None else '—'} | "
                f"{'MA50 ' + str(_ma) if _ma is not None else '—'} | "
                f"{str(_vs) + '%' if _vs is not None else '—'} |")
    if m.get("vix"):
        ri_vix = (m.get("regime_inputs") or {}).get("vix") or {}
        anchor = (m.get("regime_inputs") or {}).get("anchor_session")
        if ri_vix.get("close") is not None and anchor:
            vix_cell = f"{m['vix']} (anchored {ri_vix['close']} @ {anchor})"
            _a_ma20 = ri_vix.get('ma20', m.get('vix_ma20'))
            # closing round-16: anchored close can exist beside a null
            # MA20 (short history) — '—', never 'MA20 None'
            ma20_cell = f"MA20 {_a_ma20}" if _a_ma20 is not None else "—"
            # 4-angle closing round-2: the row shows the ANCHORED pair —
            # its percentage must come from the same pair, not from the
            # live quote (live 24 vs anchored 20 previously rendered an
            # anchored 20/20 row with a 20.0% column).
            _c = ri_vix.get("close")
            if (isinstance(_c, (int, float)) and not isinstance(_c, bool)
                    and isinstance(_a_ma20, (int, float))
                    and not isinstance(_a_ma20, bool) and _a_ma20 > 0):
                pct_cell = f"{round((_c - _a_ma20) / _a_ma20 * 100, 2)}%"
            else:
                pct_cell = "—"
        else:
            vix_cell = f"{m['vix']}"
            _vma = m.get('vix_ma20')
            _vpct = m.get('vix_vs_ma20_pct')
            ma20_cell = f"MA20 {_vma}" if _vma is not None else "—"
            pct_cell = f"{_vpct}%" if _vpct is not None else "—"
        lines.append(f"| VIX | {vix_cell} | {ma20_cell} | {pct_cell} |")
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
        if cur is None:
            cur = 0
        tgt = d.get("target_weight_pct")
        if tgt is None:
            tgt = cur  # closing round-16: explicit null target (hold/skip) reads as unchanged, never 'None%' 
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
        # Feedback 2026-06-11 #3b: a human reviewing the MD must see the
        # broker-side in-flight orders next to the decision they may
        # contradict (the JSON snapshot alone was reviewer-invisible).
        for oo in d.get("open_orders_snapshot") or []:
            # Canonical price-field vocabulary (C15b): an order in the
            # {type: limit, limit_price: X} shape rendered "@ ?" when this
            # line read only 'price' — the exact visibility this exists for.
            # 2026-08-10: price the line the SAME way the validator priced
            # it. An earlier revision passed display_only=True to show the
            # order's own resting price, but for an UNCAPPED working order
            # that is a stale self-report: a market buy estimated at $50
            # against a $200 quote was costed at $200 by the safety math and
            # rendered "@ 50" here — two numbers for one live broker
            # commitment. A capped order still shows its own limit (the
            # quote is not added on that branch), so only the uncapped case
            # moves. display_only survives as the FALLBACK: with no quote
            # the projection is 0 (unprojectable) and this line would
            # otherwise revert to "@ ?", the exact symptom C15b removed.
            from scripts.validate import _order_price
            oo_px = (_order_price(oo, ticker_prices)
                     or _order_price(oo, {}, display_only=True) or "?")
            lines.append(
                f"- ⚠ 在途单: {oo.get('type', '?')} {oo.get('shares', '?')}股 "
                f"@ {oo_px} ({oo.get('duration', 'GTC')})"
            )
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
            # Show the price VALIDATION used — which enrichment already
            # persisted as est_execution_price. This line previously read
            # `limit_price` first, so one audit record could carry three
            # different numbers: a market buy with a stray limit_price was
            # quote-bounded to $200 by the safety math yet advertised $50.
            # A re-derivation here was tried and dropped: it has to mirror
            # the projection's DIRECTION (max over ceilings for a buy, min
            # for a sell) or it re-introduces the very disagreement it was
            # added to fix (measured 2026-08-10: rendered $100 beside a
            # persisted $50 when the two ceiling fields disagreed). Since a
            # capped order now projects AT its ceiling, est_execution_price
            # IS the limit for those — so deferring to it is both simpler
            # and correct by construction.
            px = o.get("est_execution_price")
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
        # Cold review 2026-06-11 R2 MED-7: the coverage caveat (e.g.
        # degenerate scenarios on empty open_orders) must be visible to a
        # human reading PASS rows, not only in the JSON.
        for w in st.get("validator_warnings") or []:
            lines.append("")
            lines.append(f"- ⚠ {w}")
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
        src = yaml.safe_load(f)
    # Match the compiler: only an EMPTY document is {}. `... or {}` turned every
    # falsy root — [], false, 0, "" — into an empty policy, so a corrupted
    # strategy.yaml VERIFIED against an empty-policy artifact and a malformed
    # mid-run replacement could be persisted as valid.
    if src is None:
        src = {}
    if not isinstance(src, dict):
        raise ValueError(
            f"{strategy_path} must be a mapping, got {type(src).__name__} — "
            f"policy cannot be verified against a malformed source"
        )
    from scripts.schemas.strategy import canonical_policy_hash
    # ONE formula, shared with the compiler and the orchestration fallback
    # (scripts.schemas.strategy.canonical_policy_hash). It was previously
    # hand-matched here and in SKILL.md's inline snippet, over `principles`
    # alone — so a policy value written under `risk:` was outside the hash,
    # produced a cache hit, and this log kept attesting the superseded policy.
    # That is F4.
    expected = canonical_policy_hash(src)

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
    # Notes content comparison — RESTORED after being deleted as "subsumed by
    # the widened hash". It is NOT subsumed: the hash stringifies keys and
    # falls back to `str()` for values, so it ERASES YAML types. Verified
    # collisions: the string "2026-08-13" and an unquoted YAML date hash
    # identically, as do the keys "1" and 1. A notes edit between those shapes
    # would slip past the hash check above and let decisions authored under the
    # old notes into the log — exactly the round-5 F1 failure.
    #
    # `principle_notes` is the only unrestricted domain in the payload
    # (`principles` is list[str] and `risk` is type-validated by the compiler),
    # so this one comparison covers the whole gap.
    src_notes = src.get("principle_notes") or {}
    compiled_notes = compiled.principle_notes or {}
    if src_notes != compiled_notes:
        msg = (
            f"strategy.yaml principle_notes differ from the compiled file's "
            f"— a notes edit landed after compile, so these decisions were "
            f"authored under different notes. Re-run /portfolio to recompile."
        )
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


def _orders_match_validation(blob_orders, validated_orders):
    """(ok, why) — do the blob's orders_proposed and the validator's
    orders_validated echo describe the SAME order set?

    Probe-2 A3 (+ cold-round follow-up): identity fields are (ticker,
    action, type, shares, limit_price, stop_price, est_price, price) —
    the PRICE fields are load-bearing because validate.py projects from
    them (direction-aware, and quote-bounded for uncapped orders since
    2026-08-09), so an est_price changed after validation changes every
    stress number. Compared as an
    order-insensitive multiset with None-sensitive equality — both lists
    came from the same Step-5 output, so any divergence is a
    transcription/adjustment drift that invalidates the stress results.
    Never raises (log path must stay up).
    """
    def _key(o):
        if not isinstance(o, dict):
            return ("<non-dict>",)
        return (
            str(o.get("ticker")),
            str(o.get("action")),
            str(o.get("type")),
            repr(o.get("shares")),
            repr(o.get("limit_price")),
            repr(o.get("stop_price")),
            repr(o.get("est_price")),
            repr(o.get("price")),
        )

    a = sorted(_key(o) for o in (blob_orders or []))
    b = sorted(_key(o) for o in (validated_orders or []))
    if a == b:
        return True, ""
    if len(a) != len(b):
        return False, f"count differs: blob={len(a)} validated={len(b)}"
    for ka, kb in zip(a, b):
        if ka != kb:
            fields = ("ticker", "action", "type", "shares",
                      "limit_price", "stop_price", "est_price", "price")
            diffs = [f for f, va, vb in zip(fields, ka, kb) if va != vb]
            return False, (
                f"first divergence at {ka[0]}: fields {diffs} "
                f"(blob={ka} validated={kb})"
            )
    return False, "order sets differ"


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
    except (ValueError, yaml.YAMLError, OSError, RecursionError) as exc:
        # ValueError: hash mismatch + SchemaError (ValueError subclass).
        # RecursionError: yaml.safe_load accepts a recursive alias, and this
        # helper hashes the RAW document before any validation runs — so a
        # mid-run edit to such a file would crash the log path with a traceback
        # instead of refusing cleanly. The compiler already catches its own.
        # yaml.YAMLError / OSError: malformed file or unreadable path
        # (e.g. --constraints "" resolves to cwd → IsADirectoryError).
        # All three fail-close via the same return-code channel.
        print(f"portfolio_log write: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    # Clean diagnostics on malformed/missing inputs (cold review 2026-06-11
    # R2 HIGH-6): a raw traceback mid-persistence invites improvised
    # workarounds; the cmd_* contract is a non-zero RETURN with a named file.
    try:
        blob = _read_json(pathlib.Path(args.decisions_blob))
    except (OSError, ValueError) as exc:
        print(f"portfolio_log: cannot read decisions_blob "
              f"{args.decisions_blob}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    try:
        state = _read_yaml(pathlib.Path(args.state))
    except (OSError, yaml.YAMLError) as exc:
        print(f"portfolio_log: cannot read state {args.state}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        macro = _read_json(pathlib.Path(args.macro))
    except (OSError, ValueError) as exc:
        print(f"portfolio_log: cannot read macro {args.macro}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    prices = macro.get("ticker_prices") or {}

    # Cold review 2026-06-11 R6 HIGH-1: an ABSENT open_orders key is
    # indistinguishable from "no working orders" — which is exactly how the
    # field failure happened (broker held a full-clear GTC sell; the state
    # sync updated holdings/cash and never thought about orders; the engine
    # proposed `hold` against an unseen liquidation). Key present (even as
    # an empty list) = the user explicitly attested; key absent = never
    # considered → refuse.
    if "open_orders" not in (state or {}):
        print(
            "portfolio_log: REFUSED — portfolio-state.yaml has no "
            "`open_orders` key. Sync the broker's working orders into it, "
            "or write `open_orders: []` to explicitly attest there are none "
            "(an absent key is how a working GTC order goes unseen).",
            file=sys.stderr,
        )
        return 2

    # Shape-validate the blob before any enrichment — replaces the old
    # `d["ticker"]` KeyError path with a structured diagnostic + exit 2.
    shape_errors = _validate_blob_shape(blob)
    # BEFORE the refusal, not after: the prints used to live inside the
    # validator, so a malformed blob emitted them and then got refused. Moving
    # them after the `return 2` would have silently dropped both advisories on
    # that path (cold review 2026-09-03 reproduced it). ONCE — the blob is
    # validated a second time below (principle-index range, which needs the
    # compiled list). See `_emit_blob_advisories`.
    _emit_blob_advisories(blob)
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
            "'shares' field (positive number) or remove the ticker from "
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

    # Price-structure Task 7: fact-vs-fact cross-check of the decision
    # evidence against THIS run's macro (basis / entry data-integrity /
    # entry consistency / declared-lens validity). Errors refuse the write
    # — a decision contradicting the macro it was authored against is an
    # audit-trail corruption, and stderr does not survive the session while
    # the log does. Warnings print and never block. Everything the check
    # deliberately does NOT catch is enumerated in the plan's §Honest
    # enforcement boundary and taught in prompts/portfolio-decide.md.
    _ev_errs, _ev_warns = _validate_decision_evidence(
        blob, macro, constraints=constraints_active)
    for _w in _ev_warns:
        print(f"portfolio_log: [WARN] {_w}", file=sys.stderr)
    if _ev_errs:
        print("portfolio_log: REFUSED — decision evidence contradicts this run's "
              "macro:", file=sys.stderr)
        for _e in _ev_errs:
            print(f"  - {_e}", file=sys.stderr)
        return 2

    # Closing round-38: with the compiled principles in hand, range-check
    # every clause-leading #N in principle_cited. An out-of-range tag
    # ("#99" against a 8-principle strategy — a plain LLM indexing typo)
    # previously rendered in decisions.md as an authoritative citation and
    # was persisted into the cited_this_run audit.
    principle_errors = _validate_blob_shape(
        blob, soft_principles=all_principles_from_doc)
    if principle_errors:
        print("portfolio_log: REFUSED — invalid principle citation(s):",
              file=sys.stderr)
        for e in principle_errors:
            print(f"  - {e}", file=sys.stderr)
        print("  Fix principle_cited to reference the numbered principles "
              "in strategy.compiled.yaml and re-run.", file=sys.stderr)
        return 2

    decisions = _enrich_decisions(blob.get("decisions", []), portfolio_before)
    orders = _enrich_orders(blob.get("orders_proposed", []) or [], prices)

    # Feedback 2026-06-11 #3b: decisions were blind to in-flight broker
    # orders (field run: MRAAY `hold` beside a full-size open GTC sell).
    # Attach the state's open orders per ticker (evidence, not verdict —
    # the decide prompt owns the reconciliation reasoning) + WARN on
    # direction conflict.
    open_orders_state = state.get("open_orders") or []
    open_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for o in open_orders_state:
        if isinstance(o, dict) and o.get("ticker"):
            open_by_ticker.setdefault(str(o["ticker"]), []).append(o)
    for d in decisions:
        oo = open_by_ticker.get(d.get("ticker"))
        if not oo:
            continue
        d["open_orders_snapshot"] = oo
        action = d.get("action")
        # ONE implementation (producer-consumer rule #3): the previous
        # inline closure mirrored validate._is_buy/_is_sell and had already
        # diverged in type tolerance — when the side vocabulary next grows,
        # a copy that isn't updated silently stops detecting direction
        # conflicts (the exact MRAAY hold-beside-open-GTC-sell failure).
        from scripts.validate import _is_buy, _is_sell
        has_sell = any(_is_sell(o) for o in oo)
        has_buy = any(_is_buy(o) for o in oo)
        if (action in ("hold", "add", "buy") and has_sell) or \
           (action in ("exit", "reduce") and has_buy):
            print(
                f"[WARN] portfolio_log: {d['ticker']} decision '{action}' may "
                f"contradict an in-flight open order "
                f"({[o.get('type') for o in oo]}) — reconcile explicitly "
                f"(keep/cancel/supersede) in the rationale.",
                file=sys.stderr,
            )

    # Probe-2 review round-14 F2: a decision's declared target_weight_pct
    # was never reconciled against the orders that claim to implement it —
    # "reduce to 40%" beside a sell that leaves 45% recorded both numbers
    # with no cross-check. Advisory WARN, not a refusal: staged rebalancing
    # across runs is legitimate; the gap just has to be visible.
    from scripts.validate import _apply_orders, _calc_account_value, \
        _usable_price
    _twp_orders = [o for o in orders if isinstance(o, dict)]
    _twp_tickers_with_orders = {
        o.get("ticker") for o in _twp_orders if o.get("ticker")
    }
    _twp_holdings, _twp_cash = _apply_orders(
        state.get("holdings", {}) or {},
        float(state.get("cash", 0) or 0),  # fail-open-ok: $0 cash is a legit state (fully invested)
        _twp_orders, prices,
    )
    _twp_total = _calc_account_value(_twp_holdings, prices, _twp_cash)
    if _twp_total > 0:
        for d in decisions:
            t = d.get("ticker")
            tgt = d.get("target_weight_pct")
            if (t not in _twp_tickers_with_orders
                    or isinstance(tgt, bool)
                    or not isinstance(tgt, (int, float))):
                continue
            px = _usable_price(prices.get(t))
            if px is None:
                continue  # missing-price gate elsewhere owns this
            achieved = _twp_holdings.get(t, 0) * px / _twp_total * 100
            if abs(achieved - float(tgt)) > 5.0:
                print(
                    f"[WARN] portfolio_log: {t} decision declares "
                    f"target_weight_pct={tgt} but the proposed orders "
                    f"project to ~{achieved:.1f}% of post-order equity "
                    f"(>5pp gap) — size the order to the target or state "
                    f"the staged plan in the rationale.",
                    file=sys.stderr,
                )

    # Closing round-27: the AUTHORING-time context seal — Step 5
    # records the state hash + per-ticker thesis vintages it
    # authored against (.decision_ctx.json). The validation-time
    # hash below only covers validate→log; this closes
    # author→validate. State drift → REFUSE; thesis refreshed
    # after authoring → WARN (the supported mid-run re-analysis
    # path requires re-affirming the decision, not a hard block).
    # Closing round-41: this check is UNCONDITIONAL — it used to sit
    # inside the --stress-test branch, so the documented all-hold/
    # no-orders path (which legitimately omits the flag) skipped seal
    # enforcement entirely and could persist S0-authored decisions
    # beside an S1 snapshot.
    _ctx_path = pathlib.Path(args.output_dir) / ".decision_ctx.json"
    if _ctx_path.exists():
        try:
            _ctx = json.loads(_ctx_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _ctx = None
        # Closing round-34: a PRESENT but unparseable seal — or one
        # missing its state hash — is a crash-window/truncation
        # artifact, NOT a legacy flow (only Step 5 ever writes this
        # file). Silently skipping it re-opened the author→validate
        # gate: an S1 state edit after a torn seal write persisted
        # S0-authored decisions without even the legacy WARN.
        # Unknown = fail-closed → REFUSE.
        _ctx_sha = (_ctx.get("state_file_sha256")
                    if isinstance(_ctx, dict) else None)
        if not (isinstance(_ctx_sha, str) and _ctx_sha):
            print(
                "portfolio_log: REFUSED — .decision_ctx.json seal "
                "exists but is unreadable or malformed (no "
                "state_file_sha256); cannot verify the decisions "
                "were authored against the current state. Re-run "
                "the Step-5 seal (or delete the file to proceed as "
                "a legacy unsealed flow).",
                file=sys.stderr,
            )
            return 2
        import hashlib as _hl2
        try:
            with open(args.state, "rb") as _sf2:
                _now_sha = _hl2.sha256(
                    _sf2.read()).hexdigest()
        except OSError:
            _now_sha = None
        if _now_sha != _ctx_sha:
            print(
                f"portfolio_log: REFUSED — portfolio-state"
                f".yaml changed since the decisions were "
                f"AUTHORED (Step-5 seal {_ctx_sha[:12]}…, "
                f"current {(_now_sha or 'unreadable')[:12]}"
                f"…). Re-run the decision step against the "
                f"current state.",
                file=sys.stderr,
            )
            return 2
        _ctx_ga = _ctx.get("thesis_generated_at")
        if isinstance(_ctx_ga, dict):
            for _d in (blob.get("decisions") or []):
                _t = (_d or {}).get("ticker")
                if not _t or _t not in _ctx_ga:
                    continue
                _cur_dir = None
                try:
                    from scripts.delta.resolver import \
                        find_latest_prior as _flp
                    _cur_dir = _flp(_t, "investment-thesis",
                                    include_today=True)
                except Exception:  # noqa: BLE001 — advisory check only
                    _cur_dir = None
                if _cur_dir is None:
                    continue
                _tp = _cur_dir / "investment_thesis.json"
                if not _tp.exists():
                    continue
                try:
                    _cur_ga = (json.loads(
                        _tp.read_text(encoding="utf-8"))
                        .get("meta") or {}).get("generated_at")
                except (OSError, ValueError):
                    continue
                if _cur_ga != _ctx_ga.get(_t):
                    print(
                        f"[WARN] portfolio_log: the thesis for "
                        f"{_t} was refreshed AFTER the decision "
                        f"was authored (sealed "
                        f"{_ctx_ga.get(_t)!r}, current "
                        f"{_cur_ga!r}) — the persisted "
                        f"thesis_snapshot reflects the NEW "
                        f"thesis while the action/rationale "
                        f"predate it; re-affirm the decision.",
                        file=sys.stderr,
                    )
        # 2026-08-10: ...and WHICH STRATEGY authored them. The seal bound
        # state and thesis vintages only, so a mid-run principle edit plus a
        # legitimate recompile let a superseded decision be logged and
        # stamped with the NEW constraints_active.source_hash — the rendered
        # log then recommended a sell the user's current principles
        # contradict, with no warning. `constraints_validated` cannot catch
        # it (it binds four hard-limit VALUES, which a text-only principle
        # edit leaves untouched) and `_verify_source_hash` only proves
        # strategy.yaml matches the compiled file NOW, not that either
        # matches what authored the decisions. Same posture as the state
        # binding: present-and-mismatched → REFUSE; absent → legacy WARN.
        _ctx_strat = _ctx.get("strategy_source_hash")
        if "strategy_source_hash" not in _ctx:
            print(
                "[WARN] portfolio_log: .decision_ctx.json seal carries no "
                "strategy_source_hash (legacy seal) — cannot verify the "
                "decisions were authored under THESE principles.",
                file=sys.stderr,
            )
        elif _ctx_strat != src_hash:
            print(
                f"portfolio_log: REFUSED — strategy.compiled.yaml changed "
                f"since the decisions were AUTHORED (Step-5 seal "
                f"{str(_ctx_strat)[:12]}…, current {str(src_hash)[:12]}…). "
                f"The decisions were reasoned under DIFFERENT principles "
                f"than this log would attribute them to. Re-author the "
                f"decisions against the current strategy.",
                file=sys.stderr,
            )
            return 2
    else:
        print(
            "[WARN] portfolio_log: no .decision_ctx.json seal in "
            "the output dir (legacy flow) — cannot verify the "
            "decisions were authored against the current state/"
            "thesis vintages.",
            file=sys.stderr,
        )

    stress = None
    if args.stress_test:
        stress_path = pathlib.Path(args.stress_test)
        if not stress_path.exists():
            # Cold review 2026-06-11 R3 HIGH-2: a supplied-but-missing path
            # is an orchestration bug (lost step-boundary variable, wrong
            # run dir) — silently skipping it dropped the stress section
            # from the log with no error.
            print(f"portfolio_log: --stress-test path does not exist: "
                  f"{stress_path} — run scripts.validate with --output to "
                  f"that path first (or omit the flag for a no-orders run).",
                  file=sys.stderr)
            return 2
        try:
            v = _read_json(stress_path)
        except (OSError, ValueError) as exc:
            print(f"portfolio_log: cannot read stress-test "
                  f"{args.stress_test}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 2
        # Probe-2 A3: bind the validator artifact to THIS blob's order set.
        # The artifact previously carried no echo of what it validated, so
        # a transcription drift between Step 6 (validate) and Step 8 (log)
        # could record a DIFFERENT order set as "stress-tested". Legacy
        # artifacts without the echo get a WARN (can't verify), a present
        # echo that mismatches is a hard refusal.
        if isinstance(v, dict):
            # Concurrency probe 2026-08-03 F3: verify the validator ran
            # against the SAME portfolio-state content we are about to
            # snapshot — an edit between Step 6 and Step 8 (user or another
            # session) previously persisted S0-authored decisions beside an
            # S1 snapshot undetected. Same posture as the orders echo:
            # present-and-mismatched → REFUSE; absent → legacy WARN.
            _v_state_sha = v.get("state_file_sha256")
            if isinstance(_v_state_sha, str) and _v_state_sha:
                import hashlib as _hl
                try:
                    with open(args.state, "rb") as _sf:
                        _cur_sha = _hl.sha256(_sf.read()).hexdigest()
                except OSError:
                    _cur_sha = None
                if _cur_sha != _v_state_sha:
                    print(
                        f"portfolio_log: REFUSED — portfolio-state.yaml "
                        f"changed between validation and logging (validator "
                        f"bound sha {_v_state_sha[:12]}…, current "
                        f"{(_cur_sha or 'unreadable')[:12]}…). The decisions "
                        f"were authored against a DIFFERENT state. Re-run "
                        f"scripts.validate (and re-check the decisions) "
                        f"against the current state.",
                        file=sys.stderr,
                    )
                    return 2
            else:
                print(
                    "[WARN] portfolio_log: stress-test artifact carries no "
                    "state_file_sha256 binding (legacy validator output) — "
                    "cannot verify the state was unchanged since validation.",
                    file=sys.stderr,
                )
            # Cold codex review 2026-08-10: quotes are verdict-determining
            # (they value every holding for the ratio checks, and price
            # uncapped market orders), but nothing bound them — so a PASS
            # computed at one set of quotes could be logged against another,
            # with the order echo still matching. This is the concrete shape
            # of the documented two-concurrent-sessions hazard. Same posture:
            # present-and-mismatched → REFUSE; absent → legacy WARN.
            _v_px_sha = v.get("ticker_prices_sha256")
            if isinstance(_v_px_sha, str) and _v_px_sha:
                from scripts.validate import ticker_prices_fingerprint

                _cur_px_sha = ticker_prices_fingerprint(macro)
                if _cur_px_sha != _v_px_sha:
                    print(
                        f"portfolio_log: REFUSED — ticker_prices changed "
                        f"between validation and logging (validator bound "
                        f"sha {_v_px_sha[:12]}…, current "
                        f"{_cur_px_sha[:12]}…). The stress verdict was "
                        f"computed against DIFFERENT quotes than the ones "
                        f"this log will record. Re-run scripts.validate "
                        f"against the current macro.json.",
                        file=sys.stderr,
                    )
                    return 2
            else:
                print(
                    "[WARN] portfolio_log: stress-test artifact carries no "
                    "ticker_prices_sha256 binding (legacy validator output) "
                    "— cannot verify it validated against THESE quotes.",
                    file=sys.stderr,
                )
            # Closing round-13 F1: bind the validator artifact to the SAME
            # constraint values this log will attest as active — a mid-run
            # recompile (legit principle edit) previously paired NEW
            # constraints_active with an OLD stress verdict. Same posture
            # as the orders echo: present-and-mismatched → REFUSE; absent
            # → legacy WARN.
            _cv = v.get("constraints_validated")
            if isinstance(_cv, dict):
                _now = {k: hard.get(k)
                        for k in ("max_single_position", "max_sector",
                                  "min_cash", "max_holdings")}
                if _cv != _now:
                    print(
                        f"portfolio_log: REFUSED — the stress-test artifact "
                        f"validated under constraints {_cv} but the current "
                        f"compiled constraints are {_now} (recompiled "
                        f"mid-run?). Re-run scripts.validate against the "
                        f"current strategy.compiled.yaml.",
                        file=sys.stderr,
                    )
                    return 2
            else:
                print(
                    "[WARN] portfolio_log: stress-test artifact carries no "
                    "constraints_validated echo (legacy validator output) — "
                    "cannot verify it validated under THESE constraints.",
                    file=sys.stderr,
                )
            echo = v.get("orders_validated")
            if echo is None:
                print(
                    "[WARN] portfolio_log: stress-test artifact carries no "
                    "orders_validated echo (legacy validator output) — "
                    "cannot verify it validated THIS order set. Re-run "
                    "scripts.validate to regenerate.",
                    file=sys.stderr,
                )
            else:
                ok, why = _orders_match_validation(
                    blob.get("orders_proposed", []) or [], echo
                )
                if not ok:
                    print(
                        f"portfolio_log: REFUSED — the stress-test artifact "
                        f"validated a DIFFERENT order set than this blob's "
                        f"orders_proposed ({why}). Re-run scripts.validate "
                        f"on the final orders before logging.",
                        file=sys.stderr,
                    )
                    return 2

        # Cold review 2026-06-11 R4 HIGH-2: a validator artifact that says
        # passed:false must not be persisted as the durable "proposed"
        # record — Step 6's adjust-and-revalidate loop exists to resolve it
        # BEFORE the log write. Recording failing orders would let the user
        # execute from the log without ever seeing the (ephemeral) stderr.
        if isinstance(v, dict) and v.get("passed") is not True:
            print(
                f"portfolio_log: REFUSED — stress-test artifact "
                f"{args.stress_test} records a FAILED validation "
                f"({len(v.get('violations') or [])} violation(s)). Adjust "
                f"the orders and re-run scripts.validate; do not log a "
                f"proposed set that failed its own safety check.",
                file=sys.stderr,
            )
            return 2
        stress = v.get("stress_test") if isinstance(v, dict) else None
        if stress:
            # Canonical scenario completeness (cold review 2026-06-11 R2
            # HIGH-4, escalated by probe-2 round-25 F1): a partial
            # artifact would read as "fully stress-checked" in the
            # persisted log while the stderr warning evaporates with the
            # session — the missing scenario is exactly where a negative-
            # cash breach could hide. The validator in this repo always
            # emits all five; absence means producer drift → REFUSE, same
            # posture as the FAILED-validation refusal above.
            _CANON_SCENARIOS = ("base", "all_buy", "all_sell",
                                "extreme_down", "defensive")
            missing_scen = [s for s in _CANON_SCENARIOS if s not in stress]
            if missing_scen:
                print(
                    f"portfolio_log: REFUSED — stress_test artifact is "
                    f"missing canonical scenario(s): {missing_scen}. A "
                    f"partial stress section must not persist as a "
                    f"fully-checked record; re-run scripts.validate to "
                    f"regenerate the artifact.",
                    file=sys.stderr,
                )
                return 2
            stress["hard_constraint_violations"] = len(v.get("violations", []))
            # Carry validator warnings (e.g. degenerate stress scenarios on
            # empty open_orders — feedback 2026-06-11 #3c) into the log so
            # the audit record shows the stress coverage caveat.
            if v.get("warnings"):
                stress["validator_warnings"] = v["warnings"]

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
    if (orders or open_orders_state) and stress is None:
        # Cold review 2026-06-11 R3 HIGH-2, escalated by R4 HIGH-3: proposed
        # orders AND working broker orders are both stress inputs; a log
        # without cash-survivability coverage for either is not an audit
        # record, it is a liability — and stderr does not survive the
        # session, the log does. /portfolio Step 6 always produces the
        # artifact, so reaching here means the orchestration skipped it.
        print(
            f"portfolio_log: REFUSED — {len(orders)} proposed order(s) and "
            f"{len(open_orders_state)} open order(s) but NO stress_test. "
            "Run scripts.validate --output <path> and pass --stress-test "
            "<path> (an all-hold run with no open orders may omit it).",
            file=sys.stderr,
        )
        return 2

    # principle audit — shared with `cmd_audit`, which previews this exact
    # split before the write so the author's `principle_audit_interpretation`
    # cannot contradict it inside the file the next run reads.
    cited_tags, not_cited = _principle_audit_split(
        decisions, all_principles_from_doc)
    principle_audit = {
        "cited_this_run": cited_tags,
        "not_cited_this_run": not_cited,
    }
    if blob.get("principle_audit_interpretation"):
        principle_audit["interpretation"] = blob["principle_audit_interpretation"]

    # Date: ET-day to match reports/portfolio/{YYYYMMDD}/ dir naming
    # (spec §11). UTC date can drift by one calendar day.
    today = today_et()
    # Cold review 2026-06-11 R5 MED-1: the dir name and the log's `date`
    # must agree on the SAME (ET) calendar — an Asia-local "today" lands the
    # run under tomorrow's dir while date says the ET day, misfiling
    # review/archive chronology (both order by directory glob).
    if out_dir.name != today.strftime("%Y%m%d"):
        print(
            f"portfolio_log: --output-dir day {out_dir.name!r} != ET day "
            f"{today.strftime('%Y%m%d')} — the reports/portfolio/{{YYYYMMDD}} "
            f"convention is the ET calendar day (spec §11); use the ET date.",
            file=sys.stderr,
        )
        return 2
    now_utc = dt.datetime.now(dt.timezone.utc)
    date = today.isoformat()
    # Microsecond precision: same-day reruns archive the prior pair under
    # its run_id (feedback 2026-06-11 #4) — second-precision ids could
    # collide on a fast rerun and corrupt the archive naming.
    run_id = now_utc.strftime("%Y%m%dT%H%M%S%fZ")

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

    # Write-gate == read-gate (whole-project review 2026-06-11 C14): prove
    # the log we are about to persist LOADS through the same typed contract
    # tomorrow's Step-0 review applies. The blob-shape validator is the
    # early human-friendly diagnostic; THIS is the guarantee — the two can
    # never drift apart again because the reader itself is the check.
    try:
        from scripts.schemas.decisions import validate_decisions
        validate_decisions(log)
    except ValueError as exc:   # SchemaError is a ValueError subclass
        print(
            f"portfolio_log: REFUSED — the assembled log fails the decisions "
            f"schema that tomorrow's review must load it through: {exc}. "
            f"Fix the blob (this error names the field) and re-run.",
            file=sys.stderr,
        )
        return 2

    # Render MD BEFORE any persistence — a render exception leaves no
    # partial JSON on disk. Both files then land via atomic tmp+rename
    # through cli_utils helpers (matches run_meta.save convention).
    md_text = _render_md(log, (macro or {}).get("ticker_prices") or {})

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Feedback 2026-06-11 #6: fail visible with remediation — never let
        # a caller improvise an out-of-root location (/tmp is ephemeral in
        # Cowork; a silently relocated decision log is worse than no log).
        print(
            f"portfolio_log: FATAL — cannot create {out_dir}: "
            f"{type(exc).__name__}: {exc}. Fix the directory/mount and "
            f"re-run; do NOT write the log anywhere else.",
            file=sys.stderr,
        )
        return 2
    json_path = out_dir / "decisions.json"
    md_path = out_dir / "decisions.md"

    # Feedback 2026-06-11 #4: a same-day rerun used to clobber the earlier
    # run's decisions.json/md (losing its user_confirmation audit). Archive
    # the existing pair under its run_id before writing the new canonical.
    if json_path.exists():
        try:
            prior_run_id = (_read_json(json_path) or {}).get("run_id")
        except (OSError, ValueError):
            prior_run_id = None
        # Sanitize: the id becomes a FILENAME — a corrupt artifact must not
        # path-traverse; ':' (legal in the loader's run_id charset) is
        # invalid on Windows.
        if not (isinstance(prior_run_id, str)
                and re.fullmatch(r"[A-Za-z0-9TZ._\-]{4,64}", prior_run_id)):
            prior_run_id = f"unknown-{int(json_path.stat().st_mtime)}"
        archive_json = out_dir / f"decisions.{prior_run_id}.json"
        n = 2
        while archive_json.exists():   # duplicate id → never overwrite an archive
            archive_json = out_dir / f"decisions.{prior_run_id}-{n}.json"
            n += 1
        # Round-25 F2: COPY, don't rename — the old flow renamed the
        # canonical away and only then wrote the replacement, so an I/O
        # failure in that window (disk full at mkstemp/write) left NO
        # decisions.json at all; cmd_review searches only */decisions.json
        # and would silently fall back to an older date or "first run".
        # Copy-then-atomic-replace keeps a canonical on disk at every
        # instant; a crash after the copy merely leaves a duplicate
        # archive next run (the while-loop suffixes it).
        import shutil
        shutil.copy2(json_path, archive_json)
        if md_path.exists():
            # Closing round-4 F1: the pair replace is per-file atomic, so a
            # crash between the JSON and MD replaces can leave JSON=run B
            # beside MD=run A. Archiving both under B's id would freeze a
            # permanently mixed pair. The MD renders its own run_id line —
            # verify it matches; on mismatch archive the MD under its OWN
            # id and warn.
            _md_text = md_path.read_text(encoding="utf-8")
            _m = re.search(r"\*\*run_id\*\*: `([A-Za-z0-9TZ._:\-]{4,64})`",
                           _md_text)
            _md_rid = _m.group(1).replace(":", "_") if _m else None
            if (_md_rid is not None and prior_run_id is not None
                    and not prior_run_id.startswith("unknown-")
                    and _md_rid != prior_run_id
                    and re.fullmatch(r"[A-Za-z0-9TZ._\-]{4,64}", _md_rid)):
                md_archive = out_dir / f"decisions.{_md_rid}.md"
                n2 = 2
                while md_archive.exists():
                    md_archive = out_dir / f"decisions.{_md_rid}-{n2}.md"
                    n2 += 1
                shutil.copy2(md_path, md_archive)
                print(
                    f"[WARN] portfolio_log: canonical decisions.md carries "
                    f"run_id {_md_rid} but decisions.json carries "
                    f"{prior_run_id} (torn pair from an interrupted write) — "
                    f"archived the MD under its OWN id ({md_archive.name}) "
                    f"instead of pairing it with the JSON archive.",
                    file=sys.stderr,
                )
            else:
                shutil.copy2(md_path, archive_json.with_suffix(".md"))
        print(f"portfolio_log: archived prior same-day run -> {archive_json.name}")

    # Pair write: stages both tmps first, replaces JSON (canonical) then
    # MD. Per-file atomic; pair is not strictly atomic across an external
    # observer between the two replaces — see write_pair_atomic docstring.
    write_pair_atomic(log, str(json_path), md_text, str(md_path))

    print(f"portfolio_log: wrote {json_path}")
    print(f"portfolio_log: wrote {md_path}")
    return 0


def state_key_hashes(state: Dict[str, Any]) -> Dict[str, str]:
    """Per-top-level-key digest of a loaded `portfolio-state.yaml`.

    Hashed over the PARSED value, canonicalised, not over the YAML bytes: a
    re-indent, a comment or a reordered mapping must not read as a change,
    or the line that names what moved becomes noise. `default=str` folds a
    quoted and an unquoted YAML date onto the same digest. `allow_nan` is
    left at its permissive default deliberately — this is a fingerprint, not
    a wire format, and refusing NaN here would turn one odd value in a
    personal config file into a crash in the seal writer.

    ONE implementation, two callers (`.claude/rules/producer-consumer.md`
    §3): `/portfolio` Step 5 seals the map with it and `review` compares
    against it. Two spellings of a hash diverge silently and the diff then
    names every key.
    """
    out: Dict[str, str] = {}
    for k, v in (state or {}).items():
        if not isinstance(k, str):
            continue
        try:
            blob = json.dumps(v, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            blob = repr(v)
        out[k] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return out


def _review_state_freshness(prior: pathlib.Path,
                            log: Dict[str, Any],
                            state_path: Optional[str]) -> None:
    """Print whether the prior decision set still covers the CURRENT state.

    Feedback 2026-08-30(b) ②: after a same-day `portfolio-state.yaml` edit,
    review's three lines all looked healthy, so an unattended rerun had to
    choose blind between clobbering a still-valid log and missing a real
    change. Both facts were already in review's own inputs. Advisory only —
    never changes the exit code, because "the state moved" is a reason to
    look, not a reason to stop.
    """
    if not state_path:
        return
    sp = pathlib.Path(state_path)
    try:
        state_bytes = sp.read_bytes()
        state = yaml.safe_load(state_bytes.decode("utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return  # cmd_write owns the loud refusal; review must not crash Step 0
    if not isinstance(state, dict):
        return
    cur_sha = hashlib.sha256(state_bytes).hexdigest()

    prior_sha = None
    prior_keys = None
    ctx_path = prior.parent / ".decision_ctx.json"
    if ctx_path.exists():
        try:
            ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ctx = None
        if isinstance(ctx, dict):
            v = ctx.get("state_file_sha256")
            if isinstance(v, str) and v:
                prior_sha = v
            km = ctx.get("state_key_sha256")
            if isinstance(km, dict):
                prior_keys = {k: v2 for k, v2 in km.items()
                              if isinstance(k, str) and isinstance(v2, str)}
    if prior_sha is None:
        # Kept deliberately (codex 2026-08-31 Q9 proposed cutting it): once
        # STALE exists, SILENCE starts to mean "unchanged", so an unsealed
        # prior run would newly mislead — a hazard this feature creates.
        print(f"\n  state: prior run carries no authoring seal — cannot tell "
              f"whether {sp.as_posix()} changed since it.")
    elif prior_sha != cur_sha:
        # Two hash prefixes cannot tell "the decision was EXECUTED" from "the
        # decision was OVERTURNED" — opposite directions with the same line
        # (feedback 2026-08-31 portfolio ③: the user placed the prior run's
        # RKLB reduce at the broker, so `open_orders` gained a row, and only
        # a hand diff of two state files could say so). Name the top-level
        # keys instead. Zero named keys is a real answer, not a fallback:
        # the file's bytes moved but nothing the run reads did — a comment
        # or a re-indent.
        if prior_keys is not None:
            cur_keys = state_key_hashes(state)
            moved = sorted(k for k in set(prior_keys) | set(cur_keys)
                           if prior_keys.get(k) != cur_keys.get(k))
            # "no TRACKED key": `state_key_hashes` skips a non-string
            # top-level key and deliberately folds a quoted and an unquoted
            # YAML date onto one digest, so zero moved keys does not prove the
            # edit was cosmetic — only that nothing the digest tracks moved.
            # (A numeric change DOES move its key; codex's claim otherwise was
            # reproduced and refuted, 2026-09-03.)
            detail = (f" (changed: {', '.join(moved)})" if moved
                      else " (no tracked top-level key changed — a comment, a"
                           " re-indent, or an edit the canonical digest folds)")
        else:
            # A seal older than per-key hashing. Falling back SILENTLY put this
            # line byte-for-byte alongside the zero-keys-moved answer above,
            # which is a REAL finding — so a run whose watchlist had gained a
            # ticker read as "only a comment moved" (feedback 2026-09-01
            # portfolio ③). One-shot per schema bump, and it recurs on every
            # future one, so it says which of the two it is.
            detail = (" (key-level attribution unavailable — the prior run's"
                      " seal carries no per-key hashes)")
        print(f"\n  STALE: {sp.as_posix()} changed since the prior run "
              f"({prior_sha[:8]}… -> {cur_sha[:8]}…){detail}")

    # str-only on BOTH sides: YAML 1.1 coerces the real ticker `ON` (ON
    # Semiconductor) to the boolean True, and sorting/joining a mixed
    # str+bool set raises TypeError — at Step 0, before any analysis.
    holdings = state.get("holdings") or {}
    expected = {t for t in holdings if isinstance(t, str)} if isinstance(
        holdings, dict) else set()
    watchlist = state.get("watchlist") or []
    if isinstance(watchlist, list):
        expected |= {t for t in watchlist if isinstance(t, str)}
    covered = {
        d.get("ticker") for d in (log.get("decisions") or [])
        if isinstance(d, dict) and d.get("ticker")
    }
    uncovered = sorted(expected - covered)
    if uncovered:
        print(f"  UNCOVERED: {', '.join(uncovered)} — the prior decision set "
              f"does not cover these")


def cmd_audit(args: argparse.Namespace) -> int:
    """Print the principle-citation split the logger WOULD persist. No writes.

    `cited_this_run` / `not_cited_this_run` are derived by the logger from each
    `principle_cited`'s clause-leading `#N`, while the author writes
    `principle_audit_interpretation` — a sentence ABOUT that split — before
    ever seeing it. When the two disagree the log contradicts itself, exits 0,
    and the contradiction surfaces only after the artifact is on disk, where
    the next run's Step 0 reads it (feedback 2026-09-03 portfolio ①).

    This is the SAME computation, callable before the write: both commands go
    through `_principle_audit_split` and the same range-aware
    `_validate_blob_shape`, so a dry run that passes cannot differ from the
    write that follows. Deliberately not a gate and not a new rule. It needs
    neither macro nor stress test, because the author runs it while the blob
    is still the only thing that exists.
    """
    from scripts.schemas import SchemaError
    try:
        blob = json.loads(
            pathlib.Path(args.decisions_blob).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"portfolio_log: cannot read --decisions-blob "
              f"{args.decisions_blob}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(blob, dict):
        print(f"portfolio_log: --decisions-blob is a "
              f"{type(blob).__name__}, not an object", file=sys.stderr)
        return 2
    try:
        _doc, _hard, _src, all_principles = \
            _prepare_compiled_context(args.constraints)
    except (SchemaError, yaml.YAMLError, OSError) as exc:
        print(f"portfolio_log: strategy.compiled.yaml invalid: "
              f"{type(exc).__name__}: {exc}. Recompile via /portfolio skill.",
              file=sys.stderr)
        return 2

    errors = _validate_blob_shape(blob, soft_principles=all_principles)
    if errors:
        print("portfolio_log: REFUSED — the blob would not write:",
              file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    cited, not_cited = _principle_audit_split(
        blob.get("decisions") or [], all_principles)
    print(f"cited: {cited}   not_cited: {not_cited}")
    print("Read your principle_audit_interpretation against THIS split before "
          "calling `write`: the logger persists the line above, not the "
          "sentence.")
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

    # Most recent decisions.json INCLUDING today (feedback 2026-06-11 #4):
    # Step 0 runs before today's write, so whatever exists IS the prior run
    # — excluding today made a same-day earlier run invisible exactly when
    # a re-evaluation needed it. Archived pairs (decisions.<run_id>.json)
    # don't match this glob, so only canonical latest-per-day candidates
    # appear. `today` remains in use for follow-up due-date comparison.
    candidates = sorted(reports_dir.glob("*/decisions.json"), reverse=True)
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

    try:
        _review_state_freshness(prior, log, getattr(args, "state", None))
    except Exception as exc:  # fail-open-ok: an ADVISORY may never take Step 0 (and with it the whole run) down; every loud refusal on these same inputs is cmd_write's
        print(f"  state: advisory unavailable ({type(exc).__name__})")

    # unfilled execution outcomes flag
    outcomes = log.get("execution_outcomes") or {}
    if outcomes.get("reflection") is None and confirm in ("accepted", "modified"):
        print(f"\n  ⚠ Prior run has no reflection recorded. Consider filling")
        print(f"    {prior}:execution_outcomes.reflection before today's rerun archives it.")
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

    a = sub.add_parser(
        "audit",
        help="Dry-run the principle-citation split the write WOULD persist")
    a.add_argument("--decisions-blob", required=True,
                   help="LLM-authored blob JSON path")
    a.add_argument("--constraints", default="strategy.compiled.yaml")
    a.set_defaults(func=cmd_audit)

    r = sub.add_parser("review", help="Review prior run's follow-ups")
    r.add_argument("--reports-dir", default="reports/portfolio")
    r.add_argument("--today", default=None, help="YYYY-MM-DD (default: today)")
    r.add_argument(
        "--state",
        default="portfolio-state.yaml",
        help=(
            "portfolio-state.yaml path — compared against the prior run's "
            "authoring seal to report state drift + uncovered tickers "
            "(advisory; never changes the exit code)."
        ),
    )
    r.set_defaults(func=cmd_review)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
