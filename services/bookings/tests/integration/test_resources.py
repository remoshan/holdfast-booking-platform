from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient, Response

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]


async def test_create_resource(
    client: AsyncClient, organisation: Json, location: Json, resource_request: Json
) -> None:
    response = await client.post(
        f"/organisations/{organisation['id']}/locations/{location['id']}/resources",
        json=resource_request,
    )

    assert response.status_code == status.HTTP_201_CREATED
    resource = response.json()
    assert resource["location_id"] == location["id"]
    assert resource["kind"] == "meeting_room"
    assert resource["hourly_price"] == 2000
    assert response.headers["Location"] == (
        f"http://test/organisations/{organisation['id']}/resources/{resource['id']}"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", ""), ("kind", "sofa"), ("capacity", 0), ("hourly_price", -1)],
)
async def test_create_resource_validates_fields(
    client: AsyncClient,
    organisation: Json,
    location: Json,
    resource_request: Json,
    field: str,
    value: object,
) -> None:
    response = await client.post(
        f"/organisations/{organisation['id']}/locations/{location['id']}/resources",
        json=resource_request | {field: value},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [error["pointer"] for error in response.json()["errors"]] == [f"#/{field}"]


async def test_create_resource_in_another_organisations_location_returns_404(
    client: AsyncClient, location: Json, resource_request: Json, create_organisation: Factory
) -> None:
    other = await create_organisation(slug="other")

    response = await client.post(
        f"/organisations/{other['id']}/locations/{location['id']}/resources",
        json=resource_request,
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_list_resources(
    client: AsyncClient, organisation: Json, location: Json, resource: Json
) -> None:
    response = await client.get(
        f"/organisations/{organisation['id']}/locations/{location['id']}/resources"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"items": [resource], "next_cursor": None}


async def test_list_resources_of_unknown_location_returns_404(
    client: AsyncClient, organisation: Json
) -> None:
    response = await client.get(
        f"/organisations/{organisation['id']}/locations/{uuid4()}/resources"
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_get_resource(client: AsyncClient, organisation: Json, resource: Json) -> None:
    response = await client.get(f"/organisations/{organisation['id']}/resources/{resource['id']}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == resource


async def test_get_resource_of_another_organisation_returns_404(
    client: AsyncClient, resource: Json, create_organisation: Factory
) -> None:
    other = await create_organisation(slug="other")

    response = await client.get(f"/organisations/{other['id']}/resources/{resource['id']}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_update_resource(
    client: AsyncClient, organisation: Json, resource: Json, resource_request: Json
) -> None:
    change = {"name": "Boardroom", "capacity": 12, "hourly_price": 0}

    response = await client.put(
        f"/organisations/{organisation['id']}/resources/{resource['id']}",
        json=resource_request | change,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == resource | change


async def test_update_resource_validates_fields(
    client: AsyncClient, organisation: Json, resource: Json, resource_request: Json
) -> None:
    response = await client.put(
        f"/organisations/{organisation['id']}/resources/{resource['id']}",
        json=resource_request | {"capacity": "plenty"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_delete_resource(client: AsyncClient, organisation: Json, resource: Json) -> None:
    path = f"/organisations/{organisation['id']}/resources/{resource['id']}"

    response = await client.delete(path)

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert (await client.get(path)).status_code == status.HTTP_404_NOT_FOUND


async def test_delete_booked_resource_is_rejected(
    client: AsyncClient,
    organisation: Json,
    resource: Json,
    book: Callable[..., Awaitable[Response]],
) -> None:
    await book("09:00", "10:00")

    response = await client.delete(
        f"/organisations/{organisation['id']}/resources/{resource['id']}"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == (
        f"Resource {resource['id']} has bookings and cannot be deleted."
    )
