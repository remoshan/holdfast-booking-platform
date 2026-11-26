"""Reminders: a booker is reminded of a confirmed booking a day before it starts."""

from datetime import datetime, timedelta

from holdfast.bookings.domain.bookings import Booking, BookingStatus

REMINDER_LEAD = timedelta(hours=24)


def reminder_time(booking: Booking, now: datetime) -> datetime | None:
    """When to remind the booker of a booking that was just confirmed: a day before it starts,
    or never (None) if it starts sooner than that."""
    remind_at = booking.period.start - REMINDER_LEAD
    return remind_at if remind_at > now else None


def needs_reminder(booking: Booking, now: datetime) -> bool:
    """A booking still needs its reminder while it is confirmed and has not started."""
    return booking.status is BookingStatus.CONFIRMED and now < booking.period.start
