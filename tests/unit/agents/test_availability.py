"""Tests for the games-based availability model in injury_risk.py."""
from __future__ import annotations

import types

import pandas as pd
import pytest

from backend.agents.injury_risk import (
    SKILL_POSITIONS,
    build_player_availability,
    compute_availability_metrics,
)


def _history(*games_by_season):
    """Build an oldest→recent games_history from (season, games) pairs."""
    return [
        {"season": s, "games": g, "full_season": g >= 15}
        for s, g in games_by_season
    ]


# ---------------------------------------------------------------------------
# Risk thresholds
# ---------------------------------------------------------------------------

def test_availability_durable():
    """17/17/17 → durable, no discount."""
    out = compute_availability_metrics(_history((2023, 17), (2024, 17), (2025, 17)))
    assert out["availability_risk"] == "durable"
    assert out["availability_risk_modifier"] == 0.0


def test_availability_monitor():
    """14/13/15 (avg 14) → monitor, -0.05, stable trend."""
    out = compute_availability_metrics(_history((2023, 14), (2024, 13), (2025, 15)))
    assert out["availability_risk"] == "monitor"
    assert out["availability_risk_modifier"] == -0.05
    assert out["availability_trend"] == "stable"


def test_availability_concern():
    """9/11/10 (avg 10) → concern, -0.15."""
    out = compute_availability_metrics(_history((2023, 9), (2024, 11), (2025, 10)))
    assert out["availability_risk"] == "concern"
    assert out["availability_risk_modifier"] == -0.15


def test_declining_trend_extra_penalty():
    """17/14/10 → monitor base (-0.05) + declining (-0.05) = -0.10."""
    out = compute_availability_metrics(_history((2023, 17), (2024, 14), (2025, 10)))
    assert out["availability_trend"] == "declining"
    assert out["availability_risk_modifier"] == -0.10


def test_full_season_absence_flag():
    """0 or 1 games in any season flips the absence flag."""
    out = compute_availability_metrics(_history((2023, 16), (2024, 1), (2025, 17)))
    assert out["full_season_absence_flag"] is True

    clean = compute_availability_metrics(_history((2023, 16), (2024, 14), (2025, 17)))
    assert clean["full_season_absence_flag"] is False


def test_availability_unknown_short_history():
    """Fewer than 2 seasons → unknown, no penalty."""
    out = compute_availability_metrics(_history((2025, 12)))
    assert out["availability_risk"] == "unknown"
    assert out["availability_risk_modifier"] == 0.0
    assert out["projected_games"] is None


def test_projected_games_weights_recent():
    """17/14/10 → projection skews toward the recent 10, below the 13.7 mean."""
    out = compute_availability_metrics(_history((2023, 17), (2024, 14), (2025, 10)))
    # 10*0.5 + 14*0.3 + 17*0.2 = 12.6 → 13
    assert out["projected_games"] == 13
    assert out["projected_games"] < out["avg_games_per_season"]


def test_projected_games_capped_at_17():
    """A perfectly healthy history cannot project above the 17-game max."""
    out = compute_availability_metrics(_history((2023, 17), (2024, 17), (2025, 17)))
    assert out["projected_games"] == 17


# ---------------------------------------------------------------------------
# Resolver-backed builder
# ---------------------------------------------------------------------------

def _warehouse_with(frames: dict[int, pd.DataFrame]):
    return types.SimpleNamespace(get_target_share=lambda s: frames.get(s))


def _player(**kw):
    defaults = {
        "name": "Christian McCaffrey", "team_abbr": "SF", "position": "RB",
        "gsis_id": None, "sportradar_id": None, "sleeper_id": "4034",
    }
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def test_build_uses_resolver_including_current_season():
    """build_player_availability resolves every season via the shared resolver,
    including a 2025 frame that uses abbreviated names (CMC injury case)."""
    def row(name, games):
        return {"player_name": name, "recent_team": "SF", "position": "RB",
                "sleeper_id": "4034", "games": games, "total_carries": games * 15}

    frames = {
        2023: pd.DataFrame([row("Christian McCaffrey", 16)]),
        2024: pd.DataFrame([row("Christian McCaffrey", 4)]),
        2025: pd.DataFrame([row("C.McCaffrey", 17)]),  # abbreviated, ID still matches
    }
    out = build_player_availability(_player(), _warehouse_with(frames), [2023, 2024, 2025])

    assert [g["games"] for g in out["games_played_history"]] == [16, 4, 17]
    assert out["avg_games_per_season"] == pytest.approx(12.3, abs=0.05)
    assert out["availability_risk"] == "concern"  # avg 12.3 < 13


def test_build_returns_unknown_when_no_stats():
    """A player absent from every frame gets an unknown, no-penalty result."""
    out = build_player_availability(
        _player(sleeper_id="nobody"), _warehouse_with({}), [2023, 2024, 2025]
    )
    assert out["availability_risk"] == "unknown"
    assert out["games_played_history"] == []


# ---------------------------------------------------------------------------
# QB availability — resolves via seasonal_stats by gsis + position
# ---------------------------------------------------------------------------

def test_qb_in_skill_positions():
    """QBs are processed by the injury agent so availability is computed."""
    assert "QB" in SKILL_POSITIONS


def _qb_warehouse(games_by_season: dict[int, int], gsis="00-0036442"):
    """Warehouse whose seasonal_stats holds one QB row per season."""
    frames = {
        season: pd.DataFrame([{
            "player_id": gsis, "player_name": "Q.B", "position": "QB",
            "games": games, "fantasy_points_ppr": games * 18.0,
        }])
        for season, games in games_by_season.items()
    }
    return types.SimpleNamespace(get_seasonal_stats=lambda s: frames.get(s))


def _qb_player(gsis="00-0036442"):
    return types.SimpleNamespace(
        name="Joe Burrow", team_abbr="CIN", position="QB",
        gsis_id=gsis, sleeper_id="6770", sportradar_id=None,
    )


def test_burrow_concern_availability():
    """Burrow [10, 17, 8] (avg 11.7) → concern."""
    wh = _qb_warehouse({2023: 10, 2024: 17, 2025: 8})
    out = build_player_availability(_qb_player(), wh, [2023, 2024, 2025])
    assert [g["games"] for g in out["games_played_history"]] == [10, 17, 8]
    assert out["availability_risk"] == "concern"


def test_allen_durable_availability():
    """Allen [17, 16, 16] (avg 16.3) → durable."""
    wh = _qb_warehouse({2023: 17, 2024: 16, 2025: 16})
    out = build_player_availability(_qb_player(), wh, [2023, 2024, 2025])
    assert out["availability_risk"] == "durable"
    assert out["availability_risk_modifier"] == 0.0


def test_daniels_concern_two_seasons():
    """Daniels has no 2023 NFL data; [17, 7] (avg 12) → concern, not unknown."""
    wh = _qb_warehouse({2024: 17, 2025: 7})  # 2023 absent (college)
    out = build_player_availability(_qb_player(), wh, [2023, 2024, 2025])
    assert [g["games"] for g in out["games_played_history"]] == [17, 7]
    assert out["availability_risk"] == "concern"
