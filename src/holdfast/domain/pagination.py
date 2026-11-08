"""A page of results from a list query."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: Sequence[T]
    next_after: UUID | None
    """The key the next page continues after, or None on the last page."""
