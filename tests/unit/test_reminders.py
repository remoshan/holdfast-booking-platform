from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from holdfast.domain.bookings import Booking
from holdfast.domain.locations import Location
from holdfast.domain.periods import Period
from holdfast.domain.reminders import reminder_for, reminder_time
from holdfast.domain.resources import Resource, ResourceKind
from holdfast.domain.users import User

type LocalPeriod = Callable[[date, str, str], Period]
type NewBooking = Callable[..., Booking]

MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
NOW = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # Monday 08:00 in Amsterdam


def test_a_reminder_is_due_an_hour_before_the_booking_starts(
    new_booking: NewBooking, local_period: LocalPeriod
) -> None:
    booking = new_booking(local_period(TUESDAY, "09:00", "10:00"))

    assert reminder_time(booking, NOW) == booking.period.start - timedelta(hours=1)


def test_a_booking_made_within_the_hour_gets_no_reminder(
    new_booking: NewBooking, local_period: LocalPeriod
) -> None:
    booking = new_booking(local_period(MONDAY, "09:00", "10:00"))

    assert reminder_time(booking, NOW) is None


def test_a_reminder_gives_times_on_the_locations_clock(
    new_booking: NewBooking, local_period: LocalPeriod, amsterdam_location: Location
) -> None:
    booking = new_booking(local_period(TUESDAY, "09:00", "10:30"))
    resource = Resource(
        id=booking.resource_id,
        organisation_id=booking.organisation_id,
        location_id=amsterdam_location.id,
        name="Room 1",
        kind=ResourceKind.MEETING_ROOM,
        capacity=6,
        hourly_price=0,
        created_at=NOW,
    )
    booker = User(id=booking.user_id, email="ada@example.com", created_at=NOW)

    reminder = reminder_for(booking, resource, amsterdam_location, booker)

    assert (reminder.booking_id, reminder.to) == (booking.id, "ada@example.com")
    assert reminder.subject == "Reminder: Room 1 at 09:00"
    assert reminder.body == (
        "Your booking of Room 1 starts at 09:00 on Tuesday 3 November and ends at 10:30, "
        "Europe/Amsterdam time.\n\nCentral\nDamrak 1, Amsterdam\n"
    )
