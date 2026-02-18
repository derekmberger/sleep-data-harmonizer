"""SleepDay repository — all DB access for the sleep domain.

Encapsulates upsert, timeline queries with cursor pagination,
summary aggregation, and provenance lookups.
"""

import base64
import json
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sleep.domain.orm import (
    IdempotencyKeyModel,
    QuarantineRecordModel,
    RawVendorResponseModel,
    SleepDayModel,
)


class SleepDayRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        """Insert or update a SleepDay by fingerprint. Returns result with was_inserted flag."""
        stmt = pg_insert(SleepDayModel).values(record)
        stmt = stmt.on_conflict_do_update(
            index_elements=["fingerprint"],
            set_={
                "total_sleep_minutes": stmt.excluded.total_sleep_minutes,
                "deep_sleep_minutes": stmt.excluded.deep_sleep_minutes,
                "light_sleep_minutes": stmt.excluded.light_sleep_minutes,
                "rem_sleep_minutes": stmt.excluded.rem_sleep_minutes,
                "awake_minutes": stmt.excluded.awake_minutes,
                "sleep_efficiency": stmt.excluded.sleep_efficiency,
                "sleep_onset": stmt.excluded.sleep_onset,
                "sleep_offset": stmt.excluded.sleep_offset,
                "extra": stmt.excluded.extra,
                "raw_payload": stmt.excluded.raw_payload,
                "updated_at": func.now(),
            },
        ).returning(SleepDayModel.id, text("(xmax = 0) AS was_inserted"))
        result = await self.session.execute(stmt)
        row = result.one()
        return {"id": row[0], "was_inserted": row[1]}

    async def get_timeline(
        self,
        patient_id: UUID,
        start: date | None = None,
        end: date | None = None,
        cursor: str | None = None,
        limit: int = 25,
        descending: bool = True,
    ) -> tuple[list[SleepDayModel], str | None]:
        """Fetch paginated sleep records for a patient.

        Returns (records, next_cursor). next_cursor is None if no more results.
        Uses keyset pagination on (effective_date, id).
        """
        query = select(SleepDayModel).where(SleepDayModel.patient_id == patient_id)

        if start:
            query = query.where(SleepDayModel.effective_date >= start)
        if end:
            query = query.where(SleepDayModel.effective_date < end)

        # Decode cursor
        if cursor:
            decoded = json.loads(base64.b64decode(cursor))
            cursor_date = date.fromisoformat(decoded["date"])
            cursor_id = UUID(decoded["id"])
            if descending:
                query = query.where(
                    (SleepDayModel.effective_date < cursor_date)
                    | (
                        (SleepDayModel.effective_date == cursor_date)
                        & (SleepDayModel.id < cursor_id)
                    )
                )
            else:
                query = query.where(
                    (SleepDayModel.effective_date > cursor_date)
                    | (
                        (SleepDayModel.effective_date == cursor_date)
                        & (SleepDayModel.id > cursor_id)
                    )
                )

        if descending:
            query = query.order_by(SleepDayModel.effective_date.desc(), SleepDayModel.id.desc())
        else:
            query = query.order_by(SleepDayModel.effective_date.asc(), SleepDayModel.id.asc())

        # Fetch limit+1 to determine has_more
        query = query.limit(limit + 1)
        result = await self.session.execute(query)
        rows = list(result.scalars().all())

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        # Build next cursor
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            cursor_data = {"date": last.effective_date.isoformat(), "id": str(last.id)}
            next_cursor = base64.b64encode(json.dumps(cursor_data).encode()).decode()

        return rows, next_cursor

    async def get_summary(self, patient_id: UUID, start: date, end: date) -> dict[str, Any]:
        """Get aggregated sleep metrics for a patient over a date range."""
        query = (
            select(
                func.count().label("record_count"),
                func.avg(SleepDayModel.total_sleep_minutes).label("avg_total_sleep_minutes"),
                func.avg(SleepDayModel.deep_sleep_minutes).label("avg_deep_sleep_minutes"),
                func.avg(SleepDayModel.light_sleep_minutes).label("avg_light_sleep_minutes"),
                func.avg(SleepDayModel.rem_sleep_minutes).label("avg_rem_sleep_minutes"),
                func.avg(SleepDayModel.awake_minutes).label("avg_awake_minutes"),
                func.avg(SleepDayModel.sleep_efficiency).label("avg_sleep_efficiency"),
                func.array_agg(func.distinct(SleepDayModel.source)).label("sources"),
            )
            .where(SleepDayModel.patient_id == patient_id)
            .where(SleepDayModel.effective_date >= start)
            .where(SleepDayModel.effective_date < end)
        )
        result = await self.session.execute(query)
        row = result.one()

        def _round_or_none(val: Any) -> float | None:
            return round(float(val), 2) if val is not None else None

        return {
            "patient_id": str(patient_id),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "record_count": row.record_count,
            "avg_total_sleep_minutes": _round_or_none(row.avg_total_sleep_minutes),
            "avg_sleep_efficiency": _round_or_none(row.avg_sleep_efficiency),
            "avg_stages": {
                "deep_sleep_minutes": _round_or_none(row.avg_deep_sleep_minutes),
                "light_sleep_minutes": _round_or_none(row.avg_light_sleep_minutes),
                "rem_sleep_minutes": _round_or_none(row.avg_rem_sleep_minutes),
                "awake_minutes": _round_or_none(row.avg_awake_minutes),
            },
            "sources": [s for s in (row.sources or []) if s is not None],
        }

    async def get_provenance(
        self,
        patient_id: UUID,
        start: date | None = None,
        end: date | None = None,
        source: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Get provenance metadata for a patient's sleep records."""
        query = select(
            SleepDayModel.id,
            SleepDayModel.source,
            SleepDayModel.source_record_id,
            SleepDayModel.fingerprint,
            SleepDayModel.effective_date,
            SleepDayModel.ingested_at,
            SleepDayModel.updated_at,
        ).where(SleepDayModel.patient_id == patient_id)

        if start:
            query = query.where(SleepDayModel.effective_date >= start)
        if end:
            query = query.where(SleepDayModel.effective_date < end)
        if source:
            query = query.where(SleepDayModel.source == source)

        if cursor:
            decoded = json.loads(base64.b64decode(cursor))
            cursor_date = date.fromisoformat(decoded["date"])
            cursor_id = UUID(decoded["id"])
            query = query.where(
                (SleepDayModel.effective_date < cursor_date)
                | ((SleepDayModel.effective_date == cursor_date) & (SleepDayModel.id < cursor_id))
            )

        query = query.order_by(SleepDayModel.effective_date.desc(), SleepDayModel.id.desc()).limit(
            limit + 1
        )
        result = await self.session.execute(query)
        rows = list(result.all())

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        records = [
            {
                "sleep_day_id": str(r[0]),
                "source": r[1],
                "source_record_id": r[2],
                "fingerprint": r[3],
                "effective_date": r[4].isoformat(),
                "ingested_at": r[5].isoformat(),
                "updated_at": r[6].isoformat(),
            }
            for r in rows
        ]

        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            cursor_data = {"date": last[4].isoformat(), "id": str(last[0])}
            next_cursor = base64.b64encode(json.dumps(cursor_data).encode()).decode()

        return records, next_cursor

    async def store_raw_response(self, record: dict[str, Any]) -> UUID:
        """Store a raw vendor API response (bronze layer). Append-only."""
        stmt = pg_insert(RawVendorResponseModel).values(record).returning(RawVendorResponseModel.id)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def quarantine(self, record: dict[str, Any]) -> UUID:
        """Store a quarantined record."""
        stmt = pg_insert(QuarantineRecordModel).values(record).returning(QuarantineRecordModel.id)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def atomic_claim_idempotency_key(
        self, key: str, source: str, request_hash: str
    ) -> dict[str, Any]:
        """Atomically claim an idempotency key or return existing state.

        Uses DELETE expired + INSERT ... ON CONFLICT DO NOTHING + SELECT to
        avoid race conditions and ensure expired keys don't block new claims.

        Returns a dict with "status" key:
          - "claimed": key was successfully claimed for processing
          - "completed": key was already processed (includes status_code + response_body)
          - "in_flight": key is currently being processed by another request
          - "conflict": key exists with a different request_hash (parameter mismatch)
        """
        # Purge expired row for this key so it doesn't block re-use
        await self.session.execute(
            delete(IdempotencyKeyModel).where(
                IdempotencyKeyModel.key == key,
                IdempotencyKeyModel.expires_at <= func.now(),
            )
        )

        # Attempt atomic insert with locked_at set
        stmt = (
            pg_insert(IdempotencyKeyModel)
            .values(
                key=key,
                source=source,
                request_hash=request_hash,
                locked_at=func.now(),
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        result = await self.session.execute(stmt)

        if result.rowcount == 1:
            return {"status": "claimed"}

        # Key already exists and is not expired — read its current state
        query = select(IdempotencyKeyModel).where(
            IdempotencyKeyModel.key == key,
            IdempotencyKeyModel.expires_at > func.now(),
        )
        existing_result = await self.session.execute(query)
        existing = existing_result.scalar_one_or_none()

        if existing is None:
            # Row vanished (concurrent purge) — retry insert
            retry_result = await self.session.execute(stmt)
            if retry_result.rowcount == 1:
                return {"status": "claimed"}
            # Another request won the race; fall through not possible in practice
            return {"status": "in_flight"}

        if existing.request_hash != request_hash:
            return {"status": "conflict"}

        if existing.status_code is not None:
            return {
                "status": "completed",
                "status_code": existing.status_code,
                "response_body": existing.response_body,
            }

        # Still in flight (locked_at set, no status_code yet)
        return {"status": "in_flight"}

    async def complete_idempotency_key(
        self, key: str, status_code: int, response_body: dict
    ) -> None:
        """Mark an idempotency key as completed with the response."""
        from sqlalchemy import update

        stmt = (
            update(IdempotencyKeyModel)
            .where(IdempotencyKeyModel.key == key)
            .values(
                status_code=status_code,
                response_body=response_body,
                locked_at=None,
            )
        )
        await self.session.execute(stmt)
