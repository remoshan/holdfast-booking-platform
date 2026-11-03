"""Keyset pagination over tables keyed by time-ordered UUIDs."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.pagination import Page
from holdfast.repositories.records import EntityRecord


async def fetch_page[RecordT: EntityRecord, EntityT](
    session: AsyncSession,
    query: Select[RecordT],
    record_type: type[RecordT],
    after: UUID | None,
    limit: int,
    to_entity: Callable[[RecordT], EntityT],
) -> Page[EntityT]:
    """Return up to `limit` rows after the `after` key in id order.

    One extra row is fetched to learn whether another page exists without a COUNT query.
    """
    if after is not None:
        query = query.where(record_type.id > after)
    records = (await session.scalars(query.order_by(record_type.id).limit(limit + 1))).all()
    return Page(
        items=[to_entity(record) for record in records[:limit]], has_more=len(records) > limit
    )
