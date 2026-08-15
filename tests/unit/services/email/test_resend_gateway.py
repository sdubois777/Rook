"""Tests for the Resend gateway.

No network: httpx.AsyncClient is replaced with a fake that records the request it
was given.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from backend.config import settings
from backend.services.email import resend_gateway
from backend.services.email.resend_gateway import (
    IDEMPOTENCY_HEADER,
    ResendAmbiguous,
    ResendError,
    ResendRejected,
    send_email,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, body_is_json=True):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"id": "msg_abc123"}
        self._body_is_json = body_is_json

    def json(self):
        if not self._body_is_json:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Async context manager standing in for httpx.AsyncClient."""

    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls: list[dict] = []
        self.timeout = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def resend_configured(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_from", "Rook <rookadmin@rookff.com>")
    monkeypatch.setattr(settings, "email_reply_to", "rookadmin@rookff.com")


def _install(monkeypatch, client: FakeClient) -> FakeClient:
    def _factory(*args, **kwargs):
        client.timeout = kwargs.get("timeout")
        return client

    monkeypatch.setattr(resend_gateway.httpx, "AsyncClient", _factory)
    return client


async def _send(**over):
    kwargs = {
        "to": "sam@example.com",
        "subject": "Welcome to Rook",
        "html": "<p>hello</p>",
        "text": "hello",
        "dedupe_key": "welcome:user-1",
    }
    kwargs.update(over)
    return await send_email(**kwargs)


class TestSendEmail:
    async def test_posts_to_the_resend_endpoint_with_a_bearer_key(
        self, monkeypatch, resend_configured
    ):
        client = _install(monkeypatch, FakeClient())

        await _send()

        call = client.calls[0]
        assert call["url"] == "https://api.resend.com/emails"
        assert call["headers"]["Authorization"] == "Bearer re_test_key"

    async def test_body_carries_from_to_subject_html_text_and_reply_to(
        self, monkeypatch, resend_configured
    ):
        client = _install(monkeypatch, FakeClient())

        await _send()

        body = client.calls[0]["json"]
        assert body["from"] == "Rook <rookadmin@rookff.com>"
        assert body["to"] == ["sam@example.com"]
        assert body["subject"] == "Welcome to Rook"
        assert body["html"] == "<p>hello</p>"
        assert body["text"] == "hello"
        assert body["reply_to"] == "rookadmin@rookff.com"

    async def test_custom_headers_are_forwarded(
        self, monkeypatch, resend_configured
    ):
        """This is how List-Unsubscribe reaches the recipient."""
        client = _install(monkeypatch, FakeClient())

        await _send(headers={"List-Unsubscribe": "<https://x/u>"})

        assert client.calls[0]["json"]["headers"] == {
            "List-Unsubscribe": "<https://x/u>"
        }

    async def test_omits_the_headers_field_when_none_are_given(
        self, monkeypatch, resend_configured
    ):
        client = _install(monkeypatch, FakeClient())

        await _send()

        assert "headers" not in client.calls[0]["json"]

    async def test_returns_the_provider_message_id(
        self, monkeypatch, resend_configured
    ):
        _install(monkeypatch, FakeClient(FakeResponse(payload={"id": "msg_9"})))

        assert await _send() == "msg_9"

    async def test_uses_a_ten_second_timeout(
        self, monkeypatch, resend_configured
    ):
        """A hung provider must not pin a webhook handler open."""
        client = _install(monkeypatch, FakeClient())

        await _send()

        assert client.timeout == 10.0

    async def test_missing_api_key_raises_without_making_a_request(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "resend_api_key", None)
        client = _install(monkeypatch, FakeClient())

        with pytest.raises(ResendError):
            await _send()

        assert client.calls == []

    async def test_non_2xx_raises_and_reports_the_status_only(
        self, monkeypatch, resend_configured
    ):
        """The error body can echo request headers, and those carry the API key."""
        _install(
            monkeypatch,
            FakeClient(FakeResponse(status_code=422, payload={"message": "bad"})),
        )

        with pytest.raises(ResendError) as exc:
            await _send()

        assert "422" in str(exc.value)
        assert "re_test_key" not in str(exc.value)

    async def test_server_error_raises(self, monkeypatch, resend_configured):
        _install(monkeypatch, FakeClient(FakeResponse(status_code=500)))

        with pytest.raises(ResendError):
            await _send()

    async def test_transport_failure_raises_resend_error(
        self, monkeypatch, resend_configured
    ):
        _install(
            monkeypatch,
            FakeClient(error=httpx.ConnectError("no route to host")),
        )

        with pytest.raises(ResendError):
            await _send()

    async def test_non_json_success_body_raises(
        self, monkeypatch, resend_configured
    ):
        _install(monkeypatch, FakeClient(FakeResponse(body_is_json=False)))

        with pytest.raises(ResendError):
            await _send()

    async def test_success_without_an_id_returns_empty_string(
        self, monkeypatch, resend_configured
    ):
        """Accepted but untraceable. Not worth failing a delivered message over."""
        _install(monkeypatch, FakeClient(FakeResponse(payload={})))

        assert await _send() == ""


class TestIdempotencyKey:
    """The duplicate-send hole a retryable 'failed' row would otherwise open.

    A ReadTimeout on the 10-second budget is indistinguishable from a rejection
    at the transport level, EmailService records both as failed, and a failed row
    is re-claimable. Without a provider-side idempotency key, a message Resend
    accepted but whose response was lost is sent a second time on the next
    webhook redelivery.
    """

    async def test_the_request_carries_an_idempotency_key(
        self, monkeypatch, resend_configured
    ):
        client = _install(monkeypatch, FakeClient())

        await _send()

        assert IDEMPOTENCY_HEADER in client.calls[0]["headers"]

    async def test_the_idempotency_key_is_the_dedupe_key(
        self, monkeypatch, resend_configured
    ):
        """Same string as the email_sends send lock, so the provider's dedupe
        window and ours key off one value."""
        client = _install(monkeypatch, FakeClient())

        await _send(dedupe_key="welcome:11111111-2222-3333-4444-555555555555")

        assert (
            client.calls[0]["headers"][IDEMPOTENCY_HEADER]
            == "welcome:11111111-2222-3333-4444-555555555555"
        )

    async def test_the_same_key_rides_on_a_retry_of_the_same_message(
        self, monkeypatch, resend_configured
    ):
        """The retry is what the key protects against: two calls for one message
        must present the same key, or Resend treats the second as a new send."""
        client = _install(monkeypatch, FakeClient())

        await _send(dedupe_key="welcome:user-1")
        await _send(dedupe_key="welcome:user-1")

        keys = [call["headers"][IDEMPOTENCY_HEADER] for call in client.calls]
        assert keys == ["welcome:user-1", "welcome:user-1"]

    async def test_different_messages_carry_different_keys(
        self, monkeypatch, resend_configured
    ):
        client = _install(monkeypatch, FakeClient())

        await _send(dedupe_key="referral_reward:user-1:2")
        await _send(dedupe_key="referral_reward:user-1:3")

        keys = [call["headers"][IDEMPOTENCY_HEADER] for call in client.calls]
        assert keys == ["referral_reward:user-1:2", "referral_reward:user-1:3"]

    async def test_an_empty_key_omits_the_header_and_warns(
        self, monkeypatch, resend_configured, caplog
    ):
        """An empty header value would be rejected outright, so the message still
        goes — without duplicate protection, which the log has to say."""
        client = _install(monkeypatch, FakeClient())

        with caplog.at_level(
            logging.WARNING, logger=resend_gateway.logger.name
        ):
            await _send(dedupe_key="")

        assert IDEMPOTENCY_HEADER not in client.calls[0]["headers"]
        assert "idempotency key" in caplog.text.lower()


class TestFailureClassification:
    """A definite rejection and an ambiguous failure are different exceptions.

    Both leave a re-claimable row. The difference is what an operator reading the
    log has to conclude: a rejection means nothing was queued at the provider, a
    timeout means the message may already be in the recipient's inbox.
    """

    async def test_an_error_status_is_a_definite_rejection(
        self, monkeypatch, resend_configured
    ):
        _install(monkeypatch, FakeClient(FakeResponse(status_code=422)))

        with pytest.raises(ResendRejected):
            await _send()

    async def test_a_read_timeout_is_ambiguous_not_a_rejection(
        self, monkeypatch, resend_configured
    ):
        """THE CASE THAT MATTERS. Resend may have accepted the message and lost
        the response; calling that a rejection is a claim we cannot support."""
        _install(
            monkeypatch, FakeClient(error=httpx.ReadTimeout("timed out"))
        )

        with pytest.raises(ResendAmbiguous):
            await _send()

    async def test_a_transport_error_is_ambiguous(
        self, monkeypatch, resend_configured
    ):
        _install(
            monkeypatch, FakeClient(error=httpx.ConnectError("no route to host"))
        )

        with pytest.raises(ResendAmbiguous):
            await _send()

    async def test_an_unreadable_success_body_is_ambiguous(
        self, monkeypatch, resend_configured
    ):
        """A 2xx we cannot parse is not a rejection — the status says accepted."""
        _install(monkeypatch, FakeClient(FakeResponse(body_is_json=False)))

        with pytest.raises(ResendAmbiguous):
            await _send()

    @pytest.mark.parametrize(
        "error_class", [ResendRejected, ResendAmbiguous]
    )
    def test_both_are_resend_errors(self, error_class):
        """Callers that only care that the send failed keep working."""
        assert issubclass(error_class, ResendError)

    def test_the_two_classes_are_distinguishable(self):
        assert not issubclass(ResendRejected, ResendAmbiguous)
        assert not issubclass(ResendAmbiguous, ResendRejected)

    async def test_a_missing_api_key_is_neither_case(
        self, monkeypatch
    ):
        """No request was made, so it is not a provider outcome at all."""
        monkeypatch.setattr(settings, "resend_api_key", None)
        _install(monkeypatch, FakeClient())

        with pytest.raises(ResendError) as exc:
            await _send()

        assert not isinstance(exc.value, (ResendRejected, ResendAmbiguous))
