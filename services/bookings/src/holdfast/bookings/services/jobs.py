"""The booking service's background jobs: releasing a hold that has run out, and having a booker
reminded of their booking a day before it starts.

Each job is queued in the transaction of the change that needs it, and the worker runs it later on
the messaging library's job queue, inside a transaction of its own. Its work, the event it
publishes included, is therefore never lost and never done twice.
"""

from datetime import datetime
from enum import StrEnum, auto
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.bookings.domain.bookings import Booking
from holdfast.bookings.domain.reminders import needs_reminder, reminder_time
from holdfast.bookings.repositories import bookings
from holdfast.bookings.services import events
from holdfast.messaging.jobs import Handler, Payload, enqueue


class JobKind(StrEnum):
    EXPIRE_HOLD = auto()
    SEND_REMINDER = auto()


async def schedule_hold_expiry(session: AsyncSession, booking: Booking) -> None:
    """Queue the job that releases a held booking when its hold runs out."""
    if booking.hold_expires_at is not None:
        await enqueue(session, JobKind.EXPIRE_HOLD, _payload(booking), booking.hold_expires_at)


async def schedule_reminder(session: AsyncSession, booking: Booking, now: datetime) -> None:
    """Queue the reminder for a booking that was just confirmed, if it starts late enough."""
    remind_at = reminder_time(booking, now)
    if remind_at is not None:
        await enqueue(session, JobKind.SEND_REMINDER, _payload(booking), remind_at)


async def _expire_hold(session: AsyncSession, payload: Payload, now: datetime) -> None:
    booking = await bookings.get(
        session, UUID(payload["organisation_id"]), UUID(payload["booking_id"]), for_update=True
    )
    await bookings.save_status(session, booking.expire(now))


async def _send_reminder(session: AsyncSession, payload: Payload, now: datetime) -> None:
    booking = await bookings.get(
        session, UUID(payload["organisation_id"]), UUID(payload["booking_id"])
    )
    if needs_reminder(booking, now):
        await events.publish_reminder_due(session, booking)


HANDLERS: dict[str, Handler] = {
    JobKind.EXPIRE_HOLD: _expire_hold,
    JobKind.SEND_REMINDER: _send_reminder,
}
"""What the worker runs for each kind of job."""


def _payload(booking: Booking) -> Payload:
    return {"organisation_id": str(booking.organisation_id), "booking_id": str(booking.id)}
