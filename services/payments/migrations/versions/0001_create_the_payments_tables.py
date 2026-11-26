"""Create the ledger, the simulated provider's books, and the job queue, inbox and outbox.

Revision ID: 0001
Revises:
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The messaging library's tables, written out in full so this migration never changes when
    # the library does.
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.func.uuidv7(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('queued', 'dead')", name=op.f("ck_jobs_job_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_queued_run_at", "jobs", ["run_at"], postgresql_where=sa.text("status = 'queued'")
    )
    op.create_table(
        "received_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_received_messages")),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), server_default=sa.func.uuidv7(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_table(
        "charges",
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_charges_amount_positive")),
        sa.CheckConstraint(
            "status IN ('charged', 'declined', 'refunded')", name=op.f("ck_charges_charge_status")
        ),
        sa.PrimaryKeyConstraint("booking_id", name=op.f("pk_charges")),
    )
    op.create_table(
        "provider_charges",
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "succeeded OR refunded_at IS NULL",
            name=op.f("ck_provider_charges_refunded_only_if_succeeded"),
        ),
        sa.PrimaryKeyConstraint("idempotency_key", name=op.f("pk_provider_charges")),
    )


def downgrade() -> None:
    # The ledger and the provider's books go with their tables: never undo this on a database
    # that holds charges.
    op.drop_table("provider_charges")
    op.drop_table("charges")
    op.drop_table("outbox")
    op.drop_table("received_messages")
    op.drop_table("jobs")
