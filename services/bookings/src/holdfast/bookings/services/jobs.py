"""The booking service's background jobs: releasing a hold that has run out, and reminding a booker
of their booking a day before it starts.

Each job is queued in the transaction of the change that needs it, and the worker runs it later on
the messaging library's job queue, inside a transaction of its own. Its database work is therefore
never lost and never applied twice. Only a reminder can go out more than once: after the mail
server has accepted it, anything that stops the job committing, such as the worker dying, its
database connection dropping or the server's reply arriving too late, makes the next attempt send
it again.
"""

from datetime import datetime
from enum import StrEnum, auto
from functools import partial
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.bookings.core.mail import Mailer
from holdfast.bookings.domain.bookings import Booking
from holdfast.bookings.domain.reminders import compose_reminder, needs_reminder, reminder_time
from holdfast.bookings.repositories import bookings, locations, memberships, resources, users
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


def handlers(mailer: Mailer) -> dict[str, Handler]:
    """What the worker runs for each kind of job."""
    return {
        JobKind.EXPIRE_HOLD: _expire_hold,
        JobKind.SEND_REMINDER: partial(_send_reminder, mailer),
    }


async def _expire_hold(session: AsyncSession, payload: Payload, now: datetime) -> None:
    booking = await bookings.get(
        session, UUID(payload["organisation_id"]), UUID(payload["booking_id"]), for_update=True
    )
    await bookings.save_status(session, booking.expire(now))


async def _send_reminder(
    mailer: Mailer, session: AsyncSession, payload: Payload, now: datetime
) -> None:
    organisation_id = UUID(payload["organisation_id"])
    booking = await bookings.get(session, organisation_id, UUID(payload["booking_id"]))
    if not needs_reminder(booking, now):
        return
    # Someone who has left the organisation can no longer see or cancel the booking, and
    # must not be sent its current details.
    if await memberships.find(session, organisation_id, booking.user_id) is None:
        return
    resource = await resources.get(session, organisation_id, booking.resource_id)
    location = await locations.get(session, organisation_id, resource.location_id)
    booker = await users.get(session, booking.user_id)
    reminder = compose_reminder(booking, resource.name, location)
    await mailer.send(booker.email, reminder.subject, reminder.body, reminder.message_id)


def _payload(booking: Booking) -> Payload:
    return {"organisation_id": str(booking.organisation_id), "booking_id": str(booking.id)}
