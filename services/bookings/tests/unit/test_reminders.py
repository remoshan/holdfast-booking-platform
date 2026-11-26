from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from holdfast.bookings.domain.bookings import Booking, BookingStatus
from holdfast.bookings.domain.periods import Period
from holdfast.bookings.domain.reminders import needs_reminder, reminder_time

type LocalPeriod = Callable[[date, str, str], Period]

TUESDAY = date(2026, 11, 3)


def _booking(period: Period, status: BookingStatus = BookingStatus.CONFIRMED) -> Booking:
    return Booking(
        id=uuid4(),
        organisation_id=uuid4(),
        resource_id=uuid4(),
        user_id=uuid4(),
        period=period,
        status=status,
        price=0,
        hold_expires_at=None,
        created_at=datetime(2026, 10, 30, tzinfo=UTC),
    )


def test_the_reminder_is_due_a_day_before_the_booking_starts(local_period: LocalPeriod) -> None:
    booking = _booking(local_period(TUESDAY, "09:00", "10:00"))

    assert reminder_time(booking, now=booking.period.start - timedelta(days=3)) == (
        booking.period.start - timedelta(hours=24)
    )


def test_a_booking_that_starts_within_a_day_gets_no_reminder(local_period: LocalPeriod) -> None:
    booking = _booking(local_period(TUESDAY, "09:00", "10:00"))

    assert reminder_time(booking, now=booking.period.start - timedelta(hours=23)) is None


@pytest.mark.parametrize(
    ("status", "hours_before_start", "expected"),
    [
        (BookingStatus.CONFIRMED, 24, True),
        (BookingStatus.CANCELLED, 24, False),
        (BookingStatus.CONFIRMED, 0, False),  # it has started
    ],
)
def test_only_a_confirmed_booking_that_has_not_started_needs_its_reminder(
    local_period: LocalPeriod, status: BookingStatus, hours_before_start: int, expected: bool
) -> None:
    booking = _booking(local_period(TUESDAY, "09:00", "10:00"), status)
    now = booking.period.start - timedelta(hours=hours_before_start)

    assert needs_reminder(booking, now) is expected
