"""Index the hot queries.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain CREATE INDEX blocks writes to its table until the index is built, which takes
    # seconds on a million bookings. CONCURRENTLY lets bookings continue meanwhile, but cannot
    # run inside a transaction. If a build fails, drop the invalid index it leaves and rerun.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_bookings_resource_id_id",
            "bookings",
            ["resource_id", "id"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_bookings_active_user_id_end",
            "bookings",
            ["user_id", sa.text("upper(period)")],
            postgresql_where=sa.text("status <> 'cancelled'"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_memberships_user_id", "memberships", ["user_id"], postgresql_concurrently=True
        )
        op.create_index(
            "ix_refresh_tokens_family_id",
            "refresh_tokens",
            ["family_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table in (
            ("ix_refresh_tokens_family_id", "refresh_tokens"),
            ("ix_memberships_user_id", "memberships"),
            ("ix_bookings_active_user_id_end", "bookings"),
            ("ix_bookings_resource_id_id", "bookings"),
        ):
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
