import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import Connection, delete, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.domain.bookings import INACTIVE_STATUSES, BookingStatus
from holdfast.domain.errors import BookingConflictError
from holdfast.domain.jobs import JobKind
from holdfast.domain.periods import Period
from holdfast.domain.reminders import REMINDER_LEAD
from holdfast.repositories import bookings
from holdfast.repositories.records import Base, BookingRecord, JobRecord

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


async def test_the_reminder_backfill_queues_what_confirming_would_have(
    app: FastAPI,
    alembic_config: Config,
    book: Callable[..., Awaitable[Response]],
    free_resource: Json,
    stored_jobs: Callable[[], Awaitable[list[JobRecord]]],
) -> None:
    """Replays migration 0008 over a booking confirmed before reminders existed, and over one
    that already has its reminder."""
    reminded = (await book("09:00", "10:00", resource_id=free_resource["id"])).json()
    forgotten = (await book("11:00", "12:00", resource_id=free_resource["id"])).json()
    async with app.state.sessions() as session, session.begin():
        # As if it had been confirmed before migration 0006.
        await session.execute(
            delete(JobRecord).where(JobRecord.payload.contains({"booking_id": forgotten["id"]}))
        )

    await asyncio.to_thread(command.downgrade, alembic_config, "0007")
    await asyncio.to_thread(command.upgrade, alembic_config, "head")

    jobs = await stored_jobs()
    booked = sorted(job.payload["booking_id"] for job in jobs)
    assert booked == sorted([reminded["id"], forgotten["id"]])
    reminders = {job.payload["booking_id"]: job for job in jobs}
    assert reminders[forgotten["id"]].kind is JobKind.SEND_REMINDER
    assert reminders[forgotten["id"]].run_at == (
        datetime.fromisoformat(forgotten["start"]) - REMINDER_LEAD
    )


async def test_the_index_migration_recovers_when_run_again_after_failing_halfway(
    database_url: str, alembic_config: Config
) -> None:
    """Concurrent index builds cannot be rolled back, so a failed run of 0007 leaves its first
    indexes behind. Running it again must step over them."""
    await asyncio.to_thread(command.downgrade, alembic_config, "0006")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE UNIQUE INDEX uq_bookings_user_id_idempotency_key"
                    " ON bookings (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
                )
            )
            await connection.execute(
                text("CREATE INDEX ix_bookings_active_user_id_end_rebuilt ON bookings (user_id)")
            )

        await asyncio.to_thread(command.upgrade, alembic_config, "head")

        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'bookings'")
            )
            indexes: dict[str, str] = dict(rows.all())
            differences = await connection.run_sync(_schema_differences)
    finally:
        await engine.dispose()
    assert "ix_bookings_active_user_id_end_rebuilt" not in indexes
    assert "'expired'" in indexes["ix_bookings_active_user_id_end"]
    assert "WHERE (idempotency_key IS NOT NULL)" in indexes["uq_bookings_user_id_idempotency_key"]
    assert differences == []
