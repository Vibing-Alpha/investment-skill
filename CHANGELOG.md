# Changelog

Release notes for the distributed skill system. Newest first. Managed by
`scripts/release.py`; recipients see the latest entry on update.

## v1.19.1 — 2026-08-31

- Portfolio log staleness advisory + atomic-write orphan fix

## v1.19.0 — 2026-08-31

- market-cap split-basis guard, SBC period-basis probe, and the statement-integrity coverage checks

## v1.18.0 — 2026-08-30

- Repair a null anchor close from the same Yahoo response; make an unanchored session visible where it is acted on

## v1.17.0 — 2026-08-30

- New: on a host that inlines small tool results, `/track-record` can now archive them. `python3 -m scripts.track_record transcript list|extract` moves a recorded result out of the session log BY PROGRAM (never by retyping it), so orders/positions/balances/performance pulls no longer go unarchived. A result too large to inline is detected and refused with the path of the file the host actually wrote, so you archive that instead of a 1.5KB placeholder.

Fixes: every skill now fails closed when a run-directory resolution returns nothing — on a root session (Cowork) an empty path wrote the run into `/` and exited 0, silently placing the analysis where the delta layer can never find it. On a first run `--prior` is omitted rather than passed as the root path `/summary.changelog.md`, which could import unrelated content as a ticker changelog.

## v1.16.0 — 2026-08-30

- - macro: a whole-structure outage was invisible at every status exit. When the provider drops the anchor session's daily bar, regime closes go null, every holding's price structure goes `unknown` and the volume leg becomes unusable — while `chart_statuses` read `{"status": "PASSED"}` for all 22 symbols and the process exited 0, because the status layer only inspected the meta QUOTE. Each entry now carries `anchor_session_covered` (mirroring the structure block, one computation) and `last_bar_session`, and one stderr WARN names the anchor and every affected symbol. `/portfolio` Step 4 gained hard STOPs for a held ticker that is unanchored and for a null regime leg on any index your principles read.
- The "clear the stale artifact first" guards had never once fired on a delete-restricted mount: `rm -f` returns EPERM there for an existing file, so a missed agent write could leave an earlier same-session artifact to be stamped as this run's output. `scripts.clear_stale` replaces them at 19 sites — delete, else empty to zero bytes (which every consumer here reads as absent), else refuse — and refuses a path shaped like an unset shell variable.
- score-business proved its two-phase validation merge by the ABSENCE of a temp file, which on that same mount made the check a permanent false positive. `validation_merge` now stamps `two_phase_merged` and Step 4.5 reads it. The score clearing reads the decided tier from `.run_state.json` instead of a substituted literal, which was silently skipping the fundamental clear on a full run.
- track-record: new read-only `coverage` subcommand answers "is this quarter already pulled" — it emits the resolved UTC window from the same resolver `coverage_gap` uses, and the EFFECTIVE completeness verdict, not the pulling session's own self-report. Deciding on the stored field skips a quarter that is really truncated, and once broker retention closes that data is gone. `unlinked --since` bounds a backlog that a first session met as 555 rows, always disclosing how many it hid.
- The news classifier was asked to judge `fetch_timestamp_today` from inputs that never contained a fetch time. Both dispatch paths now pass it, and the freshness basis is the trading SESSION rather than the calendar day — judged against the calendar it is false on every non-trading day, which spent a full events re-analysis on a batch with no new article in it.
- `date_precision: confirmed` now requires an issuer- or exchange-published date; a third-party calendar is `estimated`, however exact it looks.
- New suite layer: the SKILL.md bash blocks are now EXECUTED, not just read. Four review rounds read them; the round that ran them found in minutes that the run-directory assignment was exit-checked nowhere. `make e2e-mutate` breaks the product 15 ways and asserts the layer goes red — it caught 3 of 8 before that was measured.

## v1.15.0 — 2026-08-29

- Indicators no longer compute on an incomplete trailing bar. A fetch that runs while the market is open got a mid-session stub as the newest daily bar, and MACD, Bollinger, ATR, RSI and volume all consumed it — measured on a live 13:31 ET run, the volume ratio read 0.59 where the completed session was 0.88, a fabricated 'bearish divergence', and ATR came out ~3% low so every ATR stop sat too tight. indicators.json now carries an input_completeness block saying which session the read is as of.
- New data_quality block in 02_financial_data.json surfaces three provider defects the existing anomaly detector is not contracted to see: a sign that flips inside one column (interest_expense, capital_expenditure), a metrics_snapshot lagging the statements, and a statement basis that changes mid-series (a registration statement beside periodic filings). Evidence only — no value is altered or called corrupt.
- 07_earnings.json gains surprise_eps_pct and surprise_revenue_pct. The single surprise_pct is the EPS surprise but sits between the revenue keys, and was read as the revenue beat on five of six tickers in one run (48.56% against a true 1.05%). surprise_pct is kept as a deprecated EPS alias.
- peer_multiples.json gains per-metric dispersion (min/max/ratio) and a separately-named cross-currency ratio median set. A median tells you nothing about whether the cohort agrees — one run published a forward P/E median of 270.7 across peers spanning 23 to 1414 — and the USD-only currency filter had collapsed another cohort to a single contributor per metric even though every multiple is dimensionless.
- Fail-closed: a WebSearch citation packed inside another kind's brackets ([Filing: ...; WebSearch: ...]) escaped every binding check. Marked artifacts now refuse it. Two stored artifacts carrying that shape will need re-running.
- An absent share_based_compensation column no longer reads as 'SBC is low' in growth-stock detection, and a failed staging cleanup no longer masks the schema error it was cleaning up after.
- Prompt and reference corrections where the text asserted something the code or data does not do: the growth-mode trigger table, the leverage-basis rule, analyst-estimate staleness, the alpha dimension-split axis, insider 10b5-1 handling, and the superseded partial-bar heuristic in both gotchas files.

## v1.14.4 — 2026-08-23

- The alpha scan's events_freshness spec pointed at the wrong field and sliced a UTC timestamp where the code converts it to ET — a date label that could read a day early. The prompt now names the one helper that derives it instead of restating the rule a second time.

## v1.14.3 — 2026-08-22

- The /portfolio refresh plan no longer shows numbers it has no source for: the per-ticker cost placeholders and the grand total are gone, and the two BQ buckets — which the classifier cannot tell apart — are one. A fund now has its own line.

## v1.14.2 — 2026-08-22

- A failed changelog append no longer reports success: three skills ran an unconditional cleanup after an unchecked producer, so the block exited 0 with the changelog unwritten and the staged note deleted. Each now stops and says what was not written.
- /portfolio's refresh plan identified a fund from data a later step produced, so it could route one down the stock cascade. It now reads the per-ticker instrument registry, which is available where the question is asked.
- README and CLAUDE.md list /track-record, and state where a broker is actually required: Interactive Brokers only, via the IBKR MCP server, read-only. /portfolio proposes orders you execute yourself — the vocabulary is not tied to any broker.

## v1.14.1 — 2026-08-22

- README and first-run setup now name /etf-thesis and the etf_policy approval — the command list a new user reads was still stock-only.

## v1.14.0 — 2026-08-22

- ETF support: non-leveraged equity ETFs become buyable instruments.
- New /etf-thesis skill — identity detection, eligibility screens (leverage, composition, concentration, liquidity), run-day readiness, and a bound thesis whose every number is checked against the artifact it came from.
- Buying one requires your own approval: add an etf_policy block to strategy.yaml (see strategy.example.yaml). Approvals expire after 90 days and no provider datum substitutes for one.
- /portfolio, /monitor, /write-report, /generative-ui and the two stock skills all recognise a fund and route it correctly; a fund reaching /score-business is forwarded rather than scored.

## v1.13.0 — 2026-08-20

- pull takes --response FILE instead of stdin. The steady-state per-session payload drops from ~331KB to ~61KB, so the tool no longer depends on your harness persisting an oversized tool result to a file — the limitation v1.12.0 shipped with is gone. SCOPE widens accordingly: Codex, Cursor and OpenCode are expected to work and are UNTESTED; say which harness you are on when you report a capture.
- NEW subcommand: due --journal <path> --as-of <Z> — the outstanding predictions past their deadline, as CSV. The skill no longer asks an agent to read the journal and compare timezone-aware deadlines by hand; that comparison is arithmetic and is now tested code.
- Completeness is decided at READ time over the whole archive, not trusted from the value stored when a pull was archived. A stored verdict goes stale the moment a fuller pull arrives, so two operators pulling in a different order got different permanent facts from identical evidence. coverage_gap, report and pull now all reduce the same way.
- Trades coverage is the UNION of complete pulls' windows. A quarter no longer needs one envelope covering all of it, which is what forced the 331KB pull; small rolling captures now compose. Every other tool keeps its single-envelope rule unchanged.
- Instrument identity comes from the fills' own company_name instead of a positions snapshot, so a trade is journaled under the ticker it had AT THE TRADE rather than today's. unlinked gains an eighth column carrying that name, with ?MISSING-ON-SOME and ?CONFLICTING as distinct outcomes — absence and disagreement end differently. The journal accepts company_name_at_trade as an identity.
- report names on stderr any stretch of the quarter that only one pull observed. summary.md is unchanged byte-for-byte: the disclosure is stderr-only by design, so a rendered report stays comparable across runs.

## v1.12.0 — 2026-08-18

- NEW: `scripts.track_record` — a broker-fact archiver, an append-only intent journal, and a quarterly summary renderer, as a runtime you invoke directly (`python3 -m scripts.track_record pull|tag|open|unlinked|report`). Standard library only. It exists because broker history expires and the reasoning behind a trade cannot be reconstructed from a fill afterwards; it preserves both and computes almost nothing on top. The property worth reading it for: every number degrades to `unavailable (reason)` rather than guess, and the reason names what was missing — a coverage gap, a conflicting execution, a corrupt benchmark pin each blank the figures they feed and no others. See `scripts/track_record/README.md`.
- track_record SCOPE, stated as disqualifying conditions: Interactive Brokers only (the frozen decisions are IBKR's response contract — the period-to-UTC-range mapping, `trade_id` as the execution key, `cps` as a decimal TWR series; another broker needs its own probe); SINGLE ACCOUNT only (the IBKR MCP wire format has nowhere to put an account id, so an archive spanning two accounts merges their orders and no code gate can detect it — the rows carry no account either way); and envelope fidelity depends on your harness persisting oversized tool results to a file, because a real YEAR_TO_DATE trades response exceeds 300,000 characters and retyping it is not a fidelity-preserving operation.
- track_record is EXPERIMENTAL: one operator, one IBKR connection. The first hour of real use falsified a frozen decision that nine cold review cycles had left standing — the broker restates settled `order_id`s, which the conflict rule had been reading as a corrupted execution. Expect the same of the remaining rulings until a second account has exercised them.
- The workflow layer that drives this runtime is deliberately NOT published. Its backup step pushes with no refspec to whatever `origin` a clone has — correct for a private dev repo, wrong for a clone of a public one, where it would offer to commit your broker archive and trade journal to a repository you may not control. Until a backup topology exists that is correct for a stranger, you get the runtime and write your own orchestration around it.

## v1.11.1 — 2026-08-18

- /portfolio KNOWN ISSUE (open, not fixed): an open order whose `action` and `type` disagree on side — `{action: buy, type: stop_sell}` or `{action: sell, type: stop_buy}` — is counted TWICE in the `extreme_down` stress scenario, because it matches both the buy classifier and the stop classifier. The direction is bounded: a double-applied order always spends twice, so it can cause a false REFUSAL of your orders, never a false pass. It needs a self-contradictory hand-written `open_orders` entry, which nothing currently rejects. Full write-up and reproduction in `rules/portfolio-safety.md`; fixing it changes every `extreme_down` number, so it is deferred to the money-path rigor path rather than patched.
- /score-business + /investment-thesis KNOWN ISSUE (open, not fixed): for a non-USD-statement ADR, a quarter can arrive with `net_income`, operating cash flow and capex each under-converted by exactly one FX factor while `revenue` and `operating_income` are converted correctly — and NO detector fires, because neither of `detect_anomalous_quarters`'s two signals has those fields as an input. Observed on MRAAY: a published `corrected_pe` of 82.963 against a restated 56.09, a ~48% overstatement that reached `bq_analysis.json` and `summary.md` with `anomalous_quarters` empty. Unlike the issue above the direction is NOT bounded — the same row understates margins and growth while overstating P/E. Detection currently rests on the scoring agents noticing; the pipeline contributes nothing. Full write-up in `rules/units.md`.
- No behaviour change in this release. Both entries above are disclosures of defects that already existed, written down where the agent reads them.

## v1.11.0 — 2026-08-14

- /portfolio: order recommendations may now use the full order vocabulary (gtc, limit, loc, market, moc, stop, stop_limit, stop_market) and fractional shares — the decision prompt previously allowed only market/limit/stop, which was narrower than the system has accepted for some time. Each type now states which price field it carries.
- /portfolio: a run STOPS when a HELD position has no usable price, instead of quietly proceeding on a partial book. Previously nothing refused this before the recommendations reached you.
- /portfolio: no buy/add is proposed on a ticker whose price fetch failed — such an order could pass validation and then leave the run unable to write its decision log.
- /portfolio: a stale validator result from an earlier run can no longer be read as this run's; only (clean exit + this run's own artifact + passed) is accepted.
- /portfolio: the stress-test warning shown when open_orders is empty no longer claims scenario equalities that are false whenever a stop sell is proposed; it now states what was actually covered.
- Docs: the stress-scenario descriptions, order/price rules and default risk principle now match validate.py exactly (share-ledger caps, stop-limit non-fill, stop SELLS in Defensive). The 2026-04-07 portfolio design spec is marked SUPERSEDED — it described a constraint-extraction and caching architecture that no longer exists.

## v1.10.0 — 2026-08-13

- ACTION REQUIRED for existing installs — if your `strategy.yaml` has `risk.cash_target` or `risk.max_sector`, delete those lines. `/portfolio` now stops with a message naming the key: neither ever enforced anything (`cash_target` had no consumer; `max_sector` needs sector lookup that is not implemented, so it would fail closed on every run).
- Hard constraints now come from `strategy.yaml`'s `risk:` block instead of being read out of your principle prose, and `strategy.compiled.yaml` is re-derived by a deterministic compiler on every run — there is no cache branch. Previously a limit you wrote under `risk:` could be silently ignored while the decision log kept attesting the superseded policy, because the freshness hash covered only `principles`. It now covers `principles` + `principle_notes` + `risk`, so any policy edit takes effect on the next run.
- Validator: a proposed order that DEEPENS an existing constraint breach is no longer filed as merely 'pre-existing' — the all-fills check now compares margins per constraint, so only a set that leaves the breach no worse is downgraded to a warning. And the crash scenario values a stopped ticker at its stop price rather than the pre-trigger quote, which had let a real concentration breach pass on the shrunken denominator.
- If your `strategy.yaml` has no `principles:` (the shipped example ships them commented out), the ten canonical default principles are now compiled into the artifact. Previously it could be written with an empty principle list, which silently disabled citation-range validation in the decision log.

## v1.9.2 — 2026-08-13

- fix(fetch): the segmented-revenues filing-notes fallback now settles at merge time — a ticker whose structured segmentation feed is empty but whose revenue mix IS recoverable from the filing's disaggregation note was recorded as a flat data loss, and the artifact claimed fallback_status UNAVAILABLE while the note sat beside it on disk. The promotion was unreachable in the two-phase score-business fetch (its rule ran in the phase that had no filing; the filing arrived in the phase where the rule was skipped). segmented_revenues is auxiliary, so no decision or gate changes.

## v1.9.1 — 2026-08-11

- portfolio Step 0: the run MUST open the prior decisions.json before Step 1 and explicitly reconcile every prior non-hold/non-skip decision (carry forward / supersede / drop, and why). portfolio_log review prints only the prior run's PATH — never its decisions or proposed orders — so a prior 'reduce', its proposed sell, and a user ruling recorded in that decision's rationale were all invisible, and the next run wrote a contradictory 'hold' on identical anchored prices. The gate also names the confirmation/execution-outcome order fields review never prints (so an order the user rejected or already filled cannot be silently re-proposed), and fail-closes on a non-zero review exit. Doc-layer only — no script changes.

## v1.9.0 — 2026-08-10

- Rotation solvency: a proposed market sell can now fund a proposed buy in one validated order set. Order prices are bounded by the live quote wherever a self-reported price is load-bearing and uncapped, and capped orders project at their stated contractual bound. New guards: working_buys_unfunded, a ticker_prices binding on the validator artifact, and a strategy binding on the decision-authoring seal.

## v1.8.0 — 2026-08-09

- feat(portfolio): closing-basis price structure + decision-evidence layer — macro.json now carries per-ticker ticker_price_structure (closing-basis prior-high/breakout status, hold detectors for breakout/MA/cluster with reclaim volume, high-water drawdown) and universe_rebound_structure (cohort selloff event + per-member recovery); the decision log persists this evidence and cmd_write refuses decisions whose evidence contradicts the run's macro; /portfolio prompt teaches the two parallel entry paths (closing breakout / repair) with named strength lenses

## v1.7.7 — 2026-08-07

- fix(source_tag): empty descriptor no longer swallows the next tag out of validation

## v1.7.6 — 2026-08-05

- extract_fcf / historical_multiples / adr.correct: detect year-to-date CUMULATIVE cash-flow rows before TTM aggregation. US 10-Q cash flows are year-to-date by construction; when a provider does not de-cumulate them the row's quarter key stays well-formed, so the DL4 aligned-window gate accepts it and every consumer that SUMS the window is silently corrupted. Found in this repo's own stored data: TTM FCF understated 2.40% on one ticker (carried into reverse_dcf), TTM D&A overstated 47% on another, which DEFLATES EV/EBITDA and makes a name look cheaper than truth.
- New shared guard in scripts/schemas/quarter_window.py, called by the three producers that aggregate a window. Two tiers: an attested period_value_basis=ytd is authoritative and rejects the whole window; otherwise a depreciation-and-amortization ratio against the window's fiscal-Q1 standalone baseline, scaled by fiscal-quarter index, suppresses only the affected lens. Measured over 91 corpus windows: 4 true positives, 0 false positives.
- Fails OPEN when a window cannot be fully examined, and DISCLOSES that via the new cumulative_check_ran field (extract_fcf, adr_correction) or a named-window warning (historical_multiples) — so 'not checked' can no longer read downstream as 'checked and clean'. The valuation prompt routes on both.

## v1.7.5 — 2026-08-04

- Operand-fidelity audit converged: [Calc:] operands verified against upstream API raw values across the analysis corpus; provenance-attribution corrections in stored analyses. No runtime/script changes.

## v1.7.4 — 2026-08-04

- Two-angle probe hardening (14 cold rounds): CLI path stdout prints as_posix at all 10 sites — native-Windows venv python emitted backslash paths that SKILL bash captures interpolated into inline -c string literals as octal escapes (silent path corruption); [Calc:] source-tag integrity — prompt exemplars rewritten to compliant [API: <file>, <field>] / formula forms, payload + universal-comparison-enumeration + denominator-binding rules added at the emitting prompts, [API:] KIND definition widened to run-directory files, and the DL3c FX note now carries the conversion cert's API-channel tags instead of instructing a fabricated WebSearch binding

## v1.7.3 — 2026-08-04

- 4-angle audit hardening (33 cold-review rounds): unconditional authoring-seal enforcement incl. all-hold runs, malformed-seal refusal + atomic seal writes, decisions JSON/MD pair rollback on torn writes, principle-citation range checks; screen: trading-day streak walk, completed-session liquidity floors, watchlist floor-drop disclosure, vendor-alias support; research-industry: pre-dispatch destination clear, OTC ADR universe alignment, ETF slug mapping fixes (bare cloud/ev, oil/media segment matching); thesis: guard-kept fresh events drop stale reuse context

## v1.7.2 — 2026-08-03

- Data-contract release: fetch↔scaffolding interface audit (3 fix commits, cold-reviewed to 2x CLEAN) — FMP financials history 8→16 quarters (live-verified); forward agent input list gains 02_financial_data (estimates-vs-actuals + currency gate); ADR market-cap guard in valuation prompt (never price×ordinary-shares on non-1:1 ADRs); fcf_inputs errors[] field-name fix in reverse-DCF skip template; dead filing_intelligence reference removed on both sides; prompt data-reality notes (earnings history, estimate revisions, TTM-slice continuity, span-labeled valuation band); declined-cascade fetch includes 08_institutional.

## v1.7.1 — 2026-08-03

- Hardening release: cold-review rounds 15-28 (12 fix commits) — FX/quote correctness (current_investments FX conversion, bool-as-number gates, finite-positive price/MA/regime gates, FED-rate unit domain), fail-closed config/identity (invalid dimension_weights refusal, cross-ticker score gate), orchestration abort-window consistency (exit guards, --no-completed stamping, copy-first archive, partial-stress refusal, run_meta writer preservation), delta/calendar gates (classifier shape, events structural floor chain, holiday-coverage warning, no vacuous PASSED).

## v1.7.0 — 2026-08-03

- Fix live PEG-horizon defect: peer pegRatio (5y-expected basis) can no longer be crossed with 1y growth to imply a fair-value anchor; peers.py emits a per-field basis map + fetched_at
- Price-basis consistency: ATR highs/lows adjusted onto the close basis (shared adjust_high_low); historical-multiples anchor uses raw close (removes the dividend-payer understatement) with price_basis disclosure
- Discount rate: per-ticker fetch now emits us_10y (^TNX, freshness-gated); CAPM cost of equity prefers the 10Y treasury (FED fallback warns), sanity-only clamp [5%,25%] replaces the 15% cap that suppressed every high-beta name; reverse DCF carries bracket metadata and full sensitivity; FCF/share no longer pre-rounded
- Delta time/provenance batch: calendar-today resolver cutoff, fresh-destination guards (scores/events/data categories), negative-age conservatism, inclusive materiality window, currency-aware estimates hash, NYSE half-day schedule, _source_date vintage preservation, [:10] catalyst pruning
- Ticker normalization: hand-edited amd/' AMD ' now resolves instead of silently vanishing from /portfolio and /monitor; path-unsafe inputs fail loudly; SKILL regexes accept hyphenated share classes (BRK-B); FMP statement rows retain filing_date
- Same-session rerun integrity: all always-fresh artifacts cleared before agent dispatch so a missed write fails loudly instead of silently re-serving the earlier same-session output; technical indicators always recomputed from the current price series
- 9 cold-review rounds on the final code (last two consecutive CLEAN); full CI + audit + real-network smoke green

## v1.6.0 — 2026-08-02

- fix (IMPORTANT — affects entry/reduce timing): /portfolio run-day technical indicators were computed from the LIVE partial session bar — a mid-session run read a genuinely volume-confirmed breakout as 'no volume confirmation' and could falsely arm a momentum-weakening reduce. Ticker indicators and benchmark relative-strength returns are now anchored to the last completed ET session; the live quote remains the display price
- fix: the ratio-ADR unit-mismatch guard can no longer be silently disarmed — a missing, corrupt, stale or mixed-shape adr_correction.json anchor now fails closed instead of running USD-reporting ratio-ADRs (BP/SHEL class) in non-ADR mode, which produced 2-6x per-share valuation errors with status ok; fetch writes a static-fallback anchor when the data-driven classification is unavailable, and BP/SHEL/HSBC/BHP joined the known ratio-ADR table
- fix: fed_funds in /portfolio's macro snapshot could be weeks stale while reading PASSED — any disk-cached report short-circuited the live rates fetch. Rates are now fetched live FIRST; a disk cache is used only after live failure, always marked PARTIAL with its mtime-derived vintage, and the decide prompt must read rates_status before citing the policy rate
- fix: peer valuation medians can no longer be contaminated by an unrelated same-symbol foreign issuer — exchange-suffix-resolved peers are identity-unverified, excluded from medians and audit-only for the valuation agent
- fix: portfolio safety-gate hardening — open orders with missing/null/absurd share counts or corrupted prices are structured violations instead of zero-impact projections or crashes (NaN/inf/huge-int passed bare comparisons at three boundaries); YAML boolean dimension_weights rejected; malformed dimension score files excluded audibly instead of crashing assemble mid-write
- fix: reverse DCF returns a skipped status for NaN inputs and discount_rate <= terminal_growth instead of a confident -20% implied growth; fcf shares provenance cites the aligned-window balance row actually used; thesis Step 5a/alpha-scan resolve the prior-day BQ dir on the declined-cascade path instead of crashing

## v1.5.0 — 2026-08-02

- User-strategy lens now reliably reaches the thesis layer (mandate.style_notes authoritative; evidence layer stays lens-free); macro regime_inputs adds ma200/high_52w/off_52w_high_pct for regime classification; portfolio context carries full portfolio-state incl. NAV-peak circuit-breaker bookkeeping (ratchet + reconfirmation); thesis conflict heuristics are style-dependent defaults.

## v1.4.0 — 2026-07-31

- New: run-degradation gate — a degraded data fetch now travels WITH the analysis instead of vanishing behind it: bq_analysis.json meta records validation_status + degraded_categories, the delta layer classifies a degraded BQ for re-run, and /portfolio flags or blocks decisions built on degraded data (blocked_by_data_integrity) instead of silently trading on incomplete inputs
- fix: a provider response for the WRONG or malformed company can no longer be silently relabelled as your ticker — every price/metrics/company/financials/analyst/earnings/institutional row is identity-bound to the requested symbol on both the primary and the FMP fallback, and metrics must carry a true TTM window
- fix: dozens of status-layer fail-opens closed across the fetch pipeline — rows-present-but-unusable now reads as drift (gates) instead of absence (exempt), fallback results can no longer contradict a recorded absence, unparseable dates/periods fail conservative, and news/analyst usability is judged on a single same-row basis
- fix: two-phase score-business fetch can no longer erase phase-1 degradation on merge failure — the merge is exit-gated; same-day validation files are treated as unverifiable to all later readers (a same-day re-probe may have rewritten them)

## v1.3.0 — 2026-07-27

- fix: data fetching works again behind a proxy — the HTTP layer was pinned to a direct connection, so on hosts that reach the providers only via a local/corporate proxy every price and macro call came back HTTP 403 and fetch reported FAILED with empty price data
- fix (IMPORTANT — re-run affected analyses): valuation multiples were computed on a SINGLE-QUARTER denominator, making P/E, P/S and EV/EBITDA roughly 4x too high in current_from_api, which the valuation prompt cross-checks against your own TTM series. Metrics now use the TTM window on both the primary and the FMP fallback. Any investment_thesis produced before this release carries the inflated figures — re-run /investment-thesis for tickers you still rely on
- fix: institutional 13F data was failing outright — the provider retired the endpoint (HTTP 410). Migrated, and the panel now reports holder_count separately from row_count, flags option lines (calls/puts) so they are never summed as equity, drops filings more than a year stale, and states which report periods it spans
- fix: segmented revenue no longer degrades to prose-scraped filing notes when the primary returns no coverage — an FMP fallback supplies structured product and geography rows, and a failure in one dimension no longer discards the other
- fix: EPS consistency check no longer emits false warnings — it now matches whichever share basis the provider used and skips comparisons whose currency or per-share basis cannot be established, instead of comparing across them

## v1.2.4 — 2026-06-19

- fix(monitor): advice-scan no longer false-flags factual 增持/持有/部署 prose — 新增持仓 / 机构持有股份 / 部署新产能 are facts a monitor reason cites, not trade advice; advice senses stay caught by 加仓·继续持有 + EN hold/deploy/overweight

## v1.2.3 — 2026-06-16

- Fix: news for FDS-uncovered foreign ADRs (e.g. MRAAY) is no longer silently zeroed — an FDS news HTTP 400 (ticker outside FDS's universe) now falls back to Finnhub, matching the existing 404 behavior. 401/403/429/5xx still surface as errors.

## v1.2.2 — 2026-06-12

- allocate-bq-run: when a Cowork virtiofs/FUSE orphan dentry leaves reports/<T> stat-visible but uncreatable, the FATAL now appends the verified host-side Write re-materialize recipe (fail-close semantics unchanged).
- score-business gotchas: document the three Cowork mount quirks (phantom directory, delete-blocked EPERM, in-place-overwrite truncates to old byte length).

## v1.2.1 — 2026-06-11

- Safety gate hardening: present-but-null state/price data (bare holdings:/cash:/open_orders: keys, null prices from a failed fetch, shares: null) now fails closed with structured violations instead of crashing or silently passing
- Oversell detection: a proposed sell exceeding held shares, or a SINGLE broker open order selling more than held, is now a violation (stop+limit OCA brackets remain legal); an unrecognized --orders file shape is refused instead of validating zero orders
- Decision log: write-time schema self-check — a log that tomorrow's review would reject is refused at write time; order costing uses the same est_price→limit_price→price chain as validation; open-order limit_price renders in the MD
- Preflight: the open_orders key requirement now fails fast at config_gate (was: refused only at the final write step); watchlist-only states (bare holdings:) and int-shorthand holdings (TICKER: 100) are accepted
- BQ staleness clock keys to the last FULL-tier run — frequent no-op/partial runs no longer reset the 90-day re-score ceiling
- Historical multiples: when the newest reported quarter cannot be aligned yet, summary.current is flagged (current_lags_newest_reported + warning) instead of silently presenting an older quarter as current

## v1.2.0 — 2026-06-11

- Price feed: stale Yahoo meta quotes (thin OTC ADRs) now lose to the newer chart bar in the same response; per-ticker price vintage surfaced as price_as_of/stale_meta_quote
- Regime classification: inputs anchored to the last completed ET session (regime_inputs block) — a live pre-market VIX can no longer flip risk_off to risk_on against prior-close indices
- portfolio-state.yaml: open_orders is now a REQUIRED key (write open_orders: [] to attest none) — decisions attach per-ticker open-order snapshots, warn on direction conflicts, and stress scenarios must cover working orders
- portfolio-state.yaml: optional symbol_aliases map (vendor/broker symbol split, e.g. ADR depositary renames) wired into the price fetch for /portfolio and /monitor
- Same-day /portfolio reruns archive the prior decisions pair as decisions.{run_id}.*; review now sees an earlier run today
- Decision-log hardening: refuses missing/failed stress artifacts and non-ET-day output dirs; scripts.validate exits 1 on FAIL; limit_price honored in cash projections
- Report-dir allocation failures fail visibly with do-not-redirect remediation (/tmp is ephemeral in Cowork)
- SKILL prose hardening: no bare $N literals (harness positional-arg substitution defense) + ER-lint [context-only] marker

## v1.1.0 — 2026-06-10

- Cowork thin-plugin packaging: install from this repo's marketplace, then run /stock-v7-setup (clone-launcher: persistent clone + venv in your project folder)
- All 8 skills hardened for Cowork fresh-shell execution (per-step root resolve, state rehydration, venv-aware $PYBIN)
- Money-path config gate: graded single-root guard (portfolio reads block on wrong/unconfirmed clone; single-ticker analysis warns)
- New: distribute doctor (one-shot env/config/deps/network diagnosis), bidirectional plugin-vs-clone version-skew warnings
- Anti-hallucination: WebSearch-sourced claims now bind outlet + URL + access date, validated at load (fresh runs only; old reports unaffected)

## v1.0.15 — 2026-06-08

- docs(macro): drop a stale comment that still claimed both 10Y-2Y and 10Y-5Y spreads are emitted (the 2Y shim was removed in v1.0.13; only spread_10y_5y is emitted). Comment-only, no behavior change.

## v1.0.14 — 2026-06-08

- fix(portfolio): Step 8 decision-log call used an undefined $BLOB after the P4 Write-tool switch — use the literal .decisions_blob.json path so the (non-optional) decision-log write doesn't break

## v1.0.13 — 2026-06-08

- chore(P4): remove the deprecated macro us_2y/spread_10y_2y shim (^FVX is the 5Y → us_5y/spread_10y_5y only); score-industry now requires currently-tradeable peers (skip delisted/acquired); portfolio Step 8 writes its decisions blob via the Write tool (not a fragile heredoc)

## v1.0.12 — 2026-06-07

- P2 hardening + P3: enforce FDS-field classification completeness for the mixed-currency repair (test guard); scoring agent now sanity-checks impossible debt (current_debt/total_debt > total_liabilities) and skips leverage on a violation

## v1.0.11 — 2026-06-07

- feat(score-business): detect extreme-QoQ quarters (rev ≥50% / margin ≥20pp) and surface 07_earnings cross-check evidence into 02_financial_data.json, so the fundamental agent stops mistaking a real cyclical peak for corrupt data and dropping it (P1, SNDK)

## v1.0.10 — 2026-06-04

- feat(update): the auto update-check now also notifies Codex (.codex/hooks.json SessionStart + --emit-hook-json codex) — Claude Code and Codex both get the session-start release notice; Cursor/OpenCode still manual

## v1.0.9 — 2026-06-04

- fix(update): SessionStart auto-check now surfaces a USER-VISIBLE update notice (--emit-hook-json → systemMessage) — a session-start hook's plain stdout reaches only Claude's context, so the release notice was previously invisible to the user

## v1.0.8 — 2026-06-04

- fix(update): throttle only the auto (--quiet) session-start check, never a manual one — a manual 'update check' is now always live + prints its conclusion (was silenced when the SessionStart hook had checked within the hour)

## v1.0.7 — 2026-06-04

- fix(fetch): raise FDS financials limit 8->16 to capture the buried fiscal Q4 (restores FDS-direct financials + unblocks FMP-uncovered small caps/ADRs that were failing the DL4 consecutive-quarter gate)

## v1.0.6 — 2026-06-04

- fix: always load .env so FMP_API_KEY/FINNHUB_API_KEY load even when FINANCIAL_DATASETS_API_KEY is already set in the environment

## v1.0.5 — 2026-06-03

- Fix segmented revenue fetch: financialdatasets.ai retired /financials/segmented-revenues (HTTP 404); migrated to /financials/segments with the new nested response structure. Restores the per-segment revenue breakdown (product / geography / business segment) that analysis was silently missing, plus fail-closed hardening on unusable feeds.

## v1.0.4 — 2026-06-03

- Cross-platform: explicit UTF-8 on all inline + subprocess I/O (Windows cp936); portable mktemp (macOS/BSD)
- Trim dev-only development.md rule from the published product

## v1.0.3 — 2026-06-02

- FMP_API_KEY is now required (was mislabeled optional): financials fallback for foreign ADRs / non-Dec fiscal years + /screen-stocks needs it. Bootstrap prompts for it as required + warns if empty. FINNHUB_API_KEY stays optional.

## v1.0.2 — 2026-06-02

- Fix Windows GBK console crash — UTF-8 stdout/stderr; no more PYTHONUTF8=1 needed

## v1.0.1 — 2026-06-02

- Slimmer distribution: the published repo no longer carries dev/maintenance
  tooling (publish/release/audit/test scripts + `scripts/dev/`) — only the
  runtime, skills, and the `setup` / `update` entry points.

## v1.0.0 — 2026-06-02

First public release of the Stock Analysis System v7 skill set.

- **8 skills** — `score-business`, `investment-thesis`, `portfolio`,
  `screen-stocks`, `monitor`, `research-industry`, `write-report`,
  `generative-ui`.
- **Multi-agent, zero-touch** — works on Claude Code / Cowork (`.claude/skills/`)
  and Codex / Cursor / OpenCode (`.agents/skills/` + `AGENTS.md`) with no
  per-agent setup; run your agent from the repo root.
- **Versioned releases + opt-in updates** — `python3 -m scripts.update check` /
  `apply` (auto-checked on Claude Code session start; never auto-updates).
- Sourced numbers, explicit units/FX, and fail-closed portfolio limits are
  enforced (see `.claude/rules/`). Human output language is configurable
  (`output_language` in `strategy.yaml`); JSON analysis is always English.
