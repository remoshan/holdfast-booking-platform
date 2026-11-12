"""Bookings, the rules every new booking must satisfy, and what a booking costs."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum, auto
from uuid import UUID

from holdfast.domain.errors import ConflictError, InvalidBookingError
from holdfast.domain.locations import Location
from holdfast.domain.periods import GRID_MINUTES, Period, is_on_grid

BOOKING_HORIZON_DAYS = 30
"""Bookings can start at most this many days after today, in the location's calendar."""


class BookingStatus(StrEnum):
    CONFIRMED = auto()
    CANCELLED = auto()


INACTIVE_STATUSES = frozenset({BookingStatus.CANCELLED})
"""Statuses whose bookings no longer hold their time. Every other status keeps the slot taken,
so a status added later blocks overlapping bookings until it is deliberately listed here and in
the database's exclusion constraint."""


@dataclass(frozen=True, slots=True)
class Booking:
    id: UUID
    organisation_id: UUID
    resource_id: UUID
    user_id: UUID
    period: Period
    status: BookingStatus
    price: int
    """What the booking costs, in minor units of the organisation's currency."""
    created_at: datetime

    def cancel(self, now: datetime) -> Booking:
        """Cancel a booking that has not started yet. Cancelling twice changes nothing."""
        if self.status is BookingStatus.CANCELLED:
            return self
        if self.period.start <= now:
            raise ConflictError("A booking that has already started cannot be cancelled.")
        return replace(self, status=BookingStatus.CANCELLED)


def last_bookable_day(location: Location, now: datetime) -> date:
    return now.astimezone(location.timezone).date() + timedelta(days=BOOKING_HORIZON_DAYS)


def check_booking_rules(period: Period, location: Location, now: datetime) -> None:
    """Raise InvalidBookingError if a new booking for the period breaks a rule at the location."""
    local_start = period.start.astimezone(location.timezone)
    local_end = period.end.astimezone(location.timezone)
    if not (is_on_grid(local_start) and is_on_grid(local_end)):
        raise InvalidBookingError(
            f"Bookings must start and end on a {GRID_MINUTES}-minute boundary."
        )
    if period.start < now:
        raise InvalidBookingError("Bookings cannot start in the past.")
    if local_start.date() > last_bookable_day(location, now):
        raise InvalidBookingError(
            f"Bookings can start at most {BOOKING_HORIZON_DAYS} days from today."
        )
    opening_period = location.opening_period(local_start.date())
    if opening_period is None or not opening_period.contains(period):
        raise InvalidBookingError(
            "Bookings must fall within the location's opening hours on a single day."
        )


def price_for(period: Period, hourly_price: int) -> int:
    """The price of a period at an hourly rate, rounded half up to a whole minor unit.

    The duration is elapsed time, so a booking that spans a daylight-saving change is charged
    for the hours that actually pass, not the hours on the clock.
    """
    minutes = period.duration // timedelta(minutes=1)
    return (hourly_price * minutes + 30) // 60
