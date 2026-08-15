"""
ReferralRepository — query layer for referral_codes and code_redemptions.

Same posture as backend/repositories/billing_repo.py: the write methods are
insert-or-skip and return whether the row was NEW, because the caller gates a
side effect on that boolean (raising the referrer's coupon rate). Nothing here
commits — the Stripe webhook commits once after all of its side effects, so a
mid-handler failure rolls the whole thing back and Stripe redelivers cleanly.

A redemption row moves pending -> confirmed:

  pending    written when the Checkout Session is created, before payment. It
             holds the redeemer's one-per-kind slot so a second simultaneous
             checkout cannot receive the same discount. It stops holding the slot
             after PENDING_TTL_HOURS, because a Stripe session that old can never
             be paid and the customer would otherwise be locked out forever.
  confirmed  the signature-verified webhook saw the payment complete.
  reversed   NOT REACHED IN PRODUCTION. reverse_redemption is the only thing that
             writes this status and it has no caller — no refund or dispute event
             is handled anywhere. The queries below still accept the status so
             that adding a refund handler needs no query changes, but do not read
             a `reversed` row as evidence of anything today. See
             reverse_redemption at the bottom of this file.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models.referral import (
    PENDING_TTL_HOURS,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_REVERSED,
    CodeRedemption,
    ReferralCode,
)
from backend.models.user import TIER_LIMITS, TIER_ORDER, User

# The tiers that count as paying, derived from TIER_LIMITS rather than written
# out, and matching effective_tier() in backend/models/user.py: a tier is paid
# when it grants unlimited features.
_PAID_TIERS = tuple(
    tier for tier in TIER_ORDER if TIER_LIMITS[tier]["unlimited_features"]
)


def _pending_cutoff() -> datetime:
    """Reservations created before this instant are abandoned checkouts."""
    return datetime.now(timezone.utc) - timedelta(hours=PENDING_TTL_HOURS)


def _blocks_a_new_discount():
    """WHERE clause for a redemption row that still occupies its slot.

    A CONFIRMED row blocks forever: that one row is what limits an account to one
    discount of each kind for the life of the account, however many times it
    subscribes and cancels. A PENDING row blocks only while a Stripe session
    created alongside it could still be paid. REVERSED is listed so that a refund
    handler, if one is ever added, would not silently free the slot — nothing
    writes that status today (see reverse_redemption).
    """
    return or_(
        CodeRedemption.status.in_((STATUS_CONFIRMED, STATUS_REVERSED)),
        and_(
            CodeRedemption.status == STATUS_PENDING,
            CodeRedemption.created_at >= _pending_cutoff(),
        ),
    )


class ReferralRepository:
    def __init__(self, session):
        self._session = session

    # ── referral_codes ──────────────────────────────────────────────────

    async def get_code_for_user(self, user_id: uuid.UUID) -> Optional[ReferralCode]:
        """The user's shareable code row, or None if they have never had one."""
        result = await self._session.execute(
            select(ReferralCode).where(ReferralCode.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def create_code(
        self, user_id: uuid.UUID, code: str
    ) -> Optional[str]:
        """Claim `code` for `user_id`; return the code, or None if nothing inserted.

        None means one of the two UNIQUE constraints already held: the user got a
        code from a concurrent request, or this random code is already somebody
        else's. The caller cannot tell them apart from the return value alone and
        does not need to — ReferralService re-reads the user's row to decide
        whether to return the existing code or draw a fresh one.

        ON CONFLICT DO NOTHING carries no conflict target on purpose, so BOTH
        unique constraints resolve to a skip rather than an IntegrityError. An
        IntegrityError here would abort the surrounding transaction, and the
        caller's retry would then fail on every subsequent statement.
        """
        result = await self._session.execute(
            pg_insert(ReferralCode)
            .values(user_id=user_id, code=code)
            .on_conflict_do_nothing()
            .returning(ReferralCode.code)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[ReferralCode]:
        """Look up a code row. The input is stripped and uppercased first, because
        codes get typed by hand from a text message or read aloud over a call."""
        normalized = (code or "").strip().upper()
        if not normalized:
            return None
        result = await self._session.execute(
            select(ReferralCode).where(ReferralCode.code == normalized)
        )
        return result.scalar_one_or_none()

    # ── code_redemptions ────────────────────────────────────────────────

    async def confirmed_referral_count(self, referrer_user_id: uuid.UUID) -> int:
        """How many of this user's referrals are STILL PAYING right now.

        Not a count of history. A referral only pays the referrer for as long as
        the referred account holds a paid tier: five throwaway accounts that
        subscribe once and cancel must not buy a permanent recurring discount.
        The join to users is what makes the rate decay on its own — the referrer's
        coupon is recomputed from this number, so a cancellation lowers it the
        next time anything recomputes, with no correction step to get wrong.

        Paid-tier semantics are the same as effective_tier(): a paid tier, with
        an expiry that is either absent (monthly, managed by Stripe) or still in
        the future (an unexpired season pass).

        Soft-deleted accounts are excluded. The Clerk `user.deleted` handler sets
        users.deleted_at and touches neither the tier nor Stripe, so without this
        condition a deleted account keeps paying its referrer forever. It also
        matches ReferralService._resolve_referral, which already refuses a code
        whose OWNER is soft-deleted.

        Only CONFIRMED rows count. `reversed` is not excluded by a separate
        condition because nothing writes that status (see reverse_redemption);
        the equality on STATUS_CONFIRMED already leaves it out.

        THE COUNT IS RIGHT; THE PUSH TO STRIPE CAN BE LATE. Nothing recomputes
        this on a schedule — the referrer's coupon is only re-pushed when some
        Stripe event touches them (see StripeWebhookService._set_referrer_discount).
        A referred SEASON pass expiring produces no Stripe event at all, so the
        referrer can keep the higher coupon until the next event that touches
        them. Every read of this method returns the correct number.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(CodeRedemption)
            .join(User, User.id == CodeRedemption.redeemer_user_id)
            .where(CodeRedemption.referrer_user_id == referrer_user_id)
            .where(CodeRedemption.status == STATUS_CONFIRMED)
            .where(User.deleted_at.is_(None))
            .where(User.tier.in_(_PAID_TIERS))
            .where(
                or_(
                    User.tier_expires_at.is_(None),
                    User.tier_expires_at > datetime.now(timezone.utc),
                )
            )
        )
        return int(result.scalar() or 0)

    async def blocking_redemption_status(
        self, redeemer_user_id: uuid.UUID, kind: str
    ) -> Optional[str]:
        """The STATUS of the row occupying this user's slot, or None if it is free.

        Mirrors the UNIQUE(redeemer_user_id, kind) constraint plus the pending
        TTL, so the user hears about it at checkout instead of after paying. An
        expired reservation does not block — it belongs to a Stripe session that
        can no longer be paid, and holding the slot for it would lock the user
        out of a discount they never received.

        Returns the status rather than a bool so the caller can tell the two
        cases apart in the message it shows: STATUS_PENDING means a checkout is
        open right now and may still be paid, and anything else means the
        discount was already used. They are entirely different instructions to
        the customer.
        """
        result = await self._session.execute(
            select(CodeRedemption.status)
            .where(CodeRedemption.redeemer_user_id == redeemer_user_id)
            .where(CodeRedemption.kind == kind)
            .where(_blocks_a_new_discount())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def has_redeemed(self, redeemer_user_id: uuid.UUID, kind: str) -> bool:
        """True if this user's slot for this discount kind is taken, for any
        reason. Kept for callers that do not need to distinguish an open checkout
        from a spent discount; ReferralService uses blocking_redemption_status
        because its message does."""
        return (
            await self.blocking_redemption_status(redeemer_user_id, kind)
        ) is not None

    async def has_redeemed_from(
        self, *, redeemer_user_id: uuid.UUID, referrer_user_id: uuid.UUID
    ) -> bool:
        """True if `redeemer_user_id` has already taken `referrer_user_id`'s code.

        Asked with the two ids swapped, this is the mutual-referral test: before
        paying A for referring B, check that A did not already redeem B's code.
        Uses the same freshness rule as has_redeemed, so an abandoned checkout
        does not permanently block a legitimate referral in the other direction.
        """
        result = await self._session.execute(
            select(CodeRedemption.id)
            .where(CodeRedemption.redeemer_user_id == redeemer_user_id)
            .where(CodeRedemption.referrer_user_id == referrer_user_id)
            .where(_blocks_a_new_discount())
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def redemption_for_redeemer(
        self, redeemer_user_id: uuid.UUID
    ) -> Optional[CodeRedemption]:
        """The confirmed referral this user redeemed, if any.

        Used when this user's subscription ends: it names the referrer whose
        earned rate has to be recomputed.
        """
        result = await self._session.execute(
            select(CodeRedemption)
            .where(CodeRedemption.redeemer_user_id == redeemer_user_id)
            .where(CodeRedemption.referrer_user_id.isnot(None))
            .where(CodeRedemption.status == STATUS_CONFIRMED)
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ── reservations ────────────────────────────────────────────────────

    async def reserve_redemption(
        self,
        *,
        kind: str,
        code: str,
        redeemer_user_id: uuid.UUID,
        referrer_user_id: Optional[uuid.UUID],
        stripe_session_id: str,
        percent_off: int,
    ) -> Optional[uuid.UUID]:
        """Hold this user's one-per-kind slot; return the new row id, or None.

        None means the slot is taken: a confirmed or reversed redemption, or a
        checkout opened moments ago in another tab. The caller must not create a
        discounted Stripe session.

        An EXPIRED pending row still occupies the unique constraint, so it is
        deleted first and explicitly. Leaving it would make the insert conflict
        and lock the user out of a discount over a checkout they abandoned a day
        ago. The delete is narrow — pending only, and only past the TTL — so it
        can never remove a confirmed or reversed row. Two racing requests both
        run the delete (at most one finds a row) and then both insert; the unique
        constraint still lets exactly one win.
        """
        await self._session.execute(
            delete(CodeRedemption)
            .where(CodeRedemption.redeemer_user_id == redeemer_user_id)
            .where(CodeRedemption.kind == kind)
            .where(CodeRedemption.status == STATUS_PENDING)
            .where(CodeRedemption.created_at < _pending_cutoff())
        )
        result = await self._session.execute(
            pg_insert(CodeRedemption)
            .values(
                kind=kind,
                code=(code or "").strip().upper(),
                redeemer_user_id=redeemer_user_id,
                referrer_user_id=referrer_user_id,
                stripe_session_id=stripe_session_id,
                percent_off=percent_off,
                status=STATUS_PENDING,
            )
            .on_conflict_do_nothing()
            .returning(CodeRedemption.id)
        )
        return result.scalar_one_or_none()

    async def attach_session_id(
        self, reservation_id: uuid.UUID, stripe_session_id: str
    ) -> None:
        """Replace a reservation's provisional session id with the real one."""
        await self._session.execute(
            update(CodeRedemption)
            .where(CodeRedemption.id == reservation_id)
            .values(stripe_session_id=stripe_session_id)
        )

    async def release_reservation(self, reservation_id: uuid.UUID) -> None:
        """Delete a reservation whose checkout was never created.

        Restricted to pending rows so a mistimed call can never delete a
        confirmed redemption and hand the account a second discount.
        """
        await self._session.execute(
            delete(CodeRedemption)
            .where(CodeRedemption.id == reservation_id)
            .where(CodeRedemption.status == STATUS_PENDING)
        )

    async def confirm_redemption(self, stripe_session_id: str) -> bool:
        """Flip a reservation to confirmed; True only if THIS call flipped it.

        The status guard is the idempotency: Stripe delivers at least once, and a
        redelivery finds the row already confirmed, matches nothing, and returns
        False. The caller gates the referrer's reward on that boolean, so the
        reward moves exactly once per completed checkout.
        """
        result = await self._session.execute(
            update(CodeRedemption)
            .where(CodeRedemption.stripe_session_id == stripe_session_id)
            .where(CodeRedemption.status == STATUS_PENDING)
            .values(status=STATUS_CONFIRMED)
            .returning(CodeRedemption.id)
        )
        return result.scalar_one_or_none() is not None

    async def record_redemption(
        self,
        *,
        kind: str,
        code: str,
        redeemer_user_id: uuid.UUID,
        referrer_user_id: Optional[uuid.UUID],
        stripe_session_id: str,
        percent_off: int,
    ) -> bool:
        """Record a CONFIRMED discount outright; return True only if the row is NEW.

        The fallback for a completed checkout with no reservation to confirm.
        True is the caller's permission to raise the referrer's coupon rate. False
        means this checkout was already recorded (Stripe redelivery), or this
        account already holds this kind of discount — in both cases the reward
        must not move.

        TWO unique constraints can fire here, and they must BOTH resolve to a
        skip:

          UNIQUE(stripe_session_id)       the same completed checkout redelivered
          UNIQUE(redeemer_user_id, kind)  a second checkout by an account that
                                          already holds this kind of discount

        `on_conflict_do_nothing()` is called with no conflict target so it covers
        every unique constraint on the table. Naming one via index_elements would
        leave the other raising IntegrityError, which aborts the webhook's
        transaction — the commit fails, Stripe sees a 500, and it retries the same
        event forever because every retry hits the same constraint.

        THERE IS NO SAVEPOINT AND NO IntegrityError CATCH, and that is deliberate.
        Leaving a nested transaction flushes the WHOLE session, so an
        IntegrityError raised by the webhook's own pending tier upgrade would be
        caught here, reported as "already redeemed", and rolled back to the
        savepoint — the customer pays, loses the upgrade, and the event is still
        marked processed so Stripe never retries. Untargeted ON CONFLICT already
        covers every unique constraint on this table; anything else that raises
        belongs to somebody else's statement and must propagate so the webhook
        fails and Stripe redelivers.
        """
        result = await self._session.execute(
            pg_insert(CodeRedemption)
            .values(
                kind=kind,
                code=(code or "").strip().upper(),
                redeemer_user_id=redeemer_user_id,
                referrer_user_id=referrer_user_id,
                stripe_session_id=stripe_session_id,
                percent_off=percent_off,
                status=STATUS_CONFIRMED,
            )
            .on_conflict_do_nothing()
        )
        return result.rowcount > 0

    async def reverse_redemption(
        self, stripe_session_id: str
    ) -> Optional[CodeRedemption]:
        """Mark a redemption reversed (refund/chargeback); return the row.

        THIS METHOD HAS NO CALLER. No refund or dispute event is handled — the
        webhook dispatch table in backend/services/billing/webhook_service.py
        registers checkout, subscription and invoice-failure events only — so
        STATUS_REVERSED is never written in production. It is kept, rather than
        deleted, because the schema, the slot query and these tests are the
        finished half of refund handling: adding `charge.refunded` and
        `charge.dispute.created` to that dispatch table is then one handler that
        calls this and re-pushes the referrer's rate.

        Do not rely on it for anti-farming today. What actually limits a refunded
        or cancelled referral is confirmed_referral_count, which counts only
        referrals whose referred account holds a paid tier RIGHT NOW.

        Returns None when there is no such redemption or it was already reversed,
        so a caller could lower the referrer's coupon exactly once. The row is
        never deleted: it keeps occupying the redeemer's one-per-kind slot.
        """
        result = await self._session.execute(
            select(CodeRedemption).where(
                CodeRedemption.stripe_session_id == stripe_session_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None or row.status == STATUS_REVERSED:
            return None
        row.status = STATUS_REVERSED
        row.reversed_at = datetime.now(timezone.utc)
        await self._session.flush()
        return row

    async def commit(self) -> None:
        await self._session.commit()
