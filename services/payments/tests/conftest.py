"""Fixtures for the suites that need a server: a disposable Postgres migrated to head and emptied
after every test, the simulated provider on it, and a RabbitMQ broker."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from holdfast.messaging import inbox
from holdfast.messaging.envelope import Envelope
from holdfast.messaging.records import JobRecord, OutboxRecord
from holdfast.payments.provider import Provider
from holdfast.payments.records import Base, ChargeRecord, ProviderChargeRecord
from tests.support import Json

POSTGRES_IMAGE = "postgres:18-alpine"
RABBITMQ_IMAGE = "rabbitmq:4.4-management-alpine"
RABBITMQ_PORT = 5672
PYPROJECT_PATH = Path(__file__).parents[1] / "pyproject.toml"

type Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        command.upgrade(_alembic_config(url), "head")
        yield url


@pytest.fixture
async def alembic_config(database_url: str) -> AsyncIterator[Config]:
    """Runs migrations against the test database, through asyncio.to_thread: Alembic's env.py
    starts an event loop of its own. The schema is rebuilt from scratch afterwards."""
    config = _alembic_config(database_url)
    yield config
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.upgrade, config, "head")


def _alembic_config(database_url: str) -> Config:
    config = Config(toml_file=str(PYPROJECT_PATH))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[Sessions]:
    engine = create_async_engine(database_url)
    yield async_sessionmaker(engine)
    async with engine.begin() as connection:
        await connection.run_sync(_empty_all_tables)
    await engine.dispose()


def _empty_all_tables(connection: Connection) -> None:
    """Empty every table the records describe that exists: a migration test can leave the schema
    without them until its own cleanup rebuilds it."""
    existing = set(inspect(connection).get_table_names())
    tables = [table.name for table in Base.metadata.sorted_tables if table.name in existing]
    if tables:
        connection.execute(text(f"TRUNCATE {', '.join(tables)}"))


@pytest.fixture
async def provider(database_url: str) -> AsyncIterator[Provider]:
    """The simulated provider, on an engine of its own, as the worker builds it."""
    engine = create_async_engine(database_url)
    yield Provider(async_sessionmaker(engine))
    await engine.dispose()


@pytest.fixture
def receive(sessions: Sessions) -> Callable[[str, Json, datetime], Awaitable[None]]:
    """Store a command as the worker's consumer does when one arrives, each time as a new message,
    so that the inbox lets every copy through."""

    async def receive(message_type: str, data: Json, now: datetime) -> None:
        envelope = Envelope(id=uuid4(), type=message_type, time=now, data=data)
        async with sessions() as session, session.begin():
            await inbox.receive(session, envelope.model_dump_json().encode(), now)

    return receive


@pytest.fixture
def stored_jobs(sessions: Sessions) -> Callable[[], Awaitable[list[JobRecord]]]:
    """Every job in the table, dead ones included, the earliest due first."""

    async def stored_jobs() -> list[JobRecord]:
        async with sessions() as session:
            return list(await session.scalars(select(JobRecord).order_by(JobRecord.run_at)))

    return stored_jobs


@pytest.fixture
def published(sessions: Sessions) -> Callable[[], Awaitable[list[tuple[str, Json]]]]:
    """The replies waiting in the outbox, the oldest first, each as its type and data."""

    async def published() -> list[tuple[str, Json]]:
        async with sessions() as session:
            rows = await session.scalars(select(OutboxRecord).order_by(OutboxRecord.id))
            return [(row.type, row.data) for row in rows]

    return published


@pytest.fixture
def ledger(sessions: Sessions) -> Callable[[], Awaitable[list[ChargeRecord]]]:
    """Every outcome the service recorded, the earliest first."""

    async def ledger() -> list[ChargeRecord]:
        async with sessions() as session:
            return list(
                await session.scalars(select(ChargeRecord).order_by(ChargeRecord.created_at))
            )

    return ledger


@pytest.fixture
def provider_books(sessions: Sessions) -> Callable[[], Awaitable[list[ProviderChargeRecord]]]:
    """Every charge the provider made or declined, the earliest first."""

    async def provider_books() -> list[ProviderChargeRecord]:
        async with sessions() as session:
            return list(
                await session.scalars(
                    select(ProviderChargeRecord).order_by(ProviderChargeRecord.created_at)
                )
            )

    return provider_books


@pytest.fixture(scope="session")
def rabbitmq_url() -> Iterator[str]:
    container = (
        DockerContainer(RABBITMQ_IMAGE)
        .with_env("RABBITMQ_DEFAULT_USER", "holdfast")
        .with_env("RABBITMQ_DEFAULT_PASS", "holdfast")
        .with_exposed_ports(RABBITMQ_PORT)
        .waiting_for(LogMessageWaitStrategy("Server startup complete"))
    )
    with container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(RABBITMQ_PORT)
        yield f"amqp://holdfast:holdfast@{host}:{port}/"
