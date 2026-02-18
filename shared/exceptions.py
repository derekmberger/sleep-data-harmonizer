"""RFC 9457 Problem Details exception hierarchy.

All API errors extend ProblemDetailError and are converted to
application/problem+json responses by the exception handler middleware.
"""


class ProblemDetailError(Exception):
    def __init__(
        self,
        type_uri: str,
        title: str,
        status: int,
        detail: str,
        violations: list[dict] | None = None,
    ):
        self.type_uri = type_uri
        self.title = title
        self.status = status
        self.detail = detail
        self.violations = violations
        super().__init__(detail)


class ValidationError(ProblemDetailError):
    def __init__(self, violations: list[dict]):
        super().__init__(
            type_uri="https://api.blue.health/problems/validation-error",
            title="Validation Error",
            status=422,
            detail=f"Request body contains {len(violations)} validation error(s)",
            violations=violations,
        )


class NotFoundError(ProblemDetailError):
    def __init__(self, detail: str):
        super().__init__(
            type_uri="https://api.blue.health/problems/not-found",
            title="Not Found",
            status=404,
            detail=detail,
        )


class IdempotencyConflictError(ProblemDetailError):
    def __init__(self, key: str):
        super().__init__(
            type_uri="https://api.blue.health/problems/idempotency-conflict",
            title="Idempotency Key Conflict",
            status=409,
            detail=(
                f"Idempotency key '{key}' was already used with different request parameters. "
                "Each idempotency key must be used with identical request bodies."
            ),
        )


class IdempotencyInFlightError(ProblemDetailError):
    def __init__(self, key: str):
        super().__init__(
            type_uri="https://api.blue.health/problems/idempotency-in-flight",
            title="Request In Flight",
            status=409,
            detail=f"A request with idempotency key '{key}' is currently being processed.",
        )


class InvalidDateRangeError(ProblemDetailError):
    def __init__(self, start: str, end: str):
        super().__init__(
            type_uri="https://api.blue.health/problems/invalid-date-range",
            title="Invalid Date Range",
            status=400,
            detail=f"Parameter 'start' ({start}) must be before 'end' ({end})",
        )


class MissingIdempotencyKeyError(ProblemDetailError):
    def __init__(self):
        super().__init__(
            type_uri="https://api.blue.health/problems/missing-idempotency-key",
            title="Missing Idempotency Key",
            status=400,
            detail="The Idempotency-Key header is required for POST requests.",
        )


class UnsupportedSourceError(ProblemDetailError):
    def __init__(self, source: str):
        super().__init__(
            type_uri="https://api.blue.health/problems/unsupported-source",
            title="Unsupported Source",
            status=422,
            detail=f"Source '{source}' is not supported. Must be one of: oura, withings",
        )


class InvalidSortError(ProblemDetailError):
    def __init__(self, sort: str, allowed: set[str]):
        allowed_str = ", ".join(sorted(allowed))
        super().__init__(
            type_uri="https://api.blue.health/problems/invalid-sort",
            title="Invalid Sort Parameter",
            status=400,
            detail=f"Sort value '{sort}' is not supported. Must be one of: {allowed_str}",
        )
