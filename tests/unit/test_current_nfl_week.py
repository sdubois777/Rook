"""
Unit tests for the current-NFL-week source (utils/seasons.get_current_nfl_week /
current_week_from_schedule).

The derivation is pure over an nflverse-shaped schedule frame, so these build
synthetic REG schedules and assert the week at controlled ``now`` instants —
covering the offseason sentinel, the pre-kickoff→upcoming and in-progress→current
rolls, the season-complete clamp, DST-correct kickoff localization, and the
loud-warn safety cases. The wrapper is exercised with a monkeypatched fetch.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.utils import seasons
from backend.utils.seasons import current_week_from_schedule, get_current_nfl_week


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sched(games, *, extra_non_reg=True):
    """Build a schedule frame from (week, gameday, gametime) tuples (all REG).
    Adds a POST row to prove non-REG games are filtered out."""
    rows = [
        {"game_type": "REG", "week": wk, "gameday": gd, "gametime": gt}
        for wk, gd, gt in games
    ]
    if extra_non_reg:
        rows.append({"game_type": "POST", "week": 1, "gameday": "2026-01-18", "gametime": "13:00"})
    return pd.DataFrame(rows)


def _utc(y, mo, da, h=12, mi=0):
    return datetime(y, mo, da, h, mi, tzinfo=timezone.utc)


# A compact, realistic 3-week September schedule (EDT, ET = UTC-4).
# wk1: Thu 09-04 20:20, Sun 09-07 13:00, Mon 09-08 20:00
# wk2: Thu 09-11 20:20, Sun 09-14 13:00, Mon 09-15 20:00
# wk3: Thu 09-18 20:20, Sun 09-21 13:00
_THREE_WEEKS = _sched([
    (1, "2025-09-04", "20:20"), (1, "2025-09-07", "13:00"), (1, "2025-09-08", "20:00"),
    (2, "2025-09-11", "20:20"), (2, "2025-09-14", "13:00"), (2, "2025-09-15", "20:00"),
    (3, "2025-09-18", "20:20"), (3, "2025-09-21", "13:00"),
])


# ---------------------------------------------------------------------------
# core derivation
# ---------------------------------------------------------------------------
def test_offseason_before_first_kickoff_returns_zero():
    # Wed before the Thursday opener — no REG game has kicked off yet.
    assert current_week_from_schedule(_THREE_WEEKS, now=_utc(2025, 9, 3, 12)) == 0


def test_week_in_progress_returns_that_week():
    # Friday of wk1 (Thu game played, wk1's Monday finale still ahead) → wk1.
    assert current_week_from_schedule(_THREE_WEEKS, now=_utc(2025, 9, 5, 12)) == 1
    # Sunday afternoon of wk1 → still wk1.
    assert current_week_from_schedule(_THREE_WEEKS, now=_utc(2025, 9, 7, 20)) == 1


def test_after_weeks_final_game_rolls_to_upcoming_week():
    # Tuesday after wk1's Monday finale → the UPCOMING week (wk2), the recommended
    # fantasy-week behavior (lineup/waiver decisions point at the next week).
    assert current_week_from_schedule(_THREE_WEEKS, now=_utc(2025, 9, 9, 17)) == 2
    # Tuesday after wk2 → wk3.
    assert current_week_from_schedule(_THREE_WEEKS, now=_utc(2025, 9, 16, 17)) == 3


def test_season_complete_clamps_to_last_week_never_higher():
    # After wk3's final game, every REG week is complete → clamp to the last week
    # present (3 here), never week 4.
    assert current_week_from_schedule(_THREE_WEEKS, now=_utc(2025, 9, 25, 12)) == 3


def test_naive_now_is_treated_as_utc():
    naive = datetime(2025, 9, 5, 12)                 # no tzinfo
    aware = _utc(2025, 9, 5, 12)
    assert (current_week_from_schedule(_THREE_WEEKS, now=naive)
            == current_week_from_schedule(_THREE_WEEKS, now=aware) == 1)


def test_non_reg_games_are_ignored():
    # The POST row (_sched adds one) must not affect the derived week.
    assert current_week_from_schedule(_THREE_WEEKS, now=_utc(2025, 9, 7, 20)) == 1


# ---------------------------------------------------------------------------
# timezone / DST correctness — a NOVEMBER (EST, ET = UTC-5) Sunday 13:00 game
# localizes to 18:00 UTC, not 17:00. At 17:30 UTC that game has NOT kicked off,
# so its week is still upcoming. A buggy EDT (UTC-4) localization would read it as
# started and roll a week early — this asserts EST is used for November.
# ---------------------------------------------------------------------------
def test_est_kickoff_localization_in_november():
    sched = _sched([
        (1, "2025-11-06", "20:00"),   # Thu opener (season already started)
        (2, "2025-11-09", "13:00"),   # Sun 13:00 ET = 18:00 UTC (EST)
        (3, "2025-11-16", "13:00"),   # a later week so the roll is observable
    ], extra_non_reg=False)
    # 17:30 UTC: with correct EST (kick 18:00 UTC) wk2 is NOT complete → returns 2.
    # A UTC-4 bug (kick 17:00 UTC) would mark wk2 complete → return 3.
    assert current_week_from_schedule(sched, now=_utc(2025, 11, 9, 17, 30)) == 2
    # 18:30 UTC: wk2's game has kicked off → roll to the upcoming wk3.
    assert current_week_from_schedule(sched, now=_utc(2025, 11, 9, 18, 30)) == 3


# ---------------------------------------------------------------------------
# loud-warn safety cases (never silently return a wrong week)
# ---------------------------------------------------------------------------
def test_empty_frame_returns_zero_and_warns(caplog):
    with caplog.at_level("WARNING"):
        assert current_week_from_schedule(pd.DataFrame(), now=_utc(2025, 10, 1)) == 0
    assert any("empty" in r.message.lower() for r in caplog.records)


def test_none_frame_returns_zero():
    assert current_week_from_schedule(None, now=_utc(2025, 10, 1)) == 0


def test_missing_columns_returns_zero_and_warns(caplog):
    df = pd.DataFrame([{"game_type": "REG", "week": 1}])  # no gameday/gametime
    with caplog.at_level("WARNING"):
        assert current_week_from_schedule(df, now=_utc(2025, 10, 1)) == 0
    assert any("missing columns" in r.message for r in caplog.records)


def test_no_reg_games_returns_zero_and_warns(caplog):
    df = pd.DataFrame([{"game_type": "POST", "week": 1, "gameday": "2026-01-18", "gametime": "13:00"}])
    with caplog.at_level("WARNING"):
        assert current_week_from_schedule(df, now=_utc(2025, 10, 1)) == 0
    assert any("no REG games" in r.message for r in caplog.records)


def test_unparseable_row_skipped_but_still_derives(caplog):
    sched = _sched([
        (1, "2025-09-04", "20:20"), (1, "2025-09-07", "13:00"),
        (2, "not-a-date", "13:00"),      # unparseable — skipped with a warn
        (2, "2025-09-14", "13:00"),
    ], extra_non_reg=False)
    with caplog.at_level("WARNING"):
        wk = current_week_from_schedule(sched, now=_utc(2025, 9, 9, 17))
    assert wk == 2                                   # still derives from the good rows
    assert any("unparseable" in r.message for r in caplog.records)


def test_missing_gametime_defaults_and_still_counts():
    # A blank kickoff time defaults to 13:00 ET (game still counts toward its week).
    sched = pd.DataFrame([
        {"game_type": "REG", "week": 1, "gameday": "2025-09-04", "gametime": "20:20"},
        {"game_type": "REG", "week": 1, "gameday": "2025-09-07", "gametime": None},
    ])
    # Sunday afternoon of wk1 → wk1 (not offseason, not rolled).
    assert current_week_from_schedule(sched, now=_utc(2025, 9, 7, 20)) == 1


# ---------------------------------------------------------------------------
# the fetch wrapper
# ---------------------------------------------------------------------------
def test_get_current_nfl_week_uses_cached_schedule(monkeypatch):
    import backend.integrations.nfl_data as nfl_data
    monkeypatch.setattr(nfl_data, "fetch_schedules", lambda season: _THREE_WEEKS)
    assert get_current_nfl_week(2025, now=_utc(2025, 9, 16, 17)) == 3


def test_get_current_nfl_week_defaults_season(monkeypatch):
    seen = {}

    def _fake(season):
        seen["season"] = season
        return _THREE_WEEKS

    import backend.integrations.nfl_data as nfl_data
    monkeypatch.setattr(nfl_data, "fetch_schedules", _fake)
    monkeypatch.setattr(seasons, "get_current_season", lambda: 2025)
    get_current_nfl_week(now=_utc(2025, 9, 7, 20))
    assert seen["season"] == 2025


def test_get_current_nfl_week_fetch_failure_returns_zero_and_warns(monkeypatch, caplog):
    def _boom(season):
        raise RuntimeError("network down")

    import backend.integrations.nfl_data as nfl_data
    monkeypatch.setattr(nfl_data, "fetch_schedules", _boom)
    with caplog.at_level("WARNING"):
        assert get_current_nfl_week(2025, now=_utc(2025, 10, 1)) == 0
    assert any("could not load" in r.message for r in caplog.records)
