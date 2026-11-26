"""The payments service's tables: the ledger of charges, the simulated provider's own books, and the
messaging library's job queue, inbox and outbox."""

from datetime import datetime
from enum import StrEnum, auto
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from holdfast.messaging.records import (
    NAMING_CONVENTION,
    JobRecord,
    OutboxRecord,
    ReceivedMessageRecord,
    add_tables_to,
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ChargeStatus(StrEnum):
    CHARGED = auto()
    DECLINED = auto()
    REFUNDED = auto()


class ChargeRecord(Base):
    """The ledger: the outcome of each booking's charge, kept for good. A row is written only once
    the provider has answered, in the transaction that sends the reply, so every row is final."""

    __tablename__ = "charges"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    booking_id: Mapped[UUID] = mapped_column(primary_key=True)
    amount: Mapped[int]
    """In minor units of the currency."""
    currency: Mapped[str] = mapped_column(String(3))
    # Text guarded by a CHECK constraint, as the library's job statuses are.
    status: Mapped[ChargeStatus] = mapped_column(
        Enum(
            ChargeStatus,
            name="charge_status",
            native_enum=False,
            create_constraint=True,
            length=32,
            values_callable=lambda members: [member.value for member in members],
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProviderChargeRecord(Base):
    """The simulated provider's own books, which only the provider writes, in transactions of its
    own, as an outside system would keep them."""

    __tablename__ = "provider_charges"
    __table_args__ = (
        CheckConstraint("succeeded OR refunded_at IS NULL", name="refunded_only_if_succeeded"),
    )

    idempotency_key: Mapped[UUID] = mapped_column(primary_key=True)
    amount: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    succeeded: Mapped[bool]
    """False for a decline, which is kept too, so that a retry gets the same answer."""
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Copied in, so the migrations, the schema test and the tests' cleanup see the whole database.
add_tables_to(Base.metadata, JobRecord, ReceivedMessageRecord, OutboxRecord)
