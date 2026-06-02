## Delta Materiality Rubric

Used by `prompts/delta/classify-news.md` to classify news articles
as material vs. low-signal during the delta-update probe phase.

### Source whitelist

Material signal requires the article to originate from one of:

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

Non-whitelisted sources (SeekingAlpha, Investing.com, The Motley Fool,
MSN, Yahoo syndicated, etc.) do not qualify as material. They may
still contribute to `low_signal_headlines`.

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
- Aggregator re-posts (article credited to another outlet via syndication)
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
