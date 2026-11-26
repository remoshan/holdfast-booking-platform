"""Workers racing over copies of one command: the booking is charged once, and refunded once."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.jobs import run_next_job
from holdfast.messaging.records import JobRecord
from holdfast.payments.handlers import handlers
from holdfast.payments.messages import (
    CHARGE_BOOKING,
    CHARGE_REFUNDED,
    CHARGE_SUCCEEDED,
    REFUND_BOOKING,
)
from holdfast.payments.provider import Provider
from holdfast.payments.records import ChargeRecord, ChargeStatus, ProviderChargeRecord
from tests.support import Json, about_booking, charge_command

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type Receive = Callable[[str, Json, datetime], Awaitable[None]]
type StoredJobs = Callable[[], Awaitable[list[JobRecord]]]
type Published = Callable[[], Awaitable[list[tuple[str, Json]]]]
type Ledger = Callable[[], Awaitable[list[ChargeRecord]]]
type Books = Callable[[], Awaitable[list[ProviderChargeRecord]]]

RACING_WORKERS = 4
COPIES = 10
"""Each arrives as a new message, so the inbox lets every one through."""
FIRST_ANSWER_DELAY = timedelta(seconds=0.5)
"""Keeps the worker that reaches the provider first inside its job, and its transaction, while the
others arrive, so that the copies really overlap."""


async def test_racing_workers_charge_a_booking_once_however_many_copies_arrive(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
    stored_jobs: StoredJobs,
) -> None:
    booking_id = uuid4()
    now = datetime.now(UTC)
    for _ in range(COPIES):
        await receive(CHARGE_BOOKING, charge_command(booking_id), now)

    await _race(sessions, provider, now)

    assert [(charge.booking_id, charge.status) for charge in await ledger()] == [
        (booking_id, ChargeStatus.CHARGED)
    ]
    assert [books.idempotency_key for books in await provider_books()] == [booking_id]
    assert await published() == [(CHARGE_SUCCEEDED, about_booking(booking_id))]
    assert await stored_jobs() == []


async def test_racing_workers_refund_a_booking_once_however_many_copies_arrive(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
    stored_jobs: StoredJobs,
) -> None:
    booking_id = uuid4()
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(booking_id), now)
    await run_next_job(sessions, handlers(provider), now)
    for _ in range(COPIES):
        await receive(REFUND_BOOKING, about_booking(booking_id), now)

    await _race(sessions, provider, now)

    [charge] = await ledger()
    [books] = await provider_books()
    assert charge.status is ChargeStatus.REFUNDED
    assert books.refunded_at is not None
    assert [message_type for message_type, _ in await published()] == [
        CHARGE_SUCCEEDED,
        CHARGE_REFUNDED,
    ]
    assert await stored_jobs() == []


async def _race(sessions: Sessions, provider: Provider, now: datetime) -> None:
    slow_to_answer = Provider(provider.sessions, delay=FIRST_ANSWER_DELAY)

    async def work() -> None:
        while await run_next_job(sessions, handlers(slow_to_answer), now):
            pass

    await asyncio.gather(*(work() for _ in range(RACING_WORKERS)))
