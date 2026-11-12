"""Reminders: the email a booker gets a day before a confirmed booking starts."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from holdfast.domain.bookings import Booking, BookingStatus
from holdfast.domain.locations import Location

REMINDER_LEAD = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Reminder:
    subject: str
    body: str
    message_id: str
    """The same for every attempt to send it. A worker that dies after the mail server accepted
    the email sends it again, and mail clients that drop repeated message ids show it once."""


def reminder_time(booking: Booking, now: datetime) -> datetime | None:
    """When to remind the booker of a booking that was just confirmed: a day before it starts,
    or never (None) if it starts sooner than that."""
    remind_at = booking.period.start - REMINDER_LEAD
    return remind_at if remind_at > now else None


def needs_reminder(booking: Booking, now: datetime) -> bool:
    """A booking still needs its reminder while it is confirmed and has not started."""
    return booking.status is BookingStatus.CONFIRMED and now < booking.period.start


def compose_reminder(booking: Booking, resource_name: str, location: Location) -> Reminder:
    """The reminder for a booking, with its times on the location's clock."""
    start = booking.period.start.astimezone(location.timezone)
    end = booking.period.end.astimezone(location.timezone)
    body = (
        "Hello,\n\n"
        "This is a reminder of your booking:\n\n"
        f"  {resource_name}\n"
        f"  {location.name}, {location.address}\n"
        f"  {start:%A %d %B %Y}, {start:%H:%M} to {end:%H:%M} ({location.timezone.key} time)\n\n"
        "If you no longer need it, please cancel it so that someone else can book it.\n\n"
        "Holdfast\n"
    )
    return Reminder(
        subject=f"Reminder: {resource_name}, {start:%a %d %b} at {start:%H:%M}",
        body=body,
        message_id=f"<reminder.{booking.id}@holdfast>",
    )
