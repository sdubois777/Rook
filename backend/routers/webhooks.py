"""
Webhook handlers for external service events.

Clerk webhooks: user lifecycle (created, deleted).
Stripe webhooks: subscription events (future).

All webhooks verify signatures before processing.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import get_db
from backend.database import AsyncSessionLocal
from backend.models.user import TIER_LIMITS, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _verify_clerk_signature(request: Request) -> dict:
    """
    Verify Clerk webhook signature using svix.
    Returns parsed event dict.
    Raises 400 if signature invalid.
    """
    from backend.config import settings

    webhook_secret = settings.clerk_webhook_secret
    if not webhook_secret:
        if settings.environment == "production":
            raise HTTPException(
                status_code=400,
                detail="Webhook secret not configured",
            )
        # Dev: parse without verification
        body = await request.body()
        return json.loads(body)

    try:
        from svix.webhooks import Webhook

        body = await request.body()
        headers = dict(request.headers)

        wh = Webhook(webhook_secret)
        return wh.verify(body, headers)
    except Exception as e:
        logger.warning("Webhook signature invalid: %s", e)
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook signature",
        )


@router.post("/clerk")
async def clerk_webhook(request: Request):
    """
    Handle Clerk user lifecycle events.

    Events handled:
      user.created -> ensure user record exists in DB
      user.deleted -> soft delete user record
    """
    event = await _verify_clerk_signature(request)
    event_type = event.get("type")
    data = event.get("data", {})

    logger.info("Clerk webhook: %s", event_type)

    async with AsyncSessionLocal() as db:
        if event_type == "user.created":
            email = ""
            email_addresses = data.get("email_addresses", [])
            if email_addresses:
                email = email_addresses[0].get("email_address", "")

            first = data.get("first_name", "") or ""
            last = data.get("last_name", "") or ""
            display_name = f"{first} {last}".strip()

            await db.execute(
                pg_insert(User)
                .values(
                    external_id=data["id"],
                    email=email,
                    display_name=display_name or None,
                    tier="intro",
                    credits_remaining=TIER_LIMITS["intro"]["credits_signup_bonus"],
                )
                .on_conflict_do_nothing(index_elements=["external_id"])
            )
            logger.info("Clerk webhook: created user %s", data["id"])

        elif event_type == "user.deleted":
            await db.execute(
                update(User)
                .where(User.external_id == data["id"])
                .values(deleted_at=datetime.now(timezone.utc))
            )
            logger.info("Clerk webhook: soft deleted user %s", data["id"])

        await db.commit()

    return {"ok": True}


# ── Stripe ──────────────────────────────────────────────────────────────

async def _verify_stripe_signature(request: Request) -> dict:
    """
    Verify a Stripe webhook signature and return the parsed event.

    Mirrors the Clerk posture: production REQUIRES the signing secret (an
    unverified endpoint is an entitlement-forgery hole, §0.B); development may
    parse without verification for local Stripe-CLI convenience.

    Raises 400 on an unverified/invalid payload — with NO side effects.
    """
    from backend.config import settings

    body = await request.body()
    webhook_secret = settings.stripe_webhook_secret

    if not webhook_secret:
        if settings.environment == "production":
            raise HTTPException(
                status_code=400,
                detail="Webhook secret not configured",
            )
        # Dev: parse without verification
        return json.loads(body)

    sig_header = request.headers.get("stripe-signature", "")
    try:
        from backend.services.billing import stripe_gateway

        return stripe_gateway.construct_event(body, sig_header, webhook_secret)
    except Exception as e:
        logger.warning("Stripe webhook signature invalid: %s", e)
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook signature",
        )


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle Stripe subscription/billing events.

    The SOLE entitlement-granting path: signature-verified, globally idempotent
    on event.id, resolves the user by the stored customer id only, and drives the
    §4 transitions through StripeWebhookService.
    """
    event = await _verify_stripe_signature(request)

    from backend.services.billing.webhook_service import StripeWebhookService

    service = StripeWebhookService.from_session(db)
    result = await service.process(event)

    logger.info(
        "Stripe webhook: %s (duplicate=%s, handled=%s)",
        result.event_type, result.duplicate, result.handled,
    )
    return {"ok": True}
