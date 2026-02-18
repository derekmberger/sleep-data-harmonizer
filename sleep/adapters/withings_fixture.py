"""Withings fixture adapter — parses raw payloads passed directly (no HTTP fetch)."""

from uuid import UUID

from sleep.adapters.withings_mapper import WithingsMapper
from sleep.domain.models import SleepDay


class WithingsFixtureAdapter:
    """Fixture-mode adapter: receives raw payload, delegates to mapper."""

    source_name = "withings"

    def __init__(self) -> None:
        self._mapper = WithingsMapper()

    def parse(self, raw_response: dict, patient_id: UUID) -> list[SleepDay]:
        return self._mapper.parse(raw_response, patient_id)
