"""ETF support — identity, profile, market snapshot, thesis authoring.

Design contract: docs/superpowers/specs/2026-08-21-etf-thesis-design.md
(the fenced `etf-contract` block is the normative authority; the prose
around it is rationale). Implementer constraints that the contract cannot
express live in the companion `-implementation-notes.md`.

WF.AUTHORING: "scripts/etf contains no direct HTTP" — every network call in
this package goes through `scripts.sources.fmp` or
`scripts.sources.yfinance_guard.yfinance_call`.
"""
