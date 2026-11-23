"""Time primitives shared by locations, bookings and availability."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

GRID_MINUTES = 15
"""Every booking and every opening time starts and ends on this grid, in local time."""


def is_on_grid(moment: time | datetime) -> bool:
    return moment.minute % GRID_MINUTES == 0 and moment.second == 0 and moment.microsecond == 0


@dataclass(frozen=True, slots=True, order=True)
class Period:
    """A half-open range of time, [start, end), between two UTC instants.

    Python compares and subtracts two datetimes that share a ZoneInfo by wall-clock time, which
    is wrong across a daylight-saving change. Keeping every period in UTC rules that bug out.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.utcoffset() != timedelta(0) or self.end.utcoffset() != timedelta(0):
            raise ValueError("Period bounds must be UTC datetimes.")
        if self.start >= self.end:
            raise ValueError("A period must end after it starts.")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, other: Period) -> bool:
        return self.start <= other.start and other.end <= self.end
