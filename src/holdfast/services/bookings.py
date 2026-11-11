"""Use cases for bookings. Members work with their own bookings; admins and owners with all.
The worker expires the holds that nobody confirmed in time."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.bookings import Booking, check_booking_rules, hold_expiry, price_for
from holdfast.domain.errors import BookingConflictError, IdempotencyKeyReusedError, NotFoundError
from holdfast.domain.jobs import JobKind, booking_job_payload
from holdfast.domain.memberships import Membership
from holdfast.domain.pagination import Page
from holdfast.domain.periods import Period
from holdfast.domain.reminders import reminder_time
from holdfast.repositories import bookings, jobs, locations, resources
from holdfast.services.permissions import require_membership


async def book_resource(
    session: AsyncSession,
    actor_id: UUID,
    organisation_id: UUID,
    resource_id: UUID,
    period: Period,
    idempotency_key: str | None = None,
) -> Booking:
    """Book the resource for the caller: held until it is confirmed if it costs money,
    confirmed at once if it is free.

    A request that repeats an idempotency key gets the booking its first attempt made. The job
    that later expires the hold, or sends the reminder, is queued in the same transaction as
    the booking, so neither can exist without the other.
    """
    now = datetime.now(UTC)
    requested = (organisation_id, resource_id, period)
    async with session.begin():
        await require_membership(session, organisation_id, actor_id)
        original = await _booking_made_with(session, actor_id, idempotency_key, requested)
        if original is not None:
            return original
        resource = await resources.get(session, organisation_id, resource_id)
        location = await locations.get(session, organisation_id, resource.location_id)
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
            # When one request arrives twice at the same moment, the slower copy finds the slot
            # taken by the faster one, whose booking is the answer to both.
            original = await _booking_made_with(session, actor_id, idempotency_key, requested)
            if original is None:
                raise
            return original
        if booking.expires_at is None:
            await _schedule_reminder(session, booking, now)
        else:
            await jobs.enqueue(
                session, JobKind.EXPIRE_HOLD, booking_job_payload(booking), booking.expires_at
            )
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
        return _visible_to(membership, await bookings.get(session, organisation_id, booking_id))


async def confirm_booking(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, booking_id: UUID
) -> Booking:
    """Turn a hold into a confirmed booking, and queue its reminder."""
    now = datetime.now(UTC)
    async with session.begin():
        membership = await require_membership(session, organisation_id, actor_id)
        booking = _visible_to(membership, await bookings.lock(session, organisation_id, booking_id))
        confirmed = booking.confirm(now)
        if confirmed != booking:
            await bookings.save_status(session, confirmed)
            await _schedule_reminder(session, confirmed, now)
        return confirmed


async def cancel_booking(
    session: AsyncSession, actor_id: UUID, organisation_id: UUID, booking_id: UUID
) -> Booking:
    async with session.begin():
        membership = await require_membership(session, organisation_id, actor_id)
        booking = _visible_to(membership, await bookings.lock(session, organisation_id, booking_id))
        cancelled = booking.cancel(now=datetime.now(UTC))
        await bookings.save_status(session, cancelled)
        return cancelled


async def expire_hold(
    session: AsyncSession, organisation_id: UUID, booking_id: UUID, now: datetime
) -> None:
    """Expire a hold that was not confirmed in time, which frees its slot. A booking that was
    confirmed or cancelled meanwhile stays as it is. Runs inside the transaction of the job
    that calls it."""
    booking = await bookings.lock(session, organisation_id, booking_id)
    await bookings.save_status(session, booking.expire(now))


async def _booking_made_with(
    session: AsyncSession,
    user_id: UUID,
    idempotency_key: str | None,
    requested: tuple[UUID, UUID, Period],
) -> Booking | None:
    """The booking that an earlier request with this idempotency key made, if there is one."""
    if idempotency_key is None:
        return None
    original = await bookings.find_by_idempotency_key(session, user_id, idempotency_key)
    if original is None:
        return None
    if (original.organisation_id, original.resource_id, original.period) != requested:
        raise IdempotencyKeyReusedError(
            "This idempotency key was already used for a different booking."
        )
    return original


async def _schedule_reminder(session: AsyncSession, booking: Booking, now: datetime) -> None:
    due = reminder_time(booking, now)
    if due is not None:
        await jobs.enqueue(session, JobKind.SEND_REMINDER, booking_job_payload(booking), due)


def _visible_to(membership: Membership, booking: Booking) -> Booking:
    """The booking, if this member may see it. Someone else's booking is reported as missing to a
    plain member, exactly like a booking that does not exist."""
    if not membership.sees_all_bookings and booking.user_id != membership.user_id:
        raise NotFoundError("Booking", booking.id)
    return booking
