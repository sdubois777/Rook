"""Undrafted guard + season-start no-data fix (real_league_source)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.core.exceptions import UndraftedLeagueError
from backend.services.trade import real_league_source as RLS


def _lg(**kw):
    base = dict(draft_status=None, draft_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ---- persisted_undrafted_signal (explicit signals) -------------------------

def test_signal_sleeper_pre_draft():
    assert RLS.persisted_undrafted_signal(_lg(draft_status="pre_draft")) == "draft_status"


def test_signal_sleeper_drafting():
    assert RLS.persisted_undrafted_signal(_lg(draft_status="drafting")) == "draft_status"


def test_signal_complete_is_not_undrafted():
    assert RLS.persisted_undrafted_signal(_lg(draft_status="complete")) is None


def test_signal_future_draft_date():
    future = datetime.now(timezone.utc) + timedelta(days=30)
    assert RLS.persisted_undrafted_signal(_lg(draft_date=future)) == "draft_date"


def test_signal_past_draft_date_is_drafted():
    past = datetime.now(timezone.utc) - timedelta(days=30)
    assert RLS.persisted_undrafted_signal(_lg(draft_date=past)) is None


def test_signal_none_when_no_data():
    assert RLS.persisted_undrafted_signal(_lg()) is None


def test_complete_status_wins_over_future_date():
    # An explicit 'complete' must never be overridden by a stale future date.
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert RLS.persisted_undrafted_signal(_lg(draft_status="complete", draft_date=future)) is None


# ---- UndraftedLeagueError copy ---------------------------------------------

def test_error_shape_explicit():
    e = UndraftedLeagueError("draft_date")
    assert e.status_code == 409 and e.error_code == "undrafted_league"
    assert e.detail["signal"] == "draft_date"
    assert "hasn't drafted" in e.message


def test_error_copy_inferred_is_hedged():
    # Inference copy must NOT assert undrafted as certain (could be a failed sync).
    e = UndraftedLeagueError("inferred")
    assert "re-sync" in e.message.lower()


# ---- Part 1: season-start no-data fetch -----------------------------------

@pytest.mark.asyncio
async def test_fetch_weekly_week0_returns_empty_no_fetch():
    """week < 1 (offseason) → empty frame, WITHOUT touching the nflverse layer."""
    df = await RLS._fetch_weekly(db=None, season=2026, week=0)
    assert isinstance(df, pd.DataFrame) and df.empty


@pytest.mark.asyncio
async def test_fetch_weekly_degrades_on_fetch_error(monkeypatch):
    """A no-data 404 from the fetchers degrades to an empty frame, not a 500."""
    async def _boom(*a, **k):
        raise Exception("HTTP Error 404: Not Found")
    monkeypatch.setattr("backend.integrations.nfl_weekly.weekly_player_usage", _boom)
    df = await RLS._fetch_weekly(db=None, season=2026, week=3)
    assert isinstance(df, pd.DataFrame) and df.empty
