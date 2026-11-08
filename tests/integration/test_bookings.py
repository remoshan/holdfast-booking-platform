from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient, Response

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]
type Book = Callable[..., Awaitable[Response]]


async def test_book_resource(
    book: Book, organisation: Json, resource: Json, user: Json, at: Callable[..., str]
) -> None:
    response = await book("09:00", "10:30")

    assert response.status_code == status.HTTP_201_CREATED
    booking = response.json()
    assert booking["resource_id"] == resource["id"]
    assert booking["user_id"] == user["id"]
    assert booking["status"] == "confirmed"
    assert datetime.fromisoformat(booking["start"]) == datetime.fromisoformat(at("09:00"))
    assert datetime.fromisoformat(booking["end"]) == datetime.fromisoformat(at("10:30"))
    assert booking["price"] == 3000  # 1.5 hours at 2000 per hour
    assert response.headers["Location"] == (
        f"http://test/organisations/{organisation['id']}/bookings/{booking['id']}"
    )


async def test_overlapping_booking_is_rejected(book: Book) -> None:
    await book("09:00", "10:00")

    response = await book("09:45", "11:00")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["type"].endswith("#booking-conflict")


async def test_back_to_back_bookings_are_allowed(book: Book) -> None:
    first = await book("09:00", "10:00")
    second = await book("10:00", "11:00")

    assert (first.status_code, second.status_code) == (201, 201)


@pytest.mark.parametrize(
    ("start", "end", "days_ahead", "rule"),
    [
        ("09:10", "10:00", 7, "15-minute boundary"),
        ("07:00", "09:00", 7, "opening hours"),
        ("17:00", "18:30", 7, "opening hours"),
        ("09:00", "10:00", -1, "in the past"),
        ("09:00", "10:00", 31, "30 days"),
    ],
)
async def test_booking_rules_are_enforced(
    book: Book, start: str, end: str, days_ahead: int, rule: str
) -> None:
    response = await book(start, end, days_ahead)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    problem = response.json()
    assert problem["type"].endswith("#invalid-booking")
    assert rule in problem["detail"]


async def test_booking_must_end_after_it_starts(book: Book) -> None:
    response = await book("10:00", "09:00")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["errors"] == [
        {"detail": "end must be later than start.", "pointer": "#"}
    ]


async def test_booking_times_need_a_utc_offset(
    client: AsyncClient, organisation: Json, resource: Json
) -> None:
    response = await client.post(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/bookings",
        json={"start": "2026-11-09T09:00:00", "end": "2026-11-09T10:00:00Z"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [error["pointer"] for error in response.json()["errors"]] == ["#/start"]


async def test_booking_another_organisations_resource_returns_404(
    client: AsyncClient, resource: Json, at: Callable[..., str], create_organisation: Factory
) -> None:
    other = await create_organisation(slug="other")

    response = await client.post(
        f"/organisations/{other['id']}/resources/{resource['id']}/bookings",
        json={"start": at("09:00"), "end": at("10:00")},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_list_bookings(
    client: AsyncClient, organisation: Json, resource: Json, book: Book
) -> None:
    first = (await book("09:00", "10:00")).json()
    second = (await book("11:00", "12:00")).json()

    response = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/bookings"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"items": [first, second], "next_cursor": None}


async def test_list_bookings_of_unknown_resource_returns_404(
    client: AsyncClient, organisation: Json
) -> None:
    response = await client.get(f"/organisations/{organisation['id']}/resources/{uuid4()}/bookings")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_get_booking(client: AsyncClient, organisation: Json, book: Book) -> None:
    booking = (await book("09:00", "10:00")).json()

    response = await client.get(f"/organisations/{organisation['id']}/bookings/{booking['id']}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == booking


async def test_get_booking_of_another_organisation_returns_404(
    client: AsyncClient, book: Book, create_organisation: Factory
) -> None:
    booking = (await book("09:00", "10:00")).json()
    other = await create_organisation(slug="other")

    response = await client.get(f"/organisations/{other['id']}/bookings/{booking['id']}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_cancelled_booking_frees_its_slot(
    client: AsyncClient, organisation: Json, book: Book
) -> None:
    booking = (await book("09:00", "10:00")).json()

    response = await client.post(
        f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == booking | {"status": "cancelled"}
    assert (await book("09:00", "10:00")).status_code == status.HTTP_201_CREATED


async def test_cancelling_twice_returns_the_cancelled_booking(
    client: AsyncClient, organisation: Json, book: Book
) -> None:
    booking = (await book("09:00", "10:00")).json()
    cancel_path = f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel"
    await client.post(cancel_path)

    response = await client.post(cancel_path)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "cancelled"


async def test_cancel_unknown_booking_returns_404(client: AsyncClient, organisation: Json) -> None:
    response = await client.post(f"/organisations/{organisation['id']}/bookings/{uuid4()}/cancel")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_members_list_only_their_own_bookings(
    client: AsyncClient, organisation: Json, resource: Json, book: Book, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")
    owners_booking = (await book("09:00", "10:00")).json()
    members_booking = (await book("11:00", "12:00", headers=member["headers"])).json()
    path = f"/organisations/{organisation['id']}/resources/{resource['id']}/bookings"

    as_member = await client.get(path, headers=member["headers"])
    as_owner = await client.get(path)

    assert as_member.json()["items"] == [members_booking]
    assert as_owner.json()["items"] == [owners_booking, members_booking]


async def test_members_cannot_see_or_cancel_other_peoples_bookings(
    client: AsyncClient, organisation: Json, book: Book, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")
    booking = (await book("09:00", "10:00")).json()
    path = f"/organisations/{organisation['id']}/bookings/{booking['id']}"

    seen = await client.get(path, headers=member["headers"])
    cancelled = await client.post(f"{path}/cancel", headers=member["headers"])

    assert (seen.status_code, cancelled.status_code) == (404, 404)


async def test_members_can_cancel_their_own_bookings(
    client: AsyncClient, organisation: Json, book: Book, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")
    booking = (await book("09:00", "10:00", headers=member["headers"])).json()

    response = await client.post(
        f"/organisations/{organisation['id']}/bookings/{booking['id']}/cancel",
        headers=member["headers"],
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "cancelled"
