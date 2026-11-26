"""What the payments service does with each command: ask the provider, record its answer in the
ledger, and send the outcome back, all in the job's one transaction. Each command runs as a job, so
a call that fails or times out is retried, and one that keeps failing ends up dead, waiting to be
replayed.

The provider answers before the job commits. A job that fails after the provider acted, because the
answer was lost, the call timed out or the worker died, leaves nothing in the ledger, and its retry
asks again under the same key: the provider answers as it did the first time and moves no money. So
each booking is charged at most once, and refunded at most once.
"""

import asyncio
from datetime import datetime, timedelta
from functools import partial

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from holdfast.messaging import outbox
from holdfast.messaging.jobs import Handler, Payload
from holdfast.payments.messages import (
    CHARGE_BOOKING,
    CHARGE_DECLINED,
    CHARGE_REFUNDED,
    CHARGE_SUCCEEDED,
    REFUND_BOOKING,
    ChargeBooking,
    Outcome,
    RefundBooking,
)
from holdfast.payments.provider import Provider
from holdfast.payments.records import ChargeRecord, ChargeStatus

PROVIDER_TIMEOUT = timedelta(seconds=10)
"""How long a call to the provider may take before its job gives up and fails. The limit on how long
Postgres lets one of the worker's sessions sit idle in a transaction is derived from it."""


class NothingToRefundError(Exception):
    """A refund of a booking whose charge was declined or never made. The booking service never
    asks for one, so its job fails, as an alarm, and moves no money."""


def handlers(provider: Provider) -> dict[str, Handler]:
    """What the worker runs for each command the service takes."""
    return {
        CHARGE_BOOKING: partial(_charge, provider),
        REFUND_BOOKING: partial(_refund, provider),
    }


async def _charge(
    provider: Provider, session: AsyncSession, payload: Payload, now: datetime
) -> None:
    command = ChargeBooking.model_validate(payload["data"])
    if await session.get(ChargeRecord, command.booking_id) is not None:
        return  # a copy: the outcome is recorded, and its reply sent
    async with asyncio.timeout(PROVIDER_TIMEOUT.total_seconds()):
        charged = await provider.charge(
            command.booking_id, command.amount, command.currency, command.payment_method
        )
    recorded = await session.scalar(
        insert(ChargeRecord)
        .values(
            booking_id=command.booking_id,
            amount=command.amount,
            currency=command.currency,
            status=ChargeStatus.CHARGED if charged else ChargeStatus.DECLINED,
        )
        .on_conflict_do_nothing()
        .returning(ChargeRecord.booking_id)
    )
    # A copy that raced this one may have recorded the same answer first; only one replies.
    if recorded is not None:
        outcome = Outcome(organisation_id=command.organisation_id, booking_id=command.booking_id)
        await outbox.publish(session, CHARGE_SUCCEEDED if charged else CHARGE_DECLINED, outcome)


async def _refund(
    provider: Provider, session: AsyncSession, payload: Payload, now: datetime
) -> None:
    command = RefundBooking.model_validate(payload["data"])
    charge = await session.get(ChargeRecord, command.booking_id, with_for_update=True)
    if charge is not None and charge.status is ChargeStatus.REFUNDED:
        return  # a copy
    if charge is None or charge.status is not ChargeStatus.CHARGED:
        raise NothingToRefundError(f"Booking {command.booking_id} has no charge to refund.")
    async with asyncio.timeout(PROVIDER_TIMEOUT.total_seconds()):
        await provider.refund(command.booking_id)
    charge.status = ChargeStatus.REFUNDED
    outcome = Outcome(organisation_id=command.organisation_id, booking_id=command.booking_id)
    await outbox.publish(session, CHARGE_REFUNDED, outcome)
