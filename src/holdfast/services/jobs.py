"""Running background jobs: what the worker does with the queue."""

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.jobs import Job, JobKind, JobStatus
from holdfast.repositories import jobs

type Handler = Callable[[AsyncSession, Job, datetime], Awaitable[None]]

logger = logging.getLogger(__name__)


async def run_next_job(
    session: AsyncSession, handlers: Mapping[JobKind, Handler], now: datetime
) -> bool:
    """Run the job that has been due the longest. Return False if no job was due.

    Claiming the job, doing its work and removing it are one transaction. If the worker dies
    part-way, Postgres rolls all of it back and unlocks the job, so no job is lost and nothing
    in the database is done twice. Work that leaves the database, such as an email, repeats
    only if the worker dies after sending it and before the commit.
    """
    async with session.begin():
        job = await jobs.claim_due(session, now)
        if job is None:
            return False
        try:
            # A savepoint: if the handler fails, its changes are undone and the claim is kept.
            async with session.begin_nested():
                await handlers[job.kind](session, job, now)
        except Exception as error:
            failed = job.failed(f"{type(error).__name__}: {error}", now)
            await jobs.save_failure(session, failed)
            outcome = "is dead" if failed.status is JobStatus.DEAD else "will be retried"
            logger.warning(
                "Job %s (%s) failed on attempt %d and %s: %s",
                job.id,
                job.kind,
                failed.attempts,
                outcome,
                failed.last_error,
            )
        else:
            await jobs.remove(session, job.id)
        return True
