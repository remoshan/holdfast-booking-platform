"""Integration fixtures: one disposable Postgres per test session, migrated to head."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from testcontainers.community.postgres import PostgresContainer

from holdfast.core.config import Settings
from holdfast.main import create_app

POSTGRES_IMAGE = "postgres:18-alpine"
PYPROJECT_PATH = Path(__file__).parents[2] / "pyproject.toml"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        alembic_config = Config(toml_file=str(PYPROJECT_PATH))
        alembic_config.attributes["database_url"] = url
        command.upgrade(alembic_config, "head")
        yield url


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url)


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client
