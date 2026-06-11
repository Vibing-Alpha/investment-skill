---
name: screen-stocks
description: |
  Discover US stocks by price action, sector, market cap, and optional
  technical indicators (RSI/MACD/Bollinger). Use this skill when the user
  wants to FIND tickers to research — not when they already know which
  ticker to analyze.
  Trigger phrases include "选股", "涨幅榜", "今天涨幅最高的", "最近一周涨最多的",
  "跌幅榜", "科技股涨幅", "板块轮动", "找最近涨得多的", "筛一下",
  "有哪些股票", "screener", "top gainers", "biggest losers",
  "most active", "show me movers", or any request that returns a LIST
  of tickers rather than analyzing one.
  Three scopes: whole market / specific sector / personal watchlist.
  Four time windows: 1d, 5d, 1m, 3m.
  Output is pure discovery — a ranked list with brief context, not a
  buy/sell recommendation. User decides which survivors to run through
  /score-business or /investment-thesis next.
  NOT for analyzing a single known ticker (use score-business).
  NOT for portfolio-level allocation decisions (use portfolio).
  NOT for the thesis on a specific stock (use investment-thesis).
user_invocable: true
---

# Screen Stocks — Ticker Discovery

Find candidate tickers by screening on price action + optional technical
indicators, scoped to **全市场 / 板块 / 观察池**. This skill answers ONE
question: **"What's moving, and what are the candidates worth a closer look?"**

Downstream skills answer the rest: `/score-business` ("is this a good business?"),
`/investment-thesis` ("is the thesis clean?"), `/portfolio` ("should I size it?").

## Repo-root prelude (fresh-shell — run first)

Every Bash block in this skill may run in a **fresh shell with an ephemeral cwd**
(Cowork): variables `export`ed in one block do NOT survive into the next, and the
harness Read tool does NOT follow a bash `cd`. So the repo root is resolved exactly
ONCE, here.

Run this block first and **CAPTURE the `STOCK_V7_ROOT=...` value it prints**. Substitute
that absolute path for the literal `<captured-abs-ROOT>` in every later Bash block, every
harness Read path, and every subagent-dispatch path in this skill. If this block exits
non-zero (multiple candidate roots, or no repo found), show its stderr to the user and
**STOP** — run nothing else.

If it prints a `stock-v7: WARNING — version skew` line, relay that warning to the user verbatim and continue — it is advisory only (the installed plugin and the clone are at different versions; it tells the user which half to update), never a stop.

```bash
# --- resolver-core ---   (byte-identical to scripts/templates/root_resolver.sh — Task 5 enforces)
# cwd-or-ancestor: if cwd (or ANY parent) is the repo, USE IT — CC-CLI/Codex/Cursor/OpenCode run from the
# repo (or a subdir), so this is a TRUE no-op (covers subdir runs + multi-worktree dev: always the clone
# you're in). Composite marker = scripts/ + prompts/ + strategy.example.yaml (the last is the
# stock-v7-specific tracked file; tighter than CLAUDE.md/VERSION alone).
ROOT=""; d="$PWD"
while [ "$d" != "/" ]; do                # cwd-or-ancestor; marker = scripts/ + prompts/ + strategy.example.yaml
  if [ -d "$d/scripts" ] && [ -d "$d/prompts" ] && [ -f "$d/strategy.example.yaml" ]; then ROOT="$d"; break; fi
  d=$(dirname "$d")
done
case "${STOCK_V7_HOME:-}" in /*) [ -z "$ROOT" ] && ROOT="$STOCK_V7_HOME";; esac   # env override seam — ABSOLUTE only (relative/~ is ignored, mirroring resolve_root's fail-closed; nothing can set it persistently in Cowork)
if [ -z "$ROOT" ]; then
  # Cowork (ephemeral cwd): glob the clone under USER mounts only (exclude outputs/uploads + dot-folders),
  # verify the composite repo marker (a stray dir merely NAMED stock-v7 must not count — round-11),
  # then realpath-dedup (symlinked mounts → same real dir must NOT count as multiple roots).
  HITS=$(ls -d /sessions/*/mnt/*/stock-v7 2>/dev/null | grep -vE '/mnt/(outputs|uploads|\.[^/]*)(/|$)' \
    | while IFS= read -r h; do (cd "$h" 2>/dev/null && [ -d scripts ] && [ -d prompts ] \
        && [ -f strategy.example.yaml ] && pwd -P); done | sort -u || true)
  if [ "$(printf '%s\n' "$HITS" | grep -c .)" -gt 1 ]; then
    echo "stock-v7: multiple stock-v7 roots in mounts — keep ONE:" >&2; printf '%s\n' "$HITS" >&2; exit 1
  fi
  ROOT=$(printf '%s\n' "$HITS" | head -1)   # the sole hit, or EMPTY — the consumer tail handles empty
fi
# --- end resolver-core ---
# BUSINESS tail (the setup skill replaces everything below the end-marker with its clone/pull tail):
if [ -z "$ROOT" ]; then                                   # CC-CLI marker fallback (rare: not-in-repo + no env)
  ROOT=$(cat "$HOME/.stock-v7-home" 2>/dev/null | tr -d '\r')   # strip CRLF if the marker was hand-edited on Windows
  ROOT="${ROOT:-$HOME/Claude/stock-v7}"
fi
cd "$ROOT" 2>/dev/null || { echo "stock-v7: run the setup skill first" >&2; exit 1; }
printf 'STOCK_V7_ROOT=%s\n' "$PWD"   # Step 0 EMITS the resolved abs root (post-cd $PWD) for the agent to capture
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.version_skew --expected-min "__BAKED_AT_SYNC__" || true   # skew WARNING only (installed plugin vs clone) — never gates; placeholder baked to the release VERSION by the publish-time sync
```

## Preflight: Money-path config

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
"$PYBIN" -m scripts.config_gate check
```

If it exits non-zero, STOP and show its stderr to the user (config not confirmed /
required API key missing) — do NOT run any analysis or produce numbers. Then continue
below.

## Output

- `reports/screen/{YYYYMMDD}/{scope_tag}.json` — machine-readable ranked results
- `reports/screen/{YYYYMMDD}/{scope_tag}.md` — human-readable markdown table

**Output directory matters**: always write under `reports/screen/{YYYYMMDD}/`
because the script walks that tree to compute delta (new/dropped/sustained
vs prior runs). Writing elsewhere (e.g. /tmp) still works but loses the
day-over-day comparison. Same `{scope_tag}` filename day over day is fine;
the script keys on the `scope_fingerprint` field stored inside the JSON,
not the filename.

### What's in the JSON (beyond the ranked `results`)

The script does two things a naive screener wouldn't:

- **`delta`** — `{new, dropped, sustained, prior_date}` computed vs the
  most recent prior run with the same scope+window+direction. Each row in
  `results` also gets a `streak_Nd` tag (N = consecutive days in top list).
- **`attention`** — up to 5 tickers hand-picked for the user by combining
  `held` / `watchlist` / `theme:X` / `new_today` / `streak_Nd` tags from
  `strategy.yaml` + `portfolio-state.yaml`. Silent if neither file exists.

Both sections also render in the markdown for human eyes.

## Script

Single entry point. Run `"$PYBIN" -m scripts.screen --help` for the full CLI surface
and the rationale behind each default threshold. The docstring at the top of
`<captured-abs-ROOT>/scripts/screen.py` documents the data flow and computation
pipeline.

```
"$PYBIN" -m scripts.screen \
  --scope {market | sector:NAME | watchlist:PATH} \
  --window {1d | 5d | 1m | 3m} \
  [--direction up|down] [--top N] \
  [--min-price ...] [--min-volume ...] [--min-mcap-usd ...] \
  [--tech] \
  --output-prefix reports/screen/YYYYMMDD/TAG
```

No LLM call is made inside the script — it is pure computation. The universe
is selected via 1-3 FMP API calls; OHLCV is fetched once in batch via
yfinance; RSI/MACD/Bollinger/Volume reuse the same `scripts/indicators.py`
that `/score-business` uses (single implementation, per producer-consumer rule).

## Execution

### Step 1: Parse user intent → CLI args

Map what the user said to `--scope`, `--window`, `--direction`, and whether
to set `--tech`. You have discretion — the table is guidance, not a decoder.

| User cue | scope | window | direction |
|---|---|---|---|
| "今天涨幅榜", "top gainers today" | `market` | `1d` | `up` |
| "今天跌最多的", "biggest losers today" | `market` | `1d` | `down` |
| "科技股涨幅", "tech movers" | `sector:Technology` | `1d` | `up` |
| "最近一周涨最多", "1-week winners" | `market` | `5d` | `up` |
| "这个月涨幅最高" | `market` | `1m` | `up` |
| "本季度 / 3个月" | `market` | `3m` | `up` |
| "观察池里", "my watchlist" | `watchlist:PATH` | user-implied | user-implied |
| "医药股 / 金融股 / 半导体" | `sector:{GICS name}` | — | — |

**Sector names**: the script accepts the GICS-11 sector vocabulary used by
FMP. If Claude passes an unknown name the script fails fast with the full
valid list, so do not memorize it — let the script be the source of truth.

**When to set `--tech`**: whenever the user asks about momentum quality,
chase-risk, overbought/oversold, RSI, MACD, or volume anomalies — i.e.
most of the time. Skip it only for a fast change-%-only scan.

**Watchlist path**: if the user says "观察池" without a path, check
`<captured-abs-ROOT>/portfolio-state.yaml` for holdings (each top-level key is
a ticker). If present, use it. If not, ask the user.

**Theme with no GICS sector** (e.g. "铝电容/被动元件", "固态电池", "GLP-1
CDMO" — a narrow industry that isn't one of the GICS-11 sectors, with no
watchlist file): build a candidate watchlist, but treat your memory as a
hypothesis, not the universe. The trap is **overconfidence** — on a
fast-moving theme you'll instinctively web-search, but on an *established*
one you feel you already know the names, and that's exactly when you
silently omit a real player (acquired, newly-listed, or just forgotten). An
omitted name is invisible: it can't rank even when it's the biggest mover in
the theme or a position the user holds. (A from-memory pass on "passive
components" missed TYOYY — the #1 mover that week — and VPG, a held name.)

So always anchor the universe with **ONE broad current-year WebSearch**
("US-listed <theme> makers <CURRENT_YEAR>"), reading for the names you
DON'T already have, then let the screen's deterministic layer (FMP/yfinance)
confirm existence + liquidity. Run with `--tech` (this flow does) and the
screen emits an `illiquid_stub` flag — a frozen price + no run-day volume —
marking a barely-traded zombie listing; point the user at its home-market
line instead. (The flag rides the `--tech` flags column, so a fast no-`--tech`
scan won't show it.) Verify corporate status on borderline names
(acquired / delisted / <30% exposure); web listicles carry stale names too,
so don't blind-add. Label a memory-built list as unverified.

Route to `/research-industry` INSTEAD only when the user wants its fuller
deliverable (TAM/structure + the `candidate_tickers` schema that feeds
`/score-business`); its search is no better than the above, so don't route
there merely to "verify the universe."

### Step 2: Pick filter thresholds for the scope

The script has sensible defaults tuned for `market` scope. For `sector:X`
scope, Claude should typically raise `--min-mcap-usd` one order of
magnitude — sector screens are usually about mid/large caps worth
analyzing, not penny-stock pumps that sneak through FMP's /gainers.

For `watchlist:X` scope, go the OTHER way: **relax** the floors (usually
`--min-price 0 --min-volume 0 --min-mcap-usd 0`). The watchlist is the
user's explicit, curated input, but `_filter` is scope-blind — it applies
the same market-tuned defaults (price ≥ USD 5, volume ≥ 500k, mcap ≥ USD 300M)
and silently drops anything below them, with NO record of the drop (only
fetch failures land in `warnings.ohlcv_missing`). Left at defaults, a
watchlist screen will quietly delete hand-picked thin tickers — most often
foreign ADRs that trade lightly on US OTC (e.g. Japanese passive-component
names). That is silent loss of the user's chosen universe. Add a floor back
only when the user explicitly wants to thin a large pasted list.

Relax the defaults only if the user explicitly wants small caps, penny
stocks, or micro-cap speculation. The exact numbers live in
`scripts/screen.py` constants + `--help` output; read those when you need
them rather than duplicating here.

### Step 3: Invoke + read + present

```bash
cd "<captured-abs-ROOT>"
PYBIN="$PWD/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$PWD/.venv/Scripts/python.exe"; [ -x "$PYBIN" ] || PYBIN=python3
# ET session date — matches the convention used by the rest of the delta
# layer (scripts/delta/calendar.session_et). Using `date +%Y%m%d` (local
# system time) would misplace after-hours runs into the next UTC day and
# silently break day-over-day diffs.
DATE=$("$PYBIN" -c 'from scripts.delta.calendar import session_et; print(session_et().strftime("%Y%m%d"))')
SCOPE_TAG="<snake_case_summary_of_scope_and_window>"
mkdir -p "reports/screen/$DATE"
"$PYBIN" -m scripts.screen --scope ... --window ... [...] \
  --output-prefix "reports/screen/$DATE/$SCOPE_TAG"
```

Then read the output JSON at
`<captured-abs-ROOT>/reports/screen/{DATE}/{SCOPE_TAG}.json` (absolute path —
the harness Read tool does not follow the bash `cd`) and present in
`output_language` (default zh-CN per `strategy.yaml`). The presentation should
surface signal, not just dump rows. In priority order:

1. **Lead with the `attention` list** if present. These are the 1-5
   tickers where the screen intersects the user's actual portfolio /
   strategy / multi-day momentum. Frame them as "这几个值得你先看一眼，因为 …"
   and include the reason from each row's `tags`. This is the most
   valuable signal — don't bury it under the ranked table.
2. **`delta` section** if `delta.prior_date` is non-null — report
   new entrants, names that dropped out, and anything with a
   `streak_3d+` tag (persistent leaders). These are facts baseline Claude
   cannot produce without cross-run state.
3. **The ranked table** — quote the `.md` directly or re-render a
   condensed version.
4. **2-4 market observations**: volume spikes, overbought clusters,
   multi-window confluence.
5. **Invitation to next step**: "要深入哪几个？" — user drives, skill doesn't
   auto-chain to /score-business.

If `attention` is empty and there's no prior run to diff against, it's
a cold-start run — just present the table + observations normally.

## Edge cases

- **`scope=market` with `window ≠ 1d`**: the universe is built from a broad
  `/stock-screener` query (no sector filter, `--min-mcap-usd` floor), NOT
  today's gainers/losers/actives — so multi-day leaders that aren't moving
  today are captured. The `1d` window still uses the movers endpoints (they
  literally are the 1d signal). If the broad query hits the
  `BROAD_MARKET_LIMIT` row cap, the JSON carries `warnings.universe_truncated`;
  raise `--min-mcap-usd` to tighten the universe, or screen by sector.
- **Pre-market / post-market**: FMP returns delayed EOD data; intraday
  real-time top-movers require a paid tier.
- **Delisted tickers**: yfinance emits a warning and drops them cleanly.
- **Empty survivors**: communicate it and suggest loosening filters.
- **User pastes a ticker instead of a phrase**: route to `/score-business`.

## Deliberately out of scope

This skill returns a list and a mechanical brief. It does NOT generate
"why is this moving" narratives (use `/score-business` with its web-search
step), buy/sell signals, entry levels, or size suggestions (use
`/portfolio`). Overbought flags are context, not directives. Human
decides which survivors to analyze next.

## Reference

- `scripts/screen.py` — complete implementation + defaults + rationale.
- `scripts/indicators.py` — RSI/MACD/Bollinger/volume (shared).
- `.env` — requires `FMP_API_KEY`.
- `.claude/rules/skill-architecture.md` — why this file is orchestration only.
- `.claude/rules/producer-consumer.md` — why indicators.py is reused, not re-implemented.
