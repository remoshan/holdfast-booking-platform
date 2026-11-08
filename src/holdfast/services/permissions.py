"""The membership check that every organisation-scoped use case starts with."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.domain.errors import NotFoundError
from holdfast.domain.memberships import Membership, Role
from holdfast.repositories import memberships


async def require_membership(
    session: AsyncSession, organisation_id: UUID, user_id: UUID, minimum: Role = Role.MEMBER
) -> Membership:
    """Return the caller's membership after checking that it grants at least `minimum`.

    Outsiders get the same NotFoundError as for an organisation that does not exist, so they
    cannot even learn that it does. Members whose role is too low get PermissionDeniedError.
    """
    membership = await memberships.find(session, organisation_id, user_id)
    if membership is None:
        raise NotFoundError("Organisation", organisation_id)
    membership.require(minimum)
    return membership
