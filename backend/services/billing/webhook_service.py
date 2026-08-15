"""
StripeWebhookService — the §4 event state machine.

This is the SOLE entitlement-granting path (§0.B). It is signature-verified
upstream (the router), globally deduped by event.id (layer 1), and resolves the
affected user ONLY by the stored `stripe_customer_id` — never a client-influenced
payload field (§0.C).

Collaborators are injected so the dispatch logic is unit-testable with fakes (the
unit suite never touches a real DB). `from_session` wires the real repos/service.

Transaction: mark the event, run the one matching handler, then commit ONCE. A
handler exception rolls the event record back so Stripe's redelivery reprocesses
cleanly (that's why a failed payment is honored via retry, not custom grace).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from backend.models.user import referrer_percent_off
from backend.services.billing.catalog import price_to_tier

logger = logging.getLogger(__name__)


@dataclass
class WebhookResult:
    duplicate: bool = False
    handled: bool = False
    event_type: Optional[str] = None
    retry: bool = False  # entitlement event whose customer didn't resolve → make Stripe redeliver


class UnmatchedCustomerError(Exception):
    """An entitlement-bearing event carried a customer id that resolves to no user.
    This is a 'should have matched but didn't' failure (a real payment we can't
    apply), NOT a benign no-op — the event must NOT be recorded so Stripe retries."""

    def __init__(self, customer_id: str):
        super().__init__(f"No user for Stripe customer {customer_id}")
        self.customer_id = customer_id


class StripeWebhookService:
    def __init__(
        self, db, *, user_repo, user_service, events, packs, leagues, referrals
    ):
        self._db = db
        self._users = user_repo
        self._user_service = user_service
        self._events = events
        self._packs = packs
        self._leagues = leagues  # LeagueReconciler
        self._referrals = referrals  # ReferralRepository

    @classmethod
    def from_session(cls, db) -> "StripeWebhookService":
        from backend.repositories.billing_repo import (
            GrantedPackSessionRepository,
            ProcessedStripeEventRepository,
        )
        from backend.repositories.league_repo import LeagueRepository
        from backend.repositories.referral_repo import ReferralRepository
        from backend.repositories.user_repo import UserRepository
        from backend.services.league_reconcile import LeagueReconciler
        from backend.services.user_service import UserService

        repo = UserRepository(db)
        return cls(
            db,
            user_repo=repo,
            user_service=UserService(repo),
            events=ProcessedStripeEventRepository(db),
            packs=GrantedPackSessionRepository(db),
            leagues=LeagueReconciler(LeagueRepository(db)),
            referrals=ReferralRepository(db),
        )

    async def process(self, event: dict) -> WebhookResult:
        event_id = event.get("id")
        event_type = event.get("type")

        # Layer 1 — global idempotency BEFORE any side effect. The insert lives in
        # THIS uncommitted transaction: a genuine duplicate (from a prior COMMITTED
        # run) is caught here and skipped before any side effect, while a failure
        # below rolls this insert back so a redelivery reprocesses cleanly.
        is_new = await self._events.mark_processed(event_id)
        if not is_new:
            logger.info("Stripe webhook: duplicate event %s ignored", event_id)
            return WebhookResult(duplicate=True, event_type=event_type)

        obj = (event.get("data") or {}).get("object") or {}
        handler = self._DISPATCH.get(event_type)

        # NOT APPLICABLE: an event type we intentionally ignore. Record + 200 —
        # retrying would never change the outcome.
        if handler is None:
            logger.info("Stripe webhook: unhandled event type %s", event_type)
            await self._db.commit()
            return WebhookResult(handled=False, event_type=event_type)

        try:
            await handler(self, obj)
        except UnmatchedCustomerError as exc:
            # SHOULD have matched but didn't. Roll back so the event is NOT recorded
            # (the mark_processed insert is undone), then signal a retry. A generic
            # handler exception also rolls back — it just propagates (→ 500) via the
            # get_db dependency, same redelivery outcome.
            await self._db.rollback()
            logger.error(
                "Stripe webhook UNMATCHED CUSTOMER — not recorded, will retry. "
                "event_id=%s type=%s customer=%s",
                event_id, event_type, exc.customer_id,
            )
            return WebhookResult(retry=True, event_type=event_type)

        # Success (side effect applied, or a benign no-op inside the handler such as
        # an event with no customer id). Commit the mark + side effects together.
        await self._db.commit()
        return WebhookResult(handled=True, event_type=event_type)

    # ── user resolution (customer-id only) ──────────────────────────────

    async def _user_for(self, customer_id: Optional[str]):
        """Resolve the user for an event, by stored customer id ONLY.

        Returns None ONLY when the event carries NO customer id (not applicable —
        nothing to match; the handler no-ops and the event is recorded + 200'd).
        A customer id that resolves to no user RAISES UnmatchedCustomerError — a
        real payment we can't apply must be retried, never silently dropped.
        """
        if not customer_id:
            logger.warning("Stripe webhook: event with no customer id")
            return None
        user = await self._users.get_by_stripe_customer_id(customer_id)
        if user is None:
            raise UnmatchedCustomerError(customer_id)
        return user

    # ── handlers (§4) ───────────────────────────────────────────────────

    async def _on_checkout_completed(self, obj: dict) -> None:
        """Authoritative 'started' signal for both subscriptions and packs."""
        user = await self._user_for(obj.get("customer"))
        if user is None:
            return

        mode = obj.get("mode")
        metadata = obj.get("metadata") or {}

        if mode == "subscription":
            subscription_id = obj.get("subscription")
            tier = metadata.get("tier")
            if not tier:
                logger.warning("Stripe checkout: subscription with no tier meta")
                return
            if subscription_id:
                await self._users.set_stripe_subscription_id(
                    user.id, subscription_id
                )
            # (Signup bonuses on paid tiers are 0 under the new spec — the only
            # signup grant is the free tier's 30, applied at account creation.)
            await self._user_service.upgrade_tier(
                user, tier, grant_signup_bonus=False, commit=False
            )
            # A monthly subscription supersedes any season expiry — Stripe's
            # subscription lifecycle manages the entitlement from here.
            await self._users.set_tier_expiry(user.id, None)
            await self._users.set_subscription_status(user.id, "active")
            await self._leagues.reconcile_for_tier(user.id, tier)
            # AFTER the tier write, never before: the referrer's rate counts
            # referrals whose referred user holds a paid tier RIGHT NOW, and this
            # customer only became one on the line above. The count query reads
            # the same uncommitted transaction, so ordering is what makes this
            # referral count toward the reward it just earned.
            await self._confirm_referral_redemption(user, obj, metadata)
            # This user may ALSO be a referrer. If they collected referrals while
            # on the free tier, this subscription is the first recurring invoice
            # their earned rate can attach to.
            await self._apply_own_referrer_rate(user, subscription_id, obj)

        elif mode == "payment" and (obj.get("metadata") or {}).get("interval") == "season":
            # SEASON purchase: one-time payment -> tier held until the season
            # entitlement end (users.tier_expires_at). Not a subscription — no
            # renewal, no proration.
            tier = metadata.get("tier")
            if not tier:
                logger.warning("Stripe checkout: season payment with no tier meta")
                return
            await self._user_service.upgrade_tier(
                user, tier, grant_signup_bonus=False, commit=False
            )
            await self._users.set_tier_expiry(user.id, _season_end())
            await self._users.set_subscription_status(user.id, "active")
            await self._leagues.reconcile_for_tier(user.id, tier)
            # Best-effort: stop double-billing an active monthly sub — it ends
            # at the period the user already paid for; the season carries on.
            if user.stripe_subscription_id:
                try:
                    from backend.services.billing import stripe_gateway
                    stripe_gateway.cancel_at_period_end(
                        sub_id=user.stripe_subscription_id,
                        idempotency_key=f"season_cancel_{user.id}_{obj.get('id')}",
                    )
                except Exception as exc:  # never fail the entitlement grant
                    logger.warning(
                        "Could not cancel monthly sub %s after season purchase: %s",
                        user.stripe_subscription_id, exc,
                    )

        elif mode == "payment":
            credits = _as_int(metadata.get("credits"))
            if credits is None or credits <= 0:
                logger.warning("Stripe checkout: pack with no credits meta")
                return
            # §6 pack idempotency: grant once per checkout session id, even if the
            # completed event is redelivered under a different event id.
            session_id = obj.get("id")
            is_new = await self._packs.record_grant(session_id, user.id, credits)
            if not is_new:
                logger.info("Stripe pack session %s already granted", session_id)
                return
            await self._users.update_credits(user.id, credits)

    async def _on_subscription_created(self, obj: dict) -> None:
        """Reconcile: set tier from price only if checkout hasn't already."""
        user = await self._user_for(obj.get("customer"))
        if user is None:
            return

        sub_id = obj.get("id")
        if sub_id and user.stripe_subscription_id != sub_id:
            await self._users.set_stripe_subscription_id(user.id, sub_id)

        tier = price_to_tier(_first_price_id(obj))
        if tier:
            if user.tier != tier:
                await self._user_service.upgrade_tier(
                    user, tier, grant_signup_bonus=False, commit=False
                )
            # A monthly subscription supersedes any season expiry (mirrors the
            # checkout branch). Without this, a sub that arrives without our
            # checkout metadata (dashboard-created, or checkout.session.completed
            # never landing) leaves a stale past-dated tier_expires_at from a
            # prior season pass — effective_tier() then reads a paying subscriber
            # as free, and the /account/me lazy write-back persists that
            # downgrade. Cleared only when this event actually grants a paid
            # tier (an unmapped price grants nothing).
            await self._users.set_tier_expiry(user.id, None)
        await self._users.set_subscription_status(user.id, "active")
        await self._leagues.reconcile_for_tier(user.id, tier or user.tier)

    async def _on_subscription_updated(self, obj: dict) -> None:
        """Tier change vs cancel-scheduled vs past_due — kept distinct. No downgrade."""
        user = await self._user_for(obj.get("customer"))
        if user is None:
            return

        status = obj.get("status")
        cancel_at_period_end = bool(obj.get("cancel_at_period_end"))

        if cancel_at_period_end and status == "active":
            # Keeps their tier through the paid period; downgrade waits for
            # subscription.deleted (Decision #4).
            await self._users.set_subscription_status(user.id, "canceling")
            return
        if status == "past_due":
            await self._users.set_subscription_status(user.id, "past_due")
            return
        if status == "active":
            await self._users.set_subscription_status(user.id, "active")
            tier = price_to_tier(_first_price_id(obj))
            if tier and user.tier != tier:
                await self._user_service.upgrade_tier(
                    user, tier, grant_signup_bonus=False, commit=False
                )
                # Restore parked leagues on a tier RISE; a drop leaves the
                # computed over-limit state (reconcile never auto-parks).
                await self._leagues.reconcile_for_tier(user.id, tier)

    async def _on_subscription_deleted(self, obj: dict) -> None:
        """The monthly downgrade path. Tier -> free, credits persist, clear sub
        id. EXCEPTION: an unexpired SEASON entitlement keeps its tier — this
        fires when a monthly sub ends after a season purchase superseded it."""
        from datetime import datetime, timezone

        user = await self._user_for(obj.get("customer"))
        if user is None:
            return
        expires = getattr(user, "tier_expires_at", None)
        season_active = (
            expires is not None and datetime.now(timezone.utc) < expires
        )
        if not season_active:
            await self._user_service.upgrade_tier(
                user, "free", grant_signup_bonus=False, commit=False
            )
        await self._users.set_stripe_subscription_id(user.id, None)
        await self._users.set_subscription_status(user.id, "active")
        if not season_active:
            # Drop to free (cap 1). Never auto-parks — if active > 1 the account
            # is in the computed over-limit "must choose" state until resolved.
            await self._leagues.reconcile_for_tier(user.id, "free")
            # This account stopped paying, so whoever referred it stops being paid
            # for it. Runs after the downgrade so the recomputed count already
            # excludes this user.
            await self._recompute_referrer_rate(user, obj)

    async def _on_invoice_payment_failed(self, obj: dict) -> None:
        """Mark past_due; honor Stripe retries — do NOT downgrade (Decision #5)."""
        user = await self._user_for(obj.get("customer"))
        if user is None:
            return
        await self._users.set_subscription_status(user.id, "past_due")

    # ── referral rewards ────────────────────────────────────────────────

    async def _confirm_referral_redemption(
        self, user, obj: dict, metadata: dict
    ) -> None:
        """Turn a paid checkout's discount into a confirmed redemption + reward.

        SUBSCRIPTION CHECKOUTS ONLY. Season passes and credit packs never carry a
        discount code, and a recurring referrer reward has no recurring invoice to
        attach to on a one-time payment.

        The metadata was written by our own checkout endpoint, not by the client,
        so it is a safe source for the kind and the referrer id. It exists because
        the code itself cannot be re-judged here: by the time this event lands the
        code may have been reused, revoked, or its owner deleted.
        """
        kind = metadata.get("redeemed_kind")
        session_id = obj.get("id")
        if not kind or not session_id:
            return
        referrer_id = _as_uuid(metadata.get("referrer_user_id"))

        # The reservation written at checkout time is the normal path. Flipping it
        # returns True exactly once, so a redelivered event pays no second reward.
        confirmed = await self._referrals.confirm_redemption(session_id)
        if not confirmed:
            # No pending row to flip. Either this event is a redelivery (the row
            # is already confirmed), or no reservation was ever written. Recording
            # it now covers the second case and is idempotent on the session id,
            # so the first case returns False and stops here.
            #
            # It also returns False when the account already holds this kind of
            # discount under a DIFFERENT session id — an abandoned reservation
            # that expired, was replaced, and then had its old Stripe session paid
            # after all. No reward moves, which is the safe direction: Stripe has
            # already applied the coupon, and we decline to pay a second referrer.
            recorded = await self._referrals.record_redemption(
                kind=kind,
                code=metadata.get("redeemed_code") or "",
                redeemer_user_id=user.id,
                referrer_user_id=referrer_id,
                stripe_session_id=session_id,
                percent_off=_as_int(metadata.get("redeemed_percent_off")) or 0,
            )
            if not recorded:
                logger.info(
                    "Referral redemption for session %s already recorded", session_id
                )
                return

        if referrer_id is None:
            return  # welcome code — nobody earns anything

        referrer = await self._users.get(referrer_id)
        if referrer is None:
            logger.warning("Referrer %s no longer exists — no reward", referrer_id)
            return

        count = await self._referrals.confirmed_referral_count(referrer_id)
        percent = referrer_percent_off(count)
        self._set_referrer_discount(referrer, percent, key_suffix=str(session_id))
        await self._notify_referrer(referrer, percent, count)

    async def _apply_own_referrer_rate(
        self, user, subscription_id: Optional[str], obj: dict
    ) -> None:
        """Put THIS user's own earned referrer rate on the subscription they just
        started.

        WHY THIS EXISTS. A referrer's coupon is otherwise only pushed when a NEW
        referral lands (_confirm_referral_redemption) or when one goes away
        (_recompute_referrer_rate). Both need a live subscription to write to, so
        every reward earned while the referrer was on the free tier was skipped
        and never revisited. A user who referred five friends and then subscribed
        started at 0%. This is the missing "when they do subscribe" path.

        THE RATE IS NOT STORED, so nothing is being replayed: it is recomputed
        from the confirmed count, exactly like every other push.

        WHAT HAPPENS WHEN THE SAME CHECKOUT ALSO CARRIED A ONE-TIME CODE (this
        user is a redeemer AND a referrer). The two coupons live in different
        places: the redeemer's one-time coupon is attached to the CHECKOUT
        SESSION (billing.py passes `discounts=` to create_checkout_session) and
        the referrer's recurring coupon is attached to the SUBSCRIPTION. But
        stripe_gateway.set_subscription_discount REPLACES the subscription's
        discount list rather than adding to it, so the two do not coexist on the
        subscription. The order is what makes that safe: a subscription-mode
        checkout collects the first payment on Stripe's page, so by the time
        checkout.session.completed reaches us the first invoice is already paid
        WITH the one-time discount. Replacing the list here changes renewals
        only. The customer keeps the one-time discount they were shown and gains
        the recurring one from the second invoice on.

        The `percent <= 0` skip is load-bearing for the same reason:
        referrer_coupon_id(0) is None, which CLEARS every discount on the
        subscription — including the one-time coupon that has not necessarily
        finished being applied. A user with no referrals must not have their
        discounts touched at all.

        Best-effort in every direction, like the coupon calls around it: the
        entitlement is already granted and failing here would roll it back and
        make Stripe redeliver a payment we already applied.
        """
        # The event's own subscription id, not user.stripe_subscription_id: the
        # repository write above is an UPDATE statement and does not necessarily
        # refresh the in-memory row.
        if not subscription_id:
            return
        try:
            count = await self._referrals.confirmed_referral_count(user.id)
            percent = referrer_percent_off(count)
            if percent <= 0:
                return
            self._push_subscription_coupon(
                sub_id=subscription_id,
                user_id=user.id,
                percent=percent,
                # "self_" so this can never share an idempotency key with the
                # reward push for the REFERRER of this same checkout, which is
                # keyed on the same session id.
                key_suffix=f"self_{obj.get('id')}",
            )
        except Exception:  # never fail an entitlement we already granted
            logger.exception(
                "Could not apply user %s's own earned referrer rate", user.id
            )

    async def _recompute_referrer_rate(self, user, obj: dict) -> None:
        """Lower a referrer's rate after the account they referred stopped paying.

        Best effort in every direction. This is a courtesy correction on somebody
        else's subscription; failing the webhook over it would roll back THIS
        user's downgrade and make Stripe redeliver forever.
        """
        try:
            row = await self._referrals.redemption_for_redeemer(user.id)
            if row is None or row.referrer_user_id is None:
                return
            referrer = await self._users.get(row.referrer_user_id)
            if referrer is None:
                return
            count = await self._referrals.confirmed_referral_count(
                row.referrer_user_id
            )
            self._set_referrer_discount(
                referrer, referrer_percent_off(count), key_suffix=str(obj.get("id"))
            )
        except Exception:  # never fail the downgrade over the reward
            logger.exception(
                "Could not recompute the referrer rate after %s cancelled", user.id
            )

    def _set_referrer_discount(self, referrer, percent: int, *, key_suffix: str) -> None:
        """Apply the referrer's summed reward rate to their live subscription.

        Best-effort, following the season-cancel call above: a Stripe failure
        here must not roll back an entitlement we already granted. The rate is
        recomputed from the current count every time, so the next event that
        touches this referrer repairs a call that failed.

        WHEN THE RE-PUSH IS LATE. Nothing recomputes rates on a schedule; a rate
        only moves when a Stripe event reaches one of the three callers. A
        referred SEASON pass simply expiring produces no Stripe event at all, so
        this referrer can carry a coupon one step too high until some other event
        touches them. The COUNT is always right (confirmed_referral_count reads
        live tier state); only the push to Stripe lags.
        """
        sub_id = getattr(referrer, "stripe_subscription_id", None)
        if not sub_id:
            # A referrer on the free tier, or one who cancelled, has no recurring
            # invoice to discount, so there is nothing to write today. It is not
            # lost: _apply_own_referrer_rate re-derives the rate from the count
            # when this referrer's own subscription starts.
            logger.info(
                "Referrer %s has no live subscription — reward rate %s%% not applied",
                referrer.id, percent,
            )
            return
        self._push_subscription_coupon(
            sub_id=sub_id, user_id=referrer.id, percent=percent, key_suffix=key_suffix
        )

    def _push_subscription_coupon(
        self, *, sub_id: str, user_id, percent: int, key_suffix: str
    ) -> None:
        """Write one coupon at `percent` onto a live subscription. Never raises.

        `key_suffix` is the id of the event that triggered the change. It has to
        vary, because a rate that goes 30 -> 20 -> 30 would otherwise reuse the
        idempotency key of the first call and Stripe would replay that response
        instead of applying anything.
        """
        try:
            from backend.services.billing import catalog, stripe_gateway

            stripe_gateway.set_subscription_discount(
                sub_id=sub_id,
                # None at 0% clears every discount — there is no zero-percent
                # coupon object to point at.
                coupon_id=catalog.referrer_coupon_id(percent),
                idempotency_key=f"refrate_{user_id}_{percent}_{key_suffix}",
            )
        except Exception as exc:
            logger.warning(
                "Could not set user %s subscription discount to %s%%: %s",
                user_id, percent, exc,
            )

    async def _notify_referrer(self, referrer, percent: int, count: int) -> None:
        """Tell the referrer their rate went up. Never fails the webhook.

        The email goes out on a SEPARATE database session on purpose.
        EmailService.send commits the session it was built from — it has to, since
        a message cannot be un-sent and its dedupe row must outlive any later
        rollback. Handing it this webhook's session would commit half-applied
        entitlement state before the handler finished.

        THE COST OF THAT CHOICE: this opens a SECOND pooled connection while the
        webhook's own transaction is still open, so one webhook can hold two
        connections at once. Harmless at current volume — referral rewards are
        rare and the pool in backend/database.py is 20 + 20 overflow — but if
        concurrent webhook processing ever approaches that size, this doubles the
        demand and a pool-timeout here would surface as a failed email, not a
        failed entitlement.
        """
        try:
            from backend.database import AsyncSessionLocal
            from backend.services.email.email_service import EmailService

            async with AsyncSessionLocal() as email_db:
                await EmailService.from_session(email_db).send_referral_reward(
                    user=referrer,
                    new_total_percent=percent,
                    referral_count=count,
                )
        except Exception:
            logger.exception(
                "Could not send the referral reward email to %s", referrer.id
            )

    _DISPATCH = {
        "checkout.session.completed": _on_checkout_completed,
        "customer.subscription.created": _on_subscription_created,
        "customer.subscription.updated": _on_subscription_updated,
        "customer.subscription.deleted": _on_subscription_deleted,
        "invoice.payment_failed": _on_invoice_payment_failed,
    }


# ── helpers ─────────────────────────────────────────────────────────────

def _season_end():
    """The instant a season purchase entitles through: March 1 after the season
    being played — aligned with backend.utils.seasons (March new-league-year
    cutoff), so "the season" means the same thing everywhere."""
    from datetime import datetime, timezone

    from backend.utils.seasons import get_current_season

    return datetime(get_current_season() + 1, 3, 1, tzinfo=timezone.utc)


def _first_price_id(subscription_obj: dict) -> Optional[str]:
    items = (subscription_obj.get("items") or {}).get("data") or []
    if not items:
        return None
    price = items[0].get("price") or {}
    return price.get("id")


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_uuid(value) -> Optional[uuid.UUID]:
    """Parse a metadata id, or None. Stripe metadata cannot hold a null, so the
    checkout endpoint writes an empty string when there is no referrer."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
