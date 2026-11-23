"""Use cases for users."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.bookings.domain.users import User
from holdfast.bookings.repositories import users


async def get_user(session: AsyncSession, user_id: UUID) -> User:
    async with session.begin():
        return await users.get(session, user_id)
