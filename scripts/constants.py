"""Shared runtime constants for the v7 scripts package.

Single source of truth for API endpoints, thresholds, and category definitions.
"""


class Status:
    """Canonical status values used across all scripts.

    Use these instead of raw strings to prevent silent typo bugs.
    """
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    WARNING = "WARNING"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    INCOMPLETE = "INCOMPLETE"
    SKIPPED = "SKIPPED"
    ADR_CHECK = "ADR_CHECK"

# BQ-ratio candidate set for the metrics snapshot — the canonical answer to
# "does this snapshot carry something a business-quality score can read".
# CANDIDATES, not requirements: any ONE present is enough, because the set a
# given issuer legitimately reports varies (banks report no quick_ratio,
# financial-sector companies no gross_margin) and a false "insufficient" on a
# critical-tier category is a permanent entry veto. P/E is deliberately NOT
# in this set: market cap plus a P/E carries no profitability or liquidity
# signal, and P/E is null by nature for pre-profit issuers. Read by BOTH the
# fetch.py metrics post-pass and the yfinance fallback's status rule (twelfth
# cold round) — one implementation, so the two layers cannot disagree.
BQ_METRIC_FIELDS = frozenset({
    "gross_margin", "operating_margin", "net_margin",
    "return_on_equity", "return_on_assets",
    "current_ratio", "quick_ratio",
    # forty-fifth round: four more fields the scoring calibration
    # demonstrably consumes (ROIC, asset turnover, leverage, coverage) —
    # their absence from this any-of set turned a valid snapshot carrying
    # ONLY them into a permanent entry veto. Membership only widens what
    # counts as usable: the false-veto-safe direction.
    "return_on_invested_capital", "asset_turnover",
    "debt_to_equity", "interest_coverage",
})

CATEGORIES = {
    "price": {"importance": "critical", "retry_count": 3},
    "metrics": {"importance": "critical", "retry_count": 3},
    "financials": {"importance": "critical", "retry_count": 2},
    "company": {"importance": "important", "retry_count": 2},
    "news": {"importance": "important", "retry_count": 2},
    "filing": {"importance": "critical", "retry_count": 3},
    "insider": {"importance": "auxiliary", "retry_count": 1},
    "analyst_estimates": {"importance": "auxiliary", "retry_count": 1},
    "earnings": {"importance": "auxiliary", "retry_count": 1},
    "institutional": {"importance": "auxiliary", "retry_count": 1},
    "macro_rates": {"importance": "auxiliary", "retry_count": 1},
    # historical stays IMPORTANT — a DELIBERATE TRADEOFF (thirty-third cold
    # round): "no BQ scoring agent reads it, so it should not gate" audits
    # only the BQ dims and misses the thesis layer built on the SAME run's
    # data — the ~2-year weekly series in 01_price_data.json is
    # historical_multiples' documented input (the 2Y valuation bands the
    # entry decision consumes via the thesis), so losing it degrades a
    # money-path number. And it cannot become a standing veto: measured,
    # historical is PASSED on 42 of 43 stored runs — degradation here is a
    # run-level event, exactly what the gate exists to flag.
    "historical": {"importance": "important", "retry_count": 1},
    "segmented_revenues": {"importance": "auxiliary", "retry_count": 0},
    "eps_validation": {"importance": "auxiliary", "retry_count": 0},
    "growth_stock_mode": {"importance": "auxiliary", "retry_count": 0},
}

BASE_URL = "https://api.financialdatasets.ai"
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"
# FMP's newer "stable" surface. Used only where v3/v4 lack a field we need:
# the revenue-segmentation endpoints carry `reportedCurrency` there, which the
# v4 `structure=flat` form omits — and a segment row with an assumed-USD
# currency is exactly the DL3a §2 hazard for non-USD ADRs.
FMP_STABLE_BASE_URL = "https://financialmodelingprep.com/stable"
YAHOO_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

MIN_FILING_ITEM_CHARS = 1000
REQUIRED_10K_ITEMS = ["item_1", "item_1a", "item_7"]
REQUIRED_10Q_ITEMS = ["item_2"]
REVENUE_NOTE_10K_ITEMS = ["item_8"]
REVENUE_NOTE_10Q_ITEMS = ["item_1"]
MAX_PRICE_AGE_DAYS = 1       # was 7; only same-day passes cleanly
WARNING_PRICE_AGE_DAYS = 7   # 2..7 days triggers WARNING; >7 days FAILED
# CRITICAL_PRICE_AGE_DAYS retained for backward-compat but no longer
# gates the PASSED/WARNING/FAILED classification (HIGH-20).
CRITICAL_PRICE_AGE_DAYS = 365
MAX_52WEEK_DEVIATION = 0.20
MAX_FINANCIAL_AGE_DAYS = 120
EPS_TTM_DEVIATION_THRESHOLD = 0.02
EPS_PE_PRICE_DEVIATION_THRESHOLD = 0.01

# ---------------------------------------------------------------------------
# Static ADR classification fallback table
# ---------------------------------------------------------------------------
# Used by fetch.py classify_ticker() when the API company_facts endpoint is
# unavailable or does not return ADR category information.  This is a curated
# list of known tickers maintained manually -- the runtime detect_adr() in
# scripts/adr/detect.py is the primary detection path.

KNOWN_ADR_CLASSIFICATIONS = {
    # Pure ADR (20-F filers, non-1:1 deposit ratio)
    "ASX":   {"filing_type": "20-F", "needs_ratio_correction": True,  "data_quality_tier": "pure_adr"},
    "TSM":   {"filing_type": "20-F", "needs_ratio_correction": True,  "data_quality_tier": "pure_adr"},
    "BABA":  {"filing_type": "20-F", "needs_ratio_correction": True,  "data_quality_tier": "pure_adr"},
    "NVO":   {"filing_type": "20-F", "needs_ratio_correction": True,  "data_quality_tier": "pure_adr"},
    # ISS-020 (post-DL3c loop-3 backlog): Murata Manufacturing JPY ADR
    # (1 ADR ≈ 0.5 underlying); pre-fix Financial Datasets API returned
    # is_adr=False → compute_adr_valuation_correction took the
    # "not_applicable" early-exit and adr_correction.json missed
    # corrected_pe/adr_ratio. Cross-source PE divergence was 4× (API
    # 44.85 vs computed 11.29) until this entry forced the correction
    # path via the derive_adr_profile known-adr-table fallback.
    "MRAAY": {"filing_type": "20-F", "needs_ratio_correction": True,  "data_quality_tier": "pure_adr"},
    # ISS-020: TDK Corp JPY ADR. Currently annual-only Financial
    # Datasets coverage (DL5 territory for quarterly unlock) but the
    # correction path is correct to run when adr_units / latest_shares
    # are derivable.
    "TTDKY": {"filing_type": "20-F", "needs_ratio_correction": True,  "data_quality_tier": "pure_adr"},
    # SEC-filing foreign companies (10-K filers OR foreign-private-
    # issuer 20-F filers with 1:1 deposit ratio — near-domestic data
    # quality, no ratio correction needed).
    "CLS":   {"filing_type": "10-K", "needs_ratio_correction": False, "data_quality_tier": "sec_foreign"},
    "LULU":  {"filing_type": "10-K", "needs_ratio_correction": False, "data_quality_tier": "sec_foreign"},
    "SHOP":  {"filing_type": "20-F", "needs_ratio_correction": False, "data_quality_tier": "sec_foreign"},
    # ISS-020: Nokia is a Finnish foreign-private-issuer trading on
    # NYSE with 1:1 underlying-to-ADS ratio (no deposit-ratio
    # correction needed) but is foreign-domiciled (needs
    # currency-aware FX path that DL3c added).
    "NOK":   {"filing_type": "20-F", "needs_ratio_correction": False, "data_quality_tier": "sec_foreign"},
}
