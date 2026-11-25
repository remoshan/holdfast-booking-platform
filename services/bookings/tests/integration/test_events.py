"""The events the booking service publishes through its outbox: one for each change that a service
receiving them cares about, carrying everything it needs, and none for a request that changes
nothing."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient, Response

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Book = Callable[..., Awaitable[Response]]
type Published = Callable[[], Awaitable[list[tuple[str, Json]]]]


async def test_a_free_booking_announces_that_it_is_confirmed(
    book: Book, free_resource: Json, user: Json, published: Published
) -> None:
    booking = (await book("09:00", "10:30", resource_id=free_resource["id"])).json()

    [(message_type, data)] = await published()

    assert message_type == "bookings.booking_confirmed"
    assert data | {"start": None, "end": None} == {
        "booking_id": booking["id"],
        "recipient": user["email"],
        "resource_name": "Desk 1",
        "location_name": "Central",
        "address": "Damrak 1, Amsterdam",
        "timezone": "Europe/Amsterdam",
        "start": None,
        "end": None,
        "price": 0,
        "currency": "EUR",
    }
    assert [datetime.fromisoformat(data[edge]) for edge in ("start", "end")] == [
        datetime.fromisoformat(booking[edge]) for edge in ("start", "end")
    ]


async def test_a_paid_booking_is_announced_once_it_is_confirmed_and_only_once(
    client: AsyncClient, organisation: Json, book: Book, published: Published
) -> None:
    held = (await book("09:00", "10:00")).json()
    while_held = await published()
    for _ in range(2):
        await client.post(f"/organisations/{organisation['id']}/bookings/{held['id']}/confirm")

    [(message_type, data)] = await published()

    assert while_held == []
    assert message_type == "bookings.booking_confirmed"
    assert (data["booking_id"], data["price"], data["currency"]) == (held["id"], 2000, "EUR")


async def test_cancelling_a_booking_announces_it_once(
    client: AsyncClient, organisation: Json, book: Book, free_resource: Json, published: Published
) -> None:
    booking = (await book("09:00", "10:00", resource_id=free_resource["id"])).json()
    for _ in range(2):
        await client.post(f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel")

    announced = await published()

    assert [message_type for message_type, _ in announced] == [
        "bookings.booking_confirmed",
        "bookings.booking_cancelled",
    ]
    assert announced[1][1]["booking_id"] == booking["id"]
    assert "price" not in announced[1][1]


async def test_a_request_that_changes_nothing_announces_nothing(
    book: Book, free_resource: Json, published: Published
) -> None:
    """A retried request returns the booking the first one made, and a refused one writes
    nothing: neither announces anything."""
    key = {"Idempotency-Key": str(uuid4())}
    for _ in range(2):
        await book("09:00", "10:00", headers=key, resource_id=free_resource["id"])
    refused = await book("09:00", "10:00", resource_id=free_resource["id"])

    assert refused.status_code == 409
    assert [message_type for message_type, _ in await published()] == ["bookings.booking_confirmed"]


async def test_nothing_is_announced_to_a_booker_who_has_left_the_organisation(
    client: AsyncClient,
    organisation: Json,
    book: Book,
    free_resource: Json,
    add_member: Callable[..., Awaitable[Json]],
    published: Published,
) -> None:
    """They can no longer see the booking, and must not be sent its current details, so an admin
    cancelling it afterwards sends them nothing."""
    member = await add_member(organisation["id"], "member")
    booking = (
        await book("09:00", "10:00", headers=member["headers"], resource_id=free_resource["id"])
    ).json()
    await client.delete(f"/organisations/{organisation['id']}/members/{member['id']}")

    cancelled = await client.post(
        f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel"
    )

    assert cancelled.json()["status"] == "cancelled"
    assert [message_type for message_type, _ in await published()] == ["bookings.booking_confirmed"]
