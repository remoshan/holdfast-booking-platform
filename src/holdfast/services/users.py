"""Use cases for users."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.users import User
from holdfast.repositories import users


async def create_user(session: AsyncSession, email: str) -> User:
    async with session.begin():
        return await users.add(session, email)


async def get_user(session: AsyncSession, user_id: UUID) -> User:
    async with session.begin():
        return await users.get(session, user_id)
