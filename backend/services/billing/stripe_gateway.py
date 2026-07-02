"""
Thin wrapper over the Stripe SDK.

Every outbound Stripe call and the inbound signature verification funnel through
here so (a) the api_key is set from `settings` in exactly one place, (b) handlers
never import `stripe` directly, and (c) tests can monkeypatch these functions
without a network call.

No card data ever passes through this module — Checkout/Portal are hosted by
Stripe; we only ever create sessions and read back opaque ids (§0.A).
"""
from __future__ import annotations

from typing import Optional

import stripe

from backend.config import settings


def _api_key() -> str:
    key = settings.stripe_secret_key
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")
    return key


def create_customer(
    email: Optional[str],
    external_id: str,
    user_id: str,
    idempotency_key: str,
) -> str:
    """Create a Stripe Customer bound to our user; return the customer id."""
    customer = stripe.Customer.create(
        api_key=_api_key(),
        email=email or None,
        metadata={"external_id": external_id, "user_id": user_id},
        idempotency_key=idempotency_key,
    )
    return customer["id"]


def create_checkout_session(
    *,
    customer_id: str,
    mode: str,
    price_id: str,
    success_url: str,
    cancel_url: str,
    metadata: dict,
    idempotency_key: str,
) -> str:
    """Create a Checkout Session (subscription or payment); return its URL.

    The card is collected on checkout.stripe.com — never on our origin.
    """
    session = stripe.checkout.Session.create(
        api_key=_api_key(),
        mode=mode,
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        idempotency_key=idempotency_key,
    )
    return session["url"]


def create_portal_session(*, customer_id: str, return_url: str) -> str:
    """Create a Customer Portal session (manage/cancel/card); return its URL."""
    session = stripe.billing_portal.Session.create(
        api_key=_api_key(),
        customer=customer_id,
        return_url=return_url,
    )
    return session["url"]


def _opt(obj, *keys):
    """Safe nested subscript for Stripe objects (StripeObject has no dict .get —
    attribute/`.get` access raises; subscript raises KeyError on a missing key)."""
    cur = obj
    for k in keys:
        try:
            cur = cur[k]
        except (KeyError, TypeError, AttributeError):
            return None
    return cur


def subscription_snapshot(sub_id: str) -> dict:
    """Read the fields change-plan needs off a subscription.

    In the current API the billing period lives on the subscription ITEM, not the
    top level — so period_start/period_end come from the first item.
    """
    sub = stripe.Subscription.retrieve(sub_id, api_key=_api_key())
    item = sub["items"]["data"][0]
    return {
        "status": sub["status"],
        "item_id": item["id"],
        "price_id": item["price"]["id"],
        "period_start": _opt(item, "current_period_start"),
        "period_end": _opt(item, "current_period_end"),
    }


def _proration_amount(preview) -> int:
    """Sum only the proration line items (the immediate charge/credit), in cents.

    A line is a proration via the new invoice shape
    (parent.subscription_item_details.proration) or the legacy flat flag.
    """
    total = 0
    for line in preview["lines"]["data"]:
        is_pro = _opt(line, "proration")
        if is_pro is None:
            is_pro = _opt(line, "parent", "subscription_item_details", "proration")
        if is_pro:
            total += line["amount"]
    return total


def preview_upgrade_amount(
    *, customer_id: str, sub_id: str, item_id: str,
    target_price_id: str, proration_date: int,
) -> int:
    """Preview the immediate proration for swapping the item to target_price_id at
    proration_date. Returns the amount due today in cents (>= 0). Changes nothing.
    """
    preview = stripe.Invoice.create_preview(
        api_key=_api_key(),
        customer=customer_id,
        subscription=sub_id,
        subscription_details={
            "items": [{"id": item_id, "price": target_price_id}],
            "proration_behavior": "create_prorations",
            "proration_date": proration_date,
        },
    )
    return max(0, _proration_amount(preview))


def apply_upgrade(
    *, sub_id: str, item_id: str, target_price_id: str,
    proration_date: int, idempotency_key: str,
) -> dict:
    """Swap the item to the higher price NOW, invoicing the proration immediately
    against the card on file. Reuses the preview's proration_date so the charge
    matches the preview. Returns {status}."""
    sub = stripe.Subscription.modify(
        sub_id,
        api_key=_api_key(),
        items=[{"id": item_id, "price": target_price_id}],
        proration_behavior="always_invoice",
        proration_date=proration_date,
        idempotency_key=idempotency_key,
    )
    return {"status": sub["status"]}


def schedule_downgrade(
    *, sub_id: str, current_price_id: str, target_price_id: str,
    idempotency_key: str,
) -> dict:
    """Schedule the price to drop to target at current_period_end — NO immediate
    proration/refund. The item price stays the same until then, so the webhook
    (price->tier) keeps the higher tier until the schedule advances. Returns
    {schedule_id, effective}."""
    schedule = stripe.SubscriptionSchedule.create(
        api_key=_api_key(), from_subscription=sub_id,
        idempotency_key=f"{idempotency_key}_create",
    )
    current = schedule["phases"][0]
    updated = stripe.SubscriptionSchedule.modify(
        schedule["id"],
        api_key=_api_key(),
        idempotency_key=idempotency_key,
        end_behavior="release",
        proration_behavior="none",
        phases=[
            {
                "items": [{"price": current_price_id, "quantity": 1}],
                "start_date": current["start_date"],
                "end_date": current["end_date"],
            },
            {
                "items": [{"price": target_price_id, "quantity": 1}],
            },
        ],
    )
    return {"schedule_id": updated["id"], "effective": current["end_date"]}


def construct_event(payload: bytes, sig_header: str, secret: str) -> dict:
    """Verify a webhook signature and return the parsed event.

    Raises on an invalid signature (caller maps to 400). Returns a Stripe Event
    object, which supports dict-style access identical to the dev-unverified path.
    """
    return stripe.Webhook.construct_event(payload, sig_header, secret)
