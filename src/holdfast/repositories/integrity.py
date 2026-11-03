"""Turning constraint violations reported by Postgres into domain errors."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from holdfast.domain.errors import DomainError

# Postgres SQLSTATE codes: https://www.postgresql.org/docs/current/errcodes-appendix.html
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"


@contextmanager
def violation_raises(sqlstate: str, error: DomainError) -> Iterator[None]:
    """Raise `error` if the wrapped statements break a constraint of the given kind.

    Letting the database reject the write, rather than checking first, leaves no gap for a
    concurrent request to slip through between the check and the write.
    """
    try:
        yield
    except IntegrityError as integrity_error:
        if getattr(integrity_error.orig, "sqlstate", None) == sqlstate:
            raise error from integrity_error
        raise
