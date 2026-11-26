"""The worker, end to end: the events published to RabbitMQ come out as emails."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel

from holdfast.messaging.envelope import Envelope
from holdfast.messaging.inbox import declare_queue
from holdfast.messaging.outbox import declare_exchange
from holdfast.messaging.records import JobRecord
from holdfast.notifications import worker
from holdfast.notifications.config import WorkerSettings
from holdfast.notifications.events import BOOKING_CANCELLED, BOOKING_CONFIRMED
from holdfast.notifications.mail import Mailer

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Inbox = Callable[[], Awaitable[list[Json]]]
type StoredJobs = Callable[[], Awaitable[list[JobRecord]]]

WAIT_SECONDS = 30


@pytest.fixture
async def channel(rabbitmq_url: str) -> AsyncIterator[AbstractChannel]:
    """A channel to the broker. The service's queue is deleted afterwards, with what is left in
    it."""
    async with await aio_pika.connect(rabbitmq_url) as connection:
        channel = await connection.channel()
        yield channel
        await channel.queue_delete(worker.QUEUE)


async def test_a_confirmation_delivered_twice_is_emailed_once(
    channel: AbstractChannel,
    database_url: str,
    rabbitmq_url: str,
    mailer: Mailer,
    inbox: Inbox,
    stored_jobs: StoredJobs,
) -> None:
    """RabbitMQ delivers a message again when a consumer stopped before acknowledging it. The
    cancellation is published last, so once its email is out, both copies of the confirmation
    have arrived."""
    now = datetime.now(UTC)
    booking_id = uuid4()
    booking = {
        "booking_id": str(booking_id),
        "recipient": "ada@example.com",
        "resource_name": "Room 1",
        "location_name": "Central",
        "address": "Damrak 1, Amsterdam",
        "timezone": "Europe/Amsterdam",
        "start": (now + timedelta(days=1)).isoformat(),
        "end": (now + timedelta(days=1, hours=1)).isoformat(),
    }
    confirmed = Envelope(
        id=uuid4(), type=BOOKING_CONFIRMED, time=now, data=booking | {"price": 0, "currency": "EUR"}
    )
    cancelled = Envelope(id=uuid4(), type=BOOKING_CANCELLED, time=now, data=booking)
    # Declared as the worker declares it, so that the events wait in it for the worker.
    await declare_queue(channel, worker.QUEUE, [BOOKING_CONFIRMED, BOOKING_CANCELLED])
    exchange = await declare_exchange(channel)
    for envelope in (confirmed, confirmed, cancelled):
        body = envelope.model_dump_json().encode()
        await exchange.publish(aio_pika.Message(body), routing_key=envelope.type)
    settings = WorkerSettings(
        database_url=database_url,
        rabbitmq_url=rabbitmq_url,
        smtp_host=mailer.host,
        smtp_port=mailer.port,
        mail_from=mailer.sender,
    )

    stopping = asyncio.Event()
    running = asyncio.create_task(worker.run_worker(settings, stopping))
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            while True:
                sent = {email["MessageID"] for email in await inbox()}
                if f"cancelled.{booking_id}@holdfast" in sent and await stored_jobs() == []:
                    break
                await asyncio.sleep(0.1)
    finally:
        stopping.set()
        await running

    assert sorted(email["MessageID"] for email in await inbox()) == [
        f"cancelled.{booking_id}@holdfast",
        f"confirmed.{booking_id}@holdfast",
    ]
