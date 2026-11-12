"""Every endpoint inside an organisation, called by every kind of caller.

Anonymous callers get 401 and outsiders 404, so outsiders learn nothing about the organisation.
Members whose role is too low get 403, except for other people's bookings, which a plain member
is told do not exist. A test fails when an endpoint is added without an entry in the matrix.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI, status
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]

ROLE_RANKS = {"member": 1, "admin": 2, "owner": 3}
CALLERS = ("anonymous", "outsider", "member", "admin", "owner")
ORGANISATION = "/organisations/{organisation_id}"


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    minimum_role: str
    success: int
    body: str | None = None
    denied: int = status.HTTP_403_FORBIDDEN

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


ENDPOINTS = (
    Endpoint("GET", ORGANISATION, "member", 200),
    Endpoint("PUT", ORGANISATION, "owner", 200, body="organisation"),
    Endpoint("DELETE", ORGANISATION, "owner", 204),
    Endpoint("GET", ORGANISATION + "/members", "admin", 200),
    Endpoint("POST", ORGANISATION + "/members", "admin", 201, body="new member"),
    Endpoint("GET", ORGANISATION + "/members/{user_id}", "admin", 200),
    Endpoint("PUT", ORGANISATION + "/members/{user_id}", "owner", 200, body="promotion"),
    Endpoint("DELETE", ORGANISATION + "/members/{user_id}", "admin", 204),
    Endpoint("POST", ORGANISATION + "/locations", "admin", 201, body="location"),
    Endpoint("GET", ORGANISATION + "/locations", "member", 200),
    Endpoint("GET", ORGANISATION + "/locations/{location_id}", "member", 200),
    Endpoint("PUT", ORGANISATION + "/locations/{location_id}", "admin", 200, body="location"),
    Endpoint("DELETE", ORGANISATION + "/locations/{location_id}", "admin", 204),
    Endpoint(
        "POST", ORGANISATION + "/locations/{location_id}/resources", "admin", 201, body="resource"
    ),
    Endpoint("GET", ORGANISATION + "/locations/{location_id}/resources", "member", 200),
    Endpoint("GET", ORGANISATION + "/resources/{resource_id}", "member", 200),
    Endpoint("PUT", ORGANISATION + "/resources/{resource_id}", "admin", 200, body="resource"),
    Endpoint("DELETE", ORGANISATION + "/resources/{resource_id}", "admin", 204),
    Endpoint("GET", ORGANISATION + "/resources/{resource_id}/availability", "member", 200),
    Endpoint(
        "POST", ORGANISATION + "/resources/{resource_id}/bookings", "member", 201, body="booking"
    ),
    Endpoint("GET", ORGANISATION + "/resources/{resource_id}/bookings", "member", 200),
    Endpoint("GET", ORGANISATION + "/bookings/{booking_id}", "admin", 200, denied=404),
    Endpoint("POST", ORGANISATION + "/bookings/{booking_id}/confirm", "admin", 200, denied=404),
    Endpoint("POST", ORGANISATION + "/bookings/{booking_id}/cancel", "admin", 200, denied=404),
)
PUBLIC_ENDPOINTS = {
    ("GET", "/health"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/logout"),
}
ENDPOINTS_OUTSIDE_ORGANISATIONS = {
    ("GET", "/users/me"),
    ("GET", "/users/me/bookings"),
    ("POST", "/organisations"),
    ("GET", "/organisations"),
}


def _expected_status(endpoint: Endpoint, caller: str) -> int:
    if caller == "anonymous":
        return status.HTTP_401_UNAUTHORIZED
    if caller == "outsider":
        return status.HTTP_404_NOT_FOUND
    if ROLE_RANKS[caller] < ROLE_RANKS[endpoint.minimum_role]:
        return endpoint.denied
    return endpoint.success


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=str)
async def test_each_caller_gets_exactly_the_access_their_role_allows(
    endpoint: Endpoint,
    caller: str,
    anonymous_client: AsyncClient,
    user: Json,
    create: Callable[[str, Json], Awaitable[Json]],
    create_user: Factory,
    add_member: Factory,
    location_request: Json,
    resource_request: Json,
    booking_day: date,
    at: Callable[..., str],
) -> None:
    booking_request = {"start": at("09:00"), "end": at("10:00")}
    organisation = await create(
        "/organisations", {"name": "Acme", "slug": "acme", "currency": "EUR"}
    )
    ids = await _create_what_the_path_needs(
        endpoint.path,
        organisation["id"],
        create,
        add_member,
        location_request,
        resource_request,
        booking_request,
    )
    if endpoint.body == "new member":
        await create_user(email="candidate@example.com")
    bodies = {
        "organisation": {"name": "Renamed", "slug": "renamed"},
        "new member": {"email": "candidate@example.com", "role": "member"},
        "promotion": {"role": "admin"},
        "location": location_request,
        "resource": resource_request,
        "booking": booking_request,
    }

    response = await anonymous_client.request(
        endpoint.method,
        endpoint.path.format(**ids),
        json=bodies[endpoint.body] if endpoint.body else None,
        params={"date": booking_day.isoformat()}
        if endpoint.path.endswith("availability")
        else None,
        headers=await _sign_in_as(caller, organisation["id"], user, create_user, add_member),
    )

    assert response.status_code == _expected_status(endpoint, caller), response.text


async def _create_what_the_path_needs(
    path: str,
    organisation_id: str,
    create: Callable[[str, Json], Awaitable[Json]],
    add_member: Factory,
    location_request: Json,
    resource_request: Json,
    booking_request: Json,
) -> dict[str, str]:
    """Create only the entities the path names, so that deleting a location or a resource is
    never blocked by children the endpoint does not care about."""
    ids = {"organisation_id": organisation_id}
    base = f"/organisations/{organisation_id}"
    needs_resource = "{resource_id}" in path or "{booking_id}" in path
    if needs_resource or "{location_id}" in path:
        ids["location_id"] = (await create(f"{base}/locations", location_request))["id"]
    if needs_resource:
        resources_path = f"{base}/locations/{ids['location_id']}/resources"
        ids["resource_id"] = (await create(resources_path, resource_request))["id"]
    if "{booking_id}" in path:
        bookings_path = f"{base}/resources/{ids['resource_id']}/bookings"
        ids["booking_id"] = (await create(bookings_path, booking_request))["id"]
    if "{user_id}" in path:
        ids["user_id"] = (await add_member(organisation_id, "member"))["id"]
    return ids


async def _sign_in_as(
    caller: str, organisation_id: str, owner: Json, create_user: Factory, add_member: Factory
) -> dict[str, str]:
    if caller == "anonymous":
        return {}
    if caller == "outsider":
        outsider = await create_user(email="outsider@example.com")
        return dict(outsider["headers"])
    if caller == "owner":
        return dict(owner["headers"])
    member = await add_member(organisation_id, caller)
    return dict(member["headers"])


async def test_every_endpoint_is_in_the_matrix_or_listed_as_outside_it(app: FastAPI) -> None:
    documented = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }

    in_matrix = {(endpoint.method, endpoint.path) for endpoint in ENDPOINTS}
    assert documented - PUBLIC_ENDPOINTS - ENDPOINTS_OUTSIDE_ORGANISATIONS == in_matrix


@pytest.mark.parametrize(("method", "path"), sorted(ENDPOINTS_OUTSIDE_ORGANISATIONS))
async def test_endpoints_outside_organisations_need_a_token(
    anonymous_client: AsyncClient, method: str, path: str
) -> None:
    response = await anonymous_client.request(method, path)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
