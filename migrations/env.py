"""Alembic environment: applies migrations to DATABASE_URL through the async engine."""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.core.config import DatabaseSettings
from holdfast.repositories.records import Base


def run_migrations(connection: Connection) -> None:
    # Each migration commits together with its own revision stamp, as Alembic advises when some
    # migrations work outside a transaction. In one shared transaction, such a migration's stamp
    # would be written with the next migration and lost if that one failed.
    context.configure(
        connection=connection, target_metadata=Base.metadata, transaction_per_migration=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # Tests pass a throwaway database's URL in programmatically; the CLI reads DATABASE_URL.
    database_url = context.config.attributes.get("database_url") or str(
        DatabaseSettings().database_url
    )
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        # Also when a migration fails, or its connection stays open until the process ends.
        await engine.dispose()


asyncio.run(run_migrations_online())
