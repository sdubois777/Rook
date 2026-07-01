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

from backend.models.user import TIER_LIMITS
from backend.services.billing.catalog import price_to_tier

logger = logging.getLogger(__name__)


@dataclass
class WebhookResult:
    duplicate: bool = False
    handled: bool = False
    event_type: Optional[str] = None


class StripeWebhookService:
    def __init__(self, db, *, user_repo, user_service, events, invoices):
        self._db = db
        self._users = user_repo
        self._user_service = user_service
        self._events = events
        self._invoices = invoices

    @classmethod
    def from_session(cls, db) -> "StripeWebhookService":
        from backend.repositories.billing_repo import (
            GrantedInvoiceRepository,
            ProcessedStripeEventRepository,
        )
        from backend.repositories.user_repo import UserRepository
        from backend.services.user_service import UserService

        repo = UserRepository(db)
        return cls(
            db,
            user_repo=repo,
            user_service=UserService(repo),
            events=ProcessedStripeEventRepository(db),
            invoices=GrantedInvoiceRepository(db),
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
            # upgrade_tier grants the tier's signup bonus exactly once here (it is
            # the sole first-purchase signal); plan-change/downgrade pass False.
            await self._user_service.upgrade_tier(
                user, tier, grant_signup_bonus=True, commit=False
            )
            await self._users.set_subscription_status(user.id, "active")

        elif mode == "payment":
            credits = _as_int(metadata.get("credits"))
            if credits is None or credits <= 0:
                logger.warning("Stripe checkout: pack with no credits meta")
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

    async def _on_subscription_deleted(self, obj: dict) -> None:
        """The ONLY downgrade. Tier -> intro, credits persist, clear sub id."""
        user = await self._user_for(obj.get("customer"))
        if user is None:
            return
        await self._user_service.upgrade_tier(
            user, "intro", grant_signup_bonus=False, commit=False
        )
        await self._users.set_stripe_subscription_id(user.id, None)
        await self._users.set_subscription_status(user.id, "active")

    async def _on_invoice_payment_succeeded(self, obj: dict) -> None:
        """Grant monthly credits ONLY on a real renewal, once per invoice."""
        if obj.get("billing_reason") != "subscription_cycle":
            return  # signup / proration / manual invoices do NOT grant

        user = await self._user_for(obj.get("customer"))
        if user is None:
            return

        invoice_id = obj.get("id")
        credits = TIER_LIMITS.get(user.tier, {}).get("credits_monthly", 0)

        # Layer 2 — record the grant against the invoice id; skip if already done.
        is_new = await self._invoices.record_grant(invoice_id, user.id, credits)
        if not is_new:
            logger.info(
                "Stripe invoice %s already granted — skipping", invoice_id
            )
            return
        if credits > 0:
            await self._users.update_credits(user.id, credits)

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
        "invoice.payment_succeeded": _on_invoice_payment_succeeded,
        "invoice.payment_failed": _on_invoice_payment_failed,
    }


# ── helpers ─────────────────────────────────────────────────────────────

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
