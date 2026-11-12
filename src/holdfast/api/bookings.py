"""Bookings: claims on a resource for a period of time."""

from datetime import UTC, datetime
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import AwareDatetime, Field, model_validator

from holdfast.api.dependencies import CurrentUserId, DatabaseSession
from holdfast.api.pagination import PageQuery, PageResponse, next_cursor
from holdfast.api.schemas import RequestSchema, ResponseSchema
from holdfast.domain.bookings import Booking, BookingStatus
from holdfast.domain.periods import Period
from holdfast.services import bookings as service

router = APIRouter(prefix="/organisations/{organisation_id}", tags=["bookings"])

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description=(
            "A unique value, such as a UUID, that makes retrying safe: a request that repeats "
            "the key gets the booking the first request made instead of booking again."
        ),
    ),
]


class BookingRequest(RequestSchema):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def check_end_after_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be later than start.")
        return self

    def to_period(self) -> Period:
        return Period(self.start.astimezone(UTC), self.end.astimezone(UTC))


class BookingResponse(ResponseSchema):
    id: UUID
    organisation_id: UUID
    resource_id: UUID
    user_id: UUID
    start: datetime
    end: datetime
    status: BookingStatus
    price: int = Field(description="In minor units of the organisation's currency.")
    hold_expires_at: datetime | None = Field(
        description="When the hold on a booking that costs something runs out; null if free."
    )
    created_at: datetime

    @classmethod
    def from_booking(cls, booking: Booking) -> Self:
        return cls(
            id=booking.id,
            organisation_id=booking.organisation_id,
            resource_id=booking.resource_id,
            user_id=booking.user_id,
            start=booking.period.start,
            end=booking.period.end,
            status=booking.status,
            price=booking.price,
            hold_expires_at=booking.hold_expires_at,
            created_at=booking.created_at,
        )


@router.post("/resources/{resource_id}/bookings", status_code=status.HTTP_201_CREATED)
async def book_resource(
    organisation_id: UUID,
    resource_id: UUID,
    body: BookingRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> BookingResponse:
    """Book the resource for the caller. A booking that costs something is held for 10 minutes
    and must be confirmed within them, or it expires and frees the slot."""
    booking = await service.book_resource(
        session, actor_id, organisation_id, resource_id, body.to_period(), idempotency_key
    )
    response.headers["Location"] = str(
        request.url_for("get_booking", organisation_id=organisation_id, booking_id=booking.id)
    )
    return BookingResponse.from_booking(booking)


@router.get("/resources/{resource_id}/bookings")
async def list_bookings(
    organisation_id: UUID,
    resource_id: UUID,
    page_query: PageQuery,
    actor_id: CurrentUserId,
    session: DatabaseSession,
) -> PageResponse[BookingResponse]:
    """List the resource's bookings: all of them for admins and owners, otherwise your own."""
    page = await service.list_bookings(
        session, actor_id, organisation_id, resource_id, page_query.after, page_query.limit
    )
    return PageResponse(
        items=[BookingResponse.from_booking(item) for item in page.items],
        next_cursor=next_cursor(page),
    )


@router.get("/bookings/{booking_id}")
async def get_booking(
    organisation_id: UUID, booking_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> BookingResponse:
    booking = await service.get_booking(session, actor_id, organisation_id, booking_id)
    return BookingResponse.from_booking(booking)


@router.post("/bookings/{booking_id}/confirm")
async def confirm_booking(
    organisation_id: UUID, booking_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> BookingResponse:
    """Confirm a held booking before its hold runs out. Until payments exist, this stands in for
    paying. Confirming it again returns it unchanged."""
    booking = await service.confirm_booking(session, actor_id, organisation_id, booking_id)
    return BookingResponse.from_booking(booking)


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    organisation_id: UUID, booking_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> BookingResponse:
    """Cancel a booking that has not started. Cancelling a booking that is already cancelled or
    expired returns it unchanged."""
    booking = await service.cancel_booking(session, actor_id, organisation_id, booking_id)
    return BookingResponse.from_booking(booking)
