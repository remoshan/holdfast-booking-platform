"""FastAPI dependencies shared by the routers."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.core.config import Settings
from holdfast.core.security import read_access_token
from holdfast.domain.errors import AuthenticationError

# auto_error=False lets a missing token become the API's own 401 problem, not FastAPI's error.
_bearer_token = HTTPBearer(auto_error=False, description="The access_token from POST /auth/login.")


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions() as session:
        yield session


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


DatabaseSession = Annotated[AsyncSession, Depends(_session)]
AppSettings = Annotated[Settings, Depends(_settings)]


def _current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_token)],
    settings: AppSettings,
) -> UUID:
    if credentials is None:
        raise AuthenticationError(
            "Send an access token in the header Authorization: Bearer <token>."
        )
    user_id = read_access_token(credentials.credentials, settings.jwt_secret.get_secret_value())
    if user_id is None:
        raise AuthenticationError("The access token is invalid or has expired.")
    return user_id


CurrentUserId = Annotated[UUID, Depends(_current_user_id)]
