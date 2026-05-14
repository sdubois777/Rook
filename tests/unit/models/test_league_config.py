"""Tests for LeagueConfig dataclass — replacement levels, budgets, derived values."""
from backend.models.league_config import LeagueConfig, DEFAULT_LEAGUE_CONFIG


def test_default_config_matches_current_behavior():
    """DEFAULT_LEAGUE_CONFIG produces identical output to current hardcoded values."""
    cfg = DEFAULT_LEAGUE_CONFIG
    assert cfg.team_count == 12
    assert cfg.budget == 200
    assert cfg.scoring == "ppr"
    assert cfg.draft_type == "auction"


def test_12_team_replacement_levels():
    cfg = DEFAULT_LEAGUE_CONFIG
    assert cfg.wr_replacement_rank == 31   # (2 + 0.6) * 12 = 31.2 → 31
    assert cfg.rb_replacement_rank == 29   # (2 + 0.4) * 12 = 28.8 → 29
    assert cfg.qb_replacement_rank == 14   # 1 * 12 * 1.2 = 14.4 → 14
    assert cfg.te_replacement_rank == 16   # 1 * 12 * 1.3 = 15.6 → 16


def test_8_team_replacement_levels_lower():
    """8-team: fewer teams = lower replacement rank."""
    cfg = LeagueConfig(team_count=8, budget=100)
    assert cfg.wr_replacement_rank < DEFAULT_LEAGUE_CONFIG.wr_replacement_rank
    assert cfg.rb_replacement_rank < DEFAULT_LEAGUE_CONFIG.rb_replacement_rank
    assert cfg.qb_replacement_rank < DEFAULT_LEAGUE_CONFIG.qb_replacement_rank
    assert cfg.te_replacement_rank < DEFAULT_LEAGUE_CONFIG.te_replacement_rank


def test_14_team_replacement_levels_higher():
    """14-team: more teams = higher replacement rank."""
    cfg = LeagueConfig(team_count=14)
    assert cfg.wr_replacement_rank > DEFAULT_LEAGUE_CONFIG.wr_replacement_rank
    assert cfg.rb_replacement_rank > DEFAULT_LEAGUE_CONFIG.rb_replacement_rank


def test_total_skill_pool_scales_with_teams():
    c8 = LeagueConfig(team_count=8, budget=200)
    c12 = LeagueConfig(team_count=12, budget=200)
    c14 = LeagueConfig(team_count=14, budget=200)
    assert c8.total_skill_pool < c12.total_skill_pool < c14.total_skill_pool


def test_total_skill_pool_value():
    cfg = DEFAULT_LEAGUE_CONFIG
    expected = 200 * 12 * 0.925
    assert cfg.total_skill_pool == expected


def test_rec_points_ppr_is_1():
    cfg = LeagueConfig(scoring="ppr")
    assert cfg.rec_points == 1.0


def test_rec_points_half_ppr_is_0_5():
    cfg = LeagueConfig(scoring="half_ppr")
    assert cfg.rec_points == 0.5


def test_positional_budget_sums_to_1():
    cfg = DEFAULT_LEAGUE_CONFIG
    total = sum(
        cfg.positional_budget_pct(pos)
        for pos in ("RB", "WR", "QB", "TE", "K", "DEF")
    )
    assert abs(total - 1.0) < 0.001


def test_tier_counts_scale_with_team_count():
    c8 = LeagueConfig(team_count=8)
    c12 = LeagueConfig(team_count=12)
    c16 = LeagueConfig(team_count=16)
    # More teams → more players at each tier
    assert c8.tier_counts["WR"][4] <= c12.tier_counts["WR"][4]
    assert c12.tier_counts["WR"][4] <= c16.tier_counts["WR"][4]


def test_is_auction_and_snake():
    auction = LeagueConfig(draft_type="auction")
    snake = LeagueConfig(draft_type="snake")
    assert auction.is_auction is True
    assert auction.is_snake is False
    assert snake.is_snake is True
    assert snake.is_auction is False


def test_skill_starter_slots():
    cfg = DEFAULT_LEAGUE_CONFIG
    # QB(1) + RB(2) + WR(2) + TE(1) + FLEX(1) = 7
    assert cfg.skill_starter_slots == 7


def test_positional_budget():
    cfg = DEFAULT_LEAGUE_CONFIG
    rb_budget = cfg.positional_budget("RB")
    expected = cfg.total_skill_pool * 0.38
    assert rb_budget == expected


def test_8_team_produces_different_values_than_12():
    c8 = LeagueConfig(team_count=8, budget=100)
    c12 = DEFAULT_LEAGUE_CONFIG
    assert c8.total_skill_pool != c12.total_skill_pool
    assert c8.wr_replacement_rank < c12.wr_replacement_rank
