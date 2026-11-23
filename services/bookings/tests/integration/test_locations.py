from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]


async def test_create_location(
    client: AsyncClient, organisation: Json, location_request: Json
) -> None:
    body = location_request | {
        "opening_hours": {
            "tuesday": {"opens_at": "09:00", "closes_at": "17:30"},
            "monday": {"opens_at": "08:00", "closes_at": "18:00"},
        }
    }

    response = await client.post(f"/organisations/{organisation['id']}/locations", json=body)

    assert response.status_code == status.HTTP_201_CREATED
    location = response.json()
    assert location["organisation_id"] == organisation["id"]
    assert location["timezone"] == "Europe/Amsterdam"
    assert location["opening_hours"] == {
        "monday": {"opens_at": "08:00:00", "closes_at": "18:00:00"},
        "tuesday": {"opens_at": "09:00:00", "closes_at": "17:30:00"},
    }
    assert response.headers["Location"] == (
        f"http://test/organisations/{organisation['id']}/locations/{location['id']}"
    )


CLOSES_BEFORE_OPENING = {"monday": {"opens_at": "18:00", "closes_at": "08:00"}}
OFF_THE_GRID = {"monday": {"opens_at": "08:10", "closes_at": "18:00"}}
UNKNOWN_WEEKDAY = {"someday": {"opens_at": "08:00", "closes_at": "18:00"}}


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        ({"timezone": "Mars/Olympus_Mons"}, "#/timezone"),
        ({"opening_hours": CLOSES_BEFORE_OPENING}, "#/opening_hours/monday"),
        ({"opening_hours": OFF_THE_GRID}, "#/opening_hours/monday"),
        ({"opening_hours": UNKNOWN_WEEKDAY}, "#/opening_hours/someday/[key]"),
        ({"floor": 3}, "#/floor"),
    ],
)
async def test_create_location_validates_fields(
    client: AsyncClient, organisation: Json, location_request: Json, change: Json, pointer: str
) -> None:
    response = await client.post(
        f"/organisations/{organisation['id']}/locations", json=location_request | change
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [error["pointer"] for error in response.json()["errors"]] == [pointer]


async def test_create_location_in_unknown_organisation_returns_404(
    client: AsyncClient, location_request: Json
) -> None:
    response = await client.post(f"/organisations/{uuid4()}/locations", json=location_request)

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_list_locations(client: AsyncClient, organisation: Json, location: Json) -> None:
    response = await client.get(f"/organisations/{organisation['id']}/locations")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"items": [location], "next_cursor": None}


async def test_list_locations_of_unknown_organisation_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/organisations/{uuid4()}/locations")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_get_location(client: AsyncClient, organisation: Json, location: Json) -> None:
    response = await client.get(f"/organisations/{organisation['id']}/locations/{location['id']}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == location


async def test_get_location_of_another_organisation_returns_404(
    client: AsyncClient, location: Json, create_organisation: Factory
) -> None:
    other = await create_organisation(slug="other")

    response = await client.get(f"/organisations/{other['id']}/locations/{location['id']}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_update_location(
    client: AsyncClient, organisation: Json, location: Json, location_request: Json
) -> None:
    body = location_request | {"name": "Central Station", "opening_hours": {}}

    response = await client.put(
        f"/organisations/{organisation['id']}/locations/{location['id']}", json=body
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == location | {"name": "Central Station", "opening_hours": {}}


async def test_update_unknown_location_returns_404(
    client: AsyncClient, organisation: Json, location_request: Json
) -> None:
    response = await client.put(
        f"/organisations/{organisation['id']}/locations/{uuid4()}", json=location_request
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_delete_location(client: AsyncClient, organisation: Json, location: Json) -> None:
    path = f"/organisations/{organisation['id']}/locations/{location['id']}"

    response = await client.delete(path)

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert (await client.get(path)).status_code == status.HTTP_404_NOT_FOUND


async def test_delete_location_with_resources_is_rejected(
    client: AsyncClient, organisation: Json, location: Json, resource: Json
) -> None:
    response = await client.delete(
        f"/organisations/{organisation['id']}/locations/{location['id']}"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == f"Location {location['id']} still has resources."
