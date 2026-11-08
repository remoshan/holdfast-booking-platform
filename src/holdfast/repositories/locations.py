"""Persistence for locations. Every query is scoped to one organisation."""

from collections.abc import Mapping
from datetime import time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import ConflictError, NotFoundError
from holdfast.domain.locations import Location, LocationDetails, OpeningHours, Weekday
from holdfast.domain.pagination import Page
from holdfast.repositories.integrity import FOREIGN_KEY_VIOLATION, violation_raises
from holdfast.repositories.pagination import fetch_page
from holdfast.repositories.records import LocationRecord


async def add(session: AsyncSession, organisation_id: UUID, details: LocationDetails) -> Location:
    record = LocationRecord(
        organisation_id=organisation_id,
        name=details.name,
        address=details.address,
        timezone=details.timezone.key,
        opening_hours=_opening_hours_to_json(details.opening_hours),
    )
    session.add(record)
    await session.flush()
    return _to_location(record)


async def get(session: AsyncSession, organisation_id: UUID, location_id: UUID) -> Location:
    return _to_location(await _get_record(session, organisation_id, location_id))


async def list_page(
    session: AsyncSession, organisation_id: UUID, after: UUID | None, limit: int
) -> Page[Location]:
    query = select(LocationRecord).where(LocationRecord.organisation_id == organisation_id)
    return await fetch_page(session, query, LocationRecord.id, after, limit, _to_location)


async def update(
    session: AsyncSession, organisation_id: UUID, location_id: UUID, details: LocationDetails
) -> Location:
    record = await _get_record(session, organisation_id, location_id)
    record.name = details.name
    record.address = details.address
    record.timezone = details.timezone.key
    record.opening_hours = _opening_hours_to_json(details.opening_hours)
    await session.flush()
    return _to_location(record)


async def remove(session: AsyncSession, organisation_id: UUID, location_id: UUID) -> None:
    still_used = ConflictError(f"Location {location_id} still has resources.")
    with violation_raises(FOREIGN_KEY_VIOLATION, still_used):
        deleted_id = await session.scalar(
            delete(LocationRecord)
            .where(
                LocationRecord.organisation_id == organisation_id,
                LocationRecord.id == location_id,
            )
            .returning(LocationRecord.id)
        )
    if deleted_id is None:
        raise NotFoundError("Location", location_id)


async def _get_record(
    session: AsyncSession, organisation_id: UUID, location_id: UUID
) -> LocationRecord:
    record = await session.scalar(
        select(LocationRecord).where(
            LocationRecord.organisation_id == organisation_id, LocationRecord.id == location_id
        )
    )
    if record is None:
        raise NotFoundError("Location", location_id)
    return record


def _opening_hours_to_json(
    opening_hours: Mapping[Weekday, OpeningHours],
) -> dict[str, dict[str, str]]:
    return {
        day.value: {
            "opens_at": hours.opens_at.isoformat(),
            "closes_at": hours.closes_at.isoformat(),
        }
        for day, hours in opening_hours.items()
    }


def _to_location(record: LocationRecord) -> Location:
    # Iterate weekdays in calendar order: JSONB does not preserve the order keys were written in.
    opening_hours = {
        day: OpeningHours(
            opens_at=time.fromisoformat(record.opening_hours[day.value]["opens_at"]),
            closes_at=time.fromisoformat(record.opening_hours[day.value]["closes_at"]),
        )
        for day in Weekday
        if day.value in record.opening_hours
    }
    return Location(
        id=record.id,
        organisation_id=record.organisation_id,
        name=record.name,
        address=record.address,
        timezone=ZoneInfo(record.timezone),
        opening_hours=opening_hours,
        created_at=record.created_at,
    )
