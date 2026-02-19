"""Integration test: upsert idempotency against real Postgres.

Verifies that inserting the same canonical record twice (same fingerprint)
results in exactly one row via the ON CONFLICT upsert.
"""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from sleep.repository import SleepDayRepository

PATIENT_ID = uuid4()


@pytest.fixture
def canonical_record():
    """A valid canonical record dict ready for upsert."""
    return {
        "id": uuid4(),
        "patient_id": PATIENT_ID,
        "source": "oura",
        "source_record_id": "integration-test-1",
        "raw_payload": {"test": True},
        "fingerprint": "integration_test_fp_unique_001",
        "effective_date": date(2024, 3, 14),
        "ingested_at": datetime(2024, 3, 15, 8, 0, tzinfo=UTC),
        "updated_at": datetime(2024, 3, 15, 8, 0, tzinfo=UTC),
        "total_sleep_minutes": 430,
        "deep_sleep_minutes": 85,
        "light_sleep_minutes": 260,
        "rem_sleep_minutes": 90,
        "awake_minutes": 40,
        "sleep_onset": datetime(2024, 3, 14, 23, 0, tzinfo=UTC),
        "sleep_offset": datetime(2024, 3, 15, 7, 0, tzinfo=UTC),
        "sleep_efficiency": 0.88,
        "extra": {},
    }


async def test_upsert_same_record_twice_yields_one_row(db_session, canonical_record):
    """Upserting the same fingerprint twice should result in exactly one row."""
    repo = SleepDayRepository(db_session)

    # First upsert — should be an insert
    result1 = await repo.upsert(canonical_record)
    await db_session.commit()
    assert result1["was_inserted"] is True

    # Second upsert with same fingerprint — should be an update (dedup)
    record2 = {**canonical_record, "id": uuid4(), "total_sleep_minutes": 440}
    result2 = await repo.upsert(record2)
    await db_session.commit()
    assert result2["was_inserted"] is False

    # Verify only one row exists for this fingerprint
    from sqlalchemy import func, select

    from sleep.domain.orm import SleepDayModel

    count_result = await db_session.execute(
        select(func.count()).where(
            SleepDayModel.fingerprint == canonical_record["fingerprint"]
        )
    )
    assert count_result.scalar_one() == 1

    # Verify the value was updated
    row_result = await db_session.execute(
        select(SleepDayModel).where(
            SleepDayModel.fingerprint == canonical_record["fingerprint"]
        )
    )
    row = row_result.scalar_one()
    assert row.total_sleep_minutes == 440  # updated value


async def test_different_fingerprints_yield_separate_rows(db_session, canonical_record):
    """Records with different fingerprints should create separate rows."""
    repo = SleepDayRepository(db_session)

    result1 = await repo.upsert(canonical_record)
    await db_session.commit()
    assert result1["was_inserted"] is True

    record2 = {
        **canonical_record,
        "id": uuid4(),
        "fingerprint": "integration_test_fp_unique_002",
        "source_record_id": "integration-test-2",
    }
    result2 = await repo.upsert(record2)
    await db_session.commit()
    assert result2["was_inserted"] is True
