"""HTTP client with tenacity retry for vendor API calls.

Retry policy:
- Retry on transient errors (429, 500, 502, 503, 504, timeouts)
- Do NOT retry on 400, 401, 403 (client errors / auth failures)
- Exponential backoff with jitter
- Max 3 attempts
"""

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from shared.config import settings

logger = structlog.get_logger()

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class TransientHTTPError(Exception):
    """Raised for HTTP errors that are safe to retry."""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


@retry(
    retry=retry_if_exception_type((TransientHTTPError, httpx.TimeoutException)),
    wait=wait_exponential_jitter(initial=1, max=settings.retry_max_wait_seconds, jitter=2),
    stop=stop_after_attempt(settings.retry_max_attempts),
    before_sleep=before_sleep_log(logger, "WARNING"),
    reraise=True,
)
async def fetch_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with retry on transient failures."""
    response = await client.request(method, url, **kwargs)

    if response.status_code in TRANSIENT_STATUS_CODES:
        raise TransientHTTPError(response.status_code, response.text[:200])

    response.raise_for_status()
    return response
