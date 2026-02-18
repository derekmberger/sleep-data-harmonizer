"""Canonical SleepDay domain model.

Represents one night's sleep from any vendor source, normalized into
a common schema. This is the single source of truth for product APIs.

Design principles:
- Intersection-first: only fields ALL sources provide get typed columns
- Nullable measurement fields: NULL = "vendor did not provide", not "zero"
- Provenance: every record traces back to its raw source
- Temporal: effective_date (when) vs ingested_at (when received) vs updated_at (when modified)
"""

import hashlib
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class SleepSource(StrEnum):
    OURA = "oura"
    WITHINGS = "withings"


class SleepDay(BaseModel):
    """Canonical representation of one night's sleep."""

    # Identity
    id: UUID = Field(default_factory=uuid4)
    patient_id: UUID

    # Provenance
    source: SleepSource
    source_record_id: str
    raw_payload: dict[str, Any]
    fingerprint: str

    # Temporal
    effective_date: date
    ingested_at: datetime
    updated_at: datetime

    # Canonical sleep metrics (nullable = not provided by vendor)
    total_sleep_minutes: int | None = Field(None, ge=0)
    deep_sleep_minutes: int | None = Field(None, ge=0)
    light_sleep_minutes: int | None = Field(None, ge=0)
    rem_sleep_minutes: int | None = Field(None, ge=0)
    awake_minutes: int | None = Field(None, ge=0)
    sleep_onset: datetime | None = None
    sleep_offset: datetime | None = None
    sleep_efficiency: float | None = Field(None, ge=0.0, le=1.0)

    # Extension
    extra: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def compute_fingerprint(
        source: SleepSource, source_record_id: str, effective_date: date
    ) -> str:
        raw = f"{source.value}:{source_record_id}:{effective_date.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @field_validator("sleep_efficiency", mode="before")
    @classmethod
    def normalize_efficiency(cls, v: Any) -> float | None:
        """Accept 0-100 integer (Oura) or 0-1 float (Withings) and normalize to 0.0-1.0."""
        if v is None:
            return None
        if isinstance(v, (int, float)) and v > 1.0:
            return round(v / 100.0, 4)
        return float(v)
