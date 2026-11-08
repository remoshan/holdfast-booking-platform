"""Application factory: builds the FastAPI app and owns the database engine's lifetime."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from holdfast.api import (
    auth,
    bookings,
    health,
    locations,
    memberships,
    organisations,
    resources,
    users,
)
from holdfast.api.errors import PROBLEM_HANDLERS, PROBLEM_RESPONSES
from holdfast.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(str(settings.database_url))
        app.state.engine = engine
        app.state.sessions = async_sessionmaker(engine)
        yield
        await engine.dispose()

    app = FastAPI(
        title="Holdfast",
        summary="Multi-tenant booking for shared spaces that never double-books.",
        version=metadata.version("holdfast"),
        lifespan=lifespan,
        exception_handlers=PROBLEM_HANDLERS,
        responses=PROBLEM_RESPONSES,
    )
    app.state.settings = settings
    for router in (
        health.router,
        auth.router,
        users.router,
        organisations.router,
        memberships.router,
        locations.router,
        resources.router,
        bookings.router,
    ):
        app.include_router(router)
    return app
