"""FastAPI dependencies shared by the routers."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions() as session:
        yield session


DatabaseSession = Annotated[AsyncSession, Depends(_session)]

CurrentUserId = Annotated[
    UUID,
    Header(
        alias="X-User-ID",
        description="The user making the request, until token authentication replaces it.",
    ),
]
