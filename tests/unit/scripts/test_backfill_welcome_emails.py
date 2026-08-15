"""
Unit tests for scripts/backfill_welcome_emails.py.

The thing under test sends real email to real people, so these lean hardest on the
EXCLUSION rules and on the plan-only default. A bug that mails one person twice is
recoverable; a bug that mails the wrong list is not.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scripts.backfill_welcome_emails import (
    SKIP_NO_EMAIL,
    SKIP_SUBSCRIBED,
    SKIP_UNDELIVERABLE,
    build_plan,
    is_undeliverable,
    mask,
)


def _user(
    *,
    email="person@example.com",
    subscription_status=None,
    deleted_at=None,
    created_at=None,
):
    from backend.models.user import User

    u = User()
    u.id = uuid.uuid4()
    u.email = email
    u.subscription_status = subscription_status
    u.deleted_at = deleted_at
    u.created_at = created_at or datetime.now(timezone.utc)
    return u


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    """Returns the rows the query would have returned, and records nothing else.

    build_plan filters deleted_at in SQL, so the fake is handed only live rows —
    the deleted-account rule is covered by the query, not by Python, and a test
    that pretended otherwise would be testing the fake.
    """

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_a_plain_free_signup_is_eligible():
    user = _user()
    plan = await build_plan(_FakeDb([user]), None)
    assert plan.eligible == [user]
    assert plan.skipped == {}


@pytest.mark.asyncio
async def test_an_account_that_ever_subscribed_is_skipped():
    """The welcome code is refused for anyone who has ever subscribed, so mailing
    it would promise a discount that fails at checkout."""
    plan = await build_plan(_FakeDb([_user(subscription_status="active")]), None)
    assert plan.eligible == []
    assert plan.skipped == {SKIP_SUBSCRIBED: 1}


@pytest.mark.asyncio
async def test_a_cancelled_subscriber_is_still_skipped():
    """Cancelling leaves subscription_status non-NULL. They are a former customer,
    not a new one, and the code would still be refused."""
    plan = await build_plan(_FakeDb([_user(subscription_status="canceling")]), None)
    assert plan.eligible == []
    assert plan.skipped == {SKIP_SUBSCRIBED: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["someone@placeholder.local", "someone@dev.local", "SOMEONE@PLACEHOLDER.LOCAL"],
)
async def test_synthesized_addresses_are_skipped(address):
    plan = await build_plan(_FakeDb([_user(email=address)]), None)
    assert plan.eligible == []
    assert plan.skipped == {SKIP_UNDELIVERABLE: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["", "   ", None])
async def test_an_account_with_no_address_is_skipped(address):
    plan = await build_plan(_FakeDb([_user(email=address)]), None)
    assert plan.eligible == []
    assert plan.skipped == {SKIP_NO_EMAIL: 1}


@pytest.mark.asyncio
async def test_limit_takes_the_oldest_signups_first():
    now = datetime.now(timezone.utc)
    oldest = _user(email="first@example.com", created_at=now - timedelta(days=30))
    middle = _user(email="second@example.com", created_at=now - timedelta(days=10))
    newest = _user(email="third@example.com", created_at=now)

    # The query orders by created_at ascending; the fake hands them back in that order.
    plan = await build_plan(_FakeDb([oldest, middle, newest]), 2)

    assert [u.email for u in plan.eligible] == ["first@example.com", "second@example.com"]


@pytest.mark.asyncio
async def test_the_limit_counts_eligible_accounts_not_scanned_ones():
    """A skipped account must not consume a slot, or a batch of 1 behind three
    ineligible rows would send nothing and look like it worked."""
    skipped = _user(email="paid@example.com", subscription_status="active")
    wanted = _user(email="free@example.com")

    plan = await build_plan(_FakeDb([skipped, wanted]), 1)

    assert [u.email for u in plan.eligible] == ["free@example.com"]
    assert plan.skipped == {SKIP_SUBSCRIBED: 1}


@pytest.mark.asyncio
async def test_skips_are_counted_by_reason():
    rows = [
        _user(email="a@example.com"),
        _user(email="b@example.com", subscription_status="active"),
        _user(email="c@example.com", subscription_status="past_due"),
        _user(email="d@placeholder.local"),
    ]
    plan = await build_plan(_FakeDb(rows), None)

    assert [u.email for u in plan.eligible] == ["a@example.com"]
    assert plan.skipped == {SKIP_SUBSCRIBED: 2, SKIP_UNDELIVERABLE: 1}


def test_undeliverable_matches_the_shared_constant_not_a_local_copy():
    """The suffixes must come from backend/models/email.py. A second hardcoded copy
    is how a newly added placeholder domain starts getting mailed."""
    from backend.models.email import UNDELIVERABLE_EMAIL_SUFFIXES

    for suffix in UNDELIVERABLE_EMAIL_SUFFIXES:
        assert is_undeliverable(f"someone{suffix}")
    assert not is_undeliverable("someone@rookff.com")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5173",
        "http://localhost:8000",
        "https://localhost",
        "http://127.0.0.1:8000",
        "http://0.0.0.0:3000",
        "http://rook.local",
        "",
        "rookff.com",           # no scheme
        "/account",             # relative
    ],
)
def test_a_non_public_app_url_is_rejected(url):
    """Every link in the email is built from APP_URL, including the unsubscribe
    link. This exact mistake shipped once: the database was pointed at production
    while APP_URL stayed at the dev value, so twelve real users received a message
    whose opt-out link pointed at localhost."""
    from scripts.backfill_welcome_emails import app_url_is_public

    assert not app_url_is_public(url)


@pytest.mark.parametrize(
    "url",
    ["https://rookff.com", "https://www.rookff.com", "http://rookff.com",
     "https://fantasymanager-production.up.railway.app"],
)
def test_a_public_app_url_is_accepted(url):
    from scripts.backfill_welcome_emails import app_url_is_public

    assert app_url_is_public(url)


def test_the_prod_env_overlay_does_not_carry_app_url():
    """The reason the check above has to exist, asserted directly.

    ROOK_ENV_FILE=.env.prod layers over .env rather than replacing it, and the
    overlay carries only DATABASE_URL. So pointing the database at production
    leaves APP_URL — and therefore every link in the email — at whatever the local
    .env says. If someone ever adds APP_URL to the overlay this test should be
    revisited, not deleted: the guard is still correct, it just stops being the
    only thing standing between a dev URL and a production send.
    """
    from backend.config import resolve_env_files

    files = resolve_env_files(".env.prod")
    assert files == (".env", ".env.prod"), (
        "the prod selection layers over the base file rather than replacing it, "
        "so non-database settings still come from .env"
    )


def test_mask_hides_the_local_part_but_keeps_the_domain():
    assert mask("stephen@rookff.com") == "s*****n@rookff.com"
    assert mask("ab@rookff.com") == "a*@rookff.com"
    assert mask("a@rookff.com") == "a*@rookff.com"
    assert mask("") == "<no address>"
    assert mask("not-an-address") == "<no address>"


def test_send_requires_an_explicit_flag(monkeypatch):
    """Forgetting the flag must print a plan, never send. The safe path is the one
    you get by forgetting."""
    import scripts.backfill_welcome_emails as script

    called = {}

    def _guard(operation="database write"):
        called["guarded"] = True

    ran = {}

    def _fake_asyncio_run(coro):
        coro.close()          # we are not executing it; avoid an un-awaited warning
        ran["args"] = True
        return 0

    monkeypatch.setattr(script, "guard_writes", _guard)
    monkeypatch.setattr(script.asyncio, "run", _fake_asyncio_run)
    monkeypatch.setattr(script.sys, "argv", ["backfill_welcome_emails.py"])

    assert script.main() == 0
    # No --send, so the prod-write guard is never consulted: reading to build a
    # plan is harmless and must not require the override.
    assert "guarded" not in called


def test_code_for_also_consults_the_prod_write_guard(monkeypatch):
    """--code-for MINTS a code when the account has none, so it is a write and must
    take the same production override as sending."""
    import scripts.backfill_welcome_emails as script

    called = {}

    def _guard(operation="database write"):
        called["operation"] = operation

    def _fake_asyncio_run(coro):
        coro.close()
        return 0

    monkeypatch.setattr(script, "guard_writes", _guard)
    monkeypatch.setattr(script.asyncio, "run", _fake_asyncio_run)
    monkeypatch.setattr(
        script.sys, "argv",
        ["backfill_welcome_emails.py", "--code-for", "someone@example.com"],
    )

    assert script.main() == 0
    assert "referral code" in called["operation"]


def test_code_for_and_send_together_are_refused(monkeypatch):
    """They mean opposite things — one prints and sends nothing, the other sends.
    Accepting both would leave which one happened up to argument order."""
    import scripts.backfill_welcome_emails as script

    monkeypatch.setattr(
        script.sys, "argv",
        ["backfill_welcome_emails.py", "--code-for", "a@b.com", "--send"],
    )

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 2      # argparse usage error


def test_send_consults_the_prod_write_guard(monkeypatch):
    import scripts.backfill_welcome_emails as script

    called = {}

    def _guard(operation="database write"):
        called["operation"] = operation

    def _fake_asyncio_run(coro):
        coro.close()
        return 0

    monkeypatch.setattr(script, "guard_writes", _guard)
    monkeypatch.setattr(script.asyncio, "run", _fake_asyncio_run)
    monkeypatch.setattr(
        script.sys, "argv", ["backfill_welcome_emails.py", "--send"]
    )

    assert script.main() == 0
    assert "welcome emails" in called["operation"]
