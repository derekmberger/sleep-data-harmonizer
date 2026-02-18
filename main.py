"""FastAPI application entry point.

Wires together: middleware, exception handlers, routes, metrics.
Validates config at startup.
"""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.config import settings
from shared.exceptions import ProblemDetailError
from shared.logging import configure_logging
from shared.metrics import create_metrics_app
from shared.middleware import (
    RequestIdMiddleware,
    http_exception_handler,
    problem_detail_handler,
    request_validation_handler,
)
from sleep.api import router as sleep_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    configure_logging(json_output=True)
    logger.info(
        "app_starting",
        adapter_mode=settings.adapter_mode,
        database_url=settings.database_url.split("@")[-1],  # hide credentials
    )
    yield
    logger.info("app_shutting_down")


app = FastAPI(
    title="Sleep Data Harmonizer API",
    description=(
        "Ingests sleep data from wearable devices (Oura, Withings), normalizes it "
        "into canonical domain objects, and exposes product APIs."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(RequestIdMiddleware)

# All errors emit application/problem+json (RFC 9457)
app.add_exception_handler(ProblemDetailError, problem_detail_handler)
app.add_exception_handler(RequestValidationError, request_validation_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)

app.include_router(sleep_router)

metrics_app = create_metrics_app()
app.mount("/metrics", metrics_app)


@app.get("/health")
async def health():
    return {"status": "ok"}
