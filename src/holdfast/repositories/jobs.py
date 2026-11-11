"""Persistence for the job queue."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.jobs import Job, JobKind, JobStatus
from holdfast.repositories.records import JobRecord


async def enqueue(
    session: AsyncSession, kind: JobKind, payload: Mapping[str, str], run_at: datetime
) -> None:
    """Add a job that is due from `run_at`. It exists only if the transaction commits, so no
    job is ever queued for a change that was rolled back."""
    session.add(JobRecord(kind=kind, payload=dict(payload), status=JobStatus.QUEUED, run_at=run_at))
    await session.flush()


async def claim_due(session: AsyncSession, now: datetime) -> Job | None:
    """Lock and return the job that has been due the longest, or None if no job is due.

    FOR UPDATE SKIP LOCKED makes a worker pass over a job that another worker has locked,
    instead of waiting for it, so each worker takes a different job. The lock lasts until the
    transaction ends: if the worker dies first, Postgres releases it and the job is due again.
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
    """Store what a failed attempt changed: when the job is due again, or that it is dead."""
    await session.execute(
        update(JobRecord)
        .where(JobRecord.id == job.id)
        .values(
            status=job.status, run_at=job.run_at, attempts=job.attempts, last_error=job.last_error
        )
    )


def _to_job(record: JobRecord) -> Job:
    return Job(
        id=record.id,
        kind=record.kind,
        payload=record.payload,
        status=record.status,
        run_at=record.run_at,
        attempts=record.attempts,
        last_error=record.last_error,
    )
