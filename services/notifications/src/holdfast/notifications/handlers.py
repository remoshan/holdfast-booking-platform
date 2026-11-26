"""What the notifications service does with each message it receives: compose the email and send
it. Each handler runs as a job, so a send that fails is retried, and one that keeps failing ends up
dead, waiting to be replayed.

A message that arrives twice is received once, so a copy sends no second email. But anything that
stops the job committing after the mail server accepted the email, such as the worker dying, its
database connection dropping or the server's reply arriving too late, makes the next attempt send
it again, with the same Message-ID.
"""

from collections.abc import Callable
from datetime import datetime
from functools import partial

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.messaging.jobs import Handler, Payload
from holdfast.notifications import emails
from holdfast.notifications.events import (
    BOOKING_CANCELLED,
    BOOKING_CONFIRMED,
    PAYMENT_FAILED,
    REFUND_ISSUED,
    REMINDER_DUE,
    BookingCancelled,
    BookingConfirmed,
    PaymentFailed,
    RefundIssued,
    ReminderDue,
)
from holdfast.notifications.mail import Mailer


def handlers(mailer: Mailer) -> dict[str, Handler]:
    """What the worker runs for each type of message the service receives."""
    return {
        BOOKING_CONFIRMED: partial(_send, mailer, BookingConfirmed, emails.booking_confirmed),
        BOOKING_CANCELLED: partial(_send, mailer, BookingCancelled, emails.booking_cancelled),
        PAYMENT_FAILED: partial(_send, mailer, PaymentFailed, emails.payment_failed),
        REFUND_ISSUED: partial(_send, mailer, RefundIssued, emails.refund_issued),
        REMINDER_DUE: partial(_send_reminder, mailer),
    }


async def _send[Event: BaseModel](
    mailer: Mailer,
    event_type: type[Event],
    compose: Callable[[Event], emails.Email],
    session: AsyncSession,
    payload: Payload,
    now: datetime,
) -> None:
    email = compose(event_type.model_validate(payload["data"]))
    await mailer.send(email.recipient, email.subject, email.body, email.message_id)


async def _send_reminder(
    mailer: Mailer, session: AsyncSession, payload: Payload, now: datetime
) -> None:
    event = ReminderDue.model_validate(payload["data"])
    # One that arrives after the booking started, held up by an outage, is of no use any more.
    if now < event.start:
        email = emails.reminder(event)
        await mailer.send(email.recipient, email.subject, email.body, email.message_id)
