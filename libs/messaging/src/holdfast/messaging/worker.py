"""The worker every service runs, as many instances side by side as it likes: its job loop and its
daily housekeeping, and, for a service that talks to RabbitMQ, the relay that sends its messages
and the consumer that receives them.

Each loop carries on after an error, so the job loop never depends on RabbitMQ: while the broker
is away, a service keeps running its jobs, and its messages wait in its outbox and in its queue.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aio_pika
from aio_pika.abc import AbstractConnection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from holdfast.messaging.inbox import UNREADABLE, consume, forget_received, receive_again
from holdfast.messaging.jobs import Handler, run_next_job
from holdfast.messaging.outbox import PUBLISH_TIMEOUT, declare_exchange, relay

POLL_INTERVAL = timedelta(seconds=1)
"""How long an idle job loop or relay waits before it looks again."""
CONNECT_TIMEOUT = timedelta(seconds=10)
RECONNECT_DELAY = timedelta(seconds=5)
DAILY_INTERVAL = timedelta(days=1)

type DailyTask = Callable[[AsyncSession], Awaitable[None]]
"""A chore the worker runs when it starts and then once a day. It owns its transaction."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Messaging:
    """How a service's worker talks to RabbitMQ."""

    rabbitmq_url: str
    queue: str
    """The service's own queue, bound to the types of the messages it handles."""
    messages: Mapping[str, Handler]
    """A handler for each message type the service receives, run as a job when one arrives."""
    publishes: bool
    """Whether the service sends messages, and so needs a relay for its outbox."""


def idle_in_transaction_limit(longest_job: timedelta) -> timedelta:
    """How long Postgres lets one of the worker's sessions sit idle in a transaction.

    The worker holds a job's lock, or a page of the outbox, in an open transaction. If its machine
    vanishes without closing the connection, Postgres ends the session after this long, which
    frees them. The limit must outlast the slowest unit of work, so it is five times that."""
    return 5 * max(longest_job, PUBLISH_TIMEOUT)


async def run_worker(
    *,
    database_url: str,
    jobs: Mapping[str, Handler],
    longest_job: timedelta,
    daily: Sequence[DailyTask],
    messaging: Messaging | None,
    stopping: asyncio.Event,
) -> None:
    """Run a service's worker until `stopping` is set, then let each loop finish what it holds:
    the job in hand commits, the delivery in hand is stored and acknowledged, and the relay
    finishes its page.

    `jobs` has a handler for each kind of the service's own jobs, and `longest_job` is how long
    the slowest of them, or of its message handlers, can take."""
    handlers = dict(jobs)
    chores = list(daily)
    if messaging is not None and messaging.messages:
        if clashes := handlers.keys() & messaging.messages.keys():
            raise ValueError(f"Both a job kind and a message type: {', '.join(sorted(clashes))}.")
        handlers |= messaging.messages
        handlers[UNREADABLE] = receive_again
        chores.insert(0, forget_received)
    limit = idle_in_transaction_limit(longest_job)
    engine = create_async_engine(
        database_url,
        connect_args={
            "server_settings": {
                "idle_in_transaction_session_timeout": f"{int(limit.total_seconds())}s"
            }
        },
    )
    sessions = async_sessionmaker(engine)

    async def relay_outbox(connection: AbstractConnection) -> None:
        channel = await connection.channel(on_return_raises=True)
        exchange = await declare_exchange(channel)
        # A closed channel ends the loop, so the caller connects again.
        while not stopping.is_set() and not channel.is_closed:
            if not await relay(sessions, exchange, stopping):
                await _sleep(POLL_INTERVAL, stopping)

    logger.info("The worker is running")
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(_run_jobs(sessions, handlers, stopping))
            group.create_task(_run_daily(sessions, chores, stopping))
            if messaging is not None and messaging.publishes:
                group.create_task(
                    _keep_connected("relay", messaging.rabbitmq_url, relay_outbox, stopping)
                )
            if messaging is not None and messaging.messages:
                queue, message_types = messaging.queue, list(messaging.messages)

                async def consume_queue(connection: AbstractConnection) -> None:
                    await consume(connection, sessions, queue, message_types, stopping)

                group.create_task(
                    _keep_connected("consumer", messaging.rabbitmq_url, consume_queue, stopping)
                )
    finally:
        await engine.dispose()
        logger.info("The worker has stopped")


def main(run: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    """Run a worker until SIGINT or SIGTERM, then let it finish what it holds and exit."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run_until_stopped(run))


async def _run_until_stopped(run: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Finish the work in hand, then exit. A handler is also what lets `docker stop` work at all:
    # as PID 1 in a container, a process ignores signals it has no handler for. Windows event
    # loops cannot add one; there Ctrl+C cancels the job, which rolls back and runs again later.
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(stop_signal, stopping.set)
    await run(stopping)


async def _run_jobs(
    sessions: async_sessionmaker[AsyncSession],
    handlers: Mapping[str, Handler],
    stopping: asyncio.Event,
) -> None:
    while not stopping.is_set():
        ran_a_job = False
        try:
            ran_a_job = await run_next_job(sessions, handlers, datetime.now(UTC))
        except Exception:
            # Most likely the database is unreachable. Jobs wait safely in their table.
            logger.exception("The job loop hit an error; trying again shortly")
        if not ran_a_job:
            await _sleep(POLL_INTERVAL, stopping)


async def _run_daily(
    sessions: async_sessionmaker[AsyncSession], chores: Sequence[DailyTask], stopping: asyncio.Event
) -> None:
    while not stopping.is_set():
        for chore in chores:
            try:
                async with sessions() as session:
                    await chore(session)
            except Exception:
                logger.exception("A daily chore failed; it runs again tomorrow")
        await _sleep(DAILY_INTERVAL, stopping)


async def _keep_connected(
    name: str,
    rabbitmq_url: str,
    work: Callable[[AbstractConnection], Awaitable[None]],
    stopping: asyncio.Event,
) -> None:
    """Do `work` on a connection to RabbitMQ, and connect again whenever it fails or the connection
    closes, until told to stop. Every new connection declares the exchange, and the queue, again,
    so nothing depends on what the broker kept."""
    while not stopping.is_set():
        try:
            async with await aio_pika.connect(
                rabbitmq_url, timeout=CONNECT_TIMEOUT.total_seconds()
            ) as connection:
                await work(connection)
        except asyncio.CancelledError:
            # aiormq gives up on a connection that sent nothing for three heartbeats, and fails
            # every call waiting on it with CancelledError. Only a cancellation of this task, as
            # on shutdown, may end the loop.
            if _being_cancelled():
                raise
            logger.warning("The %s's connection to RabbitMQ went silent; connecting again", name)
        except Exception:
            logger.exception("The %s lost RabbitMQ; connecting again shortly", name)
        else:
            if stopping.is_set():
                return
            logger.warning(
                "RabbitMQ ended the %s's connection or subscription; connecting again shortly", name
            )
        await _sleep(RECONNECT_DELAY, stopping)


def _being_cancelled() -> bool:
    """Whether this task itself is being cancelled, rather than handed a CancelledError for an
    operation that aiormq gave up on."""
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0


async def _sleep(interval: timedelta, stopping: asyncio.Event) -> None:
    """Wait for `interval`, or less if told to stop meanwhile."""
    with suppress(TimeoutError):
        async with asyncio.timeout(interval.total_seconds()):
            await stopping.wait()
