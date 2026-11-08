"""Use cases for organisations."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.memberships import Role
from holdfast.domain.organisations import Organisation, OrganisationDetails
from holdfast.domain.pagination import Page
from holdfast.repositories import memberships, organisations, users
from holdfast.services.permissions import require_membership


async def create_organisation(
    session: AsyncSession, actor_id: UUID, details: OrganisationDetails, currency: str
) -> Organisation:
    """Create an organisation owned by the user who creates it."""
    async with session.begin():
        creator = await users.get(session, actor_id)
        organisation = await organisations.add(session, details, currency)
        await memberships.add(session, organisation.id, creator, Role.OWNER)
        return organisation


async def list_organisations(
    session: AsyncSession, actor_id: UUID, after: UUID | None, limit: int
) -> Page[Organisation]:
    async with session.begin():
        return await organisations.list_page(session, actor_id, after, limit)


async def get_organisation(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID
) -> Organisation:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id)
        return await organisations.get(session, organisation_id)


async def update_organisation(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, details: OrganisationDetails
) -> Organisation:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id, Role.OWNER)
        return await organisations.update(session, organisation_id, details)


async def delete_organisation(session: AsyncSession, actor_id: UUID, organisation_id: UUID) -> None:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id, Role.OWNER)
        await organisations.remove(session, organisation_id)
