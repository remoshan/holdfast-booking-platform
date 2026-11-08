from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient

from holdfast.core.security import issue_access_token

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]


async def test_get_current_user(client: AsyncClient, user: Json) -> None:
    response = await client.get("/users/me")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {key: user[key] for key in ("id", "email", "created_at")}


async def test_requests_without_a_token_are_unauthorised(anonymous_client: AsyncClient) -> None:
    response = await anonymous_client.get("/users/me")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"


def _forged_token(kind: str, user_id: UUID, secret: str) -> str:
    now = datetime.now(UTC)
    if kind == "expired":
        return issue_access_token(user_id, secret, now - timedelta(hours=1))
    if kind == "signed with another key":
        return issue_access_token(user_id, "another-key-that-is-32-characters", now)
    claims = {"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=1)}
    return jwt.encode(claims, key="", algorithm="none")


@pytest.mark.parametrize("kind", ["expired", "signed with another key", "unsigned"])
async def test_invalid_access_tokens_are_rejected(
    app: FastAPI, anonymous_client: AsyncClient, user: Json, kind: str
) -> None:
    token = _forged_token(kind, UUID(user["id"]), app.state.settings.jwt_secret.get_secret_value())

    response = await anonymous_client.get("/users/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"] == "The access token is invalid or has expired."
