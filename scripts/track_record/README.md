# `scripts.track_record` — broker-fact archiver, intent journal, quarterly render

A runtime, not a workflow. Four modules and a CLI; no orchestration layer
ships with them, deliberately (see **What does not ship** below).

```
python3 -m scripts.track_record pull   --root reports/track-record/raw < envelope.json
python3 -m scripts.track_record tag    --at <YYYY-MM-DDTHH:MM:SSZ>     < event.json
python3 -m scripts.track_record open
python3 -m scripts.track_record unlinked
python3 -m scripts.track_record report --quarter 2026Q2
```

Standard library only, plus `scripts.sources.yfinance_guard` for the one
network call (the benchmark fetch). Python 3.10+.

## What it is for

Broker history expires. IBKR's trade window rolls off, and the reasoning
behind a trade cannot be reconstructed from a fill afterwards. This preserves
both, and computes almost nothing on top — the design's own sizing rule is
*build now only what cannot be added later; compute now only what is nearly
free.*

- **`archive.py`** — writes each tool response to disk verbatim, before
  anything parses it, then answers whether the archived pulls actually cover
  a requested UTC window. Files are never overwritten and never deleted.
  Publishing is atomic: bytes land in a same-directory stage file and are
  linked into place, so an interrupted write leaves nothing under an archive
  name.
- **`journal.py`** — an append-only JSONL of intent events
  (`thesis_opened` / `thesis_superseded` / `orders_linked` /
  `thesis_retired` / `prediction_resolved`). Every append validates the
  COMPLETE resulting fold — duplicate declarations, dangling references,
  supersede cycles, two successors, order double-links — before writing a
  byte.
- **`summary.py`** — renders one quarter: fill and order counts, max drawdown
  from the broker's own TWR series, and the account-minus-benchmark spread
  against a benchmark CSV pinned once per quarter and never re-fetched.
- **`__main__.py`** — the CLI above.

## The property worth reading the code for

**Every number degrades to `unavailable (reason)` rather than guess**, and
the reason names what was missing. A coverage gap, a conflicting execution, a
corrupt benchmark pin, an unparseable archive file — each blanks the figures
it feeds and no others. The motivating failure was not a trading loss: it was
ad-hoc analysis producing five mutually contradictory conclusions and six
plausible, report-ready wrong numbers. A refusal must never look like a value.

## What does not ship, and why

**The orchestration layer.** The skill that drives this CLI stays private,
because its backup protocol pushes to whatever `origin` a clone has — correct
for its author's private repo, wrong for anyone who installed from a public
one. Until a backup topology exists that is right for a stranger, shipping
the workflow would be shipping a way to publish your own trade history.

**The operator's data and their Phase-0 evidence.** The archive, the journal,
and the probe figures the frozen decisions were read off are all private.

## Scope, stated as disqualifying conditions

- **Interactive Brokers only**, through the IBKR MCP server. The frozen
  decisions this code compiles against are IBKR's response contract — the
  `period`-to-UTC-range mapping, `trade_id` as the execution key, `cps` as a
  decimal TWR series, `contract_id` living only on positions rows. None of it
  is portable; another broker needs its own probe.
- **Single account.** The IBKR MCP wire format has nowhere to put an account
  id, so `summary._SINGLE_ACCOUNT` is the archive's namespace for "the one
  account this connection reaches". An archive root spanning two accounts
  merges their orders — and a journal reference would suppress both from the
  untagged backlog, losing a real decision silently. No code gate can detect
  this; the rows carry no account either way.
- **Envelope fidelity depends on the harness.** `pull` takes the raw response
  wrapped in an envelope. A real `YEAR_TO_DATE` trades response is over
  300,000 characters — build the envelope from bytes (`json.load` on the file
  your harness writes for oversized tool results), never by retyping. On a
  harness that inlines everything, there is no fidelity-preserving path for a
  response that size, and this archive is the layer that cannot be re-derived.

## Status

Experimental. One operator, one IBKR connection. The first hour of real use
falsified a frozen decision that nine cold review cycles had left standing —
the broker restates settled `order_id`s, which the conflict rule had been
reading as a corrupted execution. Expect the same of the remaining rulings
until a second account has exercised them.
