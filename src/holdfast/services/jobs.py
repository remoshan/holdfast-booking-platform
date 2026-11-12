"""Background jobs: queued in the transaction of the change that needs them, run by the worker.

A job runs inside the transaction that claimed it, and its writes commit together with its
removal from the queue. However the worker stops, a job's work is never lost and never applied
twice; only an email can go out twice, if the worker dies between sending it and committing.
"""

import logging
from datetime import datetime
from typing import assert_never
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.core.mail import Mailer
from holdfast.domain.bookings import Booking
from holdfast.domain.jobs import Job, JobKind, JobStatus
from holdfast.domain.reminders import compose_reminder, needs_reminder, reminder_time
from holdfast.repositories import bookings, jobs, locations, resources, users

logger = logging.getLogger(__name__)


async def schedule_hold_expiry(session: AsyncSession, booking: Booking) -> None:
    """Queue the job that releases a held booking when its hold runs out."""
    if booking.hold_expires_at is not None:
        await jobs.enqueue(session, JobKind.EXPIRE_HOLD, _payload(booking), booking.hold_expires_at)


async def schedule_reminder(session: AsyncSession, booking: Booking, now: datetime) -> None:
    """Queue the reminder for a booking that was just confirmed, if it starts late enough."""
    remind_at = reminder_time(booking, now)
    if remind_at is not None:
        await jobs.enqueue(session, JobKind.SEND_REMINDER, _payload(booking), remind_at)


async def run_next_job(
    sessions: async_sessionmaker[AsyncSession], mailer: Mailer, now: datetime
) -> bool:
    """Run the most overdue job that is due by `now`, if there is one. Return whether one ran."""
    async with sessions() as session, session.begin():
        job = await jobs.claim_next(session, now)
        if job is None:
            return False
        try:
            # A failing job rolls back to this savepoint, undoing only its own writes, and its
            # failure is recorded in the transaction that still holds it.
            async with session.begin_nested():
                await _run(session, mailer, job, now)
        except Exception as error:
            failed = job.fail(f"{type(error).__name__}: {error}", now)
            await jobs.save_failure(session, failed)
            _log_failure(failed, error)
        else:
            await jobs.remove(session, job.id)
            logger.info("Job %s (%s) succeeded", job.id, job.kind)
    return True


async def _run(session: AsyncSession, mailer: Mailer, job: Job, now: datetime) -> None:
    organisation_id = UUID(job.payload["organisation_id"])
    booking_id = UUID(job.payload["booking_id"])
    match job.kind:
        case JobKind.EXPIRE_HOLD:
            await _expire_hold(session, organisation_id, booking_id, now)
        case JobKind.SEND_REMINDER:
            await _send_reminder(session, mailer, organisation_id, booking_id, now)
        case _:
            assert_never(job.kind)


async def _expire_hold(
    session: AsyncSession, organisation_id: UUID, booking_id: UUID, now: datetime
) -> None:
    booking = await bookings.get(session, organisation_id, booking_id, for_update=True)
    await bookings.save_status(session, booking.expire(now))


async def _send_reminder(
    session: AsyncSession, mailer: Mailer, organisation_id: UUID, booking_id: UUID, now: datetime
) -> None:
    booking = await bookings.get(session, organisation_id, booking_id)
    if not needs_reminder(booking, now):
        return
    resource = await resources.get(session, organisation_id, booking.resource_id)
    location = await locations.get(session, organisation_id, resource.location_id)
    booker = await users.get(session, booking.user_id)
    reminder = compose_reminder(booking, resource.name, location)
    await mailer.send(booker.email, reminder.subject, reminder.body, reminder.message_id)


def _payload(booking: Booking) -> dict[str, str]:
    return {"organisation_id": str(booking.organisation_id), "booking_id": str(booking.id)}


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
