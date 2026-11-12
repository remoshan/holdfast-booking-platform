"""Use cases for bookings. Members work with their own bookings; admins and owners with all."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.bookings import Booking, check_booking_rules, price_for
from holdfast.domain.errors import NotFoundError
from holdfast.domain.memberships import Membership
from holdfast.domain.pagination import Page
from holdfast.domain.periods import Period
from holdfast.repositories import bookings, locations, resources
from holdfast.services.permissions import require_membership


async def book_resource(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, resource_id: UUID, period: Period
) -> Booking:
    async with session.begin():
        await require_membership(session, organisation_id, actor_id)
        resource = await resources.get(session, organisation_id, resource_id)
        location = await locations.get(session, organisation_id, resource.location_id)
        check_booking_rules(period, location, now=datetime.now(UTC))
        price = price_for(period, resource.hourly_price)
        return await bookings.add(session, organisation_id, resource.id, actor_id, period, price)


async def list_bookings(
    session: AsyncSession,
    actor_id: UUID,
    organisation_id: UUID,
    resource_id: UUID,
    after: UUID | None,
    limit: int,
) -> Page[Booking]:
    async with session.begin():
        membership = await require_membership(session, organisation_id, actor_id)
        await resources.get(session, organisation_id, resource_id)
        booked_by = None if membership.sees_all_bookings else actor_id
        return await bookings.list_page(
            session, organisation_id, resource_id, booked_by, after, limit
        )


async def list_upcoming_bookings(
    session: AsyncSession, actor_id: UUID, after: UUID | None, limit: int
) -> Page[Booking]:
    """The caller's own bookings that have not ended, across the organisations they belong to."""
    async with session.begin():
        return await bookings.list_upcoming(session, actor_id, datetime.now(UTC), after, limit)


async def get_booking(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, booking_id: UUID
) -> Booking:
    async with session.begin():
        membership = await require_membership(session, organisation_id, actor_id)
        return await _visible_booking(session, membership, booking_id)


async def cancel_booking(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, booking_id: UUID
) -> Booking:
    async with session.begin():
        membership = await require_membership(session, organisation_id, actor_id)
        booking = await _visible_booking(session, membership, booking_id)
        cancelled = booking.cancel(now=datetime.now(UTC))
        await bookings.save_status(session, cancelled)
        return cancelled


async def _visible_booking(
    session: AsyncSession, membership: Membership, booking_id: UUID
) -> Booking:
    """The booking, if this member may see it. Someone else's booking is reported as missing to a
    plain member, exactly like a booking that does not exist."""
    booking = await bookings.get(session, membership.organisation_id, booking_id)
    if not membership.sees_all_bookings and booking.user_id != membership.user_id:
        raise NotFoundError("Booking", booking_id)
    return booking
