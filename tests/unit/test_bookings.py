from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from holdfast.domain.bookings import Booking, BookingStatus, check_booking_rules, price_for
from holdfast.domain.errors import ConflictError, InvalidBookingError
from holdfast.domain.locations import Location
from holdfast.domain.periods import Period

type LocalPeriod = Callable[[date, str, str], Period]

MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
SUNDAY = date(2026, 11, 8)
NOW = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # Monday 08:00 in Amsterdam


def _booking(period: Period) -> Booking:
    return Booking(
        id=uuid4(),
        organisation_id=uuid4(),
        resource_id=uuid4(),
        user_id=uuid4(),
        period=period,
        status=BookingStatus.CONFIRMED,
        price=0,
        created_at=NOW,
    )


def test_a_booking_within_opening_hours_is_accepted(
    location: Location, local_period: LocalPeriod
) -> None:
    check_booking_rules(local_period(TUESDAY, "09:00", "10:30"), location, NOW)


def test_a_booking_on_the_last_day_of_the_horizon_is_accepted(
    location: Location, local_period: LocalPeriod
) -> None:
    check_booking_rules(local_period(MONDAY + timedelta(days=30), "09:00", "10:00"), location, NOW)


@pytest.mark.parametrize(
    ("day", "start", "end", "rule"),
    [
        (TUESDAY, "09:10", "10:00", "15-minute boundary"),
        (MONDAY - timedelta(days=7), "09:00", "10:00", "in the past"),
        (MONDAY + timedelta(days=31), "09:00", "10:00", "30 days"),
        (TUESDAY, "07:00", "09:00", "opening hours"),
        (TUESDAY, "17:00", "18:15", "opening hours"),
        (SUNDAY, "09:00", "10:00", "opening hours"),
    ],
)
def test_a_booking_that_breaks_a_rule_is_rejected(
    location: Location, local_period: LocalPeriod, day: date, start: str, end: str, rule: str
) -> None:
    with pytest.raises(InvalidBookingError, match=rule):
        check_booking_rules(local_period(day, start, end), location, NOW)


@pytest.mark.parametrize(
    ("start", "end", "hourly_price", "expected"),
    [
        ("09:00", "10:00", 2000, 2000),
        ("09:00", "09:15", 2000, 500),
        ("09:00", "09:15", 1002, 251),  # 250.5 rounds half up
        ("09:00", "09:15", 1001, 250),  # 250.25 rounds down
        ("09:00", "17:00", 0, 0),
    ],
)
def test_price_is_the_hourly_rate_for_the_booked_time(
    local_period: LocalPeriod, start: str, end: str, hourly_price: int, expected: int
) -> None:
    assert price_for(local_period(TUESDAY, start, end), hourly_price) == expected


def test_price_counts_the_hours_that_pass_across_a_daylight_saving_change(
    local_period: LocalPeriod,
) -> None:
    # Clocks go back from 03:00 to 02:00 on 25 October 2026, so 00:00 to 04:00 lasts five hours.
    assert price_for(local_period(date(2026, 10, 25), "00:00", "04:00"), 1000) == 5000


def test_cancelling_a_future_booking(local_period: LocalPeriod) -> None:
    booking = _booking(local_period(TUESDAY, "09:00", "10:00"))

    assert booking.cancel(NOW).status is BookingStatus.CANCELLED


def test_cancelling_twice_changes_nothing(local_period: LocalPeriod) -> None:
    cancelled = _booking(local_period(TUESDAY, "09:00", "10:00")).cancel(NOW)

    assert cancelled.cancel(NOW) == cancelled


def test_a_booking_that_has_started_cannot_be_cancelled(local_period: LocalPeriod) -> None:
    booking = _booking(local_period(MONDAY, "08:00", "09:00"))

    with pytest.raises(ConflictError, match="already started"):
        booking.cancel(NOW)
