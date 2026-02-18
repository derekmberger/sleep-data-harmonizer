"""Oura fixture adapter — parses raw payloads passed directly (no HTTP fetch)."""

from uuid import UUID

from sleep.adapters.oura_mapper import OuraMapper
from sleep.domain.models import SleepDay


class OuraFixtureAdapter:
    """Fixture-mode adapter: receives raw payload, delegates to mapper."""

    source_name = "oura"

    def __init__(self) -> None:
        self._mapper = OuraMapper()

    def parse(self, raw_response: dict, patient_id: UUID) -> list[SleepDay]:
        return self._mapper.parse(raw_response, patient_id)
