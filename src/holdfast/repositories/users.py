"""Persistence for users."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import ConflictError, NotFoundError
from holdfast.domain.users import User
from holdfast.repositories.integrity import UNIQUE_VIOLATION, violation_raises
from holdfast.repositories.records import UserRecord


async def add(session: AsyncSession, email: str) -> User:
    record = UserRecord(email=email)
    session.add(record)
    with violation_raises(UNIQUE_VIOLATION, ConflictError(f"The email {email} is already in use.")):
        await session.flush()
    return _to_user(record)


async def get(session: AsyncSession, user_id: UUID) -> User:
    record = await session.get(UserRecord, user_id)
    if record is None:
        raise NotFoundError("User", user_id)
    return _to_user(record)


def _to_user(record: UserRecord) -> User:
    return User(id=record.id, email=record.email, created_at=record.created_at)
