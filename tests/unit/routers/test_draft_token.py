"""Tests for draft token and extension relay endpoints.

CHANGED (session-isolation refactor): the /draft/event and /draft/start tests no
longer patch the removed module globals _engine/_state/_build_engine. They now
patch the per-user `session_manager` (get_or_rehydrate/create/persist/get_warm)
and assert events route to THAT user's session and broadcast via
`ws_manager.broadcast_to_session(<user.id>, ...)` (was the global broadcast()).
/draft/start now requires auth (Depends(get_current_user)). The account
draft-token, invalid-token, and sync-platform tests are unchanged.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


def _make_user(uid=None, draft_token=None):
    user = MagicMock(spec=User)
    user.id = uid or uuid.uuid4()
    user.external_id = "clerk-test"
    user.email = "test@test.com"
    user.tier = "standard"
    user.draft_token = draft_token
    user.credits_remaining = 50
    return user


def _fake_session():
    """A per-user session stand-in: an async engine + a state mock."""
    return SimpleNamespace(engine=AsyncMock(), state=MagicMock())


def _fake_manager(*, session=None, created=None, warm=None, resumable=False):
    mgr = MagicMock()
    mgr.get_or_rehydrate = AsyncMock(return_value=session)
    mgr.create = AsyncMock(return_value=created if created is not None else session)
    mgr.persist = AsyncMock()
    mgr.end = AsyncMock()
    mgr.get_warm = MagicMock(return_value=warm)
    # /draft/start short-circuits on RESUMABILITY (recent active draft), not warm
    # presence — so the start tests drive this flag.
    mgr.is_resumable = AsyncMock(return_value=resumable)
    return mgr


def _mock_user_repo(user):
    repo = AsyncMock()
    repo.get_by_draft_token.return_value = user
    return repo


# ---------------------------------------------------------------------------
# Draft token endpoints (in /account) — unchanged by the session refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_token_created_for_new_user():
    """GET /account/draft-token creates a token when user has none."""
    user = _make_user(draft_token=None)
    db_user = _make_user(draft_token=None)
    db_user.id = user.id
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=db_user)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/account/draft-token")
        assert resp.status_code == 200
        token = resp.json()["draft_token"]
        assert token is not None
        assert db_user.draft_token == token
        mock_db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_token_stable_on_repeat_call():
    """GET /account/draft-token returns existing token without regenerating."""
    existing_token = str(uuid.uuid4())
    user = _make_user(draft_token=existing_token)
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/account/draft-token")
        assert resp.status_code == 200
        assert resp.json()["draft_token"] == existing_token
        mock_db.commit.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_revoke_generates_new_token():
    """POST /account/draft-token/revoke generates a new token."""
    old_token = str(uuid.uuid4())
    user = _make_user(draft_token=old_token)
    db_user = _make_user(draft_token=old_token)
    db_user.id = user.id
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=db_user)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/api/account/draft-token/revoke")
        assert resp.status_code == 200
        new_token = resp.json()["draft_token"]
        assert new_token != old_token
        assert db_user.draft_token == new_token
        mock_db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Draft event relay — now session-keyed (per user)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_event_rejects_invalid_token():
    """POST /draft/event with invalid token returns 401."""
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo:
        MockRepo.return_value = _mock_user_repo(None)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "nomination", "platform": "yahoo", "payload": {}},
                    headers={"X-Draft-Token": "invalid-token"},
                )
            assert resp.status_code == 401
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_event_relays_to_session_ws():
    """A valid nomination routes to the user's session and broadcasts ONLY to
    that user's session key (was a global broadcast)."""
    user = _make_user(draft_token="valid-token")
    session = _fake_session()
    mgr = _fake_manager(session=session)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._resolve_player", AsyncMock(return_value=None)
    ):
        MockRepo.return_value = _mock_user_repo(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "nomination", "platform": "yahoo", "payload": {"player": "CMC"}},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            assert resp.json()["status"] == "relayed"
            mgr.get_or_rehydrate.assert_awaited_once_with(user.id)
            session.engine.on_nomination.assert_awaited_once()
            mgr.persist.assert_awaited_once_with(user.id)
            # Broadcast went ONLY to this user's session key.
            mock_ws.broadcast_to_session.assert_awaited_once()
            key, data = mock_ws.broadcast_to_session.await_args[0]
            assert key == str(user.id)
            assert data["type"] == "nomination"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_nomination_triggers_engine():
    """A nomination resolves the player and runs the user's engine."""
    user = _make_user(draft_token="valid-token")
    session = _fake_session()
    mgr = _fake_manager(session=session)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    fake_player = MagicMock(yahoo_player_id="nfl_123", position="TE")
    # `name` is a reserved MagicMock kwarg, so set the resolved player's display
    # name explicitly — _trigger_nomination now backfills the broadcast from it.
    fake_player.name = "Sam LaPorta"
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._resolve_player", AsyncMock(return_value=fake_player)
    ):
        MockRepo.return_value = _mock_user_repo(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "nomination", "platform": "yahoo",
                          "payload": {"player_name": "Sam LaPorta", "opening_bid": 4}},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            session.engine.on_nomination.assert_awaited_once()
            sent = session.engine.on_nomination.call_args[0][0]
            assert sent["player_id"] == "nfl_123"
            assert sent["player_name"] == "Sam LaPorta"
            raw = mock_ws.broadcast_to_session.await_args[0][1]
            assert raw["type"] == "nomination"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_bid_update_relayed_without_engine_call():
    """bid_update relays to the UI but never invokes engine handlers (and never
    even resolves a session)."""
    user = _make_user(draft_token="valid-token")
    session = _fake_session()
    mgr = _fake_manager(session=session)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr):
        MockRepo.return_value = _mock_user_repo(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "bid_update", "platform": "yahoo",
                          "payload": {"player_name": "Sam LaPorta", "current_bid": 6}},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            session.engine.on_nomination.assert_not_awaited()
            session.engine.on_pick_confirmed.assert_not_awaited()
            mgr.get_or_rehydrate.assert_not_awaited()  # bid_update only relays
            raw = mock_ws.broadcast_to_session.await_args[0][1]
            assert raw["type"] == "bid_update"
            assert raw["payload"]["current_bid"] == 6
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_pick_recorded():
    """draft_pick records into the user's session state and relays raw."""
    user = _make_user(draft_token="valid-token")
    session = _fake_session()
    session.state.is_my_winning_bid.return_value = False
    mgr = _fake_manager(session=session)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    fake_player = MagicMock(yahoo_player_id="nfl_9", position="RB")
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._resolve_player", AsyncMock(return_value=fake_player)
    ):
        MockRepo.return_value = _mock_user_repo(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "draft_pick", "platform": "yahoo",
                          "payload": {"player_name": "Bijan Robinson", "final_price": 20,
                                      "winner": "Stephen", "teams_snapshot": {}}},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            session.engine.on_pick_confirmed.assert_awaited_once()
            sent = session.engine.on_pick_confirmed.call_args[0][0]
            assert sent["player_id"] == "nfl_9"
            assert sent["team_id"] == "Stephen"
            assert sent["final_price"] == 20
            mgr.persist.assert_awaited_once_with(user.id)
            raw = mock_ws.broadcast_to_session.await_args[0][1]
            assert raw["type"] == "draft_pick"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_nomination_lazy_creates_session_when_none():
    """A nomination with no existing session lazily CREATES one (replacing the
    old default-engine band-aid), then runs it."""
    user = _make_user(draft_token="valid-token")
    created = _fake_session()
    mgr = _fake_manager(session=None, created=created)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._build_state", AsyncMock(return_value="STATE")
    ), patch("backend.routers.draft._resolve_player", AsyncMock(return_value=None)):
        MockRepo.return_value = _mock_user_repo(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "nomination", "platform": "yahoo",
                          "payload": {"player_name": "Sam LaPorta"}},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            mgr.create.assert_awaited_once()  # lazily created for THIS user
            created.engine.on_nomination.assert_awaited_once()
            mgr.persist.assert_awaited_once_with(user.id)
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_nomination_uses_existing_session_no_create():
    """A nomination with an existing session does NOT create a new one."""
    user = _make_user(draft_token="valid-token")
    session = _fake_session()
    mgr = _fake_manager(session=session)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._resolve_player", AsyncMock(return_value=None)
    ):
        MockRepo.return_value = _mock_user_repo(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "nomination", "platform": "yahoo",
                          "payload": {"player_name": "Sam LaPorta"}},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            mgr.create.assert_not_awaited()
            session.engine.on_nomination.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_pick_no_session_relays_only():
    """A draft_pick with no session relays raw but never records (no lazy create)."""
    user = _make_user(draft_token="valid-token")
    mgr = _fake_manager(session=None)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._record_pick", AsyncMock()
    ) as rec:
        MockRepo.return_value = _mock_user_repo(user)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "draft_pick", "platform": "yahoo",
                          "payload": {"player_name": "X", "final_price": 5}},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            mgr.create.assert_not_awaited()
            rec.assert_not_awaited()
            mock_ws.broadcast_to_session.assert_awaited()
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Draft engine lifecycle — /start now authed + per-user session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_draft_creates_session_for_user():
    """POST /draft/start (authed) creates THIS user's session via the manager."""
    user = _make_user()
    mgr = _fake_manager(warm=None)
    from backend.core.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    build = AsyncMock(return_value="STATE")

    with patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._bridge", None
    ), patch("backend.routers.draft._build_state", build):
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post("/api/draft/start", json={"your_team_id": "team_5"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["mode"] == "extension"
            build.assert_awaited_once_with("team_5", None, None)
            mgr.create.assert_awaited_once()
            assert mgr.create.await_args[0][0] == user.id
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_start_draft_idempotent_when_session_resumable():
    """CHANGED: /draft/start short-circuits on RESUMABILITY (a recent active
    draft), not warm presence. A resumable session is NOT recreated (don't wipe a
    live draft); a stale-but-warm one IS recreated (see the regression test)."""
    user = _make_user()
    mgr = _fake_manager(resumable=True, warm=_fake_session())
    from backend.core.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user

    with patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._build_state", AsyncMock()
    ) as build:
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post("/api/draft/start", json={"your_team_id": "team_5"})
            assert resp.status_code == 200
            assert resp.json()["status"] == "ready"
            mgr.create.assert_not_awaited()
            build.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_start_recreates_stale_but_warm_session_then_state_200():
    """PROD REGRESSION (#100/#101): a just-abandoned mock's warm session lingers
    in memory while its DB row goes stale (past the resume window). Pressing Start
    must CREATE a fresh draft — NOT short-circuit on the stale warm session and
    then 409 on /state ("engine not started").

    Uses the REAL manager + in-memory store with a backdated updated_at (the real
    runtime path the prior /start mocks didn't exercise — keying on warm presence
    vs DB recency). FAILS before the fix (short-circuit → /state 409), passes after
    (create fresh → /state 200).
    """
    from backend.core.dependencies import get_current_user
    from backend.engines.draft_state_manager import DraftStateManager, LeagueConfig
    from backend.services.draft_session import DraftSessionManager, InMemorySessionStore

    user = _make_user()
    store = InMemorySessionStore()

    async def factory(state, key):
        return AsyncMock()

    mgr = DraftSessionManager(store, factory)
    # Prior abandoned draft: still WARM in memory, but its DB row is stale.
    await mgr.create(user.id, DraftStateManager(LeagueConfig(auction_budget=200), "old_team"))
    store._records[user.id]["updated_at"] = (
        store._records[user.id]["updated_at"].replace(year=2000)
    )
    assert mgr.get_warm(user.id) is not None              # warm session lingers
    assert await mgr.is_resumable(user.id, 3600) is False  # but stale → not resumable

    app.dependency_overrides[get_current_user] = lambda: user
    with patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._bridge", None
    ):
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                start = await ac.post("/api/draft/start", json={"your_team_id": "team_new"})
                assert start.status_code == 200
                assert start.json()["status"] == "ready"
                # The fresh draft is now resumable → /state 200, not the 409 regression.
                st = await ac.get("/api/draft/state")
                assert st.status_code == 200
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Recency gate on the READ path: finished/abandoned drafts must not resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_409_when_not_resumable_and_does_not_warm_the_session():
    """A stale/abandoned draft (is_resumable False) → /state 409, and the gate is
    checked BEFORE get_or_rehydrate so a read can't keep a dead session warm."""
    user = _make_user()
    mgr = MagicMock()
    mgr.is_resumable = AsyncMock(return_value=False)
    mgr.get_or_rehydrate = AsyncMock()
    from backend.core.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    with patch("backend.routers.draft.session_manager", mgr):
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get("/api/draft/state")
            assert resp.status_code == 409
            mgr.is_resumable.assert_awaited_once()
            mgr.get_or_rehydrate.assert_not_awaited()  # gated before rehydrate
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_state_returns_snake_roster_from_my_picks():
    """A snake draft's own picks live in _my_picks, not your_roster (the auction
    roster, which stays empty on snake). /state must source from get_my_roster() so
    a page refresh restores your picks instead of an empty roster."""
    from backend.core.dependencies import get_current_user
    from backend.engines.draft_state_manager import DraftStateManager, LeagueConfig
    from backend.services.draft_session import DraftSessionManager, InMemorySessionStore

    user = _make_user()
    store = InMemorySessionStore()

    async def factory(state, key):
        return AsyncMock()

    mgr = DraftSessionManager(store, factory)
    state = DraftStateManager(LeagueConfig(draft_type="snake"), "my_team")
    state.record_snake_pick(
        player_name="Bijan Robinson", position="RB", pick_number=4, round_num=1, is_yours=True
    )
    await mgr.create(user.id, state)  # fresh → resumable

    app.dependency_overrides[get_current_user] = lambda: user
    with patch("backend.routers.draft.session_manager", mgr):
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get("/api/draft/state")
            assert resp.status_code == 200
            roster = resp.json()["your_roster"]
            assert [p["player_name"] for p in roster] == ["Bijan Robinson"]
            assert roster[0]["position"] == "RB"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_state_then_end_then_state_409_real_manager():
    """End-to-end through the real manager + in-memory store: an active draft
    serves /state 200; after POST /draft/end it 409s on re-entry (immediate)."""
    from backend.core.dependencies import get_current_user
    from backend.engines.draft_state_manager import DraftStateManager, LeagueConfig
    from backend.services.draft_session import DraftSessionManager, InMemorySessionStore

    user = _make_user()

    async def factory(state, key):
        return AsyncMock()

    mgr = DraftSessionManager(InMemorySessionStore(), factory)
    await mgr.create(user.id, DraftStateManager(LeagueConfig(auction_budget=200), "team"))

    app.dependency_overrides[get_current_user] = lambda: user
    with patch("backend.routers.draft.session_manager", mgr):
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                # Active + recent → resumable → 200 with real budget/roster.
                live = await ac.get("/api/draft/state")
                assert live.status_code == 200
                assert live.json()["your_remaining_budget"] == 200

                # End Draft → is_active=False.
                ended = await ac.post("/api/draft/end")
                assert ended.status_code == 200
                assert ended.json()["status"] == "ended"

                # Re-entry now shows the board (409), not the finished draft.
                after = await ac.get("/api/draft/state")
                assert after.status_code == 409
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Passive sync — unchanged by the session refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_platform_skips_sleeper():
    """POST /leagues/sync-platform/sleeper returns skipped."""
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/api/leagues/sync-platform/sleeper", headers={"X-Draft-Token": "any"}
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"
        assert resp.json()["reason"] == "platform_excluded"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_platform_skips_invalid_token():
    """POST /leagues/sync-platform/yahoo with bad token returns 200 + skipped."""
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    with patch("backend.repositories.user_repo.UserRepository") as MockRepo:
        MockRepo.return_value = _mock_user_repo(None)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/leagues/sync-platform/yahoo", headers={"X-Draft-Token": "invalid"}
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "skipped"
            assert data["reason"] == "invalid_token"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_platform_skips_no_leagues():
    """POST /leagues/sync-platform/espn with valid token but no leagues skips."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    with patch("backend.repositories.user_repo.UserRepository") as MockUserRepo, patch(
        "backend.routers.league_connect.LeagueRepository"
    ) as MockLeagueRepo:
        MockUserRepo.return_value = _mock_user_repo(user)
        mock_league_repo = AsyncMock()
        mock_league_repo.get_user_leagues_by_platform.return_value = []
        MockLeagueRepo.return_value = mock_league_repo
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/leagues/sync-platform/espn", headers={"X-Draft-Token": "valid-token"}
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "skipped"
            assert data["reason"] == "no_leagues"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_platform_syncs_all_user_leagues():
    """POST /leagues/sync-platform/yahoo syncs all user's Yahoo leagues."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_league = MagicMock()
    mock_league.id = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    with patch("backend.repositories.user_repo.UserRepository") as MockUserRepo, patch(
        "backend.routers.league_connect.LeagueRepository"
    ) as MockLeagueRepo, patch(
        "backend.services.league_sync.LeagueSyncService"
    ) as MockSync:
        MockUserRepo.return_value = _mock_user_repo(user)
        mock_league_repo = AsyncMock()
        mock_league_repo.get_user_leagues_by_platform.return_value = [mock_league]
        MockLeagueRepo.return_value = mock_league_repo
        mock_sync = AsyncMock()
        MockSync.return_value = mock_sync
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/leagues/sync-platform/yahoo", headers={"X-Draft-Token": "valid-token"}
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["leagues_synced"] == 1
            mock_sync.sync_league.assert_called_once_with(mock_league.id)
        finally:
            app.dependency_overrides.clear()
