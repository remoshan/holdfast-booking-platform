"""What the payments tests share besides fixtures: the commands they send and the replies they
expect."""

from typing import Any
from uuid import UUID, uuid4

type Json = dict[str, Any]

ORGANISATION_ID = uuid4()
"""The organisation of every booking the tests charge. Payments only echoes it back."""


def charge_command(booking_id: UUID, payment_method: str = "pm_card_ok") -> Json:
    """The data of a command to charge the booking 20.00 EUR with the card."""
    return about_booking(booking_id) | {
        "amount": 2000,
        "currency": "EUR",
        "payment_method": payment_method,
    }


def about_booking(booking_id: UUID) -> Json:
    """The data that names a booking: all a refund command carries, and every reply."""
    return {"organisation_id": str(ORGANISATION_ID), "booking_id": str(booking_id)}
