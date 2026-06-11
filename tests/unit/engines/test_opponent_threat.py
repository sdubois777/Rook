"""Direct tests for backend/engines/opponent_threat.py.

Threat scoring, block values, combo flags, and nomination targets are
pure functions of roster + tendency data — tested with real DraftPick
objects, no mocks.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.engines.draft_state_manager import DraftPick
from backend.engines.opponent_threat import OpponentThreatAnalyzer


def _pick(position="RB", tier=1, name="Player", price=40):
    return DraftPick(
        player_id=name.lower().replace(" ", "-"),
        team_id="opp-1",
        price=price,
        player_name=name,
        position=position,
        tier=tier,
    )


# ---------------------------------------------------------------------------
# Threat score
# ---------------------------------------------------------------------------

def test_threat_score_empty_roster_is_zero():
    """No picks means no threat."""
    analyzer = OpponentThreatAnalyzer()
    assert analyzer.get_threat_score([]) == 0


def test_threat_score_higher_for_top_tier_players():
    """A tier-1 heavy roster outscores a tier-4 roster of the same size."""
    analyzer = OpponentThreatAnalyzer()
    elite = [_pick(tier=1), _pick(tier=1, name="Second RB")]
    depth = [_pick(tier=4), _pick(tier=4, name="Second RB")]

    assert analyzer.get_threat_score(elite) > analyzer.get_threat_score(depth)


def test_threat_score_positional_bias_raises_score():
    """Historical RB-heavy tendency inflates that opponent's RB threat."""
    roster = [_pick(position="RB", tier=1)]
    neutral = OpponentThreatAnalyzer()
    rb_heavy = OpponentThreatAnalyzer(
        tendencies={"opp-1": {"positional_bias": {"RB": 1.5}}}
    )

    assert (
        rb_heavy.get_threat_score(roster, team_id="opp-1")
        > neutral.get_threat_score(roster, team_id="opp-1")
    )


def test_threat_score_capped_at_100():
    """The composite score never exceeds 100 however stacked the roster."""
    analyzer = OpponentThreatAnalyzer()
    roster = [_pick(tier=1, name=f"RB {i}") for i in range(10)]

    assert analyzer.get_threat_score(roster) == 100


# ---------------------------------------------------------------------------
# Block value
# ---------------------------------------------------------------------------

def test_block_value_zero_when_opponent_budget_tapped():
    """No block is warranted against an opponent under the $15 floor."""
    analyzer = OpponentThreatAnalyzer()
    player = {"tier": 1, "position": "RB", "system_value": 50}

    value = analyzer.get_block_value(player, [_pick(tier=1)], opponent_budget=14)

    assert value == 0.0


def test_block_value_elite_rb_stack_premium():
    """A second tier-1 RB to an opponent holding one warrants a 1.5x block."""
    analyzer = OpponentThreatAnalyzer()
    player = {"tier": 1, "position": "RB", "system_value": 50}

    value = analyzer.get_block_value(player, [_pick(position="RB", tier=1)], opponent_budget=100)

    assert value == 75.0


def test_block_value_zero_when_no_combo_created():
    """A mid-tier player creating no combo is not worth blocking."""
    analyzer = OpponentThreatAnalyzer()
    player = {"tier": 3, "position": "WR", "system_value": 20}

    value = analyzer.get_block_value(player, [_pick(position="RB", tier=1)], opponent_budget=100)

    assert value == 0.0


# ---------------------------------------------------------------------------
# Combo flags
# ---------------------------------------------------------------------------

def test_combo_flags_elite_rb_stack_detected():
    """Two tier-1 RBs on one roster raises the Elite RB Stack flag."""
    analyzer = OpponentThreatAnalyzer()
    roster = [_pick(tier=1, name="CMC"), _pick(tier=1, name="Bijan")]

    flags = analyzer.get_active_combo_flags(roster)

    assert any("Elite RB Stack" in f for f in flags)
    assert any("CMC" in f and "Bijan" in f for f in flags)


def test_combo_flags_qb_wr_same_team_stack_detected():
    """A QB and WR from the same NFL team raises the stack flag once."""
    analyzer = OpponentThreatAnalyzer()
    qb = SimpleNamespace(position="QB", tier=2, player_name="QB1", team_abbr="CIN")
    wr1 = SimpleNamespace(position="WR", tier=2, player_name="WR1", team_abbr="CIN")
    wr2 = SimpleNamespace(position="WR", tier=2, player_name="WR2", team_abbr="CIN")

    flags = analyzer.get_active_combo_flags([qb, wr1, wr2])

    stack_flags = [f for f in flags if "QB/WR Stack" in f]
    assert len(stack_flags) == 1
    assert "CIN" in stack_flags[0]


def test_combo_flags_clean_roster_returns_empty():
    """A balanced roster with no combos raises no flags."""
    analyzer = OpponentThreatAnalyzer()
    roster = [_pick(position="RB", tier=2), _pick(position="WR", tier=2, name="WR1")]

    assert analyzer.get_active_combo_flags(roster) == []


# ---------------------------------------------------------------------------
# Nomination targets
# ---------------------------------------------------------------------------

def _market_player(pid, name, position, market, system):
    return {
        "yahoo_player_id": pid,
        "name": name,
        "position": position,
        "market_value": market,
        "system_value": system,
    }


def test_nomination_targets_only_overvalued_players():
    """Only players the market overvalues are nomination bait."""
    analyzer = OpponentThreatAnalyzer()
    players = [
        _market_player("p1", "Overpriced RB", "RB", market=60, system=40),
        _market_player("p2", "Bargain WR", "WR", market=20, system=35),
    ]

    targets = analyzer.get_nomination_targets(players, your_roster=[], your_budget=200)

    assert [t["player_name"] for t in targets] == ["Overpriced RB"]
    assert targets[0]["overpay_amount"] == 20.0


def test_nomination_targets_excludes_drafted_players():
    """Already-drafted players never appear as nomination targets."""
    analyzer = OpponentThreatAnalyzer()
    players = [_market_player("p1", "Overpriced RB", "RB", market=60, system=40)]

    targets = analyzer.get_nomination_targets(
        players, your_roster=[], your_budget=200, drafted_ids={"p1"}
    )

    assert targets == []


def test_nomination_targets_ranked_by_bias_weighted_drain():
    """Opponent positional bias reorders equally-overpriced targets."""
    analyzer = OpponentThreatAnalyzer(
        tendencies={"opp-1": {"positional_bias": {"WR": 2.0, "RB": 1.0}}}
    )
    players = [
        _market_player("p1", "RB Target", "RB", market=50, system=40),
        _market_player("p2", "WR Target", "WR", market=50, system=40),
    ]

    targets = analyzer.get_nomination_targets(players, your_roster=[], your_budget=200)

    assert targets[0]["player_name"] == "WR Target"
    assert targets[0]["drain_score"] > targets[1]["drain_score"]


def test_nomination_targets_capped_at_five():
    """At most five nomination targets are returned."""
    analyzer = OpponentThreatAnalyzer()
    players = [
        _market_player(f"p{i}", f"Target {i}", "WR", market=50 + i, system=30)
        for i in range(8)
    ]

    targets = analyzer.get_nomination_targets(players, your_roster=[], your_budget=200)

    assert len(targets) == 5
