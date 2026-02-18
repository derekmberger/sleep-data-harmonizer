"""Ingestion pipeline: raw payload → map → validate → upsert/quarantine.

The pipeline is idempotent end-to-end:
- Same input always produces the same output
- Upsert handles dedup via fingerprint
- Safe to replay from raw_vendor_responses at any time

Note: This pipeline processes raw payloads already received via the POST
ingest endpoint. Live adapter fetch() methods are for future scheduled/polling
workflows, not the POST ingest path.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.metrics import (
    ingestion_records_total,
    pipeline_duration_seconds,
    validation_failures_total,
)
from sleep.adapters.factory import get_adapter
from sleep.domain.models import SleepDay
from sleep.domain.validation import validate_sleep_record
from sleep.repository import SleepDayRepository

logger = structlog.get_logger()


@dataclass
class IngestRecordResult:
    """Per-record result from the ingestion pipeline."""

    sleep_day_id: str
    status: str  # "created", "deduplicated", "quarantined"


@dataclass
class IngestResult:
    """Aggregate result from a batch ingestion."""

    results: list[IngestRecordResult] = field(default_factory=list)
    records_processed: int = 0
    records_inserted: int = 0
    records_updated: int = 0
    records_quarantined: int = 0
    batch_id: str = ""

    @property
    def has_inserts(self) -> bool:
        return self.records_inserted > 0


def compute_request_hash(body: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON for idempotency parameter mismatch detection."""
    canonical = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def ingest_sleep_data(
    session: AsyncSession,
    source: str,
    raw_payload: dict[str, Any],
    patient_id: UUID,
) -> IngestResult:
    """Run the full ingestion pipeline for a raw vendor payload.

    Steps:
    1. Store raw response (bronze layer)
    2. Parse via adapter (vendor → canonical)
    3. Validate each canonical record
    4. Upsert valid records / quarantine invalid ones

    Returns IngestResult with per-record status and aggregate counts.
    """
    start_time = time.monotonic()
    repo = SleepDayRepository(session)
    adapter = get_adapter(source)
    batch_id = uuid4()
    result = IngestResult(batch_id=str(batch_id))

    raw_id = await repo.store_raw_response(
        {
            "patient_id": patient_id,
            "source": source,
            "endpoint": f"/v2/{source}/sleep",
            "request_params": {},
            "response_body": raw_payload,
            "http_status": 200,
            "batch_id": batch_id,
        }
    )

    logger.info(
        "raw_response_stored",
        source=source,
        raw_id=str(raw_id),
        batch_id=str(batch_id),
    )

    try:
        sleep_days: list[SleepDay] = adapter.parse(raw_payload, patient_id)
    except Exception:
        logger.exception("adapter_parse_failed", source=source)
        await repo.quarantine(
            {
                "patient_id": patient_id,
                "source": source,
                "pipeline_stage": "adapter",
                "quarantine_reason": "adapter_parse_error",
                "quarantine_details": {"error": "Failed to parse vendor response"},
                "raw_payload": raw_payload,
                "raw_response_id": raw_id,
            }
        )
        await session.commit()
        result.records_quarantined = 1
        ingestion_records_total.labels(source=source, status="quarantined").inc()
        pipeline_duration_seconds.labels(source=source).observe(time.monotonic() - start_time)
        return result

    for sleep_day in sleep_days:
        record_dict = sleep_day.model_dump()
        errors = validate_sleep_record(record_dict)

        if errors:
            await repo.quarantine(
                {
                    "patient_id": patient_id,
                    "source": source,
                    "pipeline_stage": "validation",
                    "quarantine_reason": errors[0].reason,
                    "quarantine_details": [
                        {
                            "field": e.field,
                            "rule": e.rule,
                            "reason": e.reason,
                            "value": str(e.value),
                        }
                        for e in errors
                    ],
                    "raw_payload": sleep_day.raw_payload,
                    "fingerprint": sleep_day.fingerprint,
                    "effective_date": sleep_day.effective_date,
                    "raw_response_id": raw_id,
                }
            )
            result.records_quarantined += 1
            result.results.append(IngestRecordResult(sleep_day_id="", status="quarantined"))
            ingestion_records_total.labels(source=source, status="quarantined").inc()
            for e in errors:
                validation_failures_total.labels(source=source, rule=e.reason).inc()
            logger.warning(
                "record_quarantined",
                source=source,
                fingerprint=sleep_day.fingerprint,
                reasons=[e.reason for e in errors],
            )
            continue

        upsert_dict = {
            "id": sleep_day.id,
            "patient_id": sleep_day.patient_id,
            "source": sleep_day.source.value,
            "source_record_id": sleep_day.source_record_id,
            "raw_payload": sleep_day.raw_payload,
            "fingerprint": sleep_day.fingerprint,
            "effective_date": sleep_day.effective_date,
            "ingested_at": sleep_day.ingested_at,
            "updated_at": sleep_day.updated_at,
            "total_sleep_minutes": sleep_day.total_sleep_minutes,
            "deep_sleep_minutes": sleep_day.deep_sleep_minutes,
            "light_sleep_minutes": sleep_day.light_sleep_minutes,
            "rem_sleep_minutes": sleep_day.rem_sleep_minutes,
            "awake_minutes": sleep_day.awake_minutes,
            "sleep_onset": sleep_day.sleep_onset,
            "sleep_offset": sleep_day.sleep_offset,
            "sleep_efficiency": sleep_day.sleep_efficiency,
            "extra": sleep_day.extra,
        }
        try:
            db_result = await repo.upsert(upsert_dict)
        except IntegrityError as exc:
            await session.rollback()
            constraint = getattr(exc.orig, "constraint_name", "") or ""
            if "uq_sleep_days_source_record" in constraint or "source_record_id" in str(exc):
                await repo.quarantine(
                    {
                        "patient_id": patient_id,
                        "source": source,
                        "pipeline_stage": "upsert",
                        "quarantine_reason": "source_record_id_reused_across_dates",
                        "quarantine_details": {
                            "source_record_id": sleep_day.source_record_id,
                            "effective_date": str(sleep_day.effective_date),
                            "fingerprint": sleep_day.fingerprint,
                        },
                        "raw_payload": sleep_day.raw_payload,
                        "fingerprint": sleep_day.fingerprint,
                        "effective_date": sleep_day.effective_date,
                        "raw_response_id": raw_id,
                    }
                )
                result.records_quarantined += 1
                result.results.append(
                    IngestRecordResult(sleep_day_id="", status="quarantined")
                )
                ingestion_records_total.labels(source=source, status="quarantined").inc()
                logger.warning(
                    "record_quarantined",
                    source=source,
                    fingerprint=sleep_day.fingerprint,
                    reasons=["source_record_id_reused_across_dates"],
                )
                continue
            raise

        sleep_day_id = str(db_result["id"])

        if db_result["was_inserted"]:
            result.records_inserted += 1
            status = "created"
            ingestion_records_total.labels(source=source, status="created").inc()
        else:
            result.records_updated += 1
            status = "deduplicated"
            ingestion_records_total.labels(source=source, status="deduplicated").inc()

        result.records_processed += 1
        result.results.append(IngestRecordResult(sleep_day_id=sleep_day_id, status=status))

        logger.info(
            "record_upserted",
            source=source,
            fingerprint=sleep_day.fingerprint,
            status=status,
            sleep_day_id=sleep_day_id,
        )

    await session.commit()
    pipeline_duration_seconds.labels(source=source).observe(time.monotonic() - start_time)
    return result


async def replay_from_raw(
    session: AsyncSession,
    source: str,
    patient_id: UUID,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> dict[str, Any]:
    """Replay pipeline from raw vendor responses.

    Safe to run multiple times — idempotent by design.
    """
    from sqlalchemy import select

    from sleep.domain.orm import RawVendorResponseModel

    query = select(RawVendorResponseModel).where(
        RawVendorResponseModel.source == source,
        RawVendorResponseModel.patient_id == patient_id,
    )
    if start_date:
        query = query.where(RawVendorResponseModel.fetched_at >= start_date)
    if end_date:
        query = query.where(RawVendorResponseModel.fetched_at <= end_date)

    result = await session.execute(query)
    raw_records = result.scalars().all()

    total_stats = {"replayed": 0, "inserted": 0, "updated": 0, "quarantined": 0}

    for raw in raw_records:
        ingest_result = await ingest_sleep_data(session, source, raw.response_body, patient_id)
        total_stats["replayed"] += 1
        total_stats["inserted"] += ingest_result.records_inserted
        total_stats["updated"] += ingest_result.records_updated
        total_stats["quarantined"] += ingest_result.records_quarantined

    return total_stats
