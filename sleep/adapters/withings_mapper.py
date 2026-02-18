"""Withings Sleep V2 → canonical SleepDay mapper.

Inbound anti-corruption layer: translates Withings' field names, units,
and types into the canonical SleepDay model.

Key differences from Oura:
- Timestamps are Unix epoch seconds (not ISO 8601)
- sleep_efficiency is already 0.0-1.0 (no conversion needed)
- source_record_id uses `id` if present, else f"{startdate}_{enddate}"
- Alias precedence: sleep_latency > durationtosleep, wakeup_latency > durationtowakeup
- Data fields are nested inside a "data" key in each series entry
- Entry-level "timezone" (IANA) must be applied to epoch timestamps
"""

import contextlib
import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sleep.domain.models import SleepDay, SleepSource


def _sec_to_min(seconds: int | None) -> int | None:
    """Convert seconds to minutes via integer division. None passthrough."""
    return seconds // 60 if seconds is not None else None


def _unix_to_dt(ts: int | None, tz: ZoneInfo | None = None) -> datetime | None:
    """Convert Unix timestamp to timezone-aware datetime.

    If tz is provided, the timestamp is interpreted in that timezone,
    preserving the true local moment. Otherwise falls back to UTC.
    """
    if ts is None:
        return None
    if tz is not None:
        return datetime.fromtimestamp(ts, tz=tz)
    return datetime.fromtimestamp(ts, tz=UTC)


class WithingsMapper:
    source_name = "withings"

    def parse(self, raw_response: dict[str, Any], patient_id: UUID) -> list[SleepDay]:
        """Parse Withings Sleep V2 getsummary response into canonical SleepDay records.

        The raw_response has shape: {"status": 0, "body": {"series": [...], "more": bool}}
        """
        body = raw_response.get("body", raw_response)
        series = body.get("series", [])
        results: list[SleepDay] = []

        for entry in series:
            effective = date.fromisoformat(entry["date"])
            data = entry.get("data", entry)

            # Parse entry timezone (IANA string, e.g. "America/Chicago")
            tz_name = entry.get("timezone")
            tz = ZoneInfo(tz_name) if tz_name else None

            # source_record_id: prefer `id`, fallback to startdate_enddate
            if "id" in entry:
                source_id = str(entry["id"])
            else:
                source_id = f"{entry.get('startdate', '')}_{entry.get('enddate', '')}"

            now = datetime.now(UTC)

            # Alias precedence: prefer sleep_latency over durationtosleep
            latency_sec = data.get("sleep_latency") or data.get("durationtosleep")
            wakeup_latency_sec = data.get("wakeup_latency") or data.get("durationtowakeup")

            # Parse night_events: may be a JSON string or already parsed
            night_events_raw = data.get("night_events")
            if isinstance(night_events_raw, str):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    night_events_raw = json.loads(night_events_raw)

            extra = {
                k: v
                for k, v in {
                    "sleep_score": data.get("sleep_score"),
                    "avg_hr_bpm": data.get("hr_average"),
                    "min_hr_bpm": data.get("hr_min"),
                    "max_hr_bpm": data.get("hr_max"),
                    "avg_rr": data.get("rr_average"),
                    "min_rr": data.get("rr_min"),
                    "max_rr": data.get("rr_max"),
                    "breathing_disturbances": data.get("breathing_disturbances_intensity"),
                    "snoring_seconds": data.get("snoring"),
                    "snoring_episode_count": data.get("snoringepisodecount"),
                    "wakeup_count": data.get("wakeupcount"),
                    "out_of_bed_count": data.get("out_of_bed_count"),
                    "latency_minutes": _sec_to_min(latency_sec),
                    "wakeup_latency_minutes": _sec_to_min(wakeup_latency_sec),
                    "time_in_bed_minutes": _sec_to_min(data.get("total_timeinbed")),
                    "waso_minutes": _sec_to_min(data.get("waso")),
                    "rem_episode_count": data.get("nb_rem_episodes"),
                    "asleep_duration_minutes": _sec_to_min(data.get("asleepduration")),
                    "hash_deviceid": entry.get("hash_deviceid"),
                    "night_events": night_events_raw,
                    "timezone": tz_name,
                }.items()
                if v is not None
            }

            sleep_day = SleepDay(
                patient_id=patient_id,
                source=SleepSource.WITHINGS,
                source_record_id=source_id,
                effective_date=effective,
                fingerprint=SleepDay.compute_fingerprint(
                    SleepSource.WITHINGS, source_id, effective
                ),
                raw_payload=entry,
                ingested_at=now,
                updated_at=now,
                total_sleep_minutes=_sec_to_min(data.get("total_sleep_time")),
                deep_sleep_minutes=_sec_to_min(data.get("deepsleepduration")),
                light_sleep_minutes=_sec_to_min(data.get("lightsleepduration")),
                rem_sleep_minutes=_sec_to_min(data.get("remsleepduration")),
                awake_minutes=_sec_to_min(data.get("wakeupduration")),
                sleep_onset=_unix_to_dt(entry.get("startdate"), tz=tz),
                sleep_offset=_unix_to_dt(entry.get("enddate"), tz=tz),
                sleep_efficiency=data.get("sleep_efficiency"),
                extra=extra,
            )
            results.append(sleep_day)

        return results
