"""SQLAlchemy ORM models for all four tables.

Tables:
- sleep_days: canonical normalized data (silver layer)
- raw_vendor_responses: exact vendor API responses (bronze layer)
- quarantine_records: failed records with raw payload + error details
- idempotency_keys: transport-layer idempotency tracking
"""

import enum
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SleepSourceEnum(enum.Enum):
    OURA = "oura"
    WITHINGS = "withings"


class SleepDayModel(Base):
    __tablename__ = "sleep_days"

    # Identity
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    # Provenance
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Temporal
    effective_date = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=lambda: datetime.now(UTC),
    )

    # Canonical sleep metrics
    total_sleep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deep_sleep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    light_sleep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rem_sleep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    awake_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sleep_onset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sleep_offset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sleep_efficiency: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Extension
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        UniqueConstraint("source", "source_record_id", name="uq_sleep_days_source_record"),
        CheckConstraint("total_sleep_minutes >= 0", name="chk_total_sleep_minutes"),
        CheckConstraint("deep_sleep_minutes >= 0", name="chk_deep_sleep_minutes"),
        CheckConstraint("light_sleep_minutes >= 0", name="chk_light_sleep_minutes"),
        CheckConstraint("rem_sleep_minutes >= 0", name="chk_rem_sleep_minutes"),
        CheckConstraint("awake_minutes >= 0", name="chk_awake_minutes"),
        CheckConstraint(
            "sleep_efficiency >= 0.0 AND sleep_efficiency <= 1.0",
            name="chk_sleep_efficiency",
        ),
        CheckConstraint(
            "sleep_offset IS NULL OR sleep_onset IS NULL OR sleep_offset > sleep_onset",
            name="chk_sleep_offset_after_onset",
        ),
        Index("idx_sleep_days_patient_date", "patient_id", effective_date.desc()),
        Index("idx_sleep_days_ingested_at", ingested_at.desc()),
        Index("idx_sleep_days_updated_at", updated_at.desc()),
    )


class RawVendorResponseModel(Base):
    __tablename__ = "raw_vendor_responses"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    request_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    batch_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    __table_args__ = (Index("idx_raw_source_date", "source", fetched_at.desc()),)


class QuarantineRecordModel(Base):
    __tablename__ = "quarantine_records"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    pipeline_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    quarantine_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    quarantine_details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_date = mapped_column(Date, nullable=True)
    raw_response_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    # Resolution tracking
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index(
            "idx_quarantine_unresolved",
            "resolved",
            "created_at",
            postgresql_where=text("resolved = FALSE"),
        ),
        Index("idx_quarantine_source", "source", "created_at"),
        Index("idx_quarantine_reason", "quarantine_reason"),
    )


class IdempotencyKeyModel(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now() + interval '24 hours'"),
    )

    __table_args__ = (Index("idx_idempotency_keys_expires", "expires_at"),)
