"""Tests for platform data models."""
from backend.integrations.platform_models import (
    DraftPick,
    FreeAgent,
    RosteredPlayer,
    TeamRoster,
    Transaction,
    WeeklyMatchup,
)


def test_rostered_player_defaults():
    p = RosteredPlayer(
        platform_player_id="123",
        player_name="Josh Allen",
        position="QB",
        team_abbr="BUF",
    )
    assert p.is_starter is False
    assert p.injury_status is None


def test_team_roster_empty_players():
    t = TeamRoster(
        platform_team_id="1",
        manager_name="Manager",
        team_name="Team",
    )
    assert t.players == []
    assert t.faab_remaining is None
    assert t.wins == 0


def test_draft_pick_auction_vs_snake():
    auction = DraftPick(
        platform_player_id="1",
        player_name="CMC",
        position="RB",
        team_abbr="SF",
        picked_by_team_id="3",
        manager_name="Bob",
        pick_number=1,
        round_number=1,
        auction_price=62,
    )
    assert auction.auction_price == 62

    snake = DraftPick(
        platform_player_id="2",
        player_name="Mahomes",
        position="QB",
        team_abbr="KC",
        picked_by_team_id="4",
        manager_name="Alice",
        pick_number=5,
        round_number=1,
    )
    assert snake.auction_price is None


def test_free_agent_defaults():
    fa = FreeAgent(
        platform_player_id="42",
        player_name="Kicker",
        position="K",
        team_abbr="NE",
    )
    assert fa.ownership_pct == 0.0
    assert fa.waiver_priority is None


def test_weekly_matchup():
    m = WeeklyMatchup(
        week=1,
        home_team_id="1",
        away_team_id="2",
        home_score=120.5,
        away_score=110.3,
        is_complete=True,
    )
    assert m.is_complete
    assert m.home_score > m.away_score


def test_transaction_types():
    t = Transaction(
        type="trade",
        player_name="Lamar Jackson",
        position="QB",
        added_by_team_id="5",
        dropped_by_team_id="3",
    )
    assert t.type == "trade"
    assert t.faab_bid is None
