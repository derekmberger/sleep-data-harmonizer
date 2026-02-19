"""Canonical validation rules for SleepDay records.

9 rules that validate canonical-level fields only.
Vendor-specific fields in `extra` JSONB are not validated here.
Returns a list of ValidationError; empty list means valid.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass
class ValidationError:
    field: str
    rule: str
    reason: str
    value: Any


ALLOWED_SOURCES = {"oura", "withings"}
_MAX_DAILY_MINUTES = 1440
_STAGE_SUM_TOLERANCE = 1.05


def validate_sleep_record(record: dict[str, Any]) -> list[ValidationError]:
    """Validate a normalized sleep record before upsert.

    Returns an empty list if valid; otherwise returns all violations.
    """
    errors: list[ValidationError] = []
    today = date.today()

    # Rule 1: Required effective_date
    eff = record.get("effective_date")
    if not eff:
        errors.append(ValidationError("effective_date", "required", "missing_effective_date", None))

    # Rule 2: Sleep duration range [0, 1440]
    total = record.get("total_sleep_minutes")
    if total is not None and (
        not isinstance(total, (int, float)) or total < 0 or total > _MAX_DAILY_MINUTES
    ):
        errors.append(
            ValidationError("total_sleep_minutes", "range", "sleep_duration_out_of_range", total)
        )

    # Rule 3: Non-negative stages
    for stage_field in (
        "deep_sleep_minutes",
        "light_sleep_minutes",
        "rem_sleep_minutes",
        "awake_minutes",
    ):
        val = record.get(stage_field)
        if val is not None and (not isinstance(val, (int, float)) or val < 0):
            errors.append(ValidationError(stage_field, "non_negative", "negative_sleep_stage", val))

    # Rule 4: Stage sum consistency (5% tolerance for rounding)
    stages = [
        record.get(f, 0) or 0
        for f in ("deep_sleep_minutes", "light_sleep_minutes", "rem_sleep_minutes", "awake_minutes")
    ]
    stage_sum = sum(stages)
    if (
        total is not None
        and isinstance(total, (int, float))
        and stage_sum > total * _STAGE_SUM_TOLERANCE
    ):
        errors.append(
            ValidationError(
                "stage_sum",
                "consistency",
                "stage_sum_exceeds_total",
                {"stage_sum": stage_sum, "total": total},
            )
        )

    # Rule 5: No future dates
    if eff:
        eff_date = eff if isinstance(eff, date) else date.fromisoformat(str(eff))
        if eff_date > today:
            errors.append(ValidationError("effective_date", "no_future", "future_date", str(eff)))

    # Rule 6: Valid efficiency [0.0, 1.0]
    efficiency = record.get("sleep_efficiency")
    if efficiency is not None and (efficiency < 0.0 or efficiency > 1.0):
        errors.append(
            ValidationError("sleep_efficiency", "range", "efficiency_out_of_range", efficiency)
        )

    # Rule 7: Timezone on timestamps
    for ts_field in ("sleep_onset", "sleep_offset"):
        ts = record.get(ts_field)
        if ts is not None and isinstance(ts, datetime) and ts.tzinfo is None:
            errors.append(ValidationError(ts_field, "timezone", "missing_timezone", str(ts)))

    # Rule 8: Known source
    source = record.get("source")
    if source:
        source_val = getattr(source, "value", source)
        if source_val not in ALLOWED_SOURCES:
            errors.append(ValidationError("source", "known_source", "unknown_source", source_val))

    # Rule 9: Sleep window ordering (onset < offset)
    # Skip comparison if either timestamp failed timezone check (Rule 7)
    onset = record.get("sleep_onset")
    offset = record.get("sleep_offset")
    if (
        onset
        and offset
        and isinstance(onset, datetime)
        and isinstance(offset, datetime)
        and onset.tzinfo is not None
        and offset.tzinfo is not None
        and onset >= offset
    ):
        errors.append(
            ValidationError(
                "sleep_onset",
                "ordering",
                "bedtime_order_invalid",
                {"onset": str(onset), "offset": str(offset)},
            )
        )

    return errors
