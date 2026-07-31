"""opponent_profiles must never cross a tenant boundary.

The table had NO user column at all while being read on every user's live-draft
path (`load_manager_tendencies` -> `OpponentThreatAnalyzer`, backend/routers/draft.py),
so one league's manager tendencies biased threat scoring for every user. Worse,
`build_manager_profiles` deleted every row for the analysis year regardless of
owner — destruction, not just disclosure.

These tests fail against the pre-fix code, which is the only thing that makes them
worth having.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.engines.league_auction import load_manager_tendencies
from backend.models.draft_state import OpponentProfile

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
LEAGUE_A = uuid.uuid4()
LEAGUE_B = uuid.uuid4()


def _profile(user_id, league_id, team, scores):
    return OpponentProfile(
        user_id=user_id,
        user_league_id=league_id,
        season_year=2026,
        yahoo_team_id=team,
        team_name=f"manager-of-{team}",
        positional_scores=scores,
    )


def _session_returning(rows):
    """A session whose execute() returns `rows`, capturing the statement so the
    test can assert the tenant predicate is actually in the SQL."""
    session = MagicMock()
    captured: list = []

    async def execute(stmt, *a, **kw):
        captured.append(stmt)
        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = rows
        result.scalars.return_value = scalars
        return result

    session.execute = AsyncMock(side_effect=execute)
    session.captured = captured
    return session


@pytest.mark.asyncio
async def test_only_the_callers_own_profiles_are_loaded():
    """The real filtering happens in SQL, so assert the predicate is compiled in —
    a mock that returns rows regardless would pass even unscoped code."""
    rows = [_profile(USER_A, LEAGUE_A, "461.l.1.t.1", {"RB": 0.5, "WR": 0.5})]
    session = _session_returning(rows)

    await load_manager_tendencies(session, USER_A)

    stmt = session.captured[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "user_id" in compiled, (
        "load_manager_tendencies issued a query with no user_id predicate — "
        "this is the cross-tenant read."
    )
    # Postgres renders a bound UUID without hyphens under literal_binds.
    assert USER_A.hex in compiled.replace("-", "")


@pytest.mark.asyncio
async def test_user_id_is_required_not_defaulted():
    """A default of None would let a caller silently reintroduce the unscoped read."""
    import inspect

    sig = inspect.signature(load_manager_tendencies)
    param = sig.parameters["user_id"]
    assert param.default is inspect.Parameter.empty, (
        "user_id must have no default — an optional tenant key is how this bug "
        "comes back."
    )


@pytest.mark.asyncio
async def test_tendencies_still_computed_for_the_owner():
    """The fix must not break the legitimate path: two of the owner's own managers
    still produce relative positional bias."""
    rows = [
        _profile(USER_A, LEAGUE_A, "461.l.1.t.1", {"RB": 0.8, "WR": 0.2}),
        _profile(USER_A, LEAGUE_A, "461.l.1.t.2", {"RB": 0.2, "WR": 0.8}),
    ]
    session = _session_returning(rows)

    out = await load_manager_tendencies(session, USER_A)

    assert set(out) == {"461.l.1.t.1", "461.l.1.t.2"}
    # t.1 spends 0.8 on RB against a league average of 0.5 → bias > 1
    assert out["461.l.1.t.1"]["positional_bias"]["RB"] > 1.0
    assert out["461.l.1.t.2"]["positional_bias"]["RB"] < 1.0


def test_build_manager_profiles_requires_an_owner():
    """Signature guard. build_manager_profiles has no callers today; scoping it is
    containment so that wiring it up cannot recreate the leak."""
    import inspect

    from backend.engines.league_auction import build_manager_profiles

    sig = inspect.signature(build_manager_profiles)
    for name in ("user_id", "user_league_id"):
        assert name in sig.parameters, f"build_manager_profiles must take {name}"
        assert sig.parameters[name].default is inspect.Parameter.empty, (
            f"{name} must be required — an optional tenant key is how this bug "
            "comes back."
        )


def test_the_year_wide_delete_is_league_scoped():
    """The DELETE used to wipe EVERY tenant's profiles for the analysis year. That
    is data destruction across tenants, and it is the worst thing in this function.
    """
    import inspect

    from backend.engines import league_auction

    src = inspect.getsource(league_auction.build_manager_profiles)
    delete_idx = src.index("delete(OpponentProfile)")
    window = src[delete_idx:delete_idx + 300]
    assert "user_league_id" in window, (
        "delete(OpponentProfile) must be scoped by user_league_id, or a rebuild "
        "destroys every other tenant's profiles."
    )


def test_opponent_profiles_is_not_copied_dev_to_prod():
    """migrate_dev_to_prod copies board tables wholesale. opponent_profiles is
    per-tenant state carrying real manager names and dev-only user ids — copying it
    would overwrite prod tenancy and violate the new FKs."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "scripts" / "migrate_dev_to_prod.py"
    text = src.read_text(encoding="utf-8")
    board_line = text[text.index("BOARD = ["):text.index("BOARD = [") + 600]
    assert '"opponent_profiles"' not in board_line
    assert '"draft_state"' not in board_line
