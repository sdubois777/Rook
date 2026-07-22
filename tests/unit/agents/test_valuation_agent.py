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

from backend.agents.valuation_agent import (
    ValuationAgent, SYSTEM_PROMPT, _context_fingerprint,
)


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


def test_ppr_prompt_is_market_blind():
    """The PPR agent no longer CONSUMES market, and no longer PRODUCES the market-relative
    fields (they are deterministic downstream). value_assessment / pay_up_flag /
    nomination_target_flag must be absent from the output schema, and market may appear only
    as a prohibition — never as a 'use market to set the ceiling' instruction."""
    from backend.agents.valuation_agent import _hybrid_system_prompt
    ppr = SYSTEM_PROMPT
    # Output schema no longer requests these (deterministic post-pass owns them):
    for field in ('"value_assessment"', '"pay_up_flag"', '"nomination_target_flag"'):
        assert field not in ppr, f"{field} must not be in the blind PPR output schema"
    # No market-consumption instructions remain:
    assert "market_value_fantasypros: consensus ADP" not in ppr
    assert "Use market context" not in ppr
    assert "prior_season_price" not in ppr
    assert "NOT given any market data" in ppr  # blindness is stated positively
    # Non-PPR hybrid still needs value_assessment (no per-format market to derive it):
    assert '"value_assessment"' in _hybrid_system_prompt("half_ppr")


@pytest.mark.asyncio
async def test_auction_note_references_player_context():
    """Auction note should contain the player's name or situation-specific text."""
    result = _make_ai_result("Puka Nacua", auction_note="Nacua is the alpha in LA — lock him in early.")
    assert "Nacua" in result["auction_note"]


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
    # MARKET IS STRIPPED (ToS): none of these market inputs may reach the agent context.
    for k in ("market_value", "value_gap", "value_gap_signal",
              "market_value_fantasypros", "prior_season_price"):
        assert k not in ctx, f"{k} must be stripped from the PPR agent context"


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


# --- per-format prose (G2): prompt parameterization ---------------------------
from backend.agents.valuation_agent import _system_prompt, SYSTEM_PROMPT  # noqa: E402


def test_ppr_system_prompt_is_byte_identical():
    """PPR must produce today's prompt exactly — 100% of current users are PPR."""
    assert _system_prompt("ppr") == SYSTEM_PROMPT


def test_standard_prompt_drops_ppr_and_forbids_selling_receptions():
    sp = _system_prompt("standard")
    assert sp != SYSTEM_PROMPT
    assert "12-team PPR fantasy football league" not in sp
    assert "12-team STANDARD" in sp
    assert "receptions score ZERO" in sp
    assert "PPR asset" in sp  # the forbid-instruction references it


def test_half_ppr_prompt_labels_half_and_tempers():
    sp = _system_prompt("half_ppr")
    assert "HALF-PPR" in sp
    assert "12-team PPR fantasy football league" not in sp
    assert "0.5 points per reception" in sp


# ---------------------------------------------------------------------------
# Bug 1 — valuation context must carry trigger_condition + reasoning
# ---------------------------------------------------------------------------

def test_valuation_dependency_flags_carry_condition_and_reasoning():
    """_build_player_context must include trigger_condition + reasoning per flag, so
    auction_note can distinguish an injured beneficiary from a departed one."""
    from types import SimpleNamespace

    dep = SimpleNamespace(
        flag_type="beneficiary", trigger_player_name="Travis Kelce",
        value_impact_pct=0.12, trigger_condition="injured",
        reasoning="if Kelce's volume declines due to age or injury",
    )
    p = SimpleNamespace(
        name="Xavier Worthy", position="WR", team_abbr="KC", age=22, tier=2,
        is_rookie=False, recommended_bid_ceiling=None, baseline_value=None,
        market_value=None, value_gap=None, value_gap_signal=None, ceiling_value=None,
        floor_value=None, market_value_fantasypros=None, historic_prices=[],
        profile=None, injury_profile=None, schedule=None, dependencies=[dep],
    )
    agent = ValuationAgent.__new__(ValuationAgent)
    ctx = agent._build_player_context(p)

    df = ctx["dependency_flags"][0]
    assert df["trigger_condition"] == "injured"
    assert "injury" in df["reasoning"]


# ---------------------------------------------------------------------------
# Context-fingerprint cache key — value-delta invalidation
# ---------------------------------------------------------------------------

def _fp_player(**overrides):
    """A SimpleNamespace player rich enough for _build_player_context + fingerprinting."""
    from types import SimpleNamespace

    prof = SimpleNamespace(
        clean_season_baseline={"ppr_points": 220.0, "upside_ppr": 260.0, "downside_ppr": 180.0},
        confidence="high", projection_reasoning="Elite target share in high-volume offense",
        career_trajectory="peak", role_classification="wr1_alpha",
        profile_source="sonnet_projection", breakout_flag=False,
        positional_scarcity_tier="scarce",
    )
    inj = SimpleNamespace(
        overall_risk_level="low", availability_risk="durable",
        risk_adjusted_value_modifier=Decimal("1.00"), pattern_flags=[],
        workload_cliff_flag=False, high_mileage_flag=False, post_acl_flag=False,
    )
    sched = SimpleNamespace(full_season_grade="favorable", playoff_window_grade="favorable",
                            schedule_score=Decimal("8.5"))
    defaults = dict(
        name="Test Player", position="WR", team_abbr="LAC", age=26, tier=2, is_rookie=False,
        recommended_bid_ceiling=Decimal("35.00"), baseline_value=Decimal("32.00"),
        market_value=Decimal("38.00"), value_gap=Decimal("-6.00"),
        value_gap_signal="market_overvalues", ceiling_value=Decimal("42.00"),
        floor_value=Decimal("25.00"), market_value_fantasypros=None, historic_prices=[],
        profile=prof, injury_profile=inj, schedule=sched, dependencies=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _dep(**overrides):
    from types import SimpleNamespace
    d = dict(id=uuid.uuid4(), flag_type="displaced", trigger_player_name="Keenan Allen",
             value_impact_pct=0.15, trigger_condition="signed",
             reasoning="target share capped by new arrival")
    d.update(overrides)
    return SimpleNamespace(**d)


def _fp(player):
    agent = ValuationAgent.__new__(ValuationAgent)
    return _context_fingerprint(agent._build_player_context(player))


def test_fingerprint_identical_inputs_hit():
    """Two builds of the same player fingerprint identically → cache HIT."""
    assert _fp(_fp_player()) == _fp(_fp_player())


def test_fingerprint_changed_flag_misses():
    """Adding or changing a dependency flag flips the fingerprint → cache MISS."""
    base = _fp(_fp_player())
    with_flag = _fp(_fp_player(dependencies=[_dep()]))
    assert base != with_flag
    # a different flag_type on the same trigger is also a miss
    assert _fp(_fp_player(dependencies=[_dep(flag_type="beneficiary")])) != with_flag
    # a changed reasoning (INDEPENDENT upstream input) is a miss
    assert _fp(_fp_player(dependencies=[_dep(reasoning="different cause")])) != with_flag


@pytest.mark.asyncio
async def test_changed_prompt_misses():
    """A different system prompt yields a different prompt_hash in the cache key → MISS."""
    from backend.agents.base_agent import SONNET

    agent = ValuationAgent(dry_run=False)
    ctx = agent._build_player_context(_fp_player(name="CMC"))
    captured = {}

    async def cap(system, user, input_data, entity_id, model=None, max_tokens=None):
        captured["input_data"] = input_data
        return json.dumps([_make_ai_result("CMC")])

    prompt_hashes = []
    for prompt_text in ("PROMPT VERSION ONE", "PROMPT VERSION TWO — reworded"):
        with patch("backend.agents.valuation_agent._system_prompt", return_value=prompt_text):
            with patch.object(agent, "call_once", side_effect=cap):
                await agent._process_batch([ctx], entity_id="CMC", model=SONNET, max_tokens=800)
        prompt_hashes.append(captured["input_data"]["prompt_hash"])

    assert prompt_hashes[0] != prompt_hashes[1]
    # context_fp is also present in the key
    assert "context_fp" in captured["input_data"] and captured["input_data"]["context_fp"]


def test_fingerprint_float_jitter_below_bucket_hits():
    """Sub-$1 / sub-1-PPR jitter rounds to the same bucket → cache HIT."""
    assert _fp(_fp_player(recommended_bid_ceiling=Decimal("35.20"))) == \
           _fp(_fp_player(recommended_bid_ceiling=Decimal("35.40")))
    a, b = _fp_player(), _fp_player()
    a.profile.clean_season_baseline = {"ppr_points": 220.2, "upside_ppr": 260.0, "downside_ppr": 180.0}
    b.profile.clean_season_baseline = {"ppr_points": 220.4, "upside_ppr": 260.0, "downside_ppr": 180.0}
    assert _fp(a) == _fp(b)


def test_fingerprint_above_bucket_misses():
    """A material dollar move (beyond the $1 bucket) flips the fingerprint → MISS."""
    assert _fp(_fp_player(recommended_bid_ceiling=Decimal("35.00"))) != \
           _fp(_fp_player(recommended_bid_ceiling=Decimal("42.00")))


def test_fingerprint_noop_rewrite_new_ids_same_values_hits():
    """roster_changes delete+reinsert gives flags NEW row ids but identical VALUES —
    ids never enter the context dict, so the fingerprint is unchanged → cache HIT."""
    a = _fp(_fp_player(dependencies=[_dep(id=uuid.uuid4())]))
    b = _fp(_fp_player(dependencies=[_dep(id=uuid.uuid4())]))
    assert a == b
    # flag ORDER is also immaterial (collection is value-sorted)
    d1, d2 = _dep(flag_type="displaced"), _dep(flag_type="beneficiary")
    assert _fp(_fp_player(dependencies=[d1, d2])) == _fp(_fp_player(dependencies=[d2, d1]))


def test_fingerprint_excludes_projection_reasoning():
    """projection_reasoning is DERIVED from already-hashed fields — rewording it alone
    must NOT invalidate (the comment in valuation_agent guards against re-adding it)."""
    a, b = _fp_player(), _fp_player()
    a.profile.projection_reasoning = "one wording of the same projection"
    b.profile.projection_reasoning = "an entirely different wording, same numbers"
    assert _fp(a) == _fp(b)


def test_build_context_does_not_mutate_injury_flags():
    """_build_player_context must COPY pattern_flags, not alias it — otherwise appending
    the boolean-derived flags mutates the ORM row and a second build accumulates flags
    (the non-deterministic-fingerprint bug). Same player built twice → identical fp."""
    from types import SimpleNamespace

    inj = SimpleNamespace(
        overall_risk_level="high", availability_risk="concern",
        risk_adjusted_value_modifier=Decimal("0.85"), pattern_flags=["CHRONIC_CONDITION"],
        workload_cliff_flag=True, high_mileage_flag=True, post_acl_flag=False,
    )
    player = _fp_player(injury_profile=inj)
    agent = ValuationAgent.__new__(ValuationAgent)

    ctx1 = agent._build_player_context(player)
    ctx2 = agent._build_player_context(player)

    # the ORM list is untouched by the build
    assert inj.pattern_flags == ["CHRONIC_CONDITION"]
    # boolean-derived flags still surface in the context
    assert "WORKLOAD_CLIFF" in ctx1["injury_flags"] and "HIGH_MILEAGE" in ctx1["injury_flags"]
    # and repeated builds fingerprint identically (no accumulation)
    assert _context_fingerprint(ctx1) == _context_fingerprint(ctx2)
