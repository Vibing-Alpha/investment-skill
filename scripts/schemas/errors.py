"""Schema validation error.

Raised by `load_<artifact>(path)` when a file violates its contract.
Carries the artifact name + field path so callers can surface actionable
diagnostics. Does NOT inherit from FileNotFoundError / JSONDecodeError —
those remain distinct so callers can choose to retry vs. fail.

DL4: DataQualityError is added as a sibling for runtime data-content
failures (artifact loaded fine, content violates invariants). Both
SchemaError and DataQualityError inherit ValueError so existing
`except ValueError` chains continue to work; new code uses the
specific subclass.
"""


class SchemaError(ValueError):
    """Raised when an artifact file fails schema validation."""

    def __init__(self, artifact: str, field: str, message: str):
        self.artifact = artifact
        self.field = field
        self.message = message
        super().__init__(f"{artifact}:{field}: {message}")


class DataQualityError(ValueError):
    """Raised when artifact content (loaded successfully) violates a runtime
    invariant. Distinct from SchemaError which signals I/O / parse / load
    failures. Both inherit ValueError for backward compat.
    """

    def __init__(self, artifact: str, field: str, message: str):
        self.artifact = artifact
        self.field = field
        self.message = message
        super().__init__(f"{artifact}:{field}: {message}")
