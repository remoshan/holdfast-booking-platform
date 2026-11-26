"""The simulated provider: the card decides the outcome, a key it has seen gets the first answer
again, and its settings delay the answers or refuse the calls."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

import pytest

from holdfast.payments.provider import Provider, ProviderUnavailableError
from holdfast.payments.records import ProviderChargeRecord

pytestmark = pytest.mark.anyio

type Books = Callable[[], Awaitable[list[ProviderChargeRecord]]]

CALLER_PATIENCE_SECONDS = 2
"""How long a test waits for an answer it should never get in time."""


@pytest.mark.parametrize(
    ("card", "charged"),
    [("pm_card_ok", True), ("pm_card_declined", False), ("pm_card_unknown", False)],
)
async def test_the_card_decides_whether_a_charge_succeeds(
    provider: Provider, provider_books: Books, card: str, charged: bool
) -> None:
    key = uuid4()

    answer = await provider.charge(key, 2000, "EUR", card)

    [books] = await provider_books()
    assert answer is charged
    assert (books.idempotency_key, books.amount, books.currency, books.succeeded) == (
        key,
        2000,
        "EUR",
        charged,
    )


@pytest.mark.parametrize(
    ("first_card", "second_card", "answer"),
    [("pm_card_ok", "pm_card_declined", True), ("pm_card_declined", "pm_card_ok", False)],
    ids=["charged first", "declined first"],
)
async def test_a_key_the_provider_has_seen_gets_the_first_answer_and_no_new_charge(
    provider: Provider, provider_books: Books, first_card: str, second_card: str, answer: bool
) -> None:
    key = uuid4()

    first = await provider.charge(key, 2000, "EUR", first_card)
    again = await provider.charge(key, 5000, "USD", second_card)

    [books] = await provider_books()
    assert (first, again) == (answer, answer)
    assert (books.amount, books.currency, books.succeeded) == (2000, "EUR", answer)


async def test_a_charge_is_refunded_once(provider: Provider, provider_books: Books) -> None:
    key = uuid4()
    await provider.charge(key, 2000, "EUR", "pm_card_ok")

    await provider.refund(key)
    [refunded] = await provider_books()
    await provider.refund(key)
    [again] = await provider_books()

    assert refunded.refunded_at is not None
    assert again.refunded_at == refunded.refunded_at


@pytest.mark.parametrize("card", [None, "pm_card_declined"], ids=["never charged", "declined"])
async def test_only_a_charge_that_succeeded_can_be_refunded(
    provider: Provider, provider_books: Books, card: str | None
) -> None:
    key = uuid4()
    if card is not None:
        await provider.charge(key, 2000, "EUR", card)

    with pytest.raises(LookupError):
        await provider.refund(key)

    assert [books.refunded_at for books in await provider_books()] == ([None] if card else [])


async def test_a_slow_card_is_charged_but_answers_too_late_the_first_time(
    provider: Provider, provider_books: Books
) -> None:
    key = uuid4()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(CALLER_PATIENCE_SECONDS):
            await provider.charge(key, 2000, "EUR", "pm_card_slow")
    [books] = await provider_books()
    async with asyncio.timeout(CALLER_PATIENCE_SECONDS):
        retried = await provider.charge(key, 2000, "EUR", "pm_card_slow")

    assert books.succeeded
    assert retried is True


async def test_a_first_answer_delayed_past_the_callers_patience_leaves_the_charge_made(
    provider: Provider, provider_books: Books
) -> None:
    """The retry is answered at once, from the provider's books."""
    slow = Provider(provider.sessions, delay=timedelta(seconds=30))
    key = uuid4()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(CALLER_PATIENCE_SECONDS):
            await slow.charge(key, 2000, "EUR", "pm_card_ok")
    [books] = await provider_books()
    async with asyncio.timeout(CALLER_PATIENCE_SECONDS):
        retried = await slow.charge(key, 2000, "EUR", "pm_card_ok")

    assert books.succeeded
    assert retried is True


async def test_a_first_refund_answer_delayed_past_the_callers_patience_leaves_the_refund_made(
    provider: Provider, provider_books: Books
) -> None:
    """The retry is answered at once, and refunds nothing more."""
    slow = Provider(provider.sessions, delay=timedelta(seconds=30))
    key = uuid4()
    await provider.charge(key, 2000, "EUR", "pm_card_ok")

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(CALLER_PATIENCE_SECONDS):
            await slow.refund(key)
    [refunded] = await provider_books()
    async with asyncio.timeout(CALLER_PATIENCE_SECONDS):
        await slow.refund(key)

    [books] = await provider_books()
    assert refunded.refunded_at is not None
    assert books.refunded_at == refunded.refunded_at


async def test_a_provider_that_refuses_every_call_does_nothing(
    provider: Provider, provider_books: Books
) -> None:
    down = Provider(provider.sessions, failure_rate=1)
    key = uuid4()

    with pytest.raises(ProviderUnavailableError):
        await down.charge(key, 2000, "EUR", "pm_card_ok")
    nothing_charged = await provider_books()
    await provider.charge(key, 2000, "EUR", "pm_card_ok")
    with pytest.raises(ProviderUnavailableError):
        await down.refund(key)

    [books] = await provider_books()
    assert nothing_charged == []
    assert books.refunded_at is None
