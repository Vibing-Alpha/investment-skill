"""Financial Modeling Prep (FMP) API adapter.

Provides filing metadata and filing-date resolution. DI variants accept
fmp_api_key as an explicit parameter (no module-level global).
"""

import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from scripts.constants import FMP_BASE_URL
from scripts.sources.common import (
    http_get, HttpStatusError, FMP_POLICY, SEC_POLICY, safe_http_get_json,
    safe_num, emit_with_numeric_coerce,
)
from scripts.sources.adapter_result import (
    AdapterResult,
    ErrorCode,
    adapter_error_from_exception,
)
from scripts.sources.api_shapes import (
    validate_api_shape, FMP_FILING_LIST_SHAPE, ShapeError,
    _is_valid_yyyy_mm_dd,
    FMP_STATEMENT_SHAPE, FMP_ANALYST_EST_SHAPE, FMP_EARN_SURPRISE_SHAPE,
)
# ISS-220 SF-C (Loop32 cycle 2): _is_valid_yyyy_mm_dd promoted to
# api_shapes (single source of truth + single regex compile shared
# with Date_yyyy_mm_dd shape primitive + _check_date_array validator).
# Pre-fix this module had its own regex + helper, drift-prone if
# either side updated the format.


def _fmp_redact_variants(fmp_api_key: str) -> tuple:
    """Build the redact tuple covering both the raw key and its
    URL-encoded form.

    ISS-062 (Loop4): FMP URL is built via `urllib.parse.urlencode(...)`
    which percent-encodes special chars (`+`, `/`, `=`, `:`, etc.).
    A key like `abc+def/=ghi` becomes `abc%2Bdef%2F%3Dghi` in the URL.
    DL1 transport exceptions embed the URL via __str__, so the
    percent-encoded form lands in `str(e)` — and `_scrub_detail` only
    matches literally, missing the encoded variant.

    Cover both: raw key + quote/quote_plus/quote(safe='') variants.
    Empty-key case → empty tuple (no scrubbing needed).
    """
    if not fmp_api_key:
        return ()
    variants = {fmp_api_key,
                urllib.parse.quote(fmp_api_key, safe=''),
                urllib.parse.quote_plus(fmp_api_key)}
    # De-dup; cast to tuple for envelope contract.
    return tuple(variants)


# ---------------------------------------------------------------------------
# DI variant: accepts fmp_api_key as a parameter
# ---------------------------------------------------------------------------

def _fetch_filing_metadata_from_fmp_impl(
    ticker: str,
    filing_type: str,
    limit: int = 1,
    fmp_api_key: str = "",
) -> AdapterResult:
    """Fetch SEC filing metadata from Financial Modeling Prep API.

    Returns AdapterResult wrapping {"items": List[Dict]} with fillingDate,
    acceptedDate, finalLink, cik.  FMP is preferred over EFTS for filing
    dates (faster, more reliable).

    This is the DI variant: *fmp_api_key* is passed explicitly instead of
    being read from a module global.
    """
    src = "fmp._fetch_filing_metadata_from_fmp_impl"
    if not fmp_api_key:
        return AdapterResult.failed(
            code=ErrorCode.UNAUTHORIZED,
            detail="fmp_api_key not provided",
            source=src,
            retryable=False,
        )
    # ISS-152 (Loop16 cycle 1 fresh-session-3): reject limit < 1 at
    # entry. Pre-fix `raw[:0]` returned [] which vacuously passed
    # FMP_FILING_LIST_SHAPE → AdapterResult.passed(items=[]). Caller
    # asked for 0 rows = caller bug; surface as SHAPE_MISMATCH so it
    # gets noticed instead of a deceptively-PASSED empty envelope.
    # ISS-191 (Loop26 cycle 1 fresh-session-13): reject bool limit.
    # bool is int subclass, so `isinstance(True, int)` is True — without
    # this explicit check, `limit=True` (caller bug) sliced as
    # `raw[:True]` = first 1 row, masking the real argument-type error
    # as a silent narrow result. Mirror of is_bool_like rejection
    # pattern across the project.
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return AdapterResult.failed(
            code=ErrorCode.SHAPE_MISMATCH,
            detail=f"limit must be int >= 1, got {limit!r}",
            source=src,
            retryable=False,
        )

    try:
        # ISS-027: urlencode the query params (`type`, `page`, `apikey`).
        # `safe_ticker` stays as a path segment (urlencode is for query
        # strings, not path). filing_type is caller-supplied str — genuine
        # defense. apikey alnum so encoding is identity.
        safe_ticker = urllib.parse.quote(ticker, safe='')
        query = urllib.parse.urlencode({
            "type": filing_type, "page": 0, "apikey": fmp_api_key,
        })
        url = f"{FMP_BASE_URL}/sec_filings/{safe_ticker}?{query}"
        # Structural (post Loop21): safe_http_get_json centralizes
        # the status check + JSON parse contract. Replaces the
        # explicit `if resp.status >= 400: raise HttpStatusError`
        # pattern that 4 fresh-session rounds repeatedly found missing
        # in different adapters.
        raw = safe_http_get_json(url, policy=FMP_POLICY)
        # ISS-183 (Loop25 cycle 1 fresh-session-12): split the 200-but-
        # bad-shape branch from the empty-list branch. Pre-fix both
        # mapped to NOT_FOUND, but a non-list body (e.g. FMP error
        # `{"Error Message": "bad key"}`) is shape/error drift, not
        # genuinely "no filing". Misclassifying as NOT_FOUND let
        # filing fallback logic treat upstream provider failures as
        # absent data — wrong diagnosis. Now: non-list → SHAPE_MISMATCH;
        # empty list → NOT_FOUND.
        if not isinstance(raw, list):
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=(
                    f"FMP sec_filings returned non-list body "
                    f"({type(raw).__name__}) for {ticker}/{filing_type}"
                ),
                source=src,
                retryable=False,
            )
        if not raw:
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND,
                detail=f"FMP sec_filings returned empty list for {ticker}/{filing_type}",
                source=src,
                retryable=False,
            )
        # ISS-148 (Loop15 cycle 1 fresh-session-2): slice BEFORE validate
        # so a malformed row beyond `limit` doesn't fail the whole call.
        # Pre-fix `validate(raw)` then `raw[:limit]` meant a 6th row with
        # missing `symbol` failed when caller only requested 3 valid rows.
        # Slice-first preserves the contract: validate exactly what we
        # plan to return.
        results = raw[:limit]
        v = validate_api_shape(results, FMP_FILING_LIST_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)
        return AdapterResult.passed(
            data={"items": results},
            meta={"source_hint": "fmp_sec_filings"},
        )
    except Exception as e:
        # ISS-072 (Loop5): stderr scrub must also cover URL-encoded key
        # variants. Pre-fix `err.replace(fmp_api_key, ...)` only matched
        # raw key — `urlencode()` produced `%2B` / `%2F` / `%3D` forms
        # that survived. Use _scrub_detail with full variant tuple,
        # mirroring the envelope-detail path.
        from scripts.sources.adapter_result import _scrub_detail
        variants = _fmp_redact_variants(fmp_api_key)
        err = _scrub_detail(str(e), variants)
        print(f"    FMP filing metadata fetch failed: {err}", file=sys.stderr)
        return adapter_error_from_exception(
            e, source=src, redact=variants,
        )


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

def convert_fmp_to_filing_metadata(fmp_item: Dict) -> Dict:
    """Convert FMP sec_filings item to the Financial Datasets API format.

    FMP format:
        {symbol, fillingDate, acceptedDate, cik, type, link, finalLink}

    Target format:
        {ticker, report_date, filing_date, accession_number, url, filing_type}
    """
    # ISS-159 (Loop18 cycle 1 fresh-session-5): `.get(k, "")` returns
    # the default ONLY when the key is absent; if the key exists with
    # value None (FMP_FILING_LIST_SHAPE permits Optional finalLink/link),
    # we get None, then `re.search(pat, None)` raises TypeError. Normalize
    # both via `or ""` so falsy values land on a safe empty-string path.
    final_link = fmp_item.get("finalLink") or ""
    index_link = fmp_item.get("link") or ""

    # Extract report_date from finalLink filename:
    #   "rklb-20241231.htm" -> "2024-12-31"
    report_date = ""
    date_match = re.search(r'-(\d{4})(\d{2})(\d{2})\.htm', final_link)
    if date_match:
        report_date = (
            f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        )
        # ISS-220 4.21 (Loop35 cycle 1): sym-ext gap of SF-F. The sibling
        # `filing_date` path (line 232+) routes through
        # `_is_valid_yyyy_mm_dd` to reject calendar-impossible dates.
        # This `report_date` path was assembled from regex groups
        # without that check — `-20249999.htm` produced report_date
        # = "2024-99-99" which downstream `int(report_date[5:7])`
        # would interpret as month=99. Mirror SF-F validation here
        # so both sibling paths emit calendar-valid dates or "".
        if not _is_valid_yyyy_mm_dd(report_date):
            report_date = ""

    # Extract accession_number from index link or finalLink path
    accession_number = ""
    acc_match = re.search(r'/(\d{10}-\d{2}-\d{6})', index_link)
    if acc_match:
        accession_number = acc_match.group(1)
    else:
        # Try to extract from directory path:
        #   "000162828025008724" -> "0001628280-25-008724"
        dir_match = re.search(r'/(\d{18})/', final_link or index_link)
        if dir_match:
            raw = dir_match.group(1)
            accession_number = f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"

    # Extract filing_date from fillingDate
    # ISS-218 (Loop31 cycle 1 fresh-session-18): fix-of-fix of ISS-207.
    # ISS-207 added `_is_valid_yyyy_mm_dd` to `_fetch_filing_date_impl`
    # but the sibling converter here was missed — Lesson 1 ("fix one,
    # miss the others") recurrence. Validate the sliced value the same
    # way; emit "" on malformed instead of letting "unknown"[:10] =
    # "unknown" land in PASSED downstream.
    filling_date = fmp_item.get("fillingDate", "")
    sliced = filling_date[:10] if filling_date else ""
    filing_date = sliced if sliced and _is_valid_yyyy_mm_dd(sliced) else ""

    return {
        "ticker": fmp_item.get("symbol", ""),
        "report_date": report_date,
        "filing_date": filing_date,
        "accession_number": accession_number,
        "url": final_link,
        "filing_type": fmp_item.get("type", ""),
        "source": "fmp",
    }


# ---------------------------------------------------------------------------
# DI variant: filing date resolution
# ---------------------------------------------------------------------------

def _fetch_filing_date_impl(
    ticker: str,
    filing_type: str,
    accession_number: str,
    fmp_api_key: str = "",
    fetch_fmp_metadata_fn=None,
) -> AdapterResult:
    """Get actual filing date.  Tries FMP first, falls back to SEC EDGAR EFTS.

    Returns AdapterResult wrapping {"filing_date": "YYYY-MM-DD"} on success,
    or a FAILED/NOT_FOUND result on failure.

    This is the DI variant: *fmp_api_key* and *fetch_fmp_metadata_fn* are
    injected explicitly.  If *fetch_fmp_metadata_fn* is None, it defaults to
    ``_fetch_filing_metadata_from_fmp_impl``.
    """
    src = "fmp._fetch_filing_date_impl"
    if fetch_fmp_metadata_fn is None:
        fetch_fmp_metadata_fn = _fetch_filing_metadata_from_fmp_impl

    # ISS-028 (Cycle 4 backlog): track Method 1's outcome (envelope or
    # exception) so when Method 2 also fails / is unavailable, the final
    # return can surface the higher-fidelity Method 1 cause instead of
    # collapsing to NOT_FOUND. Pre-fix, an FMP 401/429 followed by no
    # accession_number → NOT_FOUND, hiding the auth/rate-limit signal.
    method1_result: Optional[AdapterResult] = None
    method1_exception: Optional[Exception] = None

    # Method 1: FMP API (preferred -- returns fillingDate directly)
    if fmp_api_key:
        try:
            meta_result = fetch_fmp_metadata_fn(
                ticker, filing_type, limit=3, fmp_api_key=fmp_api_key,
            )
            method1_result = meta_result
            # v19 unwrap: producer returns AdapterResult after T15; code below
            # iterates List[Dict] from pre-migration contract.
            # v24 (T15+T16 review I-1): aligned on `.ok` (PASSED-only) to match
            # fetch.py:210 / :221 helper-seam policy. PARTIAL would be valid if
            # the producer ever emits it, but `_fetch_filing_metadata_from_fmp_impl`
            # currently only emits PASSED/FAILED. If future change introduces
            # PARTIAL, both seams need explicit consideration.
            fmp_results = meta_result.data.get("items", []) if meta_result.ok else []
            for f in fmp_results:
                # ISS-162 (Loop19 cycle 1 fresh-session-6): `.get("link", "")`
                # returns the default ONLY when the key is absent; if the
                # key is present with value None, we get None and `.replace()`
                # raises AttributeError → INTERNAL_ERROR. Same pattern as
                # ISS-159 (convert_fmp_to_filing_metadata) which fixed
                # finalLink/link there. Symmetric fix here.
                fmp_accession = f.get("link") or ""
                accession_clean = accession_number.replace("-", "") if accession_number else ""
                if accession_clean and accession_clean in fmp_accession.replace("-", ""):
                    filling_date = f.get("fillingDate") or ""
                    if filling_date and isinstance(filling_date, str) and _is_valid_yyyy_mm_dd(filling_date[:10]):
                        return AdapterResult.passed(
                            data={"filing_date": filling_date[:10]},
                            meta={"source_hint": "fmp_method1_accession"},
                        )
            # Fall back to first filing date when no accession was supplied
            if not accession_number and fmp_results:
                filling_date = fmp_results[0].get("fillingDate") or ""
                if filling_date and isinstance(filling_date, str) and _is_valid_yyyy_mm_dd(filling_date[:10]):
                    return AdapterResult.passed(
                        data={"filing_date": filling_date[:10]},
                        meta={"source_hint": "fmp_method1_first"},
                    )
        except Exception as exc:
            # Method 1 failure falls through to Method 2 below. Track for
            # ISS-028 final-error preservation. Log the exception type so
            # operators can see that FMP tried and failed before EDGAR-EFTS
            # was consulted.
            # ISS-080 (Loop6): scrub stderr too. DI seam may inject a
            # producer that throws an exception with `apikey=...` in URL;
            # without scrub the raw key (or its url-encoded variant)
            # leaks to stderr. Use _fmp_redact_variants so ALL forms of
            # the secret are scrubbed.
            from scripts.sources.adapter_result import _scrub_detail
            method1_exception = exc
            scrubbed_exc_str = _scrub_detail(
                str(exc), _fmp_redact_variants(fmp_api_key),
            )
            print(f"    FMP filing-date method 1 failed: "
                  f"{type(exc).__name__}: {scrubbed_exc_str}", file=sys.stderr)

    # Method 2: SEC EDGAR EFTS (fallback)
    # C2-M1 (Codex Cycle 2): target is efts.sec.gov — must use SEC_POLICY,
    # not FMP_POLICY. SEC_POLICY has HTTPS-only allowed_schemes and a retry
    # set that includes 403 (SEC's rate-limit signal) and 408. FMP_POLICY
    # would miss those transient codes on this SEC endpoint.
    if accession_number:
        try:
            # ISS-008: URL-encode accession_number — although it comes from
            # FMP API responses (trusted upstream today), defense-in-depth:
            # a compromised or schema-drifted FMP response that returned
            # `"\"&from=0&size=100&q=*"` would otherwise inject EFTS query
            # params. urllib.parse.quote with safe='' escapes `&`/`=`/`"`/etc.
            safe_acc = urllib.parse.quote(accession_number, safe='')
            url = (
                f"https://efts.sec.gov/LATEST/search-index"
                f"?q=%22{safe_acc}%22"
            )
            headers = {
                "User-Agent": "StockAnalysis/1.0 (research@example.com)",
                "Accept": "application/json",
            }
            # Structural (post Loop21): safe_http_get_json centralizes
            # status check + JSON parse. C3-H1 / pre-DL1 HTTPError
            # observability is preserved via the raise-HttpStatusError
            # contract inside the helper.
            data = safe_http_get_json(url, policy=SEC_POLICY, headers=headers)
            # ISS-170 (Loop21 cycle 1 fresh-session-8): validate EFTS
            # response shape before dereferencing. Pre-fix `_source.file_date`
            # could be a list or `_source` could be None — both produce
            # garbage in PASSED envelope or AttributeError → INTERNAL_ERROR.
            if not isinstance(data, dict):
                raise ShapeError(
                    "efts", "data",
                    f"expected dict, got {type(data).__name__}",
                )
            hits_outer = data.get("hits", {})
            if not isinstance(hits_outer, dict):
                raise ShapeError(
                    "efts", "hits",
                    f"expected dict, got {type(hits_outer).__name__}",
                )
            hits = hits_outer.get("hits", [])
            if not isinstance(hits, list):
                raise ShapeError(
                    "efts", "hits.hits",
                    f"expected list, got {type(hits).__name__}",
                )
            if hits:
                if not isinstance(hits[0], dict):
                    raise ShapeError(
                        "efts", "hits.hits[0]",
                        f"expected dict, got {type(hits[0]).__name__}",
                    )
                source = hits[0].get("_source", {})
                if not isinstance(source, dict):
                    raise ShapeError(
                        "efts", "hits.hits[0]._source",
                        f"expected dict, got {type(source).__name__}",
                    )
                file_date = source.get("file_date", "")
                if file_date and not isinstance(file_date, str):
                    raise ShapeError(
                        "efts", "hits.hits[0]._source.file_date",
                        f"expected str, got {type(file_date).__name__}",
                    )
                # ISS-220 SF-C (Loop32 cycle 2): adapter contract is
                # `filing_date: YYYY-MM-DD`. Pre-fix accepted any non-
                # empty string and emitted PASSED — a malformed EFTS
                # `file_date` slipped past the adapter boundary.
                if file_date:
                    if not _is_valid_yyyy_mm_dd(file_date):
                        raise ShapeError(
                            "efts", "hits.hits[0]._source.file_date",
                            f"expected YYYY-MM-DD format, got {file_date!r}",
                        )
                    return AdapterResult.passed(
                        data={"filing_date": file_date},
                        meta={"source_hint": "edgar_efts"},
                    )
        except Exception as exc:
            # ISS-093 (Loop7): scrub stderr too. EFTS URL itself has no
            # api key, but if exception message includes accession_number
            # query or method-1 context with apikey reference, raw
            # `{exc}` print would bypass the redact protocol.
            from scripts.sources.adapter_result import (
                _scrub_detail, severity_of_error, severity_of_exception,
            )
            variants = _fmp_redact_variants(fmp_api_key)
            scrubbed_exc = _scrub_detail(str(exc), variants)
            print(f"    FMP filing-date method 2 (EDGAR-EFTS) failed: "
                  f"{type(exc).__name__}: {scrubbed_exc}", file=sys.stderr)
            # ISS-156 (Loop17 cycle 1 fresh-session-4): when both methods
            # have a structured error, return the higher-severity cause.
            # Pre-fix this path returned method 2's envelope unconditionally,
            # so an FMP UNAUTHORIZED (method 1) followed by EFTS HTTP_TRANSPORT
            # (method 2) would surface HTTP_TRANSPORT — losing the auth
            # signal that's actionable. Mirrors the post-method-2 logic
            # at L332-364 which already prefers method 1 in non-exception
            # paths; this closes the symmetric exception path.
            #
            # Pattern S (audit_fail_open): only direct AdapterResult
            # constructors / adapter_error_from_exception calls allowed
            # in the return statement, no name-bind-then-return. We
            # compute the SEVERITY decision first (without binding the
            # envelopes) then dispatch the appropriate direct call.
            method2_severity = severity_of_exception(exc)
            method1_severity = None
            if (
                method1_result is not None
                and not method1_result.ok
                and method1_result.error is not None
            ):
                method1_severity = severity_of_error(method1_result.error)
            elif method1_exception is not None:
                method1_severity = severity_of_exception(method1_exception)
            # Lower severity number = higher severity (BLOCKING=1, ...).
            if method1_severity is not None and method1_severity <= method2_severity:
                if method1_exception is not None:
                    return adapter_error_from_exception(
                        method1_exception, source=src, redact=variants,
                    )
                # method1_result envelope: re-emit at this entrypoint's
                # source while preserving full child metadata.
                # ISS-220 SF-A (Loop32 cycle 2): pre-fix this rewrap dropped
                # `shape_errors` from `failed_from_shape` envelopes — codex
                # loop32 found the gap. failed_from_child copies all 7 child
                # AdapterError fields verbatim.
                return AdapterResult.failed_from_child(
                    method1_result.error, source=src,
                )
            return adapter_error_from_exception(
                exc, source=src, redact=variants,
            )

    # ISS-028 (Cycle 4 backlog): if Method 1 failed with a structured
    # error (401/403 → UNAUTHORIZED, 429 → RATE_LIMIT, 5xx → UPSTREAM_ERROR),
    # surface that as the final cause instead of collapsing to NOT_FOUND.
    # NOT_FOUND only when both methods truly returned no data.
    if method1_exception is not None:
        return adapter_error_from_exception(
            method1_exception, source=src,
            redact=_fmp_redact_variants(fmp_api_key),
        )
    if method1_result is not None and not method1_result.ok and method1_result.error is not None:
        # Method 1 returned a structured FAILED envelope (e.g. UNAUTHORIZED
        # because empty fmp_api_key). Re-emit the same error code but with
        # this entrypoint's source so audit trails are accurate.
        # ISS-049 (Loop3 backlog): re-scrub the composed detail in case
        # method1's detail includes secrets that escaped its own scrub
        # (DI seam, future producer drift). Truncate to 400 to align
        # with envelope.error.detail format.
        from scripts.sources.adapter_result import _scrub_detail
        composed_detail = (
            f"filing date not found via method1 ({method1_result.error.code.value}: "
            f"{method1_result.error.detail}) and method2 (EFTS) returned no hits"
        )
        # ISS-092 (Loop7): use _fmp_redact_variants so URL-encoded key
        # forms in method1 detail (e.g. via DI seam returning detail
        # with `apikey=abc%2Bdef`) are also scrubbed.
        scrubbed_detail = _scrub_detail(
            composed_detail,
            _fmp_redact_variants(fmp_api_key),
        )[:400]
        # ISS-220 SF-A (Loop32 cycle 2): preserve full child metadata.
        return AdapterResult.failed_from_child(
            method1_result.error, source=src, detail=scrubbed_detail,
        )
    return AdapterResult.failed(
        code=ErrorCode.NOT_FOUND,
        detail=f"filing date not found for {ticker}/{filing_type}/{accession_number}",
        source=src,
        retryable=False,
    )


# ===========================================================================
# Financial-data fallback (2026-05-29 dual-API integration)
#
# FMP is the second API used to BACK-FILL categories where Financial Datasets
# ("FDS") returns 404 / empty / a non-consecutive quarter set (the
# missing-fiscal-Q4 cohort). These functions fetch from FMP, convert each
# response into the *FDS canonical schema* the rest of the pipeline already
# consumes, and return an AdapterResult. They are pure adapters (no global
# state); the orchestration decision lives in `_run_fmp_fallback_impl`.
#
# Currency: statement rows are tagged with their true `reportedCurrency`
# (USD for US issuers, native — e.g. JPY — for foreign ADRs). The tag is
# NEVER hardcoded to "USD" (units.md Pattern W); the downstream DL3c FX gate
# converts supported non-USD statements. See
# docs/superpowers/plans/2026-05-29-fmp-dual-api-fallback.md.
# ===========================================================================

_FMP_QUARTERLY_PERIODS = frozenset({"Q1", "Q2", "Q3", "Q4"})


def _fmp_query_url(endpoint: str, params: Dict, fmp_api_key: str) -> str:
    """Build an FMP v3 URL. `endpoint` already includes any ticker path
    segment (URL-quoted by the caller). `apikey` is appended to the query.
    """
    q = dict(params)
    q["apikey"] = fmp_api_key
    return f"{FMP_BASE_URL}/{endpoint}?{urllib.parse.urlencode(q)}"


def _fmp_fiscal_period(cal, period) -> Optional[str]:
    """FMP `calendarYear` ("2026") + `period` ("Q2") -> "2026-Q2" (YYYY-QN).

    Returns None for any non-quarterly / missing component so the row is
    dropped by the downstream quarter-window aligner instead of producing a
    malformed fiscal_period.
    """
    if cal is None or period is None:
        return None
    period = str(period).strip().upper()
    if period not in _FMP_QUARTERLY_PERIODS:
        return None
    cal_str = str(cal).strip()
    if not re.fullmatch(r"\d{4}", cal_str):
        return None
    return f"{cal_str}-{period}"


def _is_fmp_quarterly_row(r: Dict) -> bool:
    """True iff the FMP statement row is a quarterly (Q1-Q4) period."""
    if not isinstance(r, dict):
        return False
    period = r.get("period")
    return isinstance(period, str) and period.strip().upper() in _FMP_QUARTERLY_PERIODS


def _fmp_statement_common(r: Dict) -> Dict:
    """Shared identity/metadata fields every converted statement row carries."""
    currency = r.get("reportedCurrency")
    return {
        "ticker": r.get("symbol"),
        "report_period": r.get("date"),
        "fiscal_period": _fmp_fiscal_period(r.get("calendarYear"), r.get("period")),
        "period": "quarterly",
        # Faithful native-currency tag (USD / JPY / …). NEVER hardcoded —
        # the DL3c gate downstream relies on this being the true statement
        # currency to decide USD-direct vs FX-convert vs fail-close.
        "currency": currency if currency else None,
    }


def _convert_fmp_income_row(r: Dict) -> Dict:
    """FMP income-statement row -> FDS income_statements row schema."""
    out = _fmp_statement_common(r)
    ni = r.get("netIncome")
    # EBIT proxy = pretax income + interest expense (textbook). None-safe.
    ibt = safe_num(r.get("incomeBeforeTax"))
    intexp = safe_num(r.get("interestExpense"))
    ebit = (ibt + intexp) if (ibt is not None and intexp is not None) else None
    out.update({
        "revenue": r.get("revenue"),
        "cost_of_revenue": r.get("costOfRevenue"),
        "gross_profit": r.get("grossProfit"),
        "operating_expense": r.get("operatingExpenses"),
        "selling_general_and_administrative_expenses": r.get(
            "sellingGeneralAndAdministrativeExpenses"),
        "research_and_development": r.get("researchAndDevelopmentExpenses"),
        "operating_income": r.get("operatingIncome"),
        "interest_expense": r.get("interestExpense"),
        "ebit": ebit,
        "ebitda": r.get("ebitda"),
        "income_tax_expense": r.get("incomeTaxExpense"),
        "net_income": ni,
        # FMP `netIncome` is attributable to the common shareholders; pair it
        # into net_income_common_stock so the mixed-currency repair's
        # common-vs-consolidated check (NOK fix) never false-fails on
        # FMP-sourced rows.
        "net_income_common_stock": ni,
        "consolidated_income": ni,
        "earnings_per_share": r.get("eps"),
        "earnings_per_share_diluted": r.get("epsdiluted"),
        "weighted_average_shares": r.get("weightedAverageShsOut"),
        "weighted_average_shares_diluted": r.get("weightedAverageShsOutDil"),
        "filing_url": r.get("finalLink") or r.get("link") or None,
    })
    return out


def _convert_fmp_balance_row(r: Dict) -> Dict:
    """FMP balance-sheet row -> FDS balance_sheets row schema."""
    out = _fmp_statement_common(r)
    out.update({
        "total_assets": r.get("totalAssets"),
        "current_assets": r.get("totalCurrentAssets"),
        "cash_and_equivalents": r.get("cashAndCashEquivalents"),
        "inventory": r.get("inventory"),
        "current_investments": r.get("shortTermInvestments"),
        "trade_and_non_trade_receivables": r.get("netReceivables"),
        "non_current_assets": r.get("totalNonCurrentAssets"),
        "property_plant_and_equipment": r.get("propertyPlantEquipmentNet"),
        "goodwill_and_intangible_assets": r.get("goodwillAndIntangibleAssets"),
        "investments": r.get("totalInvestments"),
        "non_current_investments": r.get("longTermInvestments"),
        "total_liabilities": r.get("totalLiabilities"),
        "current_liabilities": r.get("totalCurrentLiabilities"),
        "current_debt": r.get("shortTermDebt"),
        "trade_and_non_trade_payables": r.get("accountPayables"),
        "deferred_revenue": r.get("deferredRevenue"),
        "non_current_liabilities": r.get("totalNonCurrentLiabilities"),
        "non_current_debt": r.get("longTermDebt"),
        "shareholders_equity": r.get("totalStockholdersEquity"),
        "retained_earnings": r.get("retainedEarnings"),
        "accumulated_other_comprehensive_income": r.get(
            "accumulatedOtherComprehensiveIncomeLoss"),
        "total_debt": r.get("totalDebt"),
    })
    return out


def _convert_fmp_cashflow_row(r: Dict) -> Dict:
    """FMP cash-flow row -> FDS cash_flows row schema. Only fields with a
    direct 1:1 FMP source are mapped; composite-only FDS fields are left
    absent rather than fabricated."""
    out = _fmp_statement_common(r)
    # codex 2026-05-29 (MED): the FDS field is NET equity financing (the
    # canonical yfinance mapping uses "Net Common Stock Issuance"). FMP splits
    # it across commonStockIssued + commonStockRepurchased — mapping only the
    # repurchase line dropped issuance. Sum both (None-safe); None only when
    # both are absent.
    _csi = safe_num(r.get("commonStockIssued"))
    _csr = safe_num(r.get("commonStockRepurchased"))
    _equity_net = None if (_csi is None and _csr is None) else (_csi or 0) + (_csr or 0)
    out.update({
        "net_cash_flow_from_operations": r.get("netCashProvidedByOperatingActivities"),
        "depreciation_and_amortization": r.get("depreciationAndAmortization"),
        "share_based_compensation": r.get("stockBasedCompensation"),
        "net_cash_flow_from_investing": r.get("netCashUsedForInvestingActivites"),
        "capital_expenditure": r.get("capitalExpenditure"),
        "business_acquisitions_and_disposals": r.get("acquisitionsNet"),
        "net_cash_flow_from_financing": r.get("netCashUsedProvidedByFinancingActivities"),
        "issuance_or_repayment_of_debt_securities": r.get("debtRepayment"),
        "issuance_or_purchase_of_equity_shares": _equity_net,
        "dividends_and_other_cash_distributions": r.get("dividendsPaid"),
        "change_in_cash_and_equivalents": r.get("netChangeInCash"),
        "effect_of_exchange_rate_changes": r.get("effectOfForexChangesOnCash"),
    })
    return out


def _convert_fmp_metrics(ticker: str, quote: Dict, key_metrics: Dict,
                         ratios: Dict) -> Dict:
    """FMP quote + key-metrics + ratios -> FDS metrics_snapshot schema.

    All margins/ratios are decimals in BOTH APIs (verified against a real FDS
    artifact: gross_margin 0.668, debt_to_equity 3.746) — no scale conversion.
    Fields not derivable from these three endpoints (free_cash_flow absolute,
    revenue_growth, earnings_growth) are left null rather than guessed.
    """
    return {
        "ticker": ticker,
        "period": "quarterly",
        "price_to_earnings_ratio": ratios.get("priceEarningsRatio"),
        "price_to_sales_ratio": ratios.get("priceToSalesRatio"),
        "price_to_book_ratio": ratios.get("priceToBookRatio"),
        "enterprise_value_to_ebitda_ratio": key_metrics.get("enterpriseValueOverEBITDA"),
        "enterprise_value_to_revenue_ratio": key_metrics.get("evToSales"),
        "enterprise_value": key_metrics.get("enterpriseValue"),
        "market_cap": quote.get("marketCap"),
        "free_cash_flow": None,
        "free_cash_flow_per_share": key_metrics.get("freeCashFlowPerShare"),
        "free_cash_flow_yield": key_metrics.get("freeCashFlowYield"),
        "earnings_per_share": key_metrics.get("netIncomePerShare"),
        "book_value_per_share": key_metrics.get("bookValuePerShare"),
        "current_ratio": ratios.get("currentRatio"),
        "quick_ratio": ratios.get("quickRatio"),
        "debt_to_equity": ratios.get("debtEquityRatio"),
        "gross_margin": ratios.get("grossProfitMargin"),
        "operating_margin": ratios.get("operatingProfitMargin"),
        "net_margin": ratios.get("netProfitMargin"),
        "return_on_equity": ratios.get("returnOnEquity"),
        "return_on_assets": ratios.get("returnOnAssets"),
        "revenue_growth": None,
        "earnings_growth": None,
        "peg_ratio": ratios.get("priceEarningsToGrowthRatio"),
        "payout_ratio": ratios.get("payoutRatio"),
    }


def _convert_fmp_estimate_row(r: Dict) -> Dict:
    """FMP analyst-estimates row -> FDS analyst estimates row schema.
    FMP `*Avg` consensus maps to both the primary and the `*_mean` field.

    codex 2026-05-29: match the FDS row contract exactly — `period` carries
    the granularity literal `"quarterly"` and `fiscal_period` carries the
    quarter-end date (verified against real FDS artifacts:
    period="quarterly", fiscal_period="2026-06-30"). The pre-fix converter
    inverted this (period=<date>, fiscal_period=None), breaking the
    score-forward / score-business quarter-identification convention."""
    eps_avg = r.get("estimatedEpsAvg")
    rev_avg = r.get("estimatedRevenueAvg")
    return {
        "period": "quarterly",
        "fiscal_period": r.get("date"),
        "earnings_per_share": eps_avg,
        "earnings_per_share_mean": eps_avg,
        "earnings_per_share_high": r.get("estimatedEpsHigh"),
        "earnings_per_share_low": r.get("estimatedEpsLow"),
        "revenue": rev_avg,
        "revenue_mean": rev_avg,
        "revenue_high": r.get("estimatedRevenueHigh"),
        "revenue_low": r.get("estimatedRevenueLow"),
        "ebitda": r.get("estimatedEbitdaAvg"),
        "ebit": r.get("estimatedEbitAvg"),
        "net_income": r.get("estimatedNetIncomeAvg"),
    }


def _convert_fmp_earnings(surprises: List, calendar: List) -> Dict:
    """FMP earnings-surprises (newest-first) + earning_calendar -> FDS
    earnings snapshot schema. Revenue is enriched from the calendar row whose
    date matches the surprise date. The item-level `currency` is left None —
    FMP earnings carry no currency and we never assume USD. The combined-level
    currency is owned by the caller (set from FDS earnings when present, else
    left None/unknown); it is NOT inferred from the statement currency, since
    FMP earnings EPS for an ADR is typically per-ADR in USD while the
    statements are in the native currency (e.g. JPY). (codex 2026-05-29)"""
    latest = surprises[0] if surprises else {}
    date = latest.get("date")
    actual = safe_num(latest.get("actualEarningResult"))
    est = safe_num(latest.get("estimatedEarning"))
    surprise = (actual - est) if (actual is not None and est is not None) else None
    surprise_pct = None
    if surprise is not None and est not in (None, 0):
        surprise_pct = surprise / abs(est) * 100.0  # human percent (matches FDS convention)

    rev_actual = rev_est = None
    for c in calendar:
        if isinstance(c, dict) and c.get("date") == date:
            rev_actual = c.get("revenue")
            rev_est = c.get("revenueEstimated")
            break
    ra, re_ = safe_num(rev_actual), safe_num(rev_est)
    surprise_rev = (ra - re_) if (ra is not None and re_ is not None) else None

    return {
        "report_period": date,
        "fiscal_period": None,
        "currency": None,
        "actual_eps": actual,
        "estimated_eps": est,
        "surprise_eps": surprise,
        "surprise_pct": surprise_pct,
        "actual_revenue": rev_actual,
        "estimated_revenue": rev_est,
        "surprise_revenue": surprise_rev,
    }


class _FmpNonListError(Exception):
    """FMP endpoint returned a non-list body (e.g. the error payload
    `{"Error Message": ...}`). Raised by `_fmp_fetch_list` so the entrypoint
    can map it to a DIRECT `AdapterResult.failed(SHAPE_MISMATCH)` constructor
    in its return statement (Pattern S: no name-bind-then-return envelopes)."""


def _fmp_fetch_list(endpoint: str, params: Dict, fmp_api_key: str) -> List:
    """GET an FMP endpoint expected to return a top-level JSON list.

    Returns the parsed list on success. Raises on failure so the caller's
    single try/except can dispatch DIRECT AdapterResult constructors in its
    return statements (Pattern S):
      - transport / status / parse errors propagate from safe_http_get_json
        (mapped by `adapter_error_from_exception` in the caller's except);
      - a non-list body raises `_FmpNonListError` (mapped to SHAPE_MISMATCH).
    """
    raw = safe_http_get_json(_fmp_query_url(endpoint, params, fmp_api_key),
                             policy=FMP_POLICY)
    if not isinstance(raw, list):
        raise _FmpNonListError(
            f"FMP {endpoint} returned non-list body ({type(raw).__name__})")
    return raw


def fetch_financials_from_fmp(ticker: str, *, fmp_api_key: str = "") -> AdapterResult:
    """Fetch quarterly income/balance/cash-flow statements from FMP and emit
    them in the FDS `02_financial_data` schema:
    `{"income_statements": [...], "balance_sheets": [...], "cash_flows": [...]}`
    (newest-first, quarterly rows only, native currency preserved)."""
    src = "fmp.fetch_financials_from_fmp"
    if not fmp_api_key:
        return AdapterResult.failed(code=ErrorCode.UNAUTHORIZED,
                                    detail="fmp_api_key not provided", source=src)
    safe_ticker = urllib.parse.quote(ticker, safe='')
    params = {"period": "quarter", "limit": 8}
    try:
        inc_raw = _fmp_fetch_list(f"income-statement/{safe_ticker}", params, fmp_api_key)
        bal_raw = _fmp_fetch_list(f"balance-sheet-statement/{safe_ticker}", params, fmp_api_key)
        cf_raw = _fmp_fetch_list(f"cash-flow-statement/{safe_ticker}", params, fmp_api_key)
    except _FmpNonListError as se:
        return AdapterResult.failed(code=ErrorCode.SHAPE_MISMATCH,
                                    detail=str(se), source=src, retryable=False)
    except Exception as e:  # noqa: BLE001 — routed through canonical mapper
        # Pattern S: the return MUST be a direct adapter_error_from_exception
        # constructor call (no wrapper-helper indirection). Scrub + log first.
        from scripts.sources.adapter_result import _scrub_detail
        variants = _fmp_redact_variants(fmp_api_key)
        print(f"    FMP {src} fetch failed: {_scrub_detail(str(e), variants)}",
              file=sys.stderr)
        return adapter_error_from_exception(e, source=src, redact=variants)

    for raw in (inc_raw, bal_raw, cf_raw):
        v = validate_api_shape(raw, FMP_STATEMENT_SHAPE)
        if not v.ok:
            return AdapterResult.failed_from_shape(v, source=src)

    income = [_convert_fmp_income_row(r) for r in inc_raw if _is_fmp_quarterly_row(r)]
    balance = [_convert_fmp_balance_row(r) for r in bal_raw if _is_fmp_quarterly_row(r)]
    cash = [_convert_fmp_cashflow_row(r) for r in cf_raw if _is_fmp_quarterly_row(r)]
    if not (income and balance and cash):
        return AdapterResult.failed(
            code=ErrorCode.NOT_FOUND,
            detail=f"FMP returned no quarterly statements for {ticker}",
            source=src, retryable=False,
        )

    # codex 2026-05-29 (HIGH): FMP balance sheets carry NO point-in-time share
    # count, but `extract_fcf` (FCF/share) and `historical_multiples` (P/E,
    # P/B, P/S) read `balance.outstanding_shares` and fail-close to
    # SHARES_UNAVAILABLE / skip-window when it is absent — which would defeat
    # the fallback's whole purpose on the missing-Q4 cohort. Backfill each
    # balance row from the SAME-PERIOD income row's basic weighted-average
    # share count (FMP `weightedAverageShsOut`). Weighted-avg ≈ point-in-time
    # for stable-share issuers (verified: AMD 1630.6M≈1631M, CRDO 180.6M≈
    # 182.2M); it is the same denominator EPS uses, so per-share multiples
    # stay self-consistent. Issuance/ADR-ratio skew is handled downstream
    # (adr/correct.py). Only set when the balance row lacks its own value.
    _shares_by_period = {
        r.get("report_period"): r.get("weighted_average_shares")
        for r in income
        if r.get("report_period") is not None
    }
    for b in balance:
        if b.get("outstanding_shares") is None:
            b["outstanding_shares"] = _shares_by_period.get(b.get("report_period"))

    # codex 2026-05-29 (fail-open gate): the aligned-quarter check downstream
    # validates only date/fiscal_period/currency metadata, not that the rows
    # carry load-bearing numbers. Reject a metadata-complete-but-hollow FMP
    # response (provider/schema drift) rather than stamping it PASSED and
    # feeding empty statements into valuation. Require the core signal —
    # revenue AND net income present TOGETHER on at least one income quarter
    # (same-quarter, not split across rows — a row with neither is useless to
    # the per-share/EV math even if a sibling row has one of them).
    _has_core_quarter = any(
        safe_num(r.get("revenue")) is not None
        and safe_num(r.get("net_income")) is not None
        for r in income
    )
    if not _has_core_quarter:
        return AdapterResult.failed(
            code=ErrorCode.NOT_FOUND,
            detail=(f"FMP statements for {ticker} are metadata-only "
                    f"(no revenue/net_income values)"),
            source=src, retryable=False,
        )

    from scripts.sources.financial_datasets import _FINANCIALS_NUMERIC_FIELDS
    data = emit_with_numeric_coerce(
        {"income_statements": income, "balance_sheets": balance, "cash_flows": cash},
        numeric_fields=_FINANCIALS_NUMERIC_FIELDS,
    )
    return AdapterResult.passed(data, meta={"source_hint": "fmp_financials"})


def fetch_metrics_from_fmp(ticker: str, *, fmp_api_key: str = "") -> AdapterResult:
    """Fetch quote + key-metrics + ratios from FMP and emit the FDS
    metrics_snapshot dict (bare dict, matching `fetch_metrics_data`)."""
    src = "fmp.fetch_metrics_from_fmp"
    if not fmp_api_key:
        return AdapterResult.failed(code=ErrorCode.UNAUTHORIZED,
                                    detail="fmp_api_key not provided", source=src)
    safe_ticker = urllib.parse.quote(ticker, safe='')
    try:
        quote_raw = _fmp_fetch_list(f"quote/{safe_ticker}", {}, fmp_api_key)
        km_raw = _fmp_fetch_list(f"key-metrics/{safe_ticker}", {"period": "quarter", "limit": 1}, fmp_api_key)
        ra_raw = _fmp_fetch_list(f"ratios/{safe_ticker}", {"period": "quarter", "limit": 1}, fmp_api_key)
    except _FmpNonListError as se:
        return AdapterResult.failed(code=ErrorCode.SHAPE_MISMATCH,
                                    detail=str(se), source=src, retryable=False)
    except Exception as e:  # noqa: BLE001 — routed through canonical mapper
        # Pattern S: the return MUST be a direct adapter_error_from_exception
        # constructor call (no wrapper-helper indirection). Scrub + log first.
        from scripts.sources.adapter_result import _scrub_detail
        variants = _fmp_redact_variants(fmp_api_key)
        print(f"    FMP {src} fetch failed: {_scrub_detail(str(e), variants)}",
              file=sys.stderr)
        return adapter_error_from_exception(e, source=src, redact=variants)

    # codex 2026-05-29 (consistency): the other 3 FMP fetchers validate row
    # shape via validate_api_shape; the metrics path has no wrapper shape, so
    # guard explicitly — a non-empty list whose first row is not a dict is
    # provider drift, not a partially-usable snapshot. Fail SHAPE_MISMATCH
    # rather than letting `_first` silently coerce it to {}.
    for _rows, _name in ((quote_raw, "quote"), (km_raw, "key-metrics"), (ra_raw, "ratios")):
        if _rows and not isinstance(_rows[0], dict):
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH,
                detail=f"FMP {_name} first row is {type(_rows[0]).__name__}, not dict",
                source=src, retryable=False,
            )

    def _first(rows):
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    quote, km, ra = _first(quote_raw), _first(km_raw), _first(ra_raw)
    if not (quote or km or ra):
        return AdapterResult.failed(code=ErrorCode.NOT_FOUND,
                                    detail=f"FMP returned no metrics for {ticker}",
                                    source=src, retryable=False)
    from scripts.sources.financial_datasets import _METRICS_NUMERIC_FIELDS
    snapshot = _convert_fmp_metrics(ticker, quote, km, ra)
    data = emit_with_numeric_coerce(snapshot, numeric_fields=_METRICS_NUMERIC_FIELDS)
    return AdapterResult.passed(data, meta={"source_hint": "fmp_metrics"})


# Number of near-term forward quarters to surface (FDS native returns ~10).
_FMP_ANALYST_FORWARD_QUARTERS = 12


def fetch_analyst_estimates_from_fmp(
    ticker: str, *, fmp_api_key: str = "", as_of_date: str = "",
) -> AdapterResult:
    """Fetch analyst estimates from FMP and emit the FDS
    `{"estimates": [...], "count": N, "period": "quarterly"}` schema,
    NEAR-TERM-FORWARD-FIRST to match the FDS native ordering.

    codex 2026-05-29 (HIGH): FMP's `/analyst-estimates` returns rows in
    DESCENDING date order spanning BOTH past and far-future quarters
    (e.g. MU: 2028-11-26 … 2019-02-26). A naive `limit=8` therefore kept
    ONLY the farthest-future quarters and dropped the near-term consensus
    that `score-forward` ("next quarter / next FY") and `evaluate-valuation`
    (forward EPS / NTM P/E) require — the opposite of FDS, which returns
    forward quarters ascending. We fetch a wide window, then select the
    upcoming quarters (period-end on/after `as_of_date`) ascending, nearest
    first. `as_of_date` (YYYY-MM-DD) is the run/system date supplied by the
    orchestrator; when absent (standalone call) we fall back to the most
    recent quarters by date so the result is still near-term, never the
    far-future tail.
    """
    src = "fmp.fetch_analyst_estimates_from_fmp"
    if not fmp_api_key:
        return AdapterResult.failed(code=ErrorCode.UNAUTHORIZED,
                                    detail="fmp_api_key not provided", source=src)
    safe_ticker = urllib.parse.quote(ticker, safe='')
    try:
        # Wide window: FMP packs many years descending into one response;
        # 48 quarters comfortably brackets the forward horizon plus history.
        raw = _fmp_fetch_list(f"analyst-estimates/{safe_ticker}",
                              {"period": "quarter", "limit": 48}, fmp_api_key)
    except _FmpNonListError as se:
        return AdapterResult.failed(code=ErrorCode.SHAPE_MISMATCH,
                                    detail=str(se), source=src, retryable=False)
    except Exception as e:  # noqa: BLE001 — routed through canonical mapper
        # Pattern S: the return MUST be a direct adapter_error_from_exception
        # constructor call (no wrapper-helper indirection). Scrub + log first.
        from scripts.sources.adapter_result import _scrub_detail
        variants = _fmp_redact_variants(fmp_api_key)
        print(f"    FMP {src} fetch failed: {_scrub_detail(str(e), variants)}",
              file=sys.stderr)
        return adapter_error_from_exception(e, source=src, redact=variants)
    v = validate_api_shape(raw, FMP_ANALYST_EST_SHAPE)
    if not v.ok:
        return AdapterResult.failed_from_shape(v, source=src)

    # Sort ascending by period-end date (YYYY-MM-DD sorts lexically). Rows
    # missing a date sort first under "" and are harmless (dropped by the
    # forward filter / capped out).
    rows_asc = sorted(
        (r for r in raw if isinstance(r, dict)),
        key=lambda r: r.get("date") or "",
    )
    if as_of_date:
        forward = [r for r in rows_asc if (r.get("date") or "") >= as_of_date]
        # Fallback when every estimate period is already in the past: keep the
        # most recent quarters (tail of the ascending list), not the oldest.
        chosen = forward[:_FMP_ANALYST_FORWARD_QUARTERS] if forward \
            else rows_asc[-_FMP_ANALYST_FORWARD_QUARTERS:]
    else:
        # No run-date context: surface the most recent quarters (ascending),
        # never the far-future tail.
        chosen = rows_asc[-_FMP_ANALYST_FORWARD_QUARTERS:]

    estimates = [_convert_fmp_estimate_row(r) for r in chosen]
    if not estimates:
        return AdapterResult.failed(code=ErrorCode.NOT_FOUND,
                                    detail=f"FMP returned no analyst estimates for {ticker}",
                                    source=src, retryable=False)
    from scripts.sources.financial_datasets import _ANALYST_NUMERIC_FIELDS
    data = emit_with_numeric_coerce(
        {"estimates": estimates, "count": len(estimates), "period": "quarterly"},
        numeric_fields=_ANALYST_NUMERIC_FIELDS,
    )
    return AdapterResult.passed(data, meta={"source_hint": "fmp_analyst_estimates"})


def fetch_earnings_from_fmp(ticker: str, *, fmp_api_key: str = "") -> AdapterResult:
    """Fetch earnings-surprises (+ calendar for revenue) from FMP and emit the
    FDS earnings snapshot dict (bare dict, matching `fetch_earnings_snapshot`)."""
    src = "fmp.fetch_earnings_from_fmp"
    if not fmp_api_key:
        return AdapterResult.failed(code=ErrorCode.UNAUTHORIZED,
                                    detail="fmp_api_key not provided", source=src)
    safe_ticker = urllib.parse.quote(ticker, safe='')
    try:
        sur_raw = _fmp_fetch_list(f"earnings-surprises/{safe_ticker}", {}, fmp_api_key)
    except _FmpNonListError as se:
        return AdapterResult.failed(code=ErrorCode.SHAPE_MISMATCH,
                                    detail=str(se), source=src, retryable=False)
    except Exception as e:  # noqa: BLE001 — routed through canonical mapper
        # Pattern S: the return MUST be a direct adapter_error_from_exception
        # constructor call (no wrapper-helper indirection). Scrub + log first.
        from scripts.sources.adapter_result import _scrub_detail
        variants = _fmp_redact_variants(fmp_api_key)
        print(f"    FMP {src} fetch failed: {_scrub_detail(str(e), variants)}",
              file=sys.stderr)
        return adapter_error_from_exception(e, source=src, redact=variants)
    v = validate_api_shape(sur_raw, FMP_EARN_SURPRISE_SHAPE)
    if not v.ok:
        return AdapterResult.failed_from_shape(v, source=src)
    if not sur_raw:
        return AdapterResult.failed(code=ErrorCode.NOT_FOUND,
                                    detail=f"FMP returned no earnings surprises for {ticker}",
                                    source=src, retryable=False)
    # Calendar is best-effort enrichment for revenue; a failure here must NOT
    # sink the EPS snapshot — swallow any error and fall back to no revenue.
    try:
        cal_raw = _fmp_fetch_list(f"historical/earning_calendar/{safe_ticker}",
                                  {"limit": 8}, fmp_api_key)
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        cal_raw = []
    calendar = cal_raw if isinstance(cal_raw, list) else []
    from scripts.sources.financial_datasets import _EARNINGS_NUMERIC_FIELDS
    earnings = _convert_fmp_earnings(sur_raw, calendar)
    data = emit_with_numeric_coerce(earnings, numeric_fields=_EARNINGS_NUMERIC_FIELDS)
    return AdapterResult.passed(data, meta={"source_hint": "fmp_earnings"})


# ---------------------------------------------------------------------------
# Fallback orchestrator
# ---------------------------------------------------------------------------

def _financials_yield_aligned_window(financials_data, ticker: str) -> bool:
    """True iff the 3 statement families yield ≥4 consecutive aligned
    quarters per the canonical DL4 gate — the SAME check the valuation
    producers run downstream. This is what distinguishes "FDS returned data
    but it's the non-consecutive missing-Q4 set" (insufficient) from a
    genuinely complete set."""
    if not isinstance(financials_data, dict):
        return False
    inc = financials_data.get("income_statements") or []
    bal = financials_data.get("balance_sheets") or []
    cf = financials_data.get("cash_flows") or []
    if not (inc and bal and cf):
        return False
    try:
        from scripts.schemas.quarter_window import aligned_quarters
        aligned_quarters(inc, cf, bal, ticker=ticker)
        return True
    except Exception:
        # InsufficientQuartersError / SchemaError / any drift → insufficient.
        return False


@dataclass(frozen=True)
class FmpFallbackOutcome:
    """Result of the FMP fallback pass. `*_data` are the (possibly replaced)
    category payloads; `status_updates` is a {category_status_key: dict} the
    caller merges into `category_statuses`; `fills` is the run_meta audit
    record. Mirrors YfinanceFallbackOutcome's role without cross-module
    mutation."""
    financials_data: Dict
    metrics_data: Dict
    analyst_data: Dict
    earnings_combined: Dict
    fills: Dict = field(default_factory=dict)
    status_updates: Dict = field(default_factory=dict)
    attempted: bool = False

    def to_run_meta_dict(self) -> Dict:
        return {"attempted": self.attempted, "fills": dict(self.fills)}


def _run_fmp_fallback_impl(
    ticker: str,
    *,
    financials_data: Dict,
    metrics_data: Dict,
    analyst_data: Dict,
    earnings_combined: Dict,
    fmp_api_key: str,
    want_financials: bool = False,
    want_metrics: bool = False,
    want_analyst: bool = False,
    want_earnings: bool = False,
    as_of_date: str = "",
    fetch_financials_fn: Optional[Callable] = None,
    fetch_metrics_fn: Optional[Callable] = None,
    fetch_analyst_fn: Optional[Callable] = None,
    fetch_earnings_fn: Optional[Callable] = None,
) -> FmpFallbackOutcome:
    """Back-fill FDS-insufficient categories from FMP.

    Only categories that were actually fetched (`want_*=True`) AND whose FDS
    payload is insufficient are attempted. FMP financials are adopted only if
    they themselves yield a valid aligned-quarter window (never downgrade a
    working FDS set). `as_of_date` (run/system date) is forwarded to the
    analyst fetcher so it surfaces near-term-forward consensus, not the
    far-future tail. The fetch_*_fn params allow DI for offline testing.
    """
    fills: Dict = {}
    status_updates: Dict = {}
    new_fin, new_met = financials_data, metrics_data
    new_ana, new_earn = analyst_data, earnings_combined

    if not fmp_api_key:
        return FmpFallbackOutcome(financials_data, metrics_data, analyst_data,
                                  earnings_combined, fills={}, status_updates={},
                                  attempted=False)

    fetch_financials_fn = fetch_financials_fn or fetch_financials_from_fmp
    fetch_metrics_fn = fetch_metrics_fn or fetch_metrics_from_fmp
    fetch_analyst_fn = fetch_analyst_fn or fetch_analyst_estimates_from_fmp
    fetch_earnings_fn = fetch_earnings_fn or fetch_earnings_from_fmp

    # --- Financials (income/balance/cash) ---------------------------------
    if want_financials and not _financials_yield_aligned_window(financials_data, ticker):
        res = fetch_financials_fn(ticker, fmp_api_key=fmp_api_key)
        if res.ok and _financials_yield_aligned_window(res.data, ticker):
            new_fin = res.data
            inc = res.data.get("income_statements", [])
            status_updates["financials"] = {
                "status": "PASSED",
                "data_source": "fmp_fallback",
                "income_count": len(inc),
                "balance_count": len(res.data.get("balance_sheets", [])),
                "cashflow_count": len(res.data.get("cash_flows", [])),
                "latest_period": inc[0].get("report_period") if inc else None,
            }
            fills["financials"] = {"filled": True,
                                   "income_filled": len(inc)}
        else:
            reason = (res.error.code.value if (not res.ok and res.error)
                      else "fmp_window_insufficient")
            fills["financials"] = {"filled": False, "reason": reason}

    # --- Metrics snapshot --------------------------------------------------
    _metrics_insufficient = (
        not isinstance(metrics_data, dict) or not metrics_data
        or metrics_data.get("market_cap") is None
    )
    if want_metrics and _metrics_insufficient:
        res = fetch_metrics_fn(ticker, fmp_api_key=fmp_api_key)
        if res.ok and isinstance(res.data, dict) and res.data.get("market_cap") is not None:
            # codex 2026-05-29 (consistency hardening): field-level merge, not
            # wholesale replace — mirrors the yfinance metrics fallback and the
            # analyst-path yfinance_analyst preservation. FMP is the primary
            # source here (FDS was insufficient), but if the prior FDS snapshot
            # carried any non-null field FMP leaves null (e.g. a P/E from
            # price/EPS when FDS only lacked market_cap), keep it rather than
            # dropping it. Never loses data; closes the wholesale-replace
            # asymmetry.
            new_met = dict(res.data)
            if isinstance(metrics_data, dict):
                for _k, _v in metrics_data.items():
                    if new_met.get(_k) is None and _v is not None:
                        new_met[_k] = _v
            status_updates["metrics"] = {"status": "PASSED", "data_source": "fmp_fallback"}
            fills["metrics"] = {"filled": True}
        else:
            reason = (res.error.code.value if (not res.ok and res.error)
                      else "fmp_metrics_insufficient")
            fills["metrics"] = {"filled": False, "reason": reason}

    # --- Analyst estimates -------------------------------------------------
    _analyst_insufficient = (
        not isinstance(analyst_data, dict) or not analyst_data.get("estimates")
    )
    if want_analyst and _analyst_insufficient:
        res = fetch_analyst_fn(ticker, fmp_api_key=fmp_api_key, as_of_date=as_of_date)
        if res.ok and isinstance(res.data, dict) and res.data.get("estimates"):
            # Preserve any additive yfinance_analyst sub-dict already present.
            merged = dict(res.data)
            if isinstance(analyst_data, dict) and "yfinance_analyst" in analyst_data:
                merged["yfinance_analyst"] = analyst_data["yfinance_analyst"]
            new_ana = merged
            status_updates["analyst_estimates"] = {
                "status": "PASSED",
                "data_source": "fmp_fallback",
                "count": res.data.get("count", 0),
            }
            fills["analyst"] = {"filled": True, "count": res.data.get("count", 0)}
        else:
            reason = (res.error.code.value if (not res.ok and res.error)
                      else "fmp_analyst_insufficient")
            fills["analyst"] = {"filled": False, "reason": reason}

    # --- Earnings (completion: FDS often returns a row with null EPS) ------
    _e = earnings_combined.get("earnings") if isinstance(earnings_combined, dict) else None
    _earnings_insufficient = (
        not isinstance(_e, dict) or not _e
        or (_e.get("actual_eps") is None and _e.get("estimated_eps") is None)
    )
    if want_earnings and _earnings_insufficient:
        res = fetch_earnings_fn(ticker, fmp_api_key=fmp_api_key)
        if (res.ok and isinstance(res.data, dict)
                and (res.data.get("actual_eps") is not None
                     or res.data.get("estimated_eps") is not None)):
            new_earn = dict(earnings_combined) if isinstance(earnings_combined, dict) else {}
            new_earn["earnings"] = res.data
            # Leave the combined-level currency as the caller set it (from FDS
            # earnings when present; None/unknown when FDS was starved). We do
            # NOT backfill it from the statement currency: FMP earnings EPS for
            # an ADR is typically per-ADR in USD while the statements are in the
            # native currency (e.g. JPY) — copying the statement currency would
            # mislabel it. None (honest unknown) is the fail-safe choice; we
            # never assume USD. (codex 2026-05-29)
            status_updates["earnings"] = {"status": "PARTIAL", "data_source": "fmp_fallback"}
            fills["earnings"] = {"filled": True}
        else:
            reason = (res.error.code.value if (not res.ok and res.error)
                      else "fmp_earnings_insufficient")
            fills["earnings"] = {"filled": False, "reason": reason}

    return FmpFallbackOutcome(
        financials_data=new_fin,
        metrics_data=new_met,
        analyst_data=new_ana,
        earnings_combined=new_earn,
        fills=fills,
        status_updates=status_updates,
        attempted=True,
    )
