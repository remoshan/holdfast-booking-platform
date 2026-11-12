"""Unit test fixtures: a location in Amsterdam that is open 08:00 to 18:00 on weekdays."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from holdfast.domain.locations import Location, OpeningHours, Weekday
from holdfast.domain.periods import Period

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
WEEKEND = (Weekday.SATURDAY, Weekday.SUNDAY)


@pytest.fixture
def amsterdam_location() -> Location:
    return Location(
        id=uuid4(),
        organisation_id=uuid4(),
        name="Central",
        address="Damrak 1, Amsterdam",
        timezone=AMSTERDAM,
        opening_hours={
            day: OpeningHours(time(8), time(18)) for day in Weekday if day not in WEEKEND
        },
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def local_time() -> Callable[[date, str], datetime]:
    """The UTC instant of a wall-clock time in Amsterdam, e.g. local_time(day, "09:30")."""

    def local_time(day: date, clock_time: str) -> datetime:
        return datetime.combine(day, time.fromisoformat(clock_time), AMSTERDAM).astimezone(UTC)

    return local_time


@pytest.fixture
def local_period(local_time: Callable[[date, str], datetime]) -> Callable[[date, str, str], Period]:
    """A period between two Amsterdam wall-clock times on the same day."""

    def local_period(day: date, start: str, end: str) -> Period:
        return Period(local_time(day, start), local_time(day, end))

    return local_period
