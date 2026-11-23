from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import status
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]


async def test_add_member(client: AsyncClient, organisation: Json, create_user: Factory) -> None:
    newcomer = await create_user(email="grace@example.com")

    response = await client.post(
        f"/organisations/{organisation['id']}/members", json={"email": "Grace@Example.com"}
    )

    assert response.status_code == status.HTTP_201_CREATED
    member = response.json()
    assert (member["user_id"], member["email"], member["role"]) == (
        newcomer["id"],
        "grace@example.com",
        "member",
    )
    assert response.headers["Location"] == (
        f"http://test/organisations/{organisation['id']}/members/{newcomer['id']}"
    )


async def test_add_member_needs_an_existing_account(
    client: AsyncClient, organisation: Json
) -> None:
    response = await client.post(
        f"/organisations/{organisation['id']}/members", json={"email": "nobody@example.com"}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_add_member_twice_is_rejected(
    client: AsyncClient, organisation: Json, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")

    response = await client.post(
        f"/organisations/{organisation['id']}/members", json={"email": member["email"]}
    )

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_admins_add_members_but_not_admins(
    client: AsyncClient, organisation: Json, add_member: Factory, create_user: Factory
) -> None:
    admin = await add_member(organisation["id"], "admin")
    await create_user(email="grace@example.com")
    path = f"/organisations/{organisation['id']}/members"

    as_admin = await client.post(
        path, json={"email": "grace@example.com", "role": "admin"}, headers=admin["headers"]
    )

    assert as_admin.status_code == status.HTTP_403_FORBIDDEN
    assert as_admin.json()["detail"] == "Only owners can manage members with the admin role."


async def test_owner_changes_a_members_role(
    client: AsyncClient, organisation: Json, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")

    response = await client.put(
        f"/organisations/{organisation['id']}/members/{member['id']}", json={"role": "admin"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["role"] == "admin"


async def test_the_last_owner_cannot_step_down(
    client: AsyncClient, organisation: Json, user: Json
) -> None:
    path = f"/organisations/{organisation['id']}/members/{user['id']}"

    demoted = await client.put(path, json={"role": "admin"})
    removed = await client.delete(path)

    assert (demoted.status_code, removed.status_code) == (409, 409)
    assert removed.json()["detail"] == "An organisation must keep at least one owner."


async def test_an_owner_can_step_down_while_another_remains(
    client: AsyncClient, organisation: Json, user: Json, add_member: Factory
) -> None:
    await add_member(organisation["id"], "owner")

    response = await client.delete(f"/organisations/{organisation['id']}/members/{user['id']}")

    assert response.status_code == status.HTTP_204_NO_CONTENT


async def test_admins_remove_members_but_not_other_admins(
    client: AsyncClient, organisation: Json, add_member: Factory
) -> None:
    admin = await add_member(organisation["id"], "admin")
    other_admin = await add_member(organisation["id"], "admin")
    member = await add_member(organisation["id"], "member")
    path = f"/organisations/{organisation['id']}/members"

    removed_member = await client.delete(f"{path}/{member['id']}", headers=admin["headers"])
    removed_admin = await client.delete(f"{path}/{other_admin['id']}", headers=admin["headers"])

    assert removed_member.status_code == status.HTTP_204_NO_CONTENT
    assert removed_admin.status_code == status.HTTP_403_FORBIDDEN


async def test_a_removed_member_loses_access(
    client: AsyncClient, organisation: Json, add_member: Factory
) -> None:
    member = await add_member(organisation["id"], "member")

    await client.delete(f"/organisations/{organisation['id']}/members/{member['id']}")
    response = await client.get(f"/organisations/{organisation['id']}", headers=member["headers"])

    assert response.status_code == status.HTTP_404_NOT_FOUND
