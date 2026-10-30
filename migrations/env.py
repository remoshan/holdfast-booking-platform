"""Alembic environment: applies migrations to DATABASE_URL through the async engine."""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.core.config import Settings
from holdfast.repositories.records import Base


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # Tests pass a throwaway database's URL in programmatically; the CLI reads DATABASE_URL.
    database_url = context.config.attributes.get("database_url") or str(Settings().database_url)
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


asyncio.run(run_migrations_online())
