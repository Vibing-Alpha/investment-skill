"""Pure builders for BQTierInputs / EventsReuseInputs.

Moves the 60-line inline heredocs out of the two SKILL.md orchestrations
into a testable unit. The SKILL.md files become true thin adapters:
resolve dirs + shell out to this module + react to the decision.

Design contract (spec §6.1, §7.2):

- **Fail-open on every read**: missing files, malformed JSON, or schema
  drift never crash the orchestrator, and an unreadable REQUIRED artifact
  collapses to the conservative outcome. This is enforced via
  `_read_json_status` (returns `(data, ok)`) so a read FAILURE is distinguished
  from a valid-but-empty document — without it, `_read_json_or(.., {})` would
  let an unreadable artifact read as "no signal":
    - financials (02): NO extractable statement period on either side —
      missing / unreadable / malformed / **valid-but-empty** — ⇒
      `new_financial_period=True` (full). Note the deliberate asymmetry vs
      estimates below: an empty 02 is treated as a failure, not a benign
      no-signal, because in this pipeline a successful fetch always populates
      `income_statements`, so an empty/period-less 02 means the financials
      fetch failed — and we must NOT no_op (reuse stale) when the authoritative
      new-quarter artifact is absent.
    - estimates (06): unreadable/wrong-shape on either side ⇒
      `estimates_hash_changed=True` (partial for BQ / rerun for events) —
      shared via `_estimates_changed`. Here a valid-but-empty estimates doc IS
      benign (legitimate "no analyst coverage") and hashes normally, so it does
      NOT trigger — only a genuine read/parse/shape failure does.
  **Exception — earnings (07) is an OPTIONAL artifact** (frequently absent /
  HTTP-400 for covered tickers): its absence/malformation is a benign "no
  signal" (no full-tier trigger), NOT a failure — 02 is the authoritative
  new-quarter signal, and forcing full on missing 07 would defeat delta.

- **Canonical classifier health**: `build_events_reuse_inputs` and
  `build_bq_tier_inputs` both consume classifier output via
  `materiality.validate_classifier_output`, which enforces the 3-part
  `input_healthy` check. Never subset the health contract inline.

- **Catalyst surrogate at probe time** (spec §7.2 Gate 3): since
  events.json doesn't exist yet when we're deciding whether to reuse
  the prior one, `build_events_reuse_inputs` derives today's catalyst
  candidates from the classifier `material_list` (material news within
  14 days surfaces as a "new catalyst"). An earnings-date surrogate that
  read `07_earnings.json["releases"]` was removed — that key never exists
  in the fetched artifact (which carries a single past-quarter `earnings`
  dict), so the branch was dead; real forward-earnings-date detection would
  need a next-earnings source not present in 07.

- **Anchor extracted ONCE before mutation**: `build_events_reuse_inputs`
  also returns the canonical anchor string so the orchestrator can
  thread it through Step 4 reuse_meta stamping without re-deriving
  from an already-mutated document.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Optional, Tuple

from scripts.delta.calendar import today_et
from scripts.delta.materiality import validate_classifier_output
from scripts.delta.probe import (
    BQTierInputs,
    EventsReuseInputs,
    diff_catalysts_in_window,
    hash_estimates,
    read_prior_events_run_date,
)
from scripts.delta.run_meta import RunMeta


CLASSIFIER_FAILOPEN_MATERIAL_COUNT = 999  # forces upgrade to partial/rerun


def _read_json_or(path: Path, default):
    """Fail-open read: returns default on any IO / parse failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _read_json_status(path: Path) -> Tuple[Any, bool]:
    """Read JSON, distinguishing a genuine read/parse FAILURE from a valid
    (possibly empty) document. Returns (parsed, True) on success; ({}, False)
    on missing file / OS error / parse error.

    `_read_json_or(.., {})` collapses "could not inspect the artifact" and
    "valid no-signal document" into the same `{}`, which makes the module's
    fail-open-to-conservative contract impossible to honor for hash/equality
    gates (e.g. `hash_estimates({}) == hash_estimates({})` reads as
    "unchanged"). Callers that must NOT treat a read failure as a benign
    no-signal use this status-returning read instead.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")), True
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}, False


def _estimates_changed(report_dir: Path, prior_dir: Optional[Path]) -> bool:
    """Estimates-hash gate, shared by BQ tier + events reuse (ONE impl, not two
    — producer-consumer rule #3). Returns True (conservative: forces a fresh /
    partial / rerun decision) when there is no prior, OR when EITHER side's
    `06_analyst_estimates.json` is unreadable/malformed — a read failure must
    not masquerade as "unchanged". A valid-but-empty estimates document is NOT
    a failure: it hashes normally, so a real no-estimates state does not
    spuriously trigger.
    """
    if prior_dir is None:
        return True  # no prior → force fresh
    cur_est, cur_ok = _read_json_status(report_dir / "data" / "06_analyst_estimates.json")
    prior_est, prior_ok = _read_json_status(prior_dir / "data" / "06_analyst_estimates.json")
    if not cur_ok or not prior_ok:
        return True  # read/parse failure on a required artifact → conservative
    if not isinstance(cur_est, dict) or not isinstance(prior_est, dict):
        # Valid JSON but wrong top-level shape (e.g. `[]` or `"error"`): a schema
        # failure, NOT a no-signal. hash_estimates() normalizes a non-dict to
        # all-null fields, so a wrong-shape doc would otherwise hash-equal `{}`
        # or another wrong-shape doc and read as "unchanged" → conservative.
        return True
    return hash_estimates(cur_est) != hash_estimates(prior_est)


def _load_classifier(classifier_path: Optional[Path]):
    """Return (material_count, healthy, material_list). Fail-open on
    any schema issue → (999, False, []).

    Must catch TypeError too: validate_classifier_output runs
    int(raw[...]) / list(raw[...]) which raise TypeError on shape
    drift (e.g. raw["material_list"] is a dict, not a list). Without
    this catch the probe flow crashes instead of falling open.
    """
    if classifier_path is None or not classifier_path.exists():
        return CLASSIFIER_FAILOPEN_MATERIAL_COUNT, False, []
    try:
        raw = json.loads(classifier_path.read_text(encoding="utf-8"))
        cls = validate_classifier_output(raw)
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        return CLASSIFIER_FAILOPEN_MATERIAL_COUNT, False, []
    if not cls.input_healthy:
        return CLASSIFIER_FAILOPEN_MATERIAL_COUNT, False, cls.material_list
    return cls.material_count, True, cls.material_list


def _latest_statement_period(financial_data: dict) -> Optional[str]:
    """Newest report_period across income_statements in 02_financial_data.json.

    Order-independent (uses max) and tolerant of missing/malformed rows.
    Returns None when no usable report_period is present.

    NOTE: 02_financial_data.json has NO top-level `latest_period` key — the
    canonical "latest reported quarter" signal is income_statements[*].
    report_period (the same field assemble.build_meta surfaces via
    00_validation.json:categories.financials.latest_period). An earlier
    version read a phantom top-level `latest_period`, so new_financial_period
    was silently always False on real artifacts.
    """
    if not isinstance(financial_data, dict):
        return None
    rows = financial_data.get("income_statements")
    if not isinstance(rows, list):
        return None
    periods = [
        r["report_period"]
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("report_period"), str)
    ]
    return max(periods) if periods else None


def _latest_earnings_period(earnings_data: dict) -> Optional[str]:
    """Latest report_period from 07_earnings.json.

    Handles the canonical single-dict `earnings` shape AND a list-of-records
    shape; tolerant of error payloads (string / missing / non-dict). Returns
    None when no usable report_period is present. (An earlier version read a
    phantom `releases` list that the real artifact never carries.)
    """
    if not isinstance(earnings_data, dict):
        return None
    ear = earnings_data.get("earnings")
    periods = []
    if isinstance(ear, dict):
        rp = ear.get("report_period")
        if isinstance(rp, str):
            periods.append(rp)
    elif isinstance(ear, list):
        for r in ear:
            if isinstance(r, dict) and isinstance(r.get("report_period"), str):
                periods.append(r["report_period"])
    return max(periods) if periods else None


def _new_quarter_since_prior(report_dir: Path, prior_dir: Optional[Path]) -> bool:
    """True when a new fiscal quarter was reported since the prior thesis run,
    judged by the SOURCE-STABLE 02 income-statement quarter-end `report_period`.

    Events-reuse Gate 6. Keys off 02 ONLY — deliberately NOT 07 earnings (see
    below). Compares the current run against the prior THESIS run.

    **Why 02-only (CRDO 2026-05-28 source-drift fix).** 07_earnings.report_period
    is NOT source-stable: Financial Datasets emits the fiscal quarter-END date,
    the FMP fallback emits the ANNOUNCEMENT date. So a cross-run 07 `!=` fires
    SPURIOUSLY whenever the 07 source flips (e.g. FDS→fmp_fallback) for the SAME
    quarter — the original (last-turn) version ORed a 07 comparison in and hit
    exactly this on CRDO (02 unchanged at 2026-01-31, but 07 drifted 2026-01-31→
    2026-03-02 → spurious rerun). 02's income quarter-end IS source-stable (FDS
    and FMP both emit quarter-end there) and co-advances with any genuine new
    quarter, so it is the authoritative signal. Dropping the 07 consult loses no
    real coverage: a bare earnings ANNOUNCEMENT before the 10-Q files has no new
    financials, and the post-announcement estimate revisions trip the estimates
    gate (Gate 2) → rerun anyway. Mirrors the BQ-tier new_earnings_release
    source-drift guard (both detectors now key off 02, not the drifting 07).

    **02 fail-CLOSED (to rerun).** On a delta run, if the 02 income period can't
    be established on EITHER side (missing / unreadable / malformed / valid-but-
    empty), fire the gate — a None period means the financials fetch failed and
    we must not reuse stale pre-print events on un-inspectable current data.
    (codex-review 2026-05-29 convergent finding.)

    **Baseline = prior_thesis_dir (most recent prior thesis run), not the events
    anchor.** Known bounded limitation: a stale pre-print events chain created
    BEFORE Gate 6 existed is not repaired by comparing against that run — but the
    7-day ceiling (Gate 5) forces a fresh re-anchor within ≤7 days, so such
    chains self-heal, and going forward no new stale chains form.
    """
    if prior_dir is None:
        return False  # no prior → schema gate ("" != "8.0") already forces rerun
    cur_fin = _read_json_or(report_dir / "data" / "02_financial_data.json", {})
    prior_fin = _read_json_or(prior_dir / "data" / "02_financial_data.json", {})
    cur_fp, prior_fp = _latest_statement_period(cur_fin), _latest_statement_period(prior_fin)
    if cur_fp is None or prior_fp is None:
        return True  # fail-closed-to-rerun: can't establish the authoritative 02 period
    return cur_fp != prior_fp


def build_bq_tier_inputs(
    report_dir: Path,
    prior_dir: Optional[Path],
    classifier_output_path: Optional[Path],
) -> BQTierInputs:
    """Construct BQTierInputs from filesystem state.

    - report_dir: today's run directory (has data/ with probe fetch).
    - prior_dir: most recent valid prior BQ run (or None for first run).
    - classifier_output_path: path to subagent-written classifier JSON.

    First-run case (prior_dir is None): days_since_last_full=999,
    new_financial_period/new_earnings_release default to False (no
    prior to compare against; tier decision collapses to full via the
    90-day ceiling). Caller should short-circuit to full tier when
    prior_dir is None rather than relying on this function — but the
    values returned are still safe defaults.
    """
    # Days since last full
    if prior_dir is not None:
        prior_rm = RunMeta.load_or_none(prior_dir / "run_meta.json")
        if prior_rm is not None:
            try:
                prior_date = datetime.date.fromisoformat(prior_rm.et_trading_day)
                days_since_last_full = (today_et() - prior_date).days
            except (ValueError, TypeError):
                days_since_last_full = 999
        else:
            days_since_last_full = 999
    else:
        days_since_last_full = 999

    # Financial period + earnings release diff.
    # Both detectors gate the FULL tier (so a new 10-Q / new earnings forces a
    # fundamental re-score). They read the REAL artifact keys — a newer
    # income_statements report_period, or a newer earnings report_period, than
    # the prior run. `!=` (not `>`) is deliberate: any change is the
    # conservative trigger (worst case = an unnecessary full run, never a
    # missed re-score). Helpers tolerate missing/malformed shapes (fail-open).
    cur_fin = _read_json_or(report_dir / "data" / "02_financial_data.json", {})
    prior_fin = (
        _read_json_or(prior_dir / "data" / "02_financial_data.json", {})
        if prior_dir is not None
        else {}
    )
    cur_fin_period = _latest_statement_period(cur_fin)
    prior_fin_period = _latest_statement_period(prior_fin)
    # Fail-open-to-full on a read/schema failure of the REQUIRED financials
    # artifact (module contract: BQ read failures collapse to the conservative
    # tier). On a delta run, if we cannot extract a statement period from
    # EITHER side (02 missing / malformed / **valid-but-empty**), we cannot rule
    # out a new quarter — force full rather than risk a no_op on un-inspectable
    # data. Valid-empty IS intentionally treated as a failure here (unlike
    # estimates): a successful fetch always populates income_statements, so an
    # empty 02 means the financials fetch failed — and we must not no_op when
    # the authoritative new-quarter artifact is absent.
    # Scoped to delta runs (prior_dir is None ⇒ first run ⇒ the 90-day ceiling
    # already forces full). NOTE: earnings (07) is deliberately NOT treated
    # this way — it is frequently absent / HTTP-400 for covered tickers, so its
    # absence is a benign "no signal", not a failure (02 is the authoritative
    # new-quarter signal); forcing full on missing 07 would defeat delta.
    if prior_dir is not None and (cur_fin_period is None or prior_fin_period is None):
        new_financial_period = True
    else:
        new_financial_period = bool(
            cur_fin_period and prior_fin_period and cur_fin_period != prior_fin_period
        )

    cur_earnings = _read_json_or(report_dir / "data" / "07_earnings.json", {})
    prior_earnings = (
        _read_json_or(prior_dir / "data" / "07_earnings.json", {})
        if prior_dir is not None
        else {}
    )
    cur_earn_period = _latest_earnings_period(cur_earnings)
    prior_earn_period = _latest_earnings_period(prior_earnings)
    new_earnings_release = bool(
        cur_earn_period and prior_earn_period and cur_earn_period != prior_earn_period
    )
    # Source-drift guard (CRDO 2026-05-28). 07_earnings.report_period is NOT
    # source-stable: Financial Datasets emits the fiscal quarter-END date, the
    # FMP fallback emits the ANNOUNCEMENT date. So when the 07 source flips
    # between two runs (e.g. FDS→fmp_fallback), report_period changes
    # representation for the SAME quarter and the cross-run `!=` fires a SPURIOUS
    # new_earnings_release → a wasteful `full` re-score. The source-stable
    # authority is 02's income-statement quarter-end (FDS and FMP both emit
    # quarter-end there): if it confirms the SAME latest quarter on both runs,
    # NO new fiscal quarter was reported, so suppress the 07-only trigger. This
    # never hides a real new quarter — when 02 advances, new_financial_period
    # already forces full; when 02 is un-inspectable, it fail-opens to full. A
    # bare earnings ANNOUNCEMENT before the 10-Q files has no new financials to
    # score and is correctly caught as `partial` via the estimates-hash gate,
    # not `full`. Mirrors the events-reuse Gate 6 fix (both detectors key off
    # 02, not the drifting 07). See rules/units.md / delta spec.
    if cur_fin_period and prior_fin_period and cur_fin_period == prior_fin_period:
        new_earnings_release = False

    # Estimates hash — fail-open-to-changed on an unreadable required artifact
    # (a read failure must not masquerade as "unchanged"); see _estimates_changed.
    estimates_hash_changed = _estimates_changed(report_dir, prior_dir)

    # Classifier material count (3-part health check)
    material_count, _healthy, _material_list = _load_classifier(classifier_output_path)

    return BQTierInputs(
        new_financial_period=new_financial_period,
        new_earnings_release=new_earnings_release,
        days_since_last_full=days_since_last_full,
        material_news_count=material_count,
        estimates_hash_changed=estimates_hash_changed,
    )


def build_events_reuse_inputs(
    report_dir: Path,
    prior_thesis_dir: Optional[Path],
    classifier_output_path: Optional[Path],
    window_days: int = 14,
    user_force_refresh: bool = False,
) -> Tuple[EventsReuseInputs, Optional[str]]:
    """Construct (EventsReuseInputs, canonical_events_anchor_et).

    The anchor is extracted BEFORE any mutation of prior events.json
    (spec §7.5 anchor-preserving rule). Orchestrator must thread the returned anchor
    through Step 4 reuse_meta stamping — do NOT re-derive from the
    already-mutated document.

    Returns (inputs, anchor):
      anchor is None when no prior events exists / meta is malformed
      (pre-delta artifact, first thesis run, or genuine schema drift).
      Orchestrator should treat None-anchor as implicit rerun.
    """
    # Anchor extraction — MUST happen first, before any other prior-doc reads
    prior_events_path = (
        (prior_thesis_dir / "events.json") if prior_thesis_dir is not None else None
    )
    anchor: Optional[str] = None
    if prior_events_path is not None and prior_events_path.exists():
        anchor = read_prior_events_run_date(prior_events_path)

    # Gate 2: estimates hash — shared with BQ tier; fail-open-to-changed on an
    # unreadable required artifact (read failure must not read as "unchanged").
    estimates_hash_changed = _estimates_changed(report_dir, prior_thesis_dir)

    # Classifier
    material_count, healthy, material_list = _load_classifier(classifier_output_path)

    # Gate 3: catalysts — surrogate from today's probe data (events.json
    # doesn't exist yet). Material news from the classifier forms the
    # candidate set.
    #
    # A prior earnings-date surrogate read `07_earnings.json["releases"]` — a
    # key the fetched artifact NEVER carries (it writes a single `earnings`
    # dict whose `report_period` is the LAST reported quarter, i.e. a PAST
    # date the forward window drops anyway). That branch was dead and its only
    # test fed an impossible shape, so it was removed. Real forward-earnings-
    # date detection would need a next-earnings-date source (not in 07); the
    # 7-day events ceiling (Gate 5) bounds any miss until then.
    cur_catalysts = []
    for m in material_list:
        if isinstance(m, dict) and m.get("headline"):
            cur_catalysts.append({
                "event": m["headline"][:80],
                "date": today_et().isoformat(),
            })

    # Prior catalysts + schema version
    if prior_events_path is not None and prior_events_path.exists():
        prior_doc = _read_json_or(prior_events_path, {})
        prior_catalysts = prior_doc.get("catalyst_calendar") or []
        schema_version = (
            prior_doc.get("meta", {}).get("output_version", "")
            if isinstance(prior_doc.get("meta"), dict)
            else ""
        )
    else:
        prior_catalysts = []
        schema_version = ""

    new_catalysts_in_window = diff_catalysts_in_window(
        cur_catalysts, prior_catalysts, today_et(), window_days=window_days
    )

    # Gate 5: days_since
    if anchor:
        try:
            anchor_date = datetime.date.fromisoformat(anchor)
            days_since_last_events_run = (today_et() - anchor_date).days
        except (ValueError, TypeError):
            days_since_last_events_run = 999
    else:
        days_since_last_events_run = 999

    # Gate 6: a new fiscal quarter reported since the prior thesis run (the
    # earnings-recency hole that wrongly reused MRVL's pre-print events on
    # 2026-05-29). Compared against prior_thesis_dir, mirroring decide_bq_tier.
    new_quarter_reported = _new_quarter_since_prior(report_dir, prior_thesis_dir)

    inputs = EventsReuseInputs(
        classifier_material_count=material_count,
        classifier_input_healthy=healthy,
        estimates_hash_changed=estimates_hash_changed,
        new_catalysts_in_window=new_catalysts_in_window,
        events_schema_version=schema_version,
        days_since_last_events_run=days_since_last_events_run,
        user_force_refresh=user_force_refresh,
        new_quarter_reported=new_quarter_reported,
    )
    return inputs, anchor
