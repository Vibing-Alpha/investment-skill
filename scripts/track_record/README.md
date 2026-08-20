# `scripts.track_record` — broker-fact archiver, intent journal, quarterly render

Four modules and a CLI. A Claude Code skill that drives them ships alongside
at `.claude/skills/track-record/`; the CLI is equally usable on its own.

```
python3 -m scripts.track_record pull   --root reports/track-record/raw < envelope.json
python3 -m scripts.track_record tag    --at <YYYY-MM-DDTHH:MM:SSZ>     < event.json
python3 -m scripts.track_record open
python3 -m scripts.track_record unlinked
python3 -m scripts.track_record report --quarter 2026Q2
```

Standard library for everything except the one network call: the first
benchmark pin of a quarter fetches via `yfinance` (pinned in
`requirements.txt`, installed by setup) behind `scripts.sources.yfinance_guard`.
Archiving, journalling and every count work with no third-party import and no
network at all. Python 3.10+.

## What it is for

Broker history expires. IBKR's trade window rolls off, and the reasoning
behind a trade cannot be reconstructed from a fill afterwards. This preserves
both, and computes almost nothing on top — the design's own sizing rule is
*build now only what cannot be added later; compute now only what is nearly
free.*

- **`archive.py`** — writes each tool response to disk verbatim, before
  anything parses it, then answers whether the archived pulls actually cover
  a requested UTC window. Files are never overwritten and never deleted.
  Publishing is atomic where the filesystem allows it: bytes land in a
  same-directory stage file and are hardlinked into place, so an interrupted
  write leaves nothing under an archive name. Where hardlinks are refused
  (some network and FUSE mounts, exFAT), it falls back to claiming the name
  and replacing it — exclusivity is kept, atomicity is not, and a process
  killed in that window leaves a ZERO-BYTE file. That file blanks the tool's
  figures until it is removed, and the refusal it produces says so and says
  deleting it loses nothing.
- **`journal.py`** — a JSONL of intent events, appended and never rewritten
  by this module (the skill's correction step edits a line in place, and says
  so)
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
it feeds, and the others keep rendering.

Two of those are wider than "this quarter", deliberately, and it is worth
knowing which before it happens. An **unparseable archive file** blanks that
tool's figures in EVERY quarter, not only the one it was pulled for: its
`args` cannot be read, so there is no way to tell which window it covered,
and an archive that cannot be fully read cannot prove coverage of any window.
The refusal names the file; the archive never deletes anything, so clearing
it is yours to do. A **conflicting execution** — the same `trade_id`
archived twice with different content — blanks fills, orders and
order_unknown for the quarter that execution falls in, and leaves every other
quarter alone. The motivating failure was not a trading loss: it was
ad-hoc analysis producing five mutually contradictory conclusions and six
plausible, report-ready wrong numbers. A refusal must never look like a value.

## Your files stay on your disk — which is not the same as unseen

Nothing here runs `git`, uploads, or copies anything anywhere. The archive,
the journal and the benchmark pin are written under your working tree and
stay there — **backing them up is yours to arrange**, and the skill's job is
only to keep telling you which files are unprotected.

Two things follow that are easy to miss:

- The published `.gitignore` excludes `reports/track-record/` and
  `/trade-journal.jsonl` so a routine `git add -A` in this clone cannot
  commit them. If you move them, keep them out of any repository you do not
  control.
- The **skill** hands file contents to a model: it reads the journal to find
  due predictions, shows you a line before and after a correction, and reads
  the quarterly report back to you. That is how it works, not a leak — but it
  is not the same as "the data stays on disk". Use the CLI directly if you
  want the archive built without any of it passing through a model.

Not published: the recording operator's own data, and the probe figures their
frozen decisions were read off. The rulings in
`.claude/skills/track-record/references/ibkr-freeze.md` (repo-relative — it
ships beside the skill, not beside this package)
stand on their own and each names what it was read from.

## One limit worth stating

Numbers are read with the standard JSON parser, so every decimal becomes a
binary64 float before it is archived. A response carrying more precision
than that holds — more than about 17 significant digits — would be stored
slightly changed, in a file whose whole premise is that it is verbatim.

Not fixed, and the reason is measurable rather than a shrug: across 6874
numeric values in the live archive, not one differs from its source text
after that round trip. IBKR sends prices and sizes that binary64 represents
exactly. Preserving arbitrary precision would mean a `Decimal` pipeline
through every arithmetic path in the package, to defend against a shape
this feed does not produce.

## Before the first run

You need the **IBKR MCP server** connected to your agent and answering — this
tool only reads it, and cannot install or authenticate it for you. Confirm
`get_account_positions` returns your account before starting; if the five
tools named above are not available, stop, because nothing here has another
data source.

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
falsified a frozen decision that a long review had left standing —
the broker restates settled `order_id`s, which the conflict rule had been
reading as a corrupted execution. Expect the same of the remaining rulings
until a second account has exercised them.
