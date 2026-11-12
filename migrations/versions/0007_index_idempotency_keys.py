"""Index idempotency keys, and rebuild the upcoming bookings index without expired holds.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Built concurrently, so bookings keep flowing meanwhile (ADR-0011). If a build fails, drop
    # the invalid index it leaves and rerun.
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_bookings_user_id_idempotency_key",
            "bookings",
            ["user_id", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            postgresql_concurrently=True,
        )
        _rebuild_active_user_index(where="status NOT IN ('cancelled', 'expired')")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _rebuild_active_user_index(where="status <> 'cancelled'")
        op.drop_index(
            "uq_bookings_user_id_idempotency_key",
            table_name="bookings",
            postgresql_concurrently=True,
        )


def _rebuild_active_user_index(where: str) -> None:
    """Rebuild ix_bookings_active_user_id_end with a new condition. The new index is built next to
    the old one and takes its name once the old one is gone, so the query never goes without."""
    op.create_index(
        "ix_bookings_active_user_id_end_rebuilt",
        "bookings",
        ["user_id", sa.text("upper(period)")],
        postgresql_where=sa.text(where),
        postgresql_concurrently=True,
    )
    op.drop_index(
        "ix_bookings_active_user_id_end", table_name="bookings", postgresql_concurrently=True
    )
    op.execute(
        "ALTER INDEX ix_bookings_active_user_id_end_rebuilt"
        " RENAME TO ix_bookings_active_user_id_end"
    )
