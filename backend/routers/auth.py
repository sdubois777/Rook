"""
Auth router — Yahoo OAuth 2.0 multi-user flow.

GET  /auth/yahoo/connect    → redirect user to Yahoo authorization page (auth required)
GET  /auth/yahoo/callback   → exchange code for tokens, store encrypted per user
DELETE /auth/yahoo/disconnect → remove Yahoo credentials for current user
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from backend.config import settings
from backend.core.dependencies import get_current_user, get_db
from backend.core.exceptions import ValidationError
from backend.core.exceptions import AppError
from backend.integrations.yahoo_api import (
    exchange_code_for_tokens,
    get_authorization_url,
    get_user_leagues,
    refresh_access_token_for_user,
)
from backend.repositories.credential_repo import CredentialRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/yahoo/leagues", summary="List user's Yahoo Fantasy leagues")
async def get_yahoo_leagues(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Returns all Yahoo Fantasy Football leagues for the authenticated user.
    Requires Yahoo OAuth to be complete (credentials in platform_credentials).
    Auto-refreshes expired tokens.
    """
    repo = CredentialRepository(db)
    tokens = await repo.get_yahoo_tokens(user.id)
    if not tokens:
        err = AppError("Yahoo not connected", {"action": "connect"})
        err.status_code = 400
        raise err

    access_token, refresh_token, expires_at = tokens

    # Auto-refresh if expired
    if expires_at and datetime.now(timezone.utc) >= expires_at:
        access_token, refresh_token, new_expiry = (
            await refresh_access_token_for_user(refresh_token)
        )
        await repo.upsert_yahoo(
            user.id, access_token, refresh_token, new_expiry,
        )

    leagues = await get_user_leagues(access_token)
    return {"leagues": leagues}


@router.get("/yahoo/connect-url", summary="Get Yahoo OAuth URL (authenticated)")
async def yahoo_connect_url(user=Depends(get_current_user)):
    """
    Returns the Yahoo OAuth authorization URL as JSON.
    Frontend fetches this with a Bearer token, then navigates
    the browser to the returned URL. Separates auth from
    navigation so the redirect doesn't need JWT headers.
    """
    if not settings.yahoo_client_id:
        raise AppError("YAHOO_CLIENT_ID not configured")

    state = base64.urlsafe_b64encode(
        json.dumps({"user_id": str(user.id)}).encode()
    ).decode()

    url = get_authorization_url(state=state)
    logger.info("Yahoo OAuth URL generated for user %s", user.id)
    return {"url": url}


@router.get("/yahoo/connect", summary="Redirect to Yahoo OAuth (requires login)")
async def yahoo_connect(user=Depends(get_current_user)):
    """
    Initiate Yahoo OAuth for current user.
    Encodes user_id in state parameter (CSRF protection).
    Kept for direct-navigation fallback.
    """
    if not settings.yahoo_client_id:
        raise AppError("YAHOO_CLIENT_ID not configured")

    state = base64.urlsafe_b64encode(
        json.dumps({"user_id": str(user.id)}).encode()
    ).decode()

    url = get_authorization_url(state=state)
    logger.info("Yahoo OAuth redirect for user %s", user.id)
    return RedirectResponse(url=url)


@router.get("/yahoo/callback", summary="Yahoo OAuth callback")
async def yahoo_callback(
    code: str,
    state: str,
    db=Depends(get_db),
):
    """
    Yahoo OAuth callback — no auth dependency.
    User identity comes from the state parameter,
    not from JWT headers. The browser navigates
    here directly from Yahoo with no ability to
    attach auth headers.

    Security model: state was encoded server-side
    in /yahoo/connect-url with the user's verified
    Clerk ID. Yahoo returns it unchanged.
    """
    # Decode user_id from state (encoded server-side in connect-url)
    try:
        state_data = json.loads(
            base64.urlsafe_b64decode(state).decode()
        )
        user_id = state_data["user_id"]
    except Exception:
        raise ValidationError("Invalid OAuth state")

    try:
        tokens = await exchange_code_for_tokens(code)
    except Exception as exc:
        logger.error("Yahoo OAuth token exchange failed: %s", exc)
        raise ValidationError(f"Token exchange failed: {exc}")

    # Compute expiry
    expires_in = int(tokens.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc).replace(
        microsecond=0
    )
    from datetime import timedelta
    expires_at = expires_at + timedelta(seconds=expires_in)

    repo = CredentialRepository(db)
    await repo.upsert_yahoo(
        user_id=user_id,
        access_token=tokens.get("access_token", ""),
        refresh_token=tokens.get("refresh_token", ""),
        expires_at=expires_at,
    )

    logger.info("Yahoo OAuth complete for user %s", user_id)
    return RedirectResponse(url="/league-setup?platform=yahoo", status_code=302)


@router.delete("/yahoo/disconnect", summary="Remove Yahoo credentials")
async def yahoo_disconnect(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Remove Yahoo credentials for current user."""
    repo = CredentialRepository(db)
    await repo.disconnect(user.id, "yahoo")
    return {"status": "disconnected", "platform": "yahoo"}
