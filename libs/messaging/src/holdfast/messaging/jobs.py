"""The job queue: work that runs after the transaction that queued it commits, and is retried until
it succeeds or has failed MAX_ATTEMPTS times.

A job runs inside the transaction that claimed it, and its writes commit together with its removal
from the queue. However the worker stops, a job's database work is never lost and never applied
twice. Only its effects outside the database, such as an email or a call to a payment provider, can
happen again: if the worker stops after one and before the commit, the next attempt repeats it.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.messaging.records import JobRecord, JobStatus

MAX_ATTEMPTS = 10
FIRST_RETRY_DELAY = timedelta(seconds=30)
"""Each retry waits twice as long as the one before, so ten attempts span about four hours."""

type Payload = dict[str, Any]
type Handler = Callable[[AsyncSession, Payload, datetime], Awaitable[None]]
"""Runs a job of one kind inside the claiming transaction, given its payload and the time now."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Job:
    """A job waiting in the queue. A job that succeeds is deleted."""

    id: UUID
    kind: str
    payload: Payload
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


async def enqueue(session: AsyncSession, kind: str, payload: Payload, run_at: datetime) -> None:
    """Queue a job to run at `run_at`. It commits, or rolls back, with the caller's transaction,
    so a job exists exactly when the change that asked for it does."""
    session.add(
        JobRecord(kind=kind, payload=payload, status=JobStatus.QUEUED, attempts=0, run_at=run_at)
    )
    await session.flush()


async def claim_next(session: AsyncSession, now: datetime) -> Job | None:
    """Lock the most overdue job that is due by `now`, or return None if none is.

    SKIP LOCKED passes over jobs that other workers hold, so each job runs in one worker at a
    time and workers never wait for each other. The lock lasts until the caller's transaction
    ends; if the worker dies, Postgres ends the transaction and the job is due again at once.
    """
    record = await session.scalar(
        select(JobRecord)
        .where(JobRecord.status == JobStatus.QUEUED, JobRecord.run_at <= now)
        .order_by(JobRecord.run_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return None if record is None else _to_job(record)


async def run_next_job(
    sessions: async_sessionmaker[AsyncSession], handlers: Mapping[str, Handler], now: datetime
) -> bool:
    """Run the most overdue job that is due by `now`, if there is one. Return whether one ran.

    A job whose kind has no handler fails like any other job, so an instance that does know the
    kind, such as a newer one during a deploy, can still run it on a later attempt."""
    async with sessions() as session, session.begin():
        job = await claim_next(session, now)
        if job is None:
            return False
        try:
            # A failing job rolls back to this savepoint, undoing only its own writes, and its
            # failure is recorded in the transaction that still holds it.
            async with session.begin_nested():
                handler = handlers.get(job.kind)
                if handler is None:
                    raise LookupError(f"No handler runs jobs of kind {job.kind!r}.")
                await handler(session, job.payload, now)
        except Exception as error:
            failed = job.fail(f"{type(error).__name__}: {error}", now)
            await _save_failure(session, failed)
            _log_failure(failed, error)
        else:
            await session.execute(delete(JobRecord).where(JobRecord.id == job.id))
            logger.info("Job %s (%s) succeeded", job.id, job.kind)
    return True


async def _save_failure(session: AsyncSession, job: Job) -> None:
    await session.execute(
        update(JobRecord)
        .where(JobRecord.id == job.id)
        .values(
            status=job.status, attempts=job.attempts, run_at=job.run_at, last_error=job.last_error
        )
    )


def _log_failure(job: Job, error: Exception) -> None:
    if job.status is JobStatus.DEAD:
        logger.error(
            "Job %s (%s) failed %d times and is dead",
            job.id,
            job.kind,
            job.attempts,
            exc_info=error,
        )
    else:
        logger.warning(
            "Job %s (%s) failed; retrying at %s", job.id, job.kind, job.run_at, exc_info=error
        )


def _to_job(record: JobRecord) -> Job:
    return Job(
        id=record.id,
        kind=record.kind,
        payload=record.payload,
        status=record.status,
        attempts=record.attempts,
        run_at=record.run_at,
        last_error=record.last_error,
    )
