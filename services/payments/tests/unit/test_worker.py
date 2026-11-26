"""What the payments service's worker runs. Running it for real is tested with the integration
suite, which has a broker and a database."""

import asyncio
from datetime import timedelta
from functools import partial
from typing import Any

import pytest

from holdfast.messaging import worker as messaging_worker
from holdfast.messaging.worker import idle_in_transaction_limit
from holdfast.payments import worker
from holdfast.payments.config import WorkerSettings
from holdfast.payments.handlers import PROVIDER_TIMEOUT
from holdfast.payments.messages import CHARGE_BOOKING, REFUND_BOOKING


def test_the_worker_takes_both_commands_and_waits_on_the_provider_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passed: dict[str, Any] = {}

    async def run_worker(**arguments: Any) -> None:
        passed.update(arguments)

    monkeypatch.setattr(messaging_worker, "run_worker", run_worker)
    settings = WorkerSettings(
        database_url="postgresql+asyncpg://holdfast:holdfast@localhost/holdfast",
        rabbitmq_url="amqp://holdfast:holdfast@localhost:5672/",
        provider_delay_seconds=2,
        provider_failure_rate=0.25,
    )

    asyncio.run(worker.run_worker(settings, asyncio.Event()))

    messaging = passed["messaging"]
    charge = messaging.messages[CHARGE_BOOKING]
    assert isinstance(charge, partial)
    provider = charge.args[0]
    assert (messaging.queue, set(messaging.messages), messaging.publishes) == (
        "payments",
        {CHARGE_BOOKING, REFUND_BOOKING},
        True,
    )
    assert (provider.delay, provider.failure_rate) == (timedelta(seconds=2), 0.25)
    # Postgres must not end a session whose job is still waiting on the provider.
    assert passed["longest_job"] == PROVIDER_TIMEOUT
    assert idle_in_transaction_limit(passed["longest_job"]) > PROVIDER_TIMEOUT
