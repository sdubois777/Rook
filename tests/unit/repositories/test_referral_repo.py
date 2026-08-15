"""Tests for ReferralRepository — referral codes and redemption idempotency.

Repository methods run against a mocked AsyncSession; nothing here touches a real
database. The point of these tests is the CONFLICT behaviour: a redemption that
already exists must return False rather than raise, because the Stripe webhook
gates the referrer's reward on that boolean and an exception would abort its
transaction.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from backend.models.referral import (
    KIND_REFERRAL,
    KIND_WELCOME,
    PENDING_TTL_HOURS,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_REVERSED,
)
from backend.repositories.referral_repo import ReferralRepository


def _make_session(execute_result=None, execute_side_effect=None):
    session = AsyncMock()
    if execute_side_effect is not None:
        session.execute.side_effect = execute_side_effect
    elif execute_result is not None:
        session.execute.return_value = execute_result
    return session


def _params(statement):
    """Every bound value in a statement, with IN-clause lists flattened.

    An `in_()` binds ONE parameter holding a list, so a plain membership test
    against .params.values() misses the individual values.
    """
    values = []
    for value in statement.compile().params.values():
        if isinstance(value, (list, tuple)):
            values.extend(value)
        else:
            values.append(value)
    return values


def _result(*, rowcount=0, scalar=None):
    result = MagicMock()
    result.rowcount = rowcount
    result.scalar_one_or_none.return_value = scalar
    result.scalar.return_value = scalar
    return result


# ── referral codes ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_code_for_user_returns_row():
    row = MagicMock(code="ROOK-ABC123")
    repo = ReferralRepository(_make_session(_result(scalar=row)))

    assert await repo.get_code_for_user(uuid.uuid4()) is row


@pytest.mark.asyncio
async def test_create_code_returns_code_when_inserted():
    repo = ReferralRepository(_make_session(_result(scalar="ROOK-ABC123")))

    assert await repo.create_code(uuid.uuid4(), "ROOK-ABC123") == "ROOK-ABC123"


@pytest.mark.asyncio
async def test_create_code_returns_none_when_conflict_skipped():
    """ON CONFLICT DO NOTHING returns no row — the user already had a code, or
    the drawn code belongs to somebody else."""
    repo = ReferralRepository(_make_session(_result(scalar=None)))

    assert await repo.create_code(uuid.uuid4(), "ROOK-ABC123") is None


@pytest.mark.asyncio
async def test_get_by_code_uppercases_and_strips_input():
    session = _make_session(_result(scalar=None))
    repo = ReferralRepository(session)

    await repo.get_by_code("  rook-abc123  ")

    params = session.execute.await_args.args[0].compile().params
    assert "ROOK-ABC123" in params.values()


@pytest.mark.asyncio
async def test_get_by_code_with_blank_input_skips_the_query():
    session = _make_session(_result(scalar=None))
    repo = ReferralRepository(session)

    assert await repo.get_by_code("   ") is None
    session.execute.assert_not_awaited()


# ── counts and prior redemptions ────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirmed_referral_count_returns_int():
    repo = ReferralRepository(_make_session(_result(scalar=3)))

    assert await repo.confirmed_referral_count(uuid.uuid4()) == 3


@pytest.mark.asyncio
async def test_confirmed_referral_count_with_no_rows_returns_zero():
    repo = ReferralRepository(_make_session(_result(scalar=None)))

    assert await repo.confirmed_referral_count(uuid.uuid4()) == 0


@pytest.mark.asyncio
async def test_confirmed_referral_count_filters_to_confirmed_only():
    session = _make_session(_result(scalar=0))
    repo = ReferralRepository(session)

    await repo.confirmed_referral_count(uuid.uuid4())

    statement = session.execute.await_args.args[0]
    assert "code_redemptions.status" in str(statement)
    assert STATUS_CONFIRMED in statement.compile().params.values()


@pytest.mark.asyncio
async def test_confirmed_referral_count_excludes_a_referral_who_stopped_paying():
    """The count is who is paying now, not who ever paid. Without the join, five
    throwaway accounts that subscribe once and cancel buy a permanent 50% off."""
    from backend.models.user import TIER_LIMITS, TIER_ORDER

    session = _make_session(_result(scalar=0))
    repo = ReferralRepository(session)

    await repo.confirmed_referral_count(uuid.uuid4())

    statement = session.execute.await_args.args[0]
    rendered = str(statement)
    # Joined to users and filtered on the tier — done in SQL, never by loading
    # every historical row into Python.
    assert "JOIN users" in rendered
    assert "users.tier" in rendered
    # Same semantics as effective_tier(): a paid tier whose expiry is absent or
    # still in the future.
    assert "users.tier_expires_at IS NULL" in rendered
    params = _params(statement)
    for tier in TIER_ORDER:
        if TIER_LIMITS[tier]["unlimited_features"]:
            assert tier in params
    assert "free" not in params


@pytest.mark.asyncio
async def test_has_redeemed_from_matches_one_direction_only():
    """The mutual-referral test asks whether A redeemed B's code; it must not
    also match B having redeemed A's."""
    first, second = uuid.uuid4(), uuid.uuid4()
    session = _make_session(_result(scalar=None))
    repo = ReferralRepository(session)

    await repo.has_redeemed_from(redeemer_user_id=first, referrer_user_id=second)

    statement = session.execute.await_args.args[0]
    assert "code_redemptions.redeemer_user_id" in str(statement)
    assert "code_redemptions.referrer_user_id" in str(statement)
    params = statement.compile().params
    assert params["redeemer_user_id_1"] == first
    assert params["referrer_user_id_1"] == second


@pytest.mark.asyncio
async def test_has_redeemed_true_when_row_exists():
    repo = ReferralRepository(_make_session(_result(scalar=uuid.uuid4())))

    assert await repo.has_redeemed(uuid.uuid4(), KIND_WELCOME) is True


@pytest.mark.asyncio
async def test_has_redeemed_false_when_absent():
    repo = ReferralRepository(_make_session(_result(scalar=None)))

    assert await repo.has_redeemed(uuid.uuid4(), KIND_REFERRAL) is False


@pytest.mark.asyncio
async def test_has_redeemed_counts_confirmed_reversed_and_fresh_pending():
    """A REVERSED redemption still occupies the slot, or a refund would hand the
    account a second discount. A PENDING one blocks only while the Stripe session
    it belongs to could still be paid."""
    session = _make_session(_result(scalar=None))
    repo = ReferralRepository(session)

    await repo.has_redeemed(uuid.uuid4(), KIND_REFERRAL)

    statement = session.execute.await_args.args[0]
    params = _params(statement)
    assert STATUS_CONFIRMED in params
    assert STATUS_REVERSED in params
    assert STATUS_PENDING in params
    assert "code_redemptions.created_at >=" in str(statement)


@pytest.mark.asyncio
async def test_has_redeemed_does_not_block_on_an_expired_reservation():
    """An abandoned checkout must not hold the user's once-ever slot forever:
    the Stripe session behind it expired and can never be paid."""
    session = _make_session(_result(scalar=None))
    repo = ReferralRepository(session)

    before = datetime.now(timezone.utc)
    await repo.has_redeemed(uuid.uuid4(), KIND_WELCOME)
    after = datetime.now(timezone.utc)

    statement = session.execute.await_args.args[0]
    cutoff = next(
        value for value in _params(statement) if isinstance(value, datetime)
    )
    ttl = timedelta(hours=PENDING_TTL_HOURS)
    assert before - ttl <= cutoff <= after - ttl


# ── record_redemption ───────────────────────────────────────────────────

def _record_args(**overrides):
    args = dict(
        kind=KIND_REFERRAL,
        code="ROOK-ABC123",
        redeemer_user_id=uuid.uuid4(),
        referrer_user_id=uuid.uuid4(),
        stripe_session_id="cs_test_1",
        percent_off=30,
    )
    args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_record_redemption_returns_true_when_new():
    repo = ReferralRepository(_make_session(_result(rowcount=1)))

    assert await repo.record_redemption(**_record_args()) is True


@pytest.mark.asyncio
async def test_record_redemption_returns_false_on_duplicate_session():
    """Stripe redelivered the same completed checkout — the reward must not move."""
    repo = ReferralRepository(_make_session(_result(rowcount=0)))

    assert await repo.record_redemption(**_record_args()) is False


@pytest.mark.asyncio
async def test_record_redemption_uses_untargeted_on_conflict():
    """Naming one index would leave the OTHER unique constraint raising
    IntegrityError, aborting the webhook transaction and making Stripe retry the
    same event forever."""
    session = _make_session(_result(rowcount=1))
    repo = ReferralRepository(session)

    await repo.record_redemption(**_record_args())

    statement = session.execute.await_args.args[0]
    clause = statement._post_values_clause
    assert clause.constraint_target is None
    assert clause.inferred_target_elements is None


@pytest.mark.asyncio
async def test_record_redemption_returns_false_when_the_user_kind_slot_is_taken():
    """The OTHER unique constraint. Both must resolve to a skip, because an
    IntegrityError would abort the webhook's transaction and Stripe would then
    retry the same event forever, hitting the same constraint every time."""
    repo = ReferralRepository(_make_session(_result(rowcount=0)))

    assert await repo.record_redemption(**_record_args(kind=KIND_WELCOME)) is False


@pytest.mark.asyncio
async def test_record_redemption_opens_no_savepoint():
    """Leaving a nested transaction flushes the WHOLE session. A savepoint here
    would catch an IntegrityError raised by the webhook's own pending tier
    upgrade, report it as 'already redeemed', and roll the tier write back — the
    customer pays and silently loses the upgrade."""
    session = _make_session(_result(rowcount=1))

    await ReferralRepository(session).record_redemption(**_record_args())

    session.begin_nested.assert_not_called()


@pytest.mark.asyncio
async def test_record_redemption_propagates_an_unrelated_integrity_error():
    """Untargeted ON CONFLICT already covers every unique constraint on this
    table, so anything still raising belongs to somebody else's pending statement
    and must fail the webhook so Stripe redelivers."""
    session = _make_session(
        execute_side_effect=IntegrityError("insert", {}, Exception("some other row"))
    )
    repo = ReferralRepository(session)

    with pytest.raises(IntegrityError):
        await repo.record_redemption(**_record_args())


# ── reservations ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reserve_redemption_writes_a_pending_row_and_returns_its_id():
    row_id = uuid.uuid4()
    session = _make_session(_result(scalar=row_id))
    repo = ReferralRepository(session)

    assert await repo.reserve_redemption(**_record_args()) == row_id

    insert = session.execute.await_args.args[0]
    assert insert.compile().params["status"] == STATUS_PENDING


@pytest.mark.asyncio
async def test_reserve_redemption_returns_none_when_the_slot_is_taken():
    """The second of two simultaneous checkouts. Nothing inserted means no
    discounted Stripe session may be created for it."""
    repo = ReferralRepository(_make_session(_result(scalar=None)))

    assert await repo.reserve_redemption(**_record_args()) is None


@pytest.mark.asyncio
async def test_reserve_redemption_replaces_an_expired_pending_row():
    """An abandoned reservation still occupies the unique constraint, so it is
    deleted explicitly first — otherwise the insert conflicts and the user is
    locked out of a discount over a checkout they walked away from."""
    session = _make_session(_result(scalar=uuid.uuid4()))
    repo = ReferralRepository(session)

    await repo.reserve_redemption(**_record_args())

    delete_stmt = session.execute.await_args_list[0].args[0]
    rendered = str(delete_stmt)
    assert rendered.startswith("DELETE FROM code_redemptions")
    # Narrow enough that it can never remove a confirmed or reversed row, or a
    # reservation that is still live.
    assert STATUS_PENDING in delete_stmt.compile().params.values()
    assert "code_redemptions.created_at <" in rendered
    # The insert is the SECOND statement, not the first.
    assert len(session.execute.await_args_list) == 2


@pytest.mark.asyncio
async def test_release_reservation_only_deletes_a_pending_row():
    """A mistimed release must never delete a confirmed redemption — that would
    hand the account a second discount."""
    session = _make_session(_result(rowcount=1))
    repo = ReferralRepository(session)

    await repo.release_reservation(uuid.uuid4())

    statement = session.execute.await_args.args[0]
    assert str(statement).startswith("DELETE FROM code_redemptions")
    assert STATUS_PENDING in statement.compile().params.values()


@pytest.mark.asyncio
async def test_attach_session_id_updates_the_row():
    session = _make_session(_result(rowcount=1))
    repo = ReferralRepository(session)

    await repo.attach_session_id(uuid.uuid4(), "cs_test_9")

    params = session.execute.await_args.args[0].compile().params
    assert params["stripe_session_id"] == "cs_test_9"


@pytest.mark.asyncio
async def test_confirm_redemption_flips_only_a_pending_row():
    session = _make_session(_result(scalar=uuid.uuid4()))
    repo = ReferralRepository(session)

    assert await repo.confirm_redemption("cs_test_1") is True

    statement = session.execute.await_args.args[0]
    params = statement.compile().params
    assert params["status"] == STATUS_CONFIRMED          # the SET
    assert STATUS_PENDING in params.values()             # the guard
    assert "cs_test_1" in params.values()


@pytest.mark.asyncio
async def test_confirm_redemption_is_false_on_redelivery():
    """The row is already confirmed, so the UPDATE matches nothing — which is how
    the referrer's reward moves exactly once per completed checkout."""
    repo = ReferralRepository(_make_session(_result(scalar=None)))

    assert await repo.confirm_redemption("cs_test_1") is False


@pytest.mark.asyncio
async def test_record_redemption_normalizes_the_stored_code():
    session = _make_session(_result(rowcount=1))
    repo = ReferralRepository(session)

    await repo.record_redemption(**_record_args(code=" rook-abc123 "))

    values = session.execute.await_args.args[0].compile().params
    assert values["code"] == "ROOK-ABC123"


@pytest.mark.asyncio
async def test_record_redemption_does_not_commit():
    """The webhook commits once after all its side effects."""
    session = _make_session(_result(rowcount=1))
    repo = ReferralRepository(session)

    await repo.record_redemption(**_record_args())

    session.commit.assert_not_awaited()


# ── reverse_redemption ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reverse_redemption_marks_row_reversed():
    row = MagicMock(status=STATUS_CONFIRMED, reversed_at=None)
    session = _make_session(_result(scalar=row))
    repo = ReferralRepository(session)

    returned = await repo.reverse_redemption("cs_test_1")

    assert returned is row
    assert row.status == STATUS_REVERSED
    assert row.reversed_at is not None
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_reverse_redemption_returns_none_when_absent():
    repo = ReferralRepository(_make_session(_result(scalar=None)))

    assert await repo.reverse_redemption("cs_missing") is None


@pytest.mark.asyncio
async def test_reverse_redemption_returns_none_when_already_reversed():
    """Second call is a no-op so the referrer's reward is lowered only once."""
    row = MagicMock(status=STATUS_REVERSED)
    session = _make_session(_result(scalar=row))
    repo = ReferralRepository(session)

    assert await repo.reverse_redemption("cs_test_1") is None
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_delegates_to_the_session():
    session = _make_session()
    await ReferralRepository(session).commit()
    session.commit.assert_awaited_once()
