"""Persistence for organisations."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import ConflictError, NotFoundError
from holdfast.domain.organisations import Organisation, OrganisationDetails
from holdfast.domain.pagination import Page
from holdfast.repositories.integrity import (
    FOREIGN_KEY_VIOLATION,
    UNIQUE_VIOLATION,
    violation_raises,
)
from holdfast.repositories.pagination import fetch_page
from holdfast.repositories.records import OrganisationRecord


async def add(session: AsyncSession, details: OrganisationDetails, currency: str) -> Organisation:
    record = OrganisationRecord(name=details.name, slug=details.slug, currency=currency)
    session.add(record)
    with violation_raises(UNIQUE_VIOLATION, _slug_taken(details.slug)):
        await session.flush()
    return _to_organisation(record)


async def get(session: AsyncSession, organisation_id: UUID) -> Organisation:
    return _to_organisation(await _get_record(session, organisation_id))


async def list_page(session: AsyncSession, after: UUID | None, limit: int) -> Page[Organisation]:
    return await fetch_page(
        session, select(OrganisationRecord), OrganisationRecord, after, limit, _to_organisation
    )


async def update(
    session: AsyncSession, organisation_id: UUID, details: OrganisationDetails
) -> Organisation:
    record = await _get_record(session, organisation_id)
    record.name = details.name
    record.slug = details.slug
    with violation_raises(UNIQUE_VIOLATION, _slug_taken(details.slug)):
        await session.flush()
    return _to_organisation(record)


async def remove(session: AsyncSession, organisation_id: UUID) -> None:
    still_used = ConflictError(f"Organisation {organisation_id} still has locations.")
    with violation_raises(FOREIGN_KEY_VIOLATION, still_used):
        deleted_id = await session.scalar(
            delete(OrganisationRecord)
            .where(OrganisationRecord.id == organisation_id)
            .returning(OrganisationRecord.id)
        )
    if deleted_id is None:
        raise NotFoundError("Organisation", organisation_id)


async def _get_record(session: AsyncSession, organisation_id: UUID) -> OrganisationRecord:
    record = await session.get(OrganisationRecord, organisation_id)
    if record is None:
        raise NotFoundError("Organisation", organisation_id)
    return record


def _slug_taken(slug: str) -> ConflictError:
    return ConflictError(f"The slug {slug} is already taken.")


def _to_organisation(record: OrganisationRecord) -> Organisation:
    return Organisation(
        id=record.id,
        name=record.name,
        slug=record.slug,
        currency=record.currency,
        created_at=record.created_at,
    )
