"""Expected failures raised by the domain and services; the API maps each one to an HTTP problem."""

from uuid import UUID


class DomainError(Exception):
    """A failure caused by the request rather than by a bug."""


class AuthenticationError(DomainError):
    """The caller could not be identified: missing, wrong or expired credentials."""


class PermissionDeniedError(DomainError):
    """The caller belongs to the organisation, but their role does not allow the action."""


class NotFoundError(DomainError):
    def __init__(self, entity: str, key: UUID | str) -> None:
        super().__init__(f"{entity} {key} was not found.")


class ConflictError(DomainError):
    """The request clashes with the current state, such as a slug that is already taken."""


class InvalidBookingError(DomainError):
    """The requested booking breaks one of the booking rules."""


class BookingConflictError(ConflictError):
    """The requested period overlaps an active booking of the same resource."""


class IdempotencyKeyReusedError(DomainError):
    """An idempotency key that already made one booking was sent with a different request."""
