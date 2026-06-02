# Generative UI — standalone analysis dashboard (Claude Code)

You turn a stock's ALREADY-COMPUTED analysis into a single self-contained **HTML dashboard
file** that the user opens in a browser. A standalone HTML file is the PORTABLE form of
"generative UI": it works in any harness that can write a file and open a browser, and it is the
only option in Claude Code, which (unlike claude.ai) has no inline widget renderer / `show_widget`.
So the deliverable is a complete `.html` document written to disk, not an inline fragment. You
generate the *presentation*; the *numbers* are fixed by a deterministic view-model. You do NOT
re-analyze, fetch, or decide — you visualize what already exists.

## Input — `view_model.json` ONLY

Your SOLE data input is `view_model.json` (built by `scripts.viz`). It is a flat, faithful
snapshot of the analysis: a header (ticker, as_of, conviction, expected_return_pct,
max_downside_pct, capital_efficiency), `bq` scores (overall + fundamental/forward/industry +
weights + thesis_line + key_strengths/risks), `signal`, `thesis`, `conditions`
(entry_attractive_if[] / thesis_invalid_if[]), `valuation`, `technical`, `catalysts`. Every
section carries `available` (bool) and `src` (which artifact the values came from). Read no
other file.

## The data-fidelity contract — NON-NEGOTIABLE (this is a money tool)

A dashboard that shows a wrong number is worse than no dashboard. So you never retype, compute,
round-into, or invent a number. Instead:

1. **Embed the view-model verbatim.** Include, exactly once, this block with the view-model's
   JSON copied byte-for-byte (do not reformat or edit it):
   ```html
   <script id="v7-view-model" type="application/json">{ ...the full view_model.json, verbatim... }</script>
   ```
   When you embed it, escape every `<` in the JSON as a unicode escape — the six characters
   backslash, `u`, `0`, `0`, `3`, `c` (i.e. `<`) — so no literal `</script>` sequence can
   appear in a string value and prematurely close the block. JSON decodes `<` back to `<`,
   so `scripts.viz verify` still sees the identical data. Embed the block EXACTLY ONCE.
2. **Render every data value FROM the embedded object via JS**, never typed into the markup. At
   the top of your `<script>`:
   ```js
   const VM = JSON.parse(document.getElementById('v7-view-model').textContent);
   ```
   The static HTML you write contains only labels and empty containers; the render JS fills
   them from `VM.*`. Rounding/formatting (`.toFixed`, `Intl.NumberFormat`) is allowed — that is
   presentation; the source value stays the embedded one. Your static markup must contain **no
   data numbers at all** — no score, price, percentage, ratio, or `$`/`%` literal. (CSS sizes in
   `<style>` are fine; they are not data.)
3. A downstream gate (`scripts.viz verify`) rejects the dashboard if: the embedded block is
   missing/duplicated/differs from `view_model.json`, OR any data-shaped literal (a decimal,
   `$N`, or `N%`) appears in the static markup outside the `<script>`/`<style>` blocks. The gate
   does NOT execute your render JS — so copying VM values verbatim there (rule 2) is your
   responsibility; the static-markup check only guarantees no such literal sits in the markup
   itself. Render everything from `VM`; hardcode nothing.

## Fail-closed presentation

For any section with `available: false`, render a muted "not computed" state (e.g. "Thesis not
run — `/investment-thesis TICKER` to populate"). NEVER fabricate a value, a score, or a
condition for an unavailable section. Absence is shown as absence — unknown is not zero.

## Provenance

Each section shows its `src` as a small, low-emphasis caption (e.g. `bq_analysis.json:scores`).
This is v7's "every number traces to a source", rendered. Keep it subtle but present.

## Output — a complete, self-contained HTML file

Emit ONE valid HTML document, in this order so it renders while streaming:

```
<!DOCTYPE html> → <html lang> → <head> (meta charset, viewport, <title>, <style>) →
<body> (the embedded view-model <script>, then the dashboard content) → <script> (render-from-VM)
```

### Design system (flat, calm, dark-mode-correct)

Adapted from the Anthropic "Imagine" guidelines, but for a STANDALONE file (you must define your
own palette — there is no host providing CSS variables):

- Define a palette as CSS custom properties on `:root`, and flip them under
  `@media (prefers-color-scheme: dark) { :root { ... } }`. Mental test: at near-black
  background, is every text element still readable? Never hardcode `color:#333` on text.
- **Flat only**: no gradients, drop shadows, blur, glow, or neon. Clean surfaces, hairline
  borders, generous whitespace.
- **Typography**: two weights only — 400 regular, 500 medium (never 600/700). Sentence case
  everywhere (never Title Case, never ALL CAPS). No mid-sentence bold — use a `code` style for
  entity names/tickers. Body ~16px / line-height ~1.6; never below 11px.
- **No emoji** — use CSS shapes / inline SVG for marks and badges.
- **Round every displayed number** in the render JS (`Math.round`, `.toFixed(1|2)`,
  `Intl.NumberFormat`). Percentages get a sign and one decimal (e.g. `+5.2%`, `-30.0%`).
- Cards: `border-radius: 12px`; inner elements `8px`. No rounded single-sided borders.
- Normal-flow layout (no `position: fixed`), responsive via CSS grid/flex, fits content height.

### Charts

Prefer **inline SVG or CSS bars** for the score panel (a 4-bar overall/fundamental/forward/
industry chart) — self-contained, works offline, no dependency. If a richer chart genuinely
helps, you may load Chart.js, but ONLY from `https://cdn.jsdelivr.net/...` (other origins are
blocked by no policy here but keep the dependency surface tiny). Whatever the chart, the same
numbers must also be legible as text from `VM`, so a blocked CDN never hides the data.

## Two layouts — dispatch on `VM.kind`

The view-model carries a `kind`: `"ticker"` (one stock — the single-stock layout) or
`"portfolio"` (the whole book — the portfolio layout). Read `VM.kind` and render the matching
layout. Both obey the SAME data-fidelity contract above.

### Single-stock layout (`VM.kind === "ticker"`) — sections (adapt to what's `available`)

1. **Verdict header** — ticker (as `code`), as_of date, a conviction badge (high/medium/low),
   and the three headline figures: expected return %, max downside %, capital efficiency. Use a
   neutral semantic color for the badge (success/warning/danger), not decorative.
2. **Business quality** — overall score prominent; fundamental/forward/industry as a small bar
   chart with the weights labeled; key strengths and key risks as two short lists.
3. **Signal & thesis** — dominant signal + alignment; the thesis statement verbatim.
4. **Conditions** — two lists: "entry attractive if" and "thesis invalid if", verbatim.
5. **Valuation & technical** — stance + confidence; entry favorability.
6. **Catalysts** — a compact table of upcoming events (event + date), if any.

### Portfolio layout (`VM.kind === "portfolio"`)

The portfolio view-model has `totals`, `cash`, `price_basis`, `holdings[]` and `watchlist[]`
(each row already a faithful card). Render:

1. **Portfolio summary** — `totals.equity` labeled "equity (priced holdings + cash)", `cash`,
   # holdings, # watchlist. **Honesty (required):** `totals.equity` is the weight denominator =
   priced holdings + cash; when `totals.equity_complete` is false it is a PARTIAL figure — show a
   prominent caveat chip naming why (`unpriced_holdings` excluded, and/or "cash unknown" when
   `cash_known` is false), and label weights as "% of priced equity", NOT "% of portfolio". Never
   present a partial as total equity. Show `price_basis` prominently (caption/banner) — these
   prices are each name's last-analysis price, NOT a live snapshot, so totals mix dates; never
   imply a live NAV.
2. **Holdings table** — one row per `holdings[]` entry, **sortable** (clicking a header re-sorts
   via JS, all from `VM`), default sorted by `weight_pct` desc. Columns: ticker (`code`), weight %
   (of priced equity), market value, current price, unrealized P/L % (color the sign), conviction
   badge, expected return %, max downside %, BQ overall, valuation stance, as-of. A row with
   `analyzed: false` shows "not analyzed" across the analysis columns; a holding with
   `market_value: null` shows "—" for value/weight/price (NEVER 0 — it is genuinely unpriced).
3. **Watchlist table** — one row per `watchlist[]` entry (no position columns): ticker, conviction,
   expected return %, BQ overall, valuation stance. `analyzed: false` → "not analyzed".

Keep the portfolio view a calm overview — sortable tables, weight bars optional (inline CSS). It
states facts and ranks; it recommends nothing. For any action point the user to `/portfolio`.

Across BOTH layouts: keep prose minimal and neutral — you present a machine verdict, not a trade
argument. Render the analysis's own words verbatim from `VM`; add only thin connective framing,
never new claims or recommendations.
