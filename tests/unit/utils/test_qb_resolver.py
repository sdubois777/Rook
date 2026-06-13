"""Tests for the QB path in backend/utils/player_matching.py.

QBs resolve from the seasonal_stats frame by gsis_id + position guard,
not from target_share (which has no QBs) and not by name (surname
collisions: Allen → Josh Allen QB, never Braelon Allen RB).
"""
from __future__ import annotations

import types

import pandas as pd

from backend.utils.player_matching import resolve_player_season_stats


def _seasonal_warehouse(frame: pd.DataFrame):
    return types.SimpleNamespace(
        get_seasonal_stats=lambda s: frame,
        # target_share must never be consulted for a QB
        get_target_share=lambda s: (_ for _ in ()).throw(
            AssertionError("QB path must not read target_share")
        ),
    )


def _qb(gsis_id):
    return types.SimpleNamespace(
        name="Josh Allen", team_abbr="BUF", position="QB",
        gsis_id=gsis_id, sleeper_id="4984", sportradar_id="sr-allen",
    )


# Frame with two "Allen" players: Josh (QB) and Braelon (RB).
_ALLEN_FRAME = pd.DataFrame([
    {"player_id": "00-0034857", "player_name": "J.Allen", "position": "QB",
     "games": 16, "fantasy_points_ppr": 320.0},
    {"player_id": "00-0039210", "player_name": "B.Allen", "position": "RB",
     "games": 17, "fantasy_points_ppr": 140.0},
])


def test_qb_resolver_uses_gsis_id_not_name():
    """Resolution keys on gsis_id, so it returns the row for that exact id."""
    wh = _seasonal_warehouse(_ALLEN_FRAME)
    stats = resolve_player_season_stats(_qb("00-0034857"), 2024, wh)
    assert stats is not None
    assert stats["games"] == 16          # Josh Allen QB, not Braelon's 17
    assert stats["position"] == "QB"


def test_qb_resolver_position_verified():
    """A gsis that (hypothetically) matched an RB row is rejected by the guard."""
    wh = _seasonal_warehouse(_ALLEN_FRAME)
    # Point the QB's gsis at the RB row's id — position guard must reject it.
    stats = resolve_player_season_stats(_qb("00-0039210"), 2024, wh)
    assert stats is None


def test_murray_resolves_to_qb_not_rb():
    """A frame with both a QB Murray and an RB Murray returns the QB."""
    frame = pd.DataFrame([
        {"player_id": "00-0035228", "player_name": "K.Murray", "position": "QB",
         "games": 5, "fantasy_points_ppr": 110.0},
        {"player_id": "00-0031234", "player_name": "L.Murray", "position": "RB",
         "games": 15, "fantasy_points_ppr": 130.0},
    ])
    wh = _seasonal_warehouse(frame)
    murray = types.SimpleNamespace(
        name="Kyler Murray", team_abbr="ARI", position="QB",
        gsis_id="00-0035228", sleeper_id="5849", sportradar_id=None,
    )
    stats = resolve_player_season_stats(murray, 2025, wh)
    assert stats is not None
    assert stats["position"] == "QB"
    assert stats["games"] == 5


def test_qb_resolver_strips_whitespace_gsis():
    """A leading-space gsis (sync_rosters can reintroduce it) still matches."""
    wh = _seasonal_warehouse(_ALLEN_FRAME)
    stats = resolve_player_season_stats(_qb(" 00-0034857"), 2024, wh)
    assert stats is not None
    assert stats["games"] == 16


def test_qb_resolver_none_when_no_gsis():
    wh = _seasonal_warehouse(_ALLEN_FRAME)
    assert resolve_player_season_stats(_qb(None), 2024, wh) is None


def test_qb_resolver_none_when_zero_games():
    frame = pd.DataFrame([
        {"player_id": "00-0034857", "player_name": "J.Allen", "position": "QB",
         "games": 0, "fantasy_points_ppr": 0.0},
    ])
    wh = _seasonal_warehouse(frame)
    assert resolve_player_season_stats(_qb("00-0034857"), 2024, wh) is None
