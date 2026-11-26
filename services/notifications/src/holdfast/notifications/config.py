"""The notifications service's settings, read from environment variables."""

from email.headerregistry import Address

from pydantic import AmqpDsn, Field, NameEmail, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """What every process needs, migrations included."""

    model_config = SettingsConfigDict(frozen=True)

    database_url: PostgresDsn
    """SQLAlchemy URL using the asyncpg driver, e.g. postgresql+asyncpg://user:pass@host/db."""


class WorkerSettings(DatabaseSettings):
    rabbitmq_url: AmqpDsn
    """The broker the worker receives the booking service's events from."""
    smtp_host: str = Field(min_length=1)
    """Mail server that relays the emails, such as a local Mailpit."""
    smtp_port: int = Field(ge=1, le=65535)
    mail_from: NameEmail
    """Sender of every email, e.g. Holdfast <bookings@example.com>."""

    @property
    def sender(self) -> str:
        """MAIL_FROM as a From header. str() of a NameEmail drops the quotes that some display
        names need: a comma would split the header into two addresses, and a colon would make
        the text before it a group name. The address goes in as its two parts: formataddr refuses
        any non-ASCII address, and Address(addr_spec=...) parses it again and rejects a non-ASCII
        local part, both of which NameEmail accepts."""
        username, _, domain = self.mail_from.email.rpartition("@")
        return str(Address(self.mail_from.name, username, domain))
