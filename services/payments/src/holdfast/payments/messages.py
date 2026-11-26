"""The messages the payments service takes and sends, as it reads and writes them. The booking
service sends the commands, and each outcome goes back to it as an event; what each carries is this
service's side of the contract between the two.

A booking is charged at most once, so its id names its charge everywhere: in the ledger, at the
provider, and in every reply.
"""

from uuid import UUID

from pydantic import BaseModel, Field

CHARGE_BOOKING = "bookings.charge_booking"
REFUND_BOOKING = "bookings.refund_booking"
CHARGE_SUCCEEDED = "payments.charge_succeeded"
CHARGE_DECLINED = "payments.charge_declined"
CHARGE_REFUNDED = "payments.charge_refunded"


class ChargeBooking(BaseModel):
    organisation_id: UUID
    booking_id: UUID
    amount: int = Field(gt=0)
    """The booking's price, in minor units of the currency."""
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    payment_method: str = Field(min_length=1, max_length=255)
    """The token the booker's checkout gave them, such as pm_card_ok."""


class RefundBooking(BaseModel):
    """Refund the whole of the booking's charge."""

    organisation_id: UUID
    booking_id: UUID


class Outcome(BaseModel):
    """What every reply carries: the booking it settles, and its organisation, through which the
    booking service finds the booking. Payments stores nothing of the organisation."""

    organisation_id: UUID
    booking_id: UUID
