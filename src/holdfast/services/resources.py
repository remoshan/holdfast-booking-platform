"""Use cases for resources and their availability."""

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.availability import Availability, bookable_periods
from holdfast.domain.pagination import Page
from holdfast.domain.resources import Resource, ResourceDetails
from holdfast.repositories import bookings, locations, resources


async def create_resource(
    session: AsyncSession, organisation_id: UUID, location_id: UUID, details: ResourceDetails
) -> Resource:
    async with session.begin():
        await locations.get(session, organisation_id, location_id)
        return await resources.add(session, organisation_id, location_id, details)


async def list_resources(
    session: AsyncSession, organisation_id: UUID, location_id: UUID, after: UUID | None, limit: int
) -> Page[Resource]:
    async with session.begin():
        await locations.get(session, organisation_id, location_id)
        return await resources.list_page(session, organisation_id, location_id, after, limit)


async def get_resource(session: AsyncSession, organisation_id: UUID, resource_id: UUID) -> Resource:
    async with session.begin():
        return await resources.get(session, organisation_id, resource_id)


async def update_resource(
    session: AsyncSession, organisation_id: UUID, resource_id: UUID, details: ResourceDetails
) -> Resource:
    async with session.begin():
        return await resources.update(session, organisation_id, resource_id, details)


async def delete_resource(session: AsyncSession, organisation_id: UUID, resource_id: UUID) -> None:
    async with session.begin():
        await resources.remove(session, organisation_id, resource_id)


async def get_availability(
    session: AsyncSession, organisation_id: UUID, resource_id: UUID, day: date
) -> Availability:
    async with session.begin():
        resource = await resources.get(session, organisation_id, resource_id)
        location = await locations.get(session, organisation_id, resource.location_id)
        opening_period = location.opening_period(day)
        booked = (
            []
            if opening_period is None
            else await bookings.active_periods(session, resource.id, opening_period)
        )
    free = bookable_periods(location, day, booked, now=datetime.now(UTC))
    return Availability(resource_id=resource.id, timezone=location.timezone, free=free)
