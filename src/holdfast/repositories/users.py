"""Persistence for users."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import ConflictError, NotFoundError
from holdfast.domain.users import User
from holdfast.repositories.integrity import UNIQUE_VIOLATION, violation_raises
from holdfast.repositories.records import UserRecord


async def add(session: AsyncSession, email: str, password_hash: str) -> User:
    record = UserRecord(email=email, password_hash=password_hash)
    session.add(record)
    with violation_raises(UNIQUE_VIOLATION, ConflictError(f"The email {email} is already in use.")):
        await session.flush()
    return _to_user(record)


async def get(session: AsyncSession, user_id: UUID) -> User:
    record = await session.get(UserRecord, user_id)
    if record is None:
        raise NotFoundError("User", user_id)
    return _to_user(record)


async def get_by_email(session: AsyncSession, email: str) -> User:
    record = await session.scalar(select(UserRecord).where(UserRecord.email == email))
    if record is None:
        raise NotFoundError("User", email)
    return _to_user(record)


async def find_credentials(session: AsyncSession, email: str) -> tuple[User, str] | None:
    """The user with this email and their password hash, or None if no account uses it."""
    record = await session.scalar(select(UserRecord).where(UserRecord.email == email))
    return None if record is None else (_to_user(record), record.password_hash)


async def update_password_hash(session: AsyncSession, user_id: UUID, password_hash: str) -> None:
    await session.execute(
        update(UserRecord).where(UserRecord.id == user_id).values(password_hash=password_hash)
    )


def _to_user(record: UserRecord) -> User:
    return User(id=record.id, email=record.email, created_at=record.created_at)
