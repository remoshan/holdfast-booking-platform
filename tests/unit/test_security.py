from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from holdfast.core.security import (
    hash_password,
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
    read_access_token,
    verify_password,
)

SECRET = "a-secret-used-only-by-the-unit-tests"
PASSWORD = "correct horse battery staple"


def test_an_access_token_names_its_user() -> None:
    user_id = uuid4()

    token = issue_access_token(user_id, SECRET, datetime.now(UTC))

    assert read_access_token(token, SECRET) == user_id


def test_an_access_token_expires_after_fifteen_minutes() -> None:
    token = issue_access_token(uuid4(), SECRET, datetime.now(UTC) - timedelta(minutes=16))

    assert read_access_token(token, SECRET) is None


def test_an_access_token_signed_with_another_key_is_rejected() -> None:
    token = issue_access_token(uuid4(), SECRET, datetime.now(UTC))

    assert read_access_token(token, "another-key-that-is-32-characters") is None


def test_malformed_tokens_are_rejected() -> None:
    assert read_access_token("not.a.token", SECRET) is None


def test_refresh_tokens_are_unpredictable_and_stored_only_as_hashes() -> None:
    token = new_refresh_token()

    assert token != new_refresh_token()
    assert hash_refresh_token(token) == hash_refresh_token(token)
    assert token not in hash_refresh_token(token)


@pytest.mark.anyio
async def test_a_password_matches_only_its_own_hash() -> None:
    password_hash = await hash_password(PASSWORD)

    assert await verify_password(password_hash, PASSWORD)
    assert not await verify_password(password_hash, PASSWORD.upper())


@pytest.mark.anyio
async def test_a_missing_hash_never_matches() -> None:
    assert not await verify_password(None, PASSWORD)
