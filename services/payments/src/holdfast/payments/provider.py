"""The simulated payment provider. It moves no real money, but keeps books of its own as a real one
would: in a table only it writes, in transactions of its own, so that a charge it has made survives
whatever happens to the job that asked for it.

Every call names an idempotency key, the booking's id. A call with a key the provider has seen gets
the first call's answer and moves no money, so a retry after a timeout, a lost answer or a crash
finds the charge instead of making a second one.

The card decides the outcome, so that tests and demonstrations choose it:

- pm_card_ok is charged.
- pm_card_slow is charged, but the answer to the first call never arrives in time: the caller
  gives up, and its retry finds the charge.
- pm_card_declined, and any card the provider does not know, is declined.

Two settings add trouble for trying the saga out. The first answer under each key can take longer
to arrive; a retry is answered at once, from the provider's books, as a real provider replays an
idempotent request. And a share of calls can be refused, as by a provider that is down, before
anything is done.
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from holdfast.payments.records import ProviderChargeRecord

CHARGEABLE_CARDS = frozenset({"pm_card_ok", "pm_card_slow"})
SLOW_CARD = "pm_card_slow"


class ProviderUnavailableError(Exception):
    """The provider refused the call before doing anything, as one that is down would."""


@dataclass(frozen=True, slots=True)
class Provider:
    sessions: async_sessionmaker[AsyncSession]
    """On an engine of the provider's own, so that none of its commits is ever part of a job's."""
    delay: timedelta = timedelta(0)
    """How long the first answer under each key takes to arrive."""
    failure_rate: float = 0
    """The share of calls refused as unavailable."""

    async def charge(self, key: UUID, amount: int, currency: str, payment_method: str) -> bool:
        """Charge the amount once per key, and say whether it was charged: False is a decline."""
        self._refuse_a_share_of_calls()
        async with self.sessions() as session, session.begin():
            succeeded = await session.scalar(
                insert(ProviderChargeRecord)
                .values(
                    idempotency_key=key,
                    amount=amount,
                    currency=currency,
                    succeeded=payment_method in CHARGEABLE_CARDS,
                )
                .on_conflict_do_nothing()
                .returning(ProviderChargeRecord.succeeded)
            )
            first_call = succeeded is not None
            if not first_call:
                succeeded = await session.scalar(
                    select(ProviderChargeRecord.succeeded).where(
                        ProviderChargeRecord.idempotency_key == key
                    )
                )
        if first_call:
            if payment_method == SLOW_CARD:
                await asyncio.Event().wait()  # charged, but the caller gives up before it hears so
            await asyncio.sleep(self.delay.total_seconds())
        return bool(succeeded)

    async def refund(self, key: UUID) -> None:
        """Refund in full the charge made under the key. Refunding it again changes nothing."""
        self._refuse_a_share_of_calls()
        async with self.sessions() as session, session.begin():
            charge = await session.get(ProviderChargeRecord, key, with_for_update=True)
            if charge is None or not charge.succeeded:
                raise LookupError(f"The provider has no charge {key} to refund.")
            first_call = charge.refunded_at is None
            if first_call:
                charge.refunded_at = datetime.now(UTC)
        if first_call:
            await asyncio.sleep(self.delay.total_seconds())

    def _refuse_a_share_of_calls(self) -> None:
        # Dice for a simulation, not a secret.
        if random.random() < self.failure_rate:  # noqa: S311
            raise ProviderUnavailableError("The payment provider is unavailable.")
