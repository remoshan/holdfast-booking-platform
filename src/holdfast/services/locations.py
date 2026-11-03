"""Use cases for locations."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.locations import Location, LocationDetails
from holdfast.domain.pagination import Page
from holdfast.repositories import locations, organisations


async def create_location(
    session: AsyncSession, organisation_id: UUID, details: LocationDetails
) -> Location:
    async with session.begin():
        await organisations.get(session, organisation_id)
        return await locations.add(session, organisation_id, details)


async def list_locations(
    session: AsyncSession, organisation_id: UUID, after: UUID | None, limit: int
) -> Page[Location]:
    async with session.begin():
        await organisations.get(session, organisation_id)
        return await locations.list_page(session, organisation_id, after, limit)


async def get_location(session: AsyncSession, organisation_id: UUID, location_id: UUID) -> Location:
    async with session.begin():
        return await locations.get(session, organisation_id, location_id)


async def update_location(
    session: AsyncSession, organisation_id: UUID, location_id: UUID, details: LocationDetails
) -> Location:
    async with session.begin():
        return await locations.update(session, organisation_id, location_id, details)


async def delete_location(session: AsyncSession, organisation_id: UUID, location_id: UUID) -> None:
    async with session.begin():
        await locations.remove(session, organisation_id, location_id)
