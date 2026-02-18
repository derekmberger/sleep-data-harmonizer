"""Initial schema: sleep_days, raw_vendor_responses, quarantine_records, idempotency_keys

Revision ID: 001
Revises: None
Create Date: 2026-02-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Ensure pgcrypto is available for gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- sleep_days (silver / normalized layer) ---
    op.create_table(
        "sleep_days",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_record_id", sa.Text, nullable=False),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("total_sleep_minutes", sa.Integer, nullable=True),
        sa.Column("deep_sleep_minutes", sa.Integer, nullable=True),
        sa.Column("light_sleep_minutes", sa.Integer, nullable=True),
        sa.Column("rem_sleep_minutes", sa.Integer, nullable=True),
        sa.Column("awake_minutes", sa.Integer, nullable=True),
        sa.Column("sleep_onset", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sleep_offset", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sleep_efficiency", sa.Float, nullable=True),
        sa.Column("extra", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # Constraints
        sa.UniqueConstraint("fingerprint", name="uq_sleep_days_fingerprint"),
        sa.UniqueConstraint("source", "source_record_id", name="uq_sleep_days_source_record"),
        sa.CheckConstraint("total_sleep_minutes >= 0", name="chk_total_sleep_minutes"),
        sa.CheckConstraint("deep_sleep_minutes >= 0", name="chk_deep_sleep_minutes"),
        sa.CheckConstraint("light_sleep_minutes >= 0", name="chk_light_sleep_minutes"),
        sa.CheckConstraint("rem_sleep_minutes >= 0", name="chk_rem_sleep_minutes"),
        sa.CheckConstraint("awake_minutes >= 0", name="chk_awake_minutes"),
        sa.CheckConstraint(
            "sleep_efficiency >= 0.0 AND sleep_efficiency <= 1.0",
            name="chk_sleep_efficiency",
        ),
        sa.CheckConstraint(
            "sleep_offset IS NULL OR sleep_onset IS NULL OR sleep_offset > sleep_onset",
            name="chk_sleep_offset_after_onset",
        ),
    )
    op.create_index(
        "idx_sleep_days_patient_date", "sleep_days", ["patient_id", sa.text("effective_date DESC")]
    )
    op.create_index("idx_sleep_days_ingested_at", "sleep_days", [sa.text("ingested_at DESC")])
    op.create_index("idx_sleep_days_updated_at", "sleep_days", [sa.text("updated_at DESC")])

    # updated_at trigger
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_sleep_days_updated_at
            BEFORE UPDATE ON sleep_days
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
    """)

    # --- raw_vendor_responses (bronze layer) ---
    op.create_table(
        "raw_vendor_responses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("request_params", postgresql.JSONB, nullable=True),
        sa.Column("response_body", postgresql.JSONB, nullable=False),
        sa.Column("http_status", sa.Integer, nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "idx_raw_source_date", "raw_vendor_responses", ["source", sa.text("fetched_at DESC")]
    )

    # --- quarantine_records ---
    op.create_table(
        "quarantine_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("pipeline_stage", sa.String(64), nullable=False),
        sa.Column("quarantine_reason", sa.String(128), nullable=False),
        sa.Column("quarantine_details", postgresql.JSONB, nullable=True),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("effective_date", sa.Date, nullable=True),
        sa.Column("raw_response_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(128), nullable=True),
        sa.Column("resolution_note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_quarantine_unresolved",
        "quarantine_records",
        ["resolved", "created_at"],
        postgresql_where=sa.text("resolved = FALSE"),
    )
    op.create_index("idx_quarantine_source", "quarantine_records", ["source", "created_at"])
    op.create_index("idx_quarantine_reason", "quarantine_records", ["quarantine_reason"])

    # --- idempotency_keys ---
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("response_body", postgresql.JSONB, nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '24 hours'"),
        ),
    )
    op.create_index("idx_idempotency_keys_expires", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sleep_days_updated_at ON sleep_days")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column")
    op.drop_table("idempotency_keys")
    op.drop_table("quarantine_records")
    op.drop_table("raw_vendor_responses")
    op.drop_table("sleep_days")
