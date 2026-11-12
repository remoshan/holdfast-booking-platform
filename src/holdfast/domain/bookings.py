"""Bookings, the rules every new booking must satisfy, what a booking costs, and how a booking
that costs something is held until it is confirmed."""

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
"""How long a booking that costs something holds its slot while it waits to be confirmed."""


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
    hold_expires_at: datetime | None
    """When the hold on a booking that costs something runs out; None for a free booking."""
    created_at: datetime

    def confirm(self, now: datetime) -> Booking:
        """Confirm a held booking before its hold runs out. Confirming twice changes nothing."""
        if self.status is BookingStatus.CONFIRMED:
            return self
        if self.status is not BookingStatus.HELD:
            raise ConflictError(f"A booking that is {self.status} cannot be confirmed.")
        if self._hold_has_run_out(now):
            raise ConflictError("The hold on this booking has run out.")
        return replace(self, status=BookingStatus.CONFIRMED)

    def expire(self, now: datetime) -> Booking:
        """Release a held booking whose hold has run out. A booking that was confirmed or
        cancelled in the meantime stays as it is."""
        if self.status is not BookingStatus.HELD:
            return self
        if not self._hold_has_run_out(now):
            raise ConflictError("The hold on this booking has not run out yet.")
        return replace(self, status=BookingStatus.EXPIRED)

    def cancel(self, now: datetime) -> Booking:
        """Cancel a booking that has not started yet. Cancelling a booking that is already
        cancelled or expired changes nothing."""
        if self.status in INACTIVE_STATUSES:
            return self
        if self.period.start <= now:
            raise ConflictError("A booking that has already started cannot be cancelled.")
        return replace(self, status=BookingStatus.CANCELLED)

    def _hold_has_run_out(self, now: datetime) -> bool:
        return self.hold_expires_at is not None and self.hold_expires_at <= now


def hold_expiry(price: int, now: datetime) -> datetime | None:
    """When a new booking's hold runs out. A booking that costs something is held until it is
    confirmed; a free booking is confirmed at once and needs no hold (None)."""
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
