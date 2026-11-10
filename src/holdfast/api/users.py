"""The signed-in user's own account and bookings."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter

from holdfast.api.bookings import BookingResponse
from holdfast.api.dependencies import CurrentUserId, DatabaseSession
from holdfast.api.pagination import PageQuery, PageResponse, next_cursor
from holdfast.api.schemas import ResponseSchema
from holdfast.services import bookings, users

router = APIRouter(prefix="/users", tags=["users"])


class UserResponse(ResponseSchema):
    id: UUID
    email: str
    created_at: datetime


@router.get("/me")
async def get_current_user(actor_id: CurrentUserId, session: DatabaseSession) -> UserResponse:
    return UserResponse.model_validate(await users.get_user(session, actor_id))


@router.get("/me/bookings")
async def list_upcoming_bookings(
    page_query: PageQuery, actor_id: CurrentUserId, session: DatabaseSession
) -> PageResponse[BookingResponse]:
    """List your bookings that have not ended yet, soonest first, in every organisation you
    belong to."""
    page = await bookings.list_upcoming_bookings(
        session, actor_id, page_query.after, page_query.limit
    )
    return PageResponse(
        items=[BookingResponse.from_booking(item) for item in page.items],
        next_cursor=next_cursor(page),
    )
