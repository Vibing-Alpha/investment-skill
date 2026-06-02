"""Industry slug → sector ETF mapping.

Producer-side enforcement of the mapping table that previously lived in
`.claude/skills/research-industry/SKILL.md`. Promoting it to code lets the
SKILL.md stay thin (orchestration only) and gives us a single source of
truth that tests can pin.

The mapping is pattern-based, not exact-match: an industry slug like
`ai-chips` matches the `ai-chips` prefix entry; `mid-cap-regional-banks`
matches `regional-banks` and falls through to XLF; an unknown slug like
`mlcc` falls through to the SOXX-via-adjacency rule or finally to SPY.

Usage:
    python3 -m scripts.industry.sector_etf_map --slug ai-chips
    python3 -m scripts.industry.sector_etf_map --slug mlcc
    python3 -m scripts.industry.sector_etf_map --slug unknown-stuff
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import NamedTuple


class ETFChoice(NamedTuple):
    etf: str
    proxy_note: str  # Empty if direct match; non-empty when chosen as proxy


# Pattern → ETF. Patterns are matched as substring-contains against the
# slug, in declaration order. First match wins, so put more-specific
# patterns first.
#
# Each entry: (substring_patterns, etf, is_proxy, proxy_note).
# is_proxy=True means the ETF is chosen as a thematic proxy because no
# direct sector ETF exists. The `proxy_note` is surfaced in the JSON so
# the agent can copy it into `regime_rationale`.
_MAP = (
    # Semiconductors and adjacent component plays
    (("ai-chips", "ai-chip", "semiconductor", "semi-", "mlcc",
      "passive-component", "memory", "foundry"),
     "SOXX",
     False,
     ""),
    # Cybersecurity has a dedicated ETF
    (("cybersecurity", "infosec", "cyber-security"),
     "HACK",
     False,
     ""),
    # Banks / financials
    (("regional-bank", "regional-banks", "community-bank", "bank-", "banks"),
     "XLF",
     False,
     ""),
    (("financial", "fintech", "insurance", "asset-manager"),
     "XLF",
     False,
     ""),
    # Clean energy / renewables — MUST come before generic "energy" so
    # "clean-energy" matches ICLN before substring "energy" hits XLE.
    (("clean-energy", "renewable", "solar", "wind-energy", "hydrogen"),
     "ICLN",
     False,
     ""),
    # Energy (fossil)
    (("oil", "natural-gas", "oilfield", "lng", "energy"),
     "XLE",
     False,
     ""),
    # Healthcare / pharma / biotech
    (("biotech", "biopharm", "pharma", "medical-device", "healthcare", "health-care"),
     "XLV",
     False,
     ""),
    # EV / autonomous / auto
    (("ev-", "electric-vehicle", "autonomous", "auto-",
      "automotive", "battery-cell"),
     "DRIV",
     False,
     ""),
    # Real estate / REITs
    (("reit", "real-estate", "data-center-reit"),
     "XLRE",
     False,
     ""),
    # Consumer discretionary / retail / luxury
    (("retail", "ecommerce", "luxury", "apparel", "restaurant"),
     "XLY",
     False,
     ""),
    # Consumer staples / food / beverages
    (("consumer-staples", "food-beverage", "tobacco", "household-product"),
     "XLP",
     False,
     ""),
    # Industrials / aerospace / defense
    (("aerospace", "defense", "machinery", "industrial", "rail-",
      "freight"),
     "XLI",
     False,
     ""),
    # Utilities
    (("utility", "utilities", "electric-grid", "power-utility"),
     "XLU",
     False,
     ""),
    # Communications / telecom / media
    (("telecom", "wireless-carrier", "broadband", "media",
      "streaming", "social-media", "advertising"),
     "XLC",
     False,
     ""),
    # Software / cloud / SaaS (broad tech sector since no dedicated SaaS ETF)
    (("software", "saas", "cloud-", "enterprise-software"),
     "XLK",
     False,
     ""),
    # AI broadly (not chip-specific) — also XLK
    (("ai-software", "generative-ai", "ai-platform"),
     "XLK",
     False,
     ""),
)


# Generic "tech-adjacent" fallback set — slugs that smell like tech but
# don't match the more specific patterns above. We default to XLK with
# an explicit proxy note.
_TECH_ADJACENT_HINTS = (
    "tech", "platform", "internet", "digital", "data", "ai", "machine",
    "saas", "software", "robot", "drone", "iot",
)


def map_slug_to_etf(slug: str) -> ETFChoice:
    """Return (etf, proxy_note) for an industry slug.

    Always returns a valid choice — SPY is the last-resort fallback.
    """
    if not slug:
        return ETFChoice("SPY", "empty slug → broad-market fallback")
    norm = slug.lower()

    for patterns, etf, is_proxy, note in _MAP:
        for pat in patterns:
            if pat in norm:
                return ETFChoice(etf, note if is_proxy else "")

    # Tech-adjacent fallback: XLK with explicit proxy note
    for hint in _TECH_ADJACENT_HINTS:
        if hint in norm:
            return ETFChoice(
                "XLK",
                f"slug {slug!r} has no dedicated sector ETF; using XLK "
                f"(broad tech) as thematic proxy.",
            )

    # Final fallback: SPY
    return ETFChoice(
        "SPY",
        f"slug {slug!r} has no obvious sector match; using SPY "
        f"(broad market) as a baseline. Regime signal is weak in this case.",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slug", required=True, help="Industry slug (e.g. ai-chips)")
    p.add_argument("--format", choices=("etf", "json"), default="json",
                   help="'etf' prints just the ticker; 'json' prints {etf, proxy_note}")
    args = p.parse_args(argv)

    choice = map_slug_to_etf(args.slug)
    if args.format == "etf":
        print(choice.etf)
    else:
        print(json.dumps({"etf": choice.etf, "proxy_note": choice.proxy_note}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
