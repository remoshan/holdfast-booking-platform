"""Create the job queue that runs the emails, and the record of the messages received.

Revision ID: 0001
Revises:
Create Date: 2026-11-26
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


def downgrade() -> None:
    # Emails not sent yet are lost with the jobs.
    op.drop_table("received_messages")
    op.drop_table("jobs")
