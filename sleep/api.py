"""FastAPI router for the Sleep domain.

Endpoints:
- POST /api/v1/ingest/{source}/sleep  (requires Idempotency-Key)
- GET  /api/v1/patients/{id}/sleep/timeline
- GET  /api/v1/patients/{id}/sleep/summary
- GET  /api/v1/patients/{id}/sleep/provenance
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sleep.domain.orm import SleepDayModel

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import get_session
from shared.exceptions import (
    IdempotencyConflictError,
    IdempotencyInFlightError,
    InvalidDateRangeError,
    InvalidSortError,
    MissingIdempotencyKeyError,
    UnsupportedSourceError,
)
from shared.metrics import api_requests_total, api_response_duration_seconds
from shared.middleware import request_id_var
from sleep.pipeline import compute_request_hash, ingest_sleep_data
from sleep.repository import SleepDayRepository

router = APIRouter(prefix="/api/v1")


# --- Request models ---


class IngestRequest(BaseModel):
    """Request body for the ingest endpoint."""

    patient_id: UUID
    data: dict[str, Any] = Field(..., description="Raw vendor sleep data payload")


# --- Response helpers ---


def _meta() -> dict[str, Any]:
    return {
        "request_id": request_id_var.get(""),
        "timestamp": datetime.now(UTC).isoformat(),
        "api_version": settings.api_version,
    }


def _model_to_dict(row: SleepDayModel) -> dict[str, Any]:
    """Convert a SleepDayModel ORM row to an API response dict."""
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "source": row.source,
        "effective_date": row.effective_date.isoformat(),
        "total_sleep_minutes": row.total_sleep_minutes,
        "sleep_efficiency": row.sleep_efficiency,
        "sleep_onset": row.sleep_onset.isoformat() if row.sleep_onset else None,
        "sleep_offset": row.sleep_offset.isoformat() if row.sleep_offset else None,
        "stages": {
            "deep_sleep_minutes": row.deep_sleep_minutes,
            "light_sleep_minutes": row.light_sleep_minutes,
            "rem_sleep_minutes": row.rem_sleep_minutes,
            "awake_minutes": row.awake_minutes,
        },
    }


# --- Endpoints ---


@router.post("/ingest/{source}/sleep", status_code=201)
async def ingest_sleep(
    source: str,
    body: IngestRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """Ingest raw vendor sleep data. Requires Idempotency-Key header.

    Per-record status enum: created | deduplicated | quarantined
    - created: new record inserted into sleep_days
    - deduplicated: record matched existing fingerprint (upsert, no new row)
    - quarantined: record failed data-quality validation (stored for review)

    When quarantined, sleep_day_id is null.

    HTTP status codes:
    - 201: at least one record was created
    - 200: all records deduplicated or quarantined (no new inserts)
    - 422: request-shape validation error (Pydantic, not data-quality)
    """
    start_time = time.monotonic()
    if not idempotency_key:
        raise MissingIdempotencyKeyError()

    if source not in ("oura", "withings"):
        raise UnsupportedSourceError(source)

    request_hash = compute_request_hash(body.model_dump(mode="json"))
    repo = SleepDayRepository(session)

    claim = await repo.atomic_claim_idempotency_key(idempotency_key, source, request_hash)

    if claim["status"] == "conflict":
        raise IdempotencyConflictError(idempotency_key)
    if claim["status"] == "in_flight":
        raise IdempotencyInFlightError(idempotency_key)
    if claim["status"] == "completed":
        response.status_code = claim["status_code"]
        api_requests_total.labels(
            endpoint="ingest", method="POST", status_code=str(claim["status_code"])
        ).inc()
        return {
            "data": claim["response_body"],
            "meta": _meta(),
        }

    # claim["status"] == "claimed" — proceed with pipeline
    await session.commit()

    ingest_result = await ingest_sleep_data(session, source, body.data, body.patient_id)
    if len(ingest_result.results) == 1:
        r = ingest_result.results[0]
        response_data = {
            "sleep_day_id": r.sleep_day_id or None,
            "status": r.status,
        }
    else:
        response_data = {
            "results": [
                {"sleep_day_id": r.sleep_day_id or None, "status": r.status}
                for r in ingest_result.results
            ],
            "records_processed": ingest_result.records_processed,
            "records_inserted": ingest_result.records_inserted,
            "records_updated": ingest_result.records_updated,
            "records_quarantined": ingest_result.records_quarantined,
        }

    status_code = 201 if ingest_result.has_inserts else 200
    response.status_code = status_code

    # Store result in idempotency key
    await repo.complete_idempotency_key(idempotency_key, status_code, response_data)
    await session.commit()

    duration = time.monotonic() - start_time
    api_requests_total.labels(endpoint="ingest", method="POST", status_code=str(status_code)).inc()
    api_response_duration_seconds.labels(endpoint="ingest").observe(duration)

    return {"data": response_data, "meta": _meta()}


@router.get("/patients/{patient_id}/sleep/timeline")
async def get_timeline(
    patient_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    start: date | None = Query(None),
    end: date | None = Query(None),
    sort: str = Query("-effective_date"),
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None),
):
    """Get time-ordered sleep records for a patient with cursor pagination."""
    start_time = time.monotonic()

    allowed_sorts = {"effective_date", "-effective_date"}
    if sort not in allowed_sorts:
        raise InvalidSortError(sort, allowed_sorts)

    if start and end and start >= end:
        raise InvalidDateRangeError(str(start), str(end))

    descending = sort == "-effective_date"
    repo = SleepDayRepository(session)

    rows, next_cursor = await repo.get_timeline(
        patient_id=patient_id,
        start=start,
        end=end,
        cursor=cursor,
        limit=limit,
        descending=descending,
    )

    data = [_model_to_dict(r) for r in rows]
    pagination = {
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "limit": limit,
    }

    # Link header (RFC 8288)
    if next_cursor:
        base_path = f"/api/v1/patients/{patient_id}/sleep/timeline"
        response.headers["Link"] = f'<{base_path}?cursor={next_cursor}>; rel="next"'

    duration = time.monotonic() - start_time
    api_requests_total.labels(endpoint="timeline", method="GET", status_code="200").inc()
    api_response_duration_seconds.labels(endpoint="timeline").observe(duration)

    return {"data": data, "meta": _meta(), "pagination": pagination}


@router.get("/patients/{patient_id}/sleep/summary")
async def get_summary(
    patient_id: UUID,
    session: AsyncSession = Depends(get_session),
    start: date = Query(...),
    end: date = Query(...),
):
    """Get aggregated sleep summary for a patient over a date range."""
    start_time = time.monotonic()
    if start >= end:
        raise InvalidDateRangeError(str(start), str(end))

    repo = SleepDayRepository(session)
    summary = await repo.get_summary(patient_id, start, end)

    duration = time.monotonic() - start_time
    api_requests_total.labels(endpoint="summary", method="GET", status_code="200").inc()
    api_response_duration_seconds.labels(endpoint="summary").observe(duration)

    return {"data": summary, "meta": _meta()}


@router.get("/patients/{patient_id}/sleep/provenance")
async def get_provenance(
    patient_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    start: date | None = Query(None),
    end: date | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None),
):
    """Get data provenance for a patient's sleep records."""
    start_time = time.monotonic()
    if start and end and start >= end:
        raise InvalidDateRangeError(str(start), str(end))

    repo = SleepDayRepository(session)
    records, next_cursor = await repo.get_provenance(
        patient_id=patient_id,
        start=start,
        end=end,
        source=source,
        cursor=cursor,
        limit=limit,
    )

    pagination = {
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "limit": limit,
    }

    if next_cursor:
        base_path = f"/api/v1/patients/{patient_id}/sleep/provenance"
        response.headers["Link"] = f'<{base_path}?cursor={next_cursor}>; rel="next"'

    duration = time.monotonic() - start_time
    api_requests_total.labels(endpoint="provenance", method="GET", status_code="200").inc()
    api_response_duration_seconds.labels(endpoint="provenance").observe(duration)

    return {"data": records, "meta": _meta(), "pagination": pagination}
