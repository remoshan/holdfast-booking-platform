"""Organisations: the tenants that own locations, resources and bookings."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import Field, StringConstraints

from holdfast.api.dependencies import CurrentUserId, DatabaseSession
from holdfast.api.pagination import PageQuery, PageResponse, next_cursor
from holdfast.api.schemas import Name, RequestSchema, ResponseSchema
from holdfast.domain.organisations import OrganisationDetails
from holdfast.services import organisations as service

router = APIRouter(prefix="/organisations", tags=["organisations"])

Slug = Annotated[
    str, StringConstraints(min_length=3, max_length=63, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class OrganisationRequest(RequestSchema):
    name: Name
    slug: Slug = Field(description="Lowercase letters, digits and single hyphens; unique.")

    def to_details(self) -> OrganisationDetails:
        return OrganisationDetails(name=self.name, slug=self.slug)


class NewOrganisationRequest(OrganisationRequest):
    currency: CurrencyCode = Field(
        description="ISO 4217 code for every price in the organisation. It cannot change later."
    )


class OrganisationResponse(ResponseSchema):
    id: UUID
    name: str
    slug: str
    currency: str
    created_at: datetime


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organisation(
    body: NewOrganisationRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
    request: Request,
    response: Response,
) -> OrganisationResponse:
    """Create an organisation. Its creator becomes its first owner."""
    organisation = await service.create_organisation(
        session, actor_id, body.to_details(), body.currency
    )
    response.headers["Location"] = str(
        request.url_for("get_organisation", organisation_id=organisation.id)
    )
    return OrganisationResponse.model_validate(organisation)


@router.get("")
async def list_organisations(
    page_query: PageQuery, actor_id: CurrentUserId, session: DatabaseSession
) -> PageResponse[OrganisationResponse]:
    """List the organisations the caller belongs to."""
    page = await service.list_organisations(session, actor_id, page_query.after, page_query.limit)
    return PageResponse(
        items=[OrganisationResponse.model_validate(item) for item in page.items],
        next_cursor=next_cursor(page),
    )


@router.get("/{organisation_id}")
async def get_organisation(
    organisation_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> OrganisationResponse:
    organisation = await service.get_organisation(session, actor_id, organisation_id)
    return OrganisationResponse.model_validate(organisation)


@router.put("/{organisation_id}")
async def update_organisation(
    organisation_id: UUID,
    body: OrganisationRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
) -> OrganisationResponse:
    organisation = await service.update_organisation(
        session, actor_id, organisation_id, body.to_details()
    )
    return OrganisationResponse.model_validate(organisation)


@router.delete("/{organisation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organisation(
    organisation_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> None:
    await service.delete_organisation(session, actor_id, organisation_id)
