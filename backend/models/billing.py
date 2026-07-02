"""
Billing idempotency tables.

Two purpose-built tables back the Stripe webhook's two-layer idempotency
(design §4). Neither holds cardholder data — only opaque Stripe identifiers.

  processed_stripe_events   — layer 1: global per-event dedup. Stripe delivers
                              at-least-once; a redelivered event.id must be a no-op.
  granted_monthly_invoices  — layer 2: the monthly credit grant is recorded against
                              the invoice.id (one invoice per billing cycle) so the
                              grant is provably once-per-paid-invoice even if two
                              distinct events ever referenced the same invoice.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class ProcessedStripeEvent(Base):
    __tablename__ = "processed_stripe_events"

    # Stripe event id, e.g. "evt_1P...". Primary key => insert-or-skip dedup.
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GrantedMonthlyInvoice(Base):
    __tablename__ = "granted_monthly_invoices"

    # Stripe invoice id, e.g. "in_1P...". Primary key => monthly grant is
    # recorded exactly once per invoice.
    invoice_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GrantedPackSession(Base):
    __tablename__ = "granted_pack_sessions"

    # Stripe Checkout session id, e.g. "cs_...". Primary key => a one-time credit
    # pack is granted exactly once per completed checkout session (§6), even if the
    # completed event is redelivered under a different event id.
    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
