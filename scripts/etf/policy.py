"""P.POLICY_APPROVAL — is this ticker currently authorized by the owner?

The approval record is the OWNER'S authorization, not a provider-verifiable
structural finding. Nothing a provider says can create it, improve it, or
substitute for it: a fund that scans clean on every quantitative screen and is
not in the mapping is `not_listed`, and that is not a defect.

Approvals expire. A review is a judgement made against a fund as it was on a
date, and funds change mandate, sponsor and composition; C.APPROVAL_MAX_AGE_DAYS
bounds how long that judgement is allowed to carry.

All date arithmetic for approvals is implemented HERE and nowhere else.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional

from scripts.cli_utils import normalize_ticker
from scripts.schemas.strategy import parse_canonical_iso_date

# C.APPROVAL_MAX_AGE_DAYS
ETF_APPROVAL_MAX_AGE_DAYS = 90


_MISSING = object()


@dataclass(frozen=True)
class ApprovalResult:
    status: str                 # V.APPROVAL_STATUS
    reviewed_on: Optional[str]
    age_days: Optional[int]
    reasons: tuple[str, ...]


def _canonical(ticker) -> Optional[str]:
    try:
        return normalize_ticker(ticker)
    except (ValueError, TypeError):
        return None


def etf_policy_approval(policy, ticker: str,
                        run_date: datetime.date) -> ApprovalResult:
    """Resolve `ticker` against the compiled `etf_policy`.

    `policy` is a `scripts.schemas.strategy.EtfPolicy` or None. Only an exact
    canonical ticker key can be current — there is no wildcard, no category
    rule, and no implicit default, because any of those would authorize
    instruments the owner never looked at.
    """
    wanted = _canonical(ticker)
    if policy is None or wanted is None:
        return ApprovalResult("not_listed", None, None,
                              ("no etf_policy approval record for this ticker",))

    approved = getattr(policy, "approved_equity_etfs", None) or {}
    # A sentinel, not None. A key that is ABSENT means the owner never
    # approved this fund; a key PRESENT with a null date means they did and
    # the record is unreadable. Both refuse, but they are different facts and
    # they produce different refusal reasons downstream — reporting
    # "never approved" for a corrupted record hides the corruption.
    reviewed_on = _MISSING
    for key, value in approved.items():
        if _canonical(key) == wanted:
            reviewed_on = value
            break
    if reviewed_on is _MISSING:
        return ApprovalResult("not_listed", None, None,
                              (f"{wanted} is not in approved_equity_etfs",))

    if not isinstance(reviewed_on, str):
        return ApprovalResult(
            "invalid", None, None,
            (f"reviewed_on for {wanted} is {type(reviewed_on).__name__}, "
             f"not an ISO date string",))
    parsed = parse_canonical_iso_date(reviewed_on)
    if parsed is None:
        return ApprovalResult(
            "invalid", reviewed_on, None,
            (f"reviewed_on for {wanted} is not a canonical ISO date "
             f"(YYYY-MM-DD): {reviewed_on!r}",))

    if parsed > run_date:
        # A review dated after the run has not happened. Reading it as current
        # would let a typo'd year authorize a fund indefinitely.
        return ApprovalResult(
            "invalid", reviewed_on, None,
            (f"reviewed_on for {wanted} is {reviewed_on}, after the run date "
             f"{run_date.isoformat()}",))

    age_days = (run_date - parsed).days
    if age_days <= ETF_APPROVAL_MAX_AGE_DAYS:
        return ApprovalResult("current", reviewed_on, age_days, ())
    return ApprovalResult(
        "expired", reviewed_on, age_days,
        (f"owner review of {wanted} is {age_days} days old, over the "
         f"{ETF_APPROVAL_MAX_AGE_DAYS}-day limit",))
