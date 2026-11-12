"""Use cases for bookings. Members work with their own bookings; admins and owners with all."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.bookings import (
    Booking,
    BookingStatus,
    check_booking_rules,
    hold_expiry,
    price_for,
)
from holdfast.domain.errors import BookingConflictError, IdempotencyKeyReusedError, NotFoundError
from holdfast.domain.memberships import Membership
from holdfast.domain.pagination import Page
from holdfast.domain.periods import Period
from holdfast.repositories import bookings, locations, resources
from holdfast.services import jobs
from holdfast.services.permissions import require_membership


async def book_resource(
    session: AsyncSession,
    actor_id: UUID,
    organisation_id: UUID,
    resource_id: UUID,
    period: Period,
    idempotency_key: str | None,
) -> Booking:
    """Book the resource for the caller. A booking that costs something is held until it is
    confirmed, and released if its hold runs out first.

    A request that repeats an earlier request's idempotency key gets the booking the earlier
    one made, instead of booking again.
    """
    async with session.begin():
        await require_membership(session, organisation_id, actor_id)
        earlier = await _booking_made_earlier(
            session, actor_id, idempotency_key, organisation_id, resource_id, period
        )
        if earlier is not None:
            return earlier
        resource = await resources.get(session, organisation_id, resource_id)
        location = await locations.get(session, organisation_id, resource.location_id)
        now = datetime.now(UTC)
        check_booking_rules(period, location, now)
        price = price_for(period, resource.hourly_price)
        try:
            booking = await bookings.add(
                session,
                organisation_id,
                resource.id,
                actor_id,
                period,
                price,
                hold_expiry(price, now),
                idempotency_key,
            )
        except BookingConflictError:
            # The clash may be with a concurrent request that sent the same key and won.
            earlier = await _booking_made_earlier(
                session, actor_id, idempotency_key, organisation_id, resource_id, period
            )
            if earlier is None:
                raise
            return earlier
        if booking.status is BookingStatus.HELD:
            await jobs.schedule_hold_expiry(session, booking)
        else:
            await jobs.schedule_reminder(session, booking, now)
        return booking


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


async def confirm_booking(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, booking_id: UUID
) -> Booking:
    """Confirm a held booking before its hold runs out. Until payments exist, confirming stands
    in for paying."""
    async with session.begin():
        membership = await require_membership(session, organisation_id, actor_id)
        booking = await _visible_booking(session, membership, booking_id, for_update=True)
        now = datetime.now(UTC)
        confirmed = booking.confirm(now)
        if booking.status is BookingStatus.HELD:
            await bookings.save_status(session, confirmed)
            await jobs.schedule_reminder(session, confirmed, now)
        return confirmed


async def cancel_booking(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, booking_id: UUID
) -> Booking:
    async with session.begin():
        membership = await require_membership(session, organisation_id, actor_id)
        booking = await _visible_booking(session, membership, booking_id, for_update=True)
        cancelled = booking.cancel(now=datetime.now(UTC))
        await bookings.save_status(session, cancelled)
        return cancelled


async def _visible_booking(
    session: AsyncSession, membership: Membership, booking_id: UUID, *, for_update: bool = False
) -> Booking:
    """The booking, if this member may see it. Someone else's booking is reported as missing to a
    plain member, exactly like a booking that does not exist."""
    booking = await bookings.get(
        session, membership.organisation_id, booking_id, for_update=for_update
    )
    if not membership.sees_all_bookings and booking.user_id != membership.user_id:
        raise NotFoundError("Booking", booking_id)
    return booking


async def _booking_made_earlier(
    session: AsyncSession,
    actor_id: UUID,
    idempotency_key: str | None,
    organisation_id: UUID,
    resource_id: UUID,
    period: Period,
) -> Booking | None:
    """The booking that an earlier request with this idempotency key made, if any. A key may
    only be repeated with the same request."""
    if idempotency_key is None:
        return None
    earlier = await bookings.find_by_idempotency_key(session, actor_id, idempotency_key)
    if earlier is not None and (earlier.organisation_id, earlier.resource_id, earlier.period) != (
        organisation_id,
        resource_id,
        period,
    ):
        raise IdempotencyKeyReusedError(
            "This Idempotency-Key was already used for a different booking request."
        )
    return earlier
