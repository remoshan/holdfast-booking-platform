"""Fixtures for the suites that need a server: a disposable Postgres migrated to head and emptied
after every test, a RabbitMQ broker, and a mail catcher whose inbox starts empty."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import AsyncClient
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy, LogMessageWaitStrategy

from holdfast.messaging.records import JobRecord
from holdfast.notifications.mail import Mailer
from holdfast.notifications.records import Base

POSTGRES_IMAGE = "postgres:18-alpine"
RABBITMQ_IMAGE = "rabbitmq:4.4-management-alpine"
RABBITMQ_PORT = 5672
MAILPIT_IMAGE = "axllent/mailpit:v1.33"
# A display name with a comma must be quoted, or the From header splits into two addresses.
MAIL_FROM = '"Holdfast, Inc." <bookings@example.com>'
PYPROJECT_PATH = Path(__file__).parents[1] / "pyproject.toml"

type Json = dict[str, Any]
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
def stored_jobs(sessions: Sessions) -> Callable[[], Awaitable[list[JobRecord]]]:
    """Every job in the table, dead ones included, the earliest due first."""

    async def stored_jobs() -> list[JobRecord]:
        async with sessions() as session:
            return list(await session.scalars(select(JobRecord).order_by(JobRecord.run_at)))

    return stored_jobs


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


@pytest.fixture(scope="session")
def mailpit() -> Iterator[DockerContainer]:
    container = (
        DockerContainer(MAILPIT_IMAGE)
        .with_exposed_ports(1025, 8025)
        .waiting_for(HttpWaitStrategy(8025, "/readyz"))
    )
    with container:
        yield container


@pytest.fixture
def mailer(mailpit: DockerContainer) -> Mailer:
    """Sends email to the mail catcher."""
    return Mailer(mailpit.get_container_host_ip(), mailpit.get_exposed_port(1025), MAIL_FROM)


@pytest.fixture
async def inbox(mailpit: DockerContainer) -> AsyncIterator[Callable[[], Awaitable[list[Json]]]]:
    """Read the emails that arrived during the test, newest first. It starts empty."""
    api_url = f"http://{mailpit.get_container_host_ip()}:{mailpit.get_exposed_port(8025)}"
    async with AsyncClient(base_url=api_url) as api:
        (await api.delete("/api/v1/messages")).raise_for_status()

        async def inbox() -> list[Json]:
            messages: list[Json] = (
                (await api.get("/api/v1/messages")).raise_for_status().json()["messages"]
            )
            return messages

        yield inbox
