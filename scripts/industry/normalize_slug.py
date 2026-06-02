"""Normalize raw industry input to (industry_name, slug).

Replaces the subagent-judgment step in the research-industry skill so the
slug formation is deterministic and pinnable to tests. Three input cases:

1. **Already-canonical slug** (`ai-chips`, `mid-cap-banks`) — echoed as-is
   with a Title-Cased display name derived from the slug.
2. **Plain ASCII English** (`AI Chips`, `Mid Cap Banks`, `Cybersecurity`) —
   mechanically slugified: lowercase, non-alnum → hyphen, collapse
   consecutive hyphens, strip leading/trailing hyphens.
3. **CJK / non-ASCII** — looked up in `_CJK_ALIASES` (small curated table
   below). Unknown CJK input is a fail-close (exit 2); the caller must
   either pre-translate to English or extend the alias table.

We deliberately don't pull in a transliteration dep (pypinyin etc.) —
stdlib-only is a hard project constraint, and pinyin slugs are not
recognizable English anyway.

Usage:
    python3 -m scripts.industry.normalize_slug --industry "AI Chips"
    python3 -m scripts.industry.normalize_slug --industry "AI芯片"
    python3 -m scripts.industry.normalize_slug --industry "MLCC"

Output (stdout, JSON):
    {"industry_name": "AI Chips", "slug": "ai-chips"}

Exit codes: 0 OK, 2 unknown CJK or invalid input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ASCII_PRINTABLE_RE = re.compile(r"^[\x20-\x7E]+$")


# CJK aliases. Add entries as new industries get researched. Each entry:
#   raw (zh-CN / zh-Hant common form) → (english_display, slug).
#
# Keep this table small — agent-driven normalization is acceptable when
# the user uses a long-form CJK description. The table is for the common
# / repeated cases where the slug should be stable across sessions.
_CJK_ALIASES: dict[str, tuple[str, str]] = {
    # Tech / semi
    "AI芯片": ("AI Chips", "ai-chips"),
    "人工智能芯片": ("AI Chips", "ai-chips"),
    "半导体": ("Semiconductors", "semiconductors"),
    "存储芯片": ("Memory Chips", "memory-chips"),
    "云计算": ("Cloud Computing", "cloud-computing"),
    "网络安全": ("Cybersecurity", "cybersecurity"),
    "信息安全": ("Cybersecurity", "cybersecurity"),
    # Financials
    "区域性银行": ("Regional Banks", "regional-banks"),
    "中型银行": ("Mid-Cap Banks", "mid-cap-banks"),
    "金融科技": ("Fintech", "fintech"),
    "保险": ("Insurance", "insurance"),
    # Energy / industrial
    "清洁能源": ("Clean Energy", "clean-energy"),
    "光伏": ("Solar", "solar"),
    "风电": ("Wind Energy", "wind-energy"),
    "油气": ("Oil and Gas", "oil-and-gas"),
    "石油": ("Oil and Gas", "oil-and-gas"),
    # Healthcare
    "生物科技": ("Biotech", "biotech"),
    "制药": ("Pharmaceuticals", "pharmaceuticals"),
    "医疗器械": ("Medical Devices", "medical-devices"),
    # Mobility
    "电动车": ("Electric Vehicles", "electric-vehicles"),
    "新能源车": ("Electric Vehicles", "electric-vehicles"),
    "自动驾驶": ("Autonomous Driving", "autonomous-driving"),
    # Other
    "数据中心": ("Data Centers", "data-centers"),
    "电池": ("Batteries", "batteries"),
    "被动元件": ("Passive Components", "passive-components"),
}


def _has_non_ascii(s: str) -> bool:
    return not _ASCII_PRINTABLE_RE.match(s)


def _slugify_ascii(s: str) -> str:
    """Lowercase + non-alnum→hyphen + collapse + strip. Returns "" if empty."""
    lowered = s.lower()
    # Replace any run of non-[a-z0-9] with a single hyphen
    slugged = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slugged.strip("-")


def _titlecase_from_slug(slug: str) -> str:
    """Reconstruct a display name from a slug.

    Special-cases short acronyms (≤4 chars containing no vowels by some
    heuristic) is not worth it — the user can edit the JSON if they care.
    """
    parts = slug.split("-")
    return " ".join(p.capitalize() if p else p for p in parts)


def normalize(industry: str) -> tuple[str, str]:
    """Return (industry_name, slug). Raises ValueError on un-normalizable input."""
    if not industry or not industry.strip():
        raise ValueError("empty industry input")
    s = industry.strip()

    # Case 1: already canonical slug
    if _SLUG_RE.match(s):
        return (_titlecase_from_slug(s), s)

    # Case 3: CJK or non-ASCII — try alias table
    if _has_non_ascii(s):
        alias = _CJK_ALIASES.get(s)
        if alias is None:
            raise ValueError(
                f"unknown non-ASCII industry input {s!r}. "
                f"Either pre-translate to English or add to _CJK_ALIASES in "
                f"scripts/industry/normalize_slug.py. "
                f"Known aliases: {sorted(_CJK_ALIASES.keys())[:10]}..."
            )
        return alias

    # Case 2: plain ASCII English — mechanical slugify
    slug = _slugify_ascii(s)
    if not slug:
        raise ValueError(f"input {s!r} produced empty slug after normalization")
    if not _SLUG_RE.match(slug):
        # Should be impossible given the regex used in _slugify_ascii, but
        # belt-and-suspenders.
        raise ValueError(
            f"slug {slug!r} (from input {s!r}) does not match {_SLUG_RE.pattern}"
        )

    # Display name: prefer the original casing if it's reasonable, else
    # titlecase the slug. Heuristic: if the original has mixed case or
    # contains acronyms, keep it; if it's all lowercase or all uppercase,
    # titlecase the slug.
    if s.isupper() or s.islower():
        display = _titlecase_from_slug(slug)
        # Preserve acronyms: if the slug part is ≤4 chars and all letters
        # in the original were uppercase, restore as-is. (Catches "MLCC".)
        if s.isupper() and " " not in s and len(s) <= 6:
            display = s
    else:
        display = s

    return (display, slug)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--industry", required=True,
                   help="Raw industry name (e.g. 'AI Chips', 'AI芯片', 'MLCC')")
    args = p.parse_args(argv)

    try:
        display, slug = normalize(args.industry)
    except ValueError as e:
        print(f"FATAL: normalize_slug failed: {e}", file=sys.stderr)
        return 2

    print(json.dumps({"industry_name": display, "slug": slug}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
