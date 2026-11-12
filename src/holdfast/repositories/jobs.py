"""Persistence for the job queue."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.jobs import Job, JobKind, JobStatus
from holdfast.repositories.records import JobRecord


async def enqueue(
    session: AsyncSession, kind: JobKind, payload: dict[str, str], run_at: datetime
) -> None:
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


async def remove(session: AsyncSession, job_id: UUID) -> None:
    await session.execute(delete(JobRecord).where(JobRecord.id == job_id))


async def save_failure(session: AsyncSession, job: Job) -> None:
    await session.execute(
        update(JobRecord)
        .where(JobRecord.id == job.id)
        .values(
            status=job.status, attempts=job.attempts, run_at=job.run_at, last_error=job.last_error
        )
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
