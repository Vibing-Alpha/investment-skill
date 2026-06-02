"""Mode A AdrProfile derivation + resolver for fetch.py / CLI use.

Spec source: docs/superpowers/specs/2026-05-09-dl3a-money-currency-sot-design.md §3.1, §3.5
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from scripts.adr.detect import detect_adr
from scripts.cli_utils import write_output as _atomic_write_json
from scripts.constants import KNOWN_ADR_CLASSIFICATIONS
from scripts.schemas.adr_profile import AdrProfile
from scripts.schemas.errors import SchemaError
# Module-level import (NOT `from ... import load_native_ticker`) so that
# `monkeypatch.setattr(_yf, "load_native_ticker", ...)` in tests works.
# `from X import f` rebinds the name at import time and defeats monkeypatch.
from scripts.sources import yahoo_finance as _yf


# §3.1 detect_adr reason → PrimaryAdrSignal mapping; this is THE single
# translation point per ISS-028 — no other code may compare against either
# spelling. Precedence: filings > category > explicit > foreign.
_DETECT_REASON_MAP = {
    "files_20f_or_6k": "filings_20f_or_6k",
    "category_contains_ADR": "category_string",
    "explicit_is_adr_flag": "explicit_flag",
    "foreign_domicile": "foreign_domicile",
}

_PRECEDENCE = ["filings_20f_or_6k", "category_string", "explicit_flag", "foreign_domicile"]


def _select_primary(mapped: list[str]) -> str:
    for candidate in _PRECEDENCE:
        if candidate in mapped:
            return candidate
    return mapped[0]


def derive_adr_profile(*, ticker: str, company_data: dict, as_of_date: str) -> AdrProfile:
    """§3.1 8-step derive algorithm."""
    # Step 1: normalize ticker
    ticker_n = (ticker or "").strip().upper()
    if not ticker_n:
        raise SchemaError("adr_profile.json", "ticker", "ticker is empty")

    # Resolver-side gate: company_data must have ticker AND country (intersection
    # of FD-path keys and pure-yfinance-fallback keys per §3.1 L130).
    if not company_data or not company_data.get("ticker") or not company_data.get("country"):
        raise SchemaError(
            "adr_profile.json", "company_data",
            "company_data missing required keys ticker/country (malformed/empty)"
        )

    # Step 2-3: call detect_adr; map detection_reasons
    detect = detect_adr(company_data)
    reasons = list(detect.get("detection_reasons", []) or [])
    mapped = [_DETECT_REASON_MAP[r] for r in reasons if r in _DETECT_REASON_MAP]

    # Step 2 empty-reasons branch
    if not mapped:
        primary_raw = "none_observed"
        secondary = ()
        detection_confidence_raw = "none"
    else:
        primary_raw = _select_primary(mapped)
        remaining = [m for m in mapped if m != primary_raw]
        # Dedup preserving first-occurrence order
        secondary = tuple(dict.fromkeys(remaining))
        detection_confidence_raw = detect.get("confidence", "low") or "low"

    is_adr_raw = bool(detect.get("is_adr", False)) or primary_raw != "none_observed"

    # Step 4: explicit tuple unpacking — load_native_ticker returns (str, str)
    native_ticker_raw, _native_currency_unused = _yf.load_native_ticker(ticker_n)
    native_ticker = (native_ticker_raw.strip() or None) if isinstance(native_ticker_raw, str) else None

    # Step 4.5: static-table override (ISS-020). Parallel to the
    # portfolio_yaml override in Step 5 but driven by the curated
    # KNOWN_ADR_CLASSIFICATIONS table at scripts/constants.py. Fires
    # when:
    #   1. portfolio.yaml has no native_ticker mapping for this ticker
    #      (Step 5 didn't fire), AND
    #   2. the ticker is in the static table with a non-domestic tier
    #      ("pure_adr" or "sec_foreign").
    # Use case: Financial Datasets API returns is_adr=False for known
    # foreign-private-issuers like MRAAY / NOK / TTDKY (Murata, Nokia,
    # TDK) — so detect_adr can't fire. The static table is the manual-
    # curation fallback per the comment at scripts/constants.py:60-65.
    static_classification = KNOWN_ADR_CLASSIFICATIONS.get(ticker_n)
    static_is_adr = (
        static_classification is not None
        and static_classification.get("data_quality_tier") in (
            "pure_adr", "sec_foreign",
        )
    )

    # Step 5: portfolio override
    if native_ticker is not None:
        is_adr = True
        primary_signal = "portfolio_yaml"
        if primary_raw == "none_observed":
            # §3.1 step 4 exception: do NOT prepend none_observed
            secondary_signals = secondary
        else:
            # Prepend previously-selected primary, dedup
            secondary_signals = tuple(dict.fromkeys((primary_raw,) + secondary))
        detection_confidence = "high"
    elif static_is_adr:
        # Step 5b: static-table override (ISS-020). Same shape as
        # portfolio_yaml override but with the table as the source.
        is_adr = True
        primary_signal = "known_adr_table"
        if primary_raw == "none_observed":
            secondary_signals = secondary
        else:
            secondary_signals = tuple(dict.fromkeys((primary_raw,) + secondary))
        detection_confidence = "high"
    else:
        is_adr = is_adr_raw
        primary_signal = primary_raw
        secondary_signals = secondary
        detection_confidence = detection_confidence_raw

    # Step 6: requires_20f with isinstance guard (FIX-5.4)
    _ct = company_data.get("company_type")
    if not isinstance(_ct, dict):
        _ct = {}
    requires_20f = bool(_ct.get("requires_20f", is_adr))

    # Step 7: provenance
    country = company_data.get("country") or ""
    if native_ticker is not None:
        provenance = (f"[YAML: portfolio.yaml.native_ticker={native_ticker}]",)
    elif primary_signal == "known_adr_table":
        # ISS-020: provenance tag for static-table override path.
        tier = (
            static_classification.get("data_quality_tier")
            if static_classification else "?"
        )
        provenance = (
            f"[Config: scripts/constants.py:KNOWN_ADR_CLASSIFICATIONS"
            f"[{ticker_n}]:tier={tier}]",
        )
    elif primary_signal == "none_observed":
        provenance = ("[Calc: domestic_default]",)
    elif "category" in company_data or "latest_filings" in company_data:
        # FD path
        if primary_raw == "category_string":
            provenance = (f"[API: 03_company_news.category={company_data.get('category', '')}]",)
        elif primary_raw == "filings_20f_or_6k":
            provenance = ("[API: 03_company_news.latest_filings:20-F]",)
        elif primary_raw == "explicit_flag":
            provenance = ("[API: 03_company_news.is_adr=True]",)
        else:
            provenance = (f"[API: 03_company_news.country={country}]",)
    else:
        # yfinance fallback only
        if primary_raw == "explicit_flag":
            provenance = ("[API: yfinance.info.is_adr=True]",)
        else:
            provenance = (f"[API: yfinance.info.country={country}]",)

    return AdrProfile(
        ticker=ticker_n,
        is_adr=is_adr,
        primary_signal=primary_signal,
        secondary_signals=secondary_signals,
        detection_confidence=detection_confidence,
        native_ticker=native_ticker,
        requires_20f=requires_20f,
        as_of_date=as_of_date,
        source_ticker=ticker_n,
        provenance=provenance,
    )


def resolve_adr_profile(
    *,
    ticker: str,
    company_data: dict,
    output_dir: Path,
    as_of_date: str,
    require: bool = True,
) -> Optional[AdrProfile]:
    """Mode A only — derive + write data/adr_profile.json.

    Caller-side gate (fetch.py): only invoke when `_should_fetch('03_company_news')`
    AND `(category_statuses.get('company') or {}).get('status') != 'FAILED'`
    (category_statuses[cat] is a dict, NOT an object with `.status` attribute —
    see Task 16 anchor). The not-fetched case
    routes through `require=`:
      - require=True (default) → SchemaError when company_data is empty
      - require=False → return None silently (subset-fetch workflow)
    Malformed-fetched (non-empty but missing required keys) ALWAYS raises
    regardless of `require=` (FIX-C5-A-H1 — upstream drift must surface).
    """
    not_fetched = not company_data
    if not_fetched:
        if require:
            raise SchemaError(
                "adr_profile.json", "company_data",
                "company_data empty and require=True; resolver cannot derive"
            )
        return None

    # malformed-fetched case: derive_adr_profile's resolver-side gate raises
    # SchemaError regardless of require=.
    profile = derive_adr_profile(
        ticker=ticker, company_data=company_data, as_of_date=as_of_date,
    )
    payload = {
        "ticker": profile.ticker,
        "is_adr": profile.is_adr,
        "primary_signal": profile.primary_signal,
        "secondary_signals": list(profile.secondary_signals),
        "detection_confidence": profile.detection_confidence,
        "native_ticker": profile.native_ticker,
        "requires_20f": profile.requires_20f,
        "as_of_date": profile.as_of_date,
        "source_ticker": profile.source_ticker,
        "provenance": list(profile.provenance),
    }
    # Atomic write (temp + os.replace) — concurrent CLI consumers must never
    # observe a partial / torn JSON file. write_output handles fdopen/replace
    # + cleanup on failure.
    _atomic_write_json(payload, str(output_dir / "adr_profile.json"))
    return profile
