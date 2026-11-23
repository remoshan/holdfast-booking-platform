from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient, Response

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]
type Book = Callable[..., Awaitable[Response]]


def _free_periods(availability: Json) -> list[tuple[datetime, datetime]]:
    return [
        (datetime.fromisoformat(period["start"]), datetime.fromisoformat(period["end"]))
        for period in availability["free"]
    ]


async def test_availability_leaves_out_booked_periods(
    client: AsyncClient,
    organisation: Json,
    resource: Json,
    book: Book,
    booking_day: date,
    at: Callable[..., str],
) -> None:
    await book("09:00", "10:00")
    await book("12:00", "13:30")

    response = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/availability",
        params={"date": booking_day.isoformat()},
    )

    assert response.status_code == status.HTTP_200_OK
    availability = response.json()
    assert availability["timezone"] == "Europe/Amsterdam"
    expected = [("08:00", "09:00"), ("10:00", "12:00"), ("13:30", "18:00")]
    assert _free_periods(availability) == [
        (datetime.fromisoformat(at(start)), datetime.fromisoformat(at(end)))
        for start, end in expected
    ]


async def test_availability_ignores_cancelled_bookings(
    client: AsyncClient,
    organisation: Json,
    resource: Json,
    book: Book,
    booking_day: date,
    at: Callable[..., str],
) -> None:
    booking = (await book("09:00", "10:00")).json()
    await client.post(f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel")

    response = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/availability",
        params={"date": booking_day.isoformat()},
    )

    assert _free_periods(response.json()) == [
        (datetime.fromisoformat(at("08:00")), datetime.fromisoformat(at("18:00")))
    ]


async def test_closed_location_has_no_availability(
    client: AsyncClient,
    organisation: Json,
    create_location: Factory,
    create_resource: Factory,
    booking_day: date,
) -> None:
    closed = await create_location(organisation["id"], opening_hours={})
    resource = await create_resource(organisation["id"], closed["id"])

    response = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/availability",
        params={"date": booking_day.isoformat()},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["free"] == []


async def test_availability_of_unknown_resource_returns_404(
    client: AsyncClient, organisation: Json, booking_day: date
) -> None:
    response = await client.get(
        f"/organisations/{organisation['id']}/resources/{uuid4()}/availability",
        params={"date": booking_day.isoformat()},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_availability_needs_a_valid_date(
    client: AsyncClient, organisation: Json, resource: Json
) -> None:
    response = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/availability",
        params={"date": "2026-02-30"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [error["parameter"] for error in response.json()["errors"]] == ["date"]
