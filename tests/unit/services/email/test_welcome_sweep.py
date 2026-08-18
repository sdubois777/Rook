"""Tests for the welcome-email backstop.

CONTEXT. The Clerk user.created webhook was the ONLY thing that ever sent a
welcome email, and in production it never fired once — no webhook endpoint had
been created in Clerk. Every account was made by the first-authenticated-request
path instead, which sends nothing. Nineteen accounts existed and not one had been
welcomed automatically; the only messages customers received came from someone
running scripts/backfill_welcome_emails.py by hand.

backend/services/email/welcome_sweep.py is the backstop. The tests that matter
most here are the ones about WHO IT SELECTS, because this job emails real
customers: selecting too few leaves people unwelcomed, and selecting too many
mails people a second time.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.email.welcome_sweep import (
    WELCOME_KEY_PREFIX,
    _already_welcomed,
    _welcome_key,
    count_accounts_missing_welcome,
    find_accounts_missing_welcome,
    run_welcome_sweep,
)


# ---------------------------------------------------------------------------
# The match between a user row and their welcome send.
#
# This is the highest-value test in the file. The first version of this module
# built the match with a Python f-string applied to a SQLAlchemy COLUMN, which
# interpolates the column's repr: the pattern became the literal text
# "welcome:users.id", matched no row, and every account looked unwelcomed. Run
# read-only against production it selected 16 customers who had already been
# mailed. Had it shipped, all 16 would have been emailed a second time.
# ---------------------------------------------------------------------------

def _compiled(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))


def test_the_match_concatenates_the_user_id_in_sql_not_in_python():
    """The LIKE pattern must be built per row BY THE DATABASE.

    A Python f-string over a column produces one constant pattern containing the
    text "users.id" for every row. Matching nothing, it reports every account as
    never welcomed — which is the same answer as "the feature is broken", and is
    why this went unnoticed until it was pointed at real data.
    """
    sql = _compiled(_already_welcomed().select())

    # THE BROKEN FORM: the whole pattern collapses into one quoted literal holding
    # the column's repr. Depending on how the column is referenced that renders as
    # 'welcome:User.id%' or 'welcome:users.id%', so match either — an earlier
    # version of this assertion pinned only the lowercase spelling and therefore
    # did NOT catch the very bug it was written for.
    lowered = sql.lower()
    assert "'welcome:user" not in lowered, (
        "the pattern is a constant containing the column's repr, so it matches "
        f"nothing and every account reads as never welcomed. Compiled SQL:\n{sql}"
    )
    # THE CORRECT FORM: the database concatenates the prefix, this row's id, and
    # the wildcard, per row.
    assert "||" in sql or "concat" in sql.lower(), (
        f"no SQL string concatenation in the pattern. Compiled SQL:\n{sql}"
    )
    assert "CAST(users.id" in sql or "users.id::" in sql, (
        f"the id is not cast for concatenation. Compiled SQL:\n{sql}"
    )
    assert WELCOME_KEY_PREFIX in sql


def test_the_key_helper_still_builds_a_real_key_for_a_real_id():
    uid = uuid.uuid4()
    assert _welcome_key(uid) == f"welcome:{uid}"


def test_the_match_also_covers_a_corrected_resend():
    """A corrected resend claims welcome:<id>:<reason>. Someone who received only
    that copy HAS been welcomed and must not be mailed again.

    Twelve accounts in production are in exactly this state: an original send whose
    links pointed at localhost, then a corrected one under a suffixed key.
    """
    sql = _compiled(_already_welcomed().select())
    # A prefix match ("... || '%'"), not an equality test, is what covers the suffix.
    assert "LIKE" in sql.upper()
    assert "'%'" in sql or "%" in sql


# ---------------------------------------------------------------------------
# Selection rules
# ---------------------------------------------------------------------------

def _fake_user(email="new@example.com", display_name=""):
    u = MagicMock()
    u.id = uuid.uuid4()
    u.email = email
    u.display_name = display_name
    return u


def _session_returning(users):
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = users
    result.scalar_one.return_value = len(users)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_placeholder_addresses_are_never_selected():
    """Synthesized addresses resolve to nothing. Mailing them earns hard bounces,
    and a bounce rate is what gets a sending domain blocked."""
    real = _fake_user("real@gmail.com")
    placeholders = [_fake_user("someone@dev.local"),
                    _fake_user("other@placeholder.local")]
    session = _session_returning([real] + placeholders)

    found = await find_accounts_missing_welcome(session, limit=25, min_age_minutes=10)

    assert found == [real]


@pytest.mark.asyncio
async def test_the_batch_limit_and_age_floor_reach_the_query():
    session = _session_returning([])
    await find_accounts_missing_welcome(session, limit=7, min_age_minutes=30)

    sql = _compiled(session.execute.await_args.args[0])
    assert "LIMIT 7" in sql
    # The age floor is a bound parameter on created_at, so assert the column is
    # constrained rather than trying to match a timestamp literal.
    assert "created_at <=" in sql


@pytest.mark.asyncio
async def test_the_count_ignores_age_and_batch():
    """The count answers 'is the invariant holding', so it must not be narrowed by
    the sweep's own pacing controls — otherwise a backlog reads as zero."""
    session = _session_returning([_fake_user(), _fake_user()])
    await count_accounts_missing_welcome(session)

    sql = _compiled(session.execute.await_args.args[0])
    assert "LIMIT" not in sql.upper()
    assert "created_at <=" not in sql


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_disabled_sweep_sends_nothing():
    session = _session_returning([_fake_user()])
    with patch("backend.config.settings.welcome_sweep_enabled", False):
        result = await run_welcome_sweep(session)
    assert result == {"enabled": False, "found": 0, "sent": 0, "failed": 0}


@pytest.mark.asyncio
async def test_one_account_failing_does_not_stop_the_rest():
    """This runs on the scheduler. One bad account must not cost the others their
    email, and must not kill the job."""
    from backend.services.email.email_service import SEND_SENT

    good_a, bad, good_b = _fake_user("a@x.com"), _fake_user("b@x.com"), _fake_user("c@x.com")
    session = _session_returning([good_a, bad, good_b])

    emails = MagicMock()
    emails.send_welcome = AsyncMock(side_effect=[SEND_SENT, RuntimeError("boom"), SEND_SENT])
    referrals = MagicMock()
    referrals.get_or_create_code = AsyncMock(return_value="CODE")
    referrals.welcome_code_for = MagicMock(return_value="WELCOME")

    with (
        patch("backend.services.email.email_service.EmailService.from_session",
              return_value=emails),
        patch("backend.services.referral_service.ReferralService.from_session",
              return_value=referrals),
    ):
        result = await run_welcome_sweep(session)

    assert result["found"] == 3
    assert result["sent"] == 2
    assert result["failed"] == 1
    assert emails.send_welcome.await_count == 3


@pytest.mark.asyncio
async def test_a_refused_send_counts_as_failed_and_is_not_reported_as_sent():
    """Most refusals write NO email_sends row, so if the sweep counted them as
    successes the backstop would report health while sending nothing — the exact
    blindness this module exists to remove."""
    from backend.services.email.email_service import SEND_SKIPPED

    session = _session_returning([_fake_user()])
    emails = MagicMock()
    emails.send_welcome = AsyncMock(return_value=SEND_SKIPPED)
    referrals = MagicMock()
    referrals.get_or_create_code = AsyncMock(return_value="CODE")
    referrals.welcome_code_for = MagicMock(return_value="WELCOME")

    with (
        patch("backend.services.email.email_service.EmailService.from_session",
              return_value=emails),
        patch("backend.services.referral_service.ReferralService.from_session",
              return_value=referrals),
    ):
        result = await run_welcome_sweep(session)

    assert result["sent"] == 0
    assert result["failed"] == 1
    assert result["outcomes"] == {SEND_SKIPPED: 1}


@pytest.mark.asyncio
async def test_the_referral_code_is_committed_before_the_message_goes_out():
    """The message contains the code. A code rolled back after delivery leaves the
    recipient holding one that does not exist."""
    from backend.services.email.email_service import SEND_SENT

    session = _session_returning([_fake_user()])
    order: list[str] = []

    emails = MagicMock()

    async def _send(**_kw):
        order.append("send")
        return SEND_SENT

    emails.send_welcome = AsyncMock(side_effect=_send)
    referrals = MagicMock()
    referrals.get_or_create_code = AsyncMock(return_value="CODE")
    referrals.welcome_code_for = MagicMock(return_value="WELCOME")

    async def _commit():
        order.append("commit")

    session.commit = AsyncMock(side_effect=_commit)

    with (
        patch("backend.services.email.email_service.EmailService.from_session",
              return_value=emails),
        patch("backend.services.referral_service.ReferralService.from_session",
              return_value=referrals),
    ):
        await run_welcome_sweep(session)

    assert order.index("commit") < order.index("send")
