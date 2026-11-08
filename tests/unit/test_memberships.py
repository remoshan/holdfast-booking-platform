from datetime import UTC, datetime
from uuid import uuid4

import pytest

from holdfast.domain.errors import PermissionDeniedError
from holdfast.domain.memberships import Membership, Role


def _membership(role: Role) -> Membership:
    return Membership(
        organisation_id=uuid4(),
        user_id=uuid4(),
        email="ada@example.com",
        role=role,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _may_manage(actor: Role, target: Role) -> bool:
    try:
        _membership(actor).require_can_manage(target)
    except PermissionDeniedError:
        return False
    return True


@pytest.mark.parametrize(
    ("role", "minimum", "allowed"),
    [
        (Role.MEMBER, Role.MEMBER, True),
        (Role.MEMBER, Role.ADMIN, False),
        (Role.ADMIN, Role.ADMIN, True),
        (Role.ADMIN, Role.OWNER, False),
        (Role.OWNER, Role.MEMBER, True),
        (Role.OWNER, Role.OWNER, True),
    ],
)
def test_each_role_includes_the_roles_below_it(role: Role, minimum: Role, allowed: bool) -> None:
    assert role.includes(minimum) is allowed


def test_a_role_below_the_minimum_is_refused() -> None:
    with pytest.raises(PermissionDeniedError, match="needs the admin role or higher"):
        _membership(Role.MEMBER).require(Role.ADMIN)


@pytest.mark.parametrize(
    ("actor", "target", "allowed"),
    [
        (Role.OWNER, Role.OWNER, True),
        (Role.OWNER, Role.ADMIN, True),
        (Role.OWNER, Role.MEMBER, True),
        (Role.ADMIN, Role.OWNER, False),
        (Role.ADMIN, Role.ADMIN, False),
        (Role.ADMIN, Role.MEMBER, True),
        (Role.MEMBER, Role.MEMBER, False),
    ],
)
def test_owners_manage_everyone_and_admins_only_members(
    actor: Role, target: Role, allowed: bool
) -> None:
    assert _may_manage(actor, target) is allowed


@pytest.mark.parametrize(
    ("role", "sees_all"), [(Role.MEMBER, False), (Role.ADMIN, True), (Role.OWNER, True)]
)
def test_only_admins_and_owners_see_every_booking(role: Role, sees_all: bool) -> None:
    assert _membership(role).sees_all_bookings is sees_all
