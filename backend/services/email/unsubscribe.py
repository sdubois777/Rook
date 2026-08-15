"""
Unsubscribe tokens — HMAC-signed, self-contained, no expiry.

Format is ``<b64url(purpose + email)>.<hexsig>``: the payload is base64url of the
purpose tag followed by the address, the signature is HMAC-SHA256 under
settings.secret_key, and the two are joined with a dot.

WHY THE PURPOSE TAG. backend/routers/auth.py builds OAuth state values with the
same construction — b64url payload, dot, hex HMAC-SHA256 — under the SAME
secret_key. Without a tag the two token families are byte-for-byte
interchangeable, so any recipient of a Rook email could take the token out of
their own unsubscribe link and present it as the OAuth `state` on the callback.
It would pass signature verification there and then fail in the JSON decode,
which is an unauthenticated 500. The tag appears in two places and each one is
load-bearing:

  * It is inside the SIGNED material (``_sign(_PURPOSE + body)``), so the
    signature over an unsubscribe token can never equal HMAC(body) — the value
    the OAuth verifier computes. That is what stops an unsubscribe token from
    verifying as OAuth state.
  * It is inside the ENCODED body, and read_token rejects a body that does not
    start with it. That is what stops a token minted for some other purpose from
    reading back as an address here.

Fixing only this side is deliberate: backend/routers/auth.py is outside this
module's ownership. The residual risk is that some FUTURE token family repeats
the untagged construction.

WHY SIGNED RATHER THAN A STORED RANDOM TOKEN. The link has to work from an email
that may be years old and from a mail client that never authenticates. A signed
token needs no table, cannot be enumerated to discover addresses, and cannot be
edited into someone else's address without the secret.

WHY NO EXPIRY. An unsubscribe link that stops working is a CAN-SPAM violation,
and old messages sit in inboxes indefinitely. The token is deliberately eternal.
The consequence is that rotating SECRET_KEY invalidates every outstanding link at
once — noted here because that is the only way this breaks.

The base64 padding is stripped on the way out and restored on the way in. "=" is
legal in a query string but survives fewer trips through link rewriters and
tracking redirectors than an unpadded token does.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Optional
from urllib.parse import quote

from backend.config import settings

# All API routers are mounted under /api in backend/main.py, so this is the real
# path. The un-prefixed /email/unsubscribe would be swallowed by the SPA
# catch-all and answered with index.html and a 200 — a link that looks like it
# worked and silently did nothing, which is the worst possible failure for an
# unsubscribe.
UNSUBSCRIBE_PATH = "/api/email/unsubscribe"

# The purpose tag. Versioned so a future change to what the payload carries can
# be introduced as "unsub:v2:" without old links reading back as the new shape.
# No unsubscribe token has ever been minted in any environment (the migration
# that creates the email tables has not been applied anywhere), so there is no
# untagged token in the wild to keep accepting.
_PURPOSE = "unsub:v1:"

# What a well-formed token looks like. Checked BEFORE hmac.compare_digest,
# because compare_digest with str arguments requires both to be ASCII-only and
# raises TypeError otherwise — and the signature half arrives straight from an
# unauthenticated query string, where "abc.%C3%A9" is one request away. The body
# is unpadded base64url; the signature is exactly 64 lowercase hex characters.
_BODY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SIG_RE = re.compile(r"^[0-9a-f]{64}$")

# An address cannot be longer than 254 characters, so a legitimate body cannot
# approach this. The bound exists so a multi-megabyte query string is rejected
# on sight rather than base64-decoded first.
_MAX_TOKEN_CHARS = 1024


def _normalize(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _sign(body: str) -> str:
    return hmac.new(
        settings.secret_key.encode(), body.encode(), hashlib.sha256
    ).hexdigest()


def make_token(email: str) -> str:
    """Signed token carrying the (normalized) address."""
    body = (
        base64.urlsafe_b64encode((_PURPOSE + _normalize(email)).encode())
        .decode()
        .rstrip("=")
    )
    return f"{body}.{_sign(_PURPOSE + body)}"


def read_token(token: Optional[str]) -> Optional[str]:
    """The address inside a valid token, or None if it is missing or forged.

    NEVER RAISES, for any input at all. This is called from an unauthenticated
    endpoint that has to render a page rather than a traceback no matter what
    arrives in the query string, so every failure mode collapses to None:
    a missing token, the wrong shape, a non-ASCII or over-long signature, a bad
    HMAC, an undecodable body, or a body signed for a different purpose. The
    outer try is the backstop for anything not enumerated above.
    """
    try:
        if not token or len(token) > _MAX_TOKEN_CHARS:
            return None

        body, dot, sig = token.partition(".")
        if not dot:
            return None
        # Shape first. compare_digest below would raise TypeError on a non-ASCII
        # signature, which escaped as a 500 before this check existed.
        if not _BODY_RE.match(body) or not _SIG_RE.match(sig):
            return None

        if not hmac.compare_digest(_sign(_PURPOSE + body), sig):
            return None

        padded = body + "=" * (-len(body) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        if not decoded.startswith(_PURPOSE):
            return None

        return _normalize(decoded[len(_PURPOSE):]) or None
    except Exception:  # noqa: BLE001 — see the docstring: this must not raise.
        return None


def unsubscribe_url(email: str) -> str:
    """Full one-click unsubscribe URL for an address."""
    base = settings.app_url.rstrip("/")
    return f"{base}{UNSUBSCRIBE_PATH}?token={quote(make_token(email), safe='')}"


def unsubscribe_url_for_token(token: str) -> str:
    """Same URL built from an already-minted token.

    Used when the message body and the List-Unsubscribe header must carry the
    identical token, so a support conversation about "the link in my email" is
    about one string and not two.
    """
    base = settings.app_url.rstrip("/")
    return f"{base}{UNSUBSCRIBE_PATH}?token={quote(token, safe='')}"
