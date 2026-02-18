"""Oura V2 → canonical SleepDay mapper.

Inbound anti-corruption layer: translates Oura's field names, units,
and types into the canonical SleepDay model.

Filter policy: only process records where type == "long_sleep" and period == 0
(primary overnight sleep).
"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sleep.domain.models import SleepDay, SleepSource


def _sec_to_min(seconds: int | None) -> int | None:
    """Convert seconds to minutes via integer division. None passthrough."""
    return seconds // 60 if seconds is not None else None


def _pct_to_ratio(pct: int | None) -> float | None:
    """Convert 0-100 integer percentage to 0.0-1.0 float. None passthrough."""
    return round(pct / 100.0, 4) if pct is not None else None


def _parse_iso(iso_str: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp string to timezone-aware datetime."""
    return datetime.fromisoformat(iso_str) if iso_str else None


class OuraMapper:
    source_name = "oura"

    def parse(self, raw_response: dict[str, Any], patient_id: UUID) -> list[SleepDay]:
        """Parse Oura V2 sleep response into canonical SleepDay records.

        The raw_response is the full Oura API response with a "data" array.
        Only records with type=="long_sleep" and period==0 are processed.
        """
        records = raw_response.get("data", [])
        results: list[SleepDay] = []

        for entry in records:
            # Filter: only primary overnight sleep
            if entry.get("type") != "long_sleep":
                continue
            if entry.get("period", 0) != 0:
                continue

            effective = date.fromisoformat(entry["day"])
            source_id = entry["id"]
            now = datetime.now(UTC)

            extra = {
                k: v
                for k, v in {
                    "time_in_bed_minutes": _sec_to_min(entry.get("time_in_bed")),
                    "latency_minutes": _sec_to_min(entry.get("latency")),
                    "avg_hr_bpm": entry.get("average_heart_rate"),
                    "avg_hrv_ms": entry.get("average_hrv"),
                    "avg_breath_rate": entry.get("average_breath"),
                    "lowest_hr_bpm": entry.get("lowest_heart_rate"),
                    "restless_periods": entry.get("restless_periods"),
                    "sleep_type": entry.get("type"),
                    "sleep_phase_5_min": entry.get("sleep_phase_5_min"),
                    "movement_30_sec": entry.get("movement_30_sec"),
                    "readiness": entry.get("readiness"),
                    "readiness_score_delta": entry.get("readiness_score_delta"),
                    "sleep_score_delta": entry.get("sleep_score_delta"),
                    "sleep_algorithm_version": entry.get("sleep_algorithm_version"),
                    "sleep_analysis_reason": entry.get("sleep_analysis_reason"),
                }.items()
                if v is not None
            }

            sleep_day = SleepDay(
                patient_id=patient_id,
                source=SleepSource.OURA,
                source_record_id=source_id,
                effective_date=effective,
                fingerprint=SleepDay.compute_fingerprint(SleepSource.OURA, source_id, effective),
                raw_payload=entry,
                ingested_at=now,
                updated_at=now,
                total_sleep_minutes=_sec_to_min(entry.get("total_sleep_duration")),
                deep_sleep_minutes=_sec_to_min(entry.get("deep_sleep_duration")),
                light_sleep_minutes=_sec_to_min(entry.get("light_sleep_duration")),
                rem_sleep_minutes=_sec_to_min(entry.get("rem_sleep_duration")),
                awake_minutes=_sec_to_min(entry.get("awake_time")),
                sleep_onset=_parse_iso(entry.get("bedtime_start")),
                sleep_offset=_parse_iso(entry.get("bedtime_end")),
                sleep_efficiency=_pct_to_ratio(entry.get("efficiency")),
                extra=extra,
            )
            results.append(sleep_day)

        return results
