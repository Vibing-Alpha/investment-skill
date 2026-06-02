"""SEC EDGAR filing adapter.

Handles direct download of SEC filings, HTML-to-text conversion,
filing structure extraction, and revenue-note extraction.
Also provides the API /filings/items fallback.
"""

import re
import sys
import threading
import urllib.parse
from typing import Optional, Tuple
from urllib.parse import urlparse

from scripts.constants import BASE_URL, MIN_FILING_ITEM_CHARS
from scripts.sources.common import (
    http_get,
    safe_http_get_json,
    SEC_POLICY,
    SEC_FILING_POLICY,
    ResponseTooLargeError,
    HttpStatusError,
    HttpTransportError,
    RetryExhaustedError,
    SsrfBlockedError,
)

# DL2 migration (T12): module-top `make_request` (hoisted from inline
# at L769). DL1 v2.9 lesson — inline imports break mock.patch.
from scripts.sources.common import make_request

# DL2 imports (MODULE-TOP per DL1 v2.9):
from scripts.sources.adapter_result import (
    AdapterResult,
    AdapterError,
    ErrorCode,
    adapter_error_from_exception,
)


def _sanitize_url_for_detail(raw_url: str) -> str:
    """Strip query string + userinfo from URL before embedding in
    error.detail. ISS-047 (Loop3): caller-supplied filing_url could
    theoretically carry credentials/tokens; the detail blob lands on
    disk via category_statuses[*]["error_detail"] and may enter LLM
    context. Keep only scheme+host+path."""
    try:
        parsed = urlparse(raw_url)
        # Strip userinfo from netloc (username:password@host)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return f"{parsed.scheme}://{netloc}{parsed.path}"
    except (ValueError, TypeError):
        return "<malformed-url>"


def _replace_url_in_str(s: str, raw_url: str, safe_url: str) -> str:
    """Replace `raw_url` with `safe_url` in `s` to keep query/userinfo
    out of envelope.error.detail. Used by ISS-061 (Loop4) to clean
    DL1 transport exception messages that embed the URL via __str__.
    No-op if raw_url not present (defensive)."""
    if raw_url and raw_url in s and raw_url != safe_url:
        return s.replace(raw_url, safe_url)
    return s
from scripts.sources.api_shapes import validate_api_shape, SEC_SUBMISSIONS_SHAPE


# ---------------------------------------------------------------------------
# SSRF-safe redirect handling is now enforced by SEC_FILING_POLICY / SEC_POLICY
# via scripts.sources.common.http_get (allowed_host_suffixes=(".sec.gov",)).
# The legacy _SecOnlyRedirectHandler has been removed as part of DL1 migration.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

def strip_html_tags(html: str) -> str:
    """Convert SEC filing HTML to plain text.

    Handles <br>, <p>, <div>, <tr> as line breaks, skips <script>/<style>.
    """
    from html.parser import HTMLParser

    class _HTMLStripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.result = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ('script', 'style'):
                self._skip = True
            elif tag in ('br', 'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4'):
                self.result.append('\n')
            elif tag == 'td':
                self.result.append('\t')

        def handle_endtag(self, tag):
            if tag in ('script', 'style'):
                self._skip = False
            elif tag in ('p', 'div', 'tr', 'table', 'li', 'h1', 'h2', 'h3', 'h4'):
                self.result.append('\n')

        def handle_data(self, data):
            if not self._skip:
                self.result.append(data)

        def handle_entityref(self, name):
            if not self._skip:
                entities = {
                    'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"',
                    'apos': "'", 'nbsp': ' ',
                }
                self.result.append(entities.get(name, f'&{name};'))

        def handle_charref(self, name):
            if not self._skip:
                try:
                    if name.startswith('x'):
                        self.result.append(chr(int(name[1:], 16)))
                    else:
                        self.result.append(chr(int(name)))
                except (ValueError, OverflowError):
                    pass

    stripper = _HTMLStripper()
    stripper.feed(html)
    text = ''.join(stripper.result)

    # Clean up excessive whitespace while preserving line structure
    text = re.sub(r'[ \t]+', ' ', text)           # Collapse horizontal whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)         # Max 2 consecutive newlines
    text = re.sub(r'\n +', '\n', text)              # Remove leading spaces on lines
    return text.strip()


# ---------------------------------------------------------------------------
# Filing content formatting
# ---------------------------------------------------------------------------

def format_filing_content(content: str) -> str:
    """Format single-line filing content to multi-line for readability.

    Adds line breaks at sentence boundaries and section headers to enable
    line-number based navigation.
    """
    if not content:
        return content

    # 1. Add line breaks after sentences (period + 2+ spaces + capital letter)
    content = re.sub(r'\.\s{2,}([A-Z])', r'.\n\1', content)

    # 2. Add double line breaks before common section markers
    section_markers = [
        'Executive Summary', 'Results of Operations',
        'Liquidity and Capital Resources', 'Critical Accounting',
        'Business Environment', 'Significant Events',
        'Risk Factors', 'Known Trends', 'Market Dynamics',
        'Financial Condition', 'Overview', 'Segment Results',
        'Non-GAAP Financial Measures', 'Forward-Looking Statements',
        'Recent Developments', 'Business Strategy',
    ]
    for marker in section_markers:
        pattern = re.compile(
            rf'([.!?])\s+({re.escape(marker)})', re.IGNORECASE,
        )
        content = pattern.sub(rf'\1\n\n\2', content)

    # 3. Add line breaks after table rows (dollar amounts followed by spaces)
    content = re.sub(r'(\s*\$[\d,]+\s{3,})', r'\1\n', content)

    # 4. Add line breaks before bullet points
    content = re.sub(r'\s+(•|·|○|►)', r'\n\1', content)

    return content


# ---------------------------------------------------------------------------
# Filing structure extraction
# ---------------------------------------------------------------------------

def extract_filing_structure(content: str, formatted_content: str) -> dict:
    """Extract keywords count and section positions from filing content.

    Returns sections (with line numbers) and keyword counts for intelligent
    reading.  Does NOT generate text summary -- the analysis module uses
    Grep for targeted searches.
    """
    result = {
        "sections": [],
        "keywords": {},
    }

    if not content:
        return result

    # 1. Identify section positions (based on formatted content with line breaks)
    section_patterns = [
        'Executive Summary', 'Results of Operations',
        'Liquidity and Capital Resources', 'Critical Accounting',
        'Business Environment', 'Significant Events',
        'Known Trends', 'Risk Factors', 'Market Dynamics',
        'Financial Condition', 'Overview', 'Segment Results',
        'Non-GAAP Financial Measures', 'Recent Developments',
        'Business Strategy', 'Capital Expenditures',
    ]

    lines = formatted_content.split('\n')
    found_sections = set()
    content_lower_for_sections = content.lower()  # compute once, not per-match

    for i, line in enumerate(lines):
        line_lower = line.lower().strip()
        if len(line) > 200:
            continue
        for pat in section_patterns:
            pat_lower = pat.lower()
            if pat_lower in line_lower and pat_lower not in found_sections:
                char_pos = content_lower_for_sections.find(pat_lower)
                if char_pos >= 0:
                    result["sections"].append({
                        "title": pat,
                        "line": i + 1,
                        "char_pos": char_pos,
                    })
                    found_sections.add(pat_lower)
                break

    result["sections"] = sorted(
        result["sections"], key=lambda x: x["char_pos"],
    )[:15]

    # 2. Keyword counting
    content_lower = content.lower()
    keywords_to_count = {
        "guidance": ["guidance"],
        "outlook": ["outlook"],
        "risk": ["risk"],
        "capex": ["capex", "capital expenditure", "capital expenditures"],
        "strategy": ["strategy", "strategic"],
        "challenge": ["challenge", "challenges"],
        "growth": ["growth"],
        "revenue": ["revenue", "revenues"],
        "margin": ["margin", "margins"],
        "earnings": ["earnings"],
        "cash_flow": ["cash flow", "cash flows"],
        "liquidity": ["liquidity"],
        "debt": ["debt"],
        "acquisition": ["acquisition", "acquisitions"],
    }

    for key, terms in keywords_to_count.items():
        count = sum(content_lower.count(term) for term in terms)
        result["keywords"][key] = count

    return result


# ---------------------------------------------------------------------------
# Revenue note extraction
# ---------------------------------------------------------------------------

def extract_revenue_notes(content: str, filing_type: str = "10-K") -> str:
    """Extract revenue disaggregation notes from Financial Statements.

    Searches for Note Q / revenue disaggregation content:
    - Revenue by customer grouping
    - Revenue by geography
    - Customer concentration (>10%)

    Returns extracted content (typically 2-10KB) or empty string.
    """
    if not content or len(content) < 1000:
        return ""

    content_lower = content.lower()

    extraction_markers = [
        r'disaggregation\s+of\s+revenue',
        r'revenue\s+from\s+contracts\s+with\s+customers',
        r'(?:note|footnote)\s+\w+[\s\-–—.:]+revenue[s]?\s',
        r'\n\s*(?:\d+|[a-z]|[A-Z])[\s.)]+\s*Revenue[s]?\s*\n',
    ]

    start_pos = -1
    for pattern in extraction_markers:
        match = re.search(pattern, content_lower)
        if match:
            search_start = max(0, match.start() - 200)
            preceding = content[search_start:match.start()]
            note_header = re.search(
                r'(?:Note|FOOTNOTE)\s+\w+', preceding, re.IGNORECASE,
            )
            if note_header:
                start_pos = search_start + note_header.start()
            else:
                start_pos = match.start()
            break

    if start_pos < 0:
        revenue_table_patterns = [
            r'(?:civil|national\s+security|commercial|government).*?\$[\d,]+',
            r'(?:united\s+states|europe|asia|domestic|international).*?\$[\d,]+',
        ]
        for pattern in revenue_table_patterns:
            match = re.search(pattern, content_lower)
            if match:
                start_pos = max(0, match.start() - 500)
                break

    if start_pos < 0:
        return ""

    search_from = start_pos + 500
    end_patterns = [
        r'\n\s*(?:Note|FOOTNOTE)\s+\w+[\s\-–—.:]+(?!revenue)',
        r'\n\s*\d+\.\s+[A-Z][a-z]+\s+[A-Z]',
        r'\n\s*(?:SEGMENT|INCOME\s+TAX|STOCK|EQUITY|DEBT|LEASE|COMMITMENT)',
    ]

    end_pos = len(content)
    for pattern in end_patterns:
        match = re.search(pattern, content[search_from:], re.IGNORECASE)
        if match:
            candidate = search_from + match.start()
            if candidate < end_pos and candidate > start_pos + 1000:
                end_pos = candidate
                break

    max_extract = 15000
    if end_pos - start_pos > max_extract:
        end_pos = start_pos + max_extract

    extracted = content[start_pos:end_pos].strip()

    if len(extracted) < 200:
        return ""

    header = f"# Revenue Disaggregation Notes (Extracted from {filing_type})\n"
    header += f"# Extraction: chars {start_pos}-{end_pos} of {len(content)} total\n\n"

    return header + extracted


# ---------------------------------------------------------------------------
# SEC EDGAR direct download
# ---------------------------------------------------------------------------

def fetch_filing_from_sec_edgar(
    filing_url: str,
    ticker: str,
    filing_type: str,
) -> AdapterResult:
    """Download SEC filing HTML from EDGAR and extract required items.

    filing_type: "10-K", "10-Q", or "20-F"

    Returns: AdapterResult with data = {"items_metadata": ..., "content_dict": ...}
    on PASSED; .failed with mapped ErrorCode on failure.
    """
    src = "sec_edgar.fetch_filing_from_sec_edgar"
    try:
        items_metadata = {}
        content_dict = {}

        # ISS-195 (Loop28 cycle 1 fresh-session-15): type-guard
        # filing_url. Pre-fix `urlparse(non_str)` raises AttributeError
        # → INTERNAL_ERROR. fetch.py's _validate_filings_list only
        # validates that each filing item is a dict, not the field
        # types within. A drifted upstream `url: 123` reached this
        # adapter with the int directly. Treat non-str as missing
        # URL — same fail-closed path the empty-string branch uses.
        if filing_url is not None and not isinstance(filing_url, str):
            print(
                f"    SEC EDGAR: filing_url not str ({type(filing_url).__name__}); "
                f"treating as missing",
                file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=f"filing_url type {type(filing_url).__name__} (expected str)",
                source=src,
                retryable=False,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )

        if not filing_url:
            print(
                f"    SEC EDGAR: no {filing_type} URL available", file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"no {filing_type} URL available",
                source=src,
            )

        # Define items to extract based on filing type
        if filing_type == "10-K":
            items_spec = [
                ("item_1",
                 r'item\s+1\.?\s*[-–—.]?\s*(?:business|overview|general\s+develop)',
                 [r'item\s+1a\.?\s*[-–—.]?\s*(?:risk)', r'item\s+1b\.?\s*'],
                 "Item 1", "Business", False),
                ("item_1a",
                 r'item\s+1a\.?\s*[-–—.]?\s*(?:risk)',
                 [r'item\s+1b\.?\s*',
                  r'item\s+2\.?\s*[-–—.]?\s*(?:properties|selected|unresolved)'],
                 "Item 1A", "Risk Factors", False),
                ("item_7",
                 r'item\s+7\.?\s*[-–—.]?\s*(?:management|md&a|discussion)',
                 [r'item\s+7a\.?\s*',
                  r'item\s+8\.?\s*[-–—.]?\s*(?:financial|consolidated)'],
                 "Item 7", "Management Discussion & Analysis (MD&A)", False),
                ("item_8",
                 r'item\s+8\.?\s*[-–—.]?\s*(?:financial|consolidated)',
                 [r'item\s+9\.?\s*'],
                 "Item 8", "Financial Statements", True),
            ]
            prefix = "10k"
        elif filing_type == "10-Q":
            items_spec = [
                ("item_1",
                 r'item\s+1\.?\s*[-–—.]?\s*(?:financial\s+statements|condensed)',
                 [r'item\s+2\.?\s*[-–—.]?\s*(?:management|md&a|discussion)'],
                 "Item 1", "Financial Statements", True),
                ("item_2",
                 r'item\s+2\.?\s*[-–—.]?\s*(?:management|md&a|discussion)',
                 [r'item\s+3\.?\s*[-–—.]?\s*(?:quantitative|quant\.|controls)',
                  r'part\s+ii\b'],
                 "Item 2", "Management Discussion & Analysis (MD&A)", False),
            ]
            prefix = "10q"
        elif filing_type == "20-F":
            items_spec = [
                ("item_3",
                 r'item\s+3\.?\s*[-–—.]?\s*(?:key\s+information|selected|risk\s+factors)',
                 [r'item\s+4\.?\s*[-–—.]?\s*(?:information\s+on|history|business)'],
                 "Item 3", "Key Information (Risk Factors)", False),
                ("item_4",
                 r'item\s+4\.?\s*[-–—.]?\s*(?:information\s+on|history|business)',
                 [r'item\s+5\.?\s*[-–—.]?\s*(?:operating|financial\s+review)'],
                 "Item 4", "Information on the Company", False),
                ("item_5",
                 r'item\s+5\.?\s*[-–—.]?\s*(?:operating|financial\s+review)',
                 [r'item\s+6\.?\s*[-–—.]?\s*(?:directors|senior\s+management)'],
                 "Item 5", "Operating and Financial Review", False),
            ]
            prefix = "20f"
        else:
            print(
                f"    SEC EDGAR: unsupported filing type {filing_type}",
                file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"unsupported filing type {filing_type}",
                source=src,
            )

        # ISS-070 (Loop5): sanitize URL in stderr too — pre-fix raw URL
        # leaked userinfo/query/token into logs. Envelope detail was
        # already sanitized; bring stderr in line.
        print(
            f"    SEC EDGAR: downloading {filing_type} from "
            f"{_sanitize_url_for_detail(filing_url)}",
            file=sys.stderr,
        )

        # SSRF guard: only allow HTTPS URLs to sec.gov domains.
        # ISS-013 fix: failure paths return FAILED (not PASSED-empty) so DL2
        # observability shows the true cause. Consumer (`fetch.py:_fetch_filing_data_impl`)
        # branches on `data.get("items_metadata")` truthiness, so empty data
        # in FAILED still falls through to API fallback chain unchanged.
        parsed_url = urlparse(filing_url)
        hostname = (parsed_url.hostname or "").lower()
        if parsed_url.scheme != "https" or not (
            hostname == "sec.gov"
            or hostname == "www.sec.gov"
            or hostname.endswith(".sec.gov")
        ):
            print(
                f"    SEC EDGAR: rejected non-SEC URL: {hostname}",
                file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.SSRF_BLOCKED,
                detail=f"non-SEC URL rejected: scheme={parsed_url.scheme} host={hostname}",
                source=src,
                retryable=False,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )

        # Step 1: Download HTML via unified http_get.
        # H3 FIX: http_get's policy-level retry ALREADY handles 429/5xx transparently.
        # The original outer `for attempt in range(max_download_retries)` loop becomes
        # redundant after migration — http_get.policy.max_retries=3 (SEC_FILING_POLICY
        # inherits DEFAULT_POLICY.retry_on). Remove the outer retry loop; http_get
        # raises RetryExhaustedError after all retries exhausted.
        # SEC_FILING_POLICY enforces the 50MB cap + sec.gov suffix allowlist.
        # ISS-061 (Loop4): exception detail must also strip URL userinfo/
        # query/fragment. DL1 transport exceptions (`HttpTransportError`/
        # `RetryExhaustedError`/`ResponseTooLargeError`) embed the URL in
        # __str__. Pre-fix `str(e)[:400]` lands raw URL into envelope.detail,
        # bypassing the HTTP-status branches' `_sanitize_url_for_detail`
        # treatment. Use `_replace_url_in_str` to find any URL substring
        # in str(e) and replace with sanitized form.
        _safe_filing_url = _sanitize_url_for_detail(filing_url)
        try:
            headers = {"User-Agent": _SEC_UA, "Accept": "text/html"}
            resp = http_get(filing_url, policy=SEC_FILING_POLICY, headers=headers)
        except ResponseTooLargeError as e:
            print("    SEC EDGAR: filing HTML too large, skipping", file=sys.stderr)
            return AdapterResult.failed(
                code=ErrorCode.RESPONSE_TOO_LARGE,
                detail=_replace_url_in_str(str(e)[:400], filing_url, _safe_filing_url),
                source=src,
                retryable=False,
                cause="ResponseTooLargeError",
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )
        except SsrfBlockedError as e:
            # H4 fix: the old _SecOnlyRedirectHandler raised URLError for
            # non-sec.gov redirects, which the outer retry loop caught. The new
            # allowed_host_suffixes check raises SsrfBlockedError instead.
            # ISS-070: replace any raw URL in `e`'s str with sanitized form
            # so stderr does not carry query/userinfo.
            print(
                f"    SEC EDGAR: SSRF/domain-allowlist violation "
                f"({_replace_url_in_str(str(e), filing_url, _safe_filing_url)}), skipping",
                file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.SSRF_BLOCKED,
                detail=_replace_url_in_str(str(e)[:400], filing_url, _safe_filing_url),
                source=src,
                retryable=False,
                cause="SsrfBlockedError",
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )
        except RetryExhaustedError as e:
            # 5xx/429 exhausted after policy retries.
            # ISS-220 SF-B (Loop32 cycle 2): hand-roll classification was
            # bypassing the canonical mapper's SEC-host 403→RATE_LIMIT
            # routing (ISS-215). Route through `adapter_error_from_exception`
            # so SEC throttle (403 retry-exhausted) surfaces as RATE_LIMIT
            # instead of HTTP_TRANSPORT. Mapper now accepts optional
            # `data=` to preserve partial-recovery dict.
            print(
                f"    SEC EDGAR: HTTP {e.status} after {e.attempts} retries, skipping",
                file=sys.stderr,
            )
            scrubbed = _replace_url_in_str(str(e)[:400], filing_url, _safe_filing_url)
            # Use canonical mapper but rewrite detail (URL-scrubbed) since
            # the mapper would not run our SEC URL scrubber.
            envelope = adapter_error_from_exception(
                e, source=src,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )
            return AdapterResult.failed_from_child(
                envelope.error,
                source=src,
                detail=scrubbed,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )
        except HttpTransportError as e:
            # ISS-070: same — sanitize URL in stderr.
            print(
                f"    SEC EDGAR: transport error "
                f"({_replace_url_in_str(str(e), filing_url, _safe_filing_url)}), skipping",
                file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.HTTP_TRANSPORT,
                detail=_replace_url_in_str(str(e)[:400], filing_url, _safe_filing_url),
                source=src,
                retryable=True,
                cause=type(e).__name__,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )

        if resp.status == 404:
            print(
                "    SEC EDGAR: 404 Not Found (permanent), skipping",
                file=sys.stderr,
            )
            # ISS-047 (Loop3): sanitize filing_url before embedding in
            # error.detail. Caller-supplied URL may contain query/userinfo
            # (theoretical, since SSRF guard limits to sec.gov, but
            # defense-in-depth) — strip to scheme+host+path only so the
            # detail blob (which lands on disk via category_statuses
            # error_detail and may enter LLM context) doesn't carry tokens.
            _safe_url = _sanitize_url_for_detail(filing_url)
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"HTTP 404 on {_safe_url}",
                source=src,
                retryable=False,
                upstream_status=404,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )
        if resp.status >= 400:
            # Non-retryable 4xx (401/402/403) — previously raised HTTPError, now
            # returned in envelope.
            print(
                f"    SEC EDGAR: HTTP {resp.status}, skipping",
                file=sys.stderr,
            )
            if resp.status in (401, 402, 403):
                code = ErrorCode.UNAUTHORIZED
            elif resp.status == 429:
                code = ErrorCode.RATE_LIMIT
            elif 500 <= resp.status < 600:
                code = ErrorCode.UPSTREAM_ERROR
            else:
                code = ErrorCode.HTTP_STATUS
            _safe_url = _sanitize_url_for_detail(filing_url)
            return AdapterResult.failed(
                code=code,
                detail=f"HTTP {resp.status} on {_safe_url}",
                source=src,
                retryable=(500 <= resp.status < 600 or resp.status == 429),
                upstream_status=resp.status,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )

        html = resp.body.decode("utf-8", errors="replace")
        print(
            f"    SEC EDGAR: downloaded {len(html):,} chars HTML",
            file=sys.stderr,
        )

        # Step 2: Parse HTML to plain text
        try:
            plain_text = strip_html_tags(html)
            print(
                f"    SEC EDGAR: {len(plain_text):,} chars plain text",
                file=sys.stderr,
            )

            if len(plain_text) < 5000:
                # ISS-016 fix: text-too-short was a silent-success path.
                # Surface as FAILED + NOT_FOUND so observability shows the
                # parse outcome; consumer's content-driven fallback chain
                # still fires on empty items_metadata.
                print("    SEC EDGAR: text too short, skipping", file=sys.stderr)
                return AdapterResult.failed(
                    code=ErrorCode.NOT_FOUND,
                    detail=f"plain text too short ({len(plain_text)} < 5000) for {filing_type}",
                    source=src,
                    retryable=False,
                    data={"items_metadata": items_metadata, "content_dict": content_dict},
                )

            # Step 3: Extract each item
            for (
                item_key, start_pat, end_pats, number_str, display_name,
                revenue_only,
            ) in items_spec:
                # ISS-220 Sec-2 (Loop35 cycle 1): list materialization is
                # bounded by SEC_FILING_POLICY's 50 MiB byte cap (filing
                # text already capped before reaching here) + ReDoS-safe
                # patterns (literal item-N anchors, no nested unbounded
                # quantifiers) + sec.gov host pin (vendor trusted).
                # Performance optimization to streaming finditer is
                # backlog item, not a security boundary repair.
                # fail-open-ok: SEC trusted source + ReDoS-safe regex + byte cap; backlog perf optimization
                start_matches = list(
                    re.finditer(
                        r'\n\s*' + start_pat, plain_text, re.IGNORECASE,
                    )
                )
                item_start = None
                for match in reversed(start_matches):
                    after = plain_text[match.end():match.end() + 500]
                    if len(after.strip()) > 200:
                        item_start = match.start()
                        break

                if item_start is None:
                    num_part = item_key.replace("item_", "").replace("_", "")
                    broad_matches = list(
                        re.finditer(
                            r'\n\s*item\s+' + re.escape(num_part) + r'\.',
                            plain_text,
                            re.IGNORECASE,
                        )
                    )
                    for match in reversed(broad_matches):
                        after = plain_text[match.end():match.end() + 500]
                        if len(after.strip()) > 200:
                            item_start = match.start()
                            break

                if item_start is None:
                    print(
                        f"    ⚠ {filing_type} {number_str} ({display_name}): "
                        f"boundary not found",
                        file=sys.stderr,
                    )
                    continue

                # Find end position
                item_end = None
                for end_pat in end_pats:
                    end_matches = list(
                        re.finditer(
                            r'\n\s*' + end_pat, plain_text, re.IGNORECASE,
                        )
                    )
                    for match in end_matches:
                        if match.start() > item_start + 1000:
                            item_end = match.start()
                            break
                    if item_end is not None:
                        break

                if item_end is None:
                    item_end = min(item_start + 100000, len(plain_text))

                raw_content = plain_text[item_start:item_end].strip()

                if len(raw_content) < MIN_FILING_ITEM_CHARS:
                    print(
                        f"    ⚠ {filing_type} {number_str} ({display_name}): "
                        f"too short ({len(raw_content):,} chars)",
                        file=sys.stderr,
                    )
                    continue

                if revenue_only:
                    revenue_notes = extract_revenue_notes(raw_content, filing_type)
                    if revenue_notes:
                        content_dict[f"{prefix}_revenue_notes"] = revenue_notes
                        items_metadata["revenue_notes"] = {
                            "number": number_str,
                            "name": (
                                f"Revenue Disaggregation "
                                f"(extracted from {number_str})"
                            ),
                            "total_chars": len(revenue_notes),
                            "source_item": item_key,
                            "source_total_chars": len(raw_content),
                            "source": "sec_edgar_direct",
                        }
                        print(
                            f"    ✓ {filing_type} revenue notes: "
                            f"{len(revenue_notes):,} chars (from {number_str})",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"    ⚠ {filing_type} {number_str}: no revenue "
                            f"notes in {len(raw_content):,} chars",
                            file=sys.stderr,
                        )
                else:
                    formatted = format_filing_content(raw_content)
                    structure = extract_filing_structure(raw_content, formatted)

                    items_metadata[item_key] = {
                        "number": number_str,
                        "name": display_name,
                        "total_chars": len(formatted),
                        "sections": structure["sections"],
                        "keywords": structure["keywords"],
                        "source": "sec_edgar_direct",
                    }
                    content_dict[f"{prefix}_{item_key}"] = formatted
                    print(
                        f"    ✓ {filing_type} {number_str} ({display_name}): "
                        f"{len(formatted):,} chars",
                        file=sys.stderr,
                    )

        except Exception as e:
            # ISS-016 fix: extraction errors (regex / parsing failures inside
            # the items_spec loop) were silently swallowed and rolled into a
            # PASSED-empty return. Now route through the canonical mapper so
            # parse-error-class causes (ValueError, JSONDecodeError) become
            # PARSE_ERROR and other exceptions get their proper code.
            # ISS-220 4.35 (Loop38 cycle 1, iter7): preserve partial
            # already-extracted items via mapper data= param (added by
            # loop32 SF-A). Pre-fix the FAILED return discarded all
            # items extracted by earlier iterations of the items_spec
            # loop. Now downstream consumers can salvage what succeeded.
            print(f"    SEC EDGAR extraction error: {e}", file=sys.stderr)
            return adapter_error_from_exception(
                e, source=src,
                data={
                    "items_metadata": items_metadata,
                    "content_dict": content_dict,
                },
            )

        # ISS-016 fix: post-extraction empty result was silent-success even
        # when the http+parse pipeline ran clean. If items_metadata stayed
        # empty, no items matched their boundary patterns — surface as
        # NOT_FOUND so observability is honest.
        if not items_metadata:
            print(
                "    SEC EDGAR: no items extracted from filing",
                file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"no items_spec keys matched in {filing_type} filing content",
                source=src,
                retryable=False,
                data={"items_metadata": items_metadata, "content_dict": content_dict},
            )
        return AdapterResult.passed(
            data={"items_metadata": items_metadata, "content_dict": content_dict}
        )

    except Exception as e:
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# SEC EDGAR submissions API — direct CIK-based filing lookup
# ---------------------------------------------------------------------------

_SEC_UA = "StockAnalysis/1.0 (research@example.com)"

# Module-level cache: avoid re-downloading company_tickers.json (~2MB)
# on consecutive calls (e.g., 10-K then 10-Q for the same ticker).
# ISS-101 (Loop8): guard with a lock so concurrent callers can't trigger
# duplicate ~2MB downloads or observe a half-populated dict mid-update.
_cik_cache: dict = {}
# ISS-189 (Loop26) + ISS-199 (Loop28): true single-flight load.
# Use a Condition (not a bare Lock) so concurrent first misses block
# on a wait-for-notify instead of all racing to the HTTP fetch.
# Pre-fix history:
#   ISS-189 attempted single-flight but only held the lock across the
#   decision-check, releasing it BEFORE the HTTP fetch — concurrent
#   callers all saw "miss + not loaded" + ran their own download.
#   ISS-199 finishes the fix: leader thread sets `_cik_cache_loading
#   =True` under the lock and runs the fetch outside; followers see
#   loading=True and `cv.wait()` until the leader notifies. After
#   load, `_cik_cache_loaded=True` lets absent-ticker lookups return
#   immediately without refetch (the original ISS-189 win).
_cik_cache_cv = threading.Condition()
_cik_cache_lock = _cik_cache_cv  # back-compat alias for tests
_cik_cache_loaded: bool = False
_cik_cache_loading: bool = False


def _resolve_cik(ticker: str) -> Tuple[str, Optional[AdapterError]]:
    """Resolve ticker → CIK via SEC company_tickers.json (cached).

    Returns: (cik_str, error). On success: (cik, None). On the ticker
    being legitimately absent from a valid SEC ticker map: ("", None) —
    caller maps that to NOT_FOUND. On HTTP / parse / shape failures:
    ("", AdapterError) — caller surfaces the typed cause.

    ISS-143 (Loop14 cycle 1 fresh-session): pre-fix returned bare ""
    on every failure path (HTTP 429, JSON parse error, shape drift,
    missing ticker). The caller (`lookup_filing_via_sec_submissions`)
    then mapped any "" to NOT_FOUND, collapsing rate-limit / unauth
    / upstream-error / shape-drift signals into "ticker not found in
    SEC tickers". Operators saw NOT_FOUND and would conclude the
    ticker doesn't exist on SEC — wrong diagnosis, hidden outage.
    """
    import json as _json
    global _cik_cache_loaded, _cik_cache_loading

    ticker_upper = ticker.upper()
    src = "sec_edgar._resolve_cik"
    # ISS-199 (Loop28 cycle 1 fresh-session-15): true single-flight.
    # Decide leader/follower under the Condition; leader runs the
    # fetch OUTSIDE the lock; followers wait on `cv.wait()` until
    # the leader notifies. Mirrors `concurrent.futures._SingletonExecutor`
    # idiom. ISS-189's prior fix held the lock only across the
    # decision-check, so concurrent first misses all hit `not loaded`
    # before any of them set the leadership sentinel.
    is_leader = False
    with _cik_cache_cv:
        # Wait while another thread is actively loading. After it
        # signals completion, fall through to re-check the cache.
        while _cik_cache_loading and not _cik_cache_loaded:
            _cik_cache_cv.wait()
        if ticker_upper in _cik_cache:
            return _cik_cache[ticker_upper], None
        if _cik_cache_loaded:
            # Map loaded, ticker absent — genuine NOT_FOUND.
            return "", None
        # Become the leader thread for this load.
        _cik_cache_loading = True
        is_leader = True

    # ISS-220 Loop37 Sec-4: finally-block guards against BaseException
    # (KeyboardInterrupt, SystemExit, etc.) leaving the singleflight
    # sentinel set, which would deadlock subsequent waiters indefinitely.
    # Inner cleanup paths still set _cik_cache_loading=False explicitly
    # for the success / shape-drift / Exception cases (so notify_all
    # fires with the correct lock-held semantics); the finally is a
    # belt-and-suspenders for KeyboardInterrupt mid-fetch.
    try:
        # Structural (post Loop21): safe_http_get_json centralizes
        # status check + JSON parse + typed-exception propagation.
        # Pre-Loop21 this site had explicit `raise HttpStatusError`
        # branch (C3-H1 from Cycle 3); now consolidated.
        tickers_data = safe_http_get_json(
            "https://www.sec.gov/files/company_tickers.json",
            policy=SEC_POLICY,
            headers={"User-Agent": _SEC_UA},
        )
        # ISS-077 (Loop6): wire SEC_TICKER_MAP_SHAPE here. Pre-fix the
        # shape was declared but unused — `_resolve_cik` would crash
        # with KeyError on `entry["cik_str"]` if upstream renamed the
        # field, mapping to INTERNAL_ERROR instead of SHAPE_MISMATCH.
        # Now: validate the parsed dict against the shape and skip
        # entries that don't conform. Only safe to assume `cik_str`
        # presence after validation passes.
        from scripts.sources.api_shapes import (
            validate_api_shape, SEC_TICKER_MAP_SHAPE,
        )
        v = validate_api_shape(tickers_data, SEC_TICKER_MAP_SHAPE)
        if not v.ok:
            err_detail = (
                f"SEC company_tickers.json shape drift; "
                f"first error: {v.errors[0] if v.errors else '?'}"
            )
            print(f"    SEC EDGAR submissions: {err_detail}", file=sys.stderr)
            # ISS-199 (Loop28): release leadership before early-return so
            # waiters wake and re-evaluate (will hit "not loaded + not
            # loading" → become next leader).
            with _cik_cache_cv:
                _cik_cache_loading = False
                _cik_cache_cv.notify_all()
            return "", AdapterError(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=err_detail,
                source=src,
                retryable=False,
            )
        with _cik_cache_cv:
            for entry in tickers_data.values():
                t = entry.get("ticker", "").upper()
                # Cache all tickers from the response
                _cik_cache[t] = str(entry["cik_str"]).zfill(10)
            # ISS-189 (Loop26): mark map loaded so subsequent absent-
            # ticker lookups return immediately without refetch.
            _cik_cache_loaded = True
            # ISS-199 (Loop28): release leader status + wake all waiters.
            _cik_cache_loading = False
            _cik_cache_cv.notify_all()
    except Exception as exc:
        # ISS-143 (Loop14): map exception to typed AdapterError so caller
        # can preserve cause classification.
        # ISS-199 (Loop28): on error path, also release leadership so
        # waiters can either retry (next call) or fall through. Don't
        # set _cik_cache_loaded=True — the load failed.
        print(
            f"    SEC EDGAR submissions: ticker lookup failed: {exc}",
            file=sys.stderr,
        )
        ar = adapter_error_from_exception(exc, source=src)
        with _cik_cache_cv:
            _cik_cache_loading = False
            _cik_cache_cv.notify_all()
        return "", ar.error
    finally:
        # ISS-220 Loop37 Sec-4: backstop for BaseException paths
        # (KeyboardInterrupt / SystemExit / GeneratorExit) where the
        # explicit cleanup in the success/shape-drift/Exception
        # branches above doesn't run. Without this, a Ctrl-C during
        # the fetch would leave _cik_cache_loading=True and any
        # subsequent caller blocks indefinitely waiting for the
        # never-arriving notify. Idempotent — safe to also clear when
        # a normal-exit branch already cleared.
        if is_leader:
            with _cik_cache_cv:
                if _cik_cache_loading:
                    _cik_cache_loading = False
                    _cik_cache_cv.notify_all()

    with _cik_cache_cv:
        return _cik_cache.get(ticker_upper, ""), None


def lookup_filing_via_sec_submissions(
    ticker: str,
    filing_type: str = "10-K",
) -> AdapterResult:
    """Find the latest filing of a given type directly from SEC EDGAR.

    Uses data.sec.gov/submissions/CIK*.json which maps company tickers
    to CIK numbers and lists all recent filings. This bypasses third-party
    APIs (Financial Datasets, FMP) that may have coverage gaps for
    recently spun-off or newly listed companies.

    Returns: AdapterResult. On success, data contains a dict compatible
    with ``_fetch_filing_data_impl`` expectations:
        {"report_date": ..., "url": ..., "accession_number": ...,
         "filing_type": ..., "filing_date": ...}
    On failure, returns AdapterResult.failed with NOT_FOUND.
    """
    import json as _json

    src = "sec_edgar.lookup_filing_via_sec_submissions"
    try:
        # ISS-143 (Loop14): _resolve_cik returns (cik, error). When error
        # is set, surface the typed cause (RATE_LIMIT/UNAUTHORIZED/
        # SHAPE_MISMATCH/UPSTREAM_ERROR) instead of collapsing all
        # failures into NOT_FOUND. NOT_FOUND is reserved for "the
        # SEC ticker map loaded successfully but the requested ticker
        # is genuinely absent."
        cik, resolve_error = _resolve_cik(ticker)
        if resolve_error is not None:
            # ISS-216 (Loop31) + ISS-220 (Loop32 SF-A): preserve full
            # child AdapterError metadata via canonical helper.
            return AdapterResult.failed_from_child(
                resolve_error,
                source=src,
                detail=(
                    f"CIK lookup failed for {ticker}: "
                    f"{resolve_error.detail}"
                ),
            )
        if not cik:
            print(
                f"    SEC EDGAR submissions: ticker {ticker} not found in SEC tickers",
                file=sys.stderr,
            )
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"ticker {ticker} not found in SEC tickers",
                source=src,
            )

        submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            resp = http_get(
                submissions_url,
                policy=SEC_POLICY,
                headers={"User-Agent": _SEC_UA},
            )
        except Exception as exc:
            print(
                f"    SEC EDGAR submissions: CIK {cik} fetch failed: {exc}",
                file=sys.stderr,
            )
            raise

        # Explicit 404 → NOT_FOUND mapping (per spec contract test).
        if resp.status == 404:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"HTTP 404 SEC submissions for CIK {cik}",
                source=src,
                upstream_status=404,
            )

        # C3-H1 (Codex Cycle 3): check status before JSON parse so an HTTP
        # error surfaces as "HTTP 4xx/5xx" in the log rather than as
        # JSONDecodeError hiding the actual fetch failure.
        # ISS-066 (Loop4): per-status canonical mapping — match the same
        # 4xx/5xx → ErrorCode dispatch used by HttpStatusError row in
        # adapter_error_from_exception. Pre-fix mapped EVERYTHING to
        # UPSTREAM_ERROR/non-retryable, hiding 429 RATE_LIMIT (retryable)
        # and 401/403 UNAUTHORIZED. Mirror the dispatch table directly.
        if resp.status >= 400:
            msg = f"HTTP {resp.status} fetching SEC submissions for CIK {cik}"
            print(
                f"    SEC EDGAR submissions: CIK {cik} fetch failed: {msg}",
                file=sys.stderr,
            )
            if resp.status in (401, 402, 403):
                code = ErrorCode.UNAUTHORIZED
                retryable = False
            elif resp.status == 429:
                code = ErrorCode.RATE_LIMIT
                retryable = True
            elif 500 <= resp.status < 600:
                code = ErrorCode.UPSTREAM_ERROR
                retryable = True
            else:
                code = ErrorCode.HTTP_STATUS
                retryable = False
            return AdapterResult.failed(
                code=code,
                detail=msg,
                source=src,
                upstream_status=resp.status,
                retryable=retryable,
            )
        data = _json.loads(resp.body)
        # ISS-220 Loop37 Sec-2: this path manually parses (custom 404 +
        # per-status dispatch) and previously bypassed
        # safe_http_get_json's post-parse JSON object-size cap.
        # Apply the same cap here for parity with the canonical path.
        from scripts.sources.common import _validate_json_object_size
        _json_cap = (SEC_POLICY.max_decompressed_bytes or 64 * 1024 * 1024) * 4
        _validate_json_object_size(data, max_bytes=_json_cap, url=submissions_url)

        # v24 (T12+T13 review I1): validate raw SEC submissions shape —
        # the plan explicitly required `validate_api_shape(...)` with
        # SEC_SUBMISSIONS_SHAPE (T12 Step 1 bullet 3) but initial
        # implementation imported the symbols without wiring the call.
        # Protects against SEC JSON shape drift (e.g., cik/name renames,
        # filings.recent layout change) by surfacing SHAPE_MISMATCH at
        # the adapter boundary instead of silent .get()-fallback rot.
        v = validate_api_shape(data, SEC_SUBMISSIONS_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)

        # Step 3: Find the latest filing of the requested type
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        report_dates = recent.get("reportDate", [])

        for i, form in enumerate(forms):
            if form == filing_type:
                acc = accessions[i]
                acc_path = acc.replace("-", "")
                doc = primary_docs[i]
                # ISS-046 (Loop3): validate `acc` against SEC accession
                # number regex (10-2-6 digit form NNNNNNNNNN-NN-NNNNNN)
                # and quote `doc` as a single path segment so a malformed
                # upstream `primaryDocument` containing `?`, `#`, `..`, `/`
                # cannot inject path traversal or query into the SEC
                # Archives URL.
                if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc):
                    print(
                        f"    SEC EDGAR submissions: malformed accession "
                        f"{acc!r}; skipping",
                        file=sys.stderr,
                    )
                    continue
                # `safe=''` rejects `/?#`. Reject ".." early to be explicit
                # about path traversal intent.
                if ".." in doc or "/" in doc or "\\" in doc:
                    print(
                        f"    SEC EDGAR submissions: suspicious "
                        f"primaryDocument {doc!r}; skipping",
                        file=sys.stderr,
                    )
                    continue
                safe_doc = urllib.parse.quote(doc, safe='')
                url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik.lstrip('0')}/{acc_path}/{safe_doc}"
                )
                rd_raw = report_dates[i] if i < len(report_dates) else ""
                rd = rd_raw if rd_raw else None
                filing_date = dates[i] if i < len(dates) else ""
                if rd is None:
                    # ISS-026 (Cycle 4 backlog): pre-fix returned PASSED with
                    # `data["status"]="missing_report_date"` sentinel — `.ok`
                    # consumers wrongly treated this as success. Now PARTIAL +
                    # NOT_FOUND so envelope semantics match: data is partly
                    # there (url/accession/filing_date) but the report_date
                    # is missing. Sentinel kept in data for back-compat with
                    # consumer fail-closed path (fetch.py:395-410 reads
                    # `filing_20f.get("status") == "missing_report_date"`).
                    print(
                        f"    SEC EDGAR submissions: {filing_type} "
                        f"missing reportDate (CIK {cik}, acc {acc}); "
                        f"fail-closed",
                        file=sys.stderr,
                    )
                    return AdapterResult.partial(
                        data={
                            "report_date": None,
                            "url": url,
                            "accession_number": acc,
                            "filing_type": filing_type,
                            "filing_date": filing_date,
                            "status": "missing_report_date",
                        },
                        error=AdapterError(
                            code=ErrorCode.NOT_FOUND,
                            detail=(
                                f"upstream submissions response lacks "
                                f"reportDate for {filing_type} "
                                f"(CIK {cik}, accession {acc})"
                            ),
                            source="sec_edgar.lookup_filing_via_sec_submissions",
                            retryable=False,
                        ),
                    )
                result = {
                    "report_date": rd,
                    "url": url,
                    "accession_number": acc,
                    "filing_type": filing_type,
                    "filing_date": filing_date,
                    "status": "ok",
                }
                print(
                    f"    SEC EDGAR submissions: found {filing_type} "
                    f"report_date={result['report_date']} "
                    f"(CIK {cik})",
                    file=sys.stderr,
                )
                return AdapterResult.passed(data=result)

        print(
            f"    SEC EDGAR submissions: no {filing_type} found for CIK {cik}",
            file=sys.stderr,
        )
        return AdapterResult.failed(
            code=ErrorCode.NOT_FOUND,
            detail=f"no {filing_type} found for CIK {cik}",
            source=src,
        )

    except Exception as e:
        return adapter_error_from_exception(e, source=src)


# ---------------------------------------------------------------------------
# API /filings/items fallback
# ---------------------------------------------------------------------------

def fetch_filing_items_from_api(
    ticker: str,
    filing_type: str,
    year: int,
    quarter: int = None,
) -> AdapterResult:
    """Fallback: fetch filing items from Financial Datasets API /filings/items.

    Used when SEC EDGAR direct download fails.

    Returns: AdapterResult with data = {"items_metadata": ..., "content_dict": ...}
    in the same shape as ``fetch_filing_from_sec_edgar()``.
    """
    src = "sec_edgar.fetch_filing_items_from_api"
    try:
        items_metadata = {}
        content_dict = {}

        if filing_type == "10-K":
            requested_items = {
                "Item 1": ("item_1", "Business", False),
                "Item 1A": ("item_1a", "Risk Factors", False),
                "Item 7": (
                    "item_7", "Management Discussion & Analysis (MD&A)", False,
                ),
                "Item 8": ("item_8", "Financial Statements", True),
            }
            prefix = "10k"
        elif filing_type == "10-Q":
            requested_items = {
                "Item 1": ("item_1", "Financial Statements", True),
                "Item 2": (
                    "item_2", "Management Discussion & Analysis (MD&A)", False,
                ),
            }
            prefix = "10q"
        else:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"unsupported filing type {filing_type}",
                source=src,
            )

        # ISS-027: urlencode all params including filing_type/year/quarter.
        params = {"ticker": ticker, "filing_type": filing_type, "year": year}
        if quarter and filing_type == "10-Q":
            params["quarter"] = quarter
        url = f"{BASE_URL}/filings/items?" + urllib.parse.urlencode(params)

        print(
            f"    API fallback: fetching {filing_type} items from API...",
            file=sys.stderr,
        )

        try:
            response = make_request(url)
            # ISS-084 (Loop6 backlog): validate shape before consumer
            # iteration. Pre-fix `{"items": null}` or `{"items": [non-dict]}`
            # would crash downstream `api_item.get(...)` and map to
            # INTERNAL_ERROR. Now: SHAPE_MISMATCH at the boundary.
            from scripts.sources.api_shapes import FD_FILINGS_ITEMS_SHAPE
            shape_v = validate_api_shape(response, FD_FILINGS_ITEMS_SHAPE)
            if not shape_v.ok:
                return AdapterResult.failed_from_shape(shape_v, source=src)
            api_items = response["items"]

            if not api_items:
                print("    API fallback: no items returned", file=sys.stderr)
                return AdapterResult.failed(
                    code=ErrorCode.NOT_FOUND,
                    detail=f"API filings/items returned empty for {ticker}/{filing_type}/{year}",
                    source=src,
                    retryable=False,
                    data={"items_metadata": items_metadata, "content_dict": content_dict},
                )

            for api_item in api_items:
                raw_item_number = str(api_item.get("number", "") or "")
                item_number = re.sub(r"\s+", " ", raw_item_number).strip().rstrip(".")
                # ISS-196 (Loop28 cycle 1 fresh-session-15): text/name are
                # Optional_(str) in FD_FILINGS_ITEMS_SHAPE; `.get(k, "")`
                # returns the default ONLY when key is absent. Present-with-
                # None returns None, then `len(None)` → TypeError →
                # INTERNAL_ERROR. Same root pattern as ISS-159/162/193 —
                # normalize via isinstance guard before slice/len.
                _raw_text = api_item.get("text")
                item_text = _raw_text if isinstance(_raw_text, str) else ""
                _raw_name = api_item.get("name") or api_item.get("title")
                item_name = _raw_name if isinstance(_raw_name, str) else ""

                if item_number not in requested_items:
                    continue
                if len(item_text) < MIN_FILING_ITEM_CHARS:
                    continue

                item_key, display_name, revenue_only = requested_items[item_number]

                if revenue_only:
                    revenue_notes = extract_revenue_notes(item_text, filing_type)
                    if revenue_notes:
                        content_dict[f"{prefix}_revenue_notes"] = revenue_notes
                        items_metadata["revenue_notes"] = {
                            "number": item_number,
                            "name": (
                                f"Revenue Disaggregation "
                                f"(extracted from {item_number})"
                            ),
                            "total_chars": len(revenue_notes),
                            "source_item": item_key,
                            "source_total_chars": len(item_text),
                            "source": "api_filings_items",
                        }
                        print(
                            f"    ✓ API fallback: {filing_type} revenue notes: "
                            f"{len(revenue_notes):,} chars",
                            file=sys.stderr,
                        )
                else:
                    formatted = format_filing_content(item_text)
                    structure = extract_filing_structure(item_text, formatted)

                    items_metadata[item_key] = {
                        "number": item_number,
                        "name": display_name,
                        "total_chars": len(formatted),
                        "sections": structure["sections"],
                        "keywords": structure["keywords"],
                        "source": "api_filings_items",
                    }
                    content_dict[f"{prefix}_{item_key}"] = formatted
                    print(
                        f"    ✓ API fallback: {filing_type} {item_number} "
                        f"({display_name}): {len(formatted):,} chars",
                        file=sys.stderr,
                    )

            if not items_metadata:
                # ISS-012 fix: items returned but none matched our requested
                # items / passed MIN_FILING_ITEM_CHARS — surface as NOT_FOUND
                # not silent PASSED-empty.
                print(
                    "    API fallback: no usable items extracted",
                    file=sys.stderr,
                )
                return AdapterResult.failed(
                    code=ErrorCode.NOT_FOUND,
                    detail=f"API filings/items had {len(api_items)} items but none matched requested keys for {filing_type}",
                    source=src,
                    retryable=False,
                    data={"items_metadata": items_metadata, "content_dict": content_dict},
                )

        except Exception as e:
            # ISS-012 fix: pre-fix swallowed exception → silent PASSED-empty.
            # Now route through canonical DL1→DL2 mapper so HTTP / parse /
            # SSRF / size errors surface with their proper ErrorCode.
            print(f"    API fallback failed: {e}", file=sys.stderr)
            return adapter_error_from_exception(e, source=src)

        return AdapterResult.passed(
            data={"items_metadata": items_metadata, "content_dict": content_dict}
        )

    except Exception as e:
        return adapter_error_from_exception(e, source=src)
