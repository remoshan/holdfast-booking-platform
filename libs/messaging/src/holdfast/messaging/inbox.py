"""Receiving messages. Each delivery is stored as a job of the receiving service, once per message
id, before RabbitMQ is told it arrived; from then on the job queue runs it, retries it and keeps it
when it dies, exactly as it does the service's own jobs.

A copy of a message, which RabbitMQ sends when a consumer stopped before acknowledging one, or a
relay sends when it stopped before deleting one it had published, finds the id it left behind and
is dropped. Ids are kept for RETENTION, far longer than any of those copies can take to arrive.
"""

import asyncio
import logging
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractQueue, AbstractQueueIterator
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging import jobs
from holdfast.messaging.envelope import Envelope
from holdfast.messaging.outbox import declare_exchange
from holdfast.messaging.records import JobRecord, JobStatus, ReceivedMessageRecord

UNREADABLE = "unreadable"
"""The kind of the dead job that keeps a delivery which is not a storable envelope."""
RETENTION = timedelta(days=7)
PREFETCH_COUNT = 1
"""Deliveries RabbitMQ sends a consumer ahead. One keeps stopping simple and shares work evenly
between instances."""
STORE_RETRY_INTERVAL = timedelta(seconds=1)

logger = logging.getLogger(__name__)


class UnreadableMessageError(Exception):
    """A delivery that is not an envelope the service can store."""


async def receive(session: AsyncSession, body: bytes, now: datetime) -> None:
    """Queue the message in a delivery's body as a job due now, unless a copy of it already
    arrived. Raises UnreadableMessageError, before writing anything, if the body is not an
    envelope the service can store."""
    try:
        envelope = Envelope.model_validate_json(body)
        payload = envelope.model_dump(mode="json")
    # Both validation and serialisation errors are ValueErrors.
    except ValueError as error:
        raise UnreadableMessageError(str(error)) from error
    first_copy = await session.scalar(
        insert(ReceivedMessageRecord)
        .values(id=envelope.id)
        .on_conflict_do_nothing()
        .returning(ReceivedMessageRecord.id)
    )
    if first_copy is not None:
        await jobs.enqueue(session, envelope.type, payload, now)


async def bury(
    session: AsyncSession, body: bytes, error: UnreadableMessageError, now: datetime
) -> None:
    """Keep an unreadable delivery as a dead job, with its body and why it could not be read, so
    it is neither lost nor retried for ever. Requeuing the job reads the body again."""
    session.add(
        JobRecord(
            kind=UNREADABLE,
            payload={"body": _storable_text(body.decode(errors="replace"))},
            status=JobStatus.DEAD,
            attempts=1,
            run_at=now,
            last_error=_storable_text(f"{type(error).__name__}: {error}"),
        )
    )
    await session.flush()


async def receive_again(session: AsyncSession, payload: jobs.Payload, now: datetime) -> None:
    """Run the receive step again for a buried delivery that someone requeued, as after a fix.
    If it still cannot be read, the job fails like any other."""
    await receive(session, payload["body"].encode(), now)


async def declare_queue(
    channel: AbstractChannel, queue: str, message_types: Collection[str]
) -> AbstractQueue:
    """Declare the service's queue and bind it to the types it handles, which its consumer does on
    each connection. Once declared, the queue keeps the service's messages while it is down."""
    exchange = await declare_exchange(channel)
    # Stated, so that a broker whose default queue type is quorum cannot make it one: a quorum
    # queue's delivery limit and acknowledgement timeout would drop or cancel deliveries behind
    # the job queue's back.
    declared = await channel.declare_queue(
        queue, durable=True, arguments={"x-queue-type": "classic"}
    )
    for message_type in message_types:
        await declared.bind(exchange, routing_key=message_type)
    return declared


async def consume(
    connection: AbstractConnection,
    sessions: async_sessionmaker[AsyncSession],
    queue: str,
    message_types: Collection[str],
    stopping: asyncio.Event,
) -> None:
    """Store each delivery from the service's queue, then acknowledge it, until told to stop, or
    until the connection closes or RabbitMQ cancels the subscription; then return, and the caller
    connects again.

    The consumer never rejects a delivery. Storing one can fail only while the database is
    unavailable, so it keeps the delivery and tries again until it can."""
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=PREFETCH_COUNT)
    declared = await declare_queue(channel, queue, message_types)
    # RabbitMQ cancels the subscription, and keeps the connection open, when the queue is
    # deleted. Ending the loop then lets the caller connect again and declare the queue again.
    cancelled = asyncio.Event()
    underlay = await channel.get_underlay_channel()
    underlay.on_consumer_cancel_callbacks.add(lambda _: cancelled.set())
    async with declared.iterator() as deliveries:
        closer = asyncio.create_task(_close_when_set(deliveries, stopping, cancelled))
        try:
            async for delivery in deliveries:
                if not await _store(sessions, delivery.body, stopping):
                    # Told to stop with the database still away: closing the connection hands
                    # the unacknowledged delivery back to RabbitMQ.
                    return
                await delivery.ack()
        finally:
            closer.cancel()


async def forget_received(session: AsyncSession) -> None:
    """Delete the ids of messages received more than RETENTION ago."""
    async with session.begin():
        await session.execute(
            delete(ReceivedMessageRecord).where(
                ReceivedMessageRecord.received_at < func.now() - RETENTION
            )
        )


async def _close_when_set(deliveries: AbstractQueueIterator, *events: asyncio.Event) -> None:
    """Close the iterator once any of the events is set. That cancels the subscription, hands any
    delivery RabbitMQ sent ahead back to the queue, and ends the consumer's loop."""
    waits = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
    await deliveries.close()


async def _store(
    sessions: async_sessionmaker[AsyncSession], body: bytes, stopping: asyncio.Event
) -> bool:
    """Store a delivery, retrying while the database is unavailable. Return False if told to stop
    before it could."""
    while True:
        try:
            async with sessions() as session, session.begin():
                now = datetime.now(UTC)
                try:
                    await receive(session, body, now)
                except UnreadableMessageError as error:
                    await bury(session, body, error, now)
        except Exception:
            logger.exception("A delivery could not be stored; trying again shortly")
            try:
                async with asyncio.timeout(STORE_RETRY_INTERVAL.total_seconds()):
                    await stopping.wait()
            except TimeoutError:
                continue
            return False
        else:
            return True


def _storable_text(text: str) -> str:
    # Postgres cannot store U+0000 in jsonb or text.
    return text.replace("\x00", "\N{REPLACEMENT CHARACTER}")
