"""Background jobs: work the worker does later, retried with growing delays when it fails."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum, auto
from uuid import UUID

from holdfast.domain.bookings import Booking

MAX_ATTEMPTS = 5
FIRST_RETRY_DELAY = timedelta(seconds=30)
"""The delay doubles after every failure: 30 seconds, then 1, 2 and 4 minutes."""


class JobKind(StrEnum):
    EXPIRE_HOLD = auto()
    SEND_REMINDER = auto()


class JobStatus(StrEnum):
    QUEUED = auto()
    DEAD = auto()
    """Failed too often to keep retrying. It stays in the table for someone to look at."""


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    kind: JobKind
    payload: Mapping[str, str]
    status: JobStatus
    run_at: datetime
    """The job is due from this moment on."""
    attempts: int
    """How many times the job has failed so far."""
    last_error: str | None

    def failed(self, error: str, now: datetime) -> Job:
        """The job after one more failed attempt: due again after a delay that doubles each
        time, or dead once it has used up its attempts."""
        attempts = self.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            return replace(self, status=JobStatus.DEAD, attempts=attempts, last_error=error)
        delay = FIRST_RETRY_DELAY * 2 ** (attempts - 1)
        return replace(self, run_at=now + delay, attempts=attempts, last_error=error)

    @property
    def booking_key(self) -> tuple[UUID, UUID]:
        """The organisation and booking ids of the booking this job is about."""
        return UUID(self.payload["organisation_id"]), UUID(self.payload["booking_id"])


def booking_job_payload(booking: Booking) -> dict[str, str]:
    """What a job about a booking carries: enough to find the booking again."""
    return {"organisation_id": str(booking.organisation_id), "booking_id": str(booking.id)}
