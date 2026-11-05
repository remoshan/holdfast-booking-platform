"""Use cases for bookings."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.bookings import Booking, check_booking_rules, price_for
from holdfast.domain.pagination import Page
from holdfast.domain.periods import Period
from holdfast.repositories import bookings, locations, resources, users


async def book_resource(
    session: AsyncSession, organisation_id: UUID, resource_id: UUID, user_id: UUID, period: Period
) -> Booking:
    async with session.begin():
        await users.get(session, user_id)
        resource = await resources.get(session, organisation_id, resource_id)
        location = await locations.get(session, organisation_id, resource.location_id)
        check_booking_rules(period, location, now=datetime.now(UTC))
        price = price_for(period, resource.hourly_price)
        return await bookings.add(session, organisation_id, resource.id, user_id, period, price)


async def list_bookings(
    session: AsyncSession, organisation_id: UUID, resource_id: UUID, after: UUID | None, limit: int
) -> Page[Booking]:
    async with session.begin():
        await resources.get(session, organisation_id, resource_id)
        return await bookings.list_page(session, organisation_id, resource_id, after, limit)


async def get_booking(session: AsyncSession, organisation_id: UUID, booking_id: UUID) -> Booking:
    async with session.begin():
        return await bookings.get(session, organisation_id, booking_id)


async def cancel_booking(session: AsyncSession, organisation_id: UUID, booking_id: UUID) -> Booking:
    async with session.begin():
        booking = await bookings.get(session, organisation_id, booking_id)
        cancelled = booking.cancel(now=datetime.now(UTC))
        await bookings.save_status(session, cancelled)
        return cancelled
