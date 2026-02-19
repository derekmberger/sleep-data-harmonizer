"""Tests for domain models: SleepDay, fingerprint, efficiency normalization."""

from datetime import UTC, date, datetime

from sleep.domain.models import SleepDay, SleepSource
from tests.conftest import PATIENT_ID


class TestFingerprint:
    def test_deterministic(self):
        fp1 = SleepDay.compute_fingerprint(SleepSource.OURA, "abc", date(2024, 3, 14))
        fp2 = SleepDay.compute_fingerprint(SleepSource.OURA, "abc", date(2024, 3, 14))
        assert fp1 == fp2

    def test_different_source_different_fingerprint(self):
        fp_oura = SleepDay.compute_fingerprint(SleepSource.OURA, "abc", date(2024, 3, 14))
        fp_withings = SleepDay.compute_fingerprint(SleepSource.WITHINGS, "abc", date(2024, 3, 14))
        assert fp_oura != fp_withings

    def test_different_date_different_fingerprint(self):
        fp1 = SleepDay.compute_fingerprint(SleepSource.OURA, "abc", date(2024, 3, 14))
        fp2 = SleepDay.compute_fingerprint(SleepSource.OURA, "abc", date(2024, 3, 15))
        assert fp1 != fp2

    def test_different_record_id_different_fingerprint(self):
        fp1 = SleepDay.compute_fingerprint(SleepSource.OURA, "abc", date(2024, 3, 14))
        fp2 = SleepDay.compute_fingerprint(SleepSource.OURA, "xyz", date(2024, 3, 14))
        assert fp1 != fp2

    def test_fingerprint_is_64_char_hex(self):
        fp = SleepDay.compute_fingerprint(SleepSource.OURA, "abc", date(2024, 3, 14))
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


class TestEfficiencyNormalization:
    def test_none_passthrough(self):
        day = SleepDay(
            patient_id=PATIENT_ID,
            source=SleepSource.OURA,
            source_record_id="t1",
            effective_date=date(2024, 3, 14),
            fingerprint="x",
            raw_payload={},
            ingested_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            sleep_efficiency=None,
        )
        assert day.sleep_efficiency is None

    def test_oura_integer_normalized(self):
        day = SleepDay(
            patient_id=PATIENT_ID,
            source=SleepSource.OURA,
            source_record_id="t2",
            effective_date=date(2024, 3, 14),
            fingerprint="x",
            raw_payload={},
            ingested_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            sleep_efficiency=88,  # Oura sends 0-100
        )
        assert day.sleep_efficiency == 0.88

    def test_withings_float_passthrough(self):
        day = SleepDay(
            patient_id=PATIENT_ID,
            source=SleepSource.WITHINGS,
            source_record_id="t3",
            effective_date=date(2024, 3, 14),
            fingerprint="x",
            raw_payload={},
            ingested_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            sleep_efficiency=0.91,  # Withings sends 0-1
        )
        assert day.sleep_efficiency == 0.91


class TestSleepSource:
    def test_oura_value(self):
        assert SleepSource.OURA.value == "oura"

    def test_withings_value(self):
        assert SleepSource.WITHINGS.value == "withings"
