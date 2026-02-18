"""Parametrized tests for the 9 canonical validation rules."""

from datetime import UTC, date, datetime, timedelta

import pytest

from sleep.domain.validation import validate_sleep_record


@pytest.mark.parametrize(
    "overrides, expected_reason",
    [
        # Rule 1: Required effective_date
        ({"effective_date": None}, "missing_effective_date"),
        # Rule 2: Sleep duration range
        ({"total_sleep_minutes": -1}, "sleep_duration_out_of_range"),
        ({"total_sleep_minutes": 1441}, "sleep_duration_out_of_range"),
        # Rule 3: Non-negative stages
        ({"deep_sleep_minutes": -5}, "negative_sleep_stage"),
        ({"light_sleep_minutes": -1}, "negative_sleep_stage"),
        ({"rem_sleep_minutes": -10}, "negative_sleep_stage"),
        ({"awake_minutes": -2}, "negative_sleep_stage"),
        # Rule 4: Stage sum consistency
        (
            {
                "deep_sleep_minutes": 300,
                "light_sleep_minutes": 200,
                "rem_sleep_minutes": 100,
                "awake_minutes": 60,
                "total_sleep_minutes": 480,
            },
            "stage_sum_exceeds_total",
        ),
        # Rule 5: No future dates
        (
            {"effective_date": date.today() + timedelta(days=1)},
            "future_date",
        ),
        # Rule 6: Valid efficiency
        ({"sleep_efficiency": 1.5}, "efficiency_out_of_range"),
        ({"sleep_efficiency": -0.1}, "efficiency_out_of_range"),
        # Rule 7: Timezone on timestamps
        (
            {"sleep_onset": datetime(2024, 3, 14, 23, 0)},  # naive
            "missing_timezone",
        ),
        # Rule 8: Known source
        ({"source": "fitbit"}, "unknown_source"),
        # Rule 9: Bedtime ordering
        (
            {
                "sleep_onset": datetime(2024, 3, 15, 7, 0, tzinfo=UTC),
                "sleep_offset": datetime(2024, 3, 14, 23, 0, tzinfo=UTC),
            },
            "bedtime_order_invalid",
        ),
    ],
    ids=[
        "missing_effective_date",
        "negative_duration",
        "over_24h_duration",
        "negative_deep",
        "negative_light",
        "negative_rem",
        "negative_awake",
        "stage_sum_exceeds_total",
        "future_date",
        "efficiency_above_1",
        "efficiency_negative",
        "missing_timezone_onset",
        "unknown_source",
        "reversed_bedtime",
    ],
)
def test_validation_catches_violation(valid_sleep_record, overrides, expected_reason):
    record = {**valid_sleep_record, **overrides}
    errors = validate_sleep_record(record)
    reasons = [e.reason for e in errors]
    assert expected_reason in reasons, f"Expected '{expected_reason}' in {reasons}"


def test_valid_record_passes_all_rules(valid_sleep_record):
    errors = validate_sleep_record(valid_sleep_record)
    assert errors == [], f"Valid record should pass all rules, got: {errors}"


def test_multiple_violations_reported():
    record = {
        "effective_date": date.today() + timedelta(days=1),
        "total_sleep_minutes": -5,
        "source": "oura",
    }
    errors = validate_sleep_record(record)
    reasons = {e.reason for e in errors}
    assert "future_date" in reasons
    assert "sleep_duration_out_of_range" in reasons
    assert len(errors) >= 2


@pytest.mark.parametrize(
    "total,deep,light,rem,awake",
    [
        (480, 90, 210, 120, 60),  # exact sum = total
        (480, 90, 210, 120, 80),  # sum=500, 500 <= 480*1.05=504 (within tolerance)
        (480, None, None, None, None),  # all stages None
        (None, 90, 210, 120, 60),  # total None (skip check)
    ],
)
def test_valid_stage_sums(valid_sleep_record, total, deep, light, rem, awake):
    record = {
        **valid_sleep_record,
        "total_sleep_minutes": total,
        "deep_sleep_minutes": deep,
        "light_sleep_minutes": light,
        "rem_sleep_minutes": rem,
        "awake_minutes": awake,
    }
    errors = validate_sleep_record(record)
    stage_errors = [e for e in errors if e.reason == "stage_sum_exceeds_total"]
    assert stage_errors == []


def test_edge_case_zero_duration_valid(valid_sleep_record):
    record = {**valid_sleep_record, "total_sleep_minutes": 0}
    errors = validate_sleep_record(record)
    duration_errors = [e for e in errors if e.reason == "sleep_duration_out_of_range"]
    assert duration_errors == []


def test_edge_case_max_duration_valid(valid_sleep_record):
    record = {**valid_sleep_record, "total_sleep_minutes": 1440}
    errors = validate_sleep_record(record)
    duration_errors = [e for e in errors if e.reason == "sleep_duration_out_of_range"]
    assert duration_errors == []
