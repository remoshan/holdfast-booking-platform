"""The signed-in user's own account."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter

from holdfast.api.dependencies import CurrentUserId, DatabaseSession
from holdfast.api.schemas import ResponseSchema
from holdfast.services import users as service

router = APIRouter(prefix="/users", tags=["users"])


class UserResponse(ResponseSchema):
    id: UUID
    email: str
    created_at: datetime


@router.get("/me")
async def get_current_user(actor_id: CurrentUserId, session: DatabaseSession) -> UserResponse:
    return UserResponse.model_validate(await service.get_user(session, actor_id))
