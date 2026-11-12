"""The background worker: runs due jobs, and ended sign-ins' cleanup once a day, until stopped.

Run it with `python -m holdfast.worker`. Any number of workers can run side by side.
"""

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from holdfast.core.config import WorkerSettings
from holdfast.core.mail import Mailer
from holdfast.services import auth
from holdfast.services.jobs import run_next_job

POLL_INTERVAL_SECONDS = 1
PURGE_INTERVAL = timedelta(days=1)
# A worker holds its job's lock in an open transaction. If its machine vanishes without closing
# the connection, Postgres ends the session after this long, which frees the job. It must
# outlast any job: SMTP_TIMEOUT_SECONDS bounds the slowest, a reminder.
IDLE_IN_TRANSACTION_TIMEOUT = "1min"

# Not __name__, which is "__main__" when the worker runs with `python -m`.
logger = logging.getLogger("holdfast.worker")


async def run_worker(settings: WorkerSettings, stopping: asyncio.Event) -> None:
    engine = create_async_engine(
        str(settings.database_url),
        connect_args={
            "server_settings": {"idle_in_transaction_session_timeout": IDLE_IN_TRANSACTION_TIMEOUT}
        },
    )
    sessions = async_sessionmaker(engine)
    mailer = Mailer(settings.smtp_host, settings.smtp_port, str(settings.mail_from))
    next_purge = datetime.now(UTC)
    logger.info("The worker is running")
    try:
        while not stopping.is_set():
            ran_a_job = False
            try:
                if datetime.now(UTC) >= next_purge:
                    # Scheduled first, so that a purge that fails waits for the next day.
                    next_purge = datetime.now(UTC) + PURGE_INTERVAL
                    async with sessions() as session:
                        await auth.purge_ended_sign_ins(session)
                ran_a_job = await run_next_job(sessions, mailer, datetime.now(UTC))
            except Exception:
                # Most likely the database is unreachable. Jobs wait safely in their table.
                logger.exception("The worker hit an error; trying again shortly")
            if not ran_a_job:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), POLL_INTERVAL_SECONDS)
    finally:
        await engine.dispose()
        logger.info("The worker has stopped")


async def _run_until_stopped() -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Finish the job in hand, then exit. A handler is also what lets `docker stop` work at all:
    # as PID 1 in a container, a process ignores signals it has no handler for. Windows event
    # loops cannot add one; there Ctrl+C cancels the job, which rolls back and runs again later.
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(stop_signal, stopping.set)
    await run_worker(WorkerSettings(), stopping)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run_until_stopped())


if __name__ == "__main__":
    main()
