from datetime import UTC, datetime, timedelta
from uuid import uuid4

from holdfast.domain.jobs import MAX_ATTEMPTS, Job, JobKind, JobStatus

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def _job(attempts: int = 0) -> Job:
    return Job(
        id=uuid4(),
        kind=JobKind.SEND_REMINDER,
        payload={},
        status=JobStatus.QUEUED,
        attempts=attempts,
        run_at=NOW,
        last_error=None,
    )


def test_a_failed_job_is_retried_after_a_delay_that_doubles_each_time() -> None:
    job = _job()
    delays = []
    for _ in range(MAX_ATTEMPTS - 1):
        job = job.fail("SMTPConnectError: the mail server is down", NOW)
        delays.append(job.run_at - NOW)

    assert delays == [timedelta(seconds=30) * 2**retry for retry in range(MAX_ATTEMPTS - 1)]
    assert (job.status, job.attempts) == (JobStatus.QUEUED, MAX_ATTEMPTS - 1)
    assert job.last_error == "SMTPConnectError: the mail server is down"


def test_a_job_that_fails_for_the_last_time_is_dead() -> None:
    job = _job(attempts=MAX_ATTEMPTS - 1).fail("NotFoundError: gone", NOW)

    assert (job.status, job.attempts, job.last_error) == (
        JobStatus.DEAD,
        MAX_ATTEMPTS,
        "NotFoundError: gone",
    )
