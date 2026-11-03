"""Persistence for resources. Every query is scoped to one organisation."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import ConflictError, NotFoundError
from holdfast.domain.pagination import Page
from holdfast.domain.resources import Resource, ResourceDetails
from holdfast.repositories.integrity import FOREIGN_KEY_VIOLATION, violation_raises
from holdfast.repositories.pagination import fetch_page
from holdfast.repositories.records import ResourceRecord


async def add(
    session: AsyncSession, organisation_id: UUID, location_id: UUID, details: ResourceDetails
) -> Resource:
    record = ResourceRecord(
        organisation_id=organisation_id,
        location_id=location_id,
        name=details.name,
        kind=details.kind,
        capacity=details.capacity,
        hourly_price=details.hourly_price,
    )
    session.add(record)
    await session.flush()
    return _to_resource(record)


async def get(session: AsyncSession, organisation_id: UUID, resource_id: UUID) -> Resource:
    return _to_resource(await _get_record(session, organisation_id, resource_id))


async def list_page(
    session: AsyncSession, organisation_id: UUID, location_id: UUID, after: UUID | None, limit: int
) -> Page[Resource]:
    query = select(ResourceRecord).where(
        ResourceRecord.organisation_id == organisation_id,
        ResourceRecord.location_id == location_id,
    )
    return await fetch_page(session, query, ResourceRecord, after, limit, _to_resource)


async def update(
    session: AsyncSession, organisation_id: UUID, resource_id: UUID, details: ResourceDetails
) -> Resource:
    record = await _get_record(session, organisation_id, resource_id)
    record.name = details.name
    record.kind = details.kind
    record.capacity = details.capacity
    record.hourly_price = details.hourly_price
    await session.flush()
    return _to_resource(record)


async def remove(session: AsyncSession, organisation_id: UUID, resource_id: UUID) -> None:
    # Bookings keep their resource for history, so a resource that was ever booked stays.
    has_bookings = ConflictError(f"Resource {resource_id} has bookings and cannot be deleted.")
    with violation_raises(FOREIGN_KEY_VIOLATION, has_bookings):
        deleted_id = await session.scalar(
            delete(ResourceRecord)
            .where(
                ResourceRecord.organisation_id == organisation_id,
                ResourceRecord.id == resource_id,
            )
            .returning(ResourceRecord.id)
        )
    if deleted_id is None:
        raise NotFoundError("Resource", resource_id)


async def _get_record(
    session: AsyncSession, organisation_id: UUID, resource_id: UUID
) -> ResourceRecord:
    record = await session.scalar(
        select(ResourceRecord).where(
            ResourceRecord.organisation_id == organisation_id, ResourceRecord.id == resource_id
        )
    )
    if record is None:
        raise NotFoundError("Resource", resource_id)
    return record


def _to_resource(record: ResourceRecord) -> Resource:
    return Resource(
        id=record.id,
        organisation_id=record.organisation_id,
        location_id=record.location_id,
        name=record.name,
        kind=record.kind,
        capacity=record.capacity,
        hourly_price=record.hourly_price,
        created_at=record.created_at,
    )
