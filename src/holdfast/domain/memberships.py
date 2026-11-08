"""Memberships: a user's role in one organisation, and what each role may do."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto
from uuid import UUID

from holdfast.domain.errors import PermissionDeniedError


class Role(StrEnum):
    """Roles from least to most powerful. Each may do everything the roles before it may."""

    MEMBER = auto()
    ADMIN = auto()
    OWNER = auto()

    def includes(self, other: Role) -> bool:
        roles = list(Role)
        return roles.index(self) >= roles.index(other)


@dataclass(frozen=True, slots=True)
class Membership:
    organisation_id: UUID
    user_id: UUID
    email: str
    role: Role
    created_at: datetime

    @property
    def sees_all_bookings(self) -> bool:
        """Admins and owners see every booking; members see only their own."""
        return self.role.includes(Role.ADMIN)

    def require(self, minimum: Role) -> None:
        if not self.role.includes(minimum):
            raise PermissionDeniedError(f"This action needs the {minimum} role or higher.")

    def require_can_manage(self, role: Role) -> None:
        """Owners manage everyone, while admins may only add and remove plain members."""
        if not (self.role is Role.OWNER or (self.role is Role.ADMIN and role is Role.MEMBER)):
            raise PermissionDeniedError(f"Only owners can manage members with the {role} role.")
