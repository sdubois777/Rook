"""
Email router — the unsubscribe endpoint.

  GET  /email/unsubscribe?token=...   the link in the footer of a message
  POST /email/unsubscribe?token=...   List-Unsubscribe-Post one-click

Both are UNAUTHENTICATED and both are idempotent. Unauthenticated because the
recipient may not have the app open, may not remember they have an account, and
may be reading in a client that cannot present a session — an unsubscribe link
that first asks you to log in does not count as an unsubscribe. The signed token
is the authorization: it proves the address came from a message we sent, and it
cannot be edited into a different address without SECRET_KEY.

Mail clients acting on List-Unsubscribe-Post send an empty-ish form POST to the
same URL, so the token stays in the query string for both verbs and no body is
parsed.

WHAT THE PAGES DO NOT SAY. Neither page echoes the address or reveals whether it
was already suppressed or whether an account exists. The token holder already
knows the address; anyone else must not learn one from a leaked link.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from backend.core.dependencies import get_db
from backend.models.email import SUPPRESS_UNSUBSCRIBE
from backend.repositories.email_repo import EmailRepository
from backend.services.email.unsubscribe import read_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/email", tags=["email"])

# Nothing here should sit in a proxy or a browser cache — the response reflects a
# state change, and a cached "you are unsubscribed" would outlive a resubscribe.
_NO_STORE = {"Cache-Control": "no-store"}


def _page(*, heading: str, body: str) -> str:
    """One small self-contained page. No stylesheet, no scripts, no assets — this
    is rendered inside mail-client browsers and webviews with unpredictable
    network access."""
    return (
        "<!doctype html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>{heading} — Rook</title></head>"
        "<body style=\"margin:0;background:#f4f5f7;font-family:-apple-system,"
        "BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;\">"
        "<div style=\"max-width:520px;margin:64px auto;padding:32px;"
        "background:#ffffff;border:1px solid #e2e4ea;border-radius:10px;\">"
        "<div style=\"font-size:20px;font-weight:700;color:#2a3d8f;"
        "margin-bottom:16px;\">Rook</div>"
        f"<h1 style=\"font-size:22px;line-height:30px;color:#1c1f2e;"
        f"margin:0 0 12px 0;\">{heading}</h1>"
        f"<p style=\"font-size:16px;line-height:24px;color:#5a6072;margin:0;\">"
        f"{body}</p>"
        "</div></body></html>"
    )


_OK_PAGE = _page(
    heading="You are unsubscribed",
    body=(
        "You will not receive any more marketing email from Rook. Account and "
        "billing notices still go out, because they are part of the service."
    ),
)

_BAD_TOKEN_PAGE = _page(
    heading="This link did not work",
    body=(
        "The unsubscribe link was incomplete or has been altered in transit. "
        "Reply to any Rook email and we will remove you by hand."
    ),
)

_ERROR_PAGE = _page(
    heading="Something went wrong",
    body=(
        "We could not record your request just now. Please try the link again "
        "in a few minutes, or reply to any Rook email and we will remove you "
        "by hand."
    ),
)


async def _unsubscribe(token: str | None, db) -> HTMLResponse:
    """Suppress the address in the token. Idempotent, and safe to call twice."""
    # read_token is written never to raise, and its own tests hold it to that.
    # This second guard is here because the call sits on an UNAUTHENTICATED path
    # and used to sit outside every try: when hmac.compare_digest was handed a
    # non-ASCII signature from the query string it raised TypeError, which
    # escaped the handler as a 500 that anyone could trigger. Treating any
    # exception as an invalid token means a future change inside read_token
    # cannot reintroduce that, at the cost of nothing.
    try:
        email = read_token(token)
    except Exception:
        logger.exception("Unsubscribe token could not be read")
        email = None

    if email is None:
        logger.info("Unsubscribe attempted with a missing or invalid token")
        return HTMLResponse(_BAD_TOKEN_PAGE, status_code=200, headers=_NO_STORE)

    try:
        repo = EmailRepository(db)
        newly = await repo.suppress(email, SUPPRESS_UNSUBSCRIBE)
        await repo.commit()
    except Exception:
        # A 500 here is honest: a mail client doing one-click needs to know the
        # request did not take, and the user gets a page rather than a traceback.
        logger.exception("Unsubscribe failed to write the suppression row")
        return HTMLResponse(_ERROR_PAGE, status_code=500, headers=_NO_STORE)

    logger.info("Unsubscribe processed (newly_suppressed=%s)", newly)
    return HTMLResponse(_OK_PAGE, status_code=200, headers=_NO_STORE)


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_get(
    token: str | None = Query(default=None),
    db=Depends(get_db),
) -> HTMLResponse:
    """The link in the footer of a promotional message."""
    return await _unsubscribe(token, db)


@router.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_post(
    token: str | None = Query(default=None),
    db=Depends(get_db),
) -> HTMLResponse:
    """RFC 8058 one-click, called by the mail client rather than the person.

    Same handler, same idempotency. The body a client posts
    ("List-Unsubscribe=One-Click") is deliberately not parsed — the token in the
    query string is the whole request.
    """
    return await _unsubscribe(token, db)
