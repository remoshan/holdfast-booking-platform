"""The envelope every message travels in: an id, a type, a time and the data. It is the only
schema the services share. The shape of each type's data belongs to the services that send and
receive it, and to the contracts between them."""

import math
import re
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict

TYPE_MAX_LENGTH = 32
"""The width of the jobs.kind and outbox.type columns."""

DATA_MAX_DEPTH = 32
"""How deeply the data may nest. Pydantic cannot serialise data about 100 levels deep, so a deeper
envelope could be read but never stored."""

_TYPE_PATTERN = re.compile(r"[a-z]+\.[a-z][a-z0-9_]*")


def check_type(message_type: str) -> str:
    """A message type names the service that sends it and the message, such as
    bookings.booking_confirmed. It is the routing key, and the kind of the job that receives it,
    which a service's own job kinds can never clash with: they have no dot."""
    if len(message_type) > TYPE_MAX_LENGTH or not _TYPE_PATTERN.fullmatch(message_type):
        raise ValueError(
            f"{message_type!r} is not a message type: a service name, a dot and a snake_case"
            f" name, at most {TYPE_MAX_LENGTH} characters."
        )
    return message_type


def _check_storable(value: object, depth: int = 0) -> None:
    """Refuse what Postgres cannot store in jsonb, or the envelope cannot serialise. Storing an
    envelope that passed can then fail only because the database is unavailable, never because of
    the message."""
    if depth > DATA_MAX_DEPTH:
        raise ValueError(f"Data may nest at most {DATA_MAX_DEPTH} levels deep.")
    match value:
        case str():
            if "\x00" in value:
                raise ValueError("Postgres cannot store the character U+0000.")
            value.encode()  # A lone surrogate cannot be encoded, nor stored.
        case float() if not math.isfinite(value):
            raise ValueError("Postgres cannot store NaN or an infinite number in jsonb.")
        case dict():
            for key, item in value.items():
                _check_storable(key, depth + 1)
                _check_storable(item, depth + 1)
        case list():
            for item in value:
                _check_storable(item, depth + 1)


def _storable(data: dict[str, Any]) -> dict[str, Any]:
    _check_storable(data)
    return data


class Envelope(BaseModel):
    """A message as it travels: the JSON body of every message published to RabbitMQ, and the
    payload of the job that receives it. Attributes it does not know are ignored, so a newer
    sender never breaks an older receiver."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    """Fixed when the message is written to the outbox, so every copy of it carries the same id."""
    type: Annotated[str, AfterValidator(check_type)]
    time: AwareDatetime
    """When the transaction that sent the message began. A time that matters to the receiver,
    such as when a charge succeeded, belongs in the data."""
    data: Annotated[dict[str, Any], AfterValidator(_storable)]
