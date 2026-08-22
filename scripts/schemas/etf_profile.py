"""Typed contract for `A.ETF_PROFILE`.

Path: `reports/{T}/{DATE}/data/etf_profile.json`
Producer: `scripts.etf.profile` (PR.PROFILE)

`strict: true` and `source_artifact: true` — this is the artifact that says
whether a fund may be entered, so a drifted field must fail at the load
boundary. The two rules worth naming:

- **Missing is null, never zero or neutral.** A zero `avg_dollar_volume_usd`
  reads as an illiquid fund; a zero `max_holding_weight` reads as a perfectly
  diversified one. Both are lies about a measurement that was not taken.
- **`pass` may not carry refusal reasons.** `pass` means every refusal
  condition was false. Reasons beside it make the artifact self-contradicting,
  and a consumer reading one of the two fields acts on the wrong half.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from scripts.schemas.errors import SchemaError

_ARTIFACT = "etf_profile.json"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")

INSTRUMENT_TYPES = frozenset({"etf", "equity", "unknown"})
APPROVAL_STATUSES = frozenset({"current", "not_listed", "expired", "invalid"})
LEVERAGE_STATUSES = frozenset({"suspected", "not_suspected", "unknown"})
VECTOR_STATUSES = frozenset({"valid", "invalid", "missing"})
ENTRY_ELIGIBILITIES = frozenset({"pass", "block", "unknown"})
REFUSAL_REASONS = frozenset({
    "leveraged_or_inverse", "not_owner_approved", "owner_approval_expired",
    "owner_approval_invalid", "composition_unavailable",
    "composition_out_of_scope", "fund_of_funds_structure",
    "identity_unresolved", "leverage_unknown", "liquidity_unavailable",
})


def _err(field: str, message: str) -> SchemaError:
    return SchemaError(_ARTIFACT, field, message)


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _member(raw: Mapping, key: str, allowed: frozenset[str]) -> str:
    value = raw.get(key)
    if value not in allowed:
        raise _err(key, f"{value!r} is outside {sorted(allowed)}")
    return value


def _required_number(raw: Mapping, key: str) -> float:
    value = raw.get(key)
    if not _finite(value):
        raise _err(key, f"expected a finite number, got {value!r}")
    return float(value)


def _optional_number(raw: Mapping, key: str, *, positive=False,
                     nonnegative=False) -> Optional[float]:
    value = raw.get(key)
    if value is None:
        return None
    if not _finite(value):
        raise _err(key, f"expected a finite number or null, got {value!r}")
    if positive and value <= 0:
        raise _err(key, f"must be positive when present, got {value}")
    if nonnegative and value < 0:
        raise _err(key, f"cannot be negative, got {value}")
    return float(value)


def _optional_string(raw: Mapping, key: str) -> Optional[str]:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _err(key, f"expected a non-empty string or null, got {value!r}")
    return value


def _optional_date(raw: Mapping, key: str) -> Optional[str]:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise _err(key, f"expected an ISO date or null, got {value!r}")
    return value


def _required_timestamp(raw: Mapping, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        raise _err(key, f"expected an ISO timestamp, got {value!r}")
    return value


def _nonempty_map(raw: Mapping, key: str) -> Mapping:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise _err(key, f"expected an object, got {type(value).__name__}")
    if not value:
        raise _err(key, "must be non-empty — an artifact that records no "
                        "provenance cannot be audited after the fact")
    return value


@dataclass(frozen=True)
class EtfProfile:
    ticker: str
    identity_status: str
    approval_status: str
    leverage_status: str
    asset_allocation_status: str
    holdings_vector_status: str
    entry_eligibility: str
    entry_reasons: tuple[str, ...]
    stock_frac: float
    cash_frac: float
    non_equity_frac: float
    max_holding_weight: Optional[float]
    avg_volume_shares: Optional[float]
    avg_volume_shares_as_of: Optional[str]
    avg_dollar_volume_usd: Optional[float]
    price_usd: Optional[float]
    price_as_of: Optional[str]
    retrieved_at: str
    raw: Mapping[str, Any]


def validate_etf_profile(raw: Any) -> EtfProfile:
    if not isinstance(raw, dict):
        raise _err("<root>", f"expected object, got {type(raw).__name__}")

    ticker = raw.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise _err("ticker", f"expected a non-empty string, got {ticker!r}")

    identity_status = _member(raw, "identity_status", INSTRUMENT_TYPES)
    approval_status = _member(raw, "approval_status", APPROVAL_STATUSES)
    leverage_status = _member(raw, "leverage_status", LEVERAGE_STATUSES)
    allocation_status = _member(raw, "asset_allocation_status", VECTOR_STATUSES)
    holdings_status = _member(raw, "holdings_vector_status", VECTOR_STATUSES)
    eligibility = _member(raw, "entry_eligibility", ENTRY_ELIGIBILITIES)

    reasons = raw.get("entry_reasons")
    if not isinstance(reasons, list):
        raise _err("entry_reasons",
                   f"expected a list, got {type(reasons).__name__}")
    for r in reasons:
        if r not in REFUSAL_REASONS:
            raise _err("entry_reasons", f"{r!r} is outside "
                                        f"{sorted(REFUSAL_REASONS)}")
    if eligibility == "pass" and reasons:
        raise _err("entry_reasons",
                   f"entry_eligibility is `pass` but entry_reasons carries "
                   f"{reasons}; `pass` means every refusal condition was false")
    if eligibility != "pass" and not reasons:
        raise _err("entry_reasons",
                   f"entry_eligibility is {eligibility!r} with no reason; a "
                   f"refusal the user cannot act on is not a refusal")

    stock_frac = _required_number(raw, "stock_frac")
    cash_frac = _required_number(raw, "cash_frac")
    non_equity_frac = _required_number(raw, "non_equity_frac")
    _optional_number(raw, "allocation_sum_frac")
    max_weight = _optional_number(raw, "max_holding_weight")
    _optional_number(raw, "coverage_pct")
    expense = _optional_number(raw, "expense_ratio_pct", nonnegative=True)
    _optional_number(raw, "aum_usd", nonnegative=True)
    avg_volume = _optional_number(raw, "avg_volume_shares", positive=True)
    price_usd = _optional_number(raw, "price_usd", positive=True)
    dollar_volume = _optional_number(raw, "avg_dollar_volume_usd", positive=True)

    for key in ("asset_class", "category", "legal_type", "fund_name",
                "description"):
        _optional_string(raw, key)
    price_as_of = _optional_date(raw, "price_as_of")
    _optional_date(raw, "approval_reviewed_on")
    _optional_date(raw, "holdings_as_of")

    age = raw.get("approval_age_days")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, int) or age < 0:
            raise _err("approval_age_days",
                       f"expected a non-negative int or null, got {age!r}")

    holdings = raw.get("top_holdings")
    if holdings is not None:
        if not isinstance(holdings, list):
            raise _err("top_holdings",
                       f"expected a list or null, got {type(holdings).__name__}")
        for i, row in enumerate(holdings):
            fp = f"top_holdings[{i}]"
            if not isinstance(row, dict):
                raise _err(fp, f"expected an object, got {type(row).__name__}")
            if not isinstance(row.get("symbol"), str) or not row["symbol"]:
                raise _err(f"{fp}.symbol",
                           f"expected a non-empty string, got "
                           f"{row.get('symbol')!r}")
            if not _finite(row.get("weight_pct")):
                raise _err(f"{fp}.weight_pct",
                           f"expected a finite number, got "
                           f"{row.get('weight_pct')!r}")

    evidence = raw.get("leverage_evidence")
    if not isinstance(evidence, list):
        raise _err("leverage_evidence",
                   f"expected a list, got {type(evidence).__name__}")

    if not isinstance(raw.get("approval_reasons"), list):
        raise _err("approval_reasons", "expected a list")

    retrieved_at = _required_timestamp(raw, "retrieved_at")
    volume_as_of = raw.get("avg_volume_shares_as_of")
    if volume_as_of is not None:
        _required_timestamp(raw, "avg_volume_shares_as_of")

    _nonempty_map(raw, "field_provenance")
    source_status = _nonempty_map(raw, "source_status")
    for name, entry in source_status.items():
        fp = f"source_status.{name}"
        if not isinstance(entry, dict):
            raise _err(fp, f"expected an object, got {type(entry).__name__}")
        if entry.get("status") not in ("ok", "missing", "error"):
            raise _err(f"{fp}.status",
                       f"{entry.get('status')!r} is outside "
                       f"['error', 'missing', 'ok']")

    return EtfProfile(
        ticker=ticker, identity_status=identity_status,
        approval_status=approval_status, leverage_status=leverage_status,
        asset_allocation_status=allocation_status,
        holdings_vector_status=holdings_status,
        entry_eligibility=eligibility, entry_reasons=tuple(reasons),
        stock_frac=stock_frac, cash_frac=cash_frac,
        non_equity_frac=non_equity_frac, max_holding_weight=max_weight,
        avg_volume_shares=avg_volume, avg_volume_shares_as_of=volume_as_of,
        avg_dollar_volume_usd=dollar_volume, price_usd=price_usd,
        price_as_of=price_as_of, retrieved_at=retrieved_at, raw=raw,
    )


def load_etf_profile(path: str | Path) -> EtfProfile:
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _err("<file>", f"unreadable: {exc}") from exc
    return validate_etf_profile(raw)
