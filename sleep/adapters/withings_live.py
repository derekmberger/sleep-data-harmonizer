"""Withings live adapter — fetches from Withings Sleep V2 API, then maps to canonical model.

Uses fetch_with_retry for transient-only retry (429/5xx/timeout).
Non-retryable errors (400/401/403) propagate immediately.
"""

from uuid import UUID

import httpx

from shared.config import settings
from shared.metrics import vendor_api_duration_seconds
from sleep.adapters.http_client import fetch_with_retry
from sleep.adapters.withings_mapper import WithingsMapper
from sleep.domain.models import SleepDay


class WithingsLiveAdapter:
    """Live-mode adapter: fetches from Withings API, then delegates to mapper."""

    source_name = "withings"

    def __init__(self) -> None:
        self._mapper = WithingsMapper()

    async def fetch(self, start_date: str, end_date: str) -> dict:
        """Fetch sleep summary from Withings Sleep V2 API with transient-only retry."""
        url = f"{settings.withings_base_url}/v2/sleep"
        headers = {"Authorization": f"Bearer {settings.withings_access_token}"}
        params = {
            "action": "getsummary",
            "startdateymd": start_date,
            "enddateymd": end_date,
        }

        async with httpx.AsyncClient() as client:
            with vendor_api_duration_seconds.labels(source="withings").time():
                resp = await fetch_with_retry(
                    client, "GET", url, headers=headers, params=params
                )
            return resp.json()

    def parse(self, raw_response: dict, patient_id: UUID) -> list[SleepDay]:
        return self._mapper.parse(raw_response, patient_id)
