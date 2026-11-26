"""The notifications service's worker: receives the booking service's events and sends the email
for each one, until stopped.

Run it with `python -m holdfast.notifications.worker`. Any number of workers can run side by side.
"""

import asyncio
from datetime import timedelta

from holdfast.messaging import worker as messaging_worker
from holdfast.messaging.worker import Messaging
from holdfast.notifications.config import WorkerSettings
from holdfast.notifications.handlers import handlers
from holdfast.notifications.mail import SEND_DEADLINE_SECONDS, Mailer

QUEUE = "notifications"
"""The service's queue on RabbitMQ, which keeps the events while the service is down."""
LONGEST_JOB = timedelta(seconds=SEND_DEADLINE_SECONDS)
"""An email whose send is cut off at its deadline. The limit on how long Postgres lets one of the
worker's sessions sit idle in a transaction is derived from it."""


async def run_worker(settings: WorkerSettings, stopping: asyncio.Event) -> None:
    mailer = Mailer(settings.smtp_host, settings.smtp_port, settings.sender)
    await messaging_worker.run_worker(
        database_url=str(settings.database_url),
        jobs={},
        longest_job=LONGEST_JOB,
        daily=[],
        messaging=Messaging(
            str(settings.rabbitmq_url), queue=QUEUE, messages=handlers(mailer), publishes=False
        ),
        stopping=stopping,
    )


def main() -> None:
    messaging_worker.main(lambda stopping: run_worker(WorkerSettings(), stopping))


if __name__ == "__main__":
    main()
