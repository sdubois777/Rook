"""
Billing router — Stripe Checkout + Customer Portal session creation.

Auth-required and rate-limited (§0.E). Card data never touches this server: both
endpoints just create a Stripe-hosted session and return its URL for redirect
(§0.A). The customer is bound to the authenticated user's row server-side (§0.C);
the client supplies only a tier/pack NAME — never a price id or amount (§0.B). The
success-return URL grants nothing — entitlement flips solely in the webhook.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, model_validator

from backend.config import settings
from backend.core.dependencies import get_current_user, get_db
from backend.core.exceptions import ValidationError
from backend.middleware.rate_limit import rate_limit_auth
from backend.models.user import User
from backend.repositories.user_repo import UserRepository
from backend.services.billing import catalog, stripe_gateway

router = APIRouter(
    prefix="/billing",
    tags=["billing"],
    dependencies=[Depends(rate_limit_auth)],  # §0.E — blunt abuse of session creation
)


class CheckoutRequest(BaseModel):
    """Exactly one of tier / pack. Both are server-mapped to a price id — the
    client can NEVER supply a price id or amount."""
    tier: Optional[Literal["intro", "standard", "pro"]] = None
    pack: Optional[Literal["small", "medium", "large"]] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.tier) == bool(self.pack):
            raise ValueError("Provide exactly one of 'tier' or 'pack'")
        return self


class CheckoutResponse(BaseModel):
    url: str


class PortalResponse(BaseModel):
    url: str


def _require_stripe() -> None:
    if not settings.stripe_enabled:
        raise HTTPException(status_code=503, detail="Billing is not configured")


async def _ensure_customer(user: User, db) -> str:
    """Return the user's Stripe customer id, creating + persisting one if absent.

    The customer is bound to the authenticated user (email + our ids in metadata);
    it is never accepted from the request.
    """
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer_id = stripe_gateway.create_customer(
        email=user.email,
        external_id=user.external_id,
        user_id=str(user.id),
        idempotency_key=f"cust_{user.id}",
    )
    repo = UserRepository(db)
    await repo.set_stripe_customer_id(user.id, customer_id)
    await repo.commit()
    user.stripe_customer_id = customer_id
    return customer_id


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a Checkout Session for a subscription tier or a credit pack."""
    _require_stripe()
    customer_id = await _ensure_customer(user, db)

    if body.tier:
        price_id = catalog.tier_to_price(body.tier)
        if not price_id:
            raise HTTPException(
                status_code=400,
                detail=f"No price configured for tier '{body.tier}'",
            )
        mode = "subscription"
        metadata = {"tier": body.tier, "user_id": str(user.id)}
        target = f"tier_{body.tier}"
    else:
        price_id = catalog.pack_to_price(body.pack)
        credits = catalog.pack_to_credits(body.pack)
        if not price_id or credits is None:
            raise HTTPException(
                status_code=400,
                detail=f"No price configured for pack '{body.pack}'",
            )
        mode = "payment"
        metadata = {
            "pack": body.pack,
            "credits": str(credits),
            "user_id": str(user.id),
        }
        target = f"pack_{body.pack}"

    url = stripe_gateway.create_checkout_session(
        customer_id=customer_id,
        mode=mode,
        price_id=price_id,
        # These pages grant NOTHING (§0.B) — the webhook is the only grantor.
        success_url=f"{settings.app_url}/account?billing=success",
        cancel_url=f"{settings.app_url}/pricing?billing=cancel",
        metadata=metadata,
        idempotency_key=f"co_{user.id}_{target}",
    )
    return CheckoutResponse(url=url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    user: User = Depends(get_current_user),
):
    """Create a Customer Portal session (manage/cancel/update card)."""
    _require_stripe()
    if not user.stripe_customer_id:
        raise ValidationError("No billing account for this user")

    url = stripe_gateway.create_portal_session(
        customer_id=user.stripe_customer_id,
        return_url=f"{settings.app_url}/account",
    )
    return PortalResponse(url=url)
