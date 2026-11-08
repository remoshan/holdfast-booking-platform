"""Use cases for accounts: registering, logging in, refreshing tokens and logging out."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.core.security import (
    ACCESS_TOKEN_LIFETIME,
    REFRESH_TOKEN_LIFETIME,
    hash_password,
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from holdfast.domain.errors import AuthenticationError
from holdfast.domain.users import User
from holdfast.repositories import refresh_tokens, users


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    access_token_lifetime: timedelta


async def register(session: AsyncSession, email: str, password: str) -> User:
    password_hash = await hash_password(password)
    async with session.begin():
        return await users.add(session, email, password_hash)


async def log_in(session: AsyncSession, email: str, password: str, secret: str) -> IssuedTokens:
    async with session.begin():
        credentials = await users.find_credentials(session, email)
    # Hashing takes a noticeable fraction of a second, so it runs without holding a connection.
    password_hash = credentials[1] if credentials else None
    if not await verify_password(password_hash, password) or credentials is None:
        raise AuthenticationError("The email or password is incorrect.")
    user, password_hash = credentials
    upgraded_hash = await hash_password(password) if password_needs_rehash(password_hash) else None
    async with session.begin():
        if upgraded_hash is not None:
            await users.update_password_hash(session, user.id, upgraded_hash)
        return await _issue_tokens(session, user.id, uuid7(), secret, datetime.now(UTC))


async def refresh(session: AsyncSession, refresh_token: str, secret: str) -> IssuedTokens:
    """Swap a refresh token for new tokens. Each refresh token works exactly once."""
    now = datetime.now(UTC)
    token_hash = hash_refresh_token(refresh_token)
    async with session.begin():
        family = await refresh_tokens.consume(session, token_hash, now)
        if family is not None:
            return await _issue_tokens(session, family.user_id, family.id, secret, now)
        # A spent token sent again means it was copied: end every token of that sign-in. The
        # revocation must commit, so the error is raised only after the transaction.
        await refresh_tokens.revoke_family_of(session, token_hash, now)
    raise AuthenticationError("The refresh token is invalid, expired or already used.")


async def log_out(session: AsyncSession, refresh_token: str) -> None:
    async with session.begin():
        await refresh_tokens.revoke_family_of(
            session, hash_refresh_token(refresh_token), datetime.now(UTC)
        )


async def _issue_tokens(
    session: AsyncSession, user_id: UUID, family_id: UUID, secret: str, now: datetime
) -> IssuedTokens:
    refresh_token = new_refresh_token()
    await refresh_tokens.add(
        session, user_id, family_id, hash_refresh_token(refresh_token), now + REFRESH_TOKEN_LIFETIME
    )
    return IssuedTokens(
        access_token=issue_access_token(user_id, secret, now),
        refresh_token=refresh_token,
        access_token_lifetime=ACCESS_TOKEN_LIFETIME,
    )
