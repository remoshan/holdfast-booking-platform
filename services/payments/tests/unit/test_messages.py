"""What a charge command must carry. A command that breaks these rules fails its job before the
provider is asked."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from holdfast.payments.messages import ChargeBooking
from tests.support import charge_command


@pytest.mark.parametrize(
    "broken",
    [
        {"amount": 0},
        {"currency": "eur"},
        {"currency": "EURO"},
        {"payment_method": ""},
        {"payment_method": "x" * 256},
    ],
    ids=[
        "nothing to charge",
        "a lower-case currency",
        "a four-letter currency",
        "no card",
        "a card too long",
    ],
)
def test_a_charge_command_that_breaks_the_contract_is_refused(broken: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChargeBooking.model_validate(charge_command(uuid4()) | broken)
