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
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import get_db
from backend.database import AsyncSessionLocal
from backend.models.user import TIER_LIMITS, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _is_mailable(email: str) -> bool:
    """False for an address nothing should ever be sent to.

    EmailService refuses these as well, but the check belongs here too: a
    placeholder address must not even reach the send path, because the dedupe key
    would then be claimed by a message that could never be delivered — and the
    real address, if one arrives later, would find the slot taken.

    The list of synthesized placeholder domains lives in email_service, so it is
    imported rather than restated. An import failure here means no welcome email
    rather than a failed signup webhook.
    """
    try:
        from backend.services.email.email_service import _is_deliverable

        return _is_deliverable((email or "").strip().lower())
    except Exception:
        logger.exception("Could not check whether %r is mailable", email)
        return False


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


async def _send_welcome_email(user_id, email: str, display_name: str | None) -> None:
    """Email a brand-new signup their welcome code and their referral code.

    NEVER RAISES. Clerk retries a webhook that answers with an error, and a retry
    of user.created would run the insert again — so a mail provider outage must
    not become a signup loop.

    Runs on its own database session, separate from the one that created the
    user. EmailService.send commits the session it was built from (its dedupe row
    has to survive a later rollback, since a message cannot be un-sent), and the
    referral code minted here needs committing anyway.
    """
    try:
        from backend.services.email.email_service import EmailService
        from backend.services.referral_service import ReferralService

        async with AsyncSessionLocal() as db:
            referrals = ReferralService.from_session(db)
            # get_or_create_code does not commit — this caller does.
            referral_code = await referrals.get_or_create_code(user_id)
            await db.commit()

            await EmailService.from_session(db).send_welcome(
                # A stand-in for the User row: send_welcome reads id, email and
                # display_name only, and re-reading the row we just wrote would
                # buy nothing.
                user=SimpleNamespace(
                    id=user_id, email=email, display_name=display_name
                ),
                promo_code=referrals.welcome_code_for(user_id),
                referral_code=referral_code,
            )
    except Exception:
        logger.exception("Could not send the welcome email to user %s", user_id)


@router.post("/clerk")
async def clerk_webhook(request: Request):
    """
    Handle Clerk user lifecycle events.

    Events handled:
      user.created -> ensure user record exists in DB, then welcome-email it
      user.deleted -> soft delete user record
    """
    event = await _verify_clerk_signature(request)
    event_type = event.get("type")
    data = event.get("data", {})

    logger.info("Clerk webhook: %s", event_type)

    created_user_id = None
    email = ""
    display_name = ""

    async with AsyncSessionLocal() as db:
        if event_type == "user.created":
            email_addresses = data.get("email_addresses", [])
            if email_addresses:
                email = email_addresses[0].get("email_address", "")

            first = data.get("first_name", "") or ""
            last = data.get("last_name", "") or ""
            display_name = f"{first} {last}".strip()

            result = await db.execute(
                pg_insert(User)
                .values(
                    external_id=data["id"],
                    email=email,
                    display_name=display_name or None,
                    tier="free",
                    credits_remaining=TIER_LIMITS["free"]["credits_signup_bonus"],
                )
                .on_conflict_do_nothing(index_elements=["external_id"])
                .returning(User.id)
            )
            created_user_id = result.scalar_one_or_none()

            if created_user_id is None:
                # The row already existed. DO NOT read this as "not a new signup"
                # and skip the welcome email — that was a real bug, and it silently
                # cost a real user their email.
                #
                # There are TWO paths that create a user row. This webhook is one.
                # The other is UserService.get_or_create, which runs from
                # get_current_user on the first authenticated request. Clerk
                # webhooks are asynchronous, so the browser routinely calls the API
                # before this event lands — the lazy path wins the race and this
                # insert conflicts. That is the NORMAL case for a fast client, not
                # an error, and it is indistinguishable here from a redelivered
                # event.
                #
                # Telling those two apart is unnecessary: the send lock already
                # guarantees exactly-once. EmailService claims the dedupe key
                # welcome:<user_id> before calling the provider, so a redelivery
                # is refused at the database. Gating on "did I insert the row"
                # added a second guard that only ever produced false negatives.
                created_user_id = (
                    await db.execute(
                        select(User.id).where(User.external_id == data["id"])
                    )
                ).scalar_one_or_none()
                logger.info(
                    "Clerk webhook: user %s already existed (created by the "
                    "request path); welcome email still attempted, the send lock "
                    "prevents a duplicate",
                    data["id"],
                )
                # The lazy path stores an EMPTY display_name because it only ever
                # receives an id and an email. This event carries the real name,
                # and the welcome email greets the recipient by it, so fill it in.
                if created_user_id is not None and display_name:
                    await db.execute(
                        update(User)
                        .where(User.id == created_user_id)
                        .where((User.display_name.is_(None))
                               | (User.display_name == ""))
                        .values(display_name=display_name)
                    )
            else:
                logger.info("Clerk webhook: created user %s", data["id"])

        elif event_type == "user.deleted":
            await db.execute(
                update(User)
                .where(User.external_id == data["id"])
                .values(deleted_at=datetime.now(timezone.utc))
            )
            logger.info("Clerk webhook: soft deleted user %s", data["id"])

        await db.commit()

    # After the commit, for the user this event refers to — whether this insert
    # created the row or the request path got there first. Exactly-once is the
    # send lock's job, not ours.
    if created_user_id is not None and _is_mailable(email):
        await _send_welcome_email(created_user_id, email, display_name or None)

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
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = settings.stripe_webhook_secret

    # (1) A signature is present → ALWAYS verify, in EVERY environment. A present-
    # but-invalid signature is a hard 400 — it NEVER falls through to unverified
    # parsing. If we can't verify (no secret), fail closed rather than trust it.
    if sig_header:
        if not webhook_secret:
            logger.warning("Stripe webhook: signature present but no secret configured")
            raise HTTPException(status_code=400, detail="Webhook secret not configured")
        try:
            from backend.services.billing import stripe_gateway

            # construct_event verifies the signature (raises on tamper/mismatch).
            # We then parse the raw body ourselves: construct_event returns a
            # stripe.Event (StripeObject) whose attribute access is key-based —
            # `event.get(...)` raises — so we hand the handler plain nested dicts
            # from json.loads. (Body is unchanged, so re-parsing is safe.)
            stripe_gateway.construct_event(body, sig_header, webhook_secret)
            return json.loads(body)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Stripe webhook signature invalid: %s", e)
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    # (2) No signature header. Production ALWAYS rejects (an unverified endpoint is
    # an entitlement-forgery hole). The unverified parse is a local-dev escape hatch
    # requiring TWO independent opt-ins, each defaulting to "verify":
    #   - settings.stripe_allow_unverified_webhooks is True (default False), AND
    #   - environment != "production".
    if settings.stripe_allow_unverified_webhooks and settings.environment != "production":
        logger.warning(
            "⚠️⚠️ STRIPE WEBHOOK ACCEPTED WITHOUT SIGNATURE VERIFICATION ⚠️⚠️ "
            "(stripe_allow_unverified_webhooks=True, environment=%s). "
            "This trusts an unsigned payload — DEV ONLY, NEVER production.",
            settings.environment,
        )
        return json.loads(body)

    raise HTTPException(status_code=400, detail="Missing Stripe signature")


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
        "Stripe webhook: %s (duplicate=%s, handled=%s, retry=%s)",
        result.event_type, result.duplicate, result.handled, result.retry,
    )
    # An entitlement-bearing event whose customer didn't resolve was NOT recorded
    # (see StripeWebhookService.process) — return 5xx so Stripe redelivers it,
    # rather than 200'ing a real payment into a silent drop.
    if result.retry:
        raise HTTPException(
            status_code=500,
            detail="Webhook could not resolve customer; will retry",
        )
    return {"ok": True}
