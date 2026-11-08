"""Resources: the desks and rooms of a location, and when they are free."""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import Field

from holdfast.api.dependencies import CurrentUserId, DatabaseSession
from holdfast.api.pagination import PageQuery, PageResponse, next_cursor
from holdfast.api.schemas import Name, PeriodResponse, RequestSchema, ResponseSchema
from holdfast.domain.resources import ResourceDetails, ResourceKind
from holdfast.services import resources as service

router = APIRouter(prefix="/organisations/{organisation_id}", tags=["resources"])


class ResourceRequest(RequestSchema):
    name: Name
    kind: ResourceKind
    capacity: int = Field(ge=1, le=1000, description="How many people the resource seats.")
    hourly_price: int = Field(
        ge=0,
        le=1_000_000,
        description="Per hour, in minor units of the organisation's currency; 0 means free.",
    )

    def to_details(self) -> ResourceDetails:
        return ResourceDetails(
            name=self.name, kind=self.kind, capacity=self.capacity, hourly_price=self.hourly_price
        )


class ResourceResponse(ResponseSchema):
    id: UUID
    organisation_id: UUID
    location_id: UUID
    name: str
    kind: ResourceKind
    capacity: int
    hourly_price: int
    created_at: datetime


class AvailabilityResponse(ResponseSchema):
    resource_id: UUID
    timezone: ZoneInfo
    free: list[PeriodResponse] = Field(description="Periods a new booking could still use.")


@router.post("/locations/{location_id}/resources", status_code=status.HTTP_201_CREATED)
async def create_resource(
    organisation_id: UUID,
    location_id: UUID,
    body: ResourceRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
    request: Request,
    response: Response,
) -> ResourceResponse:
    resource = await service.create_resource(
        session, actor_id, organisation_id, location_id, body.to_details()
    )
    response.headers["Location"] = str(
        request.url_for("get_resource", organisation_id=organisation_id, resource_id=resource.id)
    )
    return ResourceResponse.model_validate(resource)


@router.get("/locations/{location_id}/resources")
async def list_resources(
    organisation_id: UUID,
    location_id: UUID,
    page_query: PageQuery,
    actor_id: CurrentUserId,
    session: DatabaseSession,
) -> PageResponse[ResourceResponse]:
    page = await service.list_resources(
        session, actor_id, organisation_id, location_id, page_query.after, page_query.limit
    )
    return PageResponse(
        items=[ResourceResponse.model_validate(item) for item in page.items],
        next_cursor=next_cursor(page),
    )


@router.get("/resources/{resource_id}")
async def get_resource(
    organisation_id: UUID, resource_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> ResourceResponse:
    resource = await service.get_resource(session, actor_id, organisation_id, resource_id)
    return ResourceResponse.model_validate(resource)


@router.put("/resources/{resource_id}")
async def update_resource(
    organisation_id: UUID,
    resource_id: UUID,
    body: ResourceRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
) -> ResourceResponse:
    resource = await service.update_resource(
        session, actor_id, organisation_id, resource_id, body.to_details()
    )
    return ResourceResponse.model_validate(resource)


@router.delete("/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resource(
    organisation_id: UUID, resource_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> None:
    await service.delete_resource(session, actor_id, organisation_id, resource_id)


@router.get("/resources/{resource_id}/availability")
async def get_availability(
    organisation_id: UUID,
    resource_id: UUID,
    day: Annotated[date, Query(alias="date", description="A day in the location's time zone.")],
    actor_id: CurrentUserId,
    session: DatabaseSession,
) -> AvailabilityResponse:
    availability = await service.get_availability(
        session, actor_id, organisation_id, resource_id, day
    )
    return AvailabilityResponse.model_validate(availability)
