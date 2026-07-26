"""Tests for fetch_depth_charts schema handling in backend.integrations.nfl_data.

The 2023/2024 nflverse schema and the 2025+ schema disagree about which column holds
the UNIT and which holds the RANK. Reading the wrong one returned an empty depth chart
for every pre-2025 season, silently — the as-of 2024 board was built with no depth
signal at all as a result.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from backend.integrations.nfl_data import fetch_depth_charts


def _legacy_frame(weeks=(1, 22)) -> pd.DataFrame:
    """A 2023/2024-shaped feed: `depth_team` is the RANK, `formation` is the UNIT.

    Week 1 is the preseason chart; week 22 is a February playoff chart. Both are
    present in the real feed, which is why the snapshot filter has to be bounded.
    """
    rows = []
    for wk in weeks:
        # the starter changes between the preseason and playoff snapshots
        qb1 = "Preseason Starter" if wk == 1 else "January Starter"
        rows += [
            {"season": 2024, "club_code": "ATL", "week": float(wk), "depth_team": "1",
             "formation": "Offense", "position": "QB", "full_name": qb1},
            {"season": 2024, "club_code": "ATL", "week": float(wk), "depth_team": "2",
             "formation": "Offense", "position": "QB", "full_name": "Backup"},
            {"season": 2024, "club_code": "ATL", "week": float(wk), "depth_team": "1",
             "formation": "Offense", "position": "RB", "full_name": "Bijan"},
            {"season": 2024, "club_code": "ATL", "week": float(wk), "depth_team": "1",
             "formation": "Defense", "position": "CB", "full_name": "Corner"},
            {"season": 2024, "club_code": "ATL", "week": float(wk), "depth_team": "1",
             "formation": "Special Teams", "position": "K", "full_name": "Kicker"},
        ]
    return pd.DataFrame(rows)


@pytest.fixture
def _no_cache(tmp_path):
    """Point the parquet cache at an empty temp dir so tests never hit or write real files."""
    with patch("backend.integrations.nfl_data._cache_path",
               side_effect=lambda name: tmp_path / f"{name}.parquet"):
        yield


def test_offense_filter_reads_formation_not_depth_team(_no_cache):
    """REGRESSION: the unit lives in `formation`; `depth_team` holds the rank.

    Filtering `depth_team == "offense"` matched zero rows and returned an empty depth
    chart for every pre-2025 season.
    """
    with patch("nfl_data_py.import_depth_charts", return_value=_legacy_frame(weeks=(1,))):
        out = fetch_depth_charts(2024)

    assert not out.empty, "offense filter dropped every row"
    assert set(out["position"]) == {"QB", "RB"}, "defense/special teams leaked in"
    assert "Corner" not in set(out["full_name"])
    assert "Kicker" not in set(out["full_name"])


def test_depth_rank_is_read_from_depth_team_not_row_order(_no_cache):
    """`depth_team` IS the rank. Inferring it from row order invents a starter."""
    frame = _legacy_frame(weeks=(1,))
    # put the BACKUP first, so row order and true rank disagree
    frame = pd.concat([frame.iloc[[1]], frame.drop(index=1)], ignore_index=True)

    with patch("nfl_data_py.import_depth_charts", return_value=frame):
        out = fetch_depth_charts(2024)

    qbs = out[out["position"] == "QB"].set_index("full_name")["depth_rank"].to_dict()
    assert qbs["Preseason Starter"] == 1
    assert qbs["Backup"] == 2


def test_week_snapshot_is_bounded_by_the_asof_clock(_no_cache):
    """The feed runs to week 22, so an unbounded max hands back a FEBRUARY chart.

    Fixing the offense filter without this trades "no depth charts" for "next season's
    depth charts", which is worse because it looks correct.
    """
    with patch("nfl_data_py.import_depth_charts", return_value=_legacy_frame()), \
         patch("backend.utils.seasons.get_current_season", return_value=2024), \
         patch("backend.utils.seasons.get_current_nfl_week", return_value=0):
        out = fetch_depth_charts(2024)

    assert set(out["week"]) == {1.0}, f"expected the week-1 snapshot, got {set(out['week'])}"
    starters = out[(out["position"] == "QB") & (out["depth_rank"] == 1)]["full_name"]
    assert list(starters) == ["Preseason Starter"], "a playoff-week chart leaked in"


def test_a_past_season_is_not_week_bounded(_no_cache):
    """Only the CURRENT (as-of) season needs bounding. An earlier season is wholly
    historical relative to the clock, so its full range is legitimate."""
    with patch("nfl_data_py.import_depth_charts", return_value=_legacy_frame()), \
         patch("backend.utils.seasons.get_current_season", return_value=2026):
        out = fetch_depth_charts(2024)

    assert set(out["week"]) == {22.0}, "a fully historical season should take its latest"


def test_empty_after_offense_filter_returns_empty_frame(_no_cache):
    """A feed with no offensive rows yields an empty frame rather than raising."""
    frame = _legacy_frame(weeks=(1,))
    frame = frame[frame["formation"] != "Offense"]

    with patch("nfl_data_py.import_depth_charts", return_value=frame):
        out = fetch_depth_charts(2024)

    assert out.empty
