"""Expected failures raised by the domain and services; the API maps each one to an HTTP problem."""

from uuid import UUID


class DomainError(Exception):
    """A failure caused by the request rather than by a bug."""


class NotFoundError(DomainError):
    def __init__(self, entity: str, entity_id: UUID) -> None:
        super().__init__(f"{entity} {entity_id} was not found.")


class ConflictError(DomainError):
    """The request clashes with the current state, such as a slug that is already taken."""


class InvalidBookingError(DomainError):
    """The requested booking breaks one of the booking rules."""


class BookingConflictError(ConflictError):
    """The requested period overlaps an active booking of the same resource."""
