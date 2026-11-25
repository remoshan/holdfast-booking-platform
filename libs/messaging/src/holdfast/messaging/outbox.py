"""Sending messages. A message is a row of the outbox, written in the transaction of the change that
sends it, so it exists exactly when the change does. The relay publishes committed rows to
RabbitMQ, and deletes each one only once RabbitMQ has confirmed that it stored it.

A row has no attempt count and is never given up: a broker that is down, or a message type that no
queue takes yet, is never the message's fault, so it simply waits for the next pass.
"""

import asyncio
import logging
from collections import Counter
from datetime import timedelta
from uuid import UUID

from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractExchange
from aio_pika.exceptions import PublishError
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.envelope import Envelope, check_type
from holdfast.messaging.records import OutboxRecord

EXCHANGE = "holdfast"
"""The one exchange every message goes through, routed by its type."""
PAGE_SIZE = 100
PUBLISH_TIMEOUT = timedelta(seconds=10)
"""How long a publish may wait for RabbitMQ to confirm it, so a broker that stopped answering
cannot hold a page of the outbox, and its locks, for ever."""

logger = logging.getLogger(__name__)


async def publish(session: AsyncSession, message_type: str, data: BaseModel) -> None:
    """Send a message once the caller's transaction commits. It rolls back with the transaction,
    including a failing job's savepoint, so no message ever announces a change that did not
    happen."""
    session.add(OutboxRecord(type=check_type(message_type), data=data.model_dump(mode="json")))
    await session.flush()


async def declare_exchange(channel: AbstractChannel) -> AbstractExchange:
    """Declare the exchange, which every relay and consumer does on each connection. Declaring it
    again with the same arguments changes nothing."""
    return await channel.declare_exchange(EXCHANGE, ExchangeType.DIRECT, durable=True)


async def relay(
    sessions: async_sessionmaker[AsyncSession], exchange: AbstractExchange, stopping: asyncio.Event
) -> int:
    """Make one pass over the outbox: publish every committed message, page by page in id order,
    and delete the ones RabbitMQ confirmed. Return how many were published.

    The exchange's channel must have on_return_raises set. A message that no queue takes then
    comes back as a PublishError: it stays in the outbox for the next pass, and the pass carries on
    after it, so it never holds back the messages behind it. Any other failure, such as a negative
    acknowledgement, a timeout or a lost connection, rolls the page back, so every message in it
    is published again; the receiver drops the copies of those that had already arrived.
    """
    published = 0
    waiting: Counter[str] = Counter()
    after: UUID | None = None
    while not stopping.is_set():
        async with sessions() as session, session.begin():
            query = (
                select(OutboxRecord)
                .order_by(OutboxRecord.id)
                .limit(PAGE_SIZE)
                .with_for_update(skip_locked=True)
            )
            if after is not None:
                query = query.where(OutboxRecord.id > after)
            page = list(await session.scalars(query))
            if not page:
                break
            # Read now: committing expires the rows.
            after = page[-1].id
            outcomes = await asyncio.gather(
                *(_publish(exchange, record) for record in page), return_exceptions=True
            )
            confirmed = []
            for record, outcome in zip(page, outcomes, strict=True):
                if isinstance(outcome, PublishError):
                    waiting[record.type] += 1
                elif isinstance(outcome, asyncio.CancelledError):
                    # aiormq fails a confirm this way when it gives up on a silent connection.
                    raise ConnectionError("RabbitMQ went silent before confirming") from outcome
                elif isinstance(outcome, BaseException):
                    raise outcome
                else:
                    confirmed.append(record.id)
            await session.execute(delete(OutboxRecord).where(OutboxRecord.id.in_(confirmed)))
        published += len(confirmed)
    for message_type, count in waiting.items():
        logger.warning(
            "No queue takes messages of type %s yet; %d of them wait in the outbox",
            message_type,
            count,
        )
    return published


async def _publish(exchange: AbstractExchange, record: OutboxRecord) -> None:
    envelope = Envelope(id=record.id, type=record.type, time=record.created_at, data=record.data)
    message = Message(
        envelope.model_dump_json().encode(),
        message_id=str(envelope.id),
        type=envelope.type,
        content_type="application/json",
        # Stored on disk by RabbitMQ before it confirms, so a confirmed message survives a restart.
        delivery_mode=DeliveryMode.PERSISTENT,
    )
    await exchange.publish(
        message,
        routing_key=envelope.type,
        mandatory=True,
        timeout=PUBLISH_TIMEOUT.total_seconds(),
    )
