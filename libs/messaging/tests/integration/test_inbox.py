"""The inbox against real Postgres and RabbitMQ: every message becomes one job however many copies
arrive, a delivery stored but never acknowledged comes back and still makes one job, a consumer
without its database holds its delivery until it can store it and hands it back if stopped, a
consumer whose queue is deleted declares it again, and an unreadable delivery is kept as a dead job
that can be read again after a fix."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aio_pika
import pytest
from aio_pika import Message
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging import inbox
from holdfast.messaging.envelope import Envelope
from holdfast.messaging.inbox import UNREADABLE, declare_queue, forget_received, receive
from holdfast.messaging.jobs import Handler, Payload, run_next_job
from holdfast.messaging.outbox import declare_exchange
from holdfast.messaging.records import JobRecord, JobStatus, ReceivedMessageRecord
from holdfast.messaging.worker import Messaging, run_worker
from tests.support import FaultyLink, StoredJobs, Worker, eventually, running

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]


async def test_a_message_becomes_one_job_however_many_copies_arrive(
    sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    """A copy is dropped whether the first one's job is still queued or has already run."""
    envelope = _envelope()
    for _ in range(2):
        await _receive(sessions, envelope.model_dump_json().encode())
    [job] = await stored_jobs()

    await run_next_job(sessions, {"test.ping": _do_nothing}, datetime.now(UTC))
    await _receive(sessions, envelope.model_dump_json().encode())

    assert (job.kind, job.payload) == ("test.ping", envelope.model_dump(mode="json"))
    assert await stored_jobs() == []


async def test_received_ids_are_forgotten_after_seven_days(sessions: Sessions) -> None:
    async with sessions() as session, session.begin():
        for days_ago in (8, 6):
            session.add(
                ReceivedMessageRecord(
                    id=uuid4(), received_at=datetime.now(UTC) - timedelta(days=days_ago)
                )
            )

    async with sessions() as session:
        await forget_received(session)

    async with sessions() as session:
        kept = await session.scalars(select(ReceivedMessageRecord.received_at))
        assert [(datetime.now(UTC) - received_at).days for received_at in kept] == [6]


async def test_a_delivery_stored_but_not_acknowledged_comes_back_and_makes_one_job(
    database_url: str,
    rabbitmq_url: str,
    rabbitmq_url_through: str,
    rabbitmq_link: FaultyLink,
    queue: str,
    sessions: Sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link to RabbitMQ drops after the store commits and before the acknowledgement: RabbitMQ
    delivers the message again, and its id, already stored, drops the copy."""
    handled: list[Payload] = []
    store = inbox._store
    dropped = False

    async def store_then_drop_the_link(*args: Any) -> bool:
        nonlocal dropped
        stored = await store(*args)
        if not dropped:
            dropped = True
            rabbitmq_link.cut()
            asyncio.get_running_loop().call_later(0.5, rabbitmq_link.restore)
        return stored

    monkeypatch.setattr(inbox, "_store", store_then_drop_the_link)
    await _bind(rabbitmq_url, queue)
    envelope = _envelope()

    async with running(_consumer(database_url, rabbitmq_url_through, queue, handled)):
        await _deliver(rabbitmq_url, envelope.model_dump_json().encode())
        await eventually(lambda: _was_handled(handled))
        await eventually(lambda: _no_messages_left(rabbitmq_url, queue))
        await asyncio.sleep(0.5)  # Time enough to store a copy, if one slipped through.

    assert dropped
    assert [payload["id"] for payload in handled] == [str(envelope.id)]
    assert await _count(sessions, ReceivedMessageRecord) == 1


async def test_without_its_database_a_consumer_holds_its_delivery_until_it_can_store_it(
    database_url_through: str,
    database_link: FaultyLink,
    rabbitmq_url: str,
    queue: str,
    stored_jobs: StoredJobs,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The delivery stays with the consumer, unacknowledged, so nothing is lost or counted while
    the database is away, and it is stored the moment the database is back. The consumer keeps its
    connection to RabbitMQ throughout: it retries the store in place."""
    handled: list[Payload] = []
    await _bind(rabbitmq_url, queue)

    async with running(_consumer(database_url_through, rabbitmq_url, queue, handled)):
        database_link.cut()
        await _deliver(rabbitmq_url, _envelope().model_dump_json().encode())
        await eventually(lambda: _no_messages_left(rabbitmq_url, queue))
        await asyncio.sleep(0.5)  # The consumer keeps failing to store it meanwhile.
        jobs_while_away = await stored_jobs()
        database_link.restore()
        await eventually(lambda: _was_handled(handled))

    assert jobs_while_away == []
    assert len(handled) == 1
    assert await _no_messages_left(rabbitmq_url, queue)
    assert not [record for record in caplog.records if "lost RabbitMQ" in record.getMessage()]


async def test_a_worker_stopped_while_it_cannot_store_a_delivery_hands_it_back(
    database_url_through: str,
    database_link: FaultyLink,
    rabbitmq_url: str,
    queue: str,
    stored_jobs: StoredJobs,
) -> None:
    """It must neither acknowledge a delivery it could not store, which would lose it, nor wait
    for the database before it stops."""
    await _bind(rabbitmq_url, queue)

    async with running(_consumer(database_url_through, rabbitmq_url, queue, [])):
        database_link.cut()
        await _deliver(rabbitmq_url, _envelope().model_dump_json().encode())
        await eventually(lambda: _no_messages_left(rabbitmq_url, queue))
        await asyncio.sleep(0.5)  # The consumer keeps failing to store it meanwhile.

    assert await _ready(rabbitmq_url, queue) == 1
    assert await stored_jobs() == []


async def test_a_consumer_whose_queue_is_deleted_declares_it_again(
    database_url: str, rabbitmq_url: str, queue: str
) -> None:
    """Deleting a queue, from the management UI for example, makes RabbitMQ cancel its consumer and
    keep the connection open. The consumer must start again and declare the queue, or every message
    of its types would find no queue until the worker restarted."""
    handled: list[Payload] = []
    first, second = _envelope(), _envelope()
    await _bind(rabbitmq_url, queue)

    async with running(_consumer(database_url, rabbitmq_url, queue, handled)):
        await _deliver(rabbitmq_url, first.model_dump_json().encode())
        await eventually(lambda: _was_handled(handled))
        async with await aio_pika.connect(rabbitmq_url) as connection:
            await (await connection.channel()).queue_delete(queue)
        await eventually(lambda: _is_consumed(rabbitmq_url, queue))
        await _deliver(rabbitmq_url, second.model_dump_json().encode())
        await eventually(lambda: _handled_twice(handled))

    assert [payload["id"] for payload in handled] == [str(first.id), str(second.id)]


async def test_an_envelope_too_deep_to_store_is_buried_and_the_next_message_is_received(
    database_url: str, rabbitmq_url: str, queue: str, stored_jobs: StoredJobs
) -> None:
    """Data nested deeper than the envelope allows could be read, but not stored again. Buried at
    once, it cannot hold up the deliveries behind it."""
    handled: list[Payload] = []
    deep = (
        f'{{"id": "{uuid4()}", "type": "test.ping", "time": "{datetime.now(UTC).isoformat()}",'
        f' "data": {{"rooms": {"[" * 150}{"]" * 150}}}}}'
    )
    await _bind(rabbitmq_url, queue)

    async with running(_consumer(database_url, rabbitmq_url, queue, handled)):
        await _deliver(rabbitmq_url, deep.encode())
        await _deliver(rabbitmq_url, _envelope().model_dump_json().encode())
        await eventually(lambda: _was_handled(handled))

    [buried] = await stored_jobs()
    assert (buried.kind, buried.status) == (UNREADABLE, JobStatus.DEAD)
    assert len(handled) == 1


async def test_an_unreadable_delivery_is_kept_as_a_dead_job_and_not_delivered_again(
    database_url: str, rabbitmq_url: str, queue: str, stored_jobs: StoredJobs
) -> None:
    handled: list[Payload] = []
    await _bind(rabbitmq_url, queue)

    async with running(_consumer(database_url, rabbitmq_url, queue, handled)):
        await _deliver(rabbitmq_url, b"not an envelope")
        await eventually(lambda: _has_jobs(stored_jobs))

    [buried] = await stored_jobs()
    assert (buried.kind, buried.status, buried.payload) == (
        UNREADABLE,
        JobStatus.DEAD,
        {"body": "not an envelope"},
    )
    assert (buried.last_error or "").startswith("UnreadableMessageError: ")
    assert handled == []
    assert await _no_messages_left(rabbitmq_url, queue)


async def test_a_buried_delivery_that_can_be_read_after_a_fix_is_received_once_requeued(
    database_url: str, rabbitmq_url: str, queue: str, sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    """The body here is an envelope, as it would be once a fix lets the service read it. The
    worker's own handlers run the replayed job."""
    envelope = _envelope()
    async with sessions() as session, session.begin():
        session.add(
            JobRecord(
                kind=UNREADABLE,
                payload={"body": envelope.model_dump_json()},
                status=JobStatus.DEAD,
                attempts=1,
                run_at=datetime.now(UTC),
                last_error="UnreadableMessageError: before the fix",
            )
        )
        # The replay statement from the README.
        await session.execute(
            text("UPDATE jobs SET status = 'queued', attempts = 0, run_at = now()")
        )
    handled: list[Payload] = []

    async with running(_consumer(database_url, rabbitmq_url, queue, handled)):
        await eventually(lambda: _was_handled(handled))
        await eventually(lambda: _queue_is_empty(stored_jobs))

    assert [payload["id"] for payload in handled] == [str(envelope.id)]


def _envelope() -> Envelope:
    return Envelope(id=uuid4(), type="test.ping", time=datetime.now(UTC), data={"number": 1})


async def _receive(sessions: Sessions, body: bytes) -> None:
    async with sessions() as session, session.begin():
        await receive(session, body, datetime.now(UTC))


async def _do_nothing(*_: object) -> None:
    pass


def _recorder(handled: list[Payload]) -> Handler:
    async def record(session: AsyncSession, payload: Payload, now: datetime) -> None:
        handled.append(payload)

    return record


def _consumer(database_url: str, rabbitmq_url: str, queue: str, handled: list[Payload]) -> Worker:
    """A worker that receives test.ping messages and records each one it handles."""
    messaging = Messaging(rabbitmq_url, queue, {"test.ping": _recorder(handled)}, publishes=False)

    async def run(stopping: asyncio.Event) -> None:
        await run_worker(
            database_url=database_url,
            jobs={},
            longest_job=timedelta(0),
            daily=[],
            messaging=messaging,
            stopping=stopping,
        )

    return run


async def _bind(rabbitmq_url: str, queue: str) -> None:
    """Declare the test's queue up front, so a message sent before its consumer is up waits."""
    async with await aio_pika.connect(rabbitmq_url) as connection:
        await declare_queue(await connection.channel(), queue, ["test.ping"])


async def _deliver(rabbitmq_url: str, body: bytes) -> None:
    async with await aio_pika.connect(rabbitmq_url) as connection:
        exchange = await declare_exchange(await connection.channel())
        await exchange.publish(Message(body), routing_key="test.ping")


async def _no_messages_left(rabbitmq_url: str, queue: str) -> bool:
    """Whether the queue holds nothing ready for a consumer. A delivery that a consumer holds,
    unacknowledged, is not ready; one it gave back, or that a closed connection returned, is."""
    async with await aio_pika.connect(rabbitmq_url) as connection:
        declared = await (await connection.channel()).declare_queue(queue, passive=True)
        result = declared.declaration_result
        return result is not None and result.message_count == 0


async def _ready(rabbitmq_url: str, queue: str) -> int:
    async with await aio_pika.connect(rabbitmq_url) as connection:
        declared = await (await connection.channel()).declare_queue(queue, passive=True)
        result = declared.declaration_result
        return 0 if result is None else result.message_count or 0


async def _is_consumed(rabbitmq_url: str, queue: str) -> bool:
    """Whether the queue exists and a consumer has subscribed to it, which it does once bound."""
    async with await aio_pika.connect(rabbitmq_url) as connection:
        try:
            declared = await (await connection.channel()).declare_queue(queue, passive=True)
        except aio_pika.exceptions.ChannelNotFoundEntity:
            return False
        result = declared.declaration_result
        return result is not None and bool(result.consumer_count)


async def _was_handled(handled: list[Payload]) -> bool:
    return bool(handled)


async def _handled_twice(handled: list[Payload]) -> bool:
    return len(handled) == 2


async def _queue_is_empty(stored_jobs: StoredJobs) -> bool:
    return not await stored_jobs()


async def _has_jobs(stored_jobs: StoredJobs) -> bool:
    return bool(await stored_jobs())


async def _count(sessions: Sessions, record: type[ReceivedMessageRecord]) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(record)) or 0
