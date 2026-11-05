"""Fixtures for the suites that run against Postgres: a disposable database migrated to head and
emptied after every test, plus factories for the entities most tests need."""

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from holdfast.core.config import Settings
from holdfast.main import create_app
from holdfast.repositories.records import Base

POSTGRES_IMAGE = "postgres:18-alpine"
PYPROJECT_PATH = Path(__file__).parents[1] / "pyproject.toml"
TIMEZONE = "Europe/Amsterdam"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
BOOKING_DAYS_AHEAD = 7  # Inside the booking horizon, and never in the past.
EMPTY_ALL_TABLES = text(
    f"TRUNCATE {', '.join(table.name for table in Base.metadata.sorted_tables)}"
)

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        alembic_config = Config(toml_file=str(PYPROJECT_PATH))
        alembic_config.attributes["database_url"] = url
        command.upgrade(alembic_config, "head")
        yield url


@asynccontextmanager
async def _serve(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_url=database_url))
    async with _serve(app) as client:
        yield client
        async with app.state.engine.begin() as connection:
            await connection.execute(EMPTY_ALL_TABLES)


@pytest.fixture
async def client_without_database() -> AsyncIterator[AsyncClient]:
    # Nothing listens on port 1, so every connection attempt fails.
    unreachable = "postgresql+asyncpg://holdfast:holdfast@127.0.0.1:1/holdfast"
    async with _serve(create_app(Settings(database_url=unreachable))) as client:
        yield client


@pytest.fixture
def create(client: AsyncClient) -> Callable[[str, Json], Awaitable[Json]]:
    """POST a body and return the created entity, failing the test unless it was created."""

    async def create(path: str, body: Json) -> Json:
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
        created: Json = response.json()
        return created

    return create


@pytest.fixture
def location_request() -> Json:
    """A location open 08:00 to 18:00 every day, so tests can book on any day."""
    return {
        "name": "Central",
        "address": "Damrak 1, Amsterdam",
        "timezone": TIMEZONE,
        "opening_hours": {day: {"opens_at": "08:00", "closes_at": "18:00"} for day in WEEKDAYS},
    }


@pytest.fixture
def resource_request() -> Json:
    return {"name": "Room 1", "kind": "meeting_room", "capacity": 6, "hourly_price": 2000}


@pytest.fixture
def create_user(create: Callable[[str, Json], Awaitable[Json]]) -> Factory:
    async def create_user(**fields: Any) -> Json:
        return await create("/users", {"email": "ada@example.com"} | fields)

    return create_user


@pytest.fixture
def create_organisation(create: Callable[[str, Json], Awaitable[Json]]) -> Factory:
    async def create_organisation(**fields: Any) -> Json:
        body = {"name": "Acme Cowork", "slug": "acme", "currency": "EUR"} | fields
        return await create("/organisations", body)

    return create_organisation


@pytest.fixture
def create_location(
    create: Callable[[str, Json], Awaitable[Json]], location_request: Json
) -> Factory:
    async def create_location(organisation_id: str, **fields: Any) -> Json:
        path = f"/organisations/{organisation_id}/locations"
        return await create(path, location_request | fields)

    return create_location


@pytest.fixture
def create_resource(
    create: Callable[[str, Json], Awaitable[Json]], resource_request: Json
) -> Factory:
    async def create_resource(organisation_id: str, location_id: str, **fields: Any) -> Json:
        path = f"/organisations/{organisation_id}/locations/{location_id}/resources"
        return await create(path, resource_request | fields)

    return create_resource


@pytest.fixture
async def user(create_user: Factory) -> Json:
    return await create_user()


@pytest.fixture
async def organisation(create_organisation: Factory) -> Json:
    return await create_organisation()


@pytest.fixture
async def location(create_location: Factory, organisation: Json) -> Json:
    return await create_location(organisation["id"])


@pytest.fixture
async def resource(create_resource: Factory, organisation: Json, location: Json) -> Json:
    return await create_resource(organisation["id"], location["id"])


def _days_from_today(days: int) -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date() + timedelta(days=days)


@pytest.fixture
def booking_day() -> date:
    return _days_from_today(BOOKING_DAYS_AHEAD)


@pytest.fixture
def at() -> Callable[..., str]:
    """ISO timestamp of a wall-clock time at the location, on the booking day by default."""

    def at(clock_time: str, days_ahead: int = BOOKING_DAYS_AHEAD) -> str:
        day = _days_from_today(days_ahead)
        return datetime.combine(day, time.fromisoformat(clock_time), ZoneInfo(TIMEZONE)).isoformat()

    return at


@pytest.fixture
def book(
    client: AsyncClient, organisation: Json, resource: Json, user: Json, at: Callable[..., str]
) -> Callable[..., Awaitable[Response]]:
    """Book the default resource as the default user, from `start` to `end` on the same day."""

    async def book(start: str, end: str, days_ahead: int = BOOKING_DAYS_AHEAD) -> Response:
        return await client.post(
            f"/organisations/{organisation['id']}/resources/{resource['id']}/bookings",
            json={"start": at(start, days_ahead), "end": at(end, days_ahead)},
            headers={"X-User-ID": user["id"]},
        )

    return book
