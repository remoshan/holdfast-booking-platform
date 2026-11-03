"""Availability: the parts of a day that a resource can still be booked for."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from holdfast.domain.bookings import last_bookable_day
from holdfast.domain.locations import Location
from holdfast.domain.periods import GRID_MINUTES, Period


@dataclass(frozen=True, slots=True)
class Availability:
    resource_id: UUID
    timezone: ZoneInfo
    free: list[Period]


def bookable_periods(
    location: Location, day: date, booked: Iterable[Period], now: datetime
) -> list[Period]:
    """The free periods on a local calendar day that a new booking could still use."""
    today = now.astimezone(location.timezone).date()
    opening_period = location.opening_period(day)
    if opening_period is None or not today <= day <= last_bookable_day(location, now):
        return []
    earliest = _next_grid_point(now, location.timezone)
    if earliest >= opening_period.end:
        return []
    window = Period(max(opening_period.start, earliest), opening_period.end)
    return free_periods(window, booked)


def free_periods(window: Period, booked: Iterable[Period]) -> list[Period]:
    """The parts of the window that no booked period covers."""
    free: list[Period] = []
    start = window.start
    for period in sorted(booked):
        if period.start > start:
            free.append(Period(start, min(period.start, window.end)))
        start = max(start, period.end)
        if start >= window.end:
            return free
    free.append(Period(start, window.end))
    return free


def _next_grid_point(now: datetime, timezone: ZoneInfo) -> datetime:
    """The first grid boundary at or after now, measured on the local clock."""
    local = now.astimezone(timezone)
    floor = local.replace(
        minute=local.minute - local.minute % GRID_MINUTES, second=0, microsecond=0
    )
    floor_utc = floor.astimezone(UTC)
    return floor_utc if floor_utc == now else floor_utc + timedelta(minutes=GRID_MINUTES)
