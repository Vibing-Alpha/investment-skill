"""/monitor deterministic core: probe / validate / render-digest. No LLM."""
from __future__ import annotations

import datetime
import hashlib
import re
from pathlib import Path
import yaml

from scripts.delta.portfolio_classify import classify as _classify, _days_since_last_full_bq
from scripts.delta.resolver import find_latest_prior, DEFAULT_REPORTS_ROOT
from scripts.delta.calendar import today_et   # ET date basis — matches classify(), avoids tz-boundary drift


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _norm(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def evidence_id(kind: str, ticker: str, payload: str) -> str:
    """Stable evidence id `kind:ticker:hash8`. Kinds: condition|news|catalyst|staleness.
    Every kind hashes a normalized payload; for staleness the payload is the classification
    STATE, so a state change (e.g. fresh -> stale_bq) yields a new id and the newly-stale
    ticker resurfaces as `new` instead of collapsing to a constant `seen` id."""
    return f"{kind}:{ticker}:{_sha8(_norm(payload))}"


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def load_universe(state_path: Path):
    """Return ([{ticker, source, shares, cost_basis}], cash|None). Holding wins over watchlist.
    Missing file → ([], None) (fail-closed; caller emits a warning)."""
    if not Path(state_path).exists():
        return [], None
    with open(state_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    holdings = data.get("holdings") or {}
    watchlist = data.get("watchlist") or []
    cash = data.get("cash")
    rows, seen = [], set()
    for t, h in holdings.items():
        h = h or {}
        rows.append({"ticker": t, "source": "holding",
                     "shares": _f(h.get("shares")), "cost_basis": _f(h.get("cost_basis"))})
        seen.add(t)
    for t in watchlist:
        if t in seen:
            continue
        rows.append({"ticker": t, "source": "watchlist", "shares": None, "cost_basis": None})
        seen.add(t)
    return rows, _f(cash)


def _days_since_full_bq(ticker, reports_root=None):
    # _days_since_last_full_bq requires a real Path root (it does reports_root / ticker);
    # never pass None — fall back to the canonical reports root. ET date matches classify().
    root = Path(reports_root) if reports_root else DEFAULT_REPORTS_ROOT
    return _days_since_last_full_bq(ticker, root, today_et())


def _days_since_thesis(ticker, reports_root=None):
    d = find_latest_prior(ticker, "investment-thesis", reports_root=reports_root, include_today=True)
    if d is None:
        return None
    try:
        run = datetime.datetime.strptime(d.name, "%Y%m%d").date()
    except ValueError:
        return None
    return (today_et() - run).days


def ticker_staleness(ticker, reports_root=None):
    return {
        "state": _classify(ticker, reports_root=reports_root),
        "days_since_full_bq": _days_since_full_bq(ticker, reports_root=reports_root),
        "days_since_thesis": _days_since_thesis(ticker, reports_root=reports_root),
    }


def ticker_market_facts(ticker, snapshot, shares=None):
    price = (snapshot.get("ticker_prices") or {}).get(ticker)
    indicators = (snapshot.get("ticker_indicators") or {}).get(ticker)
    status_entry = ((snapshot.get("chart_statuses") or {}).get("ticker_prices") or {}).get(ticker) or {}
    price_status = status_entry.get("status") or "FAILED"
    indicators_available = indicators is not None
    reason = None if indicators_available else "indicators unavailable (<74 bars or compute failed)"
    market_value = None
    if shares is not None and isinstance(price, (int, float)):
        market_value = round(float(shares) * float(price), 2)
    return {
        "price": price if isinstance(price, (int, float)) else None,
        "price_status": price_status,
        "indicators": indicators,
        "indicators_available": indicators_available,
        "indicator_unavailable_reason": reason,
        "market_value": market_value,
    }


_KNOWN_PRECISION = {"confirmed", "estimated", "approximate"}
# Imprecise-but-LEGITIMATE catalyst granularity: a bare year, half, quarter, VALID month, or
# year-range. These are real (e.g. "H2 2026 product ramp") but can't be placed in a 7-day window,
# so they're skipped — silently, because they are expected data, not malformed. The month arm is a
# real 1-12 month (NOT \d{1,2}) so a malformed month like 2026-13/2026-00 still falls through to a
# warning rather than being silently swallowed. Only genuinely-garbage dates warn.
_FUZZY_DATE = re.compile(r"^\d{4}(-(H[12]|Q[1-4]|0?[1-9]|1[0-2]|\d{4}))?$", re.IGNORECASE)


def catalyst_evidence(ticker, calendar, today):
    """Return ([evidence dict], [warnings]). Due = date<=today+1; near = today+2..+7."""
    ev, warns = [], []
    for c in calendar or []:
        date_s = (c or {}).get("date")
        event = (c or {}).get("event") or ""
        if not date_s or not event:
            warns.append(f"{ticker}: skipped undated/eventless catalyst {c!r}")
            continue
        try:
            d = datetime.datetime.strptime(date_s, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            # a recognizable but imprecise date (year / half / quarter / month / range) is real
            # data that simply can't fit the 7-day window — skip it quietly. Warn only on genuine garbage.
            if not (isinstance(date_s, str) and _FUZZY_DATE.match(date_s)):
                warns.append(f"{ticker}: skipped catalyst with unparseable date {date_s!r}")
            continue
        delta = (d - today).days
        if delta < 0 or delta > 7:
            continue
        window = "due" if delta <= 1 else "near"
        prec = (c or {}).get("date_precision")
        prec = prec if prec in _KNOWN_PRECISION else "unknown"
        payload = f"{_norm(event)}|{date_s}|{prec}"
        ev.append({
            "evidence_id": evidence_id("catalyst", ticker, payload),
            "kind": "catalyst",
            "text": event,
            "meta": {"date": date_s, "precision": prec, "window": window},
        })
    return ev, warns


def news_evidence(ticker, articles):
    """Normalize {title,published_at,source,url} → evidence; id from url, else hash(title+date+source)."""
    ev, warns = [], []
    for a in articles or []:
        a = a or {}
        title = (a.get("title") or "").strip()
        date = a.get("published_at")
        source = a.get("source")
        url = (a.get("url") or "").strip()
        if url:
            ident = url
        elif title and (date or source):
            ident = f"{_norm(title)}|{date or ''}|{source or ''}"
        else:
            warns.append(f"{ticker}: skipped news article with no url/title ({a!r})")
            continue
        ev.append({
            "evidence_id": evidence_id("news", ticker, ident),
            "kind": "news",
            "text": title,
            "meta": {"source": source, "date": date, "url": a.get("url")},
        })
    return ev, warns


import json


def condition_evidence(ticker, invalid_if, entry_attractive_if):
    ev = []
    for cls, conds in (("invalid_if", invalid_if or []), ("entry_attractive_if", entry_attractive_if or [])):
        for c in conds:
            ev.append({"evidence_id": evidence_id("condition", ticker, c),
                       "kind": "condition", "text": c, "meta": {"class": cls}})
    return ev


def staleness_evidence(ticker, staleness):
    return {"evidence_id": evidence_id("staleness", ticker, staleness.get("state") or "unknown"),
            "kind": "staleness", "text": f"BQ {staleness.get('days_since_full_bq')}d / thesis "
                                         f"{staleness.get('days_since_thesis')}d ({staleness.get('state')})",
            "meta": dict(staleness)}


def load_prior_evidence_ids(monitor_root, current_dirname):
    """Union of evidence_refs from the most recent prior-OR-same-day `action_plan.json`
    (dirs `<= current_dirname`); on a same-day rerun the first run's plan is the baseline."""
    root = Path(monitor_root)
    if not root.exists():
        return set()
    # `<= current_dirname`: on a SAME-DAY rerun, today's first-run action_plan.json
    # (at current_dirname) is the correct baseline (spec §3.1 step 8). The probe reads
    # it fully before validate atomically overwrites it, so there is no mid-write race.
    dirs = sorted(d.name for d in root.iterdir()
                  if d.is_dir() and len(d.name) == 8 and d.name.isdigit() and d.name <= current_dirname)
    for name in reversed(dirs):
        plan = root / name / "action_plan.json"
        if plan.exists():
            try:
                data = json.loads(plan.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ids = set()
            for it in data.get("items") or []:
                ids.update(it.get("evidence_refs") or [])
            return ids
    return set()


# ---------------------------------------------------------------------------
# Task 9: validate gate — reject-status, bilingual advice-scan, ref resolution
# ---------------------------------------------------------------------------
from scripts.schemas.monitor import _ROUTES, _PRIORITIES   # single source of the enums


class MonitorValidationError(ValueError):
    pass


# Action/allocation vocabulary the router's AUTHORED free-text fields must not contain.
# IMPORTANT: this is a best-effort BACKSTOP, not the no-trade guarantee. Free text cannot be
# proven advice-free by a blacklist, and chasing every phrasing is a false-positive treadmill.
# The actual no-trade guarantee is structural/by-design: /monitor never places a trade; a human
# reads the digest and explicitly picks a route via AskUserQuestion; the real trade decision
# happens in /portfolio under its own hard constraints. So we list only UNAMBIGUOUS trade/
# allocation terms (multi-word where a single word would collide with factual prose like
# "selling pressure" / "short interest" / "added capacity") and accept that a novel phrasing
# may slip into an explanatory reason the human then reads.
_ADVICE_EN = re.compile(
    r"\b(buy|sell|reduce|trim|exit|hold|deploy|raise cash|allocate|overweight|underweight"
    r"|go(?:ing)? long|take profits?|lock in (?:gains?|profits?)|scale (?:in|out)"
    r"|add to (?:the )?position|(?:increase|decrease|raise|cut|trim|lighten) (?:the )?(?:exposure|position|stake))\b",
    re.IGNORECASE)
_ADVICE_ZH = re.compile(r"(买入|卖出|减仓|加仓|清仓|止损|部署|仓位|目标仓位|持有|继续持有|增持)")
# Only reject a percentage in an ALLOCATION context — a factual "price down 12%" is fine.
_PCT_WEIGHT = re.compile(r"(target\s+weight|%\s*of\s+(the\s+)?portfolio|目标仓位|仓位\s*\d+\s?%)", re.IGNORECASE)
# Allow-rule: action-ish wording is OK only as an explicit route handoff.
_ALLOW = re.compile(r"(route to `?/portfolio`?|走\s*/portfolio)", re.IGNORECASE)

# The router's output contract is key-fixed. Reject any key outside these sets: an extra
# item field (e.g. {"note": "buy NVDA"}) would otherwise carry laundered advice past the
# reason/summary scan and survive stamp_status's {**item} spread into action_plan.json.
_PLAN_KEYS = frozenset({"summary", "items"})
_ITEM_KEYS = frozenset({"ticker", "priority", "route", "reason", "evidence_refs"})
# Route well-formedness (structural, not policy — WHICH situation routes where is the
# prompt's job; this only enforces each route is shaped so the SKILL can trigger it):
#   /screen-stocks   = watchlist-dry discovery → MUST be the ticker:null item
#   fact routes      = ticker-specific + evidence-triggered (SKILL runs `/<route> <ticker>`,
#                      so a null ticker is a broken trigger; every such item references a
#                      fired condition / material news / due catalyst / staleness fact)
#   /portfolio       = portfolio-wide decision skill → ticker AND evidence both optional
#                      (a cash/allocation concern has no per-ticker evidence object)
_FACT_ROUTES = frozenset({"/investment-thesis", "/score-business"})


def _scan_advice(text: str, where: str):
    if not text:
        return
    # Remove ONLY the sanctioned handoff phrase(s), then scan what remains — so an
    # allowed "route to /portfolio" cannot launder real advice ("buy NVDA, route to /portfolio").
    residual = _ALLOW.sub(" ", text)
    if _ADVICE_EN.search(residual) or _ADVICE_ZH.search(residual) or _PCT_WEIGHT.search(residual):
        raise MonitorValidationError(f"action advice in authored field {where}: {text!r}")


def validate_raw_plan(raw: dict, probe_evidence_ids: set, probe_tickers: set):
    """HARD gate on the router's raw plan. Raises MonitorValidationError on any violation.

    `probe_tickers` is the monitored universe (held + watchlist). Every authored field is
    constrained: enums for route/priority, exact key sets, ticker ∈ universe (or null), and
    a trade-advice scan on the only two free-text fields (summary/reason). This is what keeps
    LLM-authored content — including the `ticker` value, which reaches the user-facing digest
    heading — from surfacing a trade recommendation or breaking the digest."""
    if not isinstance(raw, dict):
        raise MonitorValidationError("raw plan is not an object")
    if set(raw) != _PLAN_KEYS:                      # exact top-level contract (required AND no extras)
        raise MonitorValidationError(f"raw plan keys must be exactly {sorted(_PLAN_KEYS)}; got {sorted(raw)}")
    if not isinstance(raw.get("summary"), str):
        raise MonitorValidationError("raw plan `summary` must be a string")
    items = raw.get("items")
    if not isinstance(items, list):                 # missing/non-list items must NOT pass the gate
        raise MonitorValidationError("raw plan must have an `items` list")
    _scan_advice(raw["summary"], "summary")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise MonitorValidationError(f"items[{i}] is not an object")
        if "status" in item:                        # specific message before the generic exact-key check
            raise MonitorValidationError(f"items[{i}] must NOT contain status (stamped post-router)")
        if set(item) != _ITEM_KEYS:                 # exact item contract (required AND no laundering keys)
            raise MonitorValidationError(f"items[{i}] keys must be exactly {sorted(_ITEM_KEYS)}; got {sorted(item)}")
        ticker = item.get("ticker")
        if ticker is not None and (not isinstance(ticker, str) or ticker not in probe_tickers):
            raise MonitorValidationError(f"items[{i}] ticker must be null or a monitored-universe ticker: {ticker!r}")
        if item.get("route") not in _ROUTES:
            raise MonitorValidationError(f"items[{i}] bad route enum: {item.get('route')!r}")
        if item.get("priority") not in _PRIORITIES:
            raise MonitorValidationError(f"items[{i}] bad priority: {item.get('priority')!r}")
        if not isinstance(item.get("reason"), str):
            raise MonitorValidationError(f"items[{i}] `reason` must be a string")
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise MonitorValidationError(f"items[{i}].evidence_refs must be a list of strings")
        _scan_advice(item["reason"], f"items[{i}].reason")
        route = item["route"]                       # route↔ticker↔evidence well-formedness
        if route == "/screen-stocks" and ticker is not None:
            raise MonitorValidationError(f"items[{i}] /screen-stocks must have a null ticker (it is discovery, not ticker-specific)")
        if route in _FACT_ROUTES and ticker is None:
            raise MonitorValidationError(f"items[{i}] {route} requires a ticker (the SKILL triggers it as `{route} <ticker>`)")
        if ticker is None and refs:                  # a null-ticker item (discovery / portfolio-wide) has no per-ticker evidence
            raise MonitorValidationError(f"items[{i}] a null-ticker item must have empty evidence_refs (it is portfolio-wide or discovery)")
        if route in _FACT_ROUTES and not refs:
            raise MonitorValidationError(f"items[{i}] {route} requires at least one evidence_ref (must be evidence-grounded)")
        for ref in refs:
            if ref not in probe_evidence_ids:
                raise MonitorValidationError(f"items[{i}] evidence_ref does not resolve to a probe evidence_id: {ref}")
            # a ticker-specific item must cite ITS ticker's evidence (evidence_id = kind:ticker:hash)
            if ticker is not None and ref.split(":")[1:2] != [ticker]:
                raise MonitorValidationError(f"items[{i}] evidence_ref {ref} is not for this item's ticker {ticker!r}")


def stamp_status(raw: dict, prior_evidence_ids: set, run_date: str) -> dict:
    """Deterministically add items[].status (new|seen|null) + run_date. Pure; returns a new dict."""
    items = []
    for item in raw.get("items") or []:
        refs = item.get("evidence_refs") or []
        if not refs:
            status = None
        elif any(r not in prior_evidence_ids for r in refs):
            status = "new"
        else:
            status = "seen"
        items.append({**item, "status": status})
    return {"run_date": run_date, "items": items, "summary": raw.get("summary") or ""}


def _oneline(text) -> str:
    """Collapse LLM-authored free text to a single line before rendering, so it cannot
    inject a markdown block (a leading `\\n##` heading / list) into the user-facing digest.
    Inline markdown chars are left as-is — the digest is a local, non-executed, human-read
    file and trade advice is already gated upstream; full escaping would be over-engineering."""
    return " ".join((text or "").split())


def render_digest(plan: dict, probe: dict) -> str:
    """Deterministic markdown from the validated plan + probe evidence. No new prose."""
    by_id = {}
    for pt in probe.get("per_ticker") or []:
        for e in pt.get("evidence") or []:
            by_id[e["evidence_id"]] = e
    lines = [f"# /monitor — {plan.get('run_date','')}", "", _oneline(plan.get("summary")), ""]
    rank = {"critical": 0, "watch": 1, "info": 2}
    for item in sorted(plan.get("items") or [], key=lambda i: rank.get(i.get("priority"), 9)):
        tkr = item.get("ticker") or "(portfolio)"
        status = item.get("status")
        tag = f" _{status}_" if status else ""
        lines.append(f"## {tkr} — {item.get('priority')} → `{item.get('route')}`{tag}")
        lines.append(_oneline(item.get("reason")))
        for ref in item.get("evidence_refs") or []:
            e = by_id.get(ref)
            if e is None:
                continue   # dangling → omit (validator is the hard gate upstream)
            if e["kind"] == "news":
                src = (e.get("meta") or {}).get("source") or "?"
                lines.append(f"- news: «{e['text']}» — {src}")
            elif e["kind"] == "condition":
                lines.append(f"- fired condition: «{e['text']}»")
            elif e["kind"] == "catalyst":
                lines.append(f"- catalyst: «{e['text']}» ({(e.get('meta') or {}).get('window')})")
            elif e["kind"] == "staleness":
                lines.append(f"- {e['text']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Task 12: probe assembler + thin I/O seams + CLI subcommands
# ---------------------------------------------------------------------------
import argparse
import os
import sys
import tempfile

from scripts.macro import fetch_macro_snapshot
from scripts.sources.financial_datasets import fetch_news_data


def _macro_snapshot(tickers):                       # thin wrapper (mocked in tests)
    return fetch_macro_snapshot(tickers=tickers)


def _fetch_articles(ticker):                        # thin wrapper (mocked in tests)
    """Return (articles, warning|None). A non-PASSED fetch yields a warning, not silent [] —
    fail-closed per producer-consumer.md §4 (absent ≠ no-news)."""
    res = fetch_news_data(ticker)
    data = getattr(res, "data", None) or {}
    status = getattr(res, "status", None)
    warn = None if status in (None, "PASSED") else f"{ticker}: news fetch {status}"
    return (data.get("articles") or []), warn


def _load_thesis_conditions(ticker, reports_root=None):
    """Return (invalid_if, entry_attractive_if). `find_latest_prior` already guarantees the
    resolved dir's investment_thesis.json is PARSEABLE (it skips corrupt artifacts), so the
    except below is a defensive belt only — not a fail-closed gap."""
    d = find_latest_prior(ticker, "investment-thesis", reports_root=reports_root, include_today=True)
    if d is None:
        return [], []
    try:
        th = json.loads((d / "investment_thesis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    cond = (th.get("conditions") or {})
    return (cond.get("thesis_invalid_if") or [], cond.get("entry_attractive_if") or [])


def _load_catalyst_calendar(ticker, reports_root=None):
    """Return the catalyst_calendar list. events.json is a SIBLING of the validated thesis
    artifact (not itself validated by find_latest_prior); a corrupt/absent events.json →
    [] (no catalysts) is an acceptable MVP soft-miss (catalysts are a secondary signal)."""
    d = find_latest_prior(ticker, "investment-thesis", reports_root=reports_root, include_today=True)
    if d is None:
        return []
    try:
        ev = json.loads((d / "events.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return ev.get("catalyst_calendar") or []


def _output_language(strategy_path="strategy.yaml"):
    """Read strategy.yaml:output_language (default zh-CN per rules/language.md). Fail-soft to default."""
    try:
        with open(strategy_path, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("output_language") or "zh-CN"
    except (OSError, yaml.YAMLError):
        return "zh-CN"


def build_probe(universe, cash, monitor_root, current_dirname, run_date, reports_root=None, output_language="zh-CN"):
    tickers = [u["ticker"] for u in universe]
    snap = _macro_snapshot(tickers) if tickers else {"ticker_prices": {}, "ticker_indicators": {}, "chart_statuses": {"ticker_prices": {}}}
    today = datetime.datetime.strptime(run_date, "%Y-%m-%d").date()
    per_ticker, warnings = [], []
    for u in universe:
        t = u["ticker"]
        facts = ticker_market_facts(t, snap, shares=u.get("shares"))
        if facts["price_status"] != "PASSED":
            warnings.append(f"{t}: price status {facts['price_status']}")
        stale = ticker_staleness(t, reports_root=reports_root)
        inv, ent = _load_thesis_conditions(t, reports_root=reports_root)
        calendar = _load_catalyst_calendar(t, reports_root=reports_root)
        cat_ev, cat_w = catalyst_evidence(t, calendar, today=today)
        articles, news_fetch_warn = _fetch_articles(t)
        news_ev, news_w = news_evidence(t, articles)
        warnings.extend(cat_w)
        warnings.extend(news_w)
        if news_fetch_warn:
            warnings.append(news_fetch_warn)
        evidence = (condition_evidence(t, inv, ent) + news_ev + cat_ev + [staleness_evidence(t, stale)])
        per_ticker.append({
            "ticker": t, "source": u["source"],
            "holding": ({"shares": u["shares"], "cost_basis": u["cost_basis"]} if u["source"] == "holding" else None),
            "price": facts["price"], "market_value": facts["market_value"],
            "indicators": facts["indicators"], "price_status": facts["price_status"],
            "indicators_available": facts["indicators_available"],
            "indicator_unavailable_reason": facts["indicator_unavailable_reason"],
            # news_status is an explicit per-ticker FACT (not just a line in warnings[]) so the
            # router can treat a failed feed as "news UNKNOWN" rather than "no material news"
            # (producer-consumer.md §4: absent != neutral). Common for foreign ADRs FDS 404s.
            "news_status": "failed" if news_fetch_warn else "ok",
            "staleness": stale,
            "evidence": evidence,
            "thesis_conditions": {"invalid_if": inv, "entry_attractive_if": ent},
        })
    return {
        "run_date": run_date,
        "output_language": output_language,                  # spec §8; default zh-CN (MVP)
        "universe": [{"ticker": u["ticker"], "source": u["source"]} for u in universe],
        "market_facts": {"market": snap.get("market"), "volatility": snap.get("volatility")},
        "cash": cash,
        "prior_evidence_ids": sorted(load_prior_evidence_ids(monitor_root, current_dirname)),
        "per_ticker": per_ticker,
        "warnings": warnings,
    }


def _atomic_write(path: Path, text: str):
    """Write then os.replace so a failed run never leaves a partial/invalid final artifact."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cmd_probe(args):
    uni, cash = load_universe(Path(args.state))
    if not uni:
        # Empty universe = no holdings AND no watchlist (or missing portfolio-state.yaml) =
        # nothing to monitor. Fail-closed with a non-zero exit so the orchestrator stops on the
        # exit code, not on parsing a stderr warning (producer-consumer.md §4). Write nothing.
        print("FATAL: empty universe — no holdings or watchlist in portfolio-state.yaml; nothing to monitor",
              file=sys.stderr)
        return 1
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)          # reports/monitor/YYYYMMDD/ may not exist yet
    out_dir = out.parent
    probe = build_probe(uni, cash, monitor_root=out_dir.parent, current_dirname=out_dir.name,
                        run_date=args.run_date, output_language=_output_language())
    _atomic_write(out, json.dumps(probe, indent=2, ensure_ascii=False))   # probe = router's sole input; never torn
    return 0


def _cmd_validate(args):
    """raw → HARD gate → stamp status → ATOMIC write of the final action_plan.json (only on pass)."""
    try:
        probe = json.loads(Path(args.probe).read_text(encoding="utf-8"))
        raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # a malformed/unreadable raw plan (the LLM can emit invalid JSON) routes through the
        # SAME FATAL path as a validation failure, so the SKILL's one repair retry applies
        print(f"FATAL: could not read probe/raw plan: {e}", file=sys.stderr)
        return 1
    probe_ids = {e["evidence_id"] for pt in (probe.get("per_ticker") or []) for e in (pt.get("evidence") or [])}
    # build_probe emits exactly one per_ticker entry per universe ticker (never skips), so this
    # set == probe["universe"], and it is precisely the set the router can actually cite evidence for.
    probe_tickers = {pt.get("ticker") for pt in (probe.get("per_ticker") or [])}   # == monitored universe
    try:
        validate_raw_plan(raw, probe_ids, probe_tickers)
    except MonitorValidationError as e:
        print(f"FATAL: action plan failed validation: {e}", file=sys.stderr)
        return 1     # HARD gate: do NOT write the final artifact
    prior = set(probe.get("prior_evidence_ids") or [])
    final = stamp_status(raw, prior_evidence_ids=prior, run_date=probe.get("run_date") or args.run_date)
    _atomic_write(Path(args.output), json.dumps(final, indent=2, ensure_ascii=False))
    return 0


def _cmd_render(args):
    probe = json.loads(Path(args.probe).read_text(encoding="utf-8"))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    _atomic_write(Path(args.output), render_digest(plan, probe))          # user-facing; never torn
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="scripts.monitor")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("probe")
    pp.add_argument("--state", default="portfolio-state.yaml")
    pp.add_argument("--output", required=True)
    pp.add_argument("--run-date", required=True)
    pp.set_defaults(func=_cmd_probe)

    pv = sub.add_parser("validate")
    pv.add_argument("--raw", required=True)
    pv.add_argument("--probe", required=True)
    pv.add_argument("--output", required=True)
    pv.add_argument("--run-date", default="")
    pv.set_defaults(func=_cmd_validate)

    pr = sub.add_parser("render-digest")
    pr.add_argument("--plan", required=True)
    pr.add_argument("--probe", required=True)
    pr.add_argument("--output", required=True)
    pr.set_defaults(func=_cmd_render)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
