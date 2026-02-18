"""Shared test fixtures."""

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PATIENT_ID = UUID("a1b2c3d4-5678-90ab-cdef-1234567890ab")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture
def oura_response():
    return load_fixture("oura_sleep_response.json")


@pytest.fixture
def withings_response():
    return load_fixture("withings_sleep_response.json")


@pytest.fixture
def patient_id():
    return PATIENT_ID


@pytest.fixture
def valid_sleep_record():
    """A fully valid canonical sleep record dict for validation testing."""
    return {
        "patient_id": PATIENT_ID,
        "source": "oura",
        "source_record_id": "test-123",
        "effective_date": date(2024, 3, 14),
        "fingerprint": "abc123",
        "total_sleep_minutes": 480,
        "deep_sleep_minutes": 90,
        "light_sleep_minutes": 210,
        "rem_sleep_minutes": 120,
        "awake_minutes": 60,
        "sleep_efficiency": 0.88,
        "sleep_onset": datetime(2024, 3, 14, 23, 0, tzinfo=UTC),
        "sleep_offset": datetime(2024, 3, 15, 7, 0, tzinfo=UTC),
    }
