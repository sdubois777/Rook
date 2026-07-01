"""
Billing idempotency repositories.

Both methods are insert-or-skip (`ON CONFLICT DO NOTHING`) and return whether the
row was NEW — the webhook gates side effects on that boolean. Neither commits; the
webhook commits once after all §4 side effects so a mid-handler failure rolls the
event record back and Stripe safely redelivers.
"""
from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models.billing import GrantedMonthlyInvoice, ProcessedStripeEvent


class ProcessedStripeEventRepository:
    def __init__(self, session):
        self._session = session

    async def mark_processed(self, event_id: str) -> bool:
        """Insert the event id; return True if newly inserted, False if a dup.

        Layer 1 global idempotency: a False means Stripe redelivered an event we
        already handled — the caller must no-op.
        """
        result = await self._session.execute(
            pg_insert(ProcessedStripeEvent)
            .values(event_id=event_id)
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
        return result.rowcount > 0


class GrantedInvoiceRepository:
    def __init__(self, session):
        self._session = session

    async def record_grant(
        self, invoice_id: str, user_id: uuid.UUID, credits: int
    ) -> bool:
        """Record a monthly grant against an invoice id; return True if new.

        Layer 2 grant idempotency: a False means this invoice's monthly credits
        were already granted — the caller must NOT grant again.
        """
        result = await self._session.execute(
            pg_insert(GrantedMonthlyInvoice)
            .values(invoice_id=invoice_id, user_id=user_id, credits=credits)
            .on_conflict_do_nothing(index_elements=["invoice_id"])
        )
        return result.rowcount > 0
