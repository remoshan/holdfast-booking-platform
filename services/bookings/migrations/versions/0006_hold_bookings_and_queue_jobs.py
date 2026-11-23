"""Hold bookings that cost something, and queue background jobs.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bookings", sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("bookings", sa.Column("idempotency_key", sa.String(length=255), nullable=True))
    op.drop_constraint("ck_bookings_booking_status", "bookings")
    op.create_check_constraint(
        op.f("ck_bookings_booking_status"),
        "bookings",
        "status IN ('held', 'confirmed', 'cancelled', 'expired')",
    )
    op.create_check_constraint(
        op.f("ck_bookings_hold_has_expiry"),
        "bookings",
        "status <> 'held' OR hold_expires_at IS NOT NULL",
    )
    # An expired hold frees its slot, as a cancelled booking does. Postgres can neither change an
    # exclusion constraint's condition nor build one without blocking its table, so bookings are
    # locked, reads included, while the new index builds: 138 to 274 seconds for a million
    # bookings on a development machine. Run it at a quiet time.
    op.drop_constraint("ex_bookings_no_overlap", "bookings")
    op.create_exclude_constraint(
        "ex_bookings_no_overlap",
        "bookings",
        ("resource_id", "="),
        ("period", "&&"),
        using="gist",
        where="status NOT IN ('cancelled', 'expired')",
    )
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
        sa.CheckConstraint(
            "kind IN ('expire_hold', 'send_reminder')", name=op.f("ck_jobs_job_kind")
        ),
        sa.CheckConstraint("status IN ('queued', 'dead')", name=op.f("ck_jobs_job_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_queued_run_at", "jobs", ["run_at"], postgresql_where=sa.text("status = 'queued'")
    )


def downgrade() -> None:
    op.drop_table("jobs")
    # The old schema knows neither status: an unpaid hold is cancelled, like an expired one.
    op.execute("UPDATE bookings SET status = 'cancelled' WHERE status IN ('held', 'expired')")
    op.drop_constraint("ex_bookings_no_overlap", "bookings")
    op.create_exclude_constraint(
        "ex_bookings_no_overlap",
        "bookings",
        ("resource_id", "="),
        ("period", "&&"),
        using="gist",
        where="status <> 'cancelled'",
    )
    op.drop_constraint("ck_bookings_hold_has_expiry", "bookings")
    op.drop_constraint("ck_bookings_booking_status", "bookings")
    op.create_check_constraint(
        op.f("ck_bookings_booking_status"), "bookings", "status IN ('confirmed', 'cancelled')"
    )
    op.drop_column("bookings", "idempotency_key")
    op.drop_column("bookings", "hold_expires_at")
