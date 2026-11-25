"""Fixtures for the library's suites: a disposable Postgres holding the library's tables, emptied
after every test; a RabbitMQ broker, with a fresh queue for every test; and links in front of either
that a test can cut or stall."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from uuid import uuid4

import aio_pika
import pytest
from sqlalchemy import make_url, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from holdfast.messaging import inbox, worker
from holdfast.messaging.records import Base, JobRecord, OutboxRecord
from tests.support import Count, FaultyLink, StoredJobs

POSTGRES_IMAGE = "postgres:18-alpine"
RABBITMQ_IMAGE = "rabbitmq:4.4-management-alpine"
RABBITMQ_PORT = 5672
EMPTY_ALL_TABLES = text(
    f"TRUNCATE {', '.join(table.name for table in Base.metadata.sorted_tables)}"
)


@pytest.fixture(autouse=True)
def quick_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's loops wait a second or more between tries, for production's sake. Tests wait
    for what the loops do, so they shorten the waits."""
    short = timedelta(seconds=0.05)
    monkeypatch.setattr(worker, "POLL_INTERVAL", short)
    monkeypatch.setattr(worker, "RECONNECT_DELAY", short)
    monkeypatch.setattr(inbox, "STORE_RETRY_INTERVAL", short)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        asyncio.run(_create_tables(url))
        yield url


async def _create_tables(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(EMPTY_ALL_TABLES)
    await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine)


@pytest.fixture
def stored_jobs(sessions: async_sessionmaker[AsyncSession]) -> StoredJobs:
    """Every job in the table, dead ones included, the earliest due first."""

    async def stored_jobs() -> list[JobRecord]:
        async with sessions() as session:
            return list(await session.scalars(select(JobRecord).order_by(JobRecord.run_at)))

    return stored_jobs


@pytest.fixture
def outbox_rows(sessions: async_sessionmaker[AsyncSession]) -> Count:
    """How many messages wait in the outbox."""

    async def outbox_rows() -> int:
        async with sessions() as session:
            return len((await session.scalars(select(OutboxRecord.id))).all())

    return outbox_rows


@pytest.fixture(scope="session")
def rabbitmq() -> Iterator[DockerContainer]:
    container = (
        DockerContainer(RABBITMQ_IMAGE)
        .with_env("RABBITMQ_DEFAULT_USER", "holdfast")
        .with_env("RABBITMQ_DEFAULT_PASS", "holdfast")
        .with_exposed_ports(RABBITMQ_PORT)
        .waiting_for(LogMessageWaitStrategy("Server startup complete"))
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def rabbitmq_url(rabbitmq: DockerContainer) -> str:
    host, port = rabbitmq.get_container_host_ip(), rabbitmq.get_exposed_port(RABBITMQ_PORT)
    return f"amqp://holdfast:holdfast@{host}:{port}/"


@pytest.fixture
async def queue(rabbitmq_url: str) -> AsyncIterator[str]:
    """A queue name of the test's own; the queue is deleted afterwards."""
    name = f"test-{uuid4()}"
    yield name
    async with await aio_pika.connect(rabbitmq_url) as connection:
        channel = await connection.channel()
        await channel.queue_delete(name)


@pytest.fixture
async def rabbitmq_link(rabbitmq: DockerContainer) -> AsyncIterator[FaultyLink]:
    host, port = rabbitmq.get_container_host_ip(), rabbitmq.get_exposed_port(RABBITMQ_PORT)
    async with FaultyLink(host, int(port)) as link:
        yield link


@pytest.fixture
def rabbitmq_url_through(rabbitmq_link: FaultyLink) -> str:
    """The broker's URL through its link. Missed heartbeats end a stalled connection in seconds."""
    return f"amqp://holdfast:holdfast@127.0.0.1:{rabbitmq_link.port}/?heartbeat=2"


@pytest.fixture
async def database_link(database_url: str) -> AsyncIterator[FaultyLink]:
    url = make_url(database_url)
    async with FaultyLink(url.host or "localhost", url.port or 5432) as link:
        yield link


@pytest.fixture
def database_url_through(database_url: str, database_link: FaultyLink) -> str:
    url = make_url(database_url).set(host="127.0.0.1", port=database_link.port)
    return url.render_as_string(hide_password=False)
