import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.repositories.records import Base

pytestmark = pytest.mark.anyio


def _schema_differences(connection: Connection) -> list[object]:
    return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))


async def test_migrations_match_records(database_url: str) -> None:
    """Fails when a record changes without a matching migration."""
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        differences = await connection.run_sync(_schema_differences)
    await engine.dispose()

    assert differences == []
