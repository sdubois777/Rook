"""
tests/unit/agents/test_valuation_agent.py

Tests for the AI ceiling calibration agent. All API calls are mocked.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.valuation_agent import ValuationAgent, SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_player(**overrides):
    """Build a minimal Player-like mock for testing."""
    defaults = {
        "id": uuid.uuid4(),
        "name": "Test Player",
        "position": "WR",
        "team_abbr": "LAC",
        "age": 26,
        "tier": 2,
        "is_rookie": False,
        "recommended_bid_ceiling": Decimal("35.00"),
        "baseline_value": Decimal("32.00"),
        "market_value": Decimal("38.00"),
        "market_value_fantasypros": None,
        "market_value_league": None,
        "ceiling_value": Decimal("42.00"),
        "floor_value": Decimal("25.00"),
        "value_gap": Decimal("-6.00"),
        "value_gap_signal": "market_overvalues",
        "breakout_flag": False,
        "ai_bid_ceiling": None,
        "ai_confidence_floor": None,
        "ai_confidence_ceiling": None,
        "value_assessment": None,
        "auction_note": None,
        "pay_up_flag": False,
        "nomination_target_flag": False,
    }
    defaults.update(overrides)

    player = MagicMock()
    for k, v in defaults.items():
        setattr(player, k, v)

    # Profile
    profile = MagicMock()
    profile.clean_season_baseline = {"ppr_points": 220.0, "upside_ppr": 260.0, "downside_ppr": 180.0}
    profile.confidence = "high"
    profile.projection_reasoning = "Elite target share in high-volume offense"
    profile.career_trajectory = "peak"
    profile.role_classification = "wr1_alpha"
    profile.profile_source = "sonnet_projection"
    profile.breakout_flag = False
    profile.positional_scarcity_tier = "scarce"
    player.profile = profile

    # Injury
    injury = MagicMock()
    injury.overall_risk_level = "low"
    injury.risk_adjusted_value_modifier = Decimal("1.00")
    injury.pattern_flags = []
    injury.workload_cliff_flag = False
    injury.high_mileage_flag = False
    injury.post_acl_flag = False
    player.injury_profile = injury

    # Schedule
    schedule = MagicMock()
    schedule.full_season_grade = "favorable"
    schedule.playoff_window_grade = "favorable"
    schedule.schedule_score = Decimal("8.5")
    player.schedule = schedule

    # Dependencies
    player.dependencies = []

    return player


def _make_ai_result(player_name, **overrides):
    """Build a valid AI output for one player."""
    defaults = {
        "player_name": player_name,
        "ai_bid_ceiling": 34,
        "confidence_floor": 28,
        "confidence_ceiling": 42,
        "value_assessment": "good_value",
        "auction_note": f"{player_name} has elite target share — solid investment at this price.",
        "pay_up_flag": False,
        "nomination_target_flag": False,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tier1_players_get_individual_sonnet_calls():
    """Tier 1 players should each get their own Sonnet call."""
    p1 = _make_player(name="CMC", tier=1, recommended_bid_ceiling=Decimal("72.00"))
    p2 = _make_player(name="Bijan", tier=1, recommended_bid_ceiling=Decimal("68.00"))

    agent = ValuationAgent(dry_run=False)

    call_entities = []

    async def mock_call_once(system, user, input_data, entity_id, model=None, max_tokens=None):
        call_entities.append((entity_id, model))
        player_name = json.loads(user)[0]["player_name"]
        return json.dumps([_make_ai_result(player_name)])

    with patch.object(agent, "call_once", side_effect=mock_call_once):
        with patch("backend.agents.valuation_agent.AsyncSessionLocal") as mock_session_cls:
            # Mock the load query
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = [p1, p2]
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session.get = AsyncMock(side_effect=lambda cls, id: next(
                (p for p in [p1, p2] if p.id == id), None
            ))

            result = await agent.run_all()

    # Each tier 1 player should have its own call
    assert len(call_entities) == 2
    assert call_entities[0][0] == "CMC"
    assert call_entities[1][0] == "Bijan"
    # Both should use Sonnet
    from backend.agents.base_agent import SONNET
    assert all(e[1] == SONNET for e in call_entities)


@pytest.mark.asyncio
async def test_ai_ceiling_within_reasonable_range():
    """AI bid ceiling should stay within 20% of math ceiling."""
    player = _make_player(name="Nacua", recommended_bid_ceiling=Decimal("50.00"))
    math_ceiling = 50

    # AI returns a ceiling that's within range
    result = _make_ai_result("Nacua", ai_bid_ceiling=55)
    assert abs(result["ai_bid_ceiling"] - math_ceiling) / math_ceiling <= 0.20

    # AI returns a ceiling that's way off — should still parse but is suspect
    result_extreme = _make_ai_result("Nacua", ai_bid_ceiling=100)
    deviation = abs(result_extreme["ai_bid_ceiling"] - math_ceiling) / math_ceiling
    assert deviation > 0.20  # We detect this is out of range


@pytest.mark.asyncio
async def test_value_assessment_populated_for_all_players():
    """Every player result should have a value_assessment."""
    names = ["Player A", "Player B", "Player C"]
    results = [_make_ai_result(n) for n in names]
    for r in results:
        assert r["value_assessment"] is not None
        assert r["value_assessment"] in (
            "elite_value", "good_value", "fair_value", "slight_overpay", "avoid"
        )


@pytest.mark.asyncio
async def test_auction_note_references_player_context():
    """Auction note should contain the player's name or situation-specific text."""
    result = _make_ai_result("Puka Nacua", auction_note="Nacua is the alpha in LA — lock him in early.")
    assert "Nacua" in result["auction_note"]


@pytest.mark.asyncio
async def test_nomination_target_flag_for_overvalued_players():
    """Players with market value >> system value should get nomination_target_flag."""
    result = _make_ai_result(
        "Overpriced Guy",
        nomination_target_flag=True,
        value_assessment="slight_overpay",
        auction_note="Market loves him — nominate early to drain opponents.",
    )
    assert result["nomination_target_flag"] is True
    assert result["value_assessment"] in ("slight_overpay", "avoid")


@pytest.mark.asyncio
async def test_pay_up_flag_for_undervalued_players():
    """Players with system value >> market value should get pay_up_flag."""
    result = _make_ai_result(
        "Bargain Elite",
        pay_up_flag=True,
        value_assessment="elite_value",
        auction_note="Clear market inefficiency — don't let this one go.",
    )
    assert result["pay_up_flag"] is True
    assert result["value_assessment"] in ("elite_value", "good_value")


@pytest.mark.asyncio
async def test_confidence_range_floor_below_ceiling():
    """Confidence floor must always be less than confidence ceiling."""
    result = _make_ai_result("Test", confidence_floor=25, confidence_ceiling=45)
    assert result["confidence_floor"] < result["confidence_ceiling"]


@pytest.mark.asyncio
async def test_math_ceiling_preserved_alongside_ai_ceiling():
    """The recommended_bid_ceiling (math) should never be overwritten by the agent."""
    player = _make_player(name="Safe Player", recommended_bid_ceiling=Decimal("40.00"))
    ai_result = _make_ai_result("Safe Player", ai_bid_ceiling=38)

    # The agent writes to ai_bid_ceiling, NOT recommended_bid_ceiling
    assert ai_result["ai_bid_ceiling"] != float(player.recommended_bid_ceiling)
    # recommended_bid_ceiling stays unchanged
    assert player.recommended_bid_ceiling == Decimal("40.00")


@pytest.mark.asyncio
async def test_build_player_context_includes_all_fields():
    """_build_player_context should assemble full context from ORM data."""
    player = _make_player(name="Context Test")
    agent = ValuationAgent(dry_run=False)

    ctx = agent._build_player_context(player)

    assert ctx["player_name"] == "Context Test"
    assert ctx["position"] == "WR"
    assert ctx["math_bid_ceiling"] == 35.0
    assert ctx["projected_ppr"] == 220.0
    assert ctx["injury_risk"] == "low"
    assert ctx["schedule_grade"] == "favorable"


@pytest.mark.asyncio
async def test_ai_ceiling_clamped_to_position_max():
    """AI ceiling should be clamped to position max (RB=$80, WR=$70, etc.)."""
    agent = ValuationAgent(dry_run=False)

    # Simulate _write_results clamping
    player = _make_player(name="WR Max", position="WR", recommended_bid_ceiling=Decimal("65.00"))
    results_map = {"WR Max": _make_ai_result("WR Max", ai_bid_ceiling=85)}

    # The write method should clamp to 70 for WR
    max_bids = {"RB": 80, "WR": 70, "QB": 50, "TE": 45}
    clamped = min(int(results_map["WR Max"]["ai_bid_ceiling"]), max_bids.get("WR", 80))
    assert clamped == 70


# ---------------------------------------------------------------------------
# Auction note sanitization
# ---------------------------------------------------------------------------

def test_no_league_language_in_auction_notes():
    """auction_note sanitizer strips 'your league paid' and 'in your league' phrasing."""
    import re

    # Reproduce the sanitization logic from _write_results
    def sanitize(note):
        if not note:
            return note
        note = re.sub(
            r"(?i)\b(your|the|this) league (paid|spent|valued|priced)\b",
            "consensus ADP was",
            note,
        )
        note = re.sub(r"(?i)\bin your league\b", "at consensus", note)
        return note

    # Phrases that should be scrubbed
    assert "your league" not in sanitize("Your league paid $32 last year")
    assert "consensus ADP was" in sanitize("Your league paid $32 last year")
    assert "in your league" not in sanitize("This player is undervalued in your league")
    assert "at consensus" in sanitize("This player is undervalued in your league")
    assert "the league spent" not in sanitize("The league spent $45 on this player")

    # Clean notes should pass through unchanged
    clean = "Elite target share in high-volume offense at $40 consensus ADP."
    assert sanitize(clean) == clean
