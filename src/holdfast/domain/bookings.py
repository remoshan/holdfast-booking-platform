"""Bookings: the rules every new booking must satisfy, what a booking costs, and how a hold
becomes a confirmed booking or lapses."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum, auto
from uuid import UUID

from holdfast.domain.errors import ConflictError, InvalidBookingError
from holdfast.domain.locations import Location
from holdfast.domain.periods import GRID_MINUTES, Period, is_on_grid

BOOKING_HORIZON_DAYS = 30
"""Bookings can start at most this many days after today, in the location's calendar."""
HOLD_DURATION = timedelta(minutes=10)
"""How long a booking that costs money keeps its slot before it has to be confirmed."""


class BookingStatus(StrEnum):
    HELD = auto()
    CONFIRMED = auto()
    CANCELLED = auto()
    EXPIRED = auto()


INACTIVE_STATUSES = frozenset({BookingStatus.CANCELLED, BookingStatus.EXPIRED})
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
    expires_at: datetime | None
    """When a hold lapses unless it is confirmed first. None for a booking that was never held."""
    created_at: datetime

    def confirm(self, now: datetime) -> Booking:
        """Confirm a hold that has not lapsed. Confirming twice changes nothing."""
        if self.status is BookingStatus.CONFIRMED:
            return self
        if self.status is not BookingStatus.HELD or self._has_lapsed(now):
            raise ConflictError("Only a hold that has not expired can be confirmed.")
        return replace(self, status=BookingStatus.CONFIRMED)

    def cancel(self, now: datetime) -> Booking:
        """Cancel a booking that has not started yet. A booking that is already cancelled or
        expired is returned unchanged."""
        if self.status in INACTIVE_STATUSES:
            return self
        if self.period.start <= now:
            raise ConflictError("A booking that has already started cannot be cancelled.")
        return replace(self, status=BookingStatus.CANCELLED)

    def expire(self, now: datetime) -> Booking:
        """Expire a hold whose time has run out. Any other booking is returned unchanged."""
        if self.status is BookingStatus.HELD and self._has_lapsed(now):
            return replace(self, status=BookingStatus.EXPIRED)
        return self

    def _has_lapsed(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


def hold_expiry(price: int, now: datetime) -> datetime | None:
    """When a new booking's hold lapses. A booking that costs money is held until it is
    confirmed; a free one has nothing to wait for, so it needs no hold."""
    return now + HOLD_DURATION if price > 0 else None


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
