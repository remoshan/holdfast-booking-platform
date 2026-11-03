"""A page of results from a list query."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: Sequence[T]
    has_more: bool
