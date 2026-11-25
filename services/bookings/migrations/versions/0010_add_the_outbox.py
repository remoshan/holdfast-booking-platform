"""Give the booking service an outbox for the events it publishes.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The messaging library's outbox, written out in full so this migration never changes when
    # the library does. A new, empty table: nothing waits on it.
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


def downgrade() -> None:
    # Events that have not been published yet are lost with the table.
    op.drop_table("outbox")
