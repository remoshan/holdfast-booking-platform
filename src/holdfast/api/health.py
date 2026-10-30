"""Health check: reports whether the API can reach its database."""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

DATABASE_CHECK_TIMEOUT_SECONDS = 2

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: Literal["ok", "unavailable"]


@router.get(
    "/health",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthStatus}},
)
async def check_health(request: Request, response: Response) -> HealthStatus:
    engine: AsyncEngine = request.app.state.engine
    try:
        async with asyncio.timeout(DATABASE_CHECK_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    # OSError covers refused connections and TimeoutError, which subclasses it.
    except SQLAlchemyError, OSError:
        logger.warning("Database health check failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(status="unavailable")
    return HealthStatus(status="ok")
