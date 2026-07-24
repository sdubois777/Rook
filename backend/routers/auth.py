"""
Auth router — Yahoo OAuth 2.0 multi-user flow.

GET  /auth/yahoo/connect    → redirect user to Yahoo authorization page (auth required)
GET  /auth/yahoo/callback   → exchange code for tokens, store encrypted per user
DELETE /auth/yahoo/disconnect → remove Yahoo credentials for current user
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid as uuid_mod
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.config import settings
from backend.core.dependencies import get_current_user, get_db
from backend.core.exceptions import ValidationError
from backend.core.exceptions import AppError
from backend.integrations.yahoo_api import (
    exchange_code_for_tokens,
    get_authorization_url,
    get_league_settings,
    get_user_leagues,
    refresh_access_token_for_user,
)
from backend.models.user import User
from backend.repositories.credential_repo import CredentialRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# OAuth state binding (F1/F2/F6)
#
# Signing state alone does not stop the attack: an attacker can present their
# OWN validly-signed state alongside a victim's authorization code. So the
# callback is bound to the BROWSER that initiated the flow via a random,
# HttpOnly nonce cookie set at initiation. The signed state carries only the
# SHA-256 of that nonce; the callback recomputes the hash from the cookie the
# browser actually presents and rejects if it does not match. An attacker's
# state is bound to the attacker's cookie, which the victim's browser never
# carries — so the code exchange never lands on the wrong account.
# ---------------------------------------------------------------------------

_STATE_TTL_SECONDS = 600           # initiation → callback must complete inside 10m
_NONCE_COOKIE = "yahoo_oauth_nonce"
# Router is mounted under /api (main.py), so the live path is /api/auth/yahoo/*.
# Scope the cookie to that subtree — it rides both connect-url and callback and
# is sent nowhere else.
_NONCE_COOKIE_PATH = "/api/auth/yahoo"


def _sign_state(user_id: str, nonce: str, *, ttl: int = _STATE_TTL_SECONDS) -> str:
    """Build an HMAC-signed, browser-bound OAuth state value.

    Payload carries the user id, the SHA-256 of the initiation nonce (never the
    nonce itself), and an absolute expiry. Format: ``<b64url(payload)>.<hexsig>``.
    """
    payload = {
        "user_id": user_id,
        "nh": hashlib.sha256(nonce.encode()).hexdigest(),
        "exp": int(time.time()) + ttl,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    sig = hmac.new(
        settings.secret_key.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return f"{body}.{sig}"


def _verify_state_signature(state: str) -> dict | None:
    """Return the decoded payload iff the HMAC is valid, else ``None``.

    Only signature/format failures return ``None`` (→ ``invalid_signature``). A
    valid signature over an undecodable body would be our OWN bug — it is allowed
    to raise rather than be silently reclassified as a forged state.
    """
    body, _, sig = state.partition(".")
    if not body or not sig:
        return None
    expected = hmac.new(
        settings.secret_key.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    # Signature verified → the body is one WE issued; decode without a catch-all.
    raw = base64.urlsafe_b64decode(body.encode())
    return json.loads(raw.decode())


def _state_expired(payload: dict) -> bool:
    exp = payload.get("exp")
    return not isinstance(exp, int) or int(time.time()) > exp


def _callback_redirect(url: str) -> RedirectResponse:
    """Redirect that also clears the single-use binding cookie (all exit paths)."""
    resp = RedirectResponse(url=url, status_code=302)
    resp.delete_cookie(_NONCE_COOKIE, path=_NONCE_COOKIE_PATH)
    return resp


def _reject(code: str) -> RedirectResponse:
    """Distinct error code to the UI; the reason is logged server-side only."""
    logger.warning("Yahoo OAuth callback rejected: %s", code)
    return _callback_redirect(f"/league-setup?platform=yahoo&error={code}")


async def _get_valid_yahoo_access_token(
    repo: CredentialRepository, user_id: uuid_mod.UUID
) -> str:
    """Return a usable Yahoo access token for the user.

    Raises a 400 AppError if Yahoo is not connected. If the stored
    token is expired, refreshes it and persists the new tokens before
    returning.
    """
    tokens = await repo.get_yahoo_tokens(user_id)
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
            user_id, access_token, refresh_token, new_expiry,
        )

    return access_token


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
    access_token = await _get_valid_yahoo_access_token(repo, user.id)
    leagues = await get_user_leagues(access_token)
    return {"leagues": leagues}


@router.get("/yahoo/league-settings", summary="Fetch settings for a Yahoo league")
async def get_yahoo_league_settings(
    league_key: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Fetch full Yahoo league settings (scoring, draft type, etc.)
    for a specific league_key. Used by frontend confirm screen.
    Auto-refreshes expired tokens.
    """
    repo = CredentialRepository(db)
    access_token = await _get_valid_yahoo_access_token(repo, user.id)
    league_settings = await get_league_settings(access_token, league_key)
    return {"settings": league_settings}


@router.get("/yahoo/connect-url", summary="Get Yahoo OAuth URL (authenticated)")
async def yahoo_connect_url(user=Depends(get_current_user)):
    """
    Returns the Yahoo OAuth authorization URL as JSON, and sets a single-use,
    HttpOnly nonce cookie that binds the eventual callback to THIS browser.

    Frontend fetches this with a Bearer token (credentials included so the
    Set-Cookie stores), then navigates the browser to the returned URL. The
    state carries only the user id, a hash of the nonce, and an expiry, all
    HMAC-signed — signing plus the browser-bound cookie is what closes the
    cross-account binding hole (F1/F2/F6).
    """
    if not settings.yahoo_client_id:
        raise AppError("YAHOO_CLIENT_ID not configured")

    nonce = secrets.token_urlsafe(32)
    state = _sign_state(str(user.id), nonce)

    url = get_authorization_url(state=state)
    logger.info("Yahoo OAuth URL generated for user %s", user.id)

    resp = JSONResponse({"url": url})
    resp.set_cookie(
        key=_NONCE_COOKIE,
        value=nonce,
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        secure=True,
        # Lax (NOT Strict): Strict would suppress the cookie on the cross-site-
        # initiated top-level navigation back from Yahoo and break the flow.
        samesite="lax",
        path=_NONCE_COOKIE_PATH,
    )
    return resp


@router.get("/yahoo/callback", summary="Yahoo OAuth callback")
async def yahoo_callback(
    code: str,
    state: str,
    request: Request,
    db=Depends(get_db),
):
    """
    Yahoo OAuth callback — no auth dependency.

    Identity is NOT trusted from the request alone. Before the code is exchanged
    the callback verifies, in order: (1) the state's HMAC signature, (2) that it
    has not expired, (3) that the browser presents the binding nonce cookie set
    at initiation, and (4) that the cookie's hash matches the one signed into the
    state. Each failure redirects with a distinct error code and is logged
    server-side; the single-use cookie is cleared on every exit path. This is
    what defeats an attacker replaying their own validly-signed state with a
    victim's authorization code — the victim's browser carries no matching cookie.
    """
    nonce_cookie = request.cookies.get(_NONCE_COOKIE)

    # 1. HMAC signature — a forged or tampered state fails here.
    payload = _verify_state_signature(state)
    if payload is None:
        return _reject("invalid_signature")

    # 2. Freshness — an old (captured/leaked) state is dead after the TTL.
    if _state_expired(payload):
        return _reject("expired_state")

    # 3. Browser binding present — the initiating browser was issued the cookie;
    #    a state presented without it (the core attack) is refused.
    if not nonce_cookie:
        return _reject("missing_binding")

    # 4. Browser binding matches — the cookie must hash to the value signed into
    #    the state. Constant-time compare; an attacker's state + victim's browser
    #    mismatches here.
    actual_nh = hashlib.sha256(nonce_cookie.encode()).hexdigest()
    if not hmac.compare_digest(str(payload.get("nh", "")), actual_nh):
        return _reject("binding_mismatch")

    # Binding verified. A malformed user id in an otherwise-valid (signed) state
    # can only originate from us, so it is a defensive fallback, not an attack.
    try:
        user_id = uuid_mod.UUID(payload["user_id"])
    except (ValueError, KeyError, TypeError):
        return _reject("invalid_state")

    try:
        tokens = await exchange_code_for_tokens(code)
    except Exception as exc:
        logger.error("Yahoo OAuth token exchange failed: %s", exc)
        raise ValidationError(f"Token exchange failed: {exc}")

    # Compute expiry
    expires_in = int(tokens.get("expires_in", 3600))
    from datetime import timedelta
    expires_at = datetime.now(timezone.utc).replace(
        microsecond=0
    ) + timedelta(seconds=expires_in)

    # Verify user exists before writing credentials — Clerk webhook
    # may not have created the row yet (race condition).
    user_row = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()

    if not user_row:
        logger.warning(
            "Yahoo OAuth callback: user %s not found (Clerk webhook pending)",
            user_id,
        )
        return _callback_redirect(
            "/league-setup?platform=yahoo&error=account_not_ready&retry=true",
        )

    repo = CredentialRepository(db)
    try:
        await repo.upsert_yahoo(
            user_id=user_id,
            access_token=tokens.get("access_token", ""),
            refresh_token=tokens.get("refresh_token", ""),
            expires_at=expires_at,
        )
    except IntegrityError:
        logger.warning(
            "Yahoo OAuth callback: IntegrityError for user %s (FK violation)",
            user_id,
        )
        await db.rollback()
        return _callback_redirect(
            "/league-setup?platform=yahoo&error=account_not_ready&retry=true",
        )

    logger.info("Yahoo OAuth complete for user %s", user_id)
    return _callback_redirect("/league-setup?platform=yahoo")


@router.delete("/yahoo/disconnect", summary="Remove Yahoo credentials")
async def yahoo_disconnect(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Remove Yahoo credentials for current user."""
    repo = CredentialRepository(db)
    await repo.disconnect(user.id, "yahoo")
    return {"status": "disconnected", "platform": "yahoo"}


@router.delete("/espn/disconnect", summary="Remove ESPN credentials")
async def espn_disconnect(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Remove the user's stored ESPN cookies (espn_s2 + SWID). Mirrors the Yahoo
    disconnect and delegates to the SAME canonical CredentialRepository.disconnect —
    one revocation mechanism, parameterized by platform. Any ESPN leagues the user
    synced REMAIN (least-destructive, same as Yahoo) but become unsyncable until they
    reconnect (the next sync has no cookies). Idempotent: a no-op if none are stored."""
    repo = CredentialRepository(db)
    await repo.disconnect(user.id, "espn")
    return {"status": "disconnected", "platform": "espn"}
