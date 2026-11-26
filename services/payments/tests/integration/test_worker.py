"""The worker, end to end: commands published to RabbitMQ come back as one reply each."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractQueue

from holdfast.messaging.envelope import Envelope
from holdfast.messaging.inbox import declare_queue
from holdfast.messaging.outbox import declare_exchange
from holdfast.messaging.records import JobRecord
from holdfast.payments import worker
from holdfast.payments.config import WorkerSettings
from holdfast.payments.messages import CHARGE_BOOKING, CHARGE_SUCCEEDED, REFUND_BOOKING
from holdfast.payments.records import ProviderChargeRecord
from tests.support import Json, charge_command

pytestmark = pytest.mark.anyio

type StoredJobs = Callable[[], Awaitable[list[JobRecord]]]
type Published = Callable[[], Awaitable[list[tuple[str, Json]]]]
type Books = Callable[[], Awaitable[list[ProviderChargeRecord]]]

WAIT_SECONDS = 30


@pytest.fixture
async def channel(rabbitmq_url: str) -> AsyncIterator[AbstractChannel]:
    """A channel to the broker. The service's queue is deleted afterwards, with what is left in
    it."""
    async with await aio_pika.connect(rabbitmq_url) as connection:
        channel = await connection.channel()
        yield channel
        await channel.queue_delete(worker.QUEUE)


async def test_a_charge_delivered_twice_charges_once_and_replies_once(
    channel: AbstractChannel,
    database_url: str,
    rabbitmq_url: str,
    stored_jobs: StoredJobs,
    published: Published,
    provider_books: Books,
) -> None:
    """The second copy comes as a new message, so only the ledger can stop it. Another booking's
    command is published last: once its reply is back and nothing is left to do, both copies
    have been handled and every reply has reached the broker."""
    now = datetime.now(UTC)
    twice, last = uuid4(), uuid4()
    exchange = await declare_exchange(channel)
    replies = await channel.declare_queue(exclusive=True)
    await replies.bind(exchange, routing_key=CHARGE_SUCCEEDED)
    # Declared as the worker declares it, so that the commands wait in it for the worker.
    await declare_queue(channel, worker.QUEUE, [CHARGE_BOOKING, REFUND_BOOKING])
    for booking_id in (twice, twice, last):
        envelope = Envelope(
            id=uuid4(), type=CHARGE_BOOKING, time=now, data=charge_command(booking_id)
        )
        body = envelope.model_dump_json().encode()
        await exchange.publish(aio_pika.Message(body), routing_key=CHARGE_BOOKING)
    # The provider's trouble is pinned off, whatever the shell exports for trying failures out.
    settings = WorkerSettings(
        database_url=database_url,
        rabbitmq_url=rabbitmq_url,
        provider_delay_seconds=0,
        provider_failure_rate=0,
    )
    received: list[Json] = []

    stopping = asyncio.Event()
    running = asyncio.create_task(worker.run_worker(settings, stopping))
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                received += await _take_all(replies)
                answered = {reply["booking_id"] for reply in received}
                if str(last) in answered and await stored_jobs() == [] and await published() == []:
                    break
                await asyncio.sleep(0.1)
    finally:
        stopping.set()
        await running
    received += await _take_all(replies)

    assert sorted(reply["booking_id"] for reply in received) == sorted([str(twice), str(last)])
    assert len(await provider_books()) == 2


async def _take_all(queue: AbstractQueue) -> list[Json]:
    """The data of every reply waiting in the queue."""
    taken = []
    while (message := await queue.get(no_ack=True, fail=False)) is not None:
        taken.append(Envelope.model_validate_json(message.body).data)
    return taken
