"""Jobs: work that the background worker runs later, and how it retries work that fails."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum, auto
from uuid import UUID

MAX_ATTEMPTS = 10
FIRST_RETRY_DELAY = timedelta(seconds=30)
"""Each retry waits twice as long as the one before, so ten attempts span about four hours."""


class JobKind(StrEnum):
    EXPIRE_HOLD = auto()
    SEND_REMINDER = auto()


class JobStatus(StrEnum):
    QUEUED = auto()
    DEAD = auto()
    """Failed MAX_ATTEMPTS times. The job is kept, with its last error, until someone requeues
    or deletes it."""


@dataclass(frozen=True, slots=True)
class Job:
    """A job waiting in the queue. A job that succeeds is deleted."""

    id: UUID
    kind: JobKind
    payload: dict[str, str]
    status: JobStatus
    attempts: int
    """How many times the job has failed."""
    run_at: datetime
    last_error: str | None

    def fail(self, error: str, now: datetime) -> Job:
        """Record a failed attempt: retry after a delay that doubles each time, or give up once
        the job has failed MAX_ATTEMPTS times."""
        attempts = self.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            return replace(self, attempts=attempts, status=JobStatus.DEAD, last_error=error)
        retry_at = now + FIRST_RETRY_DELAY * 2 ** (attempts - 1)
        return replace(self, attempts=attempts, run_at=retry_at, last_error=error)
