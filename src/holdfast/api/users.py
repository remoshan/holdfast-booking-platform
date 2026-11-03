"""Users: the people who book resources."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import AfterValidator, EmailStr

from holdfast.api.dependencies import DatabaseSession
from holdfast.api.schemas import RequestSchema, ResponseSchema
from holdfast.services import users as service

router = APIRouter(prefix="/users", tags=["users"])


class UserRequest(RequestSchema):
    # Lower-cased, so the same address typed in different case cannot create a second user.
    email: Annotated[EmailStr, AfterValidator(str.lower)]


class UserResponse(ResponseSchema):
    id: UUID
    email: str
    created_at: datetime


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserRequest, session: DatabaseSession, request: Request, response: Response
) -> UserResponse:
    user = await service.create_user(session, body.email)
    response.headers["Location"] = str(request.url_for("get_user", user_id=user.id))
    return UserResponse.model_validate(user)


@router.get("/{user_id}")
async def get_user(user_id: UUID, session: DatabaseSession) -> UserResponse:
    return UserResponse.model_validate(await service.get_user(session, user_id))
