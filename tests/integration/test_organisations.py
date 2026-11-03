from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import status
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]


async def test_create_organisation(client: AsyncClient) -> None:
    response = await client.post(
        "/organisations", json={"name": "Acme Cowork", "slug": "acme", "currency": "EUR"}
    )

    assert response.status_code == status.HTTP_201_CREATED
    organisation = response.json()
    assert (organisation["name"], organisation["slug"], organisation["currency"]) == (
        "Acme Cowork",
        "acme",
        "EUR",
    )
    assert response.headers["Location"] == f"http://test/organisations/{organisation['id']}"


async def test_create_organisation_rejects_a_taken_slug(
    client: AsyncClient, organisation: Json
) -> None:
    response = await client.post(
        "/organisations", json={"name": "Copycat", "slug": organisation["slug"], "currency": "EUR"}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == "The slug acme is already taken."


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", " "), ("slug", "Not A Slug"), ("slug", "ab"), ("currency", "eur")],
)
async def test_create_organisation_validates_fields(
    client: AsyncClient, field: str, value: str
) -> None:
    body = {"name": "Acme Cowork", "slug": "acme", "currency": "EUR"} | {field: value}

    response = await client.post("/organisations", json=body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [error["pointer"] for error in response.json()["errors"]] == [f"#/{field}"]


async def test_list_organisations_in_pages(
    client: AsyncClient, create_organisation: Factory
) -> None:
    created = [await create_organisation(slug=f"acme-{number}") for number in range(3)]

    first_page = (await client.get("/organisations", params={"limit": 2})).json()
    cursor = first_page["next_cursor"]
    second_page = (await client.get("/organisations", params={"limit": 2, "cursor": cursor})).json()

    listed = [item["id"] for item in first_page["items"] + second_page["items"]]
    assert listed == sorted((item["id"] for item in created), key=UUID)
    assert second_page["next_cursor"] is None


async def test_list_organisations_rejects_an_invalid_cursor(client: AsyncClient) -> None:
    response = await client.get("/organisations", params={"cursor": "not-a-cursor"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["errors"] == [{"detail": "Invalid cursor.", "parameter": "cursor"}]


async def test_get_organisation(client: AsyncClient, organisation: Json) -> None:
    response = await client.get(f"/organisations/{organisation['id']}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == organisation


async def test_get_unknown_organisation_returns_404(client: AsyncClient) -> None:
    unknown_id = uuid4()

    response = await client.get(f"/organisations/{unknown_id}")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == f"Organisation {unknown_id} was not found."


async def test_update_organisation(client: AsyncClient, organisation: Json) -> None:
    response = await client.put(
        f"/organisations/{organisation['id']}", json={"name": "Acme Spaces", "slug": "acme-spaces"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == organisation | {"name": "Acme Spaces", "slug": "acme-spaces"}


async def test_update_organisation_rejects_a_taken_slug(
    client: AsyncClient, organisation: Json, create_organisation: Factory
) -> None:
    other = await create_organisation(slug="other")

    response = await client.put(
        f"/organisations/{other['id']}", json={"name": "Other", "slug": organisation["slug"]}
    )

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_update_unknown_organisation_returns_404(client: AsyncClient) -> None:
    response = await client.put(f"/organisations/{uuid4()}", json={"name": "Acme", "slug": "acme"})

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_delete_organisation(client: AsyncClient, organisation: Json) -> None:
    response = await client.delete(f"/organisations/{organisation['id']}")

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert (await client.get(f"/organisations/{organisation['id']}")).status_code == 404


async def test_delete_organisation_with_locations_is_rejected(
    client: AsyncClient, organisation: Json, location: Json
) -> None:
    response = await client.delete(f"/organisations/{organisation['id']}")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == f"Organisation {organisation['id']} still has locations."
