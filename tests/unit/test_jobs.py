from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from holdfast.domain.jobs import MAX_ATTEMPTS, Job, JobKind, JobStatus

NOW = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)


def _job(attempts: int) -> Job:
    return Job(
        id=uuid4(),
        kind=JobKind.SEND_REMINDER,
        payload={},
        status=JobStatus.QUEUED,
        run_at=NOW,
        attempts=attempts,
        last_error=None,
    )


@pytest.mark.parametrize(
    ("failures_before", "delay"),
    [
        (0, timedelta(seconds=30)),
        (1, timedelta(minutes=1)),
        (2, timedelta(minutes=2)),
        (3, timedelta(minutes=4)),
    ],
)
def test_a_failed_job_is_due_again_after_a_delay_that_doubles(
    failures_before: int, delay: timedelta
) -> None:
    job = _job(failures_before).failed("The mail server is down.", NOW)

    assert job.status is JobStatus.QUEUED
    assert job.run_at == NOW + delay
    assert (job.attempts, job.last_error) == (failures_before + 1, "The mail server is down.")


def test_a_job_that_keeps_failing_ends_up_dead() -> None:
    job = _job(MAX_ATTEMPTS - 1).failed("The mail server is down.", NOW)

    assert job.status is JobStatus.DEAD
    assert (job.attempts, job.run_at) == (MAX_ATTEMPTS, NOW)
