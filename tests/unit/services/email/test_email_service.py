"""Tests for EmailService — the ordering rules, the send lock, and the promise
that send() never raises.

The repository and the provider are both fakes. A single shared `events` list
records what happened in what order, because several of the rules here are about
ORDER (claim before provider, commit before provider) rather than about outcomes.
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import backend
from backend.config import settings
from backend.models.email import (
    CATEGORY_PROMOTIONAL,
    CATEGORY_TRANSACTIONAL,
    SEND_FAILED,
    SEND_PENDING,
    SEND_SENT,
    SEND_SKIPPED,
    SEND_SUPPRESSED,
    UNDELIVERABLE_EMAIL_SUFFIXES,
)
from backend.models.user import REFERRAL_PROGRAM
from backend.services.email import email_service as svc
from backend.services.email import resend_gateway
from backend.repositories.email_repo import EmailRepository
from backend.services.email.email_service import (
    EmailService,
    _hourly_cap,
    _is_deliverable,
)


# ── fakes ───────────────────────────────────────────────────────────────

class ClaimedRow:
    """A claimed email_sends row that records WHEN its id is read.

    EmailService must read the id BEFORE committing: backend/database.py sets
    expire_on_commit=False, but under SQLAlchemy's default the commit expires the
    instance and a later attribute read triggers a lazy refresh, which raises
    MissingGreenlet under asyncio — after the message has already been delivered.
    """

    def __init__(self, events):
        self._events = events
        self._id = uuid.uuid4()

    @property
    def id(self):
        self._events.append(("read_row_id", None))
        return self._id


class FakeEmailRepo:
    """Duck-types EmailRepository. `events` is shared with the fake gateway."""

    def __init__(self, events, *, suppressed=(), claimed=(), fail_on=None):
        self.events = events
        self.suppressed = {a.lower() for a in suppressed}
        self.claimed: dict[str, object] = {
            key: SimpleNamespace(id=uuid.uuid4()) for key in claimed
        }
        self.claims: list[dict] = []
        self.statuses: list[dict] = []
        self.commits = 0
        # Name of a method that should blow up, to exercise the never-raise rule.
        self.fail_on = fail_on

    def _maybe_fail(self, name):
        if self.fail_on == name:
            raise RuntimeError(f"{name} exploded")

    async def is_suppressed(self, email):
        self._maybe_fail("is_suppressed")
        self.events.append(("is_suppressed", email))
        return email.lower() in self.suppressed

    async def claim_send(self, *, dedupe_key, to_email, template, category, user_id=None):
        self._maybe_fail("claim_send")
        self.events.append(("claim_send", dedupe_key))
        self.claims.append(
            {
                "dedupe_key": dedupe_key,
                "to_email": to_email,
                "template": template,
                "category": category,
                "user_id": user_id,
            }
        )
        if dedupe_key in self.claimed:
            return None
        row = ClaimedRow(self.events)
        self.claimed[dedupe_key] = row
        return row

    async def mark_status(self, send_id, status, *, provider_message_id=None, error=None):
        self._maybe_fail("mark_status")
        self.events.append(("mark_status", status))
        self.statuses.append(
            {
                "send_id": send_id,
                "status": status,
                "provider_message_id": provider_message_id,
                "error": error,
            }
        )

    async def commit(self):
        self.events.append(("commit", self.commits))
        self.commits += 1


class FakeGateway:
    def __init__(self, events, *, message_id="msg_1", error=None):
        self.events = events
        self.message_id = message_id
        self.error = error
        self.calls: list[dict] = []

    async def send_email(self, *, to, subject, html, text, dedupe_key, headers=None):
        self.events.append(("provider", to))
        self.calls.append(
            {
                "to": to,
                "subject": subject,
                "html": html,
                "text": text,
                "dedupe_key": dedupe_key,
                "headers": headers,
            }
        )
        if self.error:
            raise self.error
        return self.message_id


@pytest.fixture(autouse=True)
def email_configured(monkeypatch):
    """Fully configured email, empty rate-cap window, for every test here."""
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_from", "Rook <rookadmin@rookff.com>")
    monkeypatch.setattr(settings, "email_reply_to", "rookadmin@rookff.com")
    monkeypatch.setattr(settings, "email_postal_address", "Rook LLC, Austin TX")
    monkeypatch.setattr(settings, "email_max_per_hour", 200)
    monkeypatch.setattr(settings, "app_url", "https://rookff.com")
    _hourly_cap.reset()
    yield
    _hourly_cap.reset()


@pytest.fixture
def events():
    return []


@pytest.fixture
def gateway(monkeypatch, events):
    fake = FakeGateway(events)
    monkeypatch.setattr(svc.resend_gateway, "send_email", fake.send_email)
    return fake


def _service(events, **repo_kwargs):
    repo = FakeEmailRepo(events, **repo_kwargs)
    return EmailService(repo), repo


async def _send(service, **over):
    kwargs = {
        "to_email": "sam@example.com",
        "subject": "Welcome",
        "html": "<p>hi</p>",
        "text": "hi",
        "template": "welcome",
        "category": CATEGORY_PROMOTIONAL,
        "dedupe_key": "welcome:user-1",
    }
    kwargs.update(over)
    return await service.send(**kwargs)


# ---------------------------------------------------------------------------
# Deliverability helper
# ---------------------------------------------------------------------------

class TestIsDeliverable:
    @pytest.mark.parametrize(
        "address",
        [
            "",
            "no-at-sign",
            "user_2abc@placeholder.local",
            "dev-user-001@dev.local",
        ],
    )
    def test_rejects_undeliverable_addresses(self, address):
        assert _is_deliverable(address) is False

    def test_a_bare_hostname_domain_is_not_deliverable(self):
        """"a@b" passed the old "@" test. A domain with no dot is not a routable
        public domain, and mailing it earns a hard bounce."""
        assert _is_deliverable("a@b") is False

    def test_an_empty_local_part_is_not_deliverable(self):
        """"@x.com" passed the old "@" test too."""
        assert _is_deliverable("@x.com") is False

    @pytest.mark.parametrize(
        "address",
        ["sam@.example.com", "sam@example.com.", "@", "@@", "sam@"],
    )
    def test_other_malformed_shapes_are_not_deliverable(self, address):
        assert _is_deliverable(address) is False

    @pytest.mark.parametrize(
        "address",
        [
            "sam@example.com",
            "sam.the.kicker+fantasy@example.co.uk",
            "a@b.co",
        ],
    )
    def test_accepts_a_real_address(self, address):
        assert _is_deliverable(address) is True


class TestUndeliverableSuffixConstant:
    """FIX 6. The list of synthesized placeholder domains used to be hardcoded in
    email_service.py, separately from the two modules that produce them. These
    tests read those two modules and fail if either synthesizes a domain the
    shared constant does not cover."""

    # The producers: backend/core/dependencies.py builds an address for the
    # dev-auth fallback and for a Clerk user with no email; backend/routers/
    # draft.py does the same two things on the websocket path.
    _BACKEND_DIR = Path(backend.__file__).parent
    _PRODUCERS = (
        _BACKEND_DIR / "core" / "dependencies.py",
        _BACKEND_DIR / "routers" / "draft.py",
    )

    # Matches an f-string that is exactly an interpolation followed by a domain,
    # e.g. f"{user_id}@dev.local" — which is how every synthesized address in
    # those files is written.
    _SYNTH_RE = re.compile(r'f"\{[^{}]+\}(@[A-Za-z0-9.\-]+)"')

    @classmethod
    def _synthesized(cls, path: Path) -> set[str]:
        return set(cls._SYNTH_RE.findall(path.read_text(encoding="utf-8")))

    @pytest.mark.parametrize("path", _PRODUCERS, ids=lambda p: p.name)
    def test_the_producer_still_synthesizes_addresses_this_test_can_see(
        self, path
    ):
        """Guards the regex. If a producer is rewritten so this stops matching,
        the coverage test below would pass vacuously — so fail here instead."""
        assert self._synthesized(path), (
            f"{path.name} no longer contains a recognisable synthesized "
            f"address; update _SYNTH_RE or this file is no longer a producer"
        )

    @pytest.mark.parametrize("path", _PRODUCERS, ids=lambda p: p.name)
    def test_every_synthesized_domain_is_covered_by_the_constant(self, path):
        uncovered = self._synthesized(path) - set(UNDELIVERABLE_EMAIL_SUFFIXES)

        assert not uncovered, (
            f"{path.name} synthesizes {sorted(uncovered)}, which "
            f"UNDELIVERABLE_EMAIL_SUFFIXES does not cover — those addresses "
            f"would be mailed and would hard-bounce"
        )

    @pytest.mark.parametrize("path", _PRODUCERS, ids=lambda p: p.name)
    def test_every_synthesized_address_is_rejected_by_is_deliverable(self, path):
        """End to end: what the producer actually writes, through the real check."""
        for suffix in self._synthesized(path):
            assert _is_deliverable(f"user_2abc{suffix}") is False

    def test_email_service_does_not_keep_its_own_copy_of_the_list(self):
        """The point of the fix: one tuple, imported, not two literals."""
        source = Path(svc.__file__).read_text(encoding="utf-8")

        for suffix in UNDELIVERABLE_EMAIL_SUFFIXES:
            assert f'"{suffix}"' not in source
            assert f"'{suffix}'" not in source


class TestConstruction:
    def test_from_session_wraps_the_session_in_a_repository(self):
        service = EmailService.from_session(AsyncMock())
        assert isinstance(service._repo, EmailRepository)


# ---------------------------------------------------------------------------
# Order of checks
# ---------------------------------------------------------------------------

class TestCheckOrder:
    async def test_email_disabled_skips_before_touching_the_repository(
        self, monkeypatch, events, gateway
    ):
        monkeypatch.setattr(settings, "resend_api_key", None)
        service, repo = _service(events)

        assert await _send(service) == SEND_SKIPPED
        assert events == []
        assert gateway.calls == []

    async def test_promotional_without_postal_address_is_skipped(
        self, monkeypatch, events, gateway
    ):
        """CAN-SPAM: refuse rather than send a commercial message with no
        physical address in it."""
        monkeypatch.setattr(settings, "email_postal_address", "  ")
        service, repo = _service(events)

        assert await _send(service) == SEND_SKIPPED
        assert repo.claims == []

    async def test_transactional_without_postal_address_still_sends(
        self, monkeypatch, events, gateway
    ):
        monkeypatch.setattr(settings, "email_postal_address", "")
        service, _ = _service(events)

        status = await _send(service, category=CATEGORY_TRANSACTIONAL)

        assert status == SEND_SENT
        assert len(gateway.calls) == 1

    @pytest.mark.parametrize(
        "address",
        [
            "user_2abc@placeholder.local",
            "dev-user-001@dev.local",
            "nope",
            "",
            "a@b",
            "@x.com",
        ],
    )
    async def test_undeliverable_address_is_skipped_before_the_repository(
        self, events, gateway, address
    ):
        service, repo = _service(events)

        assert await _send(service, to_email=address) == SEND_SKIPPED
        assert events == []
        assert gateway.calls == []

    async def test_suppressed_recipient_is_never_claimed_or_sent(
        self, events, gateway
    ):
        service, repo = _service(events, suppressed=["sam@example.com"])

        assert await _send(service) == SEND_SUPPRESSED
        assert repo.claims == []
        assert gateway.calls == []

    async def test_suppression_check_is_case_insensitive(self, events, gateway):
        service, _ = _service(events, suppressed=["sam@example.com"])

        assert await _send(service, to_email="SAM@Example.com") == SEND_SUPPRESSED
        assert gateway.calls == []

    async def test_already_claimed_dedupe_key_never_reaches_the_provider(
        self, events, gateway
    ):
        """The send lock: a redelivered webhook must not mail the person twice."""
        service, _ = _service(events, claimed=["welcome:user-1"])

        assert await _send(service) == SEND_SKIPPED
        assert gateway.calls == []

    async def test_claim_happens_before_the_provider_call(self, events, gateway):
        service, _ = _service(events)

        await _send(service)

        names = [e[0] for e in events]
        assert names.index("claim_send") < names.index("provider")

    async def test_claim_is_committed_before_the_provider_call(
        self, events, gateway
    ):
        """A crash between the insert and the send must leave the lock behind, or
        the retry sends a second copy."""
        service, _ = _service(events)

        await _send(service)

        names = [e[0] for e in events]
        assert names.index("commit") < names.index("provider")
        assert names.index("claim_send") < names.index("commit")

    async def test_the_row_id_is_read_before_the_commit(self, events, gateway):
        """FIX 7. Reading row.id after the commit works only because
        backend/database.py sets expire_on_commit=False process-wide. Under the
        SQLAlchemy default the expired instance would lazy-refresh and raise
        MissingGreenlet, AFTER the message had already gone out."""
        service, _ = _service(events)

        await _send(service)

        names = [e[0] for e in events]
        assert names.index("read_row_id") < names.index("commit")

    async def test_nothing_between_the_commit_and_the_provider_call(
        self, events, gateway
    ):
        """FIX 5, second half. The dedupe key is burned at the commit. Anything
        that raises after it and before the provider call consumes the only
        chance to send this message without ever attempting it — so the headers
        are built earlier and the commit is immediately followed by the send."""
        service, _ = _service(events)

        await _send(service)

        names = [e[0] for e in events]
        assert names[names.index("commit") + 1] == "provider"

    async def test_a_failure_building_the_headers_does_not_burn_the_key(
        self, events, gateway, monkeypatch
    ):
        """Header construction mints an unsubscribe token. If that raises, the
        claim must not have been taken."""
        service, repo = _service(events)
        monkeypatch.setattr(
            svc.EmailService,
            "_headers",
            staticmethod(
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no secret"))
            ),
        )

        assert await _send(service) == SEND_FAILED
        assert repo.claims == []
        assert gateway.calls == []


# ---------------------------------------------------------------------------
# Rate cap
# ---------------------------------------------------------------------------

class TestHourlyCap:
    async def test_sends_are_skipped_once_the_cap_is_reached(
        self, monkeypatch, events, gateway
    ):
        monkeypatch.setattr(settings, "email_max_per_hour", 2)
        service, _ = _service(events)

        first = await _send(service, dedupe_key="k1")
        second = await _send(service, dedupe_key="k2")
        third = await _send(service, dedupe_key="k3")

        assert [first, second] == [SEND_SENT, SEND_SENT]
        assert third == SEND_SKIPPED
        assert len(gateway.calls) == 2

    async def test_cap_is_checked_before_the_dedupe_claim(
        self, monkeypatch, events, gateway
    ):
        monkeypatch.setattr(settings, "email_max_per_hour", 0)
        service, repo = _service(events)

        assert await _send(service) == SEND_SKIPPED
        assert repo.claims == []

    async def test_expired_window_entries_free_the_cap_again(
        self, monkeypatch, events, gateway
    ):
        """The window slides — an hour later the same process may send again."""
        monkeypatch.setattr(settings, "email_max_per_hour", 1)
        service, _ = _service(events)
        await _send(service, dedupe_key="k1")

        # Age the recorded send past the one-hour window.
        _hourly_cap._sends[0] -= svc._HOUR_SECONDS + 1

        assert await _send(service, dedupe_key="k2") == SEND_SENT

    async def test_a_capped_send_writes_no_email_sends_row(
        self, monkeypatch, events, gateway
    ):
        """The cap is consumed before the claim, so there is no row to find."""
        monkeypatch.setattr(settings, "email_max_per_hour", 0)
        service, repo = _service(events)

        await _send(service)

        assert repo.claims == []
        assert repo.statuses == []

    async def test_the_capped_log_line_names_the_recipient_and_the_template(
        self, monkeypatch, events, gateway, caplog
    ):
        """FIX 4. The log line is the ONLY record of a capped send, so it has to
        say who was affected."""
        monkeypatch.setattr(settings, "email_max_per_hour", 0)
        service, _ = _service(events)

        with caplog.at_level(logging.ERROR, logger=svc.logger.name):
            await _send(service, to_email="sam@example.com", template="welcome")

        message = caplog.text
        assert "sam@example.com" in message
        assert "welcome" in message

    async def test_the_capped_log_line_does_not_send_the_operator_to_email_sends(
        self, monkeypatch, events, gateway, caplog
    ):
        """It used to say "check email_sends", where the row does not exist."""
        monkeypatch.setattr(settings, "email_max_per_hour", 0)
        service, _ = _service(events)

        with caplog.at_level(logging.ERROR, logger=svc.logger.name):
            await _send(service)

        message = caplog.text.lower()
        assert "not recorded in email_sends" in message
        assert "check email_sends" not in message


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

class TestOutcomes:
    async def test_successful_send_records_sent_with_the_message_id(
        self, events, gateway
    ):
        gateway.message_id = "msg_42"
        service, repo = _service(events)

        assert await _send(service) == SEND_SENT
        assert repo.statuses[-1]["status"] == SEND_SENT
        assert repo.statuses[-1]["provider_message_id"] == "msg_42"

    async def test_provider_failure_records_failed_and_does_not_raise(
        self, monkeypatch, events
    ):
        fake = FakeGateway(events, error=RuntimeError("resend is down"))
        monkeypatch.setattr(svc.resend_gateway, "send_email", fake.send_email)
        service, repo = _service(events)

        assert await _send(service) == SEND_FAILED
        assert repo.statuses[-1]["status"] == SEND_FAILED
        assert "resend is down" in repo.statuses[-1]["error"]

    async def test_repository_failure_returns_failed_and_does_not_raise(
        self, events, gateway
    ):
        """Every caller is a webhook. A database hiccup here must not fail it."""
        service, _ = _service(events, fail_on="claim_send")

        assert await _send(service) == SEND_FAILED
        assert gateway.calls == []

    async def test_suppression_lookup_failure_returns_failed(
        self, events, gateway
    ):
        service, _ = _service(events, fail_on="is_suppressed")

        assert await _send(service) == SEND_FAILED
        assert gateway.calls == []

    async def test_bookkeeping_failure_after_a_delivered_message_still_reports_sent(
        self, events, gateway
    ):
        """The message really did go out; losing the row must not say otherwise
        and must not raise."""
        service, _ = _service(events, fail_on="mark_status")

        assert await _send(service) == SEND_SENT
        assert len(gateway.calls) == 1

    async def test_recipient_is_lowercased_for_the_claim_and_the_provider(
        self, events, gateway
    ):
        service, repo = _service(events)

        await _send(service, to_email="  SAM@Example.COM ")

        assert repo.claims[0]["to_email"] == "sam@example.com"
        assert gateway.calls[0]["to"] == "sam@example.com"

    async def test_claim_records_the_template_category_and_user(
        self, events, gateway
    ):
        user_id = uuid.uuid4()
        service, repo = _service(events)

        await _send(service, user_id=user_id, template="welcome")

        claim = repo.claims[0]
        assert claim["template"] == "welcome"
        assert claim["category"] == CATEGORY_PROMOTIONAL
        assert claim["user_id"] == user_id


# ---------------------------------------------------------------------------
# The idempotency key, and how a provider failure is logged
#
# A failed row is re-claimable. That is what stops a provider outage burning a
# dedupe key forever — and it is also what would mail someone twice after a
# ReadTimeout, because a timeout is indistinguishable from a rejection at the
# transport level and Resend may already have accepted the message. The key
# closes that at the provider; the log line tells an operator which case a row
# is in.
# ---------------------------------------------------------------------------

class TestProviderIdempotency:
    async def test_the_dedupe_key_is_handed_to_the_provider(
        self, events, gateway
    ):
        service, _ = _service(events)

        await _send(service, dedupe_key="welcome:user-9")

        assert gateway.calls[0]["dedupe_key"] == "welcome:user-9"

    async def test_the_provider_key_is_the_same_string_as_the_send_lock_key(
        self, events, gateway
    ):
        """One string, two dedupe mechanisms: the email_sends UNIQUE key and
        Resend's Idempotency-Key. They must not drift apart."""
        service, repo = _service(events)

        await _send(service, dedupe_key="referral_reward:user-9:2")

        assert repo.claims[0]["dedupe_key"] == gateway.calls[0]["dedupe_key"]

    async def test_the_wrappers_pass_their_own_keys_through(
        self, events, gateway
    ):
        service, _ = _service(events)
        user = _user()

        await service.send_welcome(user=user, promo_code="P", referral_code="R")
        await service.send_referral_reward(
            user=user, new_total_percent=20, referral_count=2
        )

        assert [call["dedupe_key"] for call in gateway.calls] == [
            f"welcome:{user.id}",
            f"referral_reward:{user.id}:2",
        ]

    def test_the_fake_gateway_accepts_what_the_real_one_does(self):
        """The fake above is what every other test in this file exercises. If the
        real gateway grows or renames a parameter and the fake does not, these
        tests keep passing against a call shape production does not accept.

        Parameter names and defaults only — annotations are not part of what a
        caller has to satisfy."""
        import inspect

        def _shape(fn):
            return [
                (name, param.kind, param.default)
                for name, param in inspect.signature(fn).parameters.items()
            ]

        assert _shape(FakeGateway(events=[]).send_email) == _shape(
            svc.resend_gateway.send_email
        )


class TestProviderFailureLogging:
    @staticmethod
    def _install_failure(monkeypatch, events, error):
        fake = FakeGateway(events, error=error)
        monkeypatch.setattr(svc.resend_gateway, "send_email", fake.send_email)
        return fake

    async def _log_for(self, monkeypatch, events, caplog, error):
        self._install_failure(monkeypatch, events, error)
        service, _ = _service(events)
        with caplog.at_level(logging.ERROR, logger=svc.logger.name):
            status = await _send(service)
        assert status == SEND_FAILED
        return caplog.text

    async def test_a_rejection_is_logged_as_a_message_that_did_not_go_out(
        self, monkeypatch, events, caplog
    ):
        text = await self._log_for(
            monkeypatch,
            events,
            caplog,
            resend_gateway.ResendRejected("status=422"),
        )

        assert "REJECTED" in text
        assert "not accepted" in text

    async def test_a_timeout_is_logged_as_an_unknown_outcome(
        self, monkeypatch, events, caplog
    ):
        """THE CASE THE IDEMPOTENCY KEY EXISTS FOR. Logging this as a rejection
        would tell an operator the message definitely did not go out, which is
        not something we know."""
        text = await self._log_for(
            monkeypatch,
            events,
            caplog,
            resend_gateway.ResendAmbiguous("No answer from Resend: timed out"),
        )

        assert "UNKNOWN" in text
        assert "may already have accepted" in text
        assert "REJECTED" not in text

    async def test_the_two_cases_do_not_produce_the_same_log_line(
        self, monkeypatch, events, caplog
    ):
        rejected = await self._log_for(
            monkeypatch, events, caplog, resend_gateway.ResendRejected("status=422")
        )
        caplog.clear()
        ambiguous = await self._log_for(
            monkeypatch,
            events,
            caplog,
            resend_gateway.ResendAmbiguous("timed out"),
        )

        assert rejected != ambiguous

    async def test_an_error_from_neither_class_says_it_never_reached_the_provider(
        self, monkeypatch, events, caplog
    ):
        """A missing API key, or a bug on our side, raises before any request."""
        text = await self._log_for(
            monkeypatch,
            events,
            caplog,
            resend_gateway.ResendError("RESEND_API_KEY not configured"),
        )

        assert "FAILED BEFORE THE PROVIDER ANSWERED" in text

    @pytest.mark.parametrize(
        "error",
        [
            resend_gateway.ResendRejected("status=422"),
            resend_gateway.ResendAmbiguous("timed out"),
        ],
        ids=["rejected", "ambiguous"],
    )
    async def test_both_cases_record_failed_and_stay_re_claimable(
        self, monkeypatch, events, error
    ):
        """Re-claimable is deliberate for both. It is safe for the ambiguous case
        only because the retry carries the same idempotency key."""
        self._install_failure(monkeypatch, events, error)
        service, repo = _service(events)

        assert await _send(service) == SEND_FAILED
        assert repo.statuses[-1]["status"] == SEND_FAILED


# ---------------------------------------------------------------------------
# One-click unsubscribe headers
# ---------------------------------------------------------------------------

class TestUnsubscribeHeaders:
    async def test_promotional_send_carries_both_list_unsubscribe_headers(
        self, events, gateway
    ):
        """Gmail requires one-click unsubscribe on bulk mail."""
        service, _ = _service(events)

        await _send(service)

        headers = gateway.calls[0]["headers"]
        assert headers["List-Unsubscribe"].startswith("<https://rookff.com/")
        assert headers["List-Unsubscribe"].endswith(">")
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    async def test_header_uses_the_supplied_token(self, events, gateway):
        from backend.services.email.unsubscribe import make_token, read_token

        token = make_token("sam@example.com")
        service, _ = _service(events)

        await _send(service, unsubscribe_token=token)

        link = gateway.calls[0]["headers"]["List-Unsubscribe"]
        assert token in link
        assert read_token(link.split("token=", 1)[1].rstrip(">")) == "sam@example.com"

    async def test_header_is_derived_from_the_address_when_no_token_is_given(
        self, events, gateway
    ):
        from backend.services.email.unsubscribe import read_token

        service, _ = _service(events)

        await _send(service, unsubscribe_token=None)

        link = gateway.calls[0]["headers"]["List-Unsubscribe"]
        assert read_token(link.split("token=", 1)[1].rstrip(">")) == "sam@example.com"

    async def test_transactional_send_carries_no_unsubscribe_headers(
        self, events, gateway
    ):
        service, _ = _service(events)

        await _send(service, category=CATEGORY_TRANSACTIONAL)

        assert gateway.calls[0]["headers"] is None


# ---------------------------------------------------------------------------
# Convenience methods
# ---------------------------------------------------------------------------

def _user(email="sam@example.com", display_name="Stephen"):
    return SimpleNamespace(
        id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        email=email,
        display_name=display_name,
    )


class TestSendWelcome:
    async def test_uses_the_welcome_dedupe_key_and_template(
        self, events, gateway
    ):
        service, repo = _service(events)
        user = _user()

        status = await service.send_welcome(
            user=user, promo_code="ROOK-WELCOME", referral_code="ROOK-7K2M9X"
        )

        assert status == SEND_SENT
        assert repo.claims[0]["dedupe_key"] == f"welcome:{user.id}"
        assert repo.claims[0]["template"] == svc.TEMPLATE_WELCOME
        assert repo.claims[0]["category"] == CATEGORY_PROMOTIONAL

    async def test_body_carries_both_codes_and_the_program_percentage(
        self, events, gateway
    ):
        service, _ = _service(events)

        await service.send_welcome(
            user=_user(), promo_code="ROOK-WELCOME", referral_code="ROOK-7K2M9X"
        )

        call = gateway.calls[0]
        pct = REFERRAL_PROGRAM["welcome_percent_off"]
        for body in (call["html"], call["text"]):
            assert "ROOK-WELCOME" in body
            assert "ROOK-7K2M9X" in body
            assert f"{pct}%" in body

    async def test_footer_carries_the_configured_postal_address(
        self, events, gateway
    ):
        service, _ = _service(events)

        await service.send_welcome(
            user=_user(), promo_code="P", referral_code="R"
        )

        assert "Rook LLC, Austin TX" in gateway.calls[0]["html"]

    async def test_second_call_for_the_same_user_is_deduplicated(
        self, events, gateway
    ):
        service, _ = _service(events)
        user = _user()

        first = await service.send_welcome(
            user=user, promo_code="P", referral_code="R"
        )
        second = await service.send_welcome(
            user=user, promo_code="P", referral_code="R"
        )

        assert (first, second) == (SEND_SENT, SEND_SKIPPED)
        assert len(gateway.calls) == 1

    async def test_placeholder_address_user_is_skipped(self, events, gateway):
        service, _ = _service(events)

        status = await service.send_welcome(
            user=_user(email="user_2abc@placeholder.local"),
            promo_code="P",
            referral_code="R",
        )

        assert status == SEND_SKIPPED
        assert gateway.calls == []


class TestSendReferralReward:
    async def test_dedupe_key_includes_the_referral_count(self, events, gateway):
        """Referral 2 and referral 3 are different messages; a redelivery of
        either is not."""
        service, repo = _service(events)
        user = _user()

        await service.send_referral_reward(
            user=user, new_total_percent=20, referral_count=2
        )
        await service.send_referral_reward(
            user=user, new_total_percent=30, referral_count=3
        )

        keys = [c["dedupe_key"] for c in repo.claims]
        assert keys == [
            f"referral_reward:{user.id}:2",
            f"referral_reward:{user.id}:3",
        ]
        assert len(gateway.calls) == 2

    async def test_repeat_for_the_same_count_is_deduplicated(
        self, events, gateway
    ):
        service, _ = _service(events)
        user = _user()

        first = await service.send_referral_reward(
            user=user, new_total_percent=20, referral_count=2
        )
        second = await service.send_referral_reward(
            user=user, new_total_percent=20, referral_count=2
        )

        assert (first, second) == (SEND_SENT, SEND_SKIPPED)
        assert len(gateway.calls) == 1

    async def test_body_states_the_new_total_and_names_nobody_else(
        self, events, gateway
    ):
        service, _ = _service(events)

        await service.send_referral_reward(
            user=_user(), new_total_percent=30, referral_count=3
        )

        call = gateway.calls[0]
        for body in (call["html"], call["text"]):
            assert "30%" in body
            assert "3 confirmed referrals" in body
            # No email address of any kind appears in the reward message — not
            # the redeemer's, and not the referrer's own. The redeemer's is
            # never even available to this call.
            assert "@" not in body

    async def test_uses_the_referral_reward_template_name(self, events, gateway):
        service, repo = _service(events)

        await service.send_referral_reward(
            user=_user(), new_total_percent=10, referral_count=1
        )

        assert repo.claims[0]["template"] == svc.TEMPLATE_REFERRAL_REWARD


# ---------------------------------------------------------------------------
# The never-raises contract on the wrappers
#
# FIX 5. send() was guarded but send_welcome and send_referral_reward were not,
# and those two are what the Clerk and Stripe webhook handlers call. They mint a
# token and render a template BEFORE send() is reached, so a raise there went
# straight into the webhook, the provider treated the event as undelivered, and
# it was redelivered indefinitely.
# ---------------------------------------------------------------------------

def _send_welcome(service):
    return service.send_welcome(user=_user(), promo_code="P", referral_code="R")


def _send_reward(service):
    return service.send_referral_reward(
        user=_user(), new_total_percent=20, referral_count=2
    )


_WRAPPERS = [
    ("send_welcome", "welcome_email", _send_welcome),
    ("send_referral_reward", "referral_reward_email", _send_reward),
]
_WRAPPER_IDS = [name for name, _, _ in _WRAPPERS]

_ALL_STATUSES = frozenset(
    {SEND_SENT, SEND_FAILED, SEND_SKIPPED, SEND_SUPPRESSED, SEND_PENDING}
)


class TestWrappersNeverRaise:
    @pytest.mark.parametrize(
        "wrapper,template_fn,call", _WRAPPERS, ids=_WRAPPER_IDS
    )
    async def test_a_renamed_template_key_returns_a_status(
        self, monkeypatch, events, gateway, wrapper, template_fn, call
    ):
        """templates.py reads keys out of REFERRAL_PROGRAM and TIER_LIMITS; a
        rename there is a KeyError raised before send() is entered."""
        service, _ = _service(events)

        def _boom(**kwargs):
            raise KeyError("referrer_percent_off_per_referral")

        monkeypatch.setattr(svc.templates, template_fn, _boom)

        assert await call(service) == SEND_FAILED
        assert gateway.calls == []

    @pytest.mark.parametrize(
        "wrapper,template_fn,call", _WRAPPERS, ids=_WRAPPER_IDS
    )
    async def test_any_template_exception_returns_a_status(
        self, monkeypatch, events, gateway, wrapper, template_fn, call
    ):
        service, _ = _service(events)

        def _boom(**kwargs):
            raise RuntimeError("template blew up")

        monkeypatch.setattr(svc.templates, template_fn, _boom)

        assert await call(service) == SEND_FAILED

    @pytest.mark.parametrize(
        "wrapper,template_fn,call", _WRAPPERS, ids=_WRAPPER_IDS
    )
    async def test_a_token_minting_failure_returns_a_status(
        self, monkeypatch, events, gateway, wrapper, template_fn, call
    ):
        """make_token is called before the template renders and reads
        settings.secret_key."""
        service, _ = _service(events)
        monkeypatch.setattr(
            svc,
            "make_token",
            lambda email: (_ for _ in ()).throw(RuntimeError("no secret key")),
        )

        assert await call(service) == SEND_FAILED
        assert gateway.calls == []

    @pytest.mark.parametrize(
        "wrapper,template_fn,call", _WRAPPERS, ids=_WRAPPER_IDS
    )
    async def test_the_failure_is_logged(
        self, monkeypatch, events, gateway, caplog, wrapper, template_fn, call
    ):
        """No email_sends row exists at this point, so the log is the only
        record."""
        service, _ = _service(events)

        def _boom(**kwargs):
            raise RuntimeError("template blew up")

        monkeypatch.setattr(svc.templates, template_fn, _boom)

        with caplog.at_level(logging.ERROR, logger=svc.logger.name):
            await call(service)

        assert "template blew up" in caplog.text

    @pytest.mark.parametrize("bad_name", [123, object(), b"bytes", 4.5])
    async def test_a_non_str_display_name_does_not_raise_out_of_send_welcome(
        self, events, gateway, bad_name
    ):
        """_greeting_name calls .strip() on the value, which is an AttributeError
        for anything that is not a str.

        Asserted as "some status came back" rather than a specific one: the
        property under test is that nothing escapes into the webhook. Making the
        template tolerate the value instead would be an improvement, and this
        test must not stand in the way of it."""
        service, _ = _service(events)
        user = SimpleNamespace(
            id=uuid.uuid4(), email="sam@example.com", display_name=bad_name
        )

        status = await service.send_welcome(
            user=user, promo_code="P", referral_code="R"
        )

        assert status in _ALL_STATUSES

    @pytest.mark.parametrize("bad_name", [123, object(), b"bytes", 4.5])
    async def test_a_non_str_display_name_does_not_raise_out_of_the_reward(
        self, events, gateway, bad_name
    ):
        service, _ = _service(events)
        user = SimpleNamespace(
            id=uuid.uuid4(), email="sam@example.com", display_name=bad_name
        )

        status = await service.send_referral_reward(
            user=user, new_total_percent=20, referral_count=2
        )

        assert status in _ALL_STATUSES

    async def test_a_user_with_no_email_attribute_returns_a_status(
        self, events, gateway
    ):
        """user.email is read before send() is entered."""
        service, _ = _service(events)
        user = SimpleNamespace(id=uuid.uuid4(), display_name="Stephen")

        assert await service.send_welcome(
            user=user, promo_code="P", referral_code="R"
        ) == SEND_FAILED
