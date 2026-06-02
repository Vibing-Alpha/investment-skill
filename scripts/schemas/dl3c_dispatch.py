"""Shared DL3c artifact-mode dispatch (§3.8.0).

This helper is imported by the DL3c-aware schema loaders:
  - scripts.schemas.fcf_inputs
  - scripts.schemas.historical_multiples
  - scripts.schemas.adr_correction
  - scripts.schemas.bq_analysis
  - scripts.schemas.investment_thesis

Every post-DL3c artifact carries `_dl3c_version: int` at root. This module
classifies an artifact into one of four modes:

  - "legacy_pre_dl3c"            — no `_dl3c_version`, no cert; synthesize
                                   usd_native CurrencyConversion.
  - "post_dl3c_usd_native"       — `_dl3c_version` present, cert absent
                                   (invariant 7: USD-native MUST emit no
                                   cert); synthesize usd_native cert. ALSO the
                                   home for a no-cert artifact whose
                                   `fx_failure_reason` is a NON-FX fail-close
                                   (NON_FX_FAILCLOSE_REASONS, e.g.
                                   adr_ratio_correction_required) — the
                                   currency is fine; the fail-close is
                                   orthogonal to FX state.
  - "post_dl3c_usd_converted"    — `_dl3c_version` present + cert present
                                   with basis=usd_converted; load cert via
                                   load_currency_conversion.
  - "post_dl3c_failed_fx"        — `_dl3c_version` present, cert absent, AND
                                   `fx_failure_reason` is a GENUINE FX failure
                                   (any closed-vocab value NOT in
                                   NON_FX_FAILCLOSE_REASONS). The data did NOT
                                   convert to USD — still local currency;
                                   `assemble._check_mixed_dl3c_modes` fail-
                                   closes on it. Unknown/typo reasons also land
                                   here (conservative fail-close direction).

Illegal states raise SchemaError:
  - `_dl3c_version` is not a positive int (rejects bool / str / 0 / -1).
  - `_dl3c_version` is from a future schema (e.g. 2): a future-version
    artifact may have a different cert shape that v1 loader cannot
    safely parse — silently accepting would corrupt downstream.
  - cert present but `_dl3c_version` absent (inconsistent state — pre-DL3c
    artifacts had no cert mechanism).
  - cert present with basis=usd_native (invariant 7: USD-native must emit
    NO cert).

cycle-15 F-15-4: validate _dl3c_version TYPE + VALUE, not just presence.
cycle-16: reject future versions to prevent silent v2 misparse.
cycle-25 F-25-1: SchemaError requires 3 args (artifact, field, message).
"""

from __future__ import annotations

from typing import Literal

from scripts.fx_constants import NON_FX_FAILCLOSE_REASONS
from scripts.schemas.currency_conversion import (
    CurrencyConversion,
    load_currency_conversion,
)
from scripts.schemas.errors import SchemaError

DL3C_SCHEMA_VERSION = 1

Dl3cMode = Literal[
    "legacy_pre_dl3c",
    "post_dl3c_usd_native",
    "post_dl3c_usd_converted",
    # post-impl loop-1 cycle-2 ISS-021: an artifact that carries
    # `_dl3c_version` + `fx_failure_reason` at root + no cert is NOT
    # USD-native — it's a FAILED FX-conversion record. Pre-fix it was
    # silently routed to `post_dl3c_usd_native`, which let
    # `assemble._check_mixed_dl3c_modes` interpret JPY-source data
    # whose FX fetch failed AS IF it were native USD. Distinct mode
    # surfaces the failure to mixed-mode + propagation logic.
    "post_dl3c_failed_fx",
]


def _synth_usd_native() -> CurrencyConversion:
    """Synthesized cert for legacy / post-DL3c usd_native artifacts.

    Returned to consumers so they can branch on `.basis` uniformly
    regardless of whether the cert was loaded from disk or auto-built.
    """
    return CurrencyConversion(
        basis="usd_native",
        source_currency="USD",  # fail-open-ok: canonical usd_native synthesized cert (DL3c §3.8.0)
        fx_source="usd_native",
        window=None,
    )


def dispatch_dl3c_mode(
    data: dict, *, artifact: str
) -> tuple[Dl3cMode, CurrencyConversion]:
    """Classify the artifact + return its currency-conversion cert.

    Args:
      data: parsed top-level dict of the artifact JSON.
      artifact: filename used in SchemaError messages (e.g.
        "fcf_inputs.json").

    Returns:
      (mode, currency_conversion) tuple.
    """
    raw_version = data.get("_dl3c_version")
    has_version = raw_version is not None

    if has_version:
        # Reject bool (which is a subclass of int), non-int, and non-positive.
        if (
            isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version < 1
        ):
            raise SchemaError(
                artifact,
                "_dl3c_version",
                f"must be a positive int; got {raw_version!r}",
            )
        if raw_version > DL3C_SCHEMA_VERSION:
            raise SchemaError(
                artifact,
                "_dl3c_version",
                f"value {raw_version} is from a future schema; this "
                f"loader handles v{DL3C_SCHEMA_VERSION} only. "
                f"Upgrade loader.",
            )

    has_cert = "currency_conversion" in data

    if not has_version:
        if has_cert:
            # Pre-DL3c artifact with cert is impossible — fail-close.
            raise SchemaError(
                artifact,
                "currency_conversion",
                "present but _dl3c_version missing; artifact in "
                "inconsistent state",
            )
        return "legacy_pre_dl3c", _synth_usd_native()

    if not has_cert:
        # post-impl loop-1 cycle-2 ISS-021: a no-cert artifact carrying an
        # explicit `fx_failure_reason` at root is the FX-failure-path
        # producer envelope (extract_fcf:287-292 / historical_multiples:318-345
        # / adr/correct skipped paths). The underlying data did NOT convert
        # to USD; surfacing this as a distinct mode lets `assemble._check_
        # mixed_dl3c_modes` fail-close when one artifact converted while
        # another's FX fetch failed (genuine partial-FX state).
        #
        # assemble-misclassification fix: route VALUE-aware, not bare key
        # presence. `adr_ratio_correction_required` (the only NON_FX_FAILCLOSE_
        # REASONS member) is plumbed through `fx_failure_reason` but is
        # currency-INDEPENDENT — the data is USD-native / already repaired, so
        # it is `post_dl3c_usd_native`, NOT failed_fx. Mapping it to failed_fx
        # made assemble abort with a misleading "FX conversion failed / add the
        # currency" FATAL when score-business was re-assembled in a dir holding
        # a ratio-unknown ADR's thesis artifacts (MRAAY). Unknown / typo
        # reasons still fall through to failed_fx (conservative direction kept).
        reason = data.get("fx_failure_reason")
        if reason is not None and reason not in NON_FX_FAILCLOSE_REASONS:
            return "post_dl3c_failed_fx", _synth_usd_native()
        return "post_dl3c_usd_native", _synth_usd_native()

    cert = load_currency_conversion(data["currency_conversion"])
    if cert.basis == "usd_native":
        raise SchemaError(
            artifact,
            "currency_conversion.basis",
            "post-DL3c artifact has cert with basis=usd_native; "
            "USD-native MUST emit NO cert per invariant 7",
        )
    return "post_dl3c_usd_converted", cert
