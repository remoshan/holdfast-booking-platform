"""Locations: the sites of an organisation, each with its own time zone and opening hours."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum, auto
from uuid import UUID
from zoneinfo import ZoneInfo

from holdfast.domain.periods import GRID_MINUTES, Period, is_on_grid


class Weekday(StrEnum):
    MONDAY = auto()
    TUESDAY = auto()
    WEDNESDAY = auto()
    THURSDAY = auto()
    FRIDAY = auto()
    SATURDAY = auto()
    SUNDAY = auto()

    @classmethod
    def of(cls, day: date) -> Weekday:
        return list(cls)[day.weekday()]


@dataclass(frozen=True, slots=True)
class OpeningHours:
    """When a location opens and closes on one weekday, as local wall-clock times."""

    opens_at: time
    closes_at: time

    def __post_init__(self) -> None:
        if self.opens_at.tzinfo is not None or self.closes_at.tzinfo is not None:
            raise ValueError("Opening hours are local times and must not include a UTC offset.")
        if not (is_on_grid(self.opens_at) and is_on_grid(self.closes_at)):
            raise ValueError(f"Opening hours must fall on a {GRID_MINUTES}-minute boundary.")
        if self.opens_at >= self.closes_at:
            raise ValueError("closes_at must be later than opens_at.")


@dataclass(frozen=True, slots=True)
class LocationDetails:
    """The fields of a location that are set on creation and replaced on update."""

    name: str
    address: str
    timezone: ZoneInfo
    opening_hours: Mapping[Weekday, OpeningHours]
    """Weekdays missing from the mapping are closed."""


@dataclass(frozen=True, slots=True)
class Location:
    id: UUID
    organisation_id: UUID
    name: str
    address: str
    timezone: ZoneInfo
    opening_hours: Mapping[Weekday, OpeningHours]
    created_at: datetime

    def opening_period(self, day: date) -> Period | None:
        """When the location is open on a local calendar day, or None if it is closed."""
        hours = self.opening_hours.get(Weekday.of(day))
        if hours is None:
            return None
        return Period(
            datetime.combine(day, hours.opens_at, self.timezone).astimezone(UTC),
            datetime.combine(day, hours.closes_at, self.timezone).astimezone(UTC),
        )
