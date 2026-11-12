from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient, Response

from holdfast.domain.bookings import HOLD_DURATION, Booking
from holdfast.domain.periods import Period
from holdfast.repositories import bookings

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]
type Book = Callable[..., Awaitable[Response]]


async def test_book_resource(
    book: Book, organisation: Json, resource: Json, user: Json, at: Callable[..., str]
) -> None:
    before = datetime.now(UTC)
    response = await book("09:00", "10:30")
    after = datetime.now(UTC)

    assert response.status_code == status.HTTP_201_CREATED
    booking = response.json()
    assert booking["resource_id"] == resource["id"]
    assert booking["user_id"] == user["id"]
    assert booking["status"] == "held"
    assert datetime.fromisoformat(booking["start"]) == datetime.fromisoformat(at("09:00"))
    assert datetime.fromisoformat(booking["end"]) == datetime.fromisoformat(at("10:30"))
    assert booking["price"] == 3000  # 1.5 hours at 2000 per hour
    hold_expires_at = datetime.fromisoformat(booking["hold_expires_at"])
    assert before + HOLD_DURATION <= hold_expires_at <= after + HOLD_DURATION
    assert response.headers["Location"] == (
        f"http://test/organisations/{organisation['id']}/bookings/{booking['id']}"
    )


async def test_a_free_booking_is_confirmed_at_once(book: Book, free_resource: Json) -> None:
    response = await book("09:00", "10:00", resource_id=free_resource["id"])

    assert response.status_code == status.HTTP_201_CREATED
    assert (response.json()["status"], response.json()["hold_expires_at"]) == ("confirmed", None)


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


async def test_confirm_a_held_booking(client: AsyncClient, organisation: Json, book: Book) -> None:
    booking = (await book("09:00", "10:00")).json()
    path = f"/organisations/{organisation['id']}/bookings/{booking['id']}"

    response = await client.post(f"{path}/confirm")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == booking | {"status": "confirmed"}
    assert (await client.get(path)).json()["status"] == "confirmed"


async def test_confirming_twice_returns_the_confirmed_booking(
    client: AsyncClient, organisation: Json, book: Book
) -> None:
    booking = (await book("09:00", "10:00")).json()
    confirm_path = f"/organisations/{organisation['id']}/bookings/{booking['id']}/confirm"
    first = await client.post(confirm_path)

    second = await client.post(confirm_path)

    assert second.status_code == status.HTTP_200_OK
    assert second.json() == first.json()


async def test_a_cancelled_booking_cannot_be_confirmed(
    client: AsyncClient, organisation: Json, book: Book
) -> None:
    booking = (await book("09:00", "10:00")).json()
    path = f"/organisations/{organisation['id']}/bookings/{booking['id']}"
    await client.post(f"{path}/cancel")

    response = await client.post(f"{path}/confirm")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == "A booking that is cancelled cannot be confirmed."


async def test_a_hold_that_has_run_out_cannot_be_confirmed(
    app: FastAPI, client: AsyncClient, organisation: Json, resource: Json, user: Json
) -> None:
    """Even before the worker has released it: the hold's expiry decides, not the job."""
    now = datetime.now(UTC)
    start = now + timedelta(days=1)
    booking = await _write_booking(
        app, organisation, resource, user, start, start + timedelta(hours=1), held_until=now
    )

    response = await client.post(
        f"/organisations/{organisation['id']}/bookings/{booking.id}/confirm"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == "The hold on this booking has run out."


async def test_a_repeated_idempotency_key_returns_the_first_booking(
    client: AsyncClient, organisation: Json, resource: Json, book: Book
) -> None:
    key = {"Idempotency-Key": str(uuid4())}
    first = await book("09:00", "10:00", headers=key)

    retried = await book("09:00", "10:00", headers=key)

    assert (first.status_code, retried.status_code) == (201, 201)
    assert retried.json() == first.json()
    assert retried.headers["Location"] == first.headers["Location"]
    stored = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/bookings"
    )
    assert stored.json()["items"] == [first.json()]


async def test_an_idempotency_key_cannot_be_reused_for_another_booking(book: Book) -> None:
    key = {"Idempotency-Key": "my-first-booking"}
    await book("09:00", "10:00", headers=key)

    response = await book("11:00", "12:00", headers=key)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["type"].endswith("#idempotency-key-reused")


async def test_each_user_has_their_own_idempotency_keys(
    organisation: Json, book: Book, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")
    key = {"Idempotency-Key": "booking-1"}

    owners = await book("09:00", "10:00", headers=key)
    members = await book("11:00", "12:00", headers=key | member["headers"])

    assert (owners.status_code, members.status_code) == (201, 201)
    assert owners.json()["id"] != members.json()["id"]


async def test_a_key_whose_request_failed_can_be_used_again(
    client: AsyncClient, organisation: Json, book: Book, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")
    taken = (await book("09:00", "10:00")).json()
    retry = {"Idempotency-Key": "retry-me"} | member["headers"]
    refused = await book("09:00", "10:00", headers=retry)
    await client.post(f"/organisations/{organisation['id']}/bookings/{taken['id']}/cancel")

    accepted = await book("09:00", "10:00", headers=retry)

    assert (refused.status_code, accepted.status_code) == (409, 201)


async def test_an_idempotency_key_has_at_most_255_characters(book: Book) -> None:
    response = await book("09:00", "10:00", headers={"Idempotency-Key": "k" * 256})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [error["header"] for error in response.json()["errors"]] == ["Idempotency-Key"]


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


async def test_upcoming_bookings_are_your_own_soonest_first_in_every_organisation(
    client: AsyncClient,
    organisation: Json,
    book: Book,
    add_member: Factory,
    create_organisation: Factory,
    create_location: Factory,
    create_resource: Factory,
    at: Callable[..., str],
) -> None:
    later = (await book("09:00", "10:00", days_ahead=7)).json()
    sooner = (await book("14:00", "15:00", days_ahead=3)).json()
    member = await add_member(organisation["id"], "member")
    await book("11:00", "12:00", headers=member["headers"])
    other = await create_organisation(slug="other")
    other_resource = await create_resource(other["id"], (await create_location(other["id"]))["id"])
    elsewhere = await client.post(
        f"/organisations/{other['id']}/resources/{other_resource['id']}/bookings",
        json={"start": at("09:00", days_ahead=5), "end": at("10:00", days_ahead=5)},
    )

    response = await client.get("/users/me/bookings")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"items": [sooner, elsewhere.json(), later], "next_cursor": None}


async def test_upcoming_bookings_keep_one_in_progress_but_drop_ended_and_cancelled(
    app: FastAPI, client: AsyncClient, organisation: Json, resource: Json, user: Json, book: Book
) -> None:
    now = datetime.now(UTC)
    await _write_booking(app, organisation, resource, user, now - timedelta(hours=2), now)
    in_progress = await _write_booking(
        app, organisation, resource, user, now, now + timedelta(hours=1)
    )
    cancelled = (await book("09:00", "10:00")).json()
    await client.post(f"/organisations/{organisation['id']}/bookings/{cancelled['id']}/cancel")

    response = await client.get("/users/me/bookings")

    assert [item["id"] for item in response.json()["items"]] == [str(in_progress.id)]


async def test_upcoming_bookings_leave_out_organisations_you_have_left(
    client: AsyncClient, organisation: Json, book: Book, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")
    await book("09:00", "10:00", headers=member["headers"])
    before = await client.get("/users/me/bookings", headers=member["headers"])

    await client.delete(f"/organisations/{organisation['id']}/members/{member['id']}")
    after = await client.get("/users/me/bookings", headers=member["headers"])

    assert len(before.json()["items"]) == 1
    assert after.json() == {"items": [], "next_cursor": None}


async def test_upcoming_bookings_page_by_start_then_id(
    client: AsyncClient,
    organisation: Json,
    location: Json,
    book: Book,
    create_resource: Factory,
    at: Callable[..., str],
) -> None:
    second_room = await create_resource(organisation["id"], location["id"], name="Room 2")
    first_room_booking = (await book("09:00", "10:00")).json()
    second_room_booking = await client.post(
        f"/organisations/{organisation['id']}/resources/{second_room['id']}/bookings",
        json={"start": at("09:00"), "end": at("10:00")},
    )
    last = (await book("11:00", "12:00")).json()
    same_start = sorted(
        [first_room_booking, second_room_booking.json()], key=lambda item: UUID(item["id"])
    )

    seen: list[Json] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": 1} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/users/me/bookings", params=params)).json()
        seen += page["items"]
        cursor = page["next_cursor"]

    assert seen == [*same_start, last]
    assert cursor is None


async def _write_booking(
    app: FastAPI,
    organisation: Json,
    resource: Json,
    user: Json,
    start: datetime,
    end: datetime,
    held_until: datetime | None = None,
) -> Booking:
    """Write a booking straight to the database, as the API refuses bookings that start in the
    past."""
    async with app.state.sessions() as session, session.begin():
        return await bookings.add(
            session,
            UUID(organisation["id"]),
            UUID(resource["id"]),
            UUID(user["id"]),
            Period(start, end),
            price=0,
            hold_expires_at=held_until,
        )
