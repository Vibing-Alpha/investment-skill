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
REUSING the prior events snapshot, so low-signal is the permissive branch. A false
material is not free — a positive count requires at least a `partial` refresh
(full-tier triggers take precedence when they also fire), which re-fetches
and reruns the forward and industry scoring plus synthesis, not merely the events
agent — but a false immaterial ships a pre-event snapshot into a trade decision.
That is the trade, stated at its real price.

A LONE non-whitelisted report of an uncorroborated story is still low-signal —
it contributes to `low_signal_headlines`, not to `material_count`. That rule is
SUBSUMED, however, and kept only as a reading aid: it has no scope of its own.
If the CATEGORY is not material the content test already refuses the item; if the
category IS material and independence cannot be settled, path (c) takes it. There
is no third case, on any batch shape — 29 of 42 stored batches are titles-only,
where every article is at once unsettleable and a lone story nobody else carries.
Do not use it as a tiebreaker. So is an item
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

### Three boundaries, so they are not decided by taste

**Size — do not judge magnitude.** The categories say "major customer contract",
"large acquisition", "large buyback"; treat those words as naming the KIND of event,
not a bar to clear. You are given `{title, source, published_at, summary, url}` and
nothing else — no market cap, no revenue — so the only defensible threshold, a
relative one, is not computable from your inputs. (Amounts named across the stored
corpus span several orders of magnitude — $100M and $500B are examples from it,
not its bounds — and issuer size spans further, so no absolute number
works either.) This gate decides whether to RE-RUN an analysis; the events layer
weighs magnitude afterwards with the data for it. A $2.1B commitment by a $4T
company is a contract event here even though it is ~0.05% of the issuer.

**Whose event — it must change the SUBJECT company's own state.** Its contracts, its
management, the regulatory action it faces, its capital structure. A startup it
invested in raising a round does not qualify; nor does its technology validating
someone else's product. Being NAMED in the story is not the test — being the party
whose state changes is.

**Read the event, not the genre.** An article in an excluded class can carry a
material fact: an opinion column headlined "Google Keeps AdX" asserts a concrete
regulatory outcome. Test 3 reads the EVENT REFERENCED, not the nature of the piece.
Refusing it for being an opinion column is the same move as refusing it for its
carrier — which is exactly what this rubric was rewritten to stop. (The exclusions
still bite when the article carries no event at all: a rating change, sector
commentary, a marketing release.)

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
