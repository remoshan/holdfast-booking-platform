"""Members: who belongs to an organisation, and with which role."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import Field

from holdfast.api.dependencies import CurrentUserId, DatabaseSession
from holdfast.api.pagination import PageQuery, PageResponse, next_cursor
from holdfast.api.schemas import Email, RequestSchema, ResponseSchema
from holdfast.domain.memberships import Role
from holdfast.services import memberships as service

router = APIRouter(prefix="/organisations/{organisation_id}/members", tags=["members"])


class NewMemberRequest(RequestSchema):
    email: Email = Field(description="The email of an existing account.")
    role: Role = Role.MEMBER


class RoleRequest(RequestSchema):
    role: Role


class MemberResponse(ResponseSchema):
    user_id: UUID
    email: str
    role: Role
    created_at: datetime = Field(description="When the user joined the organisation.")


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_member(
    organisation_id: UUID,
    body: NewMemberRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
    request: Request,
    response: Response,
) -> MemberResponse:
    member = await service.add_member(session, actor_id, organisation_id, body.email, body.role)
    response.headers["Location"] = str(
        request.url_for("get_member", organisation_id=organisation_id, user_id=member.user_id)
    )
    return MemberResponse.model_validate(member)


@router.get("")
async def list_members(
    organisation_id: UUID, page_query: PageQuery, actor_id: CurrentUserId, session: DatabaseSession
) -> PageResponse[MemberResponse]:
    page = await service.list_members(
        session, actor_id, organisation_id, page_query.after, page_query.limit
    )
    return PageResponse(
        items=[MemberResponse.model_validate(item) for item in page.items],
        next_cursor=next_cursor(page),
    )


@router.get("/{user_id}")
async def get_member(
    organisation_id: UUID, user_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> MemberResponse:
    member = await service.get_member(session, actor_id, organisation_id, user_id)
    return MemberResponse.model_validate(member)


@router.put("/{user_id}")
async def change_role(
    organisation_id: UUID,
    user_id: UUID,
    body: RoleRequest,
    actor_id: CurrentUserId,
    session: DatabaseSession,
) -> MemberResponse:
    member = await service.change_role(session, actor_id, organisation_id, user_id, body.role)
    return MemberResponse.model_validate(member)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    organisation_id: UUID, user_id: UUID, actor_id: CurrentUserId, session: DatabaseSession
) -> None:
    await service.remove_member(session, actor_id, organisation_id, user_id)
