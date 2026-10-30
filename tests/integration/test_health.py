import pytest
from fastapi import status
from httpx import AsyncClient

from holdfast.core.config import Settings

pytestmark = pytest.mark.anyio

# Nothing listens on port 1, so every connection attempt fails.
UNREACHABLE_DATABASE = Settings(
    database_url="postgresql+asyncpg://holdfast:holdfast@127.0.0.1:1/holdfast"
)


async def test_health_reports_ok_when_database_is_reachable(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("settings", [UNREACHABLE_DATABASE])
async def test_health_reports_unavailable_when_database_is_unreachable(
    client: AsyncClient,
) -> None:
    response = await client.get("/health")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {"status": "unavailable"}
