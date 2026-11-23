import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import Connection, delete, text, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.bookings.domain.bookings import INACTIVE_STATUSES, BookingStatus
from holdfast.bookings.domain.errors import BookingConflictError
from holdfast.bookings.domain.jobs import JobKind, JobStatus
from holdfast.bookings.domain.periods import Period
from holdfast.bookings.domain.reminders import REMINDER_LEAD
from holdfast.bookings.repositories import bookings
from holdfast.bookings.repositories.records import Base, BookingRecord, JobRecord

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
    client: AsyncClient,
    organisation: Json,
    user: Json,
    book: Callable[..., Awaitable[Response]],
    free_resource: Json,
    stored_jobs: Callable[[], Awaitable[list[JobRecord]]],
) -> None:
    """Replays migration 0008 over bookings from before reminders existed. Only a confirmed
    booking that starts more than a day ahead, and has no reminder yet, not even a dead one,
    gets one."""
    reminded = (await book("09:00", "10:00", resource_id=free_resource["id"])).json()
    forgotten = (await book("11:00", "12:00", resource_id=free_resource["id"])).json()
    cancelled = (await book("13:00", "14:00", resource_id=free_resource["id"])).json()
    await client.post(f"/organisations/{organisation['id']}/bookings/{cancelled['id']}/cancel")
    await book("15:00", "16:00")  # held: it costs something and nobody confirmed it
    expired = (await book("17:00", "18:00")).json()
    soon = datetime.now(UTC) + timedelta(hours=2)
    async with app.state.sessions() as session, session.begin():
        await bookings.add(
            session,
            UUID(organisation["id"]),
            UUID(free_resource["id"]),
            UUID(user["id"]),
            Period(soon, soon + timedelta(hours=1)),
            price=0,
        )
        await session.execute(
            update(BookingRecord)
            .where(BookingRecord.id == UUID(expired["id"]))
            .values(status=BookingStatus.EXPIRED)
        )
        # As if every booking but the first had been confirmed before migration 0006, and the
        # first one's reminder had failed for good.
        await session.execute(
            delete(JobRecord).where(
                JobRecord.kind == JobKind.SEND_REMINDER,
                ~JobRecord.payload.contains({"booking_id": reminded["id"]}),
            )
        )
        await session.execute(
            update(JobRecord)
            .where(JobRecord.payload.contains({"booking_id": reminded["id"]}))
            .values(status=JobStatus.DEAD)
        )

    await asyncio.to_thread(command.downgrade, alembic_config, "0007")
    await asyncio.to_thread(command.upgrade, alembic_config, "head")

    reminders = [job for job in await stored_jobs() if job.kind is JobKind.SEND_REMINDER]
    assert sorted(job.payload["booking_id"] for job in reminders) == sorted(
        [reminded["id"], forgotten["id"]]
    )
    [queued] = [job for job in reminders if job.payload["booking_id"] == forgotten["id"]]
    assert queued.payload == {"organisation_id": organisation["id"], "booking_id": forgotten["id"]}
    assert (queued.status, queued.attempts) == (JobStatus.QUEUED, 0)
    assert queued.run_at == datetime.fromisoformat(forgotten["start"]) - REMINDER_LEAD


async def test_undoing_the_holds_migration_cancels_unpaid_holds_and_keeps_the_rest(
    app: FastAPI,
    alembic_config: Config,
    database_url: str,
    book: Callable[..., Awaitable[Response]],
    free_resource: Json,
) -> None:
    """The schema before migration 0006 knows neither held nor expired bookings, and its overlap
    constraint counts both as active. Undoing it must therefore cancel them before it rebuilds
    that constraint, since the held booking here sits in the slot the expired one freed. Redoing
    it afterwards works over the bookings that are left."""
    expired = (await book("09:00", "10:00")).json()
    async with app.state.sessions() as session, session.begin():
        await session.execute(
            update(BookingRecord)
            .where(BookingRecord.id == UUID(expired["id"]))
            .values(status=BookingStatus.EXPIRED)
        )
    held = (await book("09:00", "10:00")).json()
    confirmed = (await book("09:00", "10:00", resource_id=free_resource["id"])).json()

    await asyncio.to_thread(command.downgrade, alembic_config, "0005")
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT id::text, status FROM bookings"))
            statuses: dict[str, str] = dict(rows.all())
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.upgrade, alembic_config, "head")

    assert statuses == {
        held["id"]: "cancelled",
        expired["id"]: "cancelled",
        confirmed["id"]: "confirmed",
    }


@pytest.mark.parametrize(
    ("revision_before", "leftovers", "rebuilt", "gone"),
    [
        pytest.param(
            "0004",
            [
                "CREATE INDEX ix_bookings_resource_id_id ON bookings (id)",
                "CREATE INDEX ix_memberships_user_id ON memberships (organisation_id)",
            ],
            {
                "ix_bookings_resource_id_id": "(resource_id, id)",
                "ix_memberships_user_id": "(user_id)",
            },
            [],
            id="0005",
        ),
        pytest.param(
            "0006",
            [
                "CREATE UNIQUE INDEX uq_bookings_user_id_idempotency_key ON bookings (id)",
                "CREATE INDEX ix_bookings_active_user_id_end_rebuilt ON bookings (id)",
            ],
            {
                "uq_bookings_user_id_idempotency_key": "WHERE (idempotency_key IS NOT NULL)",
                "ix_bookings_active_user_id_end": "'expired'",
            },
            ["ix_bookings_active_user_id_end_rebuilt"],
            id="0007",
        ),
    ],
)
async def test_an_index_migration_recovers_when_run_again_after_failing_halfway(
    database_url: str,
    alembic_config: Config,
    revision_before: str,
    leftovers: list[str],
    rebuilt: dict[str, str],
    gone: list[str],
) -> None:
    """Concurrent index builds cannot be rolled back, so a failed run leaves indexes behind,
    possibly half built. The leftovers here carry the right names and the wrong definitions:
    running the migration again must replace them, not keep them."""
    await asyncio.to_thread(command.downgrade, alembic_config, revision_before)
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for leftover in leftovers:
                await connection.execute(text(leftover))

        await asyncio.to_thread(command.upgrade, alembic_config, "head")

        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes"
                    " WHERE tablename IN ('bookings', 'memberships')"
                )
            )
            indexes: dict[str, str] = dict(rows.all())
            differences = await connection.run_sync(_schema_differences)
    finally:
        await engine.dispose()
    for name, definition in rebuilt.items():
        assert definition in indexes[name]
    assert not set(gone) & set(indexes)
    assert differences == []


async def test_a_migration_outside_a_transaction_keeps_its_stamp_when_the_next_one_fails(
    database_url: str,
    alembic_config: Config,
    book: Callable[..., Awaitable[Response]],
    free_resource: Json,
) -> None:
    """Migration 0007 builds its indexes outside a transaction. Its revision stamp must commit
    with it, not with migration 0008: otherwise a failure of 0008 leaves the indexes built in a
    database that still says 0006."""
    await book("09:00", "10:00", resource_id=free_resource["id"])
    await asyncio.to_thread(command.downgrade, alembic_config, "0006")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            # Migration 0008 queues the reminder this booking lacks, which the check refuses.
            await connection.execute(text("DELETE FROM jobs"))
            await connection.execute(
                text("ALTER TABLE jobs ADD CONSTRAINT refuses_every_job CHECK (false)")
            )

        with pytest.raises(IntegrityError):
            await asyncio.to_thread(command.upgrade, alembic_config, "head")

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()
    assert revision == "0007"
