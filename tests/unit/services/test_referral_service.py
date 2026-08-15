"""Tests for ReferralService — code generation and code validation.

No database and no Stripe. The repository and the user repository are replaced by
in-memory fakes, so every rule under test is the service's own.

Percentages are never written literally in this file: they are read from
REFERRAL_PROGRAM, so changing a rate in backend/models/user.py cannot leave a
test asserting the old number.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from backend.models.referral import (
    KIND_REFERRAL,
    KIND_WELCOME,
    STATUS_CONFIRMED,
    STATUS_PENDING,
)
from backend.models.user import REFERRAL_PROGRAM, referrer_percent_off
from backend.services.referral_service import ReferralService


class _FakeReferralRepo:
    """In-memory stand-in for ReferralRepository."""

    def __init__(
        self,
        *,
        codes=None,
        redeemed=None,
        confirmed_count=0,
        redeemed_from=None,
        redeemed_status=None,
    ):
        # code string -> owner user id
        self._codes = dict(codes or {})
        # set of (user_id, kind) already taken
        self._redeemed = set(redeemed or ())
        # (user_id, kind) -> status, for the cases that care WHICH status holds
        # the slot. Anything in _redeemed without an entry here reads as
        # 'confirmed', so existing callers keep the plain already-used meaning.
        self._redeemed_status = dict(redeemed_status or {})
        # set of (redeemer_user_id, referrer_user_id) pairs already redeemed
        self._redeemed_from = set(redeemed_from or ())
        self._confirmed_count = confirmed_count
        self.created: list[tuple[uuid.UUID, str]] = []
        self.reserved: list[dict] = []
        self.attached: list[tuple] = []
        self.released: list[uuid.UUID] = []
        self.commits = 0

    async def get_code_for_user(self, user_id):
        for code, owner in self._codes.items():
            if owner == user_id:
                return SimpleNamespace(user_id=owner, code=code)
        return None

    async def create_code(self, user_id, code):
        """Insert-or-skip, mirroring both UNIQUE constraints."""
        self.created.append((user_id, code))
        if code in self._codes or user_id in self._codes.values():
            return None
        self._codes[code] = user_id
        return code

    async def get_by_code(self, code):
        owner = self._codes.get((code or "").strip().upper())
        if owner is None:
            return None
        return SimpleNamespace(user_id=owner, code=code.strip().upper())

    async def confirmed_referral_count(self, referrer_user_id):
        return self._confirmed_count

    async def blocking_redemption_status(self, redeemer_user_id, kind):
        """The status of the row holding this slot, or None when it is free.

        The real repository returns the STATUS rather than a bool so the service
        can tell an open checkout apart from a spent discount — those are
        different instructions to the customer. The fake stores which status
        each taken slot carries; a slot recorded without one defaults to
        'confirmed', which is the ordinary "already used it" case.
        """
        slot = (redeemer_user_id, kind)
        if slot not in self._redeemed:
            return None
        return self._redeemed_status.get(slot, STATUS_CONFIRMED)

    async def has_redeemed(self, redeemer_user_id, kind):
        return (
            await self.blocking_redemption_status(redeemer_user_id, kind)
        ) is not None

    async def has_redeemed_from(self, *, redeemer_user_id, referrer_user_id):
        return (redeemer_user_id, referrer_user_id) in self._redeemed_from

    async def reserve_redemption(self, **kwargs):
        """Insert-or-skip on UNIQUE(redeemer_user_id, kind)."""
        self.reserved.append(kwargs)
        slot = (kwargs["redeemer_user_id"], kwargs["kind"])
        if slot in self._redeemed:
            return None
        self._redeemed.add(slot)
        return uuid.uuid4()

    async def attach_session_id(self, reservation_id, stripe_session_id):
        self.attached.append((reservation_id, stripe_session_id))

    async def release_reservation(self, reservation_id):
        self.released.append(reservation_id)

    async def commit(self):
        self.commits += 1


class _FakeUserRepo:
    """Only `get` is used — resolve_code checks the code owner still exists and
    reads the redeemer's tier before allowing a welcome code."""

    def __init__(self, users=None):
        self._users = dict(users or {})

    async def get(self, user_id):
        return self._users.get(user_id)


def _live_user(user_id, tier="free", tier_expires_at=None):
    return SimpleNamespace(
        id=user_id, deleted_at=None, tier=tier, tier_expires_at=tier_expires_at
    )


def _service(repo, users=None):
    return ReferralService(repo, _FakeUserRepo(users))


def _welcome_service(repo, user_id, **user_kwargs):
    """A service whose user repo knows `user_id` — every welcome-code path reads
    the redeeming account's tier."""
    return _service(repo, {user_id: _live_user(user_id, **user_kwargs)})


# ── code generation ─────────────────────────────────────────────────────

def test_generate_code_has_the_documented_format():
    code = _service(_FakeReferralRepo()).generate_code()
    prefix, _, body = code.partition("-")

    assert prefix == REFERRAL_PROGRAM["code_prefix"]
    assert len(body) == REFERRAL_PROGRAM["code_length"]
    assert set(body) <= set(REFERRAL_PROGRAM["code_alphabet"])


def test_generate_code_excludes_ambiguous_characters():
    """0/O and 1/I/L are not in the alphabet — codes get read aloud and retyped."""
    service = _service(_FakeReferralRepo())
    bodies = "".join(service.generate_code().split("-")[1] for _ in range(200))

    assert not set(bodies) & set("01OIL")


def test_generate_code_is_not_repeatable():
    service = _service(_FakeReferralRepo())
    assert len({service.generate_code() for _ in range(50)}) > 1


def test_welcome_code_can_never_be_generated():
    """Generated bodies are exactly code_length characters and a welcome body is
    longer, so no draw can ever collide with a welcome code."""
    service = _service(_FakeReferralRepo())
    welcome_body = service.welcome_code_for(uuid.uuid4()).split("-")[1]

    assert len(welcome_body) > REFERRAL_PROGRAM["code_length"]
    assert welcome_body.startswith("W")
    assert set(welcome_body) <= set(REFERRAL_PROGRAM["code_alphabet"])


def test_welcome_code_is_different_for_every_user():
    """A code that worked for anyone would be a standing discount for the whole
    internet the moment one customer posted it."""
    service = _service(_FakeReferralRepo())
    codes = {service.welcome_code_for(uuid.uuid4()) for _ in range(50)}

    assert len(codes) == 50


def test_welcome_code_is_stable_for_one_user():
    """It is derived, not stored, so the email and the checkout must agree."""
    service = _service(_FakeReferralRepo())
    user_id = uuid.uuid4()

    assert service.welcome_code_for(user_id) == service.welcome_code_for(user_id)


def test_welcome_code_changes_with_the_secret_key(monkeypatch):
    """It is signed, not derived from the id alone — an attacker who knows a user
    id still cannot compute their code."""
    from backend.config import settings

    user_id = uuid.uuid4()
    service = _service(_FakeReferralRepo())
    first = service.welcome_code_for(user_id)

    monkeypatch.setattr(settings, "secret_key", "a-different-secret", raising=False)

    assert service.welcome_code_for(user_id) != first


# ── get_or_create_code ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_or_create_code_returns_existing_without_inserting():
    user_id = uuid.uuid4()
    repo = _FakeReferralRepo(codes={"ROOK-EXIST1": user_id})

    assert await _service(repo).get_or_create_code(user_id) == "ROOK-EXIST1"
    assert repo.created == []


@pytest.mark.asyncio
async def test_get_or_create_code_mints_without_committing():
    """The commit belongs to the caller. This can run inside the Stripe webhook's
    transaction, where committing early would persist half-applied entitlement
    state that a later failure could no longer roll back."""
    user_id = uuid.uuid4()
    repo = _FakeReferralRepo()

    code = await _service(repo).get_or_create_code(user_id)

    assert code.startswith(REFERRAL_PROGRAM["code_prefix"] + "-")
    assert repo.commits == 0


@pytest.mark.asyncio
async def test_get_or_create_code_is_idempotent():
    user_id = uuid.uuid4()
    repo = _FakeReferralRepo()
    service = _service(repo)

    first = await service.get_or_create_code(user_id)
    second = await service.get_or_create_code(user_id)

    assert first == second


@pytest.mark.asyncio
async def test_get_or_create_code_returns_the_winner_after_a_concurrent_insert():
    """A losing insert must not raise: re-read and return whatever the winner
    stored, so both concurrent callers see the same code."""
    user_id = uuid.uuid4()

    class _LosesTheRace(_FakeReferralRepo):
        async def create_code(self, uid, code):
            self.created.append((uid, code))
            # The concurrent request won UNIQUE(user_id); our insert is skipped.
            self._codes["ROOK-WINNER"] = uid
            return None

    repo = _LosesTheRace()

    assert await _service(repo).get_or_create_code(user_id) == "ROOK-WINNER"
    assert len(repo.created) == 1  # no pointless second draw
    assert repo.commits == 0       # nothing of ours was written


@pytest.mark.asyncio
async def test_get_or_create_code_gives_up_after_bounded_retries():
    """A permanently colliding keyspace fails loudly rather than spinning."""
    class _AlwaysConflicts(_FakeReferralRepo):
        async def create_code(self, user_id, code):
            self.created.append((user_id, code))
            return None

    repo = _AlwaysConflicts()
    with pytest.raises(RuntimeError):
        await _service(repo).get_or_create_code(uuid.uuid4())

    assert len(repo.created) == 3


# ── resolve_code: rejections ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_code_rejects_season_interval():
    referrer, redeemer = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo(codes={"ROOK-FRIEND": referrer})

    resolved = await _service(repo, {referrer: _live_user(referrer)}).resolve_code(
        code="ROOK-FRIEND", redeemer_user_id=redeemer, interval="season"
    )

    assert resolved.valid is False
    assert resolved.percent_off == 0
    assert "monthly" in resolved.message.lower()


@pytest.mark.asyncio
async def test_resolve_code_rejects_self_referral():
    owner = uuid.uuid4()
    repo = _FakeReferralRepo(codes={"ROOK-MINE01": owner})

    resolved = await _service(repo, {owner: _live_user(owner)}).resolve_code(
        code="ROOK-MINE01", redeemer_user_id=owner, interval="monthly"
    )

    assert resolved.valid is False
    assert resolved.referrer_user_id is None
    assert "your own" in resolved.message.lower()


@pytest.mark.asyncio
async def test_resolve_code_rejects_unknown_code_generically():
    resolved = await _service(_FakeReferralRepo()).resolve_code(
        code="ROOK-NOPE01", redeemer_user_id=uuid.uuid4(), interval="monthly"
    )

    assert resolved.valid is False
    assert resolved.kind is None
    # The message must not hint that the code exists for somebody else.
    assert "referrer" not in resolved.message.lower()
    assert "another" not in resolved.message.lower()


@pytest.mark.asyncio
async def test_resolve_code_rejects_deleted_owner_with_the_same_generic_message():
    """A code whose owner is gone reads exactly like an unknown code, so nothing
    leaks about the state of somebody else's account."""
    referrer, redeemer = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo(codes={"ROOK-GONE01": referrer})
    deleted = SimpleNamespace(id=referrer, deleted_at="2026-01-01")

    resolved = await _service(repo, {referrer: deleted}).resolve_code(
        code="ROOK-GONE01", redeemer_user_id=redeemer, interval="monthly"
    )
    unknown = await _service(_FakeReferralRepo()).resolve_code(
        code="ROOK-NOPE01", redeemer_user_id=redeemer, interval="monthly"
    )

    assert resolved.valid is False
    assert resolved.message == unknown.message


@pytest.mark.asyncio
async def test_resolve_code_rejects_a_second_referral_redemption():
    referrer, redeemer = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo(
        codes={"ROOK-FRIEND": referrer},
        redeemed={(redeemer, KIND_REFERRAL)},
    )

    resolved = await _service(repo, {referrer: _live_user(referrer)}).resolve_code(
        code="ROOK-FRIEND", redeemer_user_id=redeemer, interval="monthly"
    )

    assert resolved.valid is False
    assert "already" in resolved.message.lower()


@pytest.mark.asyncio
async def test_resolve_code_rejects_a_second_welcome_redemption():
    redeemer = uuid.uuid4()
    repo = _FakeReferralRepo(redeemed={(redeemer, KIND_WELCOME)})
    service = _welcome_service(repo, redeemer)

    resolved = await service.resolve_code(
        code=service.welcome_code_for(redeemer),
        redeemer_user_id=redeemer,
        interval="monthly",
    )

    assert resolved.valid is False


@pytest.mark.asyncio
async def test_resolve_code_rejects_another_users_welcome_code():
    """The whole point of signing it per user: a code that leaks, gets forwarded,
    or lands on a coupon site is worthless to everyone but its owner."""
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    service = _service(
        _FakeReferralRepo(),
        {owner: _live_user(owner), stranger: _live_user(stranger)},
    )

    resolved = await service.resolve_code(
        code=service.welcome_code_for(owner),
        redeemer_user_id=stranger,
        interval="monthly",
    )

    assert resolved.valid is False
    assert resolved.percent_off == 0


@pytest.mark.asyncio
async def test_resolve_code_rejects_a_welcome_code_for_a_paying_user():
    """The welcome discount converts a signup who has never paid. An existing
    subscriber redeeming it is a discount on revenue we already had."""
    redeemer = uuid.uuid4()
    service = _welcome_service(_FakeReferralRepo(), redeemer, tier="pro")

    resolved = await service.resolve_code(
        code=service.welcome_code_for(redeemer),
        redeemer_user_id=redeemer,
        interval="monthly",
    )

    assert resolved.valid is False
    assert "subscribed" in resolved.message.lower()


@pytest.mark.asyncio
async def test_resolve_code_accepts_a_welcome_code_after_a_season_pass_expired():
    """The audience test is effective_tier, not the stored tier: a lapsed season
    pass is a free account again."""
    from datetime import datetime, timedelta, timezone

    redeemer = uuid.uuid4()
    service = _welcome_service(
        _FakeReferralRepo(),
        redeemer,
        tier="pro",
        tier_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    resolved = await service.resolve_code(
        code=service.welcome_code_for(redeemer),
        redeemer_user_id=redeemer,
        interval="monthly",
    )

    assert resolved.valid is True


@pytest.mark.asyncio
async def test_resolve_code_rejects_non_ascii_input_without_raising():
    """hmac.compare_digest raises TypeError on a non-ASCII str, so the shape test
    has to reject one before the comparison happens."""
    redeemer = uuid.uuid4()
    service = _welcome_service(_FakeReferralRepo(), redeemer)
    real = service.welcome_code_for(redeemer)

    for code in (real[:-1] + "Ä", "RÖÖK-W" + "2" * 10, "ROOK-W⁰234567"):
        resolved = await service.resolve_code(
            code=code, redeemer_user_id=redeemer, interval="monthly"
        )
        assert resolved.valid is False


@pytest.mark.asyncio
async def test_resolve_code_rejects_mutual_referral():
    """Two accounts that were both going to subscribe anyway must not be able to
    redeem each other's codes and each earn a permanent recurring reward."""
    first, second = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo(
        codes={"ROOK-FIRST1": first},
        # `first` already redeemed `second`'s code.
        redeemed_from={(first, second)},
    )

    resolved = await _service(repo, {first: _live_user(first)}).resolve_code(
        code="ROOK-FIRST1", redeemer_user_id=second, interval="monthly"
    )

    assert resolved.valid is False
    # Generic: confirming what the other account did would leak that it exists.
    assert resolved.message == "That code is not valid."


@pytest.mark.asyncio
async def test_resolve_code_rejects_blank_input():
    resolved = await _service(_FakeReferralRepo()).resolve_code(
        code="   ", redeemer_user_id=uuid.uuid4(), interval="monthly"
    )
    assert resolved.valid is False


# ── resolve_code: acceptances ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_code_accepts_the_welcome_code():
    redeemer = uuid.uuid4()
    service = _welcome_service(_FakeReferralRepo(), redeemer)

    resolved = await service.resolve_code(
        code=service.welcome_code_for(redeemer),
        redeemer_user_id=redeemer,
        interval="monthly",
    )

    assert resolved.valid is True
    assert resolved.kind == KIND_WELCOME
    assert resolved.percent_off == REFERRAL_PROGRAM["welcome_percent_off"]
    assert resolved.referrer_user_id is None


@pytest.mark.asyncio
async def test_resolve_code_accepts_a_friends_code_and_names_the_referrer():
    referrer, redeemer = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo(codes={"ROOK-FRIEND": referrer})

    resolved = await _service(repo, {referrer: _live_user(referrer)}).resolve_code(
        code="ROOK-FRIEND", redeemer_user_id=redeemer, interval="monthly"
    )

    assert resolved.valid is True
    assert resolved.kind == KIND_REFERRAL
    assert resolved.percent_off == REFERRAL_PROGRAM["referred_percent_off"]
    assert resolved.referrer_user_id == referrer


@pytest.mark.asyncio
async def test_resolve_code_accepts_lowercase_and_padded_input():
    referrer, redeemer = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo(codes={"ROOK-FRIEND": referrer})

    resolved = await _service(repo, {referrer: _live_user(referrer)}).resolve_code(
        code="  rook-friend  ", redeemer_user_id=redeemer, interval="monthly"
    )

    assert resolved.valid is True
    assert resolved.referrer_user_id == referrer


@pytest.mark.asyncio
async def test_resolve_code_rejects_the_welcome_code_on_a_season_plan():
    """The interval gate runs before the kind split, so BOTH discount kinds
    refuse a season purchase."""
    redeemer = uuid.uuid4()
    service = _welcome_service(_FakeReferralRepo(), redeemer)

    resolved = await service.resolve_code(
        code=service.welcome_code_for(redeemer),
        redeemer_user_id=redeemer,
        interval="season",
    )

    assert resolved.valid is False
    assert "monthly" in resolved.message.lower()


# ── reservations ────────────────────────────────────────────────────────

def _resolved_referral(referrer_user_id):
    from backend.services.referral_service import ResolvedCode

    return ResolvedCode(
        valid=True,
        kind=KIND_REFERRAL,
        percent_off=REFERRAL_PROGRAM["referred_percent_off"],
        referrer_user_id=referrer_user_id,
        message="",
    )


@pytest.mark.asyncio
async def test_reserve_for_checkout_writes_a_pending_row_and_commits():
    """A reservation nobody else can see is not a reservation: the concurrent
    request runs in its own session and would block on the unique index until
    this one commits — across our Stripe call."""
    referrer, redeemer = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo()

    reservation_id = await _service(repo).reserve_for_checkout(
        resolved=_resolved_referral(referrer),
        redeemer_user_id=redeemer,
        code="ROOK-FRIEND",
    )

    assert reservation_id is not None
    assert repo.commits == 1
    written = repo.reserved[0]
    assert written["redeemer_user_id"] == redeemer
    assert written["referrer_user_id"] == referrer
    assert written["percent_off"] == REFERRAL_PROGRAM["referred_percent_off"]
    # The real Stripe session does not exist yet — that is the whole point.
    assert not written["stripe_session_id"].startswith("cs_")


@pytest.mark.asyncio
async def test_a_second_concurrent_checkout_is_refused():
    """Two tabs, one discount. The first reservation takes the account's
    one-per-kind slot and the second loses at the database."""
    referrer, redeemer = uuid.uuid4(), uuid.uuid4()
    repo = _FakeReferralRepo()
    service = _service(repo)
    resolved = _resolved_referral(referrer)

    first = await service.reserve_for_checkout(
        resolved=resolved, redeemer_user_id=redeemer, code="ROOK-FRIEND"
    )
    second = await service.reserve_for_checkout(
        resolved=resolved, redeemer_user_id=redeemer, code="ROOK-FRIEND"
    )

    assert first is not None
    assert second is None
    assert repo.commits == 1  # nothing was written the second time


@pytest.mark.asyncio
async def test_attach_and_release_commit():
    repo = _FakeReferralRepo()
    service = _service(repo)
    reservation_id = uuid.uuid4()

    await service.attach_checkout_session(reservation_id, "cs_test_1")
    await service.release_reservation(reservation_id)

    assert repo.attached == [(reservation_id, "cs_test_1")]
    assert repo.released == [reservation_id]
    assert repo.commits == 2


# ── referrer_state ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_referrer_state_reports_the_earned_rate():
    user_id = uuid.uuid4()
    repo = _FakeReferralRepo(codes={"ROOK-MINE01": user_id}, confirmed_count=2)

    state = await _service(repo).referrer_state(user_id)

    per = REFERRAL_PROGRAM["referrer_percent_off_per_referral"]
    assert state["code"] == "ROOK-MINE01"
    assert state["referral_count"] == 2
    assert state["percent_off"] == per * 2
    assert state["percent_off_cap"] == REFERRAL_PROGRAM["referrer_percent_off_cap"]
    assert state["percent_off_per_referral"] == per


@pytest.mark.asyncio
async def test_referrer_state_holds_at_the_cap():
    """Referral five earns the cap; referrals six and seven add nothing."""
    per = REFERRAL_PROGRAM["referrer_percent_off_per_referral"]
    cap = REFERRAL_PROGRAM["referrer_percent_off_cap"]
    at_cap = cap // per

    for count in (at_cap, at_cap + 1, at_cap + 5):
        user_id = uuid.uuid4()
        repo = _FakeReferralRepo(
            codes={f"ROOK-CAP{count:03d}": user_id}, confirmed_count=count
        )
        state = await _service(repo).referrer_state(user_id)
        assert state["percent_off"] == cap


def test_referrer_percent_off_matches_the_program_table():
    per = REFERRAL_PROGRAM["referrer_percent_off_per_referral"]
    cap = REFERRAL_PROGRAM["referrer_percent_off_cap"]

    assert referrer_percent_off(0) == 0
    assert referrer_percent_off(1) == per
    assert referrer_percent_off(cap // per) == cap
    assert referrer_percent_off(cap // per + 3) == cap
    assert referrer_percent_off(-2) == 0
