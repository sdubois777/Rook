"""The application must be able to report that it failed to send something.

A welcome email was silently not sent to every account for months. Three separate
things had to be true at once for that to stay invisible, and each gets a test here:

  1. Nothing configured the root logger, so every INFO line the application wrote
     was discarded in production. backend/services/email/email_service.py documents
     its INFO-level skip lines as "the only record" of a message that was not sent;
     in production those lines did not exist.
  2. backend/routers/webhooks.py discarded the status the email service returned,
     so a refused send and a delivered one were indistinguishable at the call site.
  3. Nothing reported whether outbound email was even switched on, so answering
     "can the running process send mail" needed hosting-provider access.
"""
from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# 1. Logging actually reaches somewhere
# ---------------------------------------------------------------------------

def test_importing_the_app_attaches_a_handler_and_enables_application_info():
    """Uvicorn configures only its own loggers and defines no root logger, so
    without this the application's INFO output goes nowhere."""
    import backend.main  # noqa: F401  (import configures logging)

    root = logging.getLogger()
    assert root.handlers, "no handler on the root logger — all output is discarded"

    backend_logger = logging.getLogger("backend.services.email.email_service")
    assert backend_logger.isEnabledFor(logging.INFO), (
        "application INFO is disabled, so the skip lines that email_service "
        "documents as 'the only record' of an unsent message are never emitted"
    )


def test_configuring_logging_twice_does_not_stack_handlers():
    """Duplicate handlers print every line twice, which is its own kind of
    unreadable."""
    from backend.main import _configure_logging

    root = logging.getLogger()
    before = len([h for h in root.handlers if getattr(h, "_rook_stdout", False)])
    _configure_logging()
    _configure_logging()
    after = len([h for h in root.handlers if getattr(h, "_rook_stdout", False)])

    assert before == after == 1


# ---------------------------------------------------------------------------
# 2. The webhook reports a send it did not make
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_webhook_warns_when_the_welcome_email_was_not_sent(caplog):
    """Most refusals write NO row to email_sends, so a discarded status left no
    record anywhere that a customer had not been welcomed."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.routers.webhooks import _send_welcome_email
    from backend.services.email.email_service import SEND_SKIPPED

    emails = MagicMock()
    emails.send_welcome = AsyncMock(return_value=SEND_SKIPPED)
    referrals = MagicMock()
    referrals.get_or_create_code = AsyncMock(return_value="CODE")
    referrals.welcome_code_for = MagicMock(return_value="WELCOME")

    session = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx),
        patch("backend.services.email.email_service.EmailService.from_session",
              return_value=emails),
        patch("backend.services.referral_service.ReferralService.from_session",
              return_value=referrals),
        caplog.at_level(logging.WARNING, logger="backend.routers.webhooks"),
    ):
        await _send_welcome_email("user-1", "someone@example.com", "Someone")

    # getMessage() applies the lazy %-args the logger was called with.
    assert any(SEND_SKIPPED in r.getMessage() for r in caplog.records), (
        "a refused welcome email produced no warning — the status is being "
        "discarded again, and nothing else records it"
    )


@pytest.mark.asyncio
async def test_a_successful_send_does_not_warn(caplog):
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.routers.webhooks import _send_welcome_email
    from backend.services.email.email_service import SEND_SENT

    emails = MagicMock()
    emails.send_welcome = AsyncMock(return_value=SEND_SENT)
    referrals = MagicMock()
    referrals.get_or_create_code = AsyncMock(return_value="CODE")
    referrals.welcome_code_for = MagicMock(return_value="WELCOME")

    session = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx),
        patch("backend.services.email.email_service.EmailService.from_session",
              return_value=emails),
        patch("backend.services.referral_service.ReferralService.from_session",
              return_value=referrals),
        caplog.at_level(logging.WARNING, logger="backend.routers.webhooks"),
    ):
        await _send_welcome_email("user-1", "someone@example.com", "Someone")

    assert not caplog.records


# ---------------------------------------------------------------------------
# 3. Email capability is answerable from outside the process
# ---------------------------------------------------------------------------

def test_health_reports_whether_outbound_email_is_enabled():
    """Whether the running process can send mail took three wrong guesses to
    establish. It is a boolean derived from config, so it can just be published.

    promotional_email_enabled is the one that governs the welcome email — it is
    PROMOTIONAL, so email_enabled being true is not sufficient on its own.
    """
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        body = client.get("/health").json()

    assert "email_enabled" in body
    assert "promotional_email_enabled" in body
    assert "welcome_sweep_enabled" in body
    assert isinstance(body["email_enabled"], bool)
    assert isinstance(body["promotional_email_enabled"], bool)


def test_health_does_not_leak_the_key_or_the_address():
    """These are capability flags. The secret and the postal address must not be
    in a public, unauthenticated response."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        raw = client.get("/health").text.lower()

    for forbidden in ("resend", "api_key", "apikey", "postal", "secret"):
        assert forbidden not in raw, f"/health exposes {forbidden!r}"
