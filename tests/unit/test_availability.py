from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from holdfast.domain.availability import bookable_periods, free_periods
from holdfast.domain.locations import Location
from holdfast.domain.periods import Period

type LocalTime = Callable[[date, str], datetime]
type LocalPeriod = Callable[[date, str, str], Period]

MONDAY = date(2026, 11, 2)
SUNDAY = date(2026, 11, 8)


def test_free_periods_are_the_gaps_between_bookings(local_period: LocalPeriod) -> None:
    window = local_period(MONDAY, "08:00", "18:00")
    booked = [local_period(MONDAY, "12:00", "13:00"), local_period(MONDAY, "09:00", "10:00")]

    assert free_periods(window, booked) == [
        local_period(MONDAY, "08:00", "09:00"),
        local_period(MONDAY, "10:00", "12:00"),
        local_period(MONDAY, "13:00", "18:00"),
    ]


def test_overlapping_and_adjacent_bookings_leave_no_gap(local_period: LocalPeriod) -> None:
    window = local_period(MONDAY, "08:00", "18:00")
    booked = [
        local_period(MONDAY, "09:00", "11:00"),
        local_period(MONDAY, "10:00", "12:00"),
        local_period(MONDAY, "12:00", "13:00"),
    ]

    assert free_periods(window, booked) == [
        local_period(MONDAY, "08:00", "09:00"),
        local_period(MONDAY, "13:00", "18:00"),
    ]


def test_bookings_reaching_past_the_window_are_clipped(local_period: LocalPeriod) -> None:
    window = local_period(MONDAY, "08:00", "18:00")
    booked = [local_period(MONDAY, "07:00", "09:00"), local_period(MONDAY, "17:00", "19:00")]

    assert free_periods(window, booked) == [local_period(MONDAY, "09:00", "17:00")]


def test_a_fully_booked_window_has_no_free_periods(local_period: LocalPeriod) -> None:
    window = local_period(MONDAY, "08:00", "18:00")

    assert free_periods(window, [window]) == []


def test_today_is_bookable_from_the_next_grid_point(
    amsterdam_location: Location, local_time: LocalTime, local_period: LocalPeriod
) -> None:
    now = local_time(MONDAY, "09:07")

    assert bookable_periods(amsterdam_location, MONDAY, [], now) == [
        local_period(MONDAY, "09:15", "18:00")
    ]


def test_closed_days_and_days_outside_the_horizon_have_no_bookable_periods(
    amsterdam_location: Location, local_time: LocalTime
) -> None:
    now = local_time(MONDAY, "09:00")

    for day in (SUNDAY, MONDAY - timedelta(days=7), MONDAY + timedelta(days=31)):
        assert bookable_periods(amsterdam_location, day, [], now) == []


def test_opening_hours_follow_daylight_saving_time(amsterdam_location: Location) -> None:
    now = datetime(2026, 10, 19, tzinfo=UTC)

    # 08:00 in Amsterdam is 06:00 UTC in summer time and 07:00 UTC after the clocks go back.
    before = bookable_periods(amsterdam_location, date(2026, 10, 23), [], now)
    after = bookable_periods(amsterdam_location, date(2026, 10, 26), [], now)

    assert before[0].start == datetime(2026, 10, 23, 6, 0, tzinfo=UTC)
    assert after[0].start == datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
