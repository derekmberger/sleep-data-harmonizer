"""Tests for vendor adapters (Oura + Withings mappers) and adapter factory."""

import json
from datetime import UTC, date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sleep.adapters.factory import get_adapter
from sleep.adapters.oura_mapper import OuraMapper
from sleep.adapters.withings_mapper import WithingsMapper
from sleep.domain.models import SleepSource
from tests.conftest import PATIENT_ID

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"


def _canonical_dict(day) -> dict:
    """Extract comparable canonical fields from a SleepDay for golden diff."""
    result = {
        "source": day.source.value,
        "source_record_id": day.source_record_id,
        "effective_date": day.effective_date.isoformat(),
        "total_sleep_minutes": day.total_sleep_minutes,
        "deep_sleep_minutes": day.deep_sleep_minutes,
        "light_sleep_minutes": day.light_sleep_minutes,
        "rem_sleep_minutes": day.rem_sleep_minutes,
        "awake_minutes": day.awake_minutes,
        "sleep_efficiency": day.sleep_efficiency,
        "extra": day.extra,
    }
    return result


class TestOuraMapper:
    def test_parses_long_sleep_record(self, oura_response):
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)

        # Should include only the long_sleep record (rest is filtered)
        assert len(results) == 1
        day = results[0]
        assert day.source == SleepSource.OURA
        assert day.source_record_id == "8f9a5221-639e-4a85-81cb-4065ef23f979"
        assert day.effective_date == date(2024, 3, 14)

    def test_filters_non_long_sleep(self, oura_response):
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)
        # The "rest" type record should be filtered out
        assert len(results) == 1
        assert results[0].extra.get("sleep_type") == "long_sleep"

    def test_seconds_to_minutes_conversion(self, oura_response):
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)
        day = results[0]

        # total_sleep_duration: 26010 seconds // 60 = 433 minutes
        assert day.total_sleep_minutes == 433
        # deep_sleep_duration: 5160 seconds // 60 = 86 minutes
        assert day.deep_sleep_minutes == 86
        # light_sleep_duration: 15600 seconds // 60 = 260 minutes
        assert day.light_sleep_minutes == 260
        # rem_sleep_duration: 5400 seconds // 60 = 90 minutes
        assert day.rem_sleep_minutes == 90
        # awake_time: 2340 seconds // 60 = 39 minutes
        assert day.awake_minutes == 39

    def test_efficiency_normalized_to_ratio(self, oura_response):
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)
        day = results[0]
        # efficiency: 88 -> 0.88
        assert day.sleep_efficiency == 0.88

    def test_timestamps_parsed_with_timezone(self, oura_response):
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)
        day = results[0]
        assert day.sleep_onset is not None
        assert day.sleep_onset.tzinfo is not None
        assert day.sleep_offset is not None
        assert day.sleep_offset.tzinfo is not None

    def test_extra_fields_populated(self, oura_response):
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)
        day = results[0]

        assert day.extra["time_in_bed_minutes"] == 472  # 28350 // 60
        assert day.extra["latency_minutes"] == 8  # 480 // 60
        assert day.extra["avg_hr_bpm"] == 52.375
        assert day.extra["avg_hrv_ms"] == 42
        assert day.extra["sleep_algorithm_version"] == "v2"
        assert day.extra["sleep_analysis_reason"] == "foreground_sleep_analysis"

    def test_fingerprint_computed(self, oura_response):
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)
        day = results[0]

        expected = day.compute_fingerprint(
            SleepSource.OURA,
            "8f9a5221-639e-4a85-81cb-4065ef23f979",
            date(2024, 3, 14),
        )
        assert day.fingerprint == expected
        assert len(day.fingerprint) == 64  # SHA-256 hex

    def test_handles_missing_optional_fields(self):
        mapper = OuraMapper()
        minimal_response = {
            "data": [
                {
                    "id": "minimal-test",
                    "day": "2024-03-14",
                    "type": "long_sleep",
                    "period": 0,
                    "total_sleep_duration": None,
                    "deep_sleep_duration": None,
                    "light_sleep_duration": None,
                    "rem_sleep_duration": None,
                    "awake_time": None,
                    "efficiency": None,
                    "bedtime_start": "2024-03-14T23:00:00-05:00",
                    "bedtime_end": "2024-03-15T07:00:00-05:00",
                    "time_in_bed": 28800,
                }
            ]
        }
        results = mapper.parse(minimal_response, PATIENT_ID)
        assert len(results) == 1
        day = results[0]
        assert day.total_sleep_minutes is None
        assert day.sleep_efficiency is None

    def test_empty_data_returns_empty_list(self):
        mapper = OuraMapper()
        assert mapper.parse({"data": []}, PATIENT_ID) == []
        assert mapper.parse({}, PATIENT_ID) == []

    def test_golden_output(self, oura_response):
        """Full adapter output matches golden fixture (snapshot test)."""
        mapper = OuraMapper()
        results = mapper.parse(oura_response, PATIENT_ID)
        actual = _canonical_dict(results[0])

        golden_path = GOLDEN_DIR / "oura_sleep_day.json"
        expected = json.loads(golden_path.read_text())

        assert actual == expected, (
            f"Oura adapter output diverged from golden fixture.\n"
            f"To update: review diff and overwrite {golden_path}"
        )


class TestWithingsMapper:
    def test_parses_series_entry(self, withings_response):
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)

        assert len(results) == 1
        day = results[0]
        assert day.source == SleepSource.WITHINGS
        assert day.source_record_id == "987654321"
        assert day.effective_date == date(2024, 3, 14)

    def test_seconds_to_minutes_conversion(self, withings_response):
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        day = results[0]

        # total_sleep_time: 25440 // 60 = 424
        assert day.total_sleep_minutes == 424
        # deepsleepduration: 5280 // 60 = 88
        assert day.deep_sleep_minutes == 88
        # lightsleepduration: 14400 // 60 = 240
        assert day.light_sleep_minutes == 240
        # remsleepduration: 5760 // 60 = 96
        assert day.rem_sleep_minutes == 96
        # wakeupduration: 1860 // 60 = 31
        assert day.awake_minutes == 31

    def test_efficiency_already_ratio(self, withings_response):
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        day = results[0]
        # Withings sleep_efficiency is already 0.0-1.0
        assert day.sleep_efficiency == 0.91

    def test_unix_timestamps_with_entry_timezone(self, withings_response):
        """Timestamps use entry-level IANA timezone, not naive UTC."""
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        day = results[0]

        assert day.sleep_onset is not None
        assert day.sleep_onset.tzinfo is not None
        # Entry has timezone: "America/Chicago"
        chicago = ZoneInfo("America/Chicago")
        assert day.sleep_onset.tzinfo == chicago
        assert day.sleep_offset.tzinfo == chicago
        # startdate: 1710468840 -> 2024-03-14T21:14:00-06:00 (CST)
        assert day.sleep_onset.year == 2024

    def test_timezone_stored_in_extra(self, withings_response):
        """IANA timezone name is preserved in extra for audit."""
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        assert results[0].extra["timezone"] == "America/Chicago"

    def test_alias_precedence(self, withings_response):
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        day = results[0]

        # sleep_latency (480) should take precedence over durationtosleep (540)
        assert day.extra["latency_minutes"] == 8  # 480 // 60
        # wakeup_latency (300) over durationtowakeup (360)
        assert day.extra["wakeup_latency_minutes"] == 5  # 300 // 60

    def test_extra_fields_populated(self, withings_response):
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        day = results[0]

        assert day.extra["sleep_score"] == 78
        assert day.extra["avg_hr_bpm"] == 54
        assert day.extra["min_hr_bpm"] == 46
        assert day.extra["max_hr_bpm"] == 72
        assert day.extra["snoring_seconds"] == 1200
        assert day.extra["wakeup_count"] == 3
        assert day.extra["hash_deviceid"] == "a0b1c2d3e4f5"

    def test_night_events_parsed_from_json_string(self, withings_response):
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        day = results[0]

        # night_events was a JSON string, should be parsed
        assert isinstance(day.extra["night_events"], list)
        assert day.extra["night_events"][0]["type"] == 1

    def test_source_record_id_fallback(self):
        mapper = WithingsMapper()
        response = {
            "body": {
                "series": [
                    {
                        "startdate": 1710468840,
                        "enddate": 1710496950,
                        "date": "2024-03-14",
                        "data": {"total_sleep_time": 25440},
                    }
                ]
            }
        }
        results = mapper.parse(response, PATIENT_ID)
        assert results[0].source_record_id == "1710468840_1710496950"

    def test_fingerprint_computed(self, withings_response):
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        day = results[0]

        expected = day.compute_fingerprint(SleepSource.WITHINGS, "987654321", date(2024, 3, 14))
        assert day.fingerprint == expected

    def test_empty_series_returns_empty(self):
        mapper = WithingsMapper()
        assert mapper.parse({"body": {"series": []}}, PATIENT_ID) == []

    def test_golden_output(self, withings_response):
        """Full adapter output matches golden fixture (snapshot test)."""
        mapper = WithingsMapper()
        results = mapper.parse(withings_response, PATIENT_ID)
        actual = _canonical_dict(results[0])

        golden_path = GOLDEN_DIR / "withings_sleep_day.json"
        expected = json.loads(golden_path.read_text())

        assert actual == expected, (
            f"Withings adapter output diverged from golden fixture.\n"
            f"To update: review diff and overwrite {golden_path}"
        )

    def test_fallback_to_utc_without_timezone(self):
        """When entry has no timezone field, fallback to UTC."""
        mapper = WithingsMapper()
        response = {
            "body": {
                "series": [
                    {
                        "startdate": 1710468840,
                        "enddate": 1710496950,
                        "date": "2024-03-14",
                        "data": {"total_sleep_time": 25440},
                    }
                ]
            }
        }
        results = mapper.parse(response, PATIENT_ID)
        day = results[0]
        assert day.sleep_onset is not None
        # Without timezone field, should use UTC
        assert day.sleep_onset.utcoffset() == UTC.utcoffset(None)


class TestAdapterFactory:
    def test_fixture_mode_returns_fixture_adapter(self):
        with patch("sleep.adapters.factory.settings") as mock_settings:
            mock_settings.adapter_mode = "fixture"
            adapter = get_adapter("oura")
            from sleep.adapters.oura_fixture import OuraFixtureAdapter

            assert isinstance(adapter, OuraFixtureAdapter)

    def test_live_mode_returns_live_adapter(self):
        with patch("sleep.adapters.factory.settings") as mock_settings:
            mock_settings.adapter_mode = "live"
            mock_settings.retry_max_attempts = 3
            mock_settings.retry_max_wait_seconds = 30
            mock_settings.oura_base_url = "https://api.ouraring.com"
            mock_settings.oura_access_token = "test"
            mock_settings.withings_base_url = "https://wbsapi.withings.net"
            mock_settings.withings_access_token = "test"
            adapter = get_adapter("oura")
            from sleep.adapters.oura_live import OuraLiveAdapter

            assert isinstance(adapter, OuraLiveAdapter)

    def test_unsupported_source_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Unsupported source"):
            get_adapter("fitbit")

    def test_fixture_adapters_implement_protocol(self):
        """Fixture adapters satisfy the SleepAdapter protocol."""
        from sleep.adapters.protocol import SleepAdapter

        with patch("sleep.adapters.factory.settings") as mock_settings:
            mock_settings.adapter_mode = "fixture"
            for source in ("oura", "withings"):
                adapter = get_adapter(source)
                assert isinstance(adapter, SleepAdapter)
