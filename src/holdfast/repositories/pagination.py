"""Keyset pagination over time-ordered UUID keys."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from holdfast.domain.pagination import Page


async def fetch_page[RecordT, EntityT](
    session: AsyncSession,
    query: Select[RecordT],
    key: InstrumentedAttribute[UUID],
    after: UUID | None,
    limit: int,
    to_entity: Callable[[RecordT], EntityT],
) -> Page[EntityT]:
    """Return up to `limit` rows whose key comes after `after`, in key order."""
    if after is not None:
        query = query.where(key > after)
    return await fetch_sorted_page(session, query.order_by(key), key, limit, to_entity)


async def fetch_sorted_page[RecordT, EntityT](
    session: AsyncSession,
    sorted_query: Select[RecordT],
    key: InstrumentedAttribute[UUID],
    limit: int,
    to_entity: Callable[[RecordT], EntityT],
) -> Page[EntityT]:
    """Return the first `limit` rows of a sorted query; the next page continues after the key
    of the last one.

    One extra row is fetched to learn whether another page exists without a COUNT query.
    """
    records = (await session.scalars(sorted_query.limit(limit + 1))).all()
    page = records[:limit]
    next_after = getattr(page[-1], key.key) if len(records) > limit else None
    return Page(items=[to_entity(record) for record in page], next_after=next_after)
