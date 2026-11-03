import pytest
from fastapi import status
from httpx import AsyncClient

pytestmark = pytest.mark.anyio


async def test_health_reports_ok_when_database_is_reachable(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


async def test_health_reports_unavailable_when_database_is_unreachable(
    client_without_database: AsyncClient,
) -> None:
    response = await client_without_database.get("/health")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {"status": "unavailable"}
