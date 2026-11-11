"""Persistence for refresh tokens, which are stored only as hashes."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.users import RefreshTokenFamily
from holdfast.repositories.records import RefreshTokenRecord


async def add(
    session: AsyncSession, user_id: UUID, family_id: UUID, token_hash: str, expires_at: datetime
) -> None:
    session.add(
        RefreshTokenRecord(
            user_id=user_id, family_id=family_id, token_hash=token_hash, expires_at=expires_at
        )
    )
    await session.flush()


async def consume(
    session: AsyncSession, token_hash: str, now: datetime
) -> RefreshTokenFamily | None:
    """Mark a live token as used and return its family, or None if the token is unknown,
    already used, revoked or expired.

    The check and the change are one UPDATE, so two requests that send the same token at once
    cannot both succeed.
    """
    consumed = (
        await session.execute(
            update(RefreshTokenRecord)
            .where(
                RefreshTokenRecord.token_hash == token_hash,
                RefreshTokenRecord.used_at.is_(None),
                RefreshTokenRecord.revoked_at.is_(None),
                RefreshTokenRecord.expires_at > now,
            )
            .values(used_at=now)
            .returning(RefreshTokenRecord.family_id, RefreshTokenRecord.user_id)
        )
    ).one_or_none()
    return None if consumed is None else RefreshTokenFamily(consumed.family_id, consumed.user_id)


async def revoke_family_of(session: AsyncSession, token_hash: str, now: datetime) -> None:
    """Revoke every token in the given token's family. Unknown tokens change nothing."""
    family_id = (
        select(RefreshTokenRecord.family_id)
        .where(RefreshTokenRecord.token_hash == token_hash)
        .scalar_subquery()
    )
    await session.execute(
        update(RefreshTokenRecord)
        .where(RefreshTokenRecord.family_id == family_id, RefreshTokenRecord.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def remove_expired(session: AsyncSession, now: datetime) -> None:
    """Delete the tokens that have expired, which can never be used again."""
    await session.execute(delete(RefreshTokenRecord).where(RefreshTokenRecord.expires_at <= now))
