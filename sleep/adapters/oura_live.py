"""Oura live adapter — fetches from Oura V2 API, then maps to canonical model.

Uses fetch_with_retry for transient-only retry (429/5xx/timeout).
Non-retryable errors (400/401/403) propagate immediately.
"""

from uuid import UUID

import httpx

from shared.config import settings
from shared.metrics import vendor_api_duration_seconds
from sleep.adapters.http_client import fetch_with_retry
from sleep.adapters.oura_mapper import OuraMapper
from sleep.domain.models import SleepDay


class OuraLiveAdapter:
    """Live-mode adapter: fetches from Oura API, then delegates to mapper."""

    source_name = "oura"

    def __init__(self) -> None:
        self._mapper = OuraMapper()

    async def fetch(self, start_date: str, end_date: str) -> dict:
        """Fetch sleep data from Oura V2 API with transient-only retry."""
        url = f"{settings.oura_base_url}/v2/usercollection/sleep"
        headers = {"Authorization": f"Bearer {settings.oura_access_token}"}
        params = {"start_date": start_date, "end_date": end_date}

        async with httpx.AsyncClient() as client:
            with vendor_api_duration_seconds.labels(source="oura").time():
                resp = await fetch_with_retry(
                    client, "GET", url, headers=headers, params=params
                )
            return resp.json()

    def parse(self, raw_response: dict, patient_id: UUID) -> list[SleepDay]:
        return self._mapper.parse(raw_response, patient_id)
