"""Use cases for organisations."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.organisations import Organisation, OrganisationDetails
from holdfast.domain.pagination import Page
from holdfast.repositories import organisations


async def create_organisation(
    session: AsyncSession, details: OrganisationDetails, currency: str
) -> Organisation:
    async with session.begin():
        return await organisations.add(session, details, currency)


async def list_organisations(
    session: AsyncSession, after: UUID | None, limit: int
) -> Page[Organisation]:
    async with session.begin():
        return await organisations.list_page(session, after, limit)


async def get_organisation(session: AsyncSession, organisation_id: UUID) -> Organisation:
    async with session.begin():
        return await organisations.get(session, organisation_id)


async def update_organisation(
    session: AsyncSession, organisation_id: UUID, details: OrganisationDetails
) -> Organisation:
    async with session.begin():
        return await organisations.update(session, organisation_id, details)


async def delete_organisation(session: AsyncSession, organisation_id: UUID) -> None:
    async with session.begin():
        await organisations.remove(session, organisation_id)
