"""Add core booking tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=sa.func.uuidv7(), nullable=False)


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    # No endpoint could create organisations before this revision, so the table is empty.
    op.add_column("organisations", sa.Column("currency", sa.String(length=3), nullable=False))

    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.String(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "locations",
        _id(),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("opening_hours", postgresql.JSONB(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_locations_organisation_id_organisations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locations")),
        sa.UniqueConstraint("organisation_id", "id", name=op.f("uq_locations_organisation_id_id")),
    )
    op.create_table(
        "resources",
        _id(),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("hourly_price", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint("capacity > 0", name=op.f("ck_resources_capacity_positive")),
        sa.CheckConstraint(
            "hourly_price >= 0", name=op.f("ck_resources_hourly_price_not_negative")
        ),
        sa.CheckConstraint(
            "kind IN ('desk', 'meeting_room')", name=op.f("ck_resources_resource_kind")
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "location_id"],
            ["locations.organisation_id", "locations.id"],
            name=op.f("fk_resources_organisation_id_locations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources")),
        sa.UniqueConstraint("organisation_id", "id", name=op.f("uq_resources_organisation_id_id")),
    )
    op.create_table(
        "bookings",
        _id(),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("period", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint("NOT isempty(period)", name=op.f("ck_bookings_period_not_empty")),
        sa.CheckConstraint("price >= 0", name=op.f("ck_bookings_price_not_negative")),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled')", name=op.f("ck_bookings_booking_status")
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id", "resource_id"],
            ["resources.organisation_id", "resources.id"],
            name=op.f("fk_bookings_organisation_id_resources"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_bookings_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
    )


def downgrade() -> None:
    op.drop_table("bookings")
    op.drop_table("resources")
    op.drop_table("locations")
    op.drop_table("users")
    op.drop_column("organisations", "currency")
