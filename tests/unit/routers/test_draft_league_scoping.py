"""_build_state must never read a league it cannot prove the caller owns.

Regression test for an IDOR. `_build_state` called the INHERITED
`LeagueRepository.get()`, which is a bare `session.get(model, id)` with no ownership
predicate (backend/repositories/base.py) — while the scoped sibling
`get_user_league()` sits ten lines away in the same class, carrying the comment
"user_id check = row-level security".

So POST /draft/start with another user's league_id returned THEIR budget,
team_count, roster_slots, draft_type and scoring, which built the caller's draft
config and surfaced through GET /draft/state. The ownership check at the call site
did not stop it: for a foreign league `get_user_league` returns None, which only
skips the suspended-league branch and falls straight through to `_build_state`.

Reachable by any authenticated user — not admin-gated.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.routers.draft import _build_state

OWNER = uuid.uuid4()
ATTACKER = uuid.uuid4()
LEAGUE_ID = uuid.uuid4()


def _repo_capturing(calls: list, *, owned_by: uuid.UUID):
    """A LeagueRepository whose scoped lookup honours ownership, and whose UNSCOPED
    lookup records any use — calling it at all is the bug."""
    repo = MagicMock()

    async def get_user_league(user_id, league_id):
        calls.append(("get_user_league", user_id, league_id))
        if user_id != owned_by:
            return None  # what the real scoped query does for a foreign league
        league = MagicMock()
        league.budget = 200
        league.team_count = 12
        league.draft_type = "auction"
        league.scoring = "ppr"
        league.roster_slots = None
        return league

    async def get(league_id):
        calls.append(("get", league_id))
        raise AssertionError(
            "_build_state used the UNSCOPED LeagueRepository.get() — this is the IDOR. "
            "Use get_user_league(user_id, league_id)."
        )

    repo.get_user_league = AsyncMock(side_effect=get_user_league)
    repo.get = AsyncMock(side_effect=get)
    return repo


@pytest.mark.asyncio
async def test_foreign_league_is_not_read_into_the_callers_config():
    calls: list = []
    repo = _repo_capturing(calls, owned_by=OWNER)

    with patch("backend.repositories.league_repo.LeagueRepository", return_value=repo), \
            patch("backend.routers.draft.AsyncSessionLocal", MagicMock()):
        state = await _build_state(str(LEAGUE_ID), None, user_id=ATTACKER)

    # The scoped method was used...
    assert ("get_user_league", ATTACKER, LEAGUE_ID) in calls
    # ...and the unscoped one never was.
    assert not any(c[0] == "get" for c in calls)
    # Falling back to defaults is the correct outcome, not the owner's settings.
    assert state is not None


@pytest.mark.asyncio
async def test_the_owner_still_gets_their_own_league_config():
    """The fix must not break the legitimate path."""
    calls: list = []
    repo = _repo_capturing(calls, owned_by=OWNER)

    with patch("backend.repositories.league_repo.LeagueRepository", return_value=repo), \
            patch("backend.routers.draft.AsyncSessionLocal", MagicMock()):
        state = await _build_state(str(LEAGUE_ID), None, user_id=OWNER)

    assert ("get_user_league", OWNER, LEAGUE_ID) in calls
    assert state is not None


@pytest.mark.asyncio
async def test_no_user_id_refuses_to_read_rather_than_reading_unscoped():
    """Fail safe. A caller that cannot prove identity gets defaults, not a lookup —
    otherwise a future call site could silently reintroduce the unscoped read.
    """
    calls: list = []
    repo = _repo_capturing(calls, owned_by=OWNER)

    with patch("backend.repositories.league_repo.LeagueRepository", return_value=repo), \
            patch("backend.routers.draft.AsyncSessionLocal", MagicMock()):
        state = await _build_state(str(LEAGUE_ID), None)

    assert calls == []          # neither method was called
    assert state is not None    # and we still built a usable default board


@pytest.mark.asyncio
async def test_start_passes_the_authenticated_user_through():
    """Pins the wiring: POST /draft/start must hand its own user id to _build_state.
    Without this, the scoped signature exists but the call site never uses it.
    """
    import inspect

    from backend.routers import draft as draft_mod

    src = inspect.getsource(draft_mod)
    assert "_build_state(req.league_id, req.draft_type, user_id=user.id)" in src, (
        "POST /draft/start must pass user_id to _build_state, or the league read is "
        "unscoped again."
    )
