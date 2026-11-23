"""Prevent overlapping bookings.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # btree_gist lets one GiST index combine equality on resource_id with overlap on period.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    # Fails if overlapping active bookings already exist; those must be resolved first.
    op.create_exclude_constraint(
        "ex_bookings_no_overlap",
        "bookings",
        ("resource_id", "="),
        ("period", "&&"),
        using="gist",
        where="status <> 'cancelled'",
    )


def downgrade() -> None:
    op.drop_constraint("ex_bookings_no_overlap", "bookings")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
