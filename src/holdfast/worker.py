"""The background worker: runs due jobs, one at a time, until it is told to stop.

python -m holdfast.worker
"""

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from holdfast.core.config import WorkerSettings
from holdfast.core.mail import Mailer
from holdfast.domain.jobs import Job, JobKind
from holdfast.domain.reminders import Reminder
from holdfast.services import auth, bookings, reminders
from holdfast.services.jobs import Handler, run_next_job
from holdfast.services.reminders import SendReminder

POLL_INTERVAL = timedelta(seconds=1)
"""How long the worker waits before looking again when no job was due."""
CLEAN_UP_INTERVAL = timedelta(hours=1)
"""How often the worker deletes expired refresh tokens."""

logger = logging.getLogger(__name__)


def job_handlers(send: SendReminder) -> dict[JobKind, Handler]:
    """What the worker does for each kind of job."""

    async def expire_hold(session: AsyncSession, job: Job, now: datetime) -> None:
        await bookings.expire_hold(session, *job.booking_key, now)

    async def send_reminder(session: AsyncSession, job: Job, now: datetime) -> None:
        await reminders.send_reminder(session, send, *job.booking_key)

    return {JobKind.EXPIRE_HOLD: expire_hold, JobKind.SEND_REMINDER: send_reminder}


async def work(settings: WorkerSettings, stop: asyncio.Event) -> None:
    """Run due jobs until `stop` is set, finishing the job in hand first."""
    mailer = Mailer(settings.smtp_host, settings.smtp_port, settings.mail_from)

    async def send(reminder: Reminder) -> None:
        reference = f"reminder-{reminder.booking_id}"
        await mailer.send(reminder.to, reminder.subject, reminder.body, reference)

    handlers = job_handlers(send)
    engine = create_async_engine(str(settings.database_url))
    sessions = async_sessionmaker(engine)
    next_clean_up = datetime.now(UTC)
    logger.info("The worker has started.")
    try:
        while not stop.is_set():
            try:
                async with sessions() as session:
                    if datetime.now(UTC) >= next_clean_up:
                        await auth.remove_expired_refresh_tokens(session)
                        next_clean_up = datetime.now(UTC) + CLEAN_UP_INTERVAL
                    ran = await run_next_job(session, handlers, datetime.now(UTC))
            except Exception:
                # Most likely the database cannot be reached. Jobs stay queued, so look again.
                logger.exception("The worker could not run a job.")
                ran = False
            if not ran:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), POLL_INTERVAL.total_seconds())
    finally:
        await engine.dispose()
        logger.info("The worker has stopped.")


async def _run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        # loop.add_signal_handler does not exist on Windows; signal.signal works everywhere.
        signal.signal(stop_signal, lambda *_: loop.call_soon_threadsafe(stop.set))
    await work(WorkerSettings(), stop)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(_run())
