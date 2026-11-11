"""Application settings, read from environment variables."""

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """What every process needs: the API, the worker and the migrations."""

    model_config = SettingsConfigDict(frozen=True)

    database_url: PostgresDsn
    """SQLAlchemy URL using the asyncpg driver, e.g. postgresql+asyncpg://user:pass@host/db."""


class ApiSettings(Settings):
    jwt_secret: SecretStr = Field(min_length=32)
    """Key that signs access tokens: at least 32 random characters, different per environment."""


class WorkerSettings(Settings):
    smtp_host: str
    smtp_port: int
    mail_from: str
    """The address that reminders come from, e.g. Holdfast <bookings@example.com>."""
