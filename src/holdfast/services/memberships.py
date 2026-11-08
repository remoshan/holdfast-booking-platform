"""Use cases for an organisation's members."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import ConflictError
from holdfast.domain.memberships import Membership, Role
from holdfast.domain.pagination import Page
from holdfast.repositories import memberships, organisations, users
from holdfast.services.permissions import require_membership


async def list_members(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, after: UUID | None, limit: int
) -> Page[Membership]:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        return await memberships.list_page(session, organisation_id, after, limit)


async def get_member(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, user_id: UUID
) -> Membership:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        return await memberships.get(session, organisation_id, user_id)


async def add_member(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, email: str, role: Role
) -> Membership:
    async with session.begin():
        actor = await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        actor.require_can_manage(role)
        user = await users.get_by_email(session, email)
        return await memberships.add(session, organisation_id, user, role)


async def change_role(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, user_id: UUID, role: Role
) -> Membership:
    async with session.begin():
        await organisations.lock(session, organisation_id)
        actor = await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        member = await memberships.get(session, organisation_id, user_id)
        actor.require_can_manage(member.role)
        actor.require_can_manage(role)
        if member.role is Role.OWNER and role is not Role.OWNER:
            await _keep_an_owner(session, organisation_id)
        return await memberships.change_role(session, organisation_id, user_id, role)


async def remove_member(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, user_id: UUID
) -> None:
    async with session.begin():
        await organisations.lock(session, organisation_id)
        actor = await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        member = await memberships.get(session, organisation_id, user_id)
        actor.require_can_manage(member.role)
        if member.role is Role.OWNER:
            await _keep_an_owner(session, organisation_id)
        await memberships.remove(session, organisation_id, user_id)


async def _keep_an_owner(session: AsyncSession, organisation_id: UUID) -> None:
    """Refuse to remove or demote the last owner.

    Callers hold the organisation's lock, taken before reading any membership, so two owners
    who step down at the same moment cannot both pass this check.
    """
    if await memberships.count_owners(session, organisation_id) <= 1:
        raise ConflictError("An organisation must keep at least one owner.")
