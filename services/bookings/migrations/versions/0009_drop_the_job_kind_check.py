"""Let each worker, not the database, decide which kinds of job it runs.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The job queue now belongs to the messaging library, which every service shares, and its
    # kinds belong to the handlers each worker registers. A job of a kind no handler knows fails
    # like any other job. Dropping a CHECK constraint scans nothing.
    op.drop_constraint(op.f("ck_jobs_job_kind"), "jobs", type_="check")


def downgrade() -> None:
    # Jobs of the kinds a later revision brings would break the constraint; workers of this
    # revision could not run them anyway.
    op.execute("DELETE FROM jobs WHERE kind NOT IN ('expire_hold', 'send_reminder')")
    op.create_check_constraint(
        op.f("ck_jobs_job_kind"), "jobs", "kind IN ('expire_hold', 'send_reminder')"
    )
