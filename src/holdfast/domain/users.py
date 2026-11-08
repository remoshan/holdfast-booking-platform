"""People who book resources, and the sign-in sessions their refresh tokens belong to."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    email: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RefreshTokenFamily:
    """The chain of refresh tokens one sign-in produces, each token replacing the one before."""

    id: UUID
    user_id: UUID
