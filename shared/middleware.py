"""FastAPI middleware for request ID injection and error handling."""

from collections.abc import Callable
from contextvars import ContextVar
from uuid import uuid4

import structlog
from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from shared.exceptions import ProblemDetailError

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject a unique request ID into every request and response.

    Header name: X-Request-ID (normalized casing per docs).
    Default format: UUID v4.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Callable[..., Response]]
    ):
        rid = request.headers.get("X-Request-ID", str(uuid4()))
        request_id_var.set(rid)
        structlog.contextvars.bind_contextvars(request_id=rid)

        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


async def problem_detail_handler(request: Request, exc: ProblemDetailError) -> JSONResponse:
    """Convert ProblemDetailError exceptions into RFC 9457 responses."""
    body: dict = {
        "type": exc.type_uri,
        "title": exc.title,
        "status": exc.status,
        "detail": exc.detail,
        "instance": str(request.url.path),
    }
    if exc.violations:
        body["violations"] = exc.violations
    return JSONResponse(
        status_code=exc.status,
        content=body,
        media_type="application/problem+json",
    )


async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Convert FastAPI/Pydantic native validation errors into RFC 9457 format.

    This ensures all 422 errors consistently use application/problem+json
    with a violations array, not FastAPI's default {detail: [...]}.
    """
    violations = []
    for err in exc.errors():
        loc = err.get("loc", ())
        field = ".".join(str(part) for part in loc if part != "body")
        violations.append(
            {
                "field": field or "(root)",
                "message": err.get("msg", "Validation error"),
                "constraint": err.get("type", "validation"),
            }
        )

    body = {
        "type": "https://api.blue.health/problems/validation-error",
        "title": "Validation Error",
        "status": 422,
        "detail": f"Request body contains {len(violations)} validation error(s)",
        "instance": str(request.url.path),
        "violations": violations,
    }
    return JSONResponse(
        status_code=422,
        content=body,
        media_type="application/problem+json",
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Convert generic HTTP exceptions into RFC 9457 format."""
    body = {
        "type": "about:blank",
        "title": exc.detail if isinstance(exc.detail, str) else "Error",
        "status": exc.status_code,
        "detail": exc.detail if isinstance(exc.detail, str) else str(exc.detail),
        "instance": str(request.url.path),
    }
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        media_type="application/problem+json",
    )
