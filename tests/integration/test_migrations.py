from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.domain.bookings import INACTIVE_STATUSES, BookingStatus
from holdfast.domain.errors import BookingConflictError
from holdfast.domain.periods import Period
from holdfast.repositories import bookings
from holdfast.repositories.records import Base, BookingRecord

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]


def _schema_differences(connection: Connection) -> list[object]:
    return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))


async def test_migrations_match_records(database_url: str) -> None:
    """Fails when a record changes without a matching migration."""
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        differences = await connection.run_sync(_schema_differences)
    await engine.dispose()

    assert differences == []


@pytest.mark.parametrize("status", list(BookingStatus))
async def test_only_inactive_bookings_leave_their_slot_free(
    app: FastAPI,
    organisation: Json,
    resource: Json,
    user: Json,
    at: Callable[..., str],
    status: BookingStatus,
) -> None:
    """Fails when a status becomes inactive without a migration that frees its slot, which the
    comparison above cannot see: Alembic does not compare exclusion constraints."""
    organisation_id, resource_id, user_id = (
        UUID(entity["id"]) for entity in (organisation, resource, user)
    )
    period = Period(
        *(datetime.fromisoformat(at(clock)).astimezone(UTC) for clock in ("09:00", "10:00"))
    )
    async with app.state.sessions() as session, session.begin():
        session.add(
            BookingRecord(
                organisation_id=organisation_id,
                resource_id=resource_id,
                user_id=user_id,
                period=Range(period.start, period.end, bounds="[)"),
                status=status,
                price=0,
                hold_expires_at=period.start,
            )
        )

    async with app.state.sessions() as session, session.begin():
        try:
            await bookings.add(session, organisation_id, resource_id, user_id, period, price=0)
        except BookingConflictError:
            slot_free = False
        else:
            slot_free = True

    assert slot_free is (status in INACTIVE_STATUSES)
