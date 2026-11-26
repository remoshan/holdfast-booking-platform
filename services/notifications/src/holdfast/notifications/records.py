"""The notifications service's tables: the messaging library's job queue and the ids of the messages
it received. The service keeps no data of its own; the events carry everything an email needs."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from holdfast.messaging.records import (
    NAMING_CONVENTION,
    JobRecord,
    ReceivedMessageRecord,
    add_tables_to,
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Copied in, so the migrations, the schema test and the tests' cleanup see the whole database.
add_tables_to(Base.metadata, JobRecord, ReceivedMessageRecord)
