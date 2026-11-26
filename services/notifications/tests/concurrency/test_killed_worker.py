"""A worker process killed in the middle of sending an email."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.envelope import Envelope
from holdfast.messaging.inbox import receive
from holdfast.messaging.records import JobRecord
from holdfast.notifications.events import BOOKING_CANCELLED
from holdfast.notifications.mail import Mailer

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Inbox = Callable[[], Awaitable[list[Json]]]
type Sessions = async_sessionmaker[AsyncSession]

WAIT_FOR_WORKER_SECONDS = 30


async def test_killing_a_worker_mid_send_loses_nothing_and_duplicates_nothing(
    sessions: Sessions, database_url: str, mailer: Mailer, inbox: Inbox
) -> None:
    booking_id = uuid4()
    await _receive_a_cancellation(sessions, booking_id)
    # A mail server that accepts the connection and never answers holds the worker mid-send.
    mid_send = asyncio.Event()
    stalling_server = await asyncio.start_server(_stall(mid_send), "127.0.0.1", 0)
    host, port = stalling_server.sockets[0].getsockname()
    async with stalling_server, _worker(database_url, mailer, host, port) as worker:
        async with asyncio.timeout(WAIT_FOR_WORKER_SECONDS):
            await mid_send.wait()
        assert await _claimable_jobs(sessions) == 0  # the worker's transaction holds the job's lock
        worker.kill()
        await worker.wait()
    await _eventually(lambda: _claimable_jobs_equal(sessions, 1))  # Postgres rolled back

    async with _worker(database_url, mailer, mailer.host, mailer.port):
        await _eventually(lambda: _no_jobs_left(sessions))

    emails = await inbox()
    assert [email["MessageID"] for email in emails] == [f"cancelled.{booking_id}@holdfast"]
    # The worker read its sender from MAIL_FROM and kept the quotes the name needs.
    assert emails[0]["From"] == {"Name": "Holdfast, Inc.", "Address": "bookings@example.com"}


async def _receive_a_cancellation(sessions: Sessions, booking_id: UUID) -> None:
    """Store the event as the worker's consumer does, so that its job is due at once."""
    now = datetime.now(UTC)
    data = {
        "booking_id": str(booking_id),
        "recipient": "ada@example.com",
        "resource_name": "Room 1",
        "location_name": "Central",
        "address": "Damrak 1, Amsterdam",
        "timezone": "Europe/Amsterdam",
        "start": (now + timedelta(days=1)).isoformat(),
        "end": (now + timedelta(days=1, hours=1)).isoformat(),
    }
    envelope = Envelope(id=uuid4(), type=BOOKING_CANCELLED, time=now, data=data)
    async with sessions() as session, session.begin():
        await receive(session, envelope.model_dump_json().encode(), now)


def _stall(
    mid_send: asyncio.Event,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        mid_send.set()
        with suppress(ConnectionError):
            await reader.read()  # returns once the worker's end of the connection closes
        writer.close()

    return accept


@asynccontextmanager
async def _worker(
    database_url: str, mailer: Mailer, smtp_host: str, smtp_port: int
) -> AsyncIterator[asyncio.subprocess.Process]:
    """A worker in its own process, as in production, killed on the way out if it still runs.
    Its log goes to the test's output."""
    settings = {
        "DATABASE_URL": database_url,
        "SMTP_HOST": smtp_host,
        "SMTP_PORT": str(smtp_port),
        "MAIL_FROM": mailer.sender,
        # Nothing listens there: the job is already stored, so the worker needs no broker.
        "RABBITMQ_URL": "amqp://holdfast:holdfast@127.0.0.1:1/",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "holdfast.notifications.worker", env=os.environ | settings
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _claimable_jobs(sessions: Sessions) -> int:
    """How many jobs a worker could claim now: those no other transaction has locked."""
    async with sessions() as session, session.begin():
        claimable = await session.scalars(select(JobRecord.id).with_for_update(skip_locked=True))
        return len(claimable.all())


async def _claimable_jobs_equal(sessions: Sessions, expected: int) -> bool:
    return await _claimable_jobs(sessions) == expected


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
