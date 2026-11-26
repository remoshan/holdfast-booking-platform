"""A worker process killed after the provider charged a booking and before its job committed: the
worst moment, since the money has moved and nothing on the service's side records it."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.records import JobRecord
from holdfast.payments.messages import CHARGE_BOOKING, CHARGE_SUCCEEDED
from holdfast.payments.records import ChargeRecord, ChargeStatus, ProviderChargeRecord
from tests.support import Json, about_booking, charge_command

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type Receive = Callable[[str, Json, datetime], Awaitable[None]]
type Published = Callable[[], Awaitable[list[tuple[str, Json]]]]
type Ledger = Callable[[], Awaitable[list[ChargeRecord]]]
type Books = Callable[[], Awaitable[list[ProviderChargeRecord]]]

WAIT_FOR_WORKER_SECONDS = 30


async def test_a_worker_killed_after_the_provider_charged_leaves_one_charge_and_one_reply(
    sessions: Sessions,
    database_url: str,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
) -> None:
    booking_id = uuid4()
    await receive(CHARGE_BOOKING, charge_command(booking_id), datetime.now(UTC))
    async with sessions() as holding:
        # A ledger row for the booking that the test never commits: once the provider has
        # charged, the worker's job waits on its own insert of the same row, mid-job.
        holding.add(
            ChargeRecord(
                booking_id=booking_id, amount=1, currency="EUR", status=ChargeStatus.DECLINED
            )
        )
        await holding.flush()
        async with _worker(database_url) as worker:
            await _eventually(lambda: _a_query_waits_for_a_lock(sessions))
            charged_before_the_kill = len(await provider_books())
            worker.kill()
            await worker.wait()
        await holding.rollback()

    async with _worker(database_url):
        await _eventually(lambda: _no_jobs_left(sessions))

    [charge] = await ledger()
    assert charged_before_the_kill == 1
    assert charge.status is ChargeStatus.CHARGED
    assert len(await provider_books()) == 1
    # Nothing listens at the broker's address, so the one reply waits in the outbox.
    assert await published() == [(CHARGE_SUCCEEDED, about_booking(booking_id))]


@asynccontextmanager
async def _worker(database_url: str) -> AsyncIterator[asyncio.subprocess.Process]:
    """A worker in its own process, as in production, killed on the way out if it still runs.
    Its log goes to the test's output."""
    settings = {
        "DATABASE_URL": database_url,
        # Nothing listens there: the command is already stored, so the worker needs no broker.
        "RABBITMQ_URL": "amqp://holdfast:holdfast@127.0.0.1:1/",
        # The provider's trouble is pinned off, whatever the shell exports for trying it out.
        "PROVIDER_DELAY_SECONDS": "0",
        "PROVIDER_FAILURE_RATE": "0",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "holdfast.payments.worker", env=os.environ | settings
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _a_query_waits_for_a_lock(sessions: Sessions) -> bool:
    async with sessions() as session:
        waiting = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND backend_type = 'client backend' AND wait_event_type = 'Lock'"
            )
        )
        return bool(waiting)


async def _no_jobs_left(sessions: Sessions) -> bool:
    async with sessions() as session:
        return (await session.scalars(select(JobRecord.id).limit(1))).first() is None


async def _eventually(condition: Callable[[], Awaitable[bool]]) -> None:
    """Wait for a worker process to make the condition true."""
    async with asyncio.timeout(WAIT_FOR_WORKER_SECONDS):
        while True:
            if await condition():
                return
            await asyncio.sleep(0.1)
