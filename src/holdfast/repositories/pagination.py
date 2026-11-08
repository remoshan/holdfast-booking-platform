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
    """Return up to `limit` rows whose key comes after `after`, in key order.

    One extra row is fetched to learn whether another page exists without a COUNT query.
    """
    if after is not None:
        query = query.where(key > after)
    records = (await session.scalars(query.order_by(key).limit(limit + 1))).all()
    page = records[:limit]
    next_after = getattr(page[-1], key.key) if len(records) > limit else None
    return Page(items=[to_entity(record) for record in page], next_after=next_after)
