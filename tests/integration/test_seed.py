"""The seed script that `make seed` runs, at a tiny scale."""

import pytest
from fastapi import status
from httpx import AsyncClient

from scripts import seed

pytestmark = pytest.mark.anyio


async def test_seed_fills_every_table_with_data_the_api_can_use(
    anonymous_client: AsyncClient, database_url: str
) -> None:
    counts = await seed.seed(database_url, organisation_count=2, history_days=3)

    assert all(counts.values()), counts
    credentials = {"email": "user-00001@example.com", "password": seed.PASSWORD}
    tokens = (await anonymous_client.post("/auth/login", json=credentials)).json()
    upcoming = await anonymous_client.get(
        "/users/me/bookings", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert upcoming.status_code == status.HTTP_200_OK


@pytest.mark.usefixtures("user")
async def test_seed_refuses_a_database_that_holds_data(database_url: str) -> None:
    with pytest.raises(seed.NotEmptyError):
        await seed.seed(database_url, organisation_count=1, history_days=0)
