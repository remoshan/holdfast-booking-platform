"""Workers racing for jobs, and a worker process killed in the middle of one."""

import asyncio
import os
import sys
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import Response
from sqlalchemy import select, update

from holdfast.core.mail import Mailer
from holdfast.domain.reminders import REMINDER_LEAD
from holdfast.repositories.records import JobRecord
from holdfast.services.jobs import run_next_job

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Book = Callable[..., Awaitable[Response]]
type Inbox = Callable[[], Awaitable[list[Json]]]

RACING_WORKERS = 4
WAIT_FOR_WORKER_SECONDS = 30


def _clock(minutes_after_opening: int) -> str:
    hours, minutes = divmod(8 * 60 + minutes_after_opening, 60)
    return f"{hours:02}:{minutes:02}"


async def test_racing_workers_run_each_job_exactly_once(
    app: FastAPI, book: Book, free_resource: Json, mailer: Mailer, inbox: Inbox
) -> None:
    # Forty back-to-back 15-minute slots fill the opening hours: forty reminders to send.
    slots = [(_clock(offset), _clock(offset + 15)) for offset in range(0, 10 * 60, 15)]
    booked = [
        (await book(start, end, resource_id=free_resource["id"])).json() for start, end in slots
    ]
    # By then every reminder is due, and no booking has started.
    now = datetime.fromisoformat(booked[-1]["start"]) - REMINDER_LEAD

    async def work() -> int:
        jobs_run = 0
        while await run_next_job(app.state.sessions, mailer, now):
            jobs_run += 1
        return jobs_run

    jobs_run = await asyncio.gather(*(work() for _ in range(RACING_WORKERS)))

    assert sum(jobs_run) == len(booked)
    assert Counter(email["MessageID"] for email in await inbox()) == Counter(
        f"reminder.{booking['id']}@holdfast" for booking in booked
    )


async def test_killing_a_worker_mid_job_loses_nothing_and_duplicates_nothing(
    app: FastAPI,
    database_url: str,
    book: Book,
    free_resource: Json,
    mailer: Mailer,
    inbox: Inbox,
) -> None:
    booking = (await book("09:00", "10:00", resource_id=free_resource["id"])).json()
    await _make_every_job_due(app)
    # A mail server that accepts the connection and never answers holds the worker mid-job.
    mid_job = asyncio.Event()
    stalling_server = await asyncio.start_server(_stall(mid_job), "127.0.0.1", 0)
    host, port = stalling_server.sockets[0].getsockname()
    async with stalling_server, _worker(database_url, mailer, host, port) as worker:
        async with asyncio.timeout(WAIT_FOR_WORKER_SECONDS):
            await mid_job.wait()
        assert await _claimable_jobs(app) == 0  # the worker's transaction holds the job's lock
        worker.kill()
        await worker.wait()
    await _eventually(lambda: _claimable_jobs_equal(app, 1))  # Postgres rolled back and let go

    async with _worker(database_url, mailer, mailer.host, mailer.port):
        await _eventually(lambda: _no_jobs_left(app))

    assert [email["MessageID"] for email in await inbox()] == [f"reminder.{booking['id']}@holdfast"]


def _stall(
    mid_job: asyncio.Event,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        mid_job.set()
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
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "holdfast.worker", env=os.environ | settings
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _make_every_job_due(app: FastAPI) -> None:
    """On the clock the worker reads, which may differ from the database container's."""
    async with app.state.sessions() as session, session.begin():
        await session.execute(update(JobRecord).values(run_at=datetime.now(UTC)))


async def _claimable_jobs(app: FastAPI) -> int:
    """How many jobs a worker could claim now: those no other transaction has locked."""
    async with app.state.sessions() as session, session.begin():
        claimable = await session.scalars(select(JobRecord.id).with_for_update(skip_locked=True))
        return len(claimable.all())


async def _claimable_jobs_equal(app: FastAPI, expected: int) -> bool:
    return await _claimable_jobs(app) == expected


async def _no_jobs_left(app: FastAPI) -> bool:
    async with app.state.sessions() as session:
        return (await session.scalars(select(JobRecord.id).limit(1))).first() is None


async def _eventually(condition: Callable[[], Awaitable[bool]]) -> None:
    """Wait for a worker process to make the condition true."""
    async with asyncio.timeout(WAIT_FOR_WORKER_SECONDS):
        while True:
            if await condition():
                return
            await asyncio.sleep(0.1)
