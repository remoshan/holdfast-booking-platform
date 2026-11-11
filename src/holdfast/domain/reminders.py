"""Reminder emails: when one is due and what it says."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from holdfast.domain.bookings import Booking
from holdfast.domain.locations import Location
from holdfast.domain.resources import Resource
from holdfast.domain.users import User

REMINDER_LEAD = timedelta(hours=1)
"""How long before a booking starts its reminder is sent."""


@dataclass(frozen=True, slots=True)
class Reminder:
    booking_id: UUID
    to: str
    subject: str
    body: str


def reminder_time(booking: Booking, now: datetime) -> datetime | None:
    """When to remind the booker, or None for a booking made too close to its start to need
    a reminder."""
    due = booking.period.start - REMINDER_LEAD
    return due if due > now else None


def reminder_for(
    booking: Booking, resource: Resource, location: Location, booker: User
) -> Reminder:
    """The reminder for a booking, with its times on the location's own clock."""
    start = booking.period.start.astimezone(location.timezone)
    end = booking.period.end.astimezone(location.timezone)
    return Reminder(
        booking_id=booking.id,
        to=booker.email,
        subject=f"Reminder: {resource.name} at {start:%H:%M}",
        body=(
            f"Your booking of {resource.name} starts at {start:%H:%M} on "
            f"{start:%A} {start.day} {start:%B} and ends at {end:%H:%M}, "
            f"{location.timezone.key} time.\n\n"
            f"{location.name}\n{location.address}\n"
        ),
    )
