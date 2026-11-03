"""Organisations: the tenants of Holdfast, such as one co-working company."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OrganisationDetails:
    """The fields of an organisation that can change after it is created."""

    name: str
    slug: str


@dataclass(frozen=True, slots=True)
class Organisation:
    id: UUID
    name: str
    slug: str
    currency: str
    """ISO 4217 code shared by every price in the organisation, so it never changes."""
    created_at: datetime
