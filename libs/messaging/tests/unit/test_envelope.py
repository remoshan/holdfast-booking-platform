"""The envelope: what survives a round trip, and what it refuses, so that every envelope it accepts
can be stored."""

import json
import math
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from holdfast.messaging.envelope import DATA_MAX_DEPTH, TYPE_MAX_LENGTH, Envelope

NUL = chr(0)
LONE_SURROGATE = chr(0xD800)


def _body(**attributes: Any) -> bytes:
    envelope = {
        "id": str(uuid4()),
        "type": "bookings.booking_confirmed",
        "time": "2026-11-24T10:15:02.123456+00:00",
        "data": {"booking_id": str(uuid4()), "amount": 2000},
    }
    return json.dumps(envelope | attributes).encode()


def test_an_envelope_survives_a_round_trip() -> None:
    envelope = Envelope(
        id=uuid4(),
        type="payments.charge_succeeded",
        time=datetime(2026, 11, 24, 10, 15, 2, 123456, tzinfo=UTC),
        data={"amount": 2000, "currency": "EUR", "nested": {"list": [1, "two", None, 3.5, True]}},
    )

    assert Envelope.model_validate_json(envelope.model_dump_json()) == envelope


def test_attributes_a_receiver_does_not_know_are_ignored() -> None:
    """So a sender can add one without breaking the services that receive its messages."""
    envelope = Envelope.model_validate_json(_body(correlation_id="added-later"))

    assert envelope.type == "bookings.booking_confirmed"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not JSON"),
        pytest.param(b"[]", id="a JSON array"),
        pytest.param(_body(id="42"), id="an id that is not a UUID"),
        pytest.param(_body(time="2026-11-24T10:15:02"), id="a time without an offset"),
        pytest.param(_body(type="booking_confirmed"), id="a type without its service"),
        pytest.param(_body(type="Bookings.Booking_Confirmed"), id="a type in upper case"),
        pytest.param(
            _body(type="bookings." + "x" * (TYPE_MAX_LENGTH - len("bookings.") + 1)),
            id="a type longer than its column",
        ),
        pytest.param(_body(data=["a list"]), id="data that is not an object"),
        pytest.param(json.dumps({"type": "bookings.booking_confirmed"}).encode(), id="no id"),
    ],
)
def test_a_body_that_is_not_an_envelope_is_refused(body: bytes) -> None:
    with pytest.raises(ValidationError):
        Envelope.model_validate_json(body)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"amount": math.nan}, id="NaN"),
        pytest.param({"amount": -math.inf}, id="an infinite number"),
        pytest.param({"name": f"Room{NUL}1"}, id="U+0000 in a value"),
        pytest.param({f"name{NUL}": "Room 1"}, id="U+0000 in a key"),
        pytest.param({"rooms": [{"name": LONE_SURROGATE}]}, id="a lone surrogate deep inside"),
    ],
)
def test_data_that_postgres_cannot_store_is_refused(data: dict[str, Any]) -> None:
    """jsonb holds neither NaN nor infinity, nor U+0000, nor half of a UTF-16 pair. An envelope
    holding one could never be stored, and its consumer would retry it for ever."""
    with pytest.raises(ValidationError):
        Envelope.model_validate_json(_body(data=data))


def test_data_may_nest_as_deeply_as_the_limit_and_no_deeper() -> None:
    """Pydantic cannot serialise data nested about 100 levels deep, so a receiver could read such
    an envelope but never store it; the limit keeps well clear of that."""
    accepted = Envelope.model_validate_json(_body(data={"deep": _nested(DATA_MAX_DEPTH - 1)}))

    with pytest.raises(ValidationError):
        Envelope.model_validate_json(_body(data={"deep": _nested(DATA_MAX_DEPTH)}))
    assert Envelope.model_validate_json(accepted.model_dump_json()) == accepted


def _nested(lists: int) -> Any:
    """A string inside this many nested lists. Inside the data, it sits one level deeper."""
    value: Any = "bottom"
    for _ in range(lists):
        value = [value]
    return value
