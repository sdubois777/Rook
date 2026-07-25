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
