"""Tests for the FHIR R4 Observation serializer."""

from datetime import UTC, date, datetime

from sleep.adapters.fhir_serializer import sleep_day_to_fhir_observation
from sleep.domain.models import SleepDay, SleepSource
from tests.conftest import PATIENT_ID


def _make_sleep_day(**overrides) -> SleepDay:
    defaults = dict(
        patient_id=PATIENT_ID,
        source=SleepSource.OURA,
        source_record_id="fhir-test-1",
        effective_date=date(2024, 3, 14),
        fingerprint="fp1",
        raw_payload={},
        ingested_at=datetime(2024, 3, 15, 8, 0, tzinfo=UTC),
        updated_at=datetime(2024, 3, 15, 8, 0, tzinfo=UTC),
        total_sleep_minutes=433,
        deep_sleep_minutes=86,
        light_sleep_minutes=260,
        rem_sleep_minutes=90,
        awake_minutes=39,
        sleep_efficiency=0.88,
        sleep_onset=datetime(2024, 3, 14, 23, 14, tzinfo=UTC),
        sleep_offset=datetime(2024, 3, 15, 7, 2, 30, tzinfo=UTC),
    )
    defaults.update(overrides)
    return SleepDay(**defaults)


class TestFHIRSerializer:
    def test_resource_type(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        assert obs["resourceType"] == "Observation"

    def test_status_final(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        assert obs["status"] == "final"

    def test_category_activity(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        cat = obs["category"][0]["coding"][0]
        assert cat["code"] == "activity"

    def test_subject_reference(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        assert obs["subject"]["reference"] == f"Patient/{PATIENT_ID}"

    def test_effective_period_when_onset_offset_present(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        assert "effectivePeriod" in obs
        assert "effectiveDateTime" not in obs

    def test_effective_datetime_fallback(self):
        day = _make_sleep_day(sleep_onset=None, sleep_offset=None)
        obs = sleep_day_to_fhir_observation(day)
        assert "effectiveDateTime" in obs
        assert obs["effectiveDateTime"] == "2024-03-14"
        assert "effectivePeriod" not in obs

    def test_loinc_coded_components(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        codes = {c["code"]["coding"][0]["code"] for c in obs["component"] if "coding" in c["code"]}
        assert "93832-4" in codes  # total sleep
        assert "93831-6" in codes  # deep sleep
        assert "93830-8" in codes  # light sleep
        assert "93829-0" in codes  # REM sleep

    def test_sleep_efficiency_text_code(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        eff_components = [
            c for c in obs["component"] if c["code"].get("text") == "Sleep efficiency"
        ]
        assert len(eff_components) == 1
        assert eff_components[0]["valueQuantity"]["value"] == 0.88
        assert eff_components[0]["valueQuantity"]["code"] == "{ratio}"

    def test_null_fields_omitted(self):
        day = _make_sleep_day(
            total_sleep_minutes=None,
            deep_sleep_minutes=None,
            light_sleep_minutes=None,
            rem_sleep_minutes=None,
            awake_minutes=None,
            sleep_efficiency=None,
        )
        obs = sleep_day_to_fhir_observation(day)
        assert obs["component"] == []

    def test_device_display(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        assert obs["device"]["display"] == "oura wearable"

    def test_meta_last_updated(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        assert obs["meta"]["lastUpdated"] == "2024-03-15T08:00:00+00:00"

    def test_issued(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        assert obs["issued"] == "2024-03-15T08:00:00+00:00"

    def test_identifier_source_and_fingerprint(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        ids = {i["system"]: i["value"] for i in obs["identifier"]}
        assert ids["urn:sleep-harmonizer:oura"] == "fhir-test-1"
        assert ids["urn:sleep-harmonizer:fingerprint"] == "fp1"

    def test_component_values(self):
        obs = sleep_day_to_fhir_observation(_make_sleep_day())
        total = next(
            c
            for c in obs["component"]
            if "coding" in c["code"] and c["code"]["coding"][0]["code"] == "93832-4"
        )
        assert total["valueQuantity"]["value"] == 433
        assert total["valueQuantity"]["unit"] == "min"
