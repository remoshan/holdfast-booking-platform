"""What the booking service's worker runs. Running it for real is tested with the integration
suite, which has a database."""

import asyncio
from typing import Any

import pytest

from holdfast.bookings import worker
from holdfast.bookings.core.config import WorkerSettings
from holdfast.bookings.services import auth, jobs
from holdfast.messaging import worker as messaging_worker


def test_the_worker_runs_the_jobs_publishes_the_outbox_and_purges_sign_ins_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passed: dict[str, Any] = {}

    async def run_worker(**arguments: Any) -> None:
        passed.update(arguments)

    monkeypatch.setattr(messaging_worker, "run_worker", run_worker)
    settings = WorkerSettings(
        database_url="postgresql+asyncpg://holdfast:holdfast@localhost/holdfast",
        rabbitmq_url="amqp://holdfast:holdfast@localhost:5672/",
    )

    asyncio.run(worker.run_worker(settings, asyncio.Event()))

    assert passed["jobs"] == jobs.HANDLERS
    assert passed["daily"] == [auth.purge_ended_sign_ins]
    assert (passed["messaging"].publishes, dict(passed["messaging"].messages)) == (True, {})
