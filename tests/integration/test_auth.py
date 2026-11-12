from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient
from sqlalchemy import select, update

from holdfast.core.security import hash_refresh_token
from holdfast.repositories.records import RefreshTokenRecord
from holdfast.services import auth

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]

PASSWORD = "correct horse battery staple"


async def test_register(anonymous_client: AsyncClient) -> None:
    response = await anonymous_client.post(
        "/auth/register", json={"email": "Ada@Example.com", "password": PASSWORD}
    )

    assert response.status_code == status.HTTP_201_CREATED
    account = response.json()
    assert account["email"] == "ada@example.com"
    assert set(account) == {"id", "email", "created_at"}


async def test_register_rejects_an_email_already_in_use(
    anonymous_client: AsyncClient, user: Json
) -> None:
    response = await anonymous_client.post(
        "/auth/register", json={"email": "ADA@example.com", "password": PASSWORD}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == "The email ada@example.com is already in use."


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ({"email": "not-an-email", "password": PASSWORD}, "#/email"),
        ({"email": "ada@example.com", "password": "eleven-char"}, "#/password"),
        ({"email": "ada@example.com", "password": "x" * 129}, "#/password"),
    ],
)
async def test_register_validates_fields(
    anonymous_client: AsyncClient, body: Json, pointer: str
) -> None:
    response = await anonymous_client.post("/auth/register", json=body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [error["pointer"] for error in response.json()["errors"]] == [pointer]


async def test_log_in(anonymous_client: AsyncClient, user: Json) -> None:
    response = await anonymous_client.post(
        "/auth/login", json={"email": "ADA@example.com", "password": PASSWORD}
    )

    assert response.status_code == status.HTTP_200_OK
    tokens = response.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 15 * 60
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "credentials",
    [
        {"email": "ada@example.com", "password": "not the right password"},
        {"email": "nobody@example.com", "password": PASSWORD},
    ],
)
async def test_log_in_rejects_wrong_credentials_without_saying_which_part(
    anonymous_client: AsyncClient, user: Json, credentials: Json
) -> None:
    response = await anonymous_client.post("/auth/login", json=credentials)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"] == "The email or password is incorrect."


async def test_refresh_replaces_both_tokens(anonymous_client: AsyncClient, user: Json) -> None:
    response = await anonymous_client.post(
        "/auth/refresh", json={"refresh_token": user["refresh_token"]}
    )

    assert response.status_code == status.HTTP_200_OK
    tokens = response.json()
    assert tokens["refresh_token"] != user["refresh_token"]
    me = await anonymous_client.get(
        "/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.json()["id"] == user["id"]


async def test_reusing_a_refresh_token_ends_the_whole_sign_in(
    anonymous_client: AsyncClient, user: Json
) -> None:
    first = await anonymous_client.post(
        "/auth/refresh", json={"refresh_token": user["refresh_token"]}
    )
    newest_token = first.json()["refresh_token"]

    replayed = await anonymous_client.post(
        "/auth/refresh", json={"refresh_token": user["refresh_token"]}
    )
    after_replay = await anonymous_client.post(
        "/auth/refresh", json={"refresh_token": newest_token}
    )

    assert replayed.status_code == status.HTTP_401_UNAUTHORIZED
    assert after_replay.status_code == status.HTTP_401_UNAUTHORIZED


async def test_unknown_refresh_token_is_rejected(anonymous_client: AsyncClient) -> None:
    response = await anonymous_client.post("/auth/refresh", json={"refresh_token": "made-up"})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_log_out_ends_the_sign_in(anonymous_client: AsyncClient, user: Json) -> None:
    response = await anonymous_client.post(
        "/auth/logout", json={"refresh_token": user["refresh_token"]}
    )
    refreshed = await anonymous_client.post(
        "/auth/refresh", json={"refresh_token": user["refresh_token"]}
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert refreshed.status_code == status.HTTP_401_UNAUTHORIZED


async def test_logging_out_one_sign_in_keeps_the_others(
    anonymous_client: AsyncClient, user: Json
) -> None:
    second_sign_in = await anonymous_client.post(
        "/auth/login", json={"email": user["email"], "password": PASSWORD}
    )

    await anonymous_client.post("/auth/logout", json={"refresh_token": user["refresh_token"]})
    refreshed = await anonymous_client.post(
        "/auth/refresh", json={"refresh_token": second_sign_in.json()["refresh_token"]}
    )

    assert refreshed.status_code == status.HTTP_200_OK


async def test_the_purge_deletes_ended_sign_ins_and_keeps_live_ones_whole(
    app: FastAPI, anonymous_client: AsyncClient, user: Json
) -> None:
    credentials = {"email": user["email"], "password": PASSWORD}
    expired = (await anonymous_client.post("/auth/login", json=credentials)).json()
    logged_out = (await anonymous_client.post("/auth/login", json=credentials)).json()
    await anonymous_client.post("/auth/logout", json={"refresh_token": logged_out["refresh_token"]})
    used = user["refresh_token"]
    newest = (await anonymous_client.post("/auth/refresh", json={"refresh_token": used})).json()
    await _expire_refresh_tokens(app, expired["refresh_token"], used)

    async with app.state.sessions() as session:
        await auth.purge_ended_sign_ins(session)

    assert await _stored_token_hashes(app) == {
        hash_refresh_token(used),
        hash_refresh_token(newest["refresh_token"]),
    }
    # The used token still finds its sign-in, so sending it again still ends that sign-in.
    replayed = await anonymous_client.post("/auth/refresh", json={"refresh_token": used})
    after_replay = await anonymous_client.post(
        "/auth/refresh", json={"refresh_token": newest["refresh_token"]}
    )
    assert (replayed.status_code, after_replay.status_code) == (401, 401)


async def _expire_refresh_tokens(app: FastAPI, *tokens: str) -> None:
    async with app.state.sessions() as session, session.begin():
        await session.execute(
            update(RefreshTokenRecord)
            .where(RefreshTokenRecord.token_hash.in_(hash_refresh_token(token) for token in tokens))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def _stored_token_hashes(app: FastAPI) -> set[str]:
    async with app.state.sessions() as session:
        return set(await session.scalars(select(RefreshTokenRecord.token_hash)))
