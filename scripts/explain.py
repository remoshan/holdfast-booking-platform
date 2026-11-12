"""Measure Holdfast's hot queries with EXPLAIN ANALYZE, before and after the indexes added for
them.

Every query is the exact SQL the application sends: a repository function runs and SQLAlchemy's
statements are captured on their way to Postgres. "Before" drops those indexes inside a
transaction that is rolled back, so both sides run on the same data and the database is left as
it was. Each side reports the median of several warm runs. Run it on seeded data:

    uv run python scripts/explain.py [--database-url URL]
"""

import argparse
import asyncio
import re
import statistics
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid7

from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from holdfast.domain.periods import Period
from holdfast.domain.resources import ResourceKind
from holdfast.repositories import bookings, jobs, organisations, refresh_tokens, resources
from holdfast.repositories.records import BookingRecord, ResourceRecord

LOCAL_DATABASE_URL = "postgresql+asyncpg://holdfast:holdfast@127.0.0.1:54320/holdfast"
MEASURED_INDEXES = (
    "ix_bookings_resource_id_id",
    "ix_bookings_active_user_id_end",
    "ix_memberships_user_id",
    "ix_refresh_tokens_family_id",
    "ix_jobs_queued_run_at",
)
WARM_UP_RUNS = 1
MEASURED_RUNS = 7
PAGE_SIZE = 20

# Queries that pick the heaviest parameters the data holds.
BUSIEST_RESOURCE = text(
    "SELECT organisation_id, resource_id, count(*) FROM bookings"
    " GROUP BY organisation_id, resource_id ORDER BY count(*) DESC LIMIT 1"
)
BUSIEST_DAY = text(
    "SELECT date_trunc('day', lower(period)) FROM bookings WHERE resource_id = :resource_id"
    " GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
)
HEAVIEST_BOOKER = text(
    "SELECT user_id FROM bookings GROUP BY user_id ORDER BY count(*) DESC LIMIT 1"
)
MOST_ORGANISATIONS = text(
    "SELECT user_id FROM memberships GROUP BY user_id ORDER BY count(*) DESC LIMIT 1"
)
LARGEST_LIVE_FAMILY = text(
    "SELECT token_hash FROM refresh_tokens WHERE family_id = ("
    " SELECT family_id FROM refresh_tokens WHERE revoked_at IS NULL"
    " GROUP BY family_id ORDER BY count(*) DESC LIMIT 1) LIMIT 1"
)
NTH_BOOKING = text(
    "SELECT id FROM bookings WHERE resource_id = :resource_id ORDER BY id OFFSET :depth LIMIT 1"
)
RAREST_BOOKER = text(
    "SELECT user_id FROM bookings WHERE resource_id = :resource_id"
    " GROUP BY user_id ORDER BY count(*), user_id LIMIT 1"
)
SIZES = text(
    "SELECT relname, pg_size_pretty(pg_relation_size(relid)) FROM pg_stat_user_tables"
    " WHERE relname IN ('bookings', 'memberships', 'refresh_tokens', 'jobs')"
    " UNION ALL SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) FROM"
    " pg_stat_user_indexes WHERE relname IN ('bookings', 'memberships', 'refresh_tokens', 'jobs')"
)

type Scenario = Callable[[AsyncSession], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class Statement:
    sql: str
    parameters: Any


@dataclass(frozen=True, slots=True)
class Measurement:
    median_ms: float
    plan: str


async def main(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        for title, scenario in await _scenarios(engine):
            statements = await _capture(engine, scenario)
            before = await _measure(engine, statements, without_indexes=True)
            after = await _measure(engine, statements, without_indexes=False)
            print(f"## {title}\n")
            print(f"Before: {before.median_ms:.3f} ms   After: {after.median_ms:.3f} ms\n")
            print(f"Before:\n{before.plan}\n\nAfter:\n{after.plan}\n")
        async with engine.connect() as connection:
            print("## Table and index sizes\n")
            for name, size in await connection.execute(SIZES):
                print(f"{name:<40} {size:>10}")
    finally:
        await engine.dispose()


async def _scenarios(engine: AsyncEngine) -> list[tuple[str, Scenario]]:
    """The queries to measure, each with the heaviest parameters the data holds."""
    async with engine.connect() as connection:
        organisation_id, resource_id, resource_bookings = (
            await connection.execute(BUSIEST_RESOURCE)
        ).one()
        busiest_day = await connection.scalar(BUSIEST_DAY, {"resource_id": resource_id})
        location_id = await connection.scalar(
            select(ResourceRecord.location_id).where(ResourceRecord.id == resource_id)
        )
        heaviest_booker = await connection.scalar(HEAVIEST_BOOKER)
        most_organisations = await connection.scalar(MOST_ORGANISATIONS)
        live_token_hash = await connection.scalar(LARGEST_LIVE_FAMILY)
        # A member who booked the resource the fewest times has the most of its bookings to skip.
        rare_booker = await connection.scalar(RAREST_BOOKER, {"resource_id": resource_id})
        depths = (0, resource_bookings // 2, resource_bookings - PAGE_SIZE)
        cursors = {
            depth: await connection.scalar(
                NTH_BOOKING, {"resource_id": resource_id, "depth": depth}
            )
            for depth in depths
        }
    day = Period(busiest_day, busiest_day + timedelta(days=1))
    now = datetime.now(UTC)
    # Two years ahead the resource is certainly free, so the insert succeeds every run.
    free_hour = now.replace(year=now.year + 2, minute=0, second=0, microsecond=0)
    free_slot = Period(free_hour, free_hour + timedelta(hours=1))
    unused_resource_id = uuid7()

    async def delete_an_unused_resource(session: AsyncSession) -> None:
        session.add(
            ResourceRecord(
                id=unused_resource_id,
                organisation_id=organisation_id,
                location_id=location_id,
                name="Unused",
                kind=ResourceKind.DESK,
                capacity=1,
                hourly_price=0,
            )
        )
        await session.flush()
        await resources.remove(session, organisation_id, unused_resource_id)

    def resource_page_by_offset(depth: int) -> Scenario:
        async def scenario(session: AsyncSession) -> object:
            return (
                await session.scalars(
                    select(BookingRecord)
                    .where(
                        BookingRecord.organisation_id == organisation_id,
                        BookingRecord.resource_id == resource_id,
                    )
                    .order_by(BookingRecord.id)
                    .offset(depth)
                    .limit(PAGE_SIZE + 1)
                )
            ).all()

        return scenario

    def resource_page_by_keyset(depth: int) -> Scenario:
        def scenario(session: AsyncSession) -> Awaitable[object]:
            return bookings.list_page(
                session, organisation_id, resource_id, None, cursors[depth], PAGE_SIZE
            )

        return scenario

    scenarios: list[tuple[str, Scenario]] = [
        (
            "Availability: one resource's active bookings on its busiest day",
            lambda session: bookings.active_periods(session, resource_id, day),
        ),
        (
            "My upcoming bookings, first page, for the user with the most bookings",
            lambda session: bookings.list_upcoming(session, heaviest_booker, now, None, PAGE_SIZE),
        ),
        (
            "A resource's bookings, first page, for the busiest resource",
            lambda session: bookings.list_page(
                session, organisation_id, resource_id, None, None, PAGE_SIZE
            ),
        ),
        (
            "The same list as a plain member, who sees only their own bookings",
            lambda session: bookings.list_page(
                session, organisation_id, resource_id, rare_booker, None, PAGE_SIZE
            ),
        ),
        (
            "My organisations, for the user in the most organisations",
            lambda session: organisations.list_page(session, most_organisations, None, PAGE_SIZE),
        ),
        (
            "Log out: revoke every refresh token of the largest live sign-in",
            lambda session: refresh_tokens.revoke_family_of(session, live_token_hash, now),
        ),
        ("Delete a resource that has no bookings", delete_an_unused_resource),
        (
            "A worker's poll for the most overdue job among the seeded reminders",
            lambda session: jobs.claim_next(session, now),
        ),
        (
            "Book a free slot: the write that every booking index slows down",
            lambda session: bookings.add(
                session, organisation_id, resource_id, heaviest_booker, free_slot, price=0
            ),
        ),
    ]
    for depth in depths:
        scenarios.append(
            (
                f"Busiest resource's bookings at depth {depth:,}: OFFSET",
                resource_page_by_offset(depth),
            )
        )
        scenarios.append(
            (
                f"Busiest resource's bookings at depth {depth:,}: keyset",
                resource_page_by_keyset(depth),
            )
        )
    return scenarios


async def _capture(engine: AsyncEngine, scenario: Scenario) -> list[Statement]:
    """Run the scenario in a transaction that is rolled back, and return the SQL it sent."""
    statements: list[Statement] = []

    def record(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: Any,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(Statement(statement, parameters))

    async with engine.connect() as connection:
        event.listen(connection.sync_connection, "before_cursor_execute", record)
        try:
            await scenario(AsyncSession(bind=connection))
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", record)
            await connection.rollback()
    return statements


async def _measure(
    engine: AsyncEngine, statements: Sequence[Statement], *, without_indexes: bool
) -> Measurement:
    """EXPLAIN ANALYZE the scenario's last statement after replaying the ones before it, each
    run in its own transaction that is rolled back."""
    *setup, measured = statements
    timings = []
    plan = ""
    for _ in range(WARM_UP_RUNS + MEASURED_RUNS):
        async with engine.connect() as connection:
            if without_indexes:
                await _drop_measured_indexes(connection)
            for statement in setup:
                await connection.exec_driver_sql(statement.sql, statement.parameters)
            rows = await connection.exec_driver_sql(
                f"EXPLAIN (ANALYZE, BUFFERS) {measured.sql}", measured.parameters
            )
            plan = "\n".join(row[0] for row in rows)
            await connection.rollback()
        timings.append(_execution_time(plan))
    return Measurement(statistics.median(timings[WARM_UP_RUNS:]), plan)


async def _drop_measured_indexes(connection: AsyncConnection) -> None:
    for name in MEASURED_INDEXES:
        await connection.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")


def _execution_time(plan: str) -> float:
    found = re.search(r"Execution Time: ([\d.]+) ms", plan)
    if found is None:
        raise ValueError(f"EXPLAIN ANALYZE reported no execution time:\n{plan}")
    return float(found.group(1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EXPLAIN ANALYZE Holdfast's hot queries.")
    parser.add_argument("--database-url", default=LOCAL_DATABASE_URL)
    asyncio.run(main(parser.parse_args().database_url))
