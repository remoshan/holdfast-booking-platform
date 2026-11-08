"""Use cases for locations. Members may read them; admins and owners manage them."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.locations import Location, LocationDetails
from holdfast.domain.memberships import Role
from holdfast.domain.pagination import Page
from holdfast.repositories import locations
from holdfast.services.permissions import require_membership


async def create_location(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, details: LocationDetails
) -> Location:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        return await locations.add(session, organisation_id, details)


async def list_locations(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, after: UUID | None, limit: int
) -> Page[Location]:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id)
        return await locations.list_page(session, organisation_id, after, limit)


async def get_location(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, location_id: UUID
) -> Location:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id)
        return await locations.get(session, organisation_id, location_id)


async def update_location(
    session: AsyncSession,
    actor_id: UUID,
    organisation_id: UUID,
    location_id: UUID,
    details: LocationDetails,
) -> Location:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        return await locations.update(session, organisation_id, location_id, details)


async def delete_location(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, location_id: UUID
) -> None:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id, Role.ADMIN)
        await locations.remove(session, organisation_id, location_id)
