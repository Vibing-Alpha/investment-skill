"""DL2 Adapter Result Envelope.

Canonical return contract for every function in ADAPTER_ENTRYPOINTS.
Consumed by scripts/fetch.py + scripts/macro.py + downstream modules.

See docs/superpowers/specs/2026-04-25-dl2-adapter-envelope-design.md
for the full contract (§Types, §AdapterResult design decisions).
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from scripts.sources.common import (
    HttpError,
    HttpStatusError,
    HttpTransportError,
    SsrfBlockedError,
    ResponseTooLargeError,
    RetryExhaustedError,
)
# Lazy-import `YfRateLimitError` / `YfCallError` inside the fn to avoid
# adapter_result.py → yfinance_guard.py dependency reversal at module load.


# ---------------------------------------------------------------------------
# ErrorCode — canonical error vocabulary (v2 upgrade from Literal to Enum)
# ---------------------------------------------------------------------------

class ErrorCode(str, Enum):
    """Canonical error codes. Subclass of str so JSON serialization of
    `error.code` produces the string value without a custom encoder,
    while Python code gets enum-level runtime validation
    (`ErrorCode(raw_string)` raises ValueError on unknown).
    """
    SHAPE_MISMATCH     = "shape_mismatch"      # validate_api_shape reported errors
    HTTP_TRANSPORT     = "http_transport"      # DL1 HttpTransportError / RetryExhaustedError
    HTTP_STATUS        = "http_status"         # 4xx/5xx from http_get (caller escalated)
    RATE_LIMIT         = "rate_limit"          # 429 or yfinance YfRateLimitError
    NOT_FOUND          = "not_found"           # 404 or upstream "no data"
    UNAUTHORIZED       = "unauthorized"        # 401 / 402 / 403
    PARSE_ERROR        = "parse_error"         # JSON decode failure
    SSRF_BLOCKED       = "ssrf_blocked"        # DL1 SsrfBlockedError
    RESPONSE_TOO_LARGE = "response_too_large"  # DL1 ResponseTooLargeError
    UPSTREAM_ERROR     = "upstream_error"      # catch-all upstream-acknowledged
    INTERNAL_ERROR     = "internal_error"      # bug in adapter itself


# ---------------------------------------------------------------------------
# AdapterError — envelope error detail
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdapterError:
    code: ErrorCode
    detail: str
    source: str
    retryable: bool
    shape_errors: tuple[str, ...] = ()
    upstream_status: int | None = None
    cause: str | None = None

    def __post_init__(self):
        # ISS-002: coerce/validate `code` to ErrorCode at construction.
        # ErrorCode is `str, Enum`, so passing a raw "not_found" string
        # produces a hybrid that breaks `result.error.code.value` reads.
        # Coerce-then-reject: try ErrorCode(self.code) for back-compat,
        # raise if the string is not a canonical value.
        if not isinstance(self.code, ErrorCode):
            try:
                object.__setattr__(self, "code", ErrorCode(self.code))
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"AdapterError.code must be ErrorCode (or its string "
                    f"value); got {self.code!r}"
                ) from exc
        # ISS-045 (Loop3): runtime-validate the remaining contract fields.
        # Type hints are documentation, not enforcement.
        # `AdapterError(code=..., detail=None, source=123, retryable="no")`
        # would have constructed a malformed envelope — consumer reads
        # then crashed at the .lower() / `if retryable` boundary.
        if not isinstance(self.detail, str):
            raise ValueError(
                f"AdapterError.detail must be str, "
                f"got {type(self.detail).__name__}"
            )
        if not isinstance(self.source, str):
            raise ValueError(
                f"AdapterError.source must be str, "
                f"got {type(self.source).__name__}"
            )
        if not isinstance(self.retryable, bool):
            raise ValueError(
                f"AdapterError.retryable must be bool, "
                f"got {type(self.retryable).__name__}"
            )
        if self.upstream_status is not None:
            # bool is int subclass — reject explicitly so True doesn't
            # masquerade as upstream_status=1.
            if isinstance(self.upstream_status, bool) or not isinstance(self.upstream_status, int):
                raise ValueError(
                    f"AdapterError.upstream_status must be int|None, "
                    f"got {type(self.upstream_status).__name__}"
                )
        if self.cause is not None and not isinstance(self.cause, str):
            raise ValueError(
                f"AdapterError.cause must be str|None, "
                f"got {type(self.cause).__name__}"
            )
        # Coerce shape_errors to tuple (frozen invariant; resists list mutation
        # post-construction even though @dataclass(frozen=True) would block
        # rebinding the field name).
        # ISS-032 (Loop2 backlog): str is iterable but its tuple-coercion
        # silently char-splits ("bad" → ("b","a","d")). Reject str explicitly
        # and validate all entries are str — shape_errors is part of the
        # cross-layer diagnostic contract (validate_api_shape feeds it via
        # failed_from_shape), and non-str entries would format malformed
        # error messages downstream.
        # ISS-058 (Loop4): also reject dict/set/None/other iterables
        # explicitly. `tuple({"k": "v"})` silently produces `("k",)` —
        # value lost. Only tuple/list/empty-tuple-default are valid.
        if isinstance(self.shape_errors, str):
            raise ValueError(
                "AdapterError.shape_errors must be tuple/list of str; "
                "got str (would char-split)"
            )
        if not isinstance(self.shape_errors, (tuple, list)):
            raise ValueError(
                f"AdapterError.shape_errors must be tuple or list, "
                f"got {type(self.shape_errors).__name__}"
            )
        if not isinstance(self.shape_errors, tuple):
            object.__setattr__(self, "shape_errors", tuple(self.shape_errors))
        for entry in self.shape_errors:
            if not isinstance(entry, str):
                raise ValueError(
                    f"AdapterError.shape_errors entries must be str, "
                    f"got {type(entry).__name__}"
                )

    @classmethod
    def from_child_fields(
        cls,
        *,
        primary: "AdapterError",
        code: ErrorCode,
        source: str,
        detail: str,
        retryable: bool,
        additional_shape_errors: tuple[str, ...] = (),
    ) -> "AdapterError":
        """ISS-220 (SF-A, Loop32 cycle 2): aggregator-friendly factory.
        Caller has computed code/detail/retryable from inspecting
        multiple children but holds a reference to the **primary**
        (min-severity / chosen) child error. This factory pulls
        ``upstream_status`` / ``cause`` / ``shape_errors`` from the
        primary so diagnostics aren't dropped during aggregation.

        Use case: ``fetch.py`` filing aggregation (PARTIAL + FAILED
        paths) builds ``primary_code`` / ``primary_detail`` /
        ``primary_retryable`` from a min-severity child but still
        wants to preserve the primary's metadata. Pre-SF-A this
        path constructed ``AdapterError(code=primary_code, ...)``
        with no ``upstream_status``/``cause``/``shape_errors`` —
        Pattern P7 (Loop32 codex review).

        Aggregators may pass additional shape_errors entries (e.g.
        synthesis of multiple shape failures); they get appended to
        primary.shape_errors.
        """
        return cls(
            code=code,
            detail=detail,
            source=source,
            retryable=retryable,
            upstream_status=primary.upstream_status,
            cause=primary.cause,
            shape_errors=primary.shape_errors + tuple(additional_shape_errors),
        )


# ---------------------------------------------------------------------------
# _coerce_meta — module-level helper for meta.truncated tri-valued coercion
# ---------------------------------------------------------------------------

def _coerce_meta(meta: dict, *, stacklevel: int = 3) -> dict:
    """Coerce legacy `meta.truncated = True` to `"possible"` with
    FutureWarning. Called by every AdapterResult classmethod before
    constructing the instance, AND by `__post_init__` for direct-
    constructor parity.

    Frame stacks at warn site:
    - classmethod path (3 frames):
        user_call → classmethod (passed/partial/failed) → _coerce_meta → warn
        stacklevel=3 → user_call (default)
    - direct constructor + __post_init__ path (4 frames):
        user_call → AdapterResult.__init__ (auto) → __post_init__ → _coerce_meta → warn
        stacklevel=4 → user_call (caller passes stacklevel=4)

    ISS-059 (Loop4 backlog): pre-fix hardcoded stacklevel=3, so direct
    constructor warnings pointed at the dataclass-generated __init__
    instead of the user line.
    """
    if meta.get("truncated") is True:
        warnings.warn(
            "meta.truncated=True deprecated; use 'possible' or 'confirmed'",
            FutureWarning,
            stacklevel=stacklevel,
        )
        meta = dict(meta)
        meta["truncated"] = "possible"
    # ISS-069 (Loop5 backlog): whitelist truncated values. Pre-fix
    # accepted "nope" / 0 / arbitrary truthy values, propagating
    # garbage to category_statuses[*]["truncated"]. Now strict:
    # only None / False / "possible" / "confirmed" allowed (True is
    # already coerced above). Use `is` checks + type guards to avoid
    # Python's `0 == False` and `1 == True` equivalence which would
    # otherwise let `0` and `1` slip through `val in (False,)`.
    if "truncated" in meta:
        val = meta["truncated"]
        is_valid = (
            val is None
            or val is False
            or (isinstance(val, str) and val in ("possible", "confirmed"))
        )
        if not is_valid:
            raise ValueError(
                f"meta.truncated must be None / False / 'possible' / "
                f"'confirmed'; got {val!r}"
            )
    return meta


# ---------------------------------------------------------------------------
# ISS-106 (Loop8 cycle 2): JSON-safety validator for envelope data + meta
# ---------------------------------------------------------------------------
# AdapterResult is a persistence + transport object — it lands on disk via
# scripts/fetch.py's category_statuses[*] and feeds LLM prompts. Pre-fix
# direct construction `AdapterResult(status="PASSED", data={"x": {1}, "n":
# float("nan")})` succeeded, then later `json.dumps(result, allow_nan=False)`
# crashed at the persistence layer (or — worse — `allow_nan=True` silently
# emitted `NaN` tokens that some JSON parsers reject).
#
# Allowed types in data + meta (recursive):
#   dict (string keys only) | list | tuple | str | finite int / finite float
#   | bool | None
# Explicitly rejected:
#   NaN / Infinity / -Infinity (non-finite floats)
#   set / frozenset / bytes / custom objects
#   non-string dict keys
#   cyclic references (would infinite-loop on serialization)

_JSON_PRIMITIVES = (str, bool, int, float, type(None))


def _iterative_json_deepcopy(value: Any) -> Any:
    """Iterative deep-copy of dict / list / tuple nodes (mutable JSON
    containers). ISS-125 (Loop9 cycle 1) compatible — no Python
    recursion, so a 1500-level nested structure copies without
    RecursionError. Other JSON-safe primitives (str / int / float /
    bool / None) are returned by reference (immutable, safe to share).
    """
    if isinstance(value, dict):
        root = {}
    elif isinstance(value, list):
        root = []
    elif isinstance(value, tuple):
        # Tuples are immutable, but their contents may be mutable
        # containers — we still need to descend. Build a list buffer
        # and convert to tuple at the end (one container at a time
        # via the work stack — see the tuple-finalize entries below).
        root = []
    else:
        return value

    # Work-stack entries:
    #   ("copy", src, dest_container, key_or_index)
    #     copy `src` into dest[key_or_index]
    #   ("finalize_tuple", src, dest_container, key_or_index, buffer)
    #     replace dest[key_or_index] with tuple(buffer)
    work: list[tuple] = []

    if isinstance(value, dict):
        for k, v in value.items():
            work.append(("copy", v, root, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            root.append(None)
            work.append(("copy", v, root, i))
    else:  # tuple at top level
        for i, v in enumerate(value):
            root.append(None)
            work.append(("copy", v, root, i))

    while work:
        kind = work[-1][0]
        if kind == "copy":
            _, src, dest, key = work.pop()
            if isinstance(src, dict):
                new_dict: dict = {}
                dest[key] = new_dict
                for k, v in src.items():
                    work.append(("copy", v, new_dict, k))
            elif isinstance(src, list):
                new_list: list = [None] * len(src)
                dest[key] = new_list
                for i, v in enumerate(src):
                    work.append(("copy", v, new_list, i))
            elif isinstance(src, tuple):
                buf: list = [None] * len(src)
                # Stage the tuple finalize FIRST so it runs LAST (LIFO).
                work.append(("finalize_tuple", src, dest, key, buf))
                for i, v in enumerate(src):
                    work.append(("copy", v, buf, i))
            else:
                # Primitive (str/int/float/bool/None) — share by reference.
                # Other types will be caught by _validate_json_safe later.
                dest[key] = src
        else:  # "finalize_tuple"
            _, src, dest, key, buf = work.pop()
            dest[key] = tuple(buf)

    if isinstance(value, tuple):
        return tuple(root)
    return root


def _validate_json_safe(value: Any, *, path: str = "$") -> None:
    """Raise ValueError if *value* contains a non-JSON-serializable
    member. Iterative implementation with explicit work stack to avoid
    Python's default 1000-frame recursion limit on deeply nested but
    valid JSON (ISS-125, Loop9 cycle 1). Detects cycles via id-tracking
    on container ancestors. Tuples are treated as lists — both
    serialize the same way.

    Cycle detection: each container pushes its id onto the ancestor
    set when its children are pushed; ancestors pop when fully
    consumed. A child whose id is already in the ancestor set is
    cyclic. We use a parallel "open" stack of ids tracking which
    ancestors are still on the work stack.
    """
    # Stack entries: ("validate", value, path) | ("close_container", id_)
    work: list[tuple] = [("validate", value, path)]
    open_ids: set[int] = set()

    while work:
        entry = work.pop()
        kind = entry[0]
        if kind == "close_container":
            # Pop ancestor id when done with its subtree
            open_ids.discard(entry[1])
            continue

        # kind == "validate"
        _, val, p = entry

        if isinstance(val, dict):
            vid = id(val)
            if vid in open_ids:
                raise ValueError(
                    f"AdapterResult JSON-safety: cyclic reference at {p}"
                )
            open_ids.add(vid)
            # Schedule cleanup of this container's id AFTER all its
            # children are processed.
            work.append(("close_container", vid))
            for k, v in val.items():
                if not isinstance(k, str):
                    raise ValueError(
                        f"AdapterResult JSON-safety: dict key at {p} must "
                        f"be str, got {type(k).__name__} ({k!r})"
                    )
                work.append(("validate", v, f"{p}.{k}"))
            continue

        if isinstance(val, (list, tuple)):
            vid = id(val)
            if vid in open_ids:
                raise ValueError(
                    f"AdapterResult JSON-safety: cyclic reference at {p}"
                )
            open_ids.add(vid)
            work.append(("close_container", vid))
            for i, item in enumerate(val):
                work.append(("validate", item, f"{p}[{i}]"))
            continue

        # Reject set / frozenset / bytes / other containers explicitly.
        if isinstance(val, (set, frozenset, bytes, bytearray)):
            raise ValueError(
                f"AdapterResult JSON-safety: {type(val).__name__} at "
                f"{p} is not JSON-serializable"
            )
        if isinstance(val, bool):
            # bool BEFORE int (bool is int subclass). Always JSON-safe.
            continue
        if isinstance(val, (int, float)):
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError(
                    f"AdapterResult JSON-safety: non-finite float at {p} "
                    f"({val!r}) is not JSON-serializable"
                )
            continue
        if val is None or isinstance(val, str):
            continue
        raise ValueError(
            f"AdapterResult JSON-safety: {type(val).__name__} at {p} "
            f"is not a JSON-serializable type"
        )


# ---------------------------------------------------------------------------
# AdapterResult — canonical adapter return envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)   # eq=False avoids auto-__hash__ on unhashable data: dict
class AdapterResult:
    status: Literal["PASSED", "PARTIAL", "FAILED"]
    data: dict[str, Any]             # canonical; list-returning adapters wrap as {"items": [...]}
    error: AdapterError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # -- Invariants -------------------------------------------------------

    def __post_init__(self):
        # ISS-003: type-check data and meta. The dataclass type-hint
        # is documentation, not runtime enforcement; direct constructor
        # `AdapterResult(status="PASSED", data=[], meta=[])` was accepted.
        if not isinstance(self.data, dict):
            raise ValueError(
                f"AdapterResult.data must be dict, got {type(self.data).__name__}"
            )
        if not isinstance(self.meta, dict):
            raise ValueError(
                f"AdapterResult.meta must be dict, got {type(self.meta).__name__}"
            )
        # ISS-036 (Loop2 backlog): direct constructor also runs
        # _coerce_meta so legacy `truncated=True` is coerced to "possible"
        # uniformly. classmethods already do this; without the
        # __post_init__ call, `AdapterResult(status="PASSED", data={},
        # meta={"truncated": True})` slipped past the contract.
        # ISS-059 (Loop4 backlog): use stacklevel=4 here so the
        # FutureWarning points at the user's `AdapterResult(...)` call
        # site, not at the dataclass-generated __init__.
        if "truncated" in self.meta:
            coerced = _coerce_meta(dict(self.meta), stacklevel=4)
            if coerced is not self.meta:
                object.__setattr__(self, "meta", coerced)
        # Spec invariant 4: PASSED iff error is None; PARTIAL/FAILED require error.
        # Belt-and-suspenders for the classmethod convention — catches direct
        # constructor bypass at runtime instead of trusting callers.
        if self.status == "PASSED":
            if self.error is not None:
                raise ValueError(
                    "AdapterResult invariant: status='PASSED' requires error=None"
                )
        elif self.status in ("PARTIAL", "FAILED"):
            if self.error is None:
                raise ValueError(
                    f"AdapterResult invariant: status={self.status!r} requires error"
                )
            # ISS-031 (Loop2): also check error is the right TYPE.
            # Pre-fix accepted error="bad" or error={"code": "x"} since
            # `not None` was the only check. Downstream `result.error.code`
            # then crashes at the consumer layer instead of here.
            if not isinstance(self.error, AdapterError):
                raise ValueError(
                    f"AdapterResult invariant: status={self.status!r} requires "
                    f"error to be AdapterError, got {type(self.error).__name__}"
                )
        else:
            raise ValueError(
                f"AdapterResult.status must be PASSED/PARTIAL/FAILED, got {self.status!r}"
            )
        # ISS-106 (Loop8 cycle 2): JSON-safety check on data + meta.
        # Catches non-finite floats, sets, non-string dict keys, and
        # cycles at construction time instead of at the persistence
        # boundary where the failure mode is "category_statuses fails
        # to serialize" or — worse — silently emits NaN tokens.
        #
        # ISS-122 (Loop9 cycle 1): deep-copy data + meta so the envelope
        # OWNS the structure. Pre-fix `dict(data)` was a shallow copy —
        # nested lists/dicts shared references with the caller, which
        # could mutate them after construction (`r.data["x"].append({1})`)
        # and silently break the invariant. The iterative deep-copy
        # means subsequent mutation on the caller's copy can't reach
        # into the envelope.
        #
        # Iterative (not `copy.deepcopy`) so deeply-nested but valid
        # JSON doesn't trip Python's default 1000-frame recursion
        # ceiling — keeps ISS-125 (deep-nesting tolerance) intact.
        #
        # ISS-138 (Loop11 cycle 1): _validate_json_safe MUST run BEFORE
        # _iterative_json_deepcopy. The deepcopy has no cycle tracking;
        # a cyclic input (`d["self"] = d`) would loop forever, exhausting
        # memory by cloning the cycle into ever-deeper nested dicts. The
        # validator (lines 290-345) has ancestor-id tracking and raises
        # `ValueError("AdapterResult JSON-safety: cyclic reference at ...")`
        # at boundary, which was silently broken when ISS-122 reordered the
        # operations. The cycle test (test_iss106_adapter_result_rejects_
        # cyclic_data) was hanging in CI as a result. Running validate
        # first preserves both invariants: cycle rejection + envelope
        # ownership (deepcopy operates on validated input).
        _validate_json_safe(self.data, path="data")
        _validate_json_safe(self.meta, path="meta")
        object.__setattr__(
            self, "data", _iterative_json_deepcopy(self.data),
        )
        object.__setattr__(
            self, "meta", _iterative_json_deepcopy(self.meta),
        )

    # -- Constructors -----------------------------------------------------

    @classmethod
    def passed(cls, data: dict, *, meta: dict | None = None) -> "AdapterResult":
        # ISS-022 fix: classmethod must reject non-dict data/meta. Pre-fix
        # `dict(data)` silently coerced an empty list to {} (because dict([])
        # is {}), bypassing the __post_init__ isinstance(data, dict) guard.
        # Reject coercible non-dict types here — the type hint is contract,
        # not a coercion suggestion.
        if not isinstance(data, dict):
            raise TypeError(
                f"AdapterResult.passed: data must be dict, "
                f"got {type(data).__name__}"
            )
        if meta is not None and not isinstance(meta, dict):
            raise TypeError(
                f"AdapterResult.passed: meta must be dict or None, "
                f"got {type(meta).__name__}"
            )
        return cls(
            status="PASSED",
            data=dict(data),
            error=None,
            meta=_coerce_meta(dict(meta or {})),
        )

    @classmethod
    def partial(cls, data: dict, *, error: AdapterError,
                meta: dict | None = None) -> "AdapterResult":
        # ISS-022 fix: same as `passed` — reject non-dict at boundary.
        if not isinstance(data, dict):
            raise TypeError(
                f"AdapterResult.partial: data must be dict, "
                f"got {type(data).__name__}"
            )
        if meta is not None and not isinstance(meta, dict):
            raise TypeError(
                f"AdapterResult.partial: meta must be dict or None, "
                f"got {type(meta).__name__}"
            )
        return cls(
            status="PARTIAL",
            data=dict(data),
            error=error,
            meta=_coerce_meta(dict(meta or {})),
        )

    @classmethod
    def failed(cls, *,
               code: ErrorCode,
               detail: str,
               source: str,
               retryable: bool = False,
               shape_errors: tuple[str, ...] = (),
               upstream_status: int | None = None,
               cause: str | None = None,
               data: dict | None = None,
               meta: dict | None = None) -> "AdapterResult":
        # ISS-022 fix: data=None is documented as "use empty dict";
        # any other non-dict must be rejected.
        if data is not None and not isinstance(data, dict):
            raise TypeError(
                f"AdapterResult.failed: data must be dict or None, "
                f"got {type(data).__name__}"
            )
        if meta is not None and not isinstance(meta, dict):
            raise TypeError(
                f"AdapterResult.failed: meta must be dict or None, "
                f"got {type(meta).__name__}"
            )
        return cls(
            status="FAILED",
            data=dict(data or {}),
            error=AdapterError(
                code=code, detail=detail, source=source,
                retryable=retryable, shape_errors=shape_errors,
                upstream_status=upstream_status, cause=cause,
            ),
            meta=_coerce_meta(dict(meta or {})),
        )

    @classmethod
    def failed_from_child(
        cls,
        child_error: "AdapterError",
        *,
        source: str,
        detail: str | None = None,
        data: dict | None = None,
        meta: dict | None = None,
    ) -> "AdapterResult":
        """ISS-220 (SF-A, Loop32 cycle 2): preserve full child AdapterError
        metadata when re-emitting at a new source. Pre-SF-A pattern P7
        was: ``AdapterResult.failed(code=child.error.code, detail=...,
        source=src, retryable=child.error.retryable)`` — silently
        dropped ``cause`` / ``shape_errors`` / ``upstream_status``.
        Three loops in a row (28/29/30/31/32) found new instances of
        this pattern.

        Note on warnings: this helper delegates to ``cls.failed`` which
        passes ``meta`` through ``_coerce_meta`` (raises
        ``FutureWarning`` on legacy ``meta.truncated=True``). The
        warning's stacklevel points at this helper, not the caller's
        line. This is acceptable because the warning surfaces only on
        legacy fixture migrations (no production caller emits the
        deprecated form), and the warning text identifies the
        deprecated key unambiguously.
        """
        return cls.failed(
            code=child_error.code,
            detail=child_error.detail if detail is None else detail,
            source=source,
            retryable=child_error.retryable,
            shape_errors=child_error.shape_errors,
            upstream_status=child_error.upstream_status,
            cause=child_error.cause,
            data=data,
            meta=meta,
        )

    @classmethod
    def partial_from_child(
        cls,
        child_error: "AdapterError",
        *,
        data: dict,
        source: str,
        detail: str | None = None,
        meta: dict | None = None,
    ) -> "AdapterResult":
        """ISS-220 (SF-A, Loop32 cycle 2): PARTIAL variant of
        ``failed_from_child``. Caller has salvaged some data from
        children but the primary cause was a child failure — re-emit
        as PARTIAL with full metadata preservation. Used by aggregators
        (``fetch.py`` filing aggregation).
        """
        new_error = AdapterError(
            code=child_error.code,
            detail=child_error.detail if detail is None else detail,
            source=source,
            retryable=child_error.retryable,
            shape_errors=child_error.shape_errors,
            upstream_status=child_error.upstream_status,
            cause=child_error.cause,
        )
        return cls.partial(data=data, error=new_error, meta=meta)

    @classmethod
    def failed_from_shape(cls, v: Any, *, source: str) -> "AdapterResult":
        """Consolidator for SHAPE_MISMATCH failures. `v` is a
        `ValidationResult` from `scripts.sources.api_shapes`.

        Precondition: `v.ok is False and len(v.errors) >= 1`. The
        `ValidationResult` contract (api_shapes.py, Task 3 Step 4)
        guarantees non-empty errors whenever `ok=False`. Empty-errors
        on `ok=False` is a producer bug we catch loudly instead of
        silently constructing a malformed detail string.

        ISS-107 (Loop8 cycle 2): also reject malformed `v` inputs.
        Pre-fix passing `v` with `errors="boom"` produced
        `shape_errors=("b","o","o","m")` (str char-split via tuple()),
        and an `ok=True` `ValidationResult` was happily downgraded to
        FAILED. Now we duck-type the contract: must have `ok` and
        `errors`, ok must be False, errors must be a non-empty
        sequence of strings — no str, no None.
        """
        # Duck-typing: not all callers import the ValidationResult class
        # directly, but every well-formed instance exposes ok + errors.
        if not (hasattr(v, "ok") and hasattr(v, "errors")):
            raise AssertionError(
                "AdapterResult.failed_from_shape: argument must be a "
                "ValidationResult-like object (with .ok and .errors); "
                f"got {type(v).__name__!r}"
            )
        if v.ok is not False:
            raise AssertionError(
                "AdapterResult.failed_from_shape: ValidationResult.ok "
                f"must be False; got {v.ok!r}"
            )
        # Reject str: tuple(str) char-splits silently.
        if isinstance(v.errors, str) or v.errors is None:
            raise AssertionError(
                "AdapterResult.failed_from_shape: ValidationResult.errors "
                "must be a non-empty sequence of strings; "
                f"got {type(v.errors).__name__!r}"
            )
        errors = tuple(v.errors)
        if not errors:
            raise AssertionError(
                "AdapterResult.failed_from_shape: ValidationResult "
                "has ok=False but errors tuple is empty — "
                "api_shapes.validate_api_shape contract violation"
            )
        if not all(isinstance(e, str) for e in errors):
            raise AssertionError(
                "AdapterResult.failed_from_shape: every error must be "
                f"a string; got types "
                f"{sorted({type(e).__name__ for e in errors})}"
            )
        detail = errors[0] if len(errors) == 1 else f"{errors[0]} [+ {len(errors) - 1} more]"
        return cls.failed(
            code=ErrorCode.SHAPE_MISMATCH,
            detail=detail,
            source=source,
            shape_errors=errors,
            retryable=False,
        )

    # -- Properties -------------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.status == "PASSED"


# ---------------------------------------------------------------------------
# adapter_error_from_exception — canonical DL1→DL2 exception mapper
# ---------------------------------------------------------------------------

# ISS-220 SF-D (Loop33 cycle 1): _scrub_detail promoted to
# common._scrub_detail so cross-module callers (yfinance scrub
# composition in this mapper, yahoo_finance._yfinance_safe_msg) don't
# need cross-module imports back into adapter_result.py.
# Re-export for back-compat (existing callers in scripts/sources/fmp.py
# and tests).
from scripts.sources.common import _scrub_detail  # noqa: E402,F401


def adapter_error_from_exception(
    exc: Exception, *, source: str, redact: tuple[str, ...] = (),
    data: dict | None = None,
) -> AdapterResult:
    """Canonical DL1→DL2 exception mapping per spec §Types mapping
    table (spec:1131-1139). Every adapter template's top-level
    try/except should end in:

        except Exception as e:
            return adapter_error_from_exception(e, source=src)

    This centralizes the 9-row mapping (1 input-shape via
    `failed_from_shape` + 8 exception rows here); adapter templates
    don't need per-branch-case correctness audits (Codex round 15 H3
    exposed 20+ templates drifting independently).

    *redact*: tuple of secret strings to scrub from `error.detail` before
    return. Pass api keys here when the URL embeds them (e.g.,
    `apikey={...}` query params). Empty strings are ignored.

    *data*: optional dict for partial-data-bearing FAILED paths. Used
    by adapters that recover partial output (e.g. sec_edgar fetches
    that succeed for some filings before transport failure on others).
    Default None preserves the canonical no-data envelope for the
    typical "exception → drop everything" path. Added in SF-A
    (Loop32 cycle 2 ISS-220) so sec_edgar.py:543 can route its
    RetryExhausted handler through this canonical mapper without
    losing the `{items_metadata, content_dict}` partial dict.
    """
    # Lazy imports for yfinance exceptions (see module-top comment)
    try:
        from scripts.sources.yfinance_guard import (
            YfRateLimitError, YfCallError,
        )
    except ImportError:
        YfRateLimitError = YfCallError = type("_Sentinel", (), {})

    # ISS-001: scrub MUST happen before truncation. Otherwise a secret
    # crossing the 400-char boundary gets cut to a prefix that no longer
    # str-matches `redact`, leaking that prefix into error.detail. Order:
    # scrub the full str(exc) first (matching is exact), then cap to 400.
    #
    # ISS-220 4.12 (Loop33 cycle 1): for yfinance exceptions
    # (YfRateLimitError / YfCallError), apply yfinance-specific scrub
    # FIRST (cookies / crumbs / cache home-paths) THEN the caller-supplied
    # redact tuple (API keys). Two-layer composition so caller's redact
    # still applies AND yfinance-internal sensitive substrings don't leak.
    detail_raw = str(exc)
    if isinstance(exc, (YfRateLimitError, YfCallError)):
        from scripts.sources.common import _yfinance_scrub
        detail_raw = _yfinance_scrub(detail_raw)
    detail = _scrub_detail(detail_raw, redact)[:400]
    cause_name = type(exc).__name__

    # Row 2: RetryExhaustedError (429 → RATE_LIMIT; SEC 403 → RATE_LIMIT;
    # other → HTTP_TRANSPORT).
    # ISS-215 (Loop31 cycle 1 fresh-session-18): SEC_POLICY treats 403 as
    # rate-limit signal (SEC EDGAR returns 403 when crawler exceeds ~10
    # req/s) — `_SEC_RETRY_ON = frozenset({403, 408, 429, ...})`. Pre-fix
    # the mapper only matched status==429 to RATE_LIMIT; sustained SEC 403
    # ratelimits exhausted retries and emerged as HTTP_TRANSPORT, hiding
    # the actionable rate-limit cause from operators. Loop29 ISS-203 fixed
    # the symmetric finnhub case (429) at the call site; SEC needs the
    # canonical mapper since multiple SEC adapter sites surface
    # RetryExhausted. Match on (host, status) so the SEC-specific
    # semantics are localized to the host-check rather than mutating
    # generic mapper behavior for any policy with a custom retry_on set.
    if isinstance(exc, RetryExhaustedError):
        status = getattr(exc, "status", None)
        url = getattr(exc, "url", "") or ""
        host = ""
        try:
            import urllib.parse as _ulp
            host = (_ulp.urlparse(url).hostname or "").lower()
        except Exception:  # defensive — host is best-effort
            host = ""
        sec_rate_limit = (
            status == 403
            and (host == "sec.gov" or host.endswith(".sec.gov"))
        )
        if status == 429 or sec_rate_limit:
            return AdapterResult.failed(
                code=ErrorCode.RATE_LIMIT, detail=detail,
                source=source, retryable=True,
                upstream_status=status, cause="RetryExhaustedError",
                data=data,
            )
        return AdapterResult.failed(
            code=ErrorCode.HTTP_TRANSPORT, detail=detail,
            source=source, retryable=True,
            upstream_status=status, cause="RetryExhaustedError",
            data=data,
        )
    # Row 3: HttpStatusError (4xx/5xx surfaced explicitly by adapter)
    # Split: 401/402/403 → UNAUTHORIZED, 404 → NOT_FOUND, 5xx → UPSTREAM_ERROR,
    # other 4xx → HTTP_STATUS. Wires the previously-unproduced HTTP_STATUS code.
    if isinstance(exc, HttpStatusError):
        status = getattr(exc, "status", None)
        if status in (401, 402, 403):
            code = ErrorCode.UNAUTHORIZED
            retryable = False
        elif status == 404:
            code = ErrorCode.NOT_FOUND
            retryable = False
        elif status == 429:
            code = ErrorCode.RATE_LIMIT
            retryable = True
        elif status is not None and 500 <= status < 600:
            code = ErrorCode.UPSTREAM_ERROR
            retryable = True
        else:
            code = ErrorCode.HTTP_STATUS
            retryable = False
        return AdapterResult.failed(
            code=code, detail=detail,
            source=source, retryable=retryable,
            upstream_status=status, cause="HttpStatusError",
            data=data,
        )
    # Row 4: HttpTransportError
    if isinstance(exc, HttpTransportError):
        return AdapterResult.failed(
            code=ErrorCode.HTTP_TRANSPORT, detail=detail,
            source=source, retryable=True, cause=cause_name,
            data=data,
        )
    # Row 5: SsrfBlockedError
    if isinstance(exc, SsrfBlockedError):
        return AdapterResult.failed(
            code=ErrorCode.SSRF_BLOCKED, detail=detail,
            source=source, retryable=False, cause="SsrfBlockedError",
            data=data,
        )
    # Row 6: ResponseTooLargeError
    if isinstance(exc, ResponseTooLargeError):
        return AdapterResult.failed(
            code=ErrorCode.RESPONSE_TOO_LARGE, detail=detail,
            source=source, retryable=False, cause="ResponseTooLargeError",
            data=data,
        )
    # Row 7: yfinance YfRateLimitError
    if isinstance(exc, YfRateLimitError):
        return AdapterResult.failed(
            code=ErrorCode.RATE_LIMIT, detail=detail,
            source=source, retryable=True, cause="YfRateLimitError",
            data=data,
        )
    # Row 8: yfinance YfCallError
    if isinstance(exc, YfCallError):
        return AdapterResult.failed(
            code=ErrorCode.UPSTREAM_ERROR, detail=detail,
            source=source, retryable=True, cause="YfCallError",
            data=data,
        )
    # Row 8.5: ShapeError (subclass of SchemaError → ValueError) maps to
    # SHAPE_MISMATCH. Must come before the generic ValueError row below;
    # otherwise a producer that raises ShapeError gets miscoded as
    # PARSE_ERROR (which has lower severity, so dual-error severity
    # ranking would also pick wrong on a daily=ShapeError + weekly=429
    # combo). ISS-142 (Loop14 cycle 1 fresh-session) made this
    # explicit when fetch_historical_prices added inline OHLCV shape
    # validation that raises ShapeError.
    try:
        from scripts.sources.api_shapes import ShapeError as _ShapeError
        if isinstance(exc, _ShapeError):
            return AdapterResult.failed(
                code=ErrorCode.SHAPE_MISMATCH, detail=detail,
                source=source, retryable=False, cause=cause_name,
                data=data,
            )
    except ImportError:
        pass
    # Row 8.6: MissingApiKeyError (auth/config) maps to UNAUTHORIZED.
    # ISS-164 (Loop20 cycle 1 fresh-session-7): pre-fix the
    # RuntimeError raised by _get_api_key fell through to Row 10
    # catch-all (INTERNAL_ERROR), making an auth-config issue look
    # like an adapter bug.
    try:
        from scripts.sources.common import MissingApiKeyError as _MissingApiKeyError
        if isinstance(exc, _MissingApiKeyError):
            return AdapterResult.failed(
                code=ErrorCode.UNAUTHORIZED, detail=detail,
                source=source, retryable=False, cause=cause_name,
                data=data,
            )
    except ImportError:
        pass
    # Row 8.7: YahooNoDataError → NOT_FOUND. ISS-220 4.15 (Loop34 cycle 1).
    # Yahoo's empty `chart.result` response is upstream "no data for this
    # ticker", not a JSON parse failure. Pre-fix the plain `ValueError`
    # was caught by Row 9 → PARSE_ERROR, misleading downstream consumers
    # about whether to retry or surface "ticker not found." Subclass
    # of ValueError so this row MUST come before Row 9.
    try:
        from scripts.sources.yahoo_finance import (
            YahooNoDataError as _YahooNoDataError,
        )
        if isinstance(exc, _YahooNoDataError):
            return AdapterResult.failed(
                code=ErrorCode.NOT_FOUND, detail=detail,
                source=source, retryable=False, cause=cause_name,
                data=data,
            )
    except ImportError:
        pass
    # Row 8.8: YahooApiError → UPSTREAM_ERROR. ISS-220 4.15.
    # `chart.error` non-empty = vendor-acknowledged failure.
    try:
        from scripts.sources.yahoo_finance import (
            YahooApiError as _YahooApiError,
        )
        if isinstance(exc, _YahooApiError):
            return AdapterResult.failed(
                code=ErrorCode.UPSTREAM_ERROR, detail=detail,
                source=source, retryable=True, cause=cause_name,
                data=data,
            )
    except ImportError:
        pass
    # Row 8.9: HttpError catch-all (base class for typed http_get errors
    # not handled by the more-specific rows above). ISS-220 4.20
    # (Loop34 cycle 1): _validate_header_safe raises bare HttpError
    # for invalid header chars (CRLF injection guard). Pre-fix this
    # fell to Row 10 INTERNAL_ERROR, misclassifying a transport-layer
    # contract violation as an adapter bug.
    if isinstance(exc, HttpError):
        return AdapterResult.failed(
            code=ErrorCode.HTTP_TRANSPORT, detail=detail,
            source=source, retryable=False, cause=cause_name,
            data=data,
        )
    # Row 9: parse errors (ValueError / KeyError / JSONDecodeError)
    # Note: JSONDecodeError is a ValueError subclass, so `ValueError`
    # catches both. KeyError is a LookupError (not a ValueError).
    if isinstance(exc, (ValueError, KeyError)):
        return AdapterResult.failed(
            code=ErrorCode.PARSE_ERROR, detail=detail,
            source=source, retryable=False, cause=cause_name,
            data=data,
        )
    # Row 10: any other exception
    return AdapterResult.failed(
        code=ErrorCode.INTERNAL_ERROR, detail=detail,
        source=source, retryable=False, cause=cause_name,
        data=data,
    )


# ---------------------------------------------------------------------------
# ADAPTER_ENTRYPOINTS — canonical whitelist of HTTP adapter functions that
# MUST return AdapterResult. Grep-verified at HEAD b6c2138 via
# `grep -nE '^def [a-z]' scripts/sources/*.py`.
# ---------------------------------------------------------------------------

ADAPTER_ENTRYPOINTS: frozenset[tuple[str, str]] = frozenset({
    # (module_stem, function_name)
    # financial_datasets.py (13)
    ("financial_datasets", "fetch_price_data"),
    ("financial_datasets", "fetch_metrics_data"),
    ("financial_datasets", "fetch_financial_statements"),
    ("financial_datasets", "fetch_company_data"),
    ("financial_datasets", "fetch_news_data"),
    ("financial_datasets", "fetch_segmented_revenues"),
    ("financial_datasets", "fetch_insider_data"),
    ("financial_datasets", "fetch_analyst_estimates"),
    ("financial_datasets", "fetch_earnings_snapshot"),
    ("financial_datasets", "fetch_earnings_press_releases"),
    ("financial_datasets", "fetch_institutional_ownership"),
    ("financial_datasets", "fetch_interest_rates_snapshot"),
    ("financial_datasets", "fetch_interest_rates_historical"),
    # yahoo_finance.py (2)
    ("yahoo_finance", "fetch_yahoo_quote_result"),   # NEW thin wrapper (T8)
    ("yahoo_finance", "fetch_historical_prices"),
    # sec_edgar.py (3)
    ("sec_edgar", "fetch_filing_from_sec_edgar"),
    ("sec_edgar", "lookup_filing_via_sec_submissions"),
    ("sec_edgar", "fetch_filing_items_from_api"),
    # fmp.py (6) — filing metadata/date + 2026-05-29 financial-data fallback
    ("fmp", "_fetch_filing_metadata_from_fmp_impl"),
    ("fmp", "_fetch_filing_date_impl"),
    ("fmp", "fetch_financials_from_fmp"),
    ("fmp", "fetch_metrics_from_fmp"),
    ("fmp", "fetch_analyst_estimates_from_fmp"),
    ("fmp", "fetch_earnings_from_fmp"),
    # adr/detect.py (1) — ISS-098 (Loop8): adr/detect.detect_adr_market_data
    # is a yfinance-calling envelope-returning adapter, structurally
    # equivalent to yahoo_finance.fetch_yahoo_quote_result. Governing it
    # under Pattern S keeps the ADAPTER_ENTRYPOINTS contract honest
    # (no other "detect.py" exists under scripts/sources/, so the bare
    # ("detect", ...) stem key has no collision risk).
    ("detect", "detect_adr_market_data"),
    # ISS-024 (Cycle 4): finnhub.fetch_news removed from whitelist.
    # The real Finnhub fallback is `_fetch_news_finnhub` inside
    # financial_datasets.py, not this stub. Stub now returns FAILED.
})


# ---------------------------------------------------------------------------
# HTTP_INFRASTRUCTURE_ALLOWLIST — non-entrypoint functions that call DL1
# transport primitives. Scanned by test_whitelist_matches_http_callsite_scan.
# Scan scope: http_get / make_request / make_api_request / safe_urlopen
# (yfinance_call explicitly out of scope — yfinance library has its own
#  HTTP stack DL1 does not govern).
# ---------------------------------------------------------------------------

HTTP_INFRASTRUCTURE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    # DL1 primitives
    ("common", "http_get"),
    ("common", "make_request"),
    ("common", "make_api_request"),
    ("common", "safe_urlopen"),
    ("yfinance_guard", "yfinance_call"),  # symmetry; scan excludes it
    # Documented shared primitive (see spec §Compatibility Matrix)
    ("yahoo_finance", "fetch_yahoo_quote"),
    # Adapter-internal HTTP helper wrappers
    ("financial_datasets", "_make_request"),        # wraps common.make_request
    ("financial_datasets", "_fetch_news_finnhub"),  # calls safe_urlopen
    ("sec_edgar", "_resolve_cik"),                  # direct http_get
})


# ---------------------------------------------------------------------------
# ERROR_CODE_SEVERITY — single source of truth for severity ranking
# ---------------------------------------------------------------------------
# ISS-074 (Loop5): pre-fix three independent severity functions in
# fetch._fetch_filing_data_impl, financial_datasets.fetch_financial_statements,
# and yahoo_finance.fetch_historical_prices used inconsistent rankings.
# Filing ranked by ErrorCode (after exception→AdapterError mapping);
# financials/historical ranked by raw exception class. Result: a 400
# HttpStatusError vs 500 HttpStatusError tied in financials but differed
# in filing — same multi-failure scenario could surface different
# error codes depending on which entrypoint computed it.
#
# All three callers should now use `severity_of_error` /
# `severity_of_exception` from this module so the ranking is
# definitionally consistent.
ERROR_CODE_SEVERITY: dict = {
    ErrorCode.SSRF_BLOCKED: 0,
    ErrorCode.RESPONSE_TOO_LARGE: 1,
    ErrorCode.RATE_LIMIT: 2,
    ErrorCode.UNAUTHORIZED: 3,
    ErrorCode.HTTP_TRANSPORT: 4,
    ErrorCode.UPSTREAM_ERROR: 5,
    ErrorCode.HTTP_STATUS: 6,
    ErrorCode.NOT_FOUND: 7,
    ErrorCode.SHAPE_MISMATCH: 8,
    ErrorCode.PARSE_ERROR: 9,
    ErrorCode.INTERNAL_ERROR: 10,
}

# ISS-108 (Loop8 cycle 2): import-time guard ensures the table covers
# every ErrorCode enum member. Pre-fix a newly-added ErrorCode with no
# severity entry would silently get rank 99 ("least severe") via
# severity_of_error's `.get(code, 99)` fallback — exactly the wrong
# failure mode for ranking multi-source adapter failures (a brand-new
# critical error code would sort BELOW INTERNAL_ERROR in the worst-
# error selection). Fail loudly at import instead.
_missing = set(ErrorCode) - set(ERROR_CODE_SEVERITY)
if _missing:  # pragma: no cover
    raise RuntimeError(
        f"ERROR_CODE_SEVERITY missing entries for {sorted(c.value for c in _missing)} "
        "— update the table when adding new ErrorCode members"
    )
del _missing


def severity_of_error(error: "AdapterError") -> int:
    """Severity rank for an AdapterError. Lower = more severe.
    Unknown codes default to 99 (least severe). The import-time
    guard above ensures the table covers every ErrorCode at module
    load — the 99 fallback is defensive only.
    """
    return ERROR_CODE_SEVERITY.get(error.code, 99)


def severity_of_exception(exc: Exception) -> int:
    """Severity rank for a raw exception, mapped through
    `adapter_error_from_exception`. Used by callers that have raw
    exceptions (financials sub-fetch, historical_prices dual-error)
    to rank without first manually constructing AdapterError."""
    envelope = adapter_error_from_exception(exc, source="_severity_lookup")
    return severity_of_error(envelope.error)


__all__ = [
    "ErrorCode",
    "AdapterError",
    "AdapterResult",
    "adapter_error_from_exception",
    "ERROR_CODE_SEVERITY",
    "severity_of_error",
    "severity_of_exception",
    # ISS-038 (Loop2 backlog): _scrub_detail also used outside the
    # canonical exception mapper (yfinance fallback writes raw error
    # text to run_meta and needs the same scrub protocol).
    "_scrub_detail",
    "ADAPTER_ENTRYPOINTS",
    "HTTP_INFRASTRUCTURE_ALLOWLIST",
]
