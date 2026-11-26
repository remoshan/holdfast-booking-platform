"""The payments service's settings, read from environment variables."""

from pydantic import AmqpDsn, Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """What every process needs, migrations included."""

    model_config = SettingsConfigDict(frozen=True)

    database_url: PostgresDsn
    """SQLAlchemy URL using the asyncpg driver, e.g. postgresql+asyncpg://user:pass@host/db."""


class WorkerSettings(DatabaseSettings):
    rabbitmq_url: AmqpDsn
    """The broker the worker takes commands from and publishes their outcomes to."""
    provider_delay_seconds: float = Field(default=0, ge=0)
    """How long the simulated provider takes to answer the first call for each booking. A retry is
    answered at once, so a delay past PROVIDER_TIMEOUT makes every first call time out after the
    provider acted, and its retry find what was done."""
    provider_failure_rate: float = Field(default=0, ge=0, le=1)
    """The share of calls the simulated provider refuses as unavailable, before it does anything."""
