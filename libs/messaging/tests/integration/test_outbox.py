"""The outbox and its relay against real Postgres and RabbitMQ: a message exists only if its
transaction commits, it leaves in its envelope once RabbitMQ has it, a relay that loses its commit
publishes it again, a message no queue takes waits without holding back the rest, and a broker that
stops answering cannot hold a page."""

import asyncio
from datetime import UTC, datetime, timedelta

import aio_pika
import pytest
from aio_pika import DeliveryMode
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging import outbox
from holdfast.messaging.envelope import Envelope
from holdfast.messaging.inbox import declare_queue, receive
from holdfast.messaging.outbox import declare_exchange, publish, relay
from holdfast.messaging.records import OutboxRecord, ReceivedMessageRecord
from tests.support import WAIT_SECONDS, Count, FaultyLink, StoredJobs

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]


class Ping(BaseModel):
    number: int


async def test_a_message_is_sent_only_if_its_transaction_commits(
    sessions: Sessions, rabbitmq_url: str, queue: str, outbox_rows: Count
) -> None:
    async with sessions() as session:
        await session.begin()
        await publish(session, "test.ping", Ping(number=1))
        await session.rollback()
    async with sessions() as session, session.begin():
        await publish(session, "test.ping", Ping(number=2))
    await _bind(rabbitmq_url, queue, "test.ping")

    published = await _relay(sessions, rabbitmq_url)

    [message] = await _take_all(rabbitmq_url, queue)
    envelope = Envelope.model_validate_json(message.body)
    assert published == 1
    assert (envelope.type, envelope.data) == ("test.ping", {"number": 2})
    assert (message.message_id, message.type) == (str(envelope.id), "test.ping")
    assert message.content_type == "application/json"
    assert message.delivery_mode == DeliveryMode.PERSISTENT
    assert await outbox_rows() == 0


async def test_a_message_published_again_after_its_relay_lost_its_commit_is_received_once(
    sessions: Sessions,
    rabbitmq_url: str,
    queue: str,
    stored_jobs: StoredJobs,
    outbox_rows: Count,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RabbitMQ confirmed the message, but the relay died before deleting its row: the row stays,
    the next pass publishes it again, and the receiver drops the copy."""
    real_publish = outbox._publish
    commit_lost = False

    async def publish_then_lose_the_commit(exchange: AbstractExchange, row: OutboxRecord) -> None:
        nonlocal commit_lost
        await real_publish(exchange, row)
        if not commit_lost:
            commit_lost = True
            raise ConnectionError("The relay lost its database before it could commit.")

    monkeypatch.setattr(outbox, "_publish", publish_then_lose_the_commit)
    async with sessions() as session, session.begin():
        await publish(session, "test.ping", Ping(number=1))
    await _bind(rabbitmq_url, queue, "test.ping")

    with pytest.raises(ConnectionError):
        await _relay(sessions, rabbitmq_url)
    rows_after_the_lost_commit = await outbox_rows()
    await _relay(sessions, rabbitmq_url)
    copies = await _take_all(rabbitmq_url, queue)
    for copy in copies:
        async with sessions() as session, session.begin():
            await receive(session, copy.body, datetime.now(UTC))

    assert rows_after_the_lost_commit == 1
    assert len(copies) == 2
    assert len({copy.message_id for copy in copies}) == 1
    assert [job.payload["data"] for job in await stored_jobs()] == [{"number": 1}]
    assert await _count(sessions, ReceivedMessageRecord) == 1
    assert await outbox_rows() == 0


async def test_a_confirm_that_aiormq_gave_up_on_rolls_the_page_back_as_a_lost_connection(
    sessions: Sessions,
    rabbitmq_url: str,
    outbox_rows: Count,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiormq fails a publish waiting for its confirm with CancelledError when it gives up on the
    connection. The relay reports that as the lost connection it is, so its caller connects
    again."""

    async def give_up(exchange: AbstractExchange, row: OutboxRecord) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(outbox, "_publish", give_up)
    async with sessions() as session, session.begin():
        await publish(session, "test.ping", Ping(number=1))

    with pytest.raises(ConnectionError):
        await _relay(sessions, rabbitmq_url)

    assert await outbox_rows() == 1


async def test_messages_no_queue_takes_wait_without_holding_back_the_rest(
    sessions: Sessions,
    rabbitmq_url: str,
    queue: str,
    outbox_rows: Count,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three messages of a type nothing is bound to fill more than a page ahead of one that is
    routed. The relay pages past them, and they go out once a queue takes their type."""
    monkeypatch.setattr(outbox, "PAGE_SIZE", 2)
    async with sessions() as session, session.begin():
        for number in range(3):
            await publish(session, "test.unbound", Ping(number=number))
        await publish(session, "test.ping", Ping(number=3))
    await _bind(rabbitmq_url, queue, "test.ping")

    first_pass = await _relay(sessions, rabbitmq_url)
    routed_at_once = await _take_all(rabbitmq_url, queue)
    waiting = await outbox_rows()
    await _bind(rabbitmq_url, queue, "test.unbound")
    second_pass = await _relay(sessions, rabbitmq_url)
    routed_later = await _take_all(rabbitmq_url, queue)

    assert (first_pass, waiting, second_pass) == (1, 3, 3)
    assert _numbers(routed_at_once) == [3]
    assert sorted(_numbers(routed_later)) == [0, 1, 2]
    assert await outbox_rows() == 0


async def test_a_page_sent_to_a_broker_that_stopped_answering_rolls_back_within_its_timeout(
    sessions: Sessions,
    rabbitmq_url: str,
    rabbitmq_url_through: str,
    rabbitmq_link: FaultyLink,
    queue: str,
    outbox_rows: Count,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a timeout, the page would wait for the confirms, and hold its rows' locks and an
    open transaction, for as long as the broker stayed silent."""
    monkeypatch.setattr(outbox, "PUBLISH_TIMEOUT", timedelta(seconds=0.5))
    async with sessions() as session, session.begin():
        await publish(session, "test.ping", Ping(number=1))
    await _bind(rabbitmq_url, queue, "test.ping")

    async with await aio_pika.connect(rabbitmq_url_through) as connection:
        exchange = await declare_exchange(await connection.channel(on_return_raises=True))
        rabbitmq_link.stall()
        async with asyncio.timeout(WAIT_SECONDS):
            with pytest.raises(TimeoutError):
                await relay(sessions, exchange, asyncio.Event())
        rows_while_silent = await outbox_rows()
        open_transactions = await _idle_in_transaction(sessions)
        rabbitmq_link.restore()
    published_later = await _relay(sessions, rabbitmq_url)

    assert (rows_while_silent, open_transactions) == (1, 0)
    assert published_later == 1
    assert _numbers(await _take_all(rabbitmq_url, queue))[-1:] == [1]
    assert await outbox_rows() == 0


async def _relay(sessions: Sessions, rabbitmq_url: str) -> int:
    """One pass of the relay, on a connection of its own."""
    async with await aio_pika.connect(rabbitmq_url) as connection:
        exchange = await declare_exchange(await connection.channel(on_return_raises=True))
        return await relay(sessions, exchange, asyncio.Event())


async def _bind(rabbitmq_url: str, queue: str, *message_types: str) -> None:
    async with await aio_pika.connect(rabbitmq_url) as connection:
        await declare_queue(await connection.channel(), queue, message_types)


async def _take_all(rabbitmq_url: str, queue: str) -> list[AbstractIncomingMessage]:
    messages = []
    async with await aio_pika.connect(rabbitmq_url) as connection:
        declared = await (await connection.channel()).get_queue(queue)
        while (message := await declared.get(no_ack=True, fail=False)) is not None:
            messages.append(message)
    return messages


def _numbers(messages: list[AbstractIncomingMessage]) -> list[int]:
    return [Envelope.model_validate_json(message.body).data["number"] for message in messages]


async def _count(sessions: Sessions, record: type[ReceivedMessageRecord]) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(record)) or 0


async def _idle_in_transaction(sessions: Sessions) -> int:
    async with sessions() as session:
        return (
            await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND state = 'idle in transaction'"
                )
            )
            or 0
        )
