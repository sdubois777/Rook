"""
Outbound email tables.

  email_sends        — one row per dedupe_key, carrying the outcome of the most
                       recent attempt against it. `dedupe_key` is UNIQUE and is
                       claimed BEFORE the provider call, so a retried webhook or
                       a double-fired background task cannot send the same
                       message twice. A failed attempt keeps its row
                       (status='failed') rather than deleting it, so a persistent
                       failure is visible instead of looking like a message that
                       was never attempted.

                       The row is NEVER deleted, because deleting it reopens the
                       double-send race it exists to close. Retry is therefore
                       expressed as RE-CLAIMING the same row: a claim may be
                       re-taken only while status is 'failed' and `attempts` is
                       below MAX_SEND_ATTEMPTS. A row that reached 'sent' is
                       never re-claimable at any attempt count.

  email_suppressions — addresses that must never be emailed again: an
                       unsubscribe, a hard bounce, or a spam complaint. Keyed on
                       the lowercased address rather than user_id, because a
                       bounce or complaint arrives from the provider identified
                       only by address, and because an address that complained
                       must stay suppressed even if the account is deleted and
                       recreated.

Suppression applies to PROMOTIONAL mail. Genuinely transactional mail (a receipt,
a security notice) is exempt under CAN-SPAM and passes an explicit flag — but
nothing in this app sends that category today, so every current send checks the
list.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base

# Send outcomes. Every one of these is a status EmailService.send returns; only
# the first three are ever WRITTEN to email_sends.status.
#
# SUPPRESSED and SKIPPED are return values only. Each check that produces one
# returns before the row is claimed, so a suppressed or skipped send leaves NO
# row in email_sends at all — its log line is the only record. An operator
# looking for a message that never arrived will not find a 'skipped' row,
# because none is ever written.
#
# PENDING and SKIPPED say different things and are deliberately not merged: a
# PENDING row means the provider call was started and we never learned the
# result, which is the state to investigate. SKIPPED means we chose not to send.
SEND_PENDING = "pending"      # claimed, provider call in flight or crashed mid-call
SEND_SENT = "sent"            # provider accepted it
SEND_FAILED = "failed"        # provider rejected it, or the call gave no answer
SEND_SUPPRESSED = "suppressed"  # returned, never stored: recipient is suppressed
SEND_SKIPPED = "skipped"      # returned, never stored: disabled, undeliverable,
                              # rate-capped, or the dedupe key is not claimable

# TOTAL provider calls allowed against one dedupe key, not the number of
# retries: the row is inserted with attempts=1 and re-claimed only while
# attempts < MAX_SEND_ATTEMPTS, so at 3 the budget is one initial call plus two
# retries. The dedupe row is never deleted, so this count is the only thing that
# distinguishes "retry this" from "this already went out". A send that reached
# SEND_SENT is never re-attempted at any attempt count.
MAX_SEND_ATTEMPTS = 3

# Domains that exist only because an identity provider gave us no address, so
# the app synthesized one to fill a NOT NULL column. Nothing behind them
# resolves; mailing them earns hard bounces, and a bounce rate is what gets a
# sending domain blocked.
#
# THIS TUPLE IS THE ONLY LIST. The producers are backend/core/dependencies.py
# (the dev-auth fallback address and the missing-Clerk-email fallback) and
# backend/routers/draft.py (the same two cases on the websocket path). It lives
# here rather than in email_service.py so a producer and the consumer that has to
# recognise it can both import one name — a second hardcoded copy is how a newly
# added placeholder domain starts getting mailed.
# tests/unit/services/email/test_email_service.py reads those two source files
# and fails if either synthesizes a domain this tuple does not cover.
UNDELIVERABLE_EMAIL_SUFFIXES = ("@placeholder.local", "@dev.local")

# Why an address is suppressed.
SUPPRESS_UNSUBSCRIBE = "unsubscribe"
SUPPRESS_BOUNCE = "bounce"
SUPPRESS_COMPLAINT = "complaint"

# Message categories. Promotional mail additionally requires a postal address
# and an unsubscribe link (CAN-SPAM); transactional mail does not.
CATEGORY_TRANSACTIONAL = "transactional"
CATEGORY_PROMOTIONAL = "promotional"


class EmailSend(Base):
    __tablename__ = "email_sends"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable: a send can be addressed to someone with no account row yet.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)

    # Caller-supplied natural key, e.g. "welcome:<user_id>". UNIQUE => claiming
    # it is the send lock. Never derive this from a timestamp or a random value,
    # or it stops deduplicating anything.
    dedupe_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Provider-call attempts made against this dedupe key. See MAX_SEND_ATTEMPTS.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailSuppression(Base):
    __tablename__ = "email_suppressions"

    # Lowercased address. Primary key => insert-or-skip, and a suppression can
    # never be duplicated by a second unsubscribe click.
    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
