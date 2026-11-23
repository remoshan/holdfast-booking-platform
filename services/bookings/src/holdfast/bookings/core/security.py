"""Password hashing and the tokens that prove who a request comes from."""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta
from functools import cache
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
_SIGNING_ALGORITHM = "HS256"

# Argon2id is deliberately slow to compute, so hashing runs in a worker thread rather than
# blocking the event loop that serves every other request.
_password_hasher = PasswordHasher()


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_password_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    """Check a password against its hash. Without a hash, take just as long and return False.

    Answering faster for emails that have no account would reveal which emails do.
    """
    try:
        await asyncio.to_thread(_password_hasher.verify, password_hash or _unused_hash(), password)
    except VerificationError:
        return False
    return password_hash is not None


def password_needs_rehash(password_hash: str) -> bool:
    """Whether the hash was made with weaker settings than today's and should be replaced."""
    return _password_hasher.check_needs_rehash(password_hash)


@cache
def _unused_hash() -> str:
    return _password_hasher.hash("a password that belongs to no account")


def issue_access_token(user_id: UUID, secret: str, now: datetime) -> str:
    claims = {"sub": str(user_id), "iat": now, "exp": now + ACCESS_TOKEN_LIFETIME}
    return jwt.encode(claims, secret, algorithm=_SIGNING_ALGORITHM)


def read_access_token(token: str, secret: str) -> UUID | None:
    """The user an access token was issued to, or None if it is forged, malformed or expired."""
    try:
        # Accepting only the algorithm we sign with stops a forged "alg: none" token.
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_SIGNING_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
        )
        return UUID(claims["sub"])
    except jwt.InvalidTokenError, ValueError:
        return None


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """Only this hash is stored, so a leaked database holds no usable tokens.

    A refresh token is 256 random bits and cannot be guessed, so unlike a password it needs
    no slow hash.
    """
    return hashlib.sha256(token.encode()).hexdigest()
