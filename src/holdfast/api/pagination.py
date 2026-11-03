"""Opaque keyset cursors and the page envelope every list endpoint returns."""

import base64
from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from holdfast.domain.pagination import Page

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class _Identified(Protocol):
    @property
    def id(self) -> UUID: ...


@dataclass(frozen=True, slots=True)
class PageRequest:
    after: UUID | None
    limit: int


class PageResponse[ItemT](BaseModel):
    items: list[ItemT]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )


def _page_request(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(description="`next_cursor` of the previous page.")] = None,
) -> PageRequest:
    return PageRequest(after=None if cursor is None else _decode(cursor), limit=limit)


PageQuery = Annotated[PageRequest, Depends(_page_request)]


def next_cursor[ItemT: _Identified](page: Page[ItemT]) -> str | None:
    """Clients treat cursors as opaque, so the server can change how they work at any time."""
    if not page.has_more:
        return None
    return base64.urlsafe_b64encode(page.items[-1].id.bytes).rstrip(b"=").decode()


def _decode(cursor: str) -> UUID:
    try:
        return UUID(bytes=base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except ValueError as error:
        invalid = {"type": "value_error", "loc": ("query", "cursor"), "msg": "Invalid cursor."}
        raise RequestValidationError([invalid]) from error
