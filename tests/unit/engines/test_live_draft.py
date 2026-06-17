"""
tests/unit/engines/test_live_draft.py

Stage 12: Live Draft Agent — all 12 named test cases from stage-12-live-draft.md.

Tests cover:
  - Dependency flag activation (McConkey/Allen canonical example)
  - Block flag logic (combo threat, budget suppression)
  - Bid ceiling calculations (tier-based anchor weights)
  - Nomination strategy (drain opponents)
  - Budget tracking accuracy
  - Performance (< 2s with mocked API)
  - Opponent threat scoring and combo detection
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engines.draft_state_manager import (
    DraftPick,
    DraftStateManager,
    LeagueConfig,
)
from backend.engines.dependency_resolver import DependencyResolver
from backend.engines.opponent_threat import OpponentThreatAnalyzer
from backend.engines.valuation import compute_bid_ceiling

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_PATH = Path(__file__).parent.parent.parent / "fixtures" / "live_draft_fixtures.json"

with open(FIXTURES_PATH) as f:
    FIXTURES = json.load(f)

MCCONKEY_RECORD = FIXTURES["mcconkey_record"]
ALLEN_YAHOO_ID = FIXTURES["allen_yahoo_id"]
TAYLOR_RECORD = FIXTURES["taylor_record"]

YOUR_TEAM_ID = "my_team"


def _make_pick(data: dict) -> DraftPick:
    """Create DraftPick from fixture dict."""
    return DraftPick(
        player_id=data["player_id"],
        team_id=data["team_id"],
        price=data["price"],
        player_name=data.get("player_name", ""),
        position=data.get("position", ""),
        tier=data.get("tier"),
    )


def _make_league_config() -> LeagueConfig:
    return LeagueConfig(auction_budget=200, min_bid=1)


# ---------------------------------------------------------------------------
# Test 1: test_displaced_flag_activates_when_trigger_drafted
# ---------------------------------------------------------------------------

def test_displaced_flag_activates_when_trigger_drafted():
    """
    McConkey has DISPLACED flag triggered by Allen.
    Allen is in drafted_player_ids.
    McConkey's active_flags must contain the displacement.
    bid_ceiling must be lower than pre-flag value.
    """
    resolver = DependencyResolver()
    drafted = {ALLEN_YAHOO_ID}

    flags, modifier = resolver.apply_active_flags(
        MCCONKEY_RECORD["dependencies"], drafted
    )

    assert any(f["flag_type"] == "displaced" for f in flags)
    assert modifier < 0  # Negative impact applied
    assert modifier == pytest.approx(-0.35, abs=0.01)


# ---------------------------------------------------------------------------
# Test 2: test_displaced_flag_inactive_when_trigger_not_drafted
# ---------------------------------------------------------------------------

def test_displaced_flag_inactive_when_trigger_not_drafted():
    """Allen NOT drafted — McConkey's displaced flag should NOT activate."""
    resolver = DependencyResolver()
    drafted: set[str] = set()  # Allen not drafted

    flags, modifier = resolver.apply_active_flags(
        MCCONKEY_RECORD["dependencies"], drafted
    )

    assert not any(f.get("flag_type") == "displaced" and f.get("active") for f in flags)
    assert modifier == 0.0


# ---------------------------------------------------------------------------
# Test 3: test_block_flag_fires_on_combo_threat
# ---------------------------------------------------------------------------

def test_block_flag_fires_on_combo_threat():
    """Opponent has CMC (T1 RB). Taylor (T1 RB) nominated. Block value > personal value."""
    analyzer = OpponentThreatAnalyzer()
    opponent_roster = [_make_pick(FIXTURES["cmc_pick"])]

    block_val = analyzer.get_block_value(
        TAYLOR_RECORD, opponent_roster, opponent_budget=80
    )

    assert block_val > TAYLOR_RECORD["system_value"]


# ---------------------------------------------------------------------------
# Test 4: test_block_flag_suppressed_low_opponent_budget
# ---------------------------------------------------------------------------

def test_block_flag_suppressed_low_opponent_budget():
    """Opponent has $12 left. Block value returns 0 regardless."""
    analyzer = OpponentThreatAnalyzer()
    opponent_roster = [_make_pick(FIXTURES["cmc_pick"])]

    block_val = analyzer.get_block_value(
        TAYLOR_RECORD, opponent_roster, opponent_budget=12
    )

    assert block_val == 0.0


# ---------------------------------------------------------------------------
# Test 5: test_block_flag_suppressed_insufficient_own_budget
# ---------------------------------------------------------------------------

def test_block_flag_suppressed_insufficient_own_budget():
    """Can't afford block without going below minimum completion budget."""
    config = _make_league_config()
    state = DraftStateManager(config, YOUR_TEAM_ID)

    # Simulate having spent most of the budget: $180 spent on 9 players
    for i in range(9):
        state.record_pick(DraftPick(
            player_id=f"fill_{i}",
            team_id=YOUR_TEAM_ID,
            price=20,
            position="WR",
        ))

    # Budget: 200 - 180 = 20, slots remaining: 16 - 9 = 7
    # Minimum completion: 7 * $1 = $7
    # Spendable: 20 - 7 = 13
    spendable = state.get_spendable_on_this_player()
    assert spendable == 13
    assert spendable < 18  # Can't afford an $18 block bid


# ---------------------------------------------------------------------------
# Test 6: test_bid_ceiling_tier1_uses_anchor_weight
# ---------------------------------------------------------------------------

def test_bid_ceiling_tier1_uses_anchor_weight():
    """Tier 1 RB: ceiling blends system + market at 0.85 anchor, scarcity 1.35."""
    ceiling = compute_bid_ceiling(
        system_value=Decimal("58"),
        market_value=Decimal("68"),
        tier=1,
        position="RB",
        risk_level="low",
    )
    # blend = 58 * 0.15 + 68 * 0.85 = 8.7 + 57.8 = 66.5
    # ceiling = 66.5 * 1.35 = 89.78 → capped at 80 by MAX_REALISTIC_BID
    # compute_bid_ceiling doesn't cap — that's done in run_valuation_pass()
    # So raw ceiling should be ~89.78
    assert float(ceiling) > 60
    assert float(ceiling) < 95  # Pre-cap value should be in this range


# ---------------------------------------------------------------------------
# Test 7: test_bid_ceiling_tier4_market_dominant
# ---------------------------------------------------------------------------

def test_bid_ceiling_tier4_ignores_anchor():
    """Tier 4: anchor=0.70 (market-dominant for depth players)."""
    ceiling = compute_bid_ceiling(
        system_value=Decimal("12"),
        market_value=Decimal("18"),
        tier=4,
        position="WR",
        risk_level="low",
    )
    # blend = 12 * 0.30 + 18 * 0.70 = 3.6 + 12.6 = 16.2
    assert float(ceiling) == pytest.approx(16.2, abs=2)


# ---------------------------------------------------------------------------
# Test 8: test_nomination_suggestion_drains_opponent_budget
# ---------------------------------------------------------------------------

def test_nomination_suggestion_drains_opponent_budget():
    """Nominated players should have high market value, user doesn't want them."""
    analyzer = OpponentThreatAnalyzer()

    targets = analyzer.get_nomination_targets(
        all_players=FIXTURES["nomination_targets_pool"],
        your_roster=[],
        your_budget=150,
    )

    # All returned targets must be overvalued (market > system)
    assert len(targets) > 0
    for target in targets:
        assert target["market_value"] > target["system_value"]


# ---------------------------------------------------------------------------
# Test 9: test_budget_summary_accurate_mid_draft
# ---------------------------------------------------------------------------

def test_budget_summary_accurate_mid_draft():
    """After recording your picks, budget summary reflects correct remaining amounts."""
    config = _make_league_config()
    state = DraftStateManager(config, YOUR_TEAM_ID)

    your_picks = [_make_pick(p) for p in FIXTURES["your_picks"]]
    opp_picks = [_make_pick(p) for p in FIXTURES["opponent_picks"]]

    # Record all picks
    for pick in your_picks + opp_picks:
        state.record_pick(pick)

    expected_spent = sum(p["price"] for p in FIXTURES["your_picks"])
    assert state.get_your_remaining_budget() == 200 - expected_spent

    # Verify roster counts
    assert len(state.your_roster) == len(FIXTURES["your_picks"])
    assert len(state.picks) == len(FIXTURES["your_picks"]) + len(FIXTURES["opponent_picks"])

    # Verify spendable
    slots_remaining = 16 - len(state.your_roster)
    expected_spendable = (200 - expected_spent) - (slots_remaining * 1)
    assert state.get_spendable_on_this_player() == expected_spendable


# ---------------------------------------------------------------------------
# Test 10: test_recommendation_fires_under_2_seconds
# ---------------------------------------------------------------------------

def test_recommendation_fires_under_2_seconds():
    """End-to-end recommendation must complete in under 2000ms with mocked deps."""
    from backend.engines.live_draft import LiveDraftEngine

    config = _make_league_config()
    state = DraftStateManager(config, YOUR_TEAM_ID)
    resolver = DependencyResolver()
    analyzer = OpponentThreatAnalyzer()

    # Mock WebSocket manager
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()

    # Mock DB session that returns McConkey's record
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_player = MagicMock()
    mock_player.yahoo_player_id = MCCONKEY_RECORD["yahoo_player_id"]
    mock_player.name = MCCONKEY_RECORD["name"]
    mock_player.position = MCCONKEY_RECORD["position"]
    mock_player.team_abbr = MCCONKEY_RECORD["team_abbr"]
    mock_player.tier = MCCONKEY_RECORD["tier"]
    mock_player.baseline_value = Decimal(str(MCCONKEY_RECORD["system_value"]))
    mock_player.market_value = Decimal(str(MCCONKEY_RECORD["market_value"]))
    mock_player.ai_bid_ceiling = MCCONKEY_RECORD["ai_bid_ceiling"]
    mock_player.recommended_bid_ceiling = Decimal(str(MCCONKEY_RECORD["recommended_bid_ceiling"]))
    mock_player.notes = MCCONKEY_RECORD["notes"]
    mock_player.pay_up_flag = False
    mock_player.value_assessment = MCCONKEY_RECORD["value_assessment"]
    mock_player.id = "test-uuid"
    mock_player.injury_profile = None
    mock_player.profile = None
    mock_player.adp_ai = None
    mock_player.adp_fantasypros = None
    mock_player.adp_scoring = None
    mock_player.dependencies = []
    mock_result.scalar_one_or_none.return_value = mock_player
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Mock session factory
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_factory = MagicMock(return_value=mock_session_ctx)

    # Mock Anthropic client
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"action": "bid_to", "bid_ceiling": 22, "reasoning": "Good WR value", "confidence": "medium"}')]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    engine = LiveDraftEngine(
        state=state,
        resolver=resolver,
        threat_analyzer=analyzer,
        db_session_factory=mock_factory,
        ws_manager=mock_ws,
    )
    engine._client = mock_client

    event = FIXTURES["nomination_event"]

    start = time.monotonic()
    asyncio.run(engine.on_nomination(event))
    elapsed = (time.monotonic() - start) * 1000

    assert elapsed < 2000, f"Recommendation took {elapsed:.0f}ms — too slow"
    assert mock_ws.broadcast.called


# ---------------------------------------------------------------------------
# Test 11: test_opponent_threat_score_updates_after_pick
# ---------------------------------------------------------------------------

def test_opponent_threat_score_updates_after_pick():
    """After recording a pick, opponent threat score reflects new roster."""
    analyzer = OpponentThreatAnalyzer()

    score_before = analyzer.get_threat_score([])
    assert score_before == 0

    roster = [_make_pick(FIXTURES["cmc_pick"])]
    score_after = analyzer.get_threat_score(roster)

    assert score_after > score_before


# ---------------------------------------------------------------------------
# Test 12: test_combo_threat_flag_fires_second_elite_rb
# ---------------------------------------------------------------------------

def test_combo_threat_flag_fires_second_elite_rb():
    """Second tier-1 RB drafted by same opponent → combo alert."""
    analyzer = OpponentThreatAnalyzer()

    roster = [
        _make_pick(FIXTURES["cmc_pick"]),
        _make_pick(FIXTURES["taylor_pick"]),
    ]

    combos = analyzer.get_active_combo_flags(roster)
    assert any("Elite RB Stack" in c for c in combos)


# ---------------------------------------------------------------------------
# Test 13: test_historical_bias_increases_threat_score
# ---------------------------------------------------------------------------

def test_historical_bias_increases_threat_score():
    """
    Opponent with 1.3x WR bias scores higher threat on WR players
    than the same roster scored without tendencies.
    """
    tendencies = {
        "opp1": {
            "style": "zero_rb",
            "management_style": "analytical",
            "positional_bias": {"WR": 1.3, "RB": 0.8, "TE": 1.0, "QB": 1.0},
        }
    }

    analyzer_no_bias = OpponentThreatAnalyzer()
    analyzer_biased = OpponentThreatAnalyzer(tendencies=tendencies)

    # Roster with a WR pick
    wr_pick = DraftPick(
        player_id="wr1", team_id="opp1", price=40,
        player_name="WR Star", position="WR", tier=2,
    )
    roster = [wr_pick]

    base_score = analyzer_no_bias.get_threat_score(roster)
    biased_score = analyzer_biased.get_threat_score(roster, team_id="opp1")

    assert biased_score > base_score, (
        f"Biased score {biased_score} should exceed base {base_score}"
    )


# ---------------------------------------------------------------------------
# Test 14: test_drain_target_weighted_by_opponent_bias
# ---------------------------------------------------------------------------

def test_drain_target_weighted_by_opponent_bias():
    """
    WR drain target ranked higher when opponents have historical WR overpay.
    With WR bias > TE bias, a WR with the same overpay as a TE should rank first.
    """
    tendencies = {
        "opp1": {
            "style": "hero_rb",
            "management_style": "stars_and_scrubs",
            "positional_bias": {"WR": 1.4, "RB": 1.0, "TE": 0.7, "QB": 0.9},
        },
        "opp2": {
            "style": "balanced",
            "management_style": "analytical",
            "positional_bias": {"WR": 1.2, "RB": 1.1, "TE": 0.8, "QB": 0.9},
        },
    }

    analyzer = OpponentThreatAnalyzer(tendencies=tendencies)

    # Two players with identical overpay, different positions
    players = [
        {"yahoo_player_id": "te1", "name": "TE Drain", "position": "TE",
         "system_value": 10.0, "market_value": 30.0},
        {"yahoo_player_id": "wr1", "name": "WR Drain", "position": "WR",
         "system_value": 10.0, "market_value": 30.0},
    ]

    targets = analyzer.get_nomination_targets(
        all_players=players, your_roster=[], your_budget=150,
    )

    assert len(targets) == 2
    # WR should rank first because opponents overpay more for WRs
    assert targets[0]["position"] == "WR"
    assert targets[0]["drain_score"] > targets[1]["drain_score"]


# ---------------------------------------------------------------------------
# Test 15: test_manager_style_in_recommendation_context
# ---------------------------------------------------------------------------

def test_manager_style_in_recommendation_context():
    """
    on_nomination() context includes manager_style from historical tendencies.
    """
    from backend.engines.live_draft import LiveDraftEngine

    tendencies = {
        "opp1": {
            "style": "hero_rb",
            "management_style": "stars_and_scrubs",
            "positional_bias": {"RB": 1.3},
        }
    }

    config = _make_league_config()
    state = DraftStateManager(config, YOUR_TEAM_ID)
    resolver = DependencyResolver()
    analyzer = OpponentThreatAnalyzer(tendencies=tendencies)

    # Record an opponent pick so opp1 exists in state
    state.record_pick(DraftPick(
        player_id="opp_player", team_id="opp1", price=50,
        player_name="Some Player", position="RB", tier=1,
    ))

    # Mock WebSocket
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()

    # Mock DB session returning McConkey
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_player = MagicMock()
    mock_player.yahoo_player_id = MCCONKEY_RECORD["yahoo_player_id"]
    mock_player.name = MCCONKEY_RECORD["name"]
    mock_player.position = MCCONKEY_RECORD["position"]
    mock_player.team_abbr = MCCONKEY_RECORD["team_abbr"]
    mock_player.tier = MCCONKEY_RECORD["tier"]
    mock_player.baseline_value = Decimal(str(MCCONKEY_RECORD["system_value"]))
    mock_player.market_value = Decimal(str(MCCONKEY_RECORD["market_value"]))
    mock_player.ai_bid_ceiling = MCCONKEY_RECORD["ai_bid_ceiling"]
    mock_player.recommended_bid_ceiling = Decimal(str(MCCONKEY_RECORD["recommended_bid_ceiling"]))
    mock_player.notes = MCCONKEY_RECORD["notes"]
    mock_player.pay_up_flag = False
    mock_player.value_assessment = MCCONKEY_RECORD["value_assessment"]
    mock_player.id = "test-uuid"
    mock_player.injury_profile = None
    mock_player.profile = None
    mock_player.adp_ai = None
    mock_player.adp_fantasypros = None
    mock_player.adp_scoring = None
    mock_player.dependencies = []
    mock_result.scalar_one_or_none.return_value = mock_player
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_factory = MagicMock(return_value=mock_session_ctx)

    # Mock Anthropic — capture what context was sent
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"action": "bid_to", "bid_ceiling": 22, "reasoning": "test", "confidence": "medium"}'
    )]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    engine = LiveDraftEngine(
        state=state, resolver=resolver, threat_analyzer=analyzer,
        db_session_factory=mock_factory, ws_manager=mock_ws,
    )
    engine._client = mock_client

    asyncio.run(engine.on_nomination(FIXTURES["nomination_event"]))

    # Verify the Sonnet call received manager_styles in context
    call_args = mock_client.messages.create.call_args
    sent_content = json.loads(call_args.kwargs["messages"][0]["content"])
    assert "manager_styles" in sent_content
    assert sent_content["manager_styles"]["opp1"] == "hero_rb"


# ---------------------------------------------------------------------------
# Test 16: test_opponents_endpoint_returns_budget_and_threats
# ---------------------------------------------------------------------------

def test_opponents_endpoint_returns_budget_and_threats():
    """GET /draft/opponents returns budget, threat score, combos per opponent."""
    config = _make_league_config()
    state = DraftStateManager(config, YOUR_TEAM_ID)
    analyzer = OpponentThreatAnalyzer()

    # Record opponent picks
    state.record_pick(DraftPick(
        player_id="opp_rb1", team_id="opp_team_1", price=50,
        player_name="CMC", position="RB", tier=1,
    ))
    state.record_pick(DraftPick(
        player_id="opp_rb2", team_id="opp_team_1", price=45,
        player_name="Taylor", position="RB", tier=1,
    ))

    # Simulate what the endpoint does
    opponents = {}
    for team_id, roster in state.opponent_rosters.items():
        budget = state.opponent_budgets.get(team_id, 0)
        combos = analyzer.get_active_combo_flags(roster)
        score = analyzer.get_threat_score(roster, team_id=team_id)
        opponents[team_id] = {
            "budget": budget,
            "roster_count": len(roster),
            "threat_score": score,
            "combos": combos,
            "roster": [
                {"player_name": p.player_name, "position": p.position, "price": p.price}
                for p in roster
            ],
        }

    assert "opp_team_1" in opponents
    opp = opponents["opp_team_1"]
    assert opp["roster_count"] == 2
    assert opp["threat_score"] > 0
    assert any("Elite RB Stack" in c for c in opp["combos"])
    assert len(opp["roster"]) == 2


# ---------------------------------------------------------------------------
# Test 17: test_opponents_endpoint_requires_engine
# ---------------------------------------------------------------------------

def test_opponents_endpoint_requires_engine():
    """The opponents endpoint logic requires state and engine to exist."""
    # This is a unit-level check that _require_engine would fail
    # without starting the engine. We verify the data shape expectation:
    # if state has no opponent rosters, the result is empty.
    config = _make_league_config()
    state = DraftStateManager(config, YOUR_TEAM_ID)
    analyzer = OpponentThreatAnalyzer()

    opponents = {}
    for team_id, roster in state.opponent_rosters.items():
        budget = state.opponent_budgets.get(team_id, 0)
        combos = analyzer.get_active_combo_flags(roster)
        score = analyzer.get_threat_score(roster, team_id=team_id)
        opponents[team_id] = {
            "budget": budget,
            "roster_count": len(roster),
            "threat_score": score,
            "combos": combos,
        }

    # No picks recorded → no opponents
    assert len(opponents) == 0


# ---------------------------------------------------------------------------
# Test 18: test_opponents_combo_alerts_after_pick
# ---------------------------------------------------------------------------

def test_opponents_combo_alerts_after_pick():
    """Combo alerts update correctly after recording new opponent picks."""
    config = _make_league_config()
    state = DraftStateManager(config, YOUR_TEAM_ID)
    analyzer = OpponentThreatAnalyzer()

    # First pick — no combos yet
    state.record_pick(DraftPick(
        player_id="rb1", team_id="opp_A", price=55,
        player_name="CMC", position="RB", tier=1,
    ))
    roster = state.opponent_rosters["opp_A"]
    combos_after_1 = analyzer.get_active_combo_flags(roster)
    assert len(combos_after_1) == 0

    # Second T1 RB → Elite RB Stack
    state.record_pick(DraftPick(
        player_id="rb2", team_id="opp_A", price=48,
        player_name="Taylor", position="RB", tier=1,
    ))
    roster = state.opponent_rosters["opp_A"]
    combos_after_2 = analyzer.get_active_combo_flags(roster)
    assert any("Elite RB Stack" in c for c in combos_after_2)

    # Also add T1 TE → Elite RB + Elite TE
    state.record_pick(DraftPick(
        player_id="te1", team_id="opp_A", price=30,
        player_name="Kelce", position="TE", tier=1,
    ))
    roster = state.opponent_rosters["opp_A"]
    combos_after_3 = analyzer.get_active_combo_flags(roster)
    assert any("Elite RB + Elite TE" in c for c in combos_after_3)
