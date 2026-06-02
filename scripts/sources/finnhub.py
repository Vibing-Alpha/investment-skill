"""Finnhub news API adapter — placeholder.

ISS-024 (Cycle 4): the original `fetch_news` returned PASSED-empty,
which mis-represented "not implemented" as "successful no-data".
Real Finnhub news fallback lives in
`scripts.sources.financial_datasets._fetch_news_finnhub` (called
from `fetch_news_data` when the primary FD news endpoint returns
zero items).

This module is kept as a future-impl placeholder. `fetch_news`
returns FAILED so any caller that bypasses the production
`fetch_news_data` flow gets a clear "not implemented" signal
rather than silent empty-success.
"""
from scripts.sources.adapter_result import AdapterResult, ErrorCode


def fetch_news(ticker: str, days_back: int = 7) -> AdapterResult:  # adapter-helper-ok: deliberate non-entrypoint placeholder; not in ADAPTER_ENTRYPOINTS
    """Stub. Returns FAILED with detail explaining the real Finnhub
    path. Removed from ADAPTER_ENTRYPOINTS — Pattern S no longer
    audits this signature.

    ISS-220 4.32 (Loop38 cycle 1, iter7): codex review flagged this
    as "AdapterResult-returning function outside the canonical
    registry → can drift without enforcement." Resolution: keep the
    placeholder shape (signaling non-implementation via FAILED is
    safer than removing the symbol entirely, which would break
    legacy import paths), but explicitly annotate as a deliberate
    non-entrypoint via the `# adapter-helper-ok` audit comment. The
    real Finnhub HTTP path lives in financial_datasets
    `_fetch_news_finnhub` and IS in HTTP_INFRASTRUCTURE_ALLOWLIST.
    """
    return AdapterResult.failed(
        code=ErrorCode.INTERNAL_ERROR,
        detail=(
            "finnhub.fetch_news is a placeholder stub; "
            "real Finnhub fallback is "
            "scripts.sources.financial_datasets._fetch_news_finnhub "
            "(invoked internally by fetch_news_data on primary empty)"
        ),
        source="finnhub.fetch_news",
        retryable=False,
        meta={"source_hint": "finnhub_stub_not_implemented"},
    )
