"""The emails the notifications service sends, one for each kind of booking event, written for the
booker: the booking's resource, place and times on the location's clock."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from holdfast.notifications.events import (
    BookingCancelled,
    BookingConfirmed,
    BookingDetails,
    ReminderDue,
)

# ISO 4217 currencies whose minor unit is not a hundredth of the major one. Prices travel as whole
# minor units, so showing one needs the number of digits after the decimal point.
_MINOR_UNIT_DIGITS = {
    **dict.fromkeys(
        (
            "BIF",
            "CLP",
            "DJF",
            "GNF",
            "ISK",
            "JPY",
            "KMF",
            "KRW",
            "PYG",
            "RWF",
            "UGX",
            "UYI",
            "VND",
            "VUV",
            "XAF",
            "XOF",
            "XPF",
        ),
        0,
    ),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
    **dict.fromkeys(("CLF", "UYW"), 4),
}


@dataclass(frozen=True, slots=True)
class Email:
    recipient: str
    subject: str
    body: str
    message_id: str
    """The same for every attempt to send it. A worker that stops after the mail server accepted
    the email sends it again, and mail clients that drop repeated message ids show it once."""


def booking_confirmed(event: BookingConfirmed) -> Email:
    paid = [f"Paid: {format_amount(event.price, event.currency)}"] if event.price else []
    return _email(event, "confirmed", "Booking confirmed", "Your booking is confirmed:", paid, "")


def booking_cancelled(event: BookingCancelled) -> Email:
    return _email(
        event, "cancelled", "Booking cancelled", "Your booking has been cancelled:", [], ""
    )


def reminder(event: ReminderDue) -> Email:
    return _email(
        event,
        "reminder",
        "Reminder",
        "This is a reminder of your booking:",
        [],
        "If you no longer need it, please cancel it so that someone else can book it.\n\n",
    )


def format_amount(minor_units: int, currency: str) -> str:
    """An amount in a currency's major unit, with as many decimal places as its minor unit needs,
    such as 20.00 EUR or 2000 JPY."""
    digits = _MINOR_UNIT_DIGITS.get(currency, 2)
    return f"{Decimal(minor_units).scaleb(-digits):.{digits}f} {currency}"


def _email(
    event: BookingDetails,
    kind: str,
    heading: str,
    opening: str,
    extra_lines: list[str],
    closing: str,
) -> Email:
    timezone = ZoneInfo(event.timezone)
    start = event.start.astimezone(timezone)
    end = event.end.astimezone(timezone)
    # A name may contain line breaks, but an email header must stay on one line.
    resource_name = " ".join(event.resource_name.split())
    lines = [
        resource_name,
        f"{event.location_name}, {event.address}",
        f"{start:%A %d %B %Y}, {_clock(start)} to {_clock(end)} ({timezone.key} time)",
        *extra_lines,
    ]
    details = "".join(f"  {line}\n" for line in lines)
    return Email(
        recipient=event.recipient,
        subject=f"{heading}: {resource_name}, {start:%a %d %b} at {_clock(start)}",
        body=f"Hello,\n\n{opening}\n\n{details}\n{closing}Holdfast\n",
        message_id=f"<{kind}.{event.booking_id}@holdfast>",
    )


def _clock(moment: datetime) -> str:
    """A time on the location's clock, with its zone's abbreviation if the clock shows that time
    twice, as in the hour repeated when the clocks go back; without it, a booking across the change
    could read 01:45 to 01:15."""
    if moment.replace(fold=1 - moment.fold).utcoffset() == moment.utcoffset():
        return f"{moment:%H:%M}"
    return f"{moment:%H:%M %Z}"
