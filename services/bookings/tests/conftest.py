"""Fixtures for the suites that run against Postgres: a disposable database migrated to head and
emptied after every test, signed-in clients, factories for the entities most tests need, and a
mail catcher for the tests that send email."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import date, datetime, time, timedelta
from itertools import count
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher, profiles
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from holdfast.bookings.core import security
from holdfast.bookings.core.config import ApiSettings
from holdfast.bookings.core.mail import Mailer
from holdfast.bookings.main import create_app
from holdfast.bookings.repositories.records import Base
from holdfast.messaging.records import JobRecord, OutboxRecord

POSTGRES_IMAGE = "postgres:18-alpine"
MAILPIT_IMAGE = "axllent/mailpit:v1.33"
# A display name with a comma must be quoted, or the From header splits into two addresses.
MAIL_FROM = '"Holdfast, Inc." <reminders@example.com>'
PYPROJECT_PATH = Path(__file__).parents[1] / "pyproject.toml"
JWT_SECRET = "a-secret-used-only-by-the-test-suite"
PASSWORD = "correct horse battery staple"
TIMEZONE = "Europe/Amsterdam"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
BOOKING_DAYS_AHEAD = 7  # Inside the booking horizon, and never in the past.
LOCK_WAIT_SECONDS = 10

type Json = dict[str, Any]
type Factory = Callable[..., Awaitable[Json]]


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2's production settings make every hash take a noticeable moment, by design. The
    suite signs up hundreds of users, so it hashes with the cheapest settings instead."""
    cheapest = PasswordHasher.from_parameters(profiles.CHEAPEST)
    monkeypatch.setattr(security, "_password_hasher", cheapest)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        command.upgrade(_alembic_config(url), "head")
        yield url


@pytest.fixture
async def alembic_config(database_url: str) -> AsyncIterator[Config]:
    """Runs migrations against the test database. Alembic's env.py starts an event loop of its
    own, so async tests call its commands through asyncio.to_thread. A test that fails while
    the database is at another revision would break every test after it, so the schema is
    rebuilt from scratch afterwards."""
    config = _alembic_config(database_url)
    yield config
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.upgrade, config, "head")


def _alembic_config(database_url: str) -> Config:
    config = Config(toml_file=str(PYPROJECT_PATH))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture(scope="session")
def mailpit() -> Iterator[DockerContainer]:
    """A mail catcher, started only for the tests that send email."""
    container = (
        DockerContainer(MAILPIT_IMAGE)
        .with_exposed_ports(1025, 8025)
        .waiting_for(HttpWaitStrategy(8025, "/readyz"))
    )
    with container:
        yield container


@pytest.fixture
def mailer(mailpit: DockerContainer) -> Mailer:
    """Sends email to the mail catcher."""
    return Mailer(mailpit.get_container_host_ip(), mailpit.get_exposed_port(1025), MAIL_FROM)


@pytest.fixture
async def inbox(mailpit: DockerContainer) -> AsyncIterator[Callable[[], Awaitable[list[Json]]]]:
    """Read the emails that arrived during the test, newest first. It starts empty."""
    api_url = f"http://{mailpit.get_container_host_ip()}:{mailpit.get_exposed_port(8025)}"
    async with AsyncClient(base_url=api_url) as api:
        (await api.delete("/api/v1/messages")).raise_for_status()

        async def inbox() -> list[Json]:
            messages: list[Json] = (
                (await api.get("/api/v1/messages")).raise_for_status().json()["messages"]
            )
            return messages

        yield inbox


def _client(app: FastAPI, headers: dict[str, str] | None = None) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


@pytest.fixture
async def app(database_url: str) -> AsyncIterator[FastAPI]:
    app = create_app(ApiSettings(database_url=database_url, jwt_secret=JWT_SECRET))
    async with app.router.lifespan_context(app):
        yield app
        async with app.state.engine.begin() as connection:
            await connection.run_sync(_empty_all_tables)


def _empty_all_tables(connection: Connection) -> None:
    """Empty every table the records describe that exists. A migration test can leave the schema
    at an older revision, without the newer tables, until its own cleanup rebuilds the schema."""
    existing = set(inspect(connection).get_table_names())
    tables = [table.name for table in Base.metadata.sorted_tables if table.name in existing]
    connection.execute(text(f"TRUNCATE {', '.join(tables)}"))


@pytest.fixture
def stored_jobs(app: FastAPI) -> Callable[[], Awaitable[list[JobRecord]]]:
    """Every job in the table, dead ones included, the earliest due first."""

    async def stored_jobs() -> list[JobRecord]:
        async with app.state.sessions() as session:
            return list(await session.scalars(select(JobRecord).order_by(JobRecord.run_at)))

    return stored_jobs


@pytest.fixture
def published(app: FastAPI) -> Callable[[], Awaitable[list[tuple[str, Json]]]]:
    """The events waiting in the outbox, the oldest first, each as its type and data."""

    async def published() -> list[tuple[str, Json]]:
        async with app.state.sessions() as session:
            rows = await session.scalars(select(OutboxRecord).order_by(OutboxRecord.id))
            return [(row.type, row.data) for row in rows]

    return published


@pytest.fixture
def until_a_query_waits_for_a_lock(app: FastAPI) -> Callable[[], Awaitable[None]]:
    """Wait until a query is blocked on a lock, such as one that the test's open transaction
    holds, so the test knows a concurrent request has reached that point."""

    async def until_a_query_waits_for_a_lock() -> None:
        async with app.state.sessions() as session, asyncio.timeout(LOCK_WAIT_SECONDS):
            while True:
                waiting = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND backend_type = 'client backend' AND wait_event_type = 'Lock'"
                    )
                )
                if waiting:
                    return
                await session.rollback()  # the next count takes a fresh snapshot
                await asyncio.sleep(0.05)

    return until_a_query_waits_for_a_lock


@pytest.fixture
async def anonymous_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with _client(app) as client:
        yield client


@pytest.fixture
async def client_without_database() -> AsyncIterator[AsyncClient]:
    # Nothing listens on port 1, so every connection attempt fails.
    unreachable = "postgresql+asyncpg://holdfast:holdfast@127.0.0.1:1/holdfast"
    app = create_app(ApiSettings(database_url=unreachable, jwt_secret=JWT_SECRET))
    async with app.router.lifespan_context(app), _client(app) as client:
        yield client


@pytest.fixture
def create_user(anonymous_client: AsyncClient) -> Factory:
    """Register and log in a user. The result carries their tokens and an Authorization header."""

    async def create_user(email: str = "ada@example.com") -> Json:
        credentials = {"email": email, "password": PASSWORD}
        registered = await anonymous_client.post("/auth/register", json=credentials)
        assert registered.status_code == 201, registered.text
        tokens = (await anonymous_client.post("/auth/login", json=credentials)).json()
        account: Json = registered.json()
        return account | {
            "headers": {"Authorization": f"Bearer {tokens['access_token']}"},
            "refresh_token": tokens["refresh_token"],
        }

    return create_user


@pytest.fixture
async def user(create_user: Factory) -> Json:
    """The user most tests act as. They create, and so own, the default organisation."""
    return await create_user()


@pytest.fixture
async def client(app: FastAPI, user: Json) -> AsyncIterator[AsyncClient]:
    """A client signed in as `user`."""
    async with _client(app, user["headers"]) as client:
        yield client


@pytest.fixture
def create(client: AsyncClient) -> Callable[[str, Json], Awaitable[Json]]:
    """POST a body as `user` and return the created entity, failing unless it was created."""

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
def add_member(create: Callable[[str, Json], Awaitable[Json]], create_user: Factory) -> Factory:
    """Sign up a new user and have `user` add them to an organisation with the given role."""
    numbers = count(1)

    async def add_member(organisation_id: str, role: str) -> Json:
        member = await create_user(email=f"{role}-{next(numbers)}@example.com")
        await create(
            f"/organisations/{organisation_id}/members", {"email": member["email"], "role": role}
        )
        return member

    return add_member


@pytest.fixture
async def organisation(create_organisation: Factory) -> Json:
    return await create_organisation()


@pytest.fixture
async def location(create_location: Factory, organisation: Json) -> Json:
    return await create_location(organisation["id"])


@pytest.fixture
async def resource(create_resource: Factory, organisation: Json, location: Json) -> Json:
    """A meeting room that costs something, so its bookings are held until confirmed."""
    return await create_resource(organisation["id"], location["id"])


@pytest.fixture
async def free_resource(create_resource: Factory, organisation: Json, location: Json) -> Json:
    """A desk that costs nothing, so its bookings are confirmed at once."""
    return await create_resource(
        organisation["id"], location["id"], name="Desk 1", kind="desk", capacity=1, hourly_price=0
    )


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
    client: AsyncClient, organisation: Json, resource: Json, at: Callable[..., str]
) -> Callable[..., Awaitable[Response]]:
    """Book the default resource, or the one given, as `user` or as whoever the headers sign in.
    Other headers, such as an Idempotency-Key, are sent along."""

    async def book(
        start: str,
        end: str,
        days_ahead: int = BOOKING_DAYS_AHEAD,
        headers: dict[str, str] | None = None,
        resource_id: str | None = None,
    ) -> Response:
        return await client.post(
            f"/organisations/{organisation['id']}/resources/{resource_id or resource['id']}"
            "/bookings",
            json={"start": at(start, days_ahead), "end": at(end, days_ahead)},
            headers=headers,
        )

    return book
