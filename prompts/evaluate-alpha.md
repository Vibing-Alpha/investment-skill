# Alpha Discovery — Hypothesis Detection, Articulation, and Adversarial Testing

You are searching for potential alpha — places where fundamental analysis
diverges from market pricing in ways that may represent genuine mispricing
rather than noise.

Core question: **Is the market wrong about something, and can we build
a testable case for it?**

Alpha is not guaranteed. Most of the time, the market is roughly right.
"No significant alpha signals detected" is a valid and valuable output.
False positives (seeing alpha that isn't there) are worse than false
negatives (missing alpha that exists), because they lead to overconfident
positions.

## Epistemic Honesty

This analysis uses public information. The market has access to the same
data. Alpha from this system does not come from secret information — it
comes from disciplined interpretation of known facts and rigorous testing
of non-consensus views. The system helps the user think clearly, not think
magically.

## Phase 1 — Alpha Scan (Automatic)

Cross-reference `bq_analysis.json` with `investment_thesis.json` and its
intermediate outputs (`valuation.json`, `technical.json`, `events.json`).
Look for these divergence patterns:

### Divergence Patterns

| Pattern | Detection | Example |
|---------|-----------|---------|
| **Framework mismatch** | Normalized P/E vs trailing P/E differ by >1.8x | Market applies cyclical discount to potentially structural business |
| **Growth expectation gap** | Reverse DCF implied growth vs BQ-supported growth differ by >30% relative | Market prices in less (or more) growth than evidence supports |
| **Dimension split** | Forward score exceeds Fundamental score by >1.5 points | Transformation underway but not yet in financials |
| **Smart money divergence** | Insider direction contradicts analyst consensus | Information asymmetry between those who operate vs those who model |
| **Technical-fundamental disconnect** | BQ ≥ 7.5 but entry_favorability = strong_avoid (or vice versa) | Quality company in distressed price action — accumulation or value trap? |
| **Peer valuation outlier** | Company multiple vs peer median deviates >50% without proportional fundamental premium | Possible mispricing relative to comparable businesses |

### Scan Rules

- Apply a significance threshold — only surface divergences where the
  magnitude is genuinely unusual, not just any difference.
- Maximum 3 candidates. If more exist, rank by magnitude and novelty.
- For each candidate, state:
  - The divergence (what vs what)
  - Direction (bullish alpha or bearish alpha)
  - Initial strength estimate (strong / moderate / weak)
  - One-line articulation of what the market might be getting wrong
- Output "No significant alpha signals detected" when appropriate.
  This is the expected output for most well-covered large-cap stocks.

### Scan Output

```json
{
  "alpha_candidates": [
    {
      "id": 1,
      "pattern": "framework_mismatch",
      "direction": "bullish",
      "initial_strength": "moderate",
      "divergence": "Normalized P/E 27.9x vs trailing 17.3x (1.6x ratio)",
      "hypothesis_seed": "Market applies cyclical discount but HBM contract structure may have changed the cycle"
    }
  ],
  "scan_note": "1 signal detected. Most divergences are within normal range.",
  "events_freshness": {
    "status": "fresh",
    "events_as_of": "<YYYY-MM-DD — date component of events.meta.generated_at>",
    "reused_from": null,
    "days_stale": 0
  }
}
```

### events_freshness — mandatory field

The scan reads `events.json` for several patterns (smart money divergence,
technical-fundamental disconnect). `events.json` may be fresh today OR
reused from a prior thesis run (up to 7 days old — ceiling_7d gate).
The `events_freshness` block records which, so a reader can judge how
current the insider / analyst / macro signals driving a candidate are.

Derivation from `events.json.meta`:
- `meta.reuse_meta` absent → `status="fresh"`, `events_as_of` = date
  component of `meta.generated_at`, `reused_from=null`, `days_stale=0`.
- `meta.reuse_meta.reused_from` present → `status="reused"`,
  `events_as_of` = date component of `meta.generated_at` (preserved
  original fresh-gen date across chain), `reused_from` = same value
  (redundant but explicit), `days_stale` = days between today ET and
  `events_as_of`.

This field is write-only and never read by the delta layer — it is
pure transparency for the human reader and downstream consumers.

Present candidates to the user via AskUserQuestion. Ask which (if any)
they want to investigate. Include option 0 (skip).

## Phase 2 — Hypothesis Articulation (Interactive)

When the user selects a candidate, help them articulate a testable
hypothesis through structured questions. This is a conversation, not
a monologue — use AskUserQuestion.

### Four Questions

1. **Market consensus**: "Based on the pricing, the market believes [X].
   Is this your reading too?"
   — State what the current price implies (from valuation.json data).

2. **Your variant view**: "Where specifically do you disagree with this?"
   — The user must name the specific assumption they think is wrong.
   — If the user cannot articulate a specific disagreement, this may not
   be alpha — it may be hope. Say so respectfully.

3. **Necessary conditions**: "If you're right, what must be true?"
   — Help the user derive the logical implications of their view.
   — Surface hidden assumptions they may not have considered.

4. **Strongest evidence**: "What is your single best piece of evidence?"
   — One concrete, verifiable data point. Not a narrative.

### Articulation Output

Synthesize into a clean hypothesis statement:

```
Hypothesis: "[Specific claim about what the market is mispricing]"
Variant view: "[How your view differs from consensus]"
Required conditions: ["Condition 1", "Condition 2", ...]
Key evidence: "[The user's strongest data point]"
Implied fair value if correct: [range]
```

Confirm with the user before proceeding to adversarial testing.

## Phase 3 — Adversarial Testing (Two Parallel Agents)

Spawn two agents in parallel. They operate independently and cannot see
each other's work. This structural adversarialism is the core anti-bias
mechanism — it cannot be skipped or softened.

### Agent A — Advocate

Role: Build the strongest possible case that the hypothesis IS correct.

- Re-examine existing data (bq_analysis, valuation, technical, events)
  through the lens of "assume the hypothesis is true"
- Optionally WebSearch for supporting evidence (not required — quality
  over quantity)
- Construct: if hypothesis is correct, what is the fair value? What is
  the expected trajectory? What catalysts would confirm it?
- Output: `alpha_advocate.json`

### Agent B — Prosecutor

Role: Build the strongest possible case that the hypothesis is WRONG
and the market is right.

- Re-examine the same data through the lens of "the market is efficient
  and has already considered this"
- Optionally WebSearch for refuting evidence
- Construct: why has the market priced it this way? What do smart
  participants know that makes the current price correct? What
  historical precedents show this type of thesis failing?
- Output: `alpha_prosecutor.json`

### Agent Rules

- Both agents must engage with the OTHER side's strongest argument,
  not just build their own case. "The strongest counter to my position
  is [X], and I respond with [Y]."
- Evidence quality matters more than quantity. One verified contract
  term outweighs ten opinion articles.
- Agents should flag the confidence level of each piece of evidence:
  hard data (filings, contracts) > analyst estimates > media reports >
  social sentiment.

## Phase 4 — Verdict

Read both `alpha_advocate.json` and `alpha_prosecutor.json`. Produce a
final assessment. The verdict agent must be genuinely independent — it
does not split the difference or hedge every statement.

### Verdict Steps

1. **Evidence weight**: Which side has higher-quality evidence? Be specific
   about which individual pieces of evidence are most compelling and why.
   Do not count arguments — weigh them.

2. **Residual risks**: Even if the advocate wins, which prosecutor arguments
   remain valid risks? These are not dismissed — they become kill criteria.

3. **Forced pre-mortem**: "Imagine the user took this position at current
   price. Six months later they have lost 30%. Write a specific,
   plausible story of what happened." This must be a concrete narrative
   (names, dates, numbers), not an abstract "macro deteriorated."

4. **Kill criteria**: 2-4 specific, measurable, time-bound conditions that
   would falsify the hypothesis.
   Bad: "if margins compress"
   Good: "Q3 <FYxxxx> gross margin < 60% (report ~<YYYY-MM-DD>)"

5. **Hypothesis rating**:
   - `strong` — Advocate evidence is materially stronger, hypothesis has
     genuine structural support, risk is asymmetric in the user's favor
   - `moderate` — Evidence is mixed but hypothesis is plausible, worth
     a position with strict kill criteria
   - `weak` — Prosecutor evidence is stronger, hypothesis is more hope
     than analysis, market pricing appears rational
   - `rejected` — Clear evidence against, no testable path to validation

6. **Conditional valuation**: If hypothesis is correct, what is the
   adjusted fair value range and ER/CE?

### Verdict Output — alpha_verdict.json

```json
{
  "hypothesis": "The articulated hypothesis statement",
  "rating": "moderate",
  "evidence_balance": {
    "advocate_strongest": "HBM contracts are 12+ month fixed-price...",
    "prosecutor_strongest": "Every prior memory 'structural shift' reverted...",
    "verdict": "Advocate has stronger near-term evidence, prosecutor has stronger historical base rate"
  },
  "pre_mortem": "Specific failure narrative...",
  "kill_criteria": [
    {"condition": "Q3 <FYxxxx> gross margin < 60%", "check_date": "<YYYY-MM-DD>", "source": "[Filing: Q3 <FYxxxx> 10-Q]"},
    {"condition": "HBM ASP declines >10% QoQ", "check_date": "<YYYY-MM-DD>", "source": "[WebSearch: trendforce_hbm_pricing]"}
  ],
  "conditional_valuation": {
    "if_hypothesis_correct": {"fair_value_range": [400, 500], "er_pct": 15.2, "ce": 0.45},
    "if_hypothesis_wrong": {"fair_value_range": [180, 260], "downside_pct": -35}
  },
  "epistemic_note": "This analysis uses public information. The market has access to the same data. Your edge, if it exists, comes from your interpretation and conviction, not from this system."
}
```

## Critical Rules

1. **Conservative scan** — Most stocks should return "no significant alpha."
   Alpha is rare by definition. A system that always finds alpha is broken.

2. **No echo chamber** — The adversarial structure is non-negotiable.
   A single agent doing "balanced analysis" does not achieve the same
   intellectual rigor as structural opposition.

3. **Pre-mortem is mandatory** — Must be a specific, plausible failure
   narrative. "Things could go wrong" is not a pre-mortem.

4. **Kill criteria must be actionable** — Specific number + specific date
   + canonical `[KIND: descriptor]` source tag (per
   `.claude/rules/anti-hallucination.md`). The user should be able to set
   a calendar reminder.

5. **Epistemic honesty** — Always include the reminder that alpha comes
   from the user's judgment, not the system's analysis. The system
   disciplines thinking; it does not generate insight.

6. **Hypothesis articulation filters pseudo-alpha** — If the user cannot
   name a specific disagreement with consensus, there is no alpha to
   test. Say so directly.

7. **Verdict must take a stance** — "It depends" or "both sides have
   merit" without a clear rating is a failure of analysis. The rating
   can be "moderate" or "weak", but it must be definitive.
