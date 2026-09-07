## Delta Materiality Rubric

Used by `prompts/delta/classify-news.md` to classify news articles
as material vs. low-signal during the delta-update probe phase.

### Credible source — TWO paths, not one

Material signal requires a credible source, which is EITHER of:

**(a) A whitelisted outlet** — the article originates from one of:

- Reuters
- Bloomberg
- SEC EDGAR (primary filings)
- Company IR / press releases
- Wall Street Journal (WSJ)
- Financial Times (FT)
- CNBC
- Barron's
- MarketWatch
- Associated Press (AP)
- Financial Datasets primary feeds (non-aggregated)

**(b) Corroboration** — the SAME material event reported by **≥2 independent
outlets**: genuinely different publishers, not one wire syndicated under
several names, and not one outlet re-posting another's story (that counts
ONCE). A concrete material event that several independent aggregators each
carry is a credible signal even with no whitelisted carrier.

Path (b) is not a softening — it is what makes the rule usable on the feed we
actually get. Measured over the stored runs (2026-09-07): barely one article in
a hundred carries a named whitelist outlet, and all but a few per cent carry
neither a whitelist outlet nor a PR wire; the feed is dominated by MarketBeat,
Yahoo, Benzinga, Stock Titan, GuruFocus, SeekingAlpha. Proportions, not counts,
because the corpus grows with every run — `tests/test_news_materiality_consistency.py`
re-measures it and fails if coverage ever climbs back. Note also that on a
`finnhub_fallback` news file EVERY article carries a `Finnhub:*` label, so path
(a) cannot fire on that feed at all. Whitelist-ONLY was therefore a gate that
discarded almost every article (an aggregate — an occasional single-article
batch is 1/1 whitelisted): it scored
a real ADBE CEO change `immaterial` because the batch held only non-whitelisted
restatements, and the events layer then reused a snapshot from BEFORE the
change — while `/monitor`, reading the same feed the same day, surfaced the
CNBC first-hand report. `prompts/monitor-route.md` §Step 1 carries the same two
paths; the two must not disagree, and `tests/test_news_materiality_consistency.py`
pins that.

**(c) Unresolved, conservatively** — the CATEGORY is material and the company is
named, but the batch gives no way to settle whether the reports are independent
(most stored batches carry no summary bodies at all — and it comes a whole batch
at a time, so expect either titles-only or full bodies, not a percentage). Count it as material and record that credibility was unresolved. The
asymmetry decides it: in the delta layer `material_count: 0` is what authorises
REUSING the prior events snapshot, so low-signal is the permissive branch — a
false material costs one re-analysis, a false immaterial ships a pre-event
snapshot into a trade decision.

A LONE non-whitelisted report of an uncorroborated story is still low-signal —
it contributes to `low_signal_headlines`, not to `material_count`. So is an item
whose CATEGORY or company you cannot pin down: 1(c) escalates an unresolved
SOURCE on a clearly material event, never an unidentifiable event.

### Material content categories

At least one of these must match:

1. **Product / contract**: product launch, major customer contract,
   large acquisition, divestiture, spin-off.
   Keywords: "signs", "wins", "launches", "acquires", "divests",
   "announces partnership".
2. **Management / governance**: C-suite transitions, board changes.
   Keywords: "CEO resigns", "names new CFO", "Chairman steps down".
3. **Regulatory / litigation**: SEC/DOJ/FTC actions, antitrust,
   investigations, material lawsuits/settlements.
4. **Guidance / preannouncement**: forward guidance changes, profit
   warnings, preannounced earnings.
   Keywords: "guidance", "preannounce", "warns", "cuts outlook", "raises
   full-year guide".
5. **Major capital events**: large buyback programs, dividend
   initiation / cut / raise, equity raise, notes offering.

### Explicit exclusions

- Pure marketing releases (product showcase without customer commit)
- Plaintiff law-firm solicitation releases ("Securities Fraud Investigation Into X
  … shareholders urged to contact …"). These match category 3 on the word
  "investigation", name the company, and carry an unsettleable source, so path (c)
  would escalate every one of them — and they are bought wire slots that recur on
  every large cap. Scope category 3 to an action by a REGULATOR or a COURT, not to
  a law firm announcing it is looking for clients.
- A LONE non-whitelisted re-post of an UNCORROBORATED story (an article credited
  to another outlet via syndication, that nothing else in the batch carries and
  that path (c) does not reach). This is a COUNTING rule, not a content one: one
  wire under five mastheads is ONE outlet, so re-posts cannot manufacture the >=2
  of path (b). It does NOT say a re-posted EVENT is not an event — a syndicated
  report of a real acquisition is still a real acquisition, and if its category is
  material and its independence unsettleable it goes to material under (c). Read
  as a blanket exclusion this line contradicts (c) outright, and the contradiction
  is live: in the SPCX 2026-09-04 batch, 4 of 10 articles were non-whitelisted
  carriers whose bodies named a whitelisted originator, one of them a closed $60B
  acquisition. (The collision predates (c) — the Edge case below already read this
  as counting while this line read as exclusion. Under a whitelist-only gate both
  reached low-signal, so nothing forced the choice.)
- Bare analyst rating changes (these are captured via the analyst-
  estimates hash in the delta layer, not by this classifier)
- Generic industry commentary without direct company reference

### Edge cases

- Article references the ticker but content is about the sector/peer
  → low-signal.
- Multiple outlets syndicate the same primary source → count once,
  prefer the whitelisted source.
- Dated articles outside the `since_date` window should not have been
  passed to the classifier; if they appear, ignore.
