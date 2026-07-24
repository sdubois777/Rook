"""F5/F7 — bound the relayed player_name so the `\\s+...$` suffix regexes can't
run O(N^2) on a huge whitespace name and stall the event loop.

Covers the ingress cap (relay_draft_event / _resolve_player), the defense-in-depth
caps inside _pick_key and _norm_name, and — critically — that NORMAL names still
normalize identically (no behavior change).
"""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.routers.draft import MAX_PLAYER_NAME_LEN, _bounded_name
from backend.engines.draft_state_manager import _pick_key
from backend.agents.roster_changes import _norm_name


# ---------------------------------------------------------------------------
# Unit: the boundary helper + the two in-function caps
# ---------------------------------------------------------------------------

def test_bounded_name_truncates_over_cap():
    assert len(_bounded_name(" " * 500_000)) == MAX_PLAYER_NAME_LEN


def test_bounded_name_passes_through_normal_and_non_str():
    assert _bounded_name("Bijan Robinson") == "Bijan Robinson"  # unchanged
    assert _bounded_name("") == ""
    assert _bounded_name(None) is None
    assert _bounded_name(12345) == 12345  # non-str passes through


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Bijan Robinson", "bijan robinson"),
        ("Michael Pittman Jr.", "michael pittman"),   # suffix stripped
        ("Amon-Ra St. Brown", "amon ra st brown"),    # hyphen→space, dots dropped
        ("D.J. Moore", "dj moore"),
    ],
)
def test_pick_key_behavior_unchanged_for_normal_names(raw, expected):
    """The 100-char cap must not change normalization of real names."""
    assert _pick_key(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Bijan Robinson", "bijan robinson"),
        ("Michael Pittman Jr.", "michael pittman"),
        ("Patrick Mahomes II", "patrick mahomes"),
    ],
)
def test_norm_name_behavior_unchanged_for_normal_names(raw, expected):
    assert _norm_name(raw) == expected


def test_pick_key_and_norm_name_fast_on_huge_whitespace():
    """The O(N^2) suffix regex must be defused by the cap — a 500k whitespace
    string returns effectively instantly instead of spending N^2 CPU."""
    huge = " " * 500_000
    t0 = time.monotonic()
    k = _pick_key(huge)
    n = _norm_name(huge)
    elapsed = time.monotonic() - t0
    # Capped path is microseconds; the unbounded O(N^2) path would be many
    # seconds. A generous 2s ceiling cleanly separates the two.
    assert elapsed < 2.0
    assert k == ""            # all-whitespace normalizes to empty
    assert n == ""


# ---------------------------------------------------------------------------
# Integration: /draft/event caps the name before it reaches the resolver
# ---------------------------------------------------------------------------

def _paid_user():
    u = MagicMock()
    u.id = uuid.uuid4()
    u.tier = "standard"          # live_draft entitlement (post-F4 gate)
    u.tier_expires_at = None
    return u


def _mock_user_repo(user):
    repo = AsyncMock()
    repo.get_by_draft_token.return_value = user
    return repo


def _fake_manager(session):
    mgr = MagicMock()
    mgr.get_or_rehydrate = AsyncMock(return_value=session)
    mgr.create = AsyncMock(return_value=session)
    mgr.persist = AsyncMock()
    return mgr


@pytest.mark.asyncio
async def test_draft_event_caps_huge_name_before_resolver_and_returns_fast():
    """A 500k-char whitespace player_name on /draft/event resolves quickly to a
    no-match: the resolver is handed a <=100 char name and the broadcast payload
    is truncated — proving the ingress cap ran before any regex."""
    user = _paid_user()
    session = SimpleNamespace(engine=AsyncMock(), state=MagicMock(draft_type="auction"))
    mgr = _fake_manager(session)
    from backend.core.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: AsyncMock()
    mock_ws = MagicMock()
    mock_ws.broadcast_to_session = AsyncMock()
    # Spy on the resolver: record the name it receives, return no match.
    resolver_spy = AsyncMock(return_value=None)

    huge = " " * 500_000
    with patch("backend.repositories.user_repo.UserRepository") as MockRepo, patch(
        "backend.routers.draft.ws_manager", mock_ws
    ), patch("backend.routers.draft.session_manager", mgr), patch(
        "backend.routers.draft._resolve_player", resolver_spy
    ):
        MockRepo.return_value = _mock_user_repo(user)
        try:
            t0 = time.monotonic()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/draft/event",
                    json={"type": "nomination", "platform": "yahoo",
                          "payload": {"player_name": huge}},
                    headers={"X-Draft-Token": "tok"},
                )
            elapsed = time.monotonic() - t0
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert elapsed < 2.0  # capped: no O(N^2) stall
    # The resolver was handed a bounded name, not the 500k blob.
    resolver_spy.assert_awaited()
    passed_name = resolver_spy.await_args.args[0]
    assert len(passed_name) <= MAX_PLAYER_NAME_LEN
    # The broadcast nominee name is likewise bounded (no giant string over the WS).
    mock_ws.broadcast_to_session.assert_awaited_once()
    _, message = mock_ws.broadcast_to_session.await_args.args
    assert len(message["payload"]["player_name"]) <= MAX_PLAYER_NAME_LEN
