"""The reminder the worker emails before a booking starts."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.bookings import BookingStatus
from holdfast.domain.reminders import Reminder, reminder_for
from holdfast.repositories import bookings, locations, resources, users

type SendReminder = Callable[[Reminder], Awaitable[None]]


async def send_reminder(
    session: AsyncSession, send: SendReminder, organisation_id: UUID, booking_id: UUID
) -> None:
    """Email the booker a reminder, unless the booking is no longer confirmed. Runs inside the
    transaction of the job that calls it."""
    booking = await bookings.get(session, organisation_id, booking_id)
    if booking.status is not BookingStatus.CONFIRMED:
        return
    resource = await resources.get(session, organisation_id, booking.resource_id)
    location = await locations.get(session, organisation_id, resource.location_id)
    booker = await users.get(session, booking.user_id)
    await send(reminder_for(booking, resource, location, booker))
