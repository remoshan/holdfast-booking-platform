"""Many bookings for one resource at the same moment: the database decides who wins."""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import status
from httpx import AsyncClient, Response

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Book = Callable[..., Awaitable[Response]]

SIMULTANEOUS_REQUESTS = 50
POOLED_CONNECTIONS = 5  # SQLAlchemy's default pool_size: idle connections the pool keeps open


def _clock(minutes_after_opening: int) -> str:
    hours, minutes = divmod(8 * 60 + minutes_after_opening, 60)
    return f"{hours:02}:{minutes:02}"


async def _open_pooled_connections(client: AsyncClient) -> None:
    """Connect before the race, as a running server would have.

    Opening a connection takes longer than a whole booking, so on a cold pool the first request
    finishes before the others can start their transactions, and a race could go unnoticed.
    """
    await asyncio.gather(*(client.get("/health") for _ in range(POOLED_CONNECTIONS)))


async def test_only_one_of_many_simultaneous_requests_for_a_slot_succeeds(
    client: AsyncClient, organisation: Json, resource: Json, book: Book
) -> None:
    await _open_pooled_connections(client)

    responses = await asyncio.gather(
        *(book("09:00", "10:00") for _ in range(SIMULTANEOUS_REQUESTS))
    )

    assert Counter(response.status_code for response in responses) == {
        status.HTTP_201_CREATED: 1,
        status.HTTP_409_CONFLICT: SIMULTANEOUS_REQUESTS - 1,
    }
    conflicts = [response for response in responses if response.status_code == 409]
    assert all(response.json()["type"].endswith("#booking-conflict") for response in conflicts)
    stored = await client.get(
        f"/organisations/{organisation['id']}/resources/{resource['id']}/bookings"
    )
    assert len(stored.json()["items"]) == 1


async def test_simultaneous_requests_for_different_slots_all_succeed(
    client: AsyncClient, book: Book
) -> None:
    # Forty back-to-back 15-minute slots fill the 08:00 to 18:00 opening hours exactly.
    slots = [(_clock(offset), _clock(offset + 15)) for offset in range(0, 10 * 60, 15)]
    await _open_pooled_connections(client)

    responses = await asyncio.gather(*(book(start, end) for start, end in slots))

    assert [response.status_code for response in responses] == [201] * len(slots)
