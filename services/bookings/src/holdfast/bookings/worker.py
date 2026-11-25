"""The booking service's worker: runs the service's jobs, and ended sign-ins' cleanup once a day,
until stopped.

Run it with `python -m holdfast.bookings.worker`. Any number of workers can run side by side.
"""

import asyncio
from datetime import timedelta

from holdfast.bookings.core.config import WorkerSettings
from holdfast.bookings.core.mail import SEND_DEADLINE_SECONDS, Mailer
from holdfast.bookings.services import auth, jobs
from holdfast.messaging import worker as messaging_worker

LONGEST_JOB = timedelta(seconds=SEND_DEADLINE_SECONDS)
"""A reminder whose send is cut off at its deadline. The limit on how long Postgres lets one of
the worker's sessions sit idle in a transaction is derived from it."""


async def run_worker(settings: WorkerSettings, stopping: asyncio.Event) -> None:
    mailer = Mailer(settings.smtp_host, settings.smtp_port, settings.sender)
    await messaging_worker.run_worker(
        database_url=str(settings.database_url),
        jobs=jobs.handlers(mailer),
        longest_job=LONGEST_JOB,
        daily=[auth.purge_ended_sign_ins],
        messaging=None,
        stopping=stopping,
    )


def main() -> None:
    messaging_worker.main(lambda stopping: run_worker(WorkerSettings(), stopping))


if __name__ == "__main__":
    main()
