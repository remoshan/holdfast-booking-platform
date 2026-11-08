"""Persistence for memberships. Every query is scoped to one organisation."""

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import ConflictError, NotFoundError
from holdfast.domain.memberships import Membership, Role
from holdfast.domain.pagination import Page
from holdfast.domain.users import User
from holdfast.repositories.integrity import UNIQUE_VIOLATION, violation_raises
from holdfast.repositories.pagination import fetch_page
from holdfast.repositories.records import MembershipRecord


async def add(session: AsyncSession, organisation_id: UUID, user: User, role: Role) -> Membership:
    record = MembershipRecord(organisation_id=organisation_id, user_id=user.id, role=role)
    session.add(record)
    already_member = ConflictError(f"{user.email} is already a member of this organisation.")
    with violation_raises(UNIQUE_VIOLATION, already_member):
        await session.flush()
    return Membership(
        organisation_id=organisation_id,
        user_id=user.id,
        email=user.email,
        role=role,
        created_at=record.created_at,
    )


async def find(session: AsyncSession, organisation_id: UUID, user_id: UUID) -> Membership | None:
    record = await session.get(MembershipRecord, (organisation_id, user_id))
    return None if record is None else _to_membership(record)


async def get(session: AsyncSession, organisation_id: UUID, user_id: UUID) -> Membership:
    return _to_membership(await _get_record(session, organisation_id, user_id))


async def list_page(
    session: AsyncSession, organisation_id: UUID, after: UUID | None, limit: int
) -> Page[Membership]:
    query = select(MembershipRecord).where(MembershipRecord.organisation_id == organisation_id)
    return await fetch_page(session, query, MembershipRecord.user_id, after, limit, _to_membership)


async def change_role(
    session: AsyncSession, organisation_id: UUID, user_id: UUID, role: Role
) -> Membership:
    record = await _get_record(session, organisation_id, user_id)
    record.role = role
    await session.flush()
    return _to_membership(record)


async def remove(session: AsyncSession, organisation_id: UUID, user_id: UUID) -> None:
    removed_id = await session.scalar(
        delete(MembershipRecord)
        .where(
            MembershipRecord.organisation_id == organisation_id,
            MembershipRecord.user_id == user_id,
        )
        .returning(MembershipRecord.user_id)
    )
    if removed_id is None:
        raise NotFoundError("Member", user_id)


async def count_owners(session: AsyncSession, organisation_id: UUID) -> int:
    count = await session.execute(
        select(func.count()).where(
            MembershipRecord.organisation_id == organisation_id,
            MembershipRecord.role == Role.OWNER,
        )
    )
    return count.scalar_one()


async def _get_record(
    session: AsyncSession, organisation_id: UUID, user_id: UUID
) -> MembershipRecord:
    record = await session.get(MembershipRecord, (organisation_id, user_id))
    if record is None:
        raise NotFoundError("Member", user_id)
    return record


def _to_membership(record: MembershipRecord) -> Membership:
    return Membership(
        organisation_id=record.organisation_id,
        user_id=record.user_id,
        email=record.user.email,
        role=record.role,
        created_at=record.created_at,
    )
