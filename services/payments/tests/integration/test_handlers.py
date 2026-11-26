"""What the service does with each command: the provider's charge or refund, the ledger's record and
the one reply, however an attempt fails. Each test stores the command as the worker's consumer does,
then runs its job as the worker would."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.jobs import FIRST_RETRY_DELAY, run_next_job
from holdfast.messaging.records import JobRecord
from holdfast.payments import handlers as handlers_module
from holdfast.payments.handlers import handlers
from holdfast.payments.messages import (
    CHARGE_BOOKING,
    CHARGE_DECLINED,
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


class _AnswerLost(Provider):
    """A provider that does what it is asked, but whose answer never reaches the caller."""

    async def charge(self, key: UUID, amount: int, currency: str, payment_method: str) -> bool:
        await super().charge(key, amount, currency, payment_method)
        raise ConnectionError("The provider's answer was lost.")

    async def refund(self, key: UUID) -> None:
        await super().refund(key)
        raise ConnectionError("The provider's answer was lost.")


def _failing_once(provider: Provider, acted: bool) -> Provider:
    """A provider whose calls fail: before it does anything, or after, with the answer lost."""
    if acted:
        return _AnswerLost(provider.sessions)
    return Provider(provider.sessions, failure_rate=1)


def _count_calls(monkeypatch: pytest.MonkeyPatch, method: str) -> list[tuple[Any, ...]]:
    """Record every call to one of the provider's methods, which still runs."""
    calls: list[tuple[Any, ...]] = []
    real = getattr(Provider, method)

    async def counted(self: Provider, *args: Any) -> Any:
        calls.append(args)
        return await real(self, *args)

    monkeypatch.setattr(Provider, method, counted)
    return calls


async def test_a_card_that_charges_is_recorded_and_reported(
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

    ran = await run_next_job(sessions, handlers(provider), now)

    [charge] = await ledger()
    [books] = await provider_books()
    assert ran
    assert (charge.booking_id, charge.amount, charge.currency, charge.status) == (
        booking_id,
        2000,
        "EUR",
        ChargeStatus.CHARGED,
    )
    assert (books.idempotency_key, books.succeeded) == (booking_id, True)
    assert await published() == [(CHARGE_SUCCEEDED, about_booking(booking_id))]
    assert await stored_jobs() == []


@pytest.mark.parametrize("card", ["pm_card_declined", "pm_card_unknown"])
async def test_a_card_that_is_declined_is_recorded_and_reported(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
    card: str,
) -> None:
    booking_id = uuid4()
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(booking_id, card), now)

    await run_next_job(sessions, handlers(provider), now)

    [charge] = await ledger()
    [books] = await provider_books()
    assert charge.status is ChargeStatus.DECLINED
    assert books.succeeded is False
    assert await published() == [(CHARGE_DECLINED, about_booking(booking_id))]


async def test_a_second_command_for_a_booking_already_charged_does_nothing(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    published: Published,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It arrives as a new message, so the inbox lets it through; the ledger stops it before the
    provider is asked again."""
    booking_id = uuid4()
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(booking_id), now)
    await run_next_job(sessions, handlers(provider), now)
    calls = _count_calls(monkeypatch, "charge")
    await receive(CHARGE_BOOKING, charge_command(booking_id, "pm_card_declined"), now)

    ran = await run_next_job(sessions, handlers(provider), now)

    assert ran
    assert calls == []
    assert await published() == [(CHARGE_SUCCEEDED, about_booking(booking_id))]


@pytest.mark.parametrize(
    ("acted", "card", "status", "reply"),
    [
        (False, "pm_card_ok", ChargeStatus.CHARGED, CHARGE_SUCCEEDED),
        (True, "pm_card_ok", ChargeStatus.CHARGED, CHARGE_SUCCEEDED),
        (True, "pm_card_declined", ChargeStatus.DECLINED, CHARGE_DECLINED),
    ],
    ids=["refused before charging", "answer lost after charging", "answer lost after declining"],
)
async def test_a_failed_charge_attempt_is_retried_under_the_same_key(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
    stored_jobs: StoredJobs,
    acted: bool,
    card: str,
    status: ChargeStatus,
    reply: str,
) -> None:
    """The retry asks the provider again with the booking's id, so the outcome of the failed
    attempt is found, never made again or turned into another."""
    booking_id = uuid4()
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(booking_id, card), now)

    await run_next_job(sessions, handlers(_failing_once(provider, acted)), now)
    [failed] = await stored_jobs()
    after_failure = (await ledger(), len(await provider_books()), await published())
    await run_next_job(sessions, handlers(provider), now + FIRST_RETRY_DELAY)

    [charge] = await ledger()
    assert failed.attempts == 1
    assert after_failure == ([], 1 if acted else 0, [])
    assert charge.status is status
    assert len(await provider_books()) == 1
    assert await published() == [(reply, about_booking(booking_id))]
    assert await stored_jobs() == []


@pytest.mark.parametrize(
    ("card", "delay"),
    [("pm_card_slow", timedelta(0)), ("pm_card_ok", timedelta(seconds=30))],
    ids=["a slow card", "a provider slower than the timeout"],
)
async def test_a_first_answer_too_slow_times_out_and_its_retry_finds_the_charge(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
    stored_jobs: StoredJobs,
    monkeypatch: pytest.MonkeyPatch,
    card: str,
    delay: timedelta,
) -> None:
    monkeypatch.setattr(handlers_module, "PROVIDER_TIMEOUT", timedelta(seconds=2))
    slow = Provider(provider.sessions, delay=delay)
    booking_id = uuid4()
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(booking_id, card), now)

    await run_next_job(sessions, handlers(slow), now)
    [timed_out] = await stored_jobs()
    await run_next_job(sessions, handlers(slow), now + FIRST_RETRY_DELAY)

    [charge] = await ledger()
    assert (timed_out.attempts, timed_out.last_error) == (1, "TimeoutError: ")
    assert charge.status is ChargeStatus.CHARGED
    assert len(await provider_books()) == 1
    assert await published() == [(CHARGE_SUCCEEDED, about_booking(booking_id))]


async def test_a_command_that_breaks_the_contract_fails_before_the_provider_is_asked(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    stored_jobs: StoredJobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_calls(monkeypatch, "charge")
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(uuid4()) | {"amount": 0}, now)

    await run_next_job(sessions, handlers(provider), now)

    [failed] = await stored_jobs()
    assert failed.attempts == 1
    assert calls == []


async def test_a_refund_is_made_once_recorded_and_reported(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
    stored_jobs: StoredJobs,
) -> None:
    """A second refund command is a copy: it does nothing, and its job succeeds rather than
    failing as an alarm."""
    booking_id = uuid4()
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(booking_id), now)
    await run_next_job(sessions, handlers(provider), now)

    await receive(REFUND_BOOKING, about_booking(booking_id), now)
    await run_next_job(sessions, handlers(provider), now)
    [refunded] = await provider_books()
    await receive(REFUND_BOOKING, about_booking(booking_id), now)
    await run_next_job(sessions, handlers(provider), now)

    [charge] = await ledger()
    [books] = await provider_books()
    assert charge.status is ChargeStatus.REFUNDED
    assert books.refunded_at is not None
    assert books.refunded_at == refunded.refunded_at
    assert await published() == [
        (CHARGE_SUCCEEDED, about_booking(booking_id)),
        (CHARGE_REFUNDED, about_booking(booking_id)),
    ]
    assert await stored_jobs() == []


@pytest.mark.parametrize(
    "acted", [False, True], ids=["refused before refunding", "answer lost after refunding"]
)
async def test_a_failed_refund_attempt_is_retried_and_refunds_once(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    ledger: Ledger,
    provider_books: Books,
    published: Published,
    acted: bool,
) -> None:
    booking_id = uuid4()
    now = datetime.now(UTC)
    await receive(CHARGE_BOOKING, charge_command(booking_id), now)
    await run_next_job(sessions, handlers(provider), now)
    await receive(REFUND_BOOKING, about_booking(booking_id), now)

    await run_next_job(sessions, handlers(_failing_once(provider, acted)), now)
    [unrefunded] = await ledger()
    [after_failure] = await provider_books()
    replies_after_failure = await published()
    await run_next_job(sessions, handlers(provider), now + FIRST_RETRY_DELAY)

    [charge] = await ledger()
    [books] = await provider_books()
    assert unrefunded.status is ChargeStatus.CHARGED
    assert (after_failure.refunded_at is not None) is acted
    assert replies_after_failure == [(CHARGE_SUCCEEDED, about_booking(booking_id))]
    assert charge.status is ChargeStatus.REFUNDED
    if acted:
        assert books.refunded_at == after_failure.refunded_at
    assert await published() == [
        (CHARGE_SUCCEEDED, about_booking(booking_id)),
        (CHARGE_REFUNDED, about_booking(booking_id)),
    ]


@pytest.mark.parametrize("card", [None, "pm_card_declined"], ids=["never charged", "declined"])
async def test_a_refund_of_a_charge_that_did_not_succeed_fails_and_moves_no_money(
    sessions: Sessions,
    provider: Provider,
    receive: Receive,
    provider_books: Books,
    published: Published,
    stored_jobs: StoredJobs,
    monkeypatch: pytest.MonkeyPatch,
    card: str | None,
) -> None:
    """The booking service never asks for one, so the failing job is an alarm."""
    booking_id = uuid4()
    now = datetime.now(UTC)
    if card is not None:
        await receive(CHARGE_BOOKING, charge_command(booking_id, card), now)
        await run_next_job(sessions, handlers(provider), now)
    replies_before = await published()
    calls = _count_calls(monkeypatch, "refund")
    await receive(REFUND_BOOKING, about_booking(booking_id), now)

    await run_next_job(sessions, handlers(provider), now)

    [failed] = await stored_jobs()
    assert failed.attempts == 1
    assert (failed.last_error or "").startswith("NothingToRefundError")
    assert calls == []
    assert [books.refunded_at for books in await provider_books()] == ([None] if card else [])
    assert await published() == replies_before
