"""The payments service's worker: takes the booking service's commands to charge and refund
bookings, carries them out through the provider, and publishes each outcome, until stopped.

Run it with `python -m holdfast.payments.worker`. Any number of workers can run side by side.
"""

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from holdfast.messaging import worker as messaging_worker
from holdfast.messaging.worker import Messaging
from holdfast.payments.config import WorkerSettings
from holdfast.payments.handlers import PROVIDER_TIMEOUT, handlers
from holdfast.payments.provider import Provider

QUEUE = "payments"
"""The service's queue on RabbitMQ, which keeps the commands while the service is down."""


async def run_worker(settings: WorkerSettings, stopping: asyncio.Event) -> None:
    # The provider keeps its books apart from the jobs' transactions, on an engine of its own.
    provider_engine = create_async_engine(str(settings.database_url))
    provider = Provider(
        async_sessionmaker(provider_engine),
        delay=timedelta(seconds=settings.provider_delay_seconds),
        failure_rate=settings.provider_failure_rate,
    )
    try:
        await messaging_worker.run_worker(
            database_url=str(settings.database_url),
            jobs={},
            # Every job waits on the provider at most this long.
            longest_job=PROVIDER_TIMEOUT,
            daily=[],
            messaging=Messaging(
                str(settings.rabbitmq_url),
                queue=QUEUE,
                messages=handlers(provider),
                publishes=True,
            ),
            stopping=stopping,
        )
    finally:
        await provider_engine.dispose()


def main() -> None:
    messaging_worker.main(lambda stopping: run_worker(WorkerSettings(), stopping))


if __name__ == "__main__":
    main()
