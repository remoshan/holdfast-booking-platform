"""Inserts for one slot that start at the same instant, below the HTTP layer.

Through HTTP only a few pooled connections reach the INSERT together. Here every session connects
first and then all of them insert at once: the contention under which a plain INSERT into an
exclusion constraint can deadlock instead of failing cleanly.
"""

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from holdfast.domain.errors import BookingConflictError
from holdfast.domain.periods import Period
from holdfast.repositories import bookings

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]

RACING_SESSIONS = 20
# One hour-long slot per round, 08:00 to 18:00. Later rounds reuse connections whose prepared
# statements are already cached, so their inserts collide within microseconds.
ROUNDS = 10


async def test_racing_inserts_for_a_slot_end_in_one_booking_and_clean_conflicts(
    database_url: str, organisation: Json, resource: Json, user: Json, at: Callable[..., str]
) -> None:
    engine = create_async_engine(database_url, pool_size=RACING_SESSIONS)
    sessions = async_sessionmaker(engine)

    async def race(period: Period, everyone_connected: asyncio.Barrier) -> str:
        async with sessions() as session:
            await session.connection()
            await everyone_connected.wait()
            try:
                await bookings.add(
                    session,
                    UUID(organisation["id"]),
                    UUID(resource["id"]),
                    UUID(user["id"]),
                    period,
                    price=0,
                )
            except BookingConflictError:
                return "conflict"
            await session.commit()
            return "booked"

    outcomes: Counter[str] = Counter()
    try:
        for hour in range(8, 8 + ROUNDS):
            period = Period(
                datetime.fromisoformat(at(f"{hour:02}:00")).astimezone(UTC),
                datetime.fromisoformat(at(f"{hour + 1:02}:00")).astimezone(UTC),
            )
            everyone_connected = asyncio.Barrier(RACING_SESSIONS)
            results = await asyncio.gather(
                *(race(period, everyone_connected) for _ in range(RACING_SESSIONS)),
                return_exceptions=True,
            )
            outcomes.update(
                type(result).__name__ if isinstance(result, BaseException) else result
                for result in results
            )
    finally:
        await engine.dispose()

    assert outcomes == {"booked": ROUNDS, "conflict": ROUNDS * (RACING_SESSIONS - 1)}
