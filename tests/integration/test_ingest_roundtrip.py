"""Integration test: ingest-to-timeline roundtrip against real Postgres.

Verifies the full path: raw payload → pipeline ingest → timeline query,
confirming the record appears in the read path with correct data.
"""

from datetime import date
from uuid import uuid4

from sleep.pipeline import ingest_sleep_data
from sleep.repository import SleepDayRepository

PATIENT_ID = uuid4()

# Payload that passes all 9 validation rules (stage sum <= total * 1.05)
VALID_OURA_PAYLOAD = {
    "data": [
        {
            "id": "integration-roundtrip-001",
            "day": "2024-03-14",
            "type": "long_sleep",
            "period": 0,
            "total_sleep_duration": 28800,  # 480 min
            "deep_sleep_duration": 5400,  # 90 min
            "light_sleep_duration": 14400,  # 240 min
            "rem_sleep_duration": 7200,  # 120 min
            "awake_time": 1800,  # 30 min → sum=480, total=480, within 5%
            "efficiency": 88,
            "bedtime_start": "2024-03-14T23:00:00-05:00",
            "bedtime_end": "2024-03-15T07:00:00-05:00",
            "time_in_bed": 28800,
        }
    ]
}


async def test_ingest_then_timeline_roundtrip(db_session):
    """Ingest a valid payload, then read it back via timeline query."""
    result = await ingest_sleep_data(db_session, "oura", VALID_OURA_PAYLOAD, PATIENT_ID)
    assert result.records_inserted == 1
    assert result.records_quarantined == 0

    # Query timeline
    repo = SleepDayRepository(db_session)
    rows, _next_cursor = await repo.get_timeline(patient_id=PATIENT_ID, limit=25)

    assert len(rows) == 1
    row = rows[0]
    assert row.source == "oura"
    assert row.patient_id == PATIENT_ID
    assert row.effective_date == date(2024, 3, 14)
    assert row.total_sleep_minutes == 480
    assert row.fingerprint is not None


async def test_ingest_idempotent_replay(db_session):
    """Ingesting the same payload twice should produce dedup, not duplicates."""
    result1 = await ingest_sleep_data(db_session, "oura", VALID_OURA_PAYLOAD, PATIENT_ID)
    assert result1.records_inserted == 1

    result2 = await ingest_sleep_data(db_session, "oura", VALID_OURA_PAYLOAD, PATIENT_ID)
    assert result2.records_inserted == 0
    assert result2.records_updated >= 1

    # Still only one row per fingerprint
    repo = SleepDayRepository(db_session)
    rows, _ = await repo.get_timeline(patient_id=PATIENT_ID, limit=100)
    fingerprints = [r.fingerprint for r in rows]
    assert len(fingerprints) == len(set(fingerprints)), "Duplicate fingerprints found"
