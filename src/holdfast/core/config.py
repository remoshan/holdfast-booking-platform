"""Settings for each process, read from environment variables. Each process gets only what it
needs, so the worker never holds the key that signs access tokens."""

from pydantic import Field, NameEmail, PostgresDsn, SecretStr
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
    smtp_host: str
    """Mail server that relays reminder emails, such as a local Mailpit."""
    smtp_port: int = Field(ge=1, le=65535)
    mail_from: NameEmail
    """Sender of every email, e.g. Holdfast <reminders@example.com>."""
