import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from holdfast.core.config import Settings
from holdfast.main import create_app

pytestmark = pytest.mark.anyio

PROBLEM_MEDIA_TYPE = "application/problem+json"


async def test_unknown_route_returns_a_problem(client: AsyncClient) -> None:
    response = await client.get("/nowhere")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
        "instance": "/nowhere",
    }


async def test_wrong_method_returns_a_problem_that_lists_the_allowed_methods(
    client: AsyncClient,
) -> None:
    response = await client.delete("/auth/login")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["Allow"] == "POST"


async def test_validation_problems_point_at_each_invalid_input(client: AsyncClient) -> None:
    response = await client.post(
        "/organisations",
        json={"name": "Acme", "slug": "Not A Slug", "currency": "EUR", "colour": "red"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    problem = response.json()
    assert problem["type"].endswith("#validation-error")
    assert [error.get("pointer") for error in problem["errors"]] == ["#/slug", "#/colour"]


async def test_openapi_documents_errors_as_problem_details(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    responses = schema["paths"]["/organisations"]["post"]["responses"]
    assert list(responses["4XX"]["content"]) == [PROBLEM_MEDIA_TYPE]
    assert "422" not in responses  # FastAPI's own validation error format is never returned


async def test_unexpected_errors_hide_their_details() -> None:
    app = create_app(
        Settings(
            database_url="postgresql+asyncpg://unused@localhost/unused",
            jwt_secret="a-secret-used-only-by-the-test-suite",
        )
    )

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("connection string with a password")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/explode")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["detail"] == "An unexpected error occurred."
    assert "password" not in response.text
