"""Tests for EmailRepository — the suppression list and the send lock.

Nothing here touches a database. The session is a mock, and the assertions about
normalization read the compiled statement's bound parameters, which is the only
way to prove the address was lowercased on the way INTO the query rather than
just on the way out.

THE RETRY RULE IS TESTED THROUGH THE COMPILED SQL. Which rows claim_send will
re-claim is decided by Postgres inside `ON CONFLICT DO UPDATE ... WHERE`, and no
Postgres runs in a unit test. Re-implementing that rule in Python here would test
the copy rather than the thing. So `_reclaim_rule_from_sql` reads the predicate
back out of the statement the repository actually emits, and the named cases are
evaluated against THAT — if the WHERE clause changes, the cases change with it or
they fail.
"""
from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects.postgresql import dialect as pg_dialect

from backend.models.email import (
    MAX_SEND_ATTEMPTS,
    SEND_FAILED,
    SEND_PENDING,
    SEND_SENT,
    SEND_SKIPPED,
    SEND_SUPPRESSED,
)
from backend.repositories.email_repo import (
    _ERROR_MAX_CHARS,
    EmailRepository,
    normalize_email,
)


def _make_session(*, execute_result=None, get_result=None):
    session = AsyncMock()
    session.execute.return_value = execute_result or MagicMock()
    session.get.return_value = get_result
    return session


def _executed_stmt(session):
    """The statement handed to session.execute on the most recent call."""
    return session.execute.await_args.args[0]


def _params(stmt) -> dict:
    return stmt.compile(dialect=pg_dialect()).params


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _pg_sql(stmt) -> str:
    """Literal-bound SQL. ON CONFLICT is a Postgres construct, so the Postgres
    dialect is required to compile it at all."""
    return str(
        stmt.compile(dialect=pg_dialect(), compile_kwargs={"literal_binds": True})
    )


# ---------------------------------------------------------------------------
# normalize_email
# ---------------------------------------------------------------------------

class TestNormalizeEmail:
    def test_lowercases_and_strips(self):
        assert normalize_email("  Sam@Example.COM ") == "sam@example.com"

    def test_none_becomes_empty_string(self):
        assert normalize_email(None) == ""


# ---------------------------------------------------------------------------
# is_suppressed
# ---------------------------------------------------------------------------

class TestIsSuppressed:
    async def test_returns_true_when_a_row_exists(self):
        result = MagicMock()
        result.scalar_one_or_none.return_value = "sam@example.com"
        repo = EmailRepository(_make_session(execute_result=result))

        assert await repo.is_suppressed("sam@example.com") is True

    async def test_returns_false_when_no_row_exists(self):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        repo = EmailRepository(_make_session(execute_result=result))

        assert await repo.is_suppressed("sam@example.com") is False

    async def test_queries_the_lowercased_address(self):
        """A mixed-case lookup must still find a lowercased suppression row —
        otherwise someone who unsubscribed gets mailed again."""
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session = _make_session(execute_result=result)
        repo = EmailRepository(session)

        await repo.is_suppressed("  Sam@Example.COM ")

        sql = _sql(_executed_stmt(session))
        assert "'sam@example.com'" in sql
        assert "Sam@Example.COM" not in sql


# ---------------------------------------------------------------------------
# suppress
# ---------------------------------------------------------------------------

class TestSuppress:
    async def test_returns_true_when_newly_suppressed(self):
        result = MagicMock(rowcount=1)
        repo = EmailRepository(_make_session(execute_result=result))

        assert await repo.suppress("sam@example.com", "unsubscribe") is True

    async def test_returns_false_when_already_suppressed(self):
        """ON CONFLICT DO NOTHING => rowcount 0 on a second unsubscribe click."""
        result = MagicMock(rowcount=0)
        repo = EmailRepository(_make_session(execute_result=result))

        assert await repo.suppress("sam@example.com", "unsubscribe") is False

    async def test_stores_the_lowercased_address_and_the_reason(self):
        session = _make_session(execute_result=MagicMock(rowcount=1))
        repo = EmailRepository(session)

        await repo.suppress(" Sam@Example.COM ", "bounce")

        params = _params(_executed_stmt(session))
        assert params["email"] == "sam@example.com"
        assert params["reason"] == "bounce"


# ---------------------------------------------------------------------------
# claim_send — the send lock
# ---------------------------------------------------------------------------

async def _claim_statement():
    """The INSERT ... ON CONFLICT that claim_send emits, as SQLAlchemy built it."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = uuid.uuid4()
    session = _make_session(execute_result=result, get_result=MagicMock())
    await EmailRepository(session).claim_send(
        dedupe_key="welcome:abc",
        to_email="sam@example.com",
        template="welcome",
        category="promotional",
    )
    return _executed_stmt(session)


# WHERE email_sends.status = '<status>' AND email_sends.attempts < <ceiling>
_RECLAIM_RE = re.compile(
    r"WHERE email_sends\.status = '(?P<status>[a-z]+)'"
    r" AND email_sends\.attempts < (?P<ceiling>\d+)"
)


async def _reclaim_rule_from_sql() -> tuple[str, int]:
    """The re-claim predicate exactly as Postgres will apply it."""
    sql = _pg_sql(await _claim_statement())
    match = _RECLAIM_RE.search(sql)
    assert match, f"claim_send carries no re-claim WHERE clause:\n{sql}"
    return match.group("status"), int(match.group("ceiling"))


async def _would_reclaim(status: str, attempts: int) -> bool:
    """Would the emitted WHERE clause re-claim a row in this state?"""
    reclaim_status, ceiling = await _reclaim_rule_from_sql()
    return status == reclaim_status and attempts < ceiling


class TestClaimSend:
    async def test_returns_the_row_when_the_key_is_new(self):
        row = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = uuid.uuid4()
        session = _make_session(execute_result=result, get_result=row)
        repo = EmailRepository(session)

        claimed = await repo.claim_send(
            dedupe_key="welcome:abc",
            to_email="sam@example.com",
            template="welcome",
            category="promotional",
        )

        assert claimed is row
        session.get.assert_awaited_once()

    async def test_returns_none_when_the_claim_is_refused(self):
        """No row came back from RETURNING, so the conflicting row did not
        satisfy the re-claim WHERE. The caller must not reach the provider."""
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session = _make_session(execute_result=result)
        repo = EmailRepository(session)

        claimed = await repo.claim_send(
            dedupe_key="welcome:abc",
            to_email="sam@example.com",
            template="welcome",
            category="promotional",
        )

        assert claimed is None
        session.get.assert_not_awaited()

    async def test_the_placeholder_status_is_pending(self):
        """'pending' means the provider call was started and we never learned the
        result — a row an operator must investigate. 'skipped' would say we chose
        not to send, which is a different and misleading claim."""
        params = _params(await _claim_statement())

        assert params["status"] == SEND_PENDING
        assert params["status"] != SEND_SKIPPED
        assert params["status"] != SEND_SENT

    async def test_a_new_key_starts_at_one_attempt(self):
        """The claim is taken in order to make the call, and EmailService makes
        it unconditionally once the claim commits — so the attempt is counted
        here, not after the provider answers."""
        assert _params(await _claim_statement())["attempts"] == 1

    async def test_a_reclaim_increments_the_stored_attempt_count(self):
        sql = _pg_sql(await _claim_statement())

        assert "attempts = (email_sends.attempts + 1)" in sql

    async def test_the_row_is_updated_in_place_and_never_deleted(self):
        """Deleting the row to allow a retry would reopen the double-send race
        the UNIQUE dedupe_key exists to close."""
        sql = _pg_sql(await _claim_statement())

        assert "ON CONFLICT (dedupe_key) DO UPDATE" in sql
        assert "DELETE" not in sql.upper()

    async def test_a_reclaim_clears_the_previous_failure_details(self):
        sql = _pg_sql(await _claim_statement())

        assert "error = NULL" in sql
        assert "provider_message_id = NULL" in sql

    async def test_a_reclaim_updates_the_recipient_address(self):
        """The audit row must name the address this attempt actually mails. An
        address can change between attempts — the identity provider's email was
        updated, or the first attempt ran while a placeholder was in play — and
        without this the row would show the old one while the message went to the
        new one."""
        sql = _pg_sql(await _claim_statement())

        assert "to_email = excluded.to_email" in sql

    async def test_a_reclaim_does_not_rewrite_the_message_identity(self):
        """template, category and user_id stay as first written: one dedupe key
        is one message to one user, so nothing about those can legitimately
        differ between attempts."""
        sql = _pg_sql(await _claim_statement())
        update_clause = sql.split("DO UPDATE SET", 1)[1]

        for column in ("template =", "category =", "user_id ="):
            assert column not in update_clause

    async def test_the_reclaim_rule_is_the_failed_status_and_max_send_attempts(
        self,
    ):
        """The two constants in the SQL are the ones in backend/models/email.py,
        not literals typed into the repository."""
        status, ceiling = await _reclaim_rule_from_sql()

        assert status == SEND_FAILED
        assert ceiling == MAX_SEND_ATTEMPTS

    @pytest.mark.parametrize("attempts", list(range(1, MAX_SEND_ATTEMPTS)))
    async def test_a_failed_send_is_reclaimable_below_the_ceiling(self, attempts):
        """The defect this fixes: a provider outage used to burn the dedupe key
        forever, so the message was never delivered and nothing retried it."""
        assert await _would_reclaim(SEND_FAILED, attempts) is True

    async def test_a_failed_send_is_not_reclaimable_at_the_ceiling(self):
        assert await _would_reclaim(SEND_FAILED, MAX_SEND_ATTEMPTS) is False

    async def test_a_failed_send_is_not_reclaimable_past_the_ceiling(self):
        assert await _would_reclaim(SEND_FAILED, MAX_SEND_ATTEMPTS + 5) is False

    @pytest.mark.parametrize("attempts", [0, 1, MAX_SEND_ATTEMPTS, MAX_SEND_ATTEMPTS + 1])
    async def test_a_sent_send_is_never_reclaimable_at_any_attempt_count(
        self, attempts
    ):
        """A delivered message cannot be un-sent, so no attempt count reopens it."""
        assert await _would_reclaim(SEND_SENT, attempts) is False

    async def test_a_pending_send_is_not_reclaimable(self):
        """'pending' is a call in flight, or one that crashed mid-call — nobody
        knows whether the message went out."""
        assert await _would_reclaim(SEND_PENDING, 1) is False

    @pytest.mark.parametrize("status", [SEND_SKIPPED, SEND_SUPPRESSED])
    async def test_a_status_that_never_reaches_this_table_is_also_refused(
        self, status
    ):
        """Neither of these is ever WRITTEN to email_sends — every check that
        produces one returns before claim_send is reached, so a skipped or
        suppressed send leaves no row at all. Asserted anyway because the WHERE
        clause is the only guard if that ever changes."""
        assert await _would_reclaim(status, 1) is False

    async def test_stores_the_lowercased_recipient_and_the_supplied_fields(self):
        user_id = uuid.uuid4()
        result = MagicMock()
        result.scalar_one_or_none.return_value = uuid.uuid4()
        session = _make_session(execute_result=result, get_result=MagicMock())
        repo = EmailRepository(session)

        await repo.claim_send(
            dedupe_key="referral_reward:abc:2",
            to_email="  Sam@Example.COM ",
            template="referral_reward",
            category="promotional",
            user_id=user_id,
        )

        params = _params(_executed_stmt(session))
        assert params["to_email"] == "sam@example.com"
        assert params["dedupe_key"] == "referral_reward:abc:2"
        assert params["template"] == "referral_reward"
        assert params["category"] == "promotional"
        assert params["user_id"] == user_id


# ---------------------------------------------------------------------------
# mark_status
# ---------------------------------------------------------------------------

class TestMarkStatus:
    async def test_records_the_provider_message_id(self):
        session = _make_session()
        repo = EmailRepository(session)
        send_id = uuid.uuid4()

        await repo.mark_status(
            send_id, SEND_SENT, provider_message_id="msg_123"
        )

        params = _params(_executed_stmt(session))
        assert params["status"] == SEND_SENT
        assert params["provider_message_id"] == "msg_123"
        assert params["error"] is None

    async def test_truncates_a_huge_provider_error(self):
        """A provider can answer with an entire HTML page; the row stays readable."""
        session = _make_session()
        repo = EmailRepository(session)

        await repo.mark_status(
            uuid.uuid4(), SEND_FAILED, error="x" * (_ERROR_MAX_CHARS * 3)
        )

        params = _params(_executed_stmt(session))
        assert len(params["error"]) == _ERROR_MAX_CHARS

    async def test_empty_error_is_stored_as_null(self):
        session = _make_session()
        repo = EmailRepository(session)

        await repo.mark_status(uuid.uuid4(), SEND_SENT, error="")

        assert _params(_executed_stmt(session))["error"] is None


class TestCommit:
    async def test_commit_delegates_to_the_session(self):
        session = _make_session()
        repo = EmailRepository(session)

        await repo.commit()

        session.commit.assert_awaited_once()
