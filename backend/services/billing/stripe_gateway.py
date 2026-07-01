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


def construct_event(payload: bytes, sig_header: str, secret: str) -> dict:
    """Verify a webhook signature and return the parsed event.

    Raises on an invalid signature (caller maps to 400). Returns a Stripe Event
    object, which supports dict-style access identical to the dev-unverified path.
    """
    return stripe.Webhook.construct_event(payload, sig_header, secret)
