"""Queue reminders for bookings confirmed before reminders existed.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Confirming a booking queues its reminder, but bookings confirmed before migration 0006 had
    # no job table to queue one in. Each that starts more than a day from now gets the reminder
    # confirming would have queued, unless it already has one.
    op.execute(
        """
        INSERT INTO jobs (kind, payload, status, attempts, run_at)
        SELECT 'send_reminder',
               jsonb_build_object('organisation_id', organisation_id, 'booking_id', id),
               'queued',
               0,
               lower(period) - interval '24 hours'
        FROM bookings
        WHERE status = 'confirmed'
          AND lower(period) - interval '24 hours' > now()
          AND NOT EXISTS (
              SELECT FROM jobs
              WHERE jobs.kind = 'send_reminder'
                AND jobs.payload ->> 'booking_id' = bookings.id::text
          )
        """
    )


def downgrade() -> None:
    # The queued reminders are ordinary jobs now, which the worker sends or has sent. Migration
    # 0006's downgrade removes the whole table.
    pass
