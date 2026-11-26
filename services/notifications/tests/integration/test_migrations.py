import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.notifications.records import Base

pytestmark = pytest.mark.anyio


async def test_migrations_match_records(database_url: str) -> None:
    """Fails when a record changes without a matching migration."""
    assert await _schema_differences(database_url) == []


async def test_the_migrations_can_be_undone_and_applied_again(
    database_url: str, alembic_config: Config
) -> None:
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    await asyncio.to_thread(command.upgrade, alembic_config, "head")

    assert await _schema_differences(database_url) == []


async def _schema_differences(database_url: str) -> list[object]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_compare)
    finally:
        await engine.dispose()


def _compare(connection: Connection) -> list[object]:
    return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))
