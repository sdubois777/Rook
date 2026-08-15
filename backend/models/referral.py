"""
Referral program tables.

Two tables, mirroring the billing idempotency posture in backend/models/billing.py
(a grant is recorded against an opaque Stripe id so it is provably once-only):

  referral_codes    — one shareable code per user. Generated lazily the first
                      time a user looks at their account page, not at signup,
                      so we never mint codes for accounts that never return.

  code_redemptions  — one row per discount actually applied at checkout, for
                      BOTH discount kinds (a friend's referral code, and the
                      welcome code emailed to a free signup). Carries the two
                      constraints that make the program hard to farm:

                        * UNIQUE(stripe_session_id) — the webhook can process the
                          same completed checkout twice (Stripe delivers at-least-
                          once, and a redelivery may carry a different event id).
                          The session id is the stable key, exactly as
                          granted_pack_sessions uses it for credit packs.

                        * UNIQUE(redeemer_user_id, kind) — a given account gets
                          each kind of discount at most once, EVER. One confirmed
                          row per account per kind, kept for the life of the
                          account, so subscribing and cancelling repeatedly buys
                          no second discount.

  WHAT PROTECTS THE PROGRAM ECONOMICALLY. Two separate mechanisms, and neither
  is the `reversed` status:

    * The redeemer's side is the unique constraint above. It is what makes each
      discount once-ever per account.

    * The referrer's side is ReferralRepository.confirmed_referral_count, which
      counts only referrals whose referred account holds a paid tier RIGHT NOW
      and is not soft-deleted. The referrer's coupon is recomputed from that
      number every time, never accumulated, so a referral that is refunded,
      cancelled, expired or deleted stops paying on the next recompute with no
      correction step. Five throwaway accounts that subscribe once and cancel
      therefore cannot buy a permanent recurring discount.

  Self-referral is blocked in the service layer rather than here, because it
  needs a comparison (referrer != redeemer) that a table constraint can express
  only as a CHECK across two columns — doable, but it would surface as an opaque
  IntegrityError at webhook time instead of a clear 400 at checkout time.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base

# Discount kinds. `welcome` is the code emailed to a signup who has not paid;
# `referral` is another user's code. A user may hold one of each but redeem only
# one per checkout — the referral code wins, being worth more.
KIND_WELCOME = "welcome"
KIND_REFERRAL = "referral"
REDEMPTION_KINDS = (KIND_WELCOME, KIND_REFERRAL)

# Redemption lifecycle.
#   PENDING    reserved when the Checkout Session is created, BEFORE payment.
#              This is what makes the (redeemer_user_id, kind) unique constraint
#              fire on a second simultaneous checkout — without it, a user can
#              open two tabs, receive two Stripe sessions each carrying the
#              coupon, and pay for both before either webhook lands.
#              A pending row older than PENDING_TTL_HOURS is abandoned (the
#              customer closed the Stripe page) and no longer blocks a retry.
#   CONFIRMED  the signature-verified webhook saw the payment complete.
#   REVERSED   DEFINED BUT NEVER WRITTEN IN PRODUCTION. The only writer is
#              ReferralRepository.reverse_redemption, which has no caller: no
#              refund or dispute event is registered in the webhook dispatch
#              table. It stays defined because the slot queries already accept
#              it, so adding refund handling later needs no query change. A
#              referred subscription ending is NOT what sets this — that path
#              needs nothing set, because the referrer's rate is recomputed from
#              who is paying right now (see confirmed_referral_count).
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REVERSED = "reversed"

# Stripe Checkout Sessions expire after 24 hours. A reservation older than that
# can never be completed, so holding the user's once-ever slot past it would lock
# them out of a discount they never actually received.
PENDING_TTL_HOURS = 24


class ReferralCode(Base):
    __tablename__ = "referral_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # One code per user. UNIQUE (not just indexed) so a concurrent double-
    # generate loses at the DB rather than leaving a user with two live codes.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    # The shareable string, e.g. "ROOK-7K2M9X". Stored exactly as displayed;
    # lookups uppercase the input first (see ReferralRepository.get_by_code).
    code: Mapped[str] = mapped_column(
        String(24), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CodeRedemption(Base):
    __tablename__ = "code_redemptions"
    __table_args__ = (
        # A given account takes each discount kind at most once, ever.
        UniqueConstraint("redeemer_user_id", "kind", name="uq_redemption_user_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The code as typed. Kept even for `welcome` (a fixed string) so the row is
    # self-describing in a support conversation without a second lookup.
    code: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    redeemer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    # NULL for `welcome` redemptions — nobody earns a reward for those.
    referrer_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    # Stripe Checkout session id, e.g. "cs_...". UNIQUE => the referrer's reward
    # is raised exactly once per completed checkout even under redelivery.
    stripe_session_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    # What was actually applied, captured at redemption time. Recorded rather
    # than recomputed because REFERRAL_PROGRAM percentages may change later and
    # an audit needs to show the rate the customer was actually given.
    percent_off: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_PENDING
    )
    reversed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
