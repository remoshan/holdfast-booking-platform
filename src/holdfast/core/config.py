"""Application settings, read from environment variables."""

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    database_url: PostgresDsn
    """SQLAlchemy URL using the asyncpg driver, e.g. postgresql+asyncpg://user:pass@host/db."""
    jwt_secret: SecretStr = Field(min_length=32)
    """Key that signs access tokens: at least 32 random characters, different per environment."""
