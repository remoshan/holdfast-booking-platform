"""Resources: the desks and rooms that people book."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto
from uuid import UUID


class ResourceKind(StrEnum):
    DESK = auto()
    MEETING_ROOM = auto()


@dataclass(frozen=True, slots=True)
class ResourceDetails:
    """The fields of a resource that are set on creation and replaced on update."""

    name: str
    kind: ResourceKind
    capacity: int
    hourly_price: int
    """In minor units of the organisation's currency, such as cents; 0 means free."""


@dataclass(frozen=True, slots=True)
class Resource:
    id: UUID
    organisation_id: UUID
    location_id: UUID
    name: str
    kind: ResourceKind
    capacity: int
    hourly_price: int
    created_at: datetime
