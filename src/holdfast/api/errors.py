"""Every error leaves the API as an RFC 9457 problem details document."""

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from fastapi import Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from holdfast.domain.errors import (
    AuthenticationError,
    BookingConflictError,
    ConflictError,
    DomainError,
    InvalidBookingError,
    NotFoundError,
    PermissionDeniedError,
)

PROBLEM_MEDIA_TYPE = "application/problem+json"
_DOCUMENTATION_URL = "https://github.com/remoshan/holdfast-booking-platform#"


@dataclass(frozen=True, slots=True)
class ProblemType:
    """A problem with its own documented meaning, beyond what the HTTP status says."""

    uri: str
    title: str


VALIDATION_ERROR = ProblemType(f"{_DOCUMENTATION_URL}validation-error", "Invalid request")
INVALID_BOOKING = ProblemType(f"{_DOCUMENTATION_URL}invalid-booking", "Booking rule broken")
BOOKING_CONFLICT = ProblemType(f"{_DOCUMENTATION_URL}booking-conflict", "Resource already booked")

# None means "about:blank": the HTTP status alone describes the problem.
_DOMAIN_PROBLEMS: Mapping[type[DomainError], tuple[int, ProblemType | None]] = {
    AuthenticationError: (status.HTTP_401_UNAUTHORIZED, None),
    PermissionDeniedError: (status.HTTP_403_FORBIDDEN, None),
    NotFoundError: (status.HTTP_404_NOT_FOUND, None),
    ConflictError: (status.HTTP_409_CONFLICT, None),
    BookingConflictError: (status.HTTP_409_CONFLICT, BOOKING_CONFLICT),
    InvalidBookingError: (status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_BOOKING),
}

type ProblemHandler = Callable[[Request, Any], Coroutine[Any, Any, Response]]


class Problem(BaseModel):
    """An RFC 9457 problem details document."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    errors: list[dict[str, str]] | None = None


PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    status_range: {
        "description": description,
        "content": {PROBLEM_MEDIA_TYPE: {"schema": Problem.model_json_schema()}},
    }
    for status_range, description in (("4XX", "Client error"), ("5XX", "Server error"))
}


def _problem_response(
    request: Request,
    status_code: int,
    detail: str,
    problem_type: ProblemType | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    problem = Problem(
        type=problem_type.uri if problem_type else "about:blank",
        title=problem_type.title if problem_type else HTTPStatus(status_code).phrase,
        status=status_code,
        detail=detail,
        instance=request.url.path,
        errors=errors,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=status_code,
        headers=headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def _http_problem(request: Request, error: HTTPException) -> Response:
    return _problem_response(request, error.status_code, str(error.detail), headers=error.headers)


async def _validation_problem(request: Request, error: RequestValidationError) -> Response:
    errors = [_field_error(field_error) for field_error in error.errors()]
    return _problem_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "One or more fields are invalid.",
        VALIDATION_ERROR,
        errors,
    )


def _domain_problem(status_code: int, problem_type: ProblemType | None) -> ProblemHandler:
    # RFC 9110 requires every 401 to name the authentication scheme the client should use.
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None

    async def handle(request: Request, error: DomainError) -> Response:
        return _problem_response(request, status_code, str(error), problem_type, headers=headers)

    return handle


async def _unexpected_problem(request: Request, _: Exception) -> Response:
    # The traceback goes to the server log; the client learns nothing about the internals.
    return _problem_response(
        request, status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred."
    )


def _field_error(error: Mapping[str, Any]) -> dict[str, str]:
    """Point at the invalid input: a JSON pointer into the body, or a parameter or header name."""
    source, *path = error["loc"]
    cause = error.get("ctx", {}).get("error")
    detail = str(cause) if error["type"] == "value_error" and cause else error["msg"]
    if source == "body":
        return {"detail": detail, "pointer": "#" + "".join(f"/{_escape(part)}" for part in path)}
    return {"detail": detail, "header" if source == "header" else "parameter": str(path[0])}


def _escape(part: object) -> str:
    """Escape a JSON pointer segment as RFC 6901 requires."""
    return str(part).replace("~", "~0").replace("/", "~1")


PROBLEM_HANDLERS: dict[int | type[Exception], ProblemHandler] = {
    HTTPException: _http_problem,
    RequestValidationError: _validation_problem,
    **{
        error_type: _domain_problem(status_code, problem_type)
        for error_type, (status_code, problem_type) in _DOMAIN_PROBLEMS.items()
    },
    Exception: _unexpected_problem,
}
