"""Tests for the HMAC-signed unsubscribe token.

A broken unsubscribe link is a CAN-SPAM violation, so the round trip and the
tamper rejection are both load-bearing, not cosmetic.

`_sign` here deliberately mirrors backend/routers/auth.py's OAuth state signing —
HMAC-SHA256 of the raw payload under settings.secret_key — rather than importing
the production signer. That is what makes TestPurposeSeparation a real test: it
mints a token the OAuth code would accept and shows this module rejects it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from backend.config import settings
from backend.services.email import unsubscribe as unsub


def _sign(body: str) -> str:
    """HMAC over the raw body — the OAuth-state construction, with NO purpose
    tag. Signing an unsubscribe token this way must not produce a valid one."""
    return hmac.new(
        settings.secret_key.encode(), body.encode(), hashlib.sha256
    ).hexdigest()


def _oauth_style_state(
    user_id: str = "user_2abc", *, strip_padding: bool = False
) -> str:
    """An OAuth state value built exactly the way backend/routers/auth.py builds
    one: b64url of a JSON payload, a dot, then HMAC-SHA256 of that payload.

    auth.py leaves the base64 padding on, and read_token rejects "=" on shape
    alone. `strip_padding` removes that easy exit so the rejection has to come
    from the purpose tag, which is the fix under test.
    """
    payload = {
        "user_id": user_id,
        "nh": hashlib.sha256(b"nonce").hexdigest(),
        "exp": int(time.time()) + 600,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    sig = _sign(body)
    return f"{body.rstrip('=')}.{sig}" if strip_padding else f"{body}.{sig}"


class TestRoundTrip:
    def test_token_reads_back_as_the_same_address(self):
        token = unsub.make_token("sam@example.com")
        assert unsub.read_token(token) == "sam@example.com"

    def test_address_is_normalized_on_the_way_in(self):
        """Suppression is keyed on the lowercased address, so the token must
        carry the lowercased form or the click suppresses nothing."""
        token = unsub.make_token("  Sam@Example.COM ")
        assert unsub.read_token(token) == "sam@example.com"

    def test_addresses_with_plus_and_dots_survive(self):
        addr = "sam.the.kicker+fantasy@example.co.uk"
        assert unsub.read_token(unsub.make_token(addr)) == addr

    def test_token_carries_no_base64_padding(self):
        """Padding survives fewer link rewriters than an unpadded token does."""
        body, _, _ = unsub.make_token("a@b.com").partition(".")
        assert "=" not in body

    def test_token_is_stable_across_calls(self):
        """Same address, same token — the header link and the body link match."""
        assert unsub.make_token("sam@example.com") == unsub.make_token(
            "sam@example.com"
        )


class TestTamperRejection:
    def test_altered_signature_is_rejected(self):
        token = unsub.make_token("sam@example.com")
        body, _, sig = token.partition(".")
        flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
        assert unsub.read_token(f"{body}.{flipped}") is None

    def test_swapping_in_another_address_is_rejected(self):
        """The attack this defends: edit the payload to unsubscribe someone else."""
        token = unsub.make_token("sam@example.com")
        _, _, sig = token.partition(".")
        other = (
            base64.urlsafe_b64encode(b"victim@example.com").decode().rstrip("=")
        )
        assert unsub.read_token(f"{other}.{sig}") is None

    def test_token_signed_with_a_different_secret_is_rejected(self):
        body = base64.urlsafe_b64encode(b"sam@example.com").decode().rstrip("=")
        forged = hmac.new(
            b"not-the-secret", body.encode(), hashlib.sha256
        ).hexdigest()
        assert unsub.read_token(f"{body}.{forged}") is None

    def test_missing_token_is_rejected(self):
        assert unsub.read_token(None) is None
        assert unsub.read_token("") is None

    def test_token_without_a_separator_is_rejected(self):
        assert unsub.read_token("nodotinhere") is None

    def test_token_with_an_empty_half_is_rejected(self):
        assert unsub.read_token(".abc") is None
        assert unsub.read_token("abc.") is None

    def test_correctly_signed_but_undecodable_body_returns_none(self):
        """Unreachable in practice, but the endpoint is unauthenticated: it must
        render a page rather than raise, whatever arrives.

        The body is legal base64url CHARACTERS at an illegal LENGTH (5, which is
        one more than a multiple of four), so it passes the shape check and then
        fails inside b64decode."""
        body = "AAAAA"
        token = f"{body}.{_sign(unsub._PURPOSE + body)}"

        assert unsub.read_token(token) is None


class TestNonAsciiSignature:
    """FIX 1. hmac.compare_digest with str arguments requires both sides to be
    ASCII and raises TypeError otherwise. The signature half arrives straight
    from an unauthenticated query string, so `?token=abc.%C3%A9` used to be an
    unauthenticated 500. read_token must return None for it instead."""

    def test_non_ascii_signature_returns_none_instead_of_raising(self):
        assert unsub.read_token("abc.é") is None

    def test_non_ascii_body_returns_none_instead_of_raising(self):
        assert unsub.read_token("é.abc") is None

    def test_the_exact_reported_token_returns_none(self):
        """GET /api/email/unsubscribe?token=abc.%C3%A9 — the trigger from the
        review, after URL decoding."""
        assert unsub.read_token("abc.é") is None

    @pytest.mark.parametrize(
        "token",
        [
            "abc.é",
            "日本語.日本語",
            "\x00.\x00",
            "abc.\U0001f600",
            "abc." + "é" * 64,
            "..",
            "a..b",
            "abc.ABCDEF0123456789" * 4,   # uppercase hex: not the sig charset
            "abc.zzzz",                    # non-hex characters
            "abc." + "0" * 63,             # one short of a SHA-256 hex digest
            "abc." + "0" * 65,             # one over
            "a b.c d",
            "abc.def!",
            "x" * 5000 + ".y",             # over the length bound
        ],
    )
    def test_read_token_never_raises_for_any_shape(self, token):
        assert unsub.read_token(token) is None


class TestPurposeSeparation:
    """FIX 2. Unsubscribe tokens and Yahoo OAuth state values are built the same
    way — b64url payload, dot, hex HMAC-SHA256 — under the same secret_key. Left
    interchangeable, any recipient of a Rook email could take the token out of
    their own unsubscribe link, send it as the OAuth `state`, pass signature
    verification, and reach an unguarded JSON decode: an unauthenticated 500."""

    def test_a_token_signed_without_the_purpose_tag_is_rejected(self):
        """The OAuth construction applied to an unsubscribe payload."""
        body = (
            base64.urlsafe_b64encode(b"unsub:v1:sam@example.com")
            .decode()
            .rstrip("=")
        )

        assert unsub.read_token(f"{body}.{_sign(body)}") is None

    @pytest.mark.parametrize("strip_padding", [False, True])
    def test_an_oauth_shaped_state_does_not_verify_as_an_unsubscribe_token(
        self, strip_padding
    ):
        state = _oauth_style_state(strip_padding=strip_padding)

        assert unsub.read_token(state) is None

    def test_a_real_unsubscribe_signature_is_not_the_oauth_signature(self):
        """The signature over an unsubscribe token cannot equal HMAC(body), which
        is what backend/routers/auth.py computes. This is the half of the fix
        that protects the OAuth callback."""
        body, _, sig = unsub.make_token("sam@example.com").partition(".")

        assert sig != _sign(body)

    def test_a_correctly_signed_body_without_the_tag_inside_is_rejected(self):
        """The other half: a payload signed with the tag but not CARRYING it is
        not an address this module minted."""
        body = (
            base64.urlsafe_b64encode(b"sam@example.com").decode().rstrip("=")
        )
        token = f"{body}.{_sign(unsub._PURPOSE + body)}"

        assert unsub.read_token(token) is None

    def test_a_body_carrying_a_different_purpose_tag_is_rejected(self):
        body = (
            base64.urlsafe_b64encode(b"unsub:v2:sam@example.com")
            .decode()
            .rstrip("=")
        )
        token = f"{body}.{_sign(unsub._PURPOSE + body)}"

        assert unsub.read_token(token) is None

    def test_the_tag_never_leaks_into_the_returned_address(self):
        assert unsub.read_token(unsub.make_token("sam@example.com")) == (
            "sam@example.com"
        )

    def test_a_token_whose_payload_is_only_the_tag_is_rejected(self):
        """No address after the tag means nothing to suppress."""
        body = (
            base64.urlsafe_b64encode(unsub._PURPOSE.encode())
            .decode()
            .rstrip("=")
        )
        token = f"{body}.{_sign(unsub._PURPOSE + body)}"

        assert unsub.read_token(token) is None


class TestUnsubscribeUrl:
    def test_url_is_built_from_app_url_and_the_api_path(self, monkeypatch):
        monkeypatch.setattr(settings, "app_url", "https://rookff.com")
        url = unsub.unsubscribe_url("sam@example.com")

        assert url.startswith("https://rookff.com/api/email/unsubscribe?token=")
        assert unsub.read_token(url.split("token=", 1)[1]) == "sam@example.com"

    def test_trailing_slash_on_app_url_does_not_double(self, monkeypatch):
        monkeypatch.setattr(settings, "app_url", "https://rookff.com/")
        assert "//api/email" not in unsub.unsubscribe_url("sam@example.com")

    def test_url_for_an_existing_token_reuses_that_token(self, monkeypatch):
        monkeypatch.setattr(settings, "app_url", "https://rookff.com")
        token = unsub.make_token("sam@example.com")
        assert unsub.unsubscribe_url_for_token(token).endswith(token)
