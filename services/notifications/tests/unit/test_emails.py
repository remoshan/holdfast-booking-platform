"""The emails composed from the booking service's events: what each one says, with the booking's
times on the location's clock and amounts in each currency's own unit."""

from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

import pytest

from holdfast.notifications import emails
from holdfast.notifications.events import (
    BookingCancelled,
    BookingConfirmed,
    PaymentFailed,
    RefundIssued,
    ReminderDue,
)

LINE_SEPARATOR = chr(0x2028)


def _event(**fields: Any) -> dict[str, Any]:
    """An event's data for a booking on Tuesday 3 November 2026, 09:00 to 10:30 in Amsterdam."""
    return {
        "booking_id": str(uuid4()),
        "recipient": "ada@example.com",
        "resource_name": "Room 1",
        "location_name": "Central",
        "address": "Damrak 1, Amsterdam",
        "timezone": "Europe/Amsterdam",
        "start": datetime(2026, 11, 3, 8, 0, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 11, 3, 9, 30, tzinfo=UTC).isoformat(),
        "price": 0,
        "currency": "EUR",
    } | fields


def test_a_confirmation_shows_the_booking_on_the_locations_clock_and_what_was_paid() -> None:
    event = BookingConfirmed.model_validate(_event(price=2000))

    email = emails.booking_confirmed(event)

    assert email.recipient == "ada@example.com"
    assert email.subject == "Booking confirmed: Room 1, Tue 03 Nov at 09:00"
    assert email.body == (
        "Hello,\n\n"
        "Your booking is confirmed:\n\n"
        "  Room 1\n"
        "  Central, Damrak 1, Amsterdam\n"
        "  Tuesday 03 November 2026, 09:00 to 10:30 (Europe/Amsterdam time)\n"
        "  Paid: 20.00 EUR\n\n"
        "Holdfast\n"
    )
    assert email.message_id == f"<confirmed.{event.booking_id}@holdfast>"


def test_a_free_booking_is_confirmed_without_an_amount() -> None:
    email = emails.booking_confirmed(BookingConfirmed.model_validate(_event(price=0)))

    assert "Paid" not in email.body


def test_a_cancellation_says_the_booking_is_cancelled() -> None:
    event = BookingCancelled.model_validate(_event())

    email = emails.booking_cancelled(event)

    assert email.subject == "Booking cancelled: Room 1, Tue 03 Nov at 09:00"
    assert "Your booking has been cancelled:" in email.body
    assert email.message_id == f"<cancelled.{event.booking_id}@holdfast>"


def test_a_reminder_asks_the_booker_to_cancel_a_booking_they_no_longer_need() -> None:
    event = ReminderDue.model_validate(_event())

    email = emails.reminder(event)

    assert email.subject == "Reminder: Room 1, Tue 03 Nov at 09:00"
    assert "please cancel it so that someone else can book it" in email.body
    assert email.message_id == f"<reminder.{event.booking_id}@holdfast>"


def test_a_failed_payment_says_nothing_was_charged_and_what_the_booking_would_have_cost() -> None:
    event = PaymentFailed.model_validate(_event(price=12345, currency="KWD"))

    email = emails.payment_failed(event)

    assert email.subject == "Payment failed: Room 1, Tue 03 Nov at 09:00"
    assert "Your payment was declined, so this booking was not made and nothing was charged:" in (
        email.body
    )
    assert "  Price: 12.345 KWD\n" in email.body
    assert email.message_id == f"<payment_failed.{event.booking_id}@holdfast>"


def test_a_refund_says_how_much_was_refunded() -> None:
    event = RefundIssued.model_validate(_event(amount=2000))

    email = emails.refund_issued(event)

    assert email.subject == "Refund issued: Room 1, Tue 03 Nov at 09:00"
    assert "Your payment for this booking has been refunded in full:" in email.body
    assert "  Refunded: 20.00 EUR\n" in email.body
    assert email.message_id == f"<refund_issued.{event.booking_id}@holdfast>"


def test_a_time_the_clock_shows_twice_carries_its_zone() -> None:
    """When the clocks go back, an hour comes twice: this booking starts in the first 01:45 and ends
    in the second 01:15."""
    event = ReminderDue.model_validate(
        _event(
            timezone="Europe/London",
            start=datetime(2026, 10, 25, 0, 45, tzinfo=UTC).isoformat(),
            end=datetime(2026, 10, 25, 1, 15, tzinfo=UTC).isoformat(),
        )
    )

    email = emails.reminder(event)

    assert email.subject == "Reminder: Room 1, Sun 25 Oct at 01:45 BST"
    assert "Sunday 25 October 2026, 01:45 BST to 01:15 GMT (Europe/London time)" in email.body


@pytest.mark.parametrize(
    ("minor_units", "currency", "shown"),
    [
        (2000, "EUR", "20.00 EUR"),
        (5, "EUR", "0.05 EUR"),
        (2000, "JPY", "2000 JPY"),
        (12345, "KWD", "12.345 KWD"),
        (10000, "CLF", "1.0000 CLF"),
    ],
)
def test_an_amount_is_shown_in_its_currencys_major_unit(
    minor_units: int, currency: str, shown: str
) -> None:
    assert emails.format_amount(minor_units, currency) == shown


@pytest.mark.parametrize(
    "name",
    ["Room 4\nSecond floor", f"Room 4{LINE_SEPARATOR}Second floor", " Room 4 \r\n\tSecond floor "],
    ids=["a newline", "a line separator", "a line break among spaces"],
)
def test_a_name_with_line_breaks_stays_on_one_line_in_the_subject(name: str) -> None:
    email = emails.reminder(ReminderDue.model_validate(_event(resource_name=name)))

    message = EmailMessage()
    message["Subject"] = email.subject  # refuses a value with a line break
    assert email.subject.startswith("Reminder: Room 4 Second floor, ")
