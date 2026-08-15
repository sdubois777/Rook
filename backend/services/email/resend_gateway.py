"""
Thin wrapper over the Resend HTTP API.

Every outbound message funnels through here so (a) the API key and the from/
reply-to addresses are read from `settings` in exactly one place, (b) callers
never build the HTTP request themselves, and (c) tests can monkeypatch one
function instead of intercepting the network. Same shape and reasoning as
backend/services/billing/stripe_gateway.py.

httpx, not the `resend` SDK. The repo already depends on httpx and uses it in 16
places; adding a package to send one POST would mean regenerating uv.lock for no
capability we do not already have.

THE IDEMPOTENCY KEY, AND WHY THE SEND LOCK ALONE IS NOT ENOUGH.

Every request carries `Idempotency-Key: <the send's dedupe_key>`. Resend honours
idempotency keys: a repeated request with the same key returns the result of the
original request instead of sending a second message.

That header closes a hole the database-side send lock cannot. The lock
(email_sends.dedupe_key, claimed before the call — see
backend/repositories/email_repo.py) refuses a second attempt while the row says
'sent'. But a request that TIMES OUT leaves us with no answer at all: Resend may
have accepted the message and lost the response on the way back. EmailService
records that as 'failed', a failed row is re-claimable, and the next attempt
would deliver a second copy of a message the recipient already has. With the
key, that second attempt returns the first result and sends nothing.

THE RESIDUAL, STATED HONESTLY. Provider idempotency keys are not retained
forever — Resend's window is 24 hours. A retry made LONGER than 24 hours after
an ambiguous failure is outside the window, so Resend treats it as a new request
and the recipient gets a duplicate. In practice the only thing that triggers a
retry is a webhook redelivery (nothing in this app schedules one — see
RETRY IS CALLER-DRIVEN in email_service.py), and both Clerk and Stripe redeliver
within minutes to hours, far inside the window. An operator re-running a send by
hand days later is the case that could still duplicate.

TWO KINDS OF FAILURE, and the caller logs which one happened:

  ResendRejected   Resend answered with a non-2xx status. The message was NOT
                   accepted, and nothing was queued at the provider.
  ResendAmbiguous  There is no readable answer — a timeout, a transport error,
                   or a 2xx whose body could not be parsed. Whether a message
                   was accepted cannot be told from here.

Both are re-claimable by EmailService, which is safe because of the idempotency
key above. The distinction exists so the log tells an operator which case a row
is in, because "Resend said no" and "we never heard back" call for different
responses.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

# Resend normally answers in well under a second. Ten seconds is long enough to
# ride out a slow response and short enough that a hung provider cannot pin a
# webhook handler open.
_TIMEOUT_SECONDS = 10.0

# The request header Resend reads for idempotency. Its value is the send's
# dedupe_key, so the provider's dedupe window and our own send lock key off the
# same string.
IDEMPOTENCY_HEADER = "Idempotency-Key"

# How long Resend remembers an idempotency key. Documented here because it is the
# exact size of the remaining duplicate-send window (see the module docstring);
# nothing reads it.
IDEMPOTENCY_WINDOW_HOURS = 24


class ResendError(RuntimeError):
    """The send did not complete. Subclasses say whether the outcome is known."""


class ResendRejected(ResendError):
    """Resend answered with an error status: the message was NOT accepted."""


class ResendAmbiguous(ResendError):
    """No readable answer from Resend, so acceptance cannot be determined here.

    A timeout, a transport error, or a success whose body could not be parsed.
    The message may already be on its way; a later attempt carrying the same
    Idempotency-Key returns the original result rather than sending again.
    """


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    dedupe_key: str,
    headers: Optional[dict[str, str]] = None,
) -> str:
    """Send one message; return the provider message id.

    `dedupe_key` is the caller's send-lock key (e.g. "welcome:<user_id>"), sent
    as the Idempotency-Key header so a repeat of this exact request returns the
    original result instead of mailing the recipient twice. It is passed in
    rather than looked up here, because the gateway never touches the database.

    Raises ResendRejected when Resend answers with a non-2xx status, and
    ResendAmbiguous when there is no readable answer. Nothing here retries: the
    dedupe row is already claimed, and a retry loop inside a webhook handler is
    how a webhook times out.
    """
    api_key = settings.resend_api_key
    if not api_key:
        # Configuration, not a provider outcome — no request was made.
        raise ResendError("RESEND_API_KEY not configured")

    payload: dict = {
        "from": settings.email_from,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to
    if headers:
        payload["headers"] = headers

    request_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if dedupe_key:
        request_headers[IDEMPOTENCY_HEADER] = dedupe_key
    else:
        # An empty header value would be rejected by the provider, so the send
        # still goes out — without duplicate protection. Callers always have a
        # key, so this is a bug in the caller and says so.
        logger.warning(
            "Sending to Resend with no idempotency key — a retry of this "
            "message could deliver a second copy"
        )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                RESEND_API_URL,
                json=payload,
                headers=request_headers,
            )
    except httpx.HTTPError as exc:
        # Covers ReadTimeout as well as connection failures. httpx gives no
        # answer in either case, so acceptance is unknown — ambiguous, never
        # "rejected".
        raise ResendAmbiguous(f"No answer from Resend: {exc}") from exc

    if not 200 <= resp.status_code < 300:
        # Status only, never the body. An error body can echo back request
        # headers, and the request header here carries the API key.
        raise ResendRejected(f"Resend refused the send: status={resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        # A 2xx we cannot read. The status says accepted, but nothing in the
        # response can be trusted to confirm it, so it is reported as ambiguous
        # rather than as a rejection.
        raise ResendAmbiguous("Resend returned a non-JSON success body") from exc

    message_id = data.get("id") if isinstance(data, dict) else None
    if not message_id:
        # Accepted but unidentifiable. Not worth failing the send over — the
        # message went out — but the id is how a delivery is traced later.
        logger.warning("Resend accepted a send with no message id in the body")
        return ""
    return str(message_id)
