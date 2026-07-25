"""The as-of clock — ROOK_ASOF_DATE.

Exists so a prospective backtest can rebuild the board as of a past preseason without
the system silently reading present-day data. Every assertion here is a contamination
guard: if one fails, an as-of run would produce a board that LOOKS fine and measures
nothing, which is worse than a crash.
"""
from __future__ import annotations

import os
from datetime import date, timezone

import pytest

from backend.utils import seasons as S


@pytest.fixture
def asof(monkeypatch):
    """Set ROOK_ASOF_DATE for one test and guarantee it is removed afterwards."""
    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv(S.ASOF_ENV, raising=False)
        else:
            monkeypatch.setenv(S.ASOF_ENV, value)
    return _set


def test_unset_means_real_time(asof):
    """Forgetting the override must leave you in the present, never in the past."""
    asof(None)
    assert S.asof_active() is False
    assert S.asof_date() == date.today()
    today = date.today()
    assert S.get_current_season() == (today.year if today.month >= 3 else today.year - 1)


def test_override_moves_the_season_window(asof):
    asof("2025-08-15")
    assert S.asof_active() is True
    assert S.get_current_season() == 2025
    assert S.get_analysis_year() == 2025
    # The whole point: the three completed seasons must EXCLUDE 2025, the season a
    # 2025-as-of board is trying to project.
    assert S.get_analysis_seasons(3) == [2022, 2023, 2024]
    assert 2025 not in S.get_analysis_seasons(6)


def test_january_boundary_still_applies_under_override(asof):
    """The March cutoff is calendar logic and must survive the override."""
    asof("2025-01-20")          # playoffs — still the 2024 season
    assert S.get_current_season() == 2024
    asof("2025-03-01")          # new league year
    assert S.get_current_season() == 2025


def test_wall_clock_moves_with_the_date(asof):
    """asof_now() must track asof_date(), or the week probe leaks the real present."""
    asof("2025-08-15")
    now = S.asof_now()
    assert now.date() == date(2025, 8, 15)
    assert now.tzinfo == timezone.utc


def test_latest_season_with_data_does_not_return_the_projected_season(asof):
    """THE look-ahead guard.

    latest_season_with_data() ceilings at get_current_season() and probes downward with
    _default_season_has_data(), which asks get_current_nfl_week(season) > 0 against the
    schedule. If that probe used the REAL clock while the date was rolled back, 2025
    would look complete and be returned — handing every completed-data metric the exact
    season being projected. team_metrics.py:388 consumes this and WRITES the result to
    team_systems, so the leak would be persisted, not just displayed.
    """
    asof(None)
    assert S.latest_season_with_data() == 2025      # real time: last completed season

    asof("2025-08-15")
    latest = S.latest_season_with_data()
    assert latest == 2024, f"expected 2024, got {latest}"
    assert latest < S.get_current_season(), "must never return the season being projected"


def test_bad_value_raises_rather_than_silently_falling_back(asof):
    """A typo must not quietly run at real time and produce a contaminated board."""
    asof("not-a-date")
    with pytest.raises(ValueError, match=S.ASOF_ENV):
        S.asof_date()
    with pytest.raises(ValueError):
        S.get_current_season()


def test_override_is_inherited_by_subprocesses(asof):
    """The pipeline shells out to seed/sync_rosters/sync_adp via subprocess.run, so an
    in-process override (module global, contextvar, threaded parameter) would NOT reach
    the three stages that write players.team_abbr and depth_chart_order. An env var does.
    """
    import subprocess
    import sys

    env = {**os.environ, S.ASOF_ENV: "2025-08-15"}
    out = subprocess.run(
        [sys.executable, "-c",
         "from backend.utils.seasons import get_current_season; print(get_current_season())"],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "2025"


def test_warehouse_build_inputs_follow_the_override(asof):
    """NflDataWarehouse.build() takes no season arguments — it resolves them at call
    time — so it must inherit the override with no signature change. Guards against
    someone 'fixing' build() by hardcoding seasons again.
    """
    asof("2025-08-15")
    assert S.get_analysis_seasons(6) == [2019, 2020, 2021, 2022, 2023, 2024]
    assert S.get_current_season() == 2025
    assert S.get_analysis_year() == 2025


# ---------------------------------------------------------------------------
# Depth charts — the input most likely to silently contaminate an as-of board
# ---------------------------------------------------------------------------

def test_depth_chart_snapshot_is_bounded_by_the_asof_clock(asof, monkeypatch, tmp_path):
    """nflverse depth charts are a TIME SERIES; taking the global latest leaks the future.

    import_depth_charts([2025]) returns 554,215 rows across 219 distinct dates running
    from 2025-08-03 into the following March. The unbounded "latest snapshot" is
    therefore a MARCH 2026 depth chart labelled 2025 — Jacoby Brissett as ARI QB1
    instead of Kyler Murray, and an IND receiver room with Pittman and Mitchell already
    gone. Those are the exact departures that inflated Josh Downs to the $33 WR1, so an
    as-of 2025 board built on it would measure nothing.
    """
    import pandas as pd
    from backend.integrations import nfl_data

    frame = pd.DataFrame({
        "dt": ["2025-08-14T07:00:00Z", "2026-03-14T07:00:00Z",
               "2025-08-14T07:00:00Z", "2026-03-14T07:00:00Z"],
        "team": ["ARI", "ARI", "IND", "IND"],
        "player_name": ["Kyler Murray", "Jacoby Brissett",
                        "Michael Pittman Jr.", "Josh Downs"],
        "gsis_id": ["00-1", "00-2", "00-3", "00-4"],
        "pos_abb": ["QB", "QB", "WR", "WR"],
        "pos_grp": ["Base Offense"] * 4,
        "pos_rank": [1, 1, 1, 1],
    })
    monkeypatch.setattr(nfl_data, "_cache_path", lambda name: tmp_path / f"{name}.parquet")
    monkeypatch.setattr(nfl_data.nfl, "import_depth_charts", lambda seasons: frame.copy())

    asof("2025-08-15")
    got = nfl_data.fetch_depth_charts(2025)
    names = set(got["full_name"])
    assert "Kyler Murray" in names
    assert "Michael Pittman Jr." in names
    assert "Jacoby Brissett" not in names, "leaked a post-as-of snapshot into a 2025 board"

    asof(None)
    got_now = nfl_data.fetch_depth_charts(2025)
    assert "Jacoby Brissett" in set(got_now["full_name"]), (
        "real-time behaviour must be unchanged — latest snapshot wins"
    )


def test_depth_chart_cache_key_separates_asof_from_real_time(asof, monkeypatch, tmp_path):
    """Same season, different clock, different answer — so they cannot share a file.

    Without the as-of suffix an as-of run would either read back the real-time snapshot
    or overwrite it, poisoning normal runs with a past-season depth chart.
    """
    import pandas as pd
    from backend.integrations import nfl_data

    written = []
    monkeypatch.setattr(
        nfl_data, "_cache_path",
        lambda name: (written.append(name) or (tmp_path / f"{name}.parquet")),
    )
    monkeypatch.setattr(
        nfl_data.nfl, "import_depth_charts",
        lambda seasons: pd.DataFrame({
            "dt": ["2025-08-14T07:00:00Z"], "team": ["ARI"],
            "player_name": ["Kyler Murray"], "gsis_id": ["00-1"],
            "pos_abb": ["QB"], "pos_grp": ["Base Offense"], "pos_rank": [1],
        }),
    )

    asof("2025-08-15")
    nfl_data.fetch_depth_charts(2025)
    asof(None)
    nfl_data.fetch_depth_charts(2025)

    assert "depth_charts_2025_asof_2025-08-15" in written
    assert "depth_charts_2025" in written
    assert len(set(written)) == 2, f"cache keys collided: {written}"


def test_sync_rosters_refuses_under_asof(asof, capsys):
    """sync_rosters must skip under as-of, or it stamps today's roster on a past board.

    Sleeper serves CURRENT state only. Running it during an as-of rebuild would write
    2026 teams, depth order and injury flags over the as-of roster — A.J. Brown on NE
    instead of PHI, an IND receiver room with Pittman and Mitchell already departed.
    Measured: 138 of 539 valued players changed team between the 2025 roster and today,
    so a quarter of the board would be silently wrong.

    Nothing is lost: seed_nfl_data.py:160 writes team_abbr from
    fetch_rosters(get_current_season()), which under the as-of clock IS the as-of
    season's roster.
    """
    import asyncio
    import importlib.util
    import sys
    from pathlib import Path
    from unittest.mock import AsyncMock, patch

    script = Path(__file__).resolve().parents[3] / "scripts" / "sync_rosters.py"
    spec = importlib.util.spec_from_file_location("sync_rosters_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    asof("2025-08-15")
    with patch.object(mod, "sync_players_from_sleeper", AsyncMock()) as synced:
        with patch.object(sys, "argv", ["sync_rosters.py", "--dry-run"]):
            asyncio.run(mod.main())
    synced.assert_not_awaited()
    assert "SKIPPED" in capsys.readouterr().out

    # Without the override it must still run normally.
    asof(None)
    with patch.object(mod, "sync_players_from_sleeper", AsyncMock(return_value={
        "updated": 0, "inserted": 0, "skipped": 0, "filtered": 0,
    })) as synced:
        with patch.object(sys, "argv", ["sync_rosters.py", "--dry-run"]):
            asyncio.run(mod.main())
    synced.assert_awaited_once()


def test_sync_rosters_depth_lookup_is_not_a_hardcoded_year():
    """The depth-chart relevance check must key off warehouse.current_season.

    It was `warehouse.depth_charts.get(2026, ...)`. A literal year silently returns an
    empty frame the moment the season rolls over or an as-of clock is set, dropping the
    check with no error — the player is then judged only on the game-appearance path.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[3] / "scripts" / "sync_rosters.py").read_text(
        encoding="utf-8"
    )
    assert "depth_charts.get(2026" not in src
    assert "depth_charts.get(warehouse.current_season" in src


def test_warehouse_skips_sleeper_rosters_under_asof(asof, monkeypatch):
    """warehouse.rosters must come from nflverse under as-of, not Sleeper.

    roster_changes diffs warehouse.rosters against warehouse.prev_rosters to detect
    arrivals and departures. prev_rosters is season-keyed (current_season - 1), but
    rosters defaulted to Sleeper, which serves CURRENT state only. Pairing a 2026
    Sleeper roster with a 2024 nflverse one would manufacture the WRONG offseason —
    two years of moves collapsed into one — and every dependency flag on the board is
    built from that diff.
    """
    import pandas as pd
    from backend.integrations import nfl_data

    called = {"sleeper": 0, "nflverse": []}

    def fake_sleeper():
        called["sleeper"] += 1
        return pd.DataFrame({"full_name": ["Someone Current"], "team": ["NE"]})

    def fake_rosters(season):
        called["nflverse"].append(season)
        return pd.DataFrame({"full_name": [f"Player {season}"], "team": ["PHI"]})

    import backend.integrations.sleeper as sleeper_mod
    monkeypatch.setattr(sleeper_mod, "fetch_sleeper_players", fake_sleeper)
    monkeypatch.setattr(nfl_data, "fetch_rosters", fake_rosters)
    monkeypatch.setattr(nfl_data, "fetch_seasonal_rosters", lambda s: pd.DataFrame())
    monkeypatch.setattr(nfl_data.NflDataWarehouse, "_load_depth_charts", lambda self: None)

    asof("2025-08-15")
    wh = nfl_data.NflDataWarehouse(
        analysis_seasons=[2022, 2023, 2024], current_season=2025, analysis_year=2025,
    )
    wh._load_infrastructure()

    assert called["sleeper"] == 0, "Sleeper must not be consulted during an as-of run"
    # Both sides of the offseason diff come from nflverse, one season apart.
    assert 2025 in called["nflverse"], "current rosters must be the as-of season"
    assert 2024 in called["nflverse"], "prev rosters must be as-of minus one"
