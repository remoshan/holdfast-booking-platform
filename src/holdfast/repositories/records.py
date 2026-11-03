"""SQLAlchemy table mappings. Repositories translate these records into domain entities."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from holdfast.domain.bookings import BookingStatus
from holdfast.domain.resources import ResourceKind

# Deterministic constraint names, so migrations can reference and drop them reliably.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class EntityRecord(Base):
    """Columns every entity table shares: a UUIDv7 key and a creation timestamp."""

    __abstract__ = True

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.uuidv7())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _string_enum(enum: type[StrEnum], name: str) -> Enum:
    """Store enum values as text guarded by a CHECK constraint, which is easier to extend later
    than a native Postgres enum."""
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [member.value for member in members],
    )


class UserRecord(EntityRecord):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(unique=True)


class OrganisationRecord(EntityRecord):
    __tablename__ = "organisations"

    name: Mapped[str]
    slug: Mapped[str] = mapped_column(unique=True)
    currency: Mapped[str] = mapped_column(String(3))


class LocationRecord(EntityRecord):
    __tablename__ = "locations"
    # Child tables reference (organisation_id, id), so a row can never point into another tenant.
    __table_args__ = (UniqueConstraint("organisation_id", "id"),)

    organisation_id: Mapped[UUID] = mapped_column(ForeignKey("organisations.id"))
    name: Mapped[str]
    address: Mapped[str]
    timezone: Mapped[str]
    # A small value object that is always read and replaced as a whole, so it lives in one column.
    opening_hours: Mapped[dict[str, dict[str, str]]] = mapped_column(JSONB)


class ResourceRecord(EntityRecord):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("organisation_id", "id"),
        ForeignKeyConstraint(
            ["organisation_id", "location_id"], ["locations.organisation_id", "locations.id"]
        ),
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint("hourly_price >= 0", name="hourly_price_not_negative"),
    )

    organisation_id: Mapped[UUID]
    location_id: Mapped[UUID]
    name: Mapped[str]
    kind: Mapped[ResourceKind] = mapped_column(_string_enum(ResourceKind, "resource_kind"))
    capacity: Mapped[int]
    hourly_price: Mapped[int]


class BookingRecord(EntityRecord):
    __tablename__ = "bookings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id", "resource_id"], ["resources.organisation_id", "resources.id"]
        ),
        CheckConstraint("NOT isempty(period)", name="period_not_empty"),
        CheckConstraint("price >= 0", name="price_not_negative"),
    )

    organisation_id: Mapped[UUID]
    resource_id: Mapped[UUID]
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    status: Mapped[BookingStatus] = mapped_column(_string_enum(BookingStatus, "booking_status"))
    price: Mapped[int]
