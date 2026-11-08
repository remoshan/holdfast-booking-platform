"""Locations: an organisation's sites, each with a time zone and weekly opening hours."""

from datetime import datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response, status
from pydantic import Field, StringConstraints

from holdfast.api.dependencies import CurrentUserId, DatabaseSession
from holdfast.api.pagination import PageQuery, PageResponse, next_cursor
from holdfast.api.schemas import Name, RequestSchema, ResponseSchema
from holdfast.domain.locations import LocationDetails, OpeningHours, Weekday
from holdfast.services import locations as service

router = APIRouter(prefix="/organisations/{organisation_id}/locations", tags=["locations"])

Address = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class LocationRequest(RequestSchema):
    name: Name
    address: Address
    timezone: ZoneInfo = Field(
        description="IANA time zone, such as Europe/London. Opening hours use its local time."
    )
    opening_hours: dict[Weekday, OpeningHours] = Field(
        description="Local opening and closing times; weekdays left out are closed."
    )

    def to_details(self) -> LocationDetails:
        return LocationDetails(
            name=self.name,
            address=self.address,
            timezone=self.timezone,
            opening_hours=self.opening_hours,
        )


class LocationResponse(ResponseSchema):
    id: UUID
    organisation_id: UUID
    name: str
    address: str
    timezone: ZoneInfo
    opening_hours: dict[Weekday, OpeningHours]
    created_at: datetime


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_location(
    organisation_id: UUID,
    body: LocationRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
    request: Request,
    response: Response,
) -> LocationResponse:
    location = await service.create_location(session, actor_id, organisation_id, body.to_details())
    response.headers["Location"] = str(
        request.url_for("get_location", organisation_id=organisation_id, location_id=location.id)
    )
    return LocationResponse.model_validate(location)


@router.get("")
async def list_locations(
    organisation_id: UUID, page_query: PageQuery, actor_id: CurrentUserId, session: DatabaseSession
) -> PageResponse[LocationResponse]:
    page = await service.list_locations(
        session, actor_id, organisation_id, page_query.after, page_query.limit
    )
    return PageResponse(
        items=[LocationResponse.model_validate(item) for item in page.items],
        next_cursor=next_cursor(page),
    )


@router.get("/{location_id}")
async def get_location(
    organisation_id: UUID, location_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> LocationResponse:
    location = await service.get_location(session, actor_id, organisation_id, location_id)
    return LocationResponse.model_validate(location)


@router.put("/{location_id}")
async def update_location(
    organisation_id: UUID,
    location_id: UUID,
    body: LocationRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
) -> LocationResponse:
    location = await service.update_location(
        session, actor_id, organisation_id, location_id, body.to_details()
    )
    return LocationResponse.model_validate(location)


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_location(
    organisation_id: UUID, location_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> None:
    await service.delete_location(session, actor_id, organisation_id, location_id)
