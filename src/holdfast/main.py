"""Application factory: builds the FastAPI app and owns the database engine's lifetime."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from holdfast.api.health import router as health_router
from holdfast.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = create_async_engine(str(settings.database_url))
        yield
        await app.state.engine.dispose()

    app = FastAPI(title="Holdfast", version=metadata.version("holdfast"), lifespan=lifespan)
    app.include_router(health_router)
    return app
