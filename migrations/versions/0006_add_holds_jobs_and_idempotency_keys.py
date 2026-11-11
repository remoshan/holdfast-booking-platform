"""Add holds, jobs and idempotency keys.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_BEFORE = "status <> 'cancelled'"
ACTIVE_NOW = "status NOT IN ('cancelled', 'expired')"


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.func.uuidv7(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('expire_hold', 'send_reminder')", name=op.f("ck_jobs_job_kind")
        ),
        sa.CheckConstraint("status IN ('queued', 'dead')", name=op.f("ck_jobs_job_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_status_run_at", "jobs", ["status", "run_at"])

    op.add_column("bookings", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("idempotency_key", sa.String(), nullable=True))
    op.drop_constraint(op.f("ck_bookings_booking_status"), "bookings", type_="check")
    op.create_check_constraint(
        op.f("ck_bookings_booking_status"),
        "bookings",
        "status IN ('held', 'confirmed', 'cancelled', 'expired')",
    )
    op.create_check_constraint(
        op.f("ck_bookings_held_has_expiry"),
        "bookings",
        "status <> 'held' OR expires_at IS NOT NULL",
    )
    op.create_index(
        "uq_bookings_user_id_idempotency_key",
        "bookings",
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # An expired hold frees its slot, so the condition that marks a booking as active changes
    # in the exclusion constraint and in the index that shares it. Postgres cannot rebuild an
    # exclusion constraint without locking its table, so bookings can be neither read nor
    # written while this runs.
    _replace_active_condition(ACTIVE_NOW)


def downgrade() -> None:
    # The schema before this revision knows neither holds nor expiry: both give up their slot.
    op.execute("UPDATE bookings SET status = 'cancelled' WHERE status IN ('held', 'expired')")
    _replace_active_condition(ACTIVE_BEFORE)

    op.drop_index("uq_bookings_user_id_idempotency_key", table_name="bookings")
    op.drop_constraint(op.f("ck_bookings_held_has_expiry"), "bookings", type_="check")
    op.drop_constraint(op.f("ck_bookings_booking_status"), "bookings", type_="check")
    op.create_check_constraint(
        op.f("ck_bookings_booking_status"), "bookings", "status IN ('confirmed', 'cancelled')"
    )
    op.drop_column("bookings", "idempotency_key")
    op.drop_column("bookings", "expires_at")

    op.drop_index("ix_jobs_status_run_at", table_name="jobs")
    op.drop_table("jobs")


def _replace_active_condition(condition: str) -> None:
    op.drop_constraint("ex_bookings_no_overlap", "bookings")
    op.create_exclude_constraint(
        "ex_bookings_no_overlap",
        "bookings",
        ("resource_id", "="),
        ("period", "&&"),
        using="gist",
        where=condition,
    )
    op.drop_index("ix_bookings_active_user_id_end", table_name="bookings")
    op.create_index(
        "ix_bookings_active_user_id_end",
        "bookings",
        ["user_id", sa.text("upper(period)")],
        postgresql_where=sa.text(condition),
    )
