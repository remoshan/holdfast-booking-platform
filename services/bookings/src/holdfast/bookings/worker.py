"""The booking service's worker: runs the service's jobs, publishes the events its changes leave in
the outbox, and cleans up ended sign-ins once a day, until stopped.

Run it with `python -m holdfast.bookings.worker`. Any number of workers can run side by side.
"""

import asyncio
from datetime import timedelta

from holdfast.bookings.core.config import WorkerSettings
from holdfast.bookings.services import auth, jobs
from holdfast.messaging import worker as messaging_worker
from holdfast.messaging.worker import Messaging


async def run_worker(settings: WorkerSettings, stopping: asyncio.Event) -> None:
    await messaging_worker.run_worker(
        database_url=str(settings.database_url),
        jobs=jobs.HANDLERS,
        # The jobs wait on nothing but the database.
        longest_job=timedelta(0),
        daily=[auth.purge_ended_sign_ins],
        # The service only publishes so far: it receives nothing, so its queue stays unused.
        messaging=Messaging(
            str(settings.rabbitmq_url), queue="bookings", messages={}, publishes=True
        ),
        stopping=stopping,
    )


def main() -> None:
    messaging_worker.main(lambda stopping: run_worker(WorkerSettings(), stopping))


if __name__ == "__main__":
    main()
