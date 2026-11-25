"""The worker's loops against real Postgres and RabbitMQ: jobs and daily chores that need no broker,
jobs that keep running while the broker is unreachable and messages that wait for it, connections
that go silent or hang, a stop that lets the job in hand finish, a loop that carries on after an
error, and sessions that Postgres ends if they are left idle."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging import inbox, worker
from holdfast.messaging.jobs import Handler, Payload, enqueue, run_next_job
from holdfast.messaging.outbox import publish
from holdfast.messaging.records import ReceivedMessageRecord
from holdfast.messaging.worker import (
    DailyTask,
    Messaging,
    run_worker,
)
from tests.support import WAIT_SECONDS, Count, FaultyLink, StoredJobs, Worker, eventually, running

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]


class Ping(BaseModel):
    number: int


async def test_a_worker_without_messaging_runs_its_jobs_and_daily_chores(
    database_url: str, sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    """It needs no broker at all, and does its chores as soon as it starts."""
    ran: list[str] = []

    async def chore(session: AsyncSession) -> None:
        ran.append("chore")

    await _queue(sessions, "local")

    async with running(_worker(database_url, {"local": _recorder(ran)}, daily=[chore])):
        await eventually(lambda: _both_ran(ran))

    assert sorted(ran) == ["chore", "local"]
    assert await stored_jobs() == []


async def test_jobs_run_and_messages_wait_while_rabbitmq_is_unreachable(
    database_url: str,
    rabbitmq_url_through: str,
    rabbitmq_link: FaultyLink,
    queue: str,
    sessions: Sessions,
    outbox_rows: Count,
) -> None:
    """Holds keep expiring when the broker is down. Its messages wait in the outbox, and go out,
    and come in, once the broker is back: the queue they need is declared on reconnecting."""
    ran: list[str] = []
    handled: list[Payload] = []
    messaging = Messaging(
        rabbitmq_url_through, queue, {"test.ping": _envelope_recorder(handled)}, publishes=True
    )
    rabbitmq_link.cut()
    await _queue(sessions, "local")
    async with sessions() as session, session.begin():
        await publish(session, "test.ping", Ping(number=1))

    async with running(_worker(database_url, {"local": _recorder(ran)}, messaging=messaging)):
        await eventually(lambda: _ran(ran))
        await asyncio.sleep(0.5)  # The relay keeps failing to connect meanwhile.
        waiting = await outbox_rows()
        rabbitmq_link.restore()
        await eventually(lambda: _ran(handled))

    assert ran == ["local"]
    assert waiting == 1
    assert [payload["data"] for payload in handled] == [{"number": 1}]
    assert await outbox_rows() == 0


async def test_a_worker_told_to_stop_finishes_the_job_in_hand(
    database_url: str, sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    """As `docker compose stop` asks it to: the job commits, and then the worker exits."""
    stopping = asyncio.Event()

    async def stop_halfway(*_: Any) -> None:
        stopping.set()

    await _queue(sessions, "local")

    async with asyncio.timeout(WAIT_SECONDS):
        await _worker(database_url, {"local": stop_halfway})(stopping)

    assert await stored_jobs() == []


async def test_the_worker_carries_on_after_an_error(
    database_url: str, sessions: Sessions, stored_jobs: StoredJobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most likely the database was unreachable for a moment; the jobs waited in their table."""
    calls = 0

    async def fail_the_first_time(*args: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("The database is unreachable.")
        return await run_next_job(*args)

    monkeypatch.setattr(worker, "run_next_job", fail_the_first_time)
    await _queue(sessions, "local")

    async with running(_worker(database_url, {"local": _recorder([])})):
        await eventually(lambda: _queue_is_empty(stored_jobs))

    assert calls >= 2


@pytest.mark.parametrize(
    ("longest_job", "limit_ms"),
    [
        pytest.param(timedelta(0), 50_000, id="never less than five publish timeouts"),
        pytest.param(timedelta(seconds=60), 300_000, id="five of its longest jobs"),
    ],
)
async def test_the_worker_runs_jobs_in_sessions_that_postgres_ends_if_they_are_left_idle(
    database_url: str,
    sessions: Sessions,
    stored_jobs: StoredJobs,
    longest_job: timedelta,
    limit_ms: int,
) -> None:
    """So a job, or a page of the outbox, whose worker's machine vanished without closing its
    connection is freed. A limit of zero would turn the timeout off."""
    limits: list[int] = []

    async def record_the_limit(session: AsyncSession, *_: Any) -> None:
        limits.append(
            await session.scalar(
                text(
                    "SELECT setting::int FROM pg_settings"
                    " WHERE name = 'idle_in_transaction_session_timeout'"
                )
            )
        )

    await _queue(sessions, "local")

    async with running(_worker(database_url, {"local": record_the_limit}, longest_job=longest_job)):
        await eventually(lambda: _queue_is_empty(stored_jobs))

    assert limits == [limit_ms]


async def test_a_worker_reconnects_once_a_connection_that_went_silent_is_given_up_on(
    database_url: str,
    rabbitmq_url_through: str,
    rabbitmq_link: FaultyLink,
    queue: str,
    sessions: Sessions,
    outbox_rows: Count,
) -> None:
    """aiormq gives up on a connection that sent nothing for three heartbeats, nine seconds at the
    link's two, and fails every call waiting on it with CancelledError. The relay and the consumer
    must take that as a lost connection and connect again, not end for good."""
    handled: list[Payload] = []
    messaging = Messaging(
        rabbitmq_url_through, queue, {"test.ping": _envelope_recorder(handled)}, publishes=True
    )

    async with running(_worker(database_url, {}, messaging=messaging)):
        async with sessions() as session, session.begin():
            await publish(session, "test.ping", Ping(number=1))
        await eventually(lambda: _ran(handled))
        rabbitmq_link.stall()
        async with sessions() as session, session.begin():
            await publish(session, "test.ping", Ping(number=2))
        await asyncio.sleep(10)  # Long enough for aiormq to give up, with a confirm waiting.
        rabbitmq_link.restore()
        await eventually(lambda: _ran_twice(handled))

    assert [payload["data"] for payload in handled] == [{"number": 1}, {"number": 2}]
    assert await outbox_rows() == 0


async def test_an_operation_aiormq_gave_up_on_does_not_end_the_consumer(
    database_url: str,
    rabbitmq_url: str,
    queue: str,
    sessions: Sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiormq fails a call waiting on a connection it gave up on with CancelledError, which is not
    a cancellation of the consumer's task. Here the first declaration of the queue fails that way:
    the consumer must connect again and declare it, so that the message waiting in the outbox finds
    its queue."""
    declare_queue = inbox.declare_queue
    given_up = False

    async def give_up_the_first_time(*args: Any) -> Any:
        nonlocal given_up
        if not given_up:
            given_up = True
            raise asyncio.CancelledError
        return await declare_queue(*args)

    monkeypatch.setattr(inbox, "declare_queue", give_up_the_first_time)
    handled: list[Payload] = []
    messaging = Messaging(
        rabbitmq_url, queue, {"test.ping": _envelope_recorder(handled)}, publishes=True
    )
    async with sessions() as session, session.begin():
        await publish(session, "test.ping", Ping(number=1))

    async with running(_worker(database_url, {}, messaging=messaging)):
        await eventually(lambda: _ran(handled))

    assert given_up


async def test_a_worker_whose_task_is_cancelled_ends_instead_of_connecting_again(
    database_url: str, rabbitmq_url: str, queue: str
) -> None:
    """As on Windows, where Ctrl+C cancels the worker. A CancelledError meant for the task must
    still end it."""
    messaging = Messaging(rabbitmq_url, queue, {"test.ping": _recorder([])}, publishes=True)
    stopping = asyncio.Event()
    running_worker = asyncio.create_task(_worker(database_url, {}, messaging=messaging)(stopping))
    await asyncio.sleep(0.5)

    running_worker.cancel()
    finished, _ = await asyncio.wait({running_worker}, timeout=WAIT_SECONDS)
    stopping.set()  # Lets a worker that swallowed the cancellation stop, so the test ends.
    with suppress(asyncio.CancelledError):
        await running_worker

    assert finished == {running_worker}
    assert running_worker.cancelled()


async def test_a_connection_attempt_that_hangs_gives_up_after_its_timeout(
    database_url: str,
    rabbitmq_url_through: str,
    rabbitmq_link: FaultyLink,
    queue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker that accepts the connection and then says nothing must not hold the relay for
    ever: each attempt times out, and the next one starts."""
    monkeypatch.setattr(worker, "CONNECT_TIMEOUT", timedelta(seconds=0.2))
    rabbitmq_link.stall()
    messaging = Messaging(rabbitmq_url_through, queue, {}, publishes=True)

    async with running(_worker(database_url, {}, messaging=messaging)):
        await eventually(lambda: _accepted_three_connections(rabbitmq_link))


async def test_a_messaging_worker_forgets_old_received_ids_when_it_starts(
    database_url: str, rabbitmq_url: str, queue: str, sessions: Sessions
) -> None:
    async with sessions() as session, session.begin():
        for days_ago in (8, 6):
            session.add(
                ReceivedMessageRecord(
                    id=uuid4(), received_at=datetime.now(UTC) - timedelta(days=days_ago)
                )
            )
    messaging = Messaging(rabbitmq_url, queue, {"test.ping": _recorder([])}, publishes=False)

    async with running(_worker(database_url, {}, messaging=messaging)):
        await eventually(lambda: _received_ids_kept(sessions, 1))


async def test_a_job_kind_that_is_also_a_message_type_is_refused(
    database_url: str, rabbitmq_url: str, queue: str
) -> None:
    messaging = Messaging(rabbitmq_url, queue, {"test.ping": _recorder([])}, publishes=False)

    with pytest.raises(ValueError, match=r"test\.ping"):
        await _worker(database_url, {"test.ping": _recorder([])}, messaging=messaging)(
            asyncio.Event()
        )


def _worker(
    database_url: str,
    jobs: dict[str, Handler],
    *,
    daily: list[DailyTask] | None = None,
    messaging: Messaging | None = None,
    longest_job: timedelta = timedelta(seconds=60),
) -> Worker:
    async def run(stopping: asyncio.Event) -> None:
        await run_worker(
            database_url=database_url,
            jobs=jobs,
            longest_job=longest_job,
            daily=daily or [],
            messaging=messaging,
            stopping=stopping,
        )

    return run


async def _queue(sessions: Sessions, kind: str) -> None:
    async with sessions() as session, session.begin():
        await enqueue(session, kind, {}, datetime.now(UTC))


def _recorder(ran: list[str]) -> Handler:
    async def record(session: AsyncSession, payload: Payload, now: datetime) -> None:
        ran.append("local")

    return record


def _envelope_recorder(handled: list[Payload]) -> Handler:
    async def record(session: AsyncSession, payload: Payload, now: datetime) -> None:
        handled.append(payload)

    return record


async def _ran(ran: list[Any]) -> bool:
    return bool(ran)


async def _both_ran(ran: list[str]) -> bool:
    return len(ran) == 2


async def _ran_twice(handled: list[Payload]) -> bool:
    return len(handled) == 2


async def _accepted_three_connections(link: FaultyLink) -> bool:
    return link.connections >= 3


async def _received_ids_kept(sessions: Sessions, expected: int) -> bool:
    async with sessions() as session:
        kept = await session.scalar(select(func.count()).select_from(ReceivedMessageRecord))
        return kept == expected


async def _queue_is_empty(stored_jobs: StoredJobs) -> bool:
    return not await stored_jobs()
