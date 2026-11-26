"""Settings for each process, read from environment variables. Each process gets only what it
needs, so the worker never holds the key that signs access tokens."""

from pydantic import AmqpDsn, Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """What every process needs, migrations included."""

    model_config = SettingsConfigDict(frozen=True)

    database_url: PostgresDsn
    """SQLAlchemy URL using the asyncpg driver, e.g. postgresql+asyncpg://user:pass@host/db."""


class ApiSettings(DatabaseSettings):
    jwt_secret: SecretStr = Field(min_length=32)
    """Key that signs access tokens: at least 32 random characters, different per environment."""


class WorkerSettings(DatabaseSettings):
    rabbitmq_url: AmqpDsn
    """The broker the worker publishes the service's events to, e.g. amqp://user:pass@host:5672/."""
