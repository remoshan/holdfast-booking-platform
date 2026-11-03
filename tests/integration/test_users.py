from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]


async def test_create_user_lowercases_the_email(client: AsyncClient) -> None:
    response = await client.post("/users", json={"email": "Ada@Example.com"})

    assert response.status_code == status.HTTP_201_CREATED
    user = response.json()
    assert user["email"] == "ada@example.com"
    assert response.headers["Location"] == f"http://test/users/{user['id']}"


async def test_create_user_rejects_an_email_already_in_use(
    client: AsyncClient, create_user: Factory
) -> None:
    await create_user(email="ada@example.com")

    response = await client.post("/users", json={"email": "ADA@example.com"})

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == "The email ada@example.com is already in use."


async def test_create_user_rejects_an_invalid_email(client: AsyncClient) -> None:
    response = await client.post("/users", json={"email": "not-an-email"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["errors"][0]["pointer"] == "#/email"


async def test_get_user(client: AsyncClient, user: Json) -> None:
    response = await client.get(f"/users/{user['id']}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == user


async def test_get_unknown_user_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/users/{uuid4()}")

    assert response.status_code == status.HTTP_404_NOT_FOUND
