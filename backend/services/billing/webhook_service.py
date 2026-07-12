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
from dataclasses import dataclass
from typing import Optional

from backend.services.billing.catalog import price_to_tier

logger = logging.getLogger(__name__)


@dataclass
class WebhookResult:
    duplicate: bool = False
    handled: bool = False
    event_type: Optional[str] = None


class StripeWebhookService:
    def __init__(self, db, *, user_repo, user_service, events, packs, leagues):
        self._db = db
        self._users = user_repo
        self._user_service = user_service
        self._events = events
        self._packs = packs
        self._leagues = leagues  # LeagueReconciler

    @classmethod
    def from_session(cls, db) -> "StripeWebhookService":
        from backend.repositories.billing_repo import (
            GrantedPackSessionRepository,
            ProcessedStripeEventRepository,
        )
        from backend.repositories.league_repo import LeagueRepository
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
        )

    async def process(self, event: dict) -> WebhookResult:
        event_id = event.get("id")
        event_type = event.get("type")

        # Layer 1 — global idempotency BEFORE any side effect.
        is_new = await self._events.mark_processed(event_id)
        if not is_new:
            logger.info("Stripe webhook: duplicate event %s ignored", event_id)
            return WebhookResult(duplicate=True, event_type=event_type)

        obj = (event.get("data") or {}).get("object") or {}
        handler = self._DISPATCH.get(event_type)
        if handler is not None:
            await handler(self, obj)
        else:
            logger.info("Stripe webhook: unhandled event type %s", event_type)

        await self._db.commit()
        return WebhookResult(handled=handler is not None, event_type=event_type)

    # ── user resolution (customer-id only) ──────────────────────────────

    async def _user_for(self, customer_id: Optional[str]):
        if not customer_id:
            logger.warning("Stripe webhook: event with no customer id")
            return None
        user = await self._users.get_by_stripe_customer_id(customer_id)
        if user is None:
            logger.warning(
                "Stripe webhook: no user for customer %s", customer_id
            )
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
        if tier and user.tier != tier:
            await self._user_service.upgrade_tier(
                user, tier, grant_signup_bonus=False, commit=False
            )
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

    async def _on_invoice_payment_failed(self, obj: dict) -> None:
        """Mark past_due; honor Stripe retries — do NOT downgrade (Decision #5)."""
        user = await self._user_for(obj.get("customer"))
        if user is None:
            return
        await self._users.set_subscription_status(user.id, "past_due")

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
