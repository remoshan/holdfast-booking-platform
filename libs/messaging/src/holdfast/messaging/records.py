"""The tables the library owns, on its own MetaData. Each service copies the ones it uses into its
own MetaData with add_tables_to, so that its migrations, its schema test and its test cleanup see
the whole of its database."""

from datetime import datetime
from enum import StrEnum, auto
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, Index, MetaData, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can reference and drop them reliably. Every
# service builds its own MetaData with them, so a copied table keeps the names it has here.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class JobStatus(StrEnum):
    QUEUED = auto()
    DEAD = auto()
    """Failed MAX_ATTEMPTS times, or a delivery that could not be read. The job is kept, with its
    last error, until someone requeues or deletes it."""


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JobRecord(Base):
    """Work that runs after the transaction that queued it commits: a service's own jobs, and the
    messages it received."""

    __tablename__ = "jobs"
    __table_args__ = (
        # Workers look only at queued jobs, the most overdue first.
        Index(
            "ix_jobs_queued_run_at",
            "run_at",
            postgresql_where=text(f"status = '{JobStatus.QUEUED}'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.uuidv7())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Text guarded by a CHECK constraint, which is easier to extend than a native Postgres enum.
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="job_status",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    attempts: Mapped[int]
    """How many times the job has failed."""
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None]


class ReceivedMessageRecord(Base):
    """The id of every message the service received in the last RETENTION, so that a copy of one
    is recognised and dropped."""

    __tablename__ = "received_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutboxRecord(Base):
    """A message waiting to be published, written in the transaction of the change that sends it."""

    __tablename__ = "outbox"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.uuidv7())
    type: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def add_tables_to(metadata: MetaData, *records: type[Base]) -> None:
    """Copy the tables of these records into a service's MetaData. The library's queries keep
    using its own mappings, which name the same tables."""
    for record in records:
        Base.metadata.tables[record.__tablename__].to_metadata(metadata)
