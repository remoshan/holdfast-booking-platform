"""The events the notifications service receives, as it reads them: the fields it needs from the
data of each. The booking service publishes them; what each must carry is this service's side of the
contract between the two, and fields it does not need are ignored."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel

BOOKING_CONFIRMED = "bookings.booking_confirmed"
BOOKING_CANCELLED = "bookings.booking_cancelled"
REMINDER_DUE = "bookings.reminder_due"


class BookingDetails(BaseModel):
    booking_id: UUID
    recipient: str
    """The booker's email address."""
    resource_name: str
    location_name: str
    address: str
    timezone: str
    """The location's IANA time zone, on whose clock the email shows the times."""
    start: AwareDatetime
    end: AwareDatetime


class BookingConfirmed(BookingDetails):
    price: int
    """In minor units of the currency; 0 for a free booking."""
    currency: str


class BookingCancelled(BookingDetails):
    pass


class ReminderDue(BookingDetails):
    pass
