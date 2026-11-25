"""The job queue against a real Postgres: retries until a job dies, a savepoint that undoes only a
failing job's own writes, workers that pass over each other's jobs, a kind nobody handles, and the
replay that brings a dead job back."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.jobs import (
    FIRST_RETRY_DELAY,
    MAX_ATTEMPTS,
    Payload,
    claim_next,
    enqueue,
    run_next_job,
)
from holdfast.messaging.records import JobRecord, JobStatus
from tests.support import WAIT_SECONDS, StoredJobs

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]


async def test_a_failing_job_is_retried_later_until_its_last_attempt_leaves_it_dead(
    sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    async def fail(*_: Any) -> None:
        raise LookupError("Booking 42 was not found.")

    handlers = {"fail": fail}
    now = datetime.now(UTC)
    await _queue(sessions, "fail", now)

    await run_next_job(sessions, handlers, now)
    [first_failure] = await stored_jobs()
    for _ in range(MAX_ATTEMPTS - 1):
        [job] = await stored_jobs()
        await run_next_job(sessions, handlers, job.run_at)
    [dead] = await stored_jobs()

    assert (first_failure.status, first_failure.attempts) == (JobStatus.QUEUED, 1)
    assert first_failure.run_at == now + FIRST_RETRY_DELAY
    assert first_failure.last_error == "LookupError: Booking 42 was not found."
    assert (dead.status, dead.attempts) == (JobStatus.DEAD, MAX_ATTEMPTS)
    assert not await run_next_job(sessions, handlers, dead.run_at + timedelta(days=365))


async def test_a_job_that_fails_at_the_database_leaves_only_its_failure_behind(
    sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    """The savepoint undoes the job's own writes and keeps the transaction usable after the
    database refused a statement, so the failure can still be recorded."""

    async def write_then_fail(session: AsyncSession, payload: Payload, now: datetime) -> None:
        await enqueue(session, "written_by_the_failing_job", {}, now)
        await session.execute(text("SELECT 1 / 0"))

    now = datetime.now(UTC)
    await _queue(sessions, "write_then_fail", now)

    ran = await run_next_job(sessions, {"write_then_fail": write_then_fail}, now)

    [job] = await stored_jobs()
    assert ran
    assert (job.kind, job.attempts, job.run_at) == ("write_then_fail", 1, now + FIRST_RETRY_DELAY)
    assert "division by zero" in (job.last_error or "")


async def test_a_worker_passes_over_a_job_another_holds_and_runs_the_most_overdue_of_the_rest(
    sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    """Every job fails, so the one that ran records an attempt. Without SKIP LOCKED, the worker
    would wait for the job that the other one holds."""

    async def fail(*_: Any) -> None:
        raise LookupError("Gone.")

    now = datetime.now(UTC)
    for minutes_overdue in (1, 3, 2):
        await _queue(sessions, "fail", now - timedelta(minutes=minutes_overdue), minutes_overdue)

    async with sessions() as other_worker, other_worker.begin():
        held = await claim_next(other_worker, now)
        async with asyncio.timeout(WAIT_SECONDS):
            ran = await run_next_job(sessions, {"fail": fail}, now)

    attempts = {job.payload["minutes_overdue"]: job.attempts for job in await stored_jobs()}
    assert held is not None
    assert held.payload["minutes_overdue"] == 3
    assert ran
    assert attempts == {1: 0, 2: 1, 3: 0}


async def test_a_job_whose_kind_has_no_handler_fails_like_any_other(
    sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    """So a worker that has yet to learn a kind, during a deploy, leaves it to one that has."""
    now = datetime.now(UTC)
    await _queue(sessions, "learnt_later", now)

    ran = await run_next_job(sessions, {}, now)

    [job] = await stored_jobs()
    assert ran
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)
    assert job.last_error == "LookupError: No handler runs jobs of kind 'learnt_later'."


async def test_a_dead_job_requeued_as_the_readme_shows_runs_once_more(
    sessions: Sessions, stored_jobs: StoredJobs
) -> None:
    runs = 0

    async def count(*_: Any) -> None:
        nonlocal runs
        runs += 1

    now = datetime.now(UTC)
    await _queue(sessions, "fixed_since", now)
    async with sessions() as session, session.begin():
        await session.execute(update(JobRecord).values(status=JobStatus.DEAD, attempts=10))
        [dead] = (await session.scalars(text("SELECT id FROM jobs"))).all()
        # The replay statement from the README.
        await session.execute(
            text("UPDATE jobs SET status = 'queued', attempts = 0, run_at = now() WHERE id = :id"),
            {"id": dead},
        )

    while await run_next_job(sessions, {"fixed_since": count}, datetime.now(UTC)):
        pass

    assert runs == 1
    assert await stored_jobs() == []


async def _queue(sessions: Sessions, kind: str, run_at: datetime, minutes_overdue: int = 0) -> None:
    async with sessions() as session, session.begin():
        await enqueue(session, kind, {"minutes_overdue": minutes_overdue}, run_at)
