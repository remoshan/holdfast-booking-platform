"""The events the booking service publishes when a booking changes, and what each one carries:
everything the services that receive it need, since none of them can read this service's database.

An event is written to the outbox in the transaction of the change it announces, so it is
published exactly when that change commits.
"""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.bookings.domain.bookings import Booking
from holdfast.bookings.repositories import locations, memberships, organisations, resources, users
from holdfast.messaging import outbox

BOOKING_CONFIRMED = "bookings.booking_confirmed"
BOOKING_CANCELLED = "bookings.booking_cancelled"
REMINDER_DUE = "bookings.reminder_due"


class BookingDetails(BaseModel):
    """What every booking event carries: the booking, who booked it, and where and when it is."""

    booking_id: UUID
    recipient: str
    """The booker's email address."""
    resource_name: str
    location_name: str
    address: str
    timezone: str
    """The location's IANA time zone, in which the booker expects to read the times."""
    start: AwareDatetime
    end: AwareDatetime


class BookingConfirmed(BookingDetails):
    price: int
    """What the booking cost, in minor units of the currency; 0 if it was free."""
    currency: str


class BookingCancelled(BookingDetails):
    pass


class ReminderDue(BookingDetails):
    pass


async def publish_confirmed(session: AsyncSession, booking: Booking) -> None:
    """Announce that a booking was confirmed: at once if it is free, once paid if not."""
    details = await _details(session, booking)
    if details is not None:
        organisation = await organisations.get(session, booking.organisation_id)
        confirmed = BookingConfirmed(
            **details.model_dump(), price=booking.price, currency=organisation.currency
        )
        await outbox.publish(session, BOOKING_CONFIRMED, confirmed)


async def publish_cancelled(session: AsyncSession, booking: Booking) -> None:
    details = await _details(session, booking)
    if details is not None:
        await outbox.publish(session, BOOKING_CANCELLED, BookingCancelled(**details.model_dump()))


async def publish_reminder_due(session: AsyncSession, booking: Booking) -> None:
    """Announce that the booker is due their reminder of the booking."""
    details = await _details(session, booking)
    if details is not None:
        await outbox.publish(session, REMINDER_DUE, ReminderDue(**details.model_dump()))


async def _details(session: AsyncSession, booking: Booking) -> BookingDetails | None:
    """The details every event about the booking carries, or None if its booker has left the
    organisation: they can no longer see the booking, and must not be sent its current details."""
    if await memberships.find(session, booking.organisation_id, booking.user_id) is None:
        return None
    resource = await resources.get(session, booking.organisation_id, booking.resource_id)
    location = await locations.get(session, booking.organisation_id, resource.location_id)
    booker = await users.get(session, booking.user_id)
    return BookingDetails(
        booking_id=booking.id,
        recipient=booker.email,
        resource_name=resource.name,
        location_name=location.name,
        address=location.address,
        timezone=location.timezone.key,
        start=booking.period.start,
        end=booking.period.end,
    )
