"""Several instances of a service's worker side by side: they share its outbox, its queue and its
job table, and still publish each message once, and receive and handle each one exactly once."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta

import aio_pika
import pytest
from aio_pika import Message
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.envelope import Envelope
from holdfast.messaging.inbox import declare_queue
from holdfast.messaging.jobs import Payload
from holdfast.messaging.outbox import declare_exchange, publish
from holdfast.messaging.records import OutboxRecord
from holdfast.messaging.worker import Messaging, run_worker
from tests.support import Count, StoredJobs, Worker, eventually, running

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]

WORKERS = 3
MESSAGES = 200


class Ping(BaseModel):
    number: int


async def test_workers_side_by_side_send_receive_and_handle_each_message_exactly_once(
    database_url: str,
    rabbitmq_url: str,
    queue: str,
    sessions: Sessions,
    stored_jobs: StoredJobs,
    outbox_rows: Count,
) -> None:
    """Every message also arrives a second time, sent by hand with the same id, as a relay that
    lost its commit would send it."""
    handled: Counter[str] = Counter()

    async def handle(session: AsyncSession, payload: Payload, now: datetime) -> None:
        handled[payload["id"]] += 1

    async with sessions() as session, session.begin():
        for number in range(MESSAGES):
            await publish(session, "test.ping", Ping(number=number))
    copies = await _envelopes_in_the_outbox(sessions)
    async with await aio_pika.connect(rabbitmq_url) as connection:
        channel = await connection.channel()
        await declare_queue(channel, queue, ["test.ping"])
        exchange = await declare_exchange(channel)
        for copy in copies:
            await exchange.publish(Message(copy.model_dump_json().encode()), routing_key=copy.type)
    messaging = Messaging(rabbitmq_url, queue, {"test.ping": handle}, publishes=True)

    async with running(*(_worker(database_url, messaging) for _ in range(WORKERS))):
        await eventually(lambda: _all_handled(handled))
        await eventually(lambda: _queue_is_empty(stored_jobs))

    assert handled == Counter({str(copy.id): 1 for copy in copies})
    assert await outbox_rows() == 0


async def test_relays_side_by_side_publish_each_message_exactly_once(
    database_url: str, rabbitmq_url: str, queue: str, sessions: Sessions, outbox_rows: Count
) -> None:
    """Counted at the queue, before any receiver could drop a copy: SKIP LOCKED gives each page of
    the outbox to one relay."""
    async with sessions() as session, session.begin():
        for number in range(MESSAGES):
            await publish(session, "test.ping", Ping(number=number))
    async with await aio_pika.connect(rabbitmq_url) as connection:
        await declare_queue(await connection.channel(), queue, ["test.ping"])
    relay_only = Messaging(rabbitmq_url, queue, {}, publishes=True)

    async with running(*(_worker(database_url, relay_only) for _ in range(WORKERS))):
        await eventually(lambda: _outbox_is_empty(outbox_rows))

    async with await aio_pika.connect(rabbitmq_url) as connection:
        declared = await (await connection.channel()).declare_queue(queue, passive=True)
    assert declared.declaration_result is not None
    assert declared.declaration_result.message_count == MESSAGES


def _worker(database_url: str, messaging: Messaging) -> Worker:
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


async def _envelopes_in_the_outbox(sessions: Sessions) -> list[Envelope]:
    async with sessions() as session:
        return [
            Envelope(id=row.id, type=row.type, time=row.created_at, data=row.data)
            for row in await session.scalars(select(OutboxRecord))
        ]


async def _outbox_is_empty(outbox_rows: Count) -> bool:
    return await outbox_rows() == 0


async def _all_handled(handled: Counter[str]) -> bool:
    return len(handled) == MESSAGES


async def _queue_is_empty(stored_jobs: StoredJobs) -> bool:
    return not await stored_jobs()
