# Screen Stocks — Known Gotchas

Accumulated failure patterns. Update as new issues emerge.

## Universe selection

### FMP `/gainers` is unfiltered — penny stock noise dominates
Raw `/gainers` returns lists like `ZSPC +2400% @ $1.54` (halt-resume artifacts,
reverse splits, delisted tickers re-listing). These outperform every real name
by an order of magnitude and poison any raw sort. The script applies
`--min-price`, `--min-volume`, `--min-mcap-usd` floors before ranking.

When adding a new scope that sources from /gainers-style endpoints, assume
the same filtering is needed — the dataset is not research-grade.

### FMP `/quote` does NOT return `sector` / `industry`
Only `/profile` does. Early versions of the script assumed batch `/quote`
would populate sector, producing empty sector fields for scope=market
(where universe comes from `/gainers` without sector). Fix applied in
§scope=market Step 5b: call `/profile` batch on the top-N after ranking
(1 extra FMP call for up to 100 symbols, cheap).

For scope=sector, `/stock-screener` already returns sector/industry — no
extra call needed. For scope=watchlist, the user knows the sector context
already, so we skip the backfill.

## Delta layer

### Watchlist fingerprint is content-hashed, NOT path-based
Early versions used `scope_fingerprint = f"{scope}__{window}__{direction}"`
where `scope` for watchlist included the file path. Users typically paste
a watchlist inline to a fresh /tmp file each session, so the path changes
and delta never fires — defeating the whole layer.

Fix (`_scope_fingerprint` in `scripts/screen.py`): for watchlist scope,
hash the **sorted ticker set** instead:
`watchlist:wl_{md5_first_8}__{window}__{direction}`.

Same tickers = same fingerprint regardless of where the file lives.
Ticker ORDER in the input doesn't matter (sorted before hashing).

### Directory layout must be `reports/screen/{YYYYMMDD}/*.json`
`_list_prior_runs` walks this tree in reverse date order. Writing outputs
elsewhere (e.g. `/tmp/foo.json`) still works for the current run but is
invisible to tomorrow's delta pass. SKILL.md instructs Claude to use the
canonical layout; if a user wants ad-hoc output, that's fine, but they
lose delta.

### Streak requires CONSECUTIVE appearances — one miss resets
`_compute_streaks` tracks per-ticker streak by walking priors backward
and breaking on the first miss. "On the list 5 of 7 days" is NOT a
streak_5d in this model — it's streak_2d (days since last miss).

## Personalization

### Both `strategy.yaml` and `portfolio-state.yaml` are fail-open
Missing files → `perz["loaded"] = False`, no tags, no attention section.
The screen still works. This is deliberate: the skill must be useful for
users who haven't set up portfolio state yet. Do NOT raise or exit on
missing personalization files.

### Theme keywords require explicit extension
`_THEME_KEYWORDS` maps user vocabulary ("AI", "space") to FMP industry
substrings. New themes need an explicit entry — there's no automatic
inference. When adding: prefer substring matches over exact ("software"
matches "Software - Application" and "Software - Infrastructure" both),
lowercase the haystack.

### Theme match is substring-based — can false-positive
"AI" keywords include "software" which will tag a pure enterprise SaaS
play as `theme:ai` even if it has nothing to do with AI. This is acceptable
noise — the tag is a hint for the attention ranker, not a classification.
Users reading the MD see the full industry name and can judge.

## Output rendering

### MD brief field has a `[tag_prefix]` that's duplicated in tag column
The brief starts with `[held,streak_4d]. <industry>. <mcap>. <move>...` so
the Attention section's "reason" would show the tags twice (once as
"reason", once in the brief). The MD renderer strips the `[...]` prefix
when rendering the Attention section specifically (see `_emit_markdown`
attention loop). If you change brief format, keep this stripping logic
aligned.

### `flags` (overbought/volume_spike) are distinct from `tags`
`flags` comes from technical indicators (RSI/BB/volume). `tags` comes from
personalization + delta. They coexist — don't merge them. A row can be
`flags: [overbought]` AND `tags: [held, streak_3d]`. Both appear in
different contexts (flags in the table's Flags column, tags in the brief
prefix + Attention section reason).

## Falsiness traps on legitimate zero

### `if vr` / `if mcap` is NOT a safe default
Python's truthiness treats `0.0`, `0`, `""`, `[]` as False. When a numeric
field can legitimately be zero (volume ratio of 0.0 for a halt-resume bar,
mcap of 0 for a pink-sheet listing), `if vr:` collapses it with None and
the downstream logic treats valid data as missing.

**Always distinguish `None` (unknown) from `0` (known-zero)**:
- ✗ `if vr and vr >= 2.0:` → fires only on non-zero, silently drops 0.0
- ✓ `if vr is not None and vr >= 2.0:` → explicit
- ✗ `if mcap:` → collapses $0 and None into "missing"
- ✓ `if mcap is not None and mcap > 0:` → explicit

This is the pattern that `_macd_state` hit on MACD zero-crosses (BUG-1)
and that the codex-review pass found propagated through `_flags`, `_brief`,
and the MD renderer. When adding any new numeric field, ask: "is zero a
valid observation for this quantity?" If yes, explicit None check.

## Partial-success is NOT success

### Track per-endpoint status in output JSON
When universe selection hits multiple endpoints (e.g.
`_universe_market` → gainers + losers + actives), a silent degraded run
(1 of 3 succeeds) writes a JSON that the delta layer can't distinguish
from a fully-sourced run. Tomorrow's new/dropped/sustained is computed
against a degraded baseline — invisibly wrong.

The fix is to record per-source status (`warnings.universe_sources`) and
similar `warnings.quote_missing` / `warnings.profile_missing` /
`warnings.ohlcv_missing` fields. Orchestration layers (SKILL.md) and
downstream analysis can now see "this run had N tickers missing OHLCV"
instead of inferring from gaps.

## FMP API safety

### Never let `raise_for_status()` leak the apikey
`requests.HTTPError` prints the full request URL including `?apikey=XXXX`.
Logging the exception to stderr (or a transcript, or CI) leaks the key.
Always scrub: `str(e).replace(api_key, "***")` before any print/log.

### Retry on 429 / 5xx with backoff
Free-tier FMP rate-limits quickly when `_universe_market` + `_batch_quote`
+ `_batch_profile` fire in sequence. A single 429 without retry crashes
the screen OR (worse) silently returns `[]` which makes `_filter` drop
every row as "missing mcap". `_fmp_get` now implements `[1,2,4]s` backoff
on 429/5xx, matching `scripts/sources/common` conventions.

## Watchlist injection surfaces

### YAML and text branches need the SAME ticker filter
Previously the text branch had `.isalpha()` filter but the YAML branch
was permissive. A malicious YAML file could inject `../../../etc/passwd`,
`NVDA; rm -rf /`, `<script>...` into the ticker list — no RCE (URL-
encoding kicks in at `_fmp_get`), but the strings flow verbatim into the
rendered MD report (display injection into downstream viewers).

The regex `^[A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?$` accepts real tickers
(BRK.B, BRK-B, BF.B, $AAPL after `$` strip) while rejecting shell
metachars, slashes, angle brackets, and compound malicious strings. Both
branches use it.

### Symlinks are rejected
`_universe_watchlist` fails closed when the path is a symlink. Even
though the regex filter would reject sensitive file contents, reading
them into memory creates an exfil path for any future maintainer who
adds debug logging. Copy files into place rather than linking.
