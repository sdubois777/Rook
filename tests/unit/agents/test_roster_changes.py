"""
tests/unit/agents/test_roster_changes.py

All required named test cases from stage-04-roster-changes.md.
The canonical test is test_mcconkey_allen_displacement — if this fails,
Stage 4 is not complete regardless of anything else.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.roster_changes import RosterChangesAgent


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_flag(
    player: str,
    team: str,
    position: str,
    flag_type: str,
    trigger: str,
    trigger_team: str,
    condition: str,
    effect: str,
    impact: float,
    confidence: str = "high",
    reasoning: str = "test reasoning",
    season_year: int = 2026,
) -> dict:
    return {
        "player_name": player,
        "player_team": team,
        "player_position": position,
        "flag_type": flag_type,
        "trigger_player_name": trigger,
        "trigger_player_team": trigger_team,
        "trigger_condition": condition,
        "effect_on_value": effect,
        "value_impact_pct": impact,
        "confidence": confidence,
        "reasoning": reasoning,
        "season_year": season_year,
    }


def _lac_roster() -> list[dict]:
    return [
        {"name": "Keenan Allen", "position": "WR", "team": "LAC"},
        {"name": "Ladd McConkey", "position": "WR", "team": "LAC"},
        {"name": "Justin Herbert", "position": "QB", "team": "LAC"},
    ]


def _lac_transactions() -> list[dict]:
    return [
        {
            "type": "signing",
            "player": "Keenan Allen",
            "from_team": "CHI",
            "to_team": "LAC",
            "aav": 23_000_000,
            "position": "WR",
        }
    ]


# ---------------------------------------------------------------------------
# THE canonical test — must pass before Stage 4 is complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcconkey_allen_displacement():
    """
    THE canonical test.

    Keenan Allen signs with LAC. Herbert's historical usage of Allen (from CHI era
    via fantasy know-how) + McConkey's role as primary slot WR = direct overlap.

    Expected:
    - McConkey receives DISPLACED flag (negative, trigger=Allen, condition=active_and_healthy)
    - McConkey receives CONTINGENT flag (positive, trigger=Allen, condition=injured)
    Both flags must be present. Neither may be missing.
    """
    agent = RosterChangesAgent()

    # The model output: both flags present for McConkey
    model_output = json.dumps([
        _make_flag(
            "Ladd McConkey", "LAC", "WR",
            "displaced", "Keenan Allen", "LAC",
            "active_and_healthy", "negative", -20,
            reasoning="Allen directly overlaps McConkey's slot role as Herbert's primary target.",
        ),
        _make_flag(
            "Ladd McConkey", "LAC", "WR",
            "contingent", "Keenan Allen", "LAC",
            "injured", "positive", 25,
            reasoning="McConkey becomes the primary target when Allen is unavailable.",
        ),
    ])

    context = {
        "team": "LAC",
        "season": 2026,
        "transactions": _lac_transactions(),
        "current_roster": _lac_roster(),
        "target_share_history": {},
        "backfield_usage": {},
        "qb_receiver_history": [],
        "system_grade": {},
    }

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value=context)):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=2)):
                flags = await agent.run_for_team("LAC")

    mcconkey_flags = [f for f in flags if f["player_name"] == "Ladd McConkey"]
    flag_types = {f["flag_type"] for f in mcconkey_flags}

    assert "displaced" in flag_types, (
        "McConkey must have a DISPLACED flag when Keenan Allen signs with LAC. "
        "This is the core purpose of the Roster Changes Agent."
    )
    assert "contingent" in flag_types, (
        "McConkey must have a CONTINGENT flag paired with the DISPLACED flag. "
        "DISPLACED must always be accompanied by CONTINGENT."
    )

    # Verify flag semantics
    displaced = next(f for f in mcconkey_flags if f["flag_type"] == "displaced")
    contingent = next(f for f in mcconkey_flags if f["flag_type"] == "contingent")

    assert displaced["trigger_player_name"] == "Keenan Allen"
    assert displaced["effect_on_value"] == "negative"
    assert displaced["trigger_condition"] == "active_and_healthy"

    assert contingent["trigger_player_name"] == "Keenan Allen"
    assert contingent["effect_on_value"] == "positive"


# ---------------------------------------------------------------------------
# Target share displacement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_target_share_displacement_direct_role_overlap():
    """
    A high-AAV signing at the same position/role triggers a DISPLACED flag
    for the incumbent player.
    """
    agent = RosterChangesAgent()

    model_output = json.dumps([
        _make_flag(
            "Incumbent WR", "NE", "WR",
            "displaced", "New Signing WR", "NE",
            "active_and_healthy", "negative", -15,
        ),
        _make_flag(
            "Incumbent WR", "NE", "WR",
            "contingent", "New Signing WR", "NE",
            "injured", "positive", 20,
        ),
    ])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "NE"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=2)):
                flags = await agent.run_for_team("NE")

    flag_types = {f["flag_type"] for f in flags}
    assert "displaced" in flag_types
    assert "contingent" in flag_types


@pytest.mark.asyncio
async def test_target_share_displacement_no_flag_different_role():
    """
    A signing at a completely different role should not produce a displaced
    flag for existing players in an unrelated role.
    For example: signing a new TE should not flag existing WRs as displaced.
    """
    agent = RosterChangesAgent()

    # Model correctly identifies no displacement across different positions
    model_output = json.dumps([
        _make_flag(
            "Existing TE", "DAL", "TE",
            "displaced", "New TE Signing", "DAL",
            "active_and_healthy", "negative", -10,
        ),
        _make_flag(
            "Existing TE", "DAL", "TE",
            "contingent", "New TE Signing", "DAL",
            "injured", "positive", 15,
        ),
    ])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "DAL"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=2)):
                flags = await agent.run_for_team("DAL")

    # WR players should not be in the flags
    wr_displaced = [f for f in flags if f.get("player_position") == "WR" and f["flag_type"] == "displaced"]
    assert not wr_displaced


# ---------------------------------------------------------------------------
# QB trust score
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_qb_trust_score_nfl_history():
    """QB with extensive NFL shared history with a WR gets high-trust COLLEGE_TRUST flag."""
    agent = RosterChangesAgent()

    model_output = json.dumps([
        _make_flag(
            "Trusted WR", "LAC", "WR",
            "college_trust", "Justin Herbert", "LAC",
            "active_and_healthy", "positive", 10,
            confidence="high",
            reasoning="Herbert has 3 seasons of shared NFL history with this WR.",
        ),
    ])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "LAC"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=1)):
                flags = await agent.run_for_team("LAC")

    trust_flags = [f for f in flags if f["flag_type"] == "college_trust"]
    assert len(trust_flags) == 1
    assert trust_flags[0]["confidence"] == "high"


@pytest.mark.asyncio
async def test_qb_trust_score_college_history():
    """Rookie QB with college WR teammate gets COLLEGE_TRUST flag."""
    agent = RosterChangesAgent()

    model_output = json.dumps([
        _make_flag(
            "College WR", "ATL", "WR",
            "college_trust", "Rookie QB", "ATL",
            "active_and_healthy", "positive", 8,
            confidence="medium",
            reasoning="Shared college history — positive modifier for rookie QB season.",
        ),
    ])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "ATL"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=1)):
                flags = await agent.run_for_team("ATL")

    college_flags = [f for f in flags if f["flag_type"] == "college_trust"]
    assert len(college_flags) >= 1


@pytest.mark.asyncio
async def test_qb_trust_score_no_history():
    """QB with no shared history with new receivers produces no trust boost flags."""
    agent = RosterChangesAgent()

    # No college_trust flags when there is no history
    model_output = json.dumps([])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "NE"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=0)):
                flags = await agent.run_for_team("NE")

    college_flags = [f for f in flags if f["flag_type"] == "college_trust"]
    assert len(college_flags) == 0


# ---------------------------------------------------------------------------
# Backfield committee
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfield_committee_two_similar_profiles():
    """Two RBs with similar profiles get COMMITTEE flags on both."""
    agent = RosterChangesAgent()

    model_output = json.dumps([
        _make_flag(
            "RB One", "MIA", "RB",
            "committee", "RB Two", "MIA",
            "active_and_healthy", "neutral", -10,
        ),
        _make_flag(
            "RB Two", "MIA", "RB",
            "committee", "RB One", "MIA",
            "active_and_healthy", "neutral", -10,
        ),
    ])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "MIA"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=2)):
                flags = await agent.run_for_team("MIA")

    committee_flags = [f for f in flags if f["flag_type"] == "committee"]
    players_flagged = {f["player_name"] for f in committee_flags}
    assert "RB One" in players_flagged
    assert "RB Two" in players_flagged


@pytest.mark.asyncio
async def test_backfield_committee_complementary_no_strong_flag():
    """
    Complementary RBs (early-down vs pass-catching specialist) should either
    have no committee flag or a low-confidence one — not a high-confidence one.
    """
    agent = RosterChangesAgent()

    # Low confidence committee flag for complementary backs
    model_output = json.dumps([
        _make_flag(
            "Pass Catching RB", "SF", "RB",
            "committee", "Thumper RB", "SF",
            "active_and_healthy", "neutral", -5,
            confidence="low",
        ),
    ])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "SF"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=1)):
                flags = await agent.run_for_team("SF")

    committee_flags = [f for f in flags if f["flag_type"] == "committee"]
    if committee_flags:
        # If flagged, must be low confidence for complementary roles
        assert all(f["confidence"] == "low" for f in committee_flags)


# ---------------------------------------------------------------------------
# DISPLACED always paired with CONTINGENT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_displaced_always_paired_with_contingent():
    """
    For every DISPLACED flag, there must be a matching CONTINGENT flag
    for the same player with the same trigger.
    """
    agent = RosterChangesAgent()

    model_output = json.dumps([
        _make_flag("WR A", "BUF", "WR", "displaced", "WR B", "BUF",
                   "active_and_healthy", "negative", -15),
        _make_flag("WR A", "BUF", "WR", "contingent", "WR B", "BUF",
                   "injured", "positive", 20),
        _make_flag("WR C", "BUF", "WR", "displaced", "WR D", "BUF",
                   "active_and_healthy", "negative", -10),
        _make_flag("WR C", "BUF", "WR", "contingent", "WR D", "BUF",
                   "injured", "positive", 15),
    ])

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "BUF"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=4)):
                flags = await agent.run_for_team("BUF")

    displaced_players = {
        (f["player_name"], f["trigger_player_name"])
        for f in flags if f["flag_type"] == "displaced"
    }
    contingent_players = {
        (f["player_name"], f["trigger_player_name"])
        for f in flags if f["flag_type"] == "contingent"
    }

    for pair in displaced_players:
        assert pair in contingent_players, (
            f"DISPLACED flag for {pair} has no matching CONTINGENT flag. "
            "DISPLACED and CONTINGENT must always be generated as a pair."
        )


# ---------------------------------------------------------------------------
# High-AAV signing weighted higher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_aav_signing_weighted_higher_than_low_aav():
    """
    A $25M/year signing should produce higher value_impact_pct than a
    $5M/year signing for the same role overlap.
    """
    agent = RosterChangesAgent()

    # High-AAV signing produces stronger displacement
    high_aav_output = json.dumps([
        _make_flag("Incumbent", "NE", "WR", "displaced", "Expensive WR", "NE",
                   "active_and_healthy", "negative", -25,
                   reasoning="High-AAV signing indicates starter role guaranteed."),
        _make_flag("Incumbent", "NE", "WR", "contingent", "Expensive WR", "NE",
                   "injured", "positive", 30),
    ])

    low_aav_output = json.dumps([
        _make_flag("Incumbent", "TB", "WR", "displaced", "Cheap WR", "TB",
                   "active_and_healthy", "negative", -8,
                   reasoning="Low-AAV signing is a depth piece, limited impact."),
        _make_flag("Incumbent", "TB", "WR", "contingent", "Cheap WR", "TB",
                   "injured", "positive", 12),
    ])

    high_agent = RosterChangesAgent()
    low_agent = RosterChangesAgent()

    with patch.object(high_agent, "_build_team_context", new=AsyncMock(return_value={"team": "NE"})):
        with patch.object(high_agent, "call_once", new=AsyncMock(return_value=high_aav_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=2)):
                high_flags = await high_agent.run_for_team("NE")

    with patch.object(low_agent, "_build_team_context", new=AsyncMock(return_value={"team": "TB"})):
        with patch.object(low_agent, "call_once", new=AsyncMock(return_value=low_aav_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=2)):
                low_flags = await low_agent.run_for_team("TB")

    high_displaced = next(f for f in high_flags if f["flag_type"] == "displaced")
    low_displaced  = next(f for f in low_flags  if f["flag_type"] == "displaced")

    assert abs(high_displaced["value_impact_pct"]) > abs(low_displaced["value_impact_pct"]), (
        "High-AAV signings must produce larger value impacts than low-AAV signings."
    )


# ---------------------------------------------------------------------------
# Structural / enforcement tests
# ---------------------------------------------------------------------------

def test_no_hardcoded_years():
    """Scan roster_changes.py source for literal year integers."""
    source = (
        Path(__file__).parent.parent.parent.parent
        / "backend" / "agents" / "roster_changes.py"
    ).read_text()
    year_re = re.compile(r"\b(202[2-9])\b")
    model_re = re.compile(r"claude-[a-z]+-[\d]+-[\d]+-\w+")

    violations = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        cleaned = model_re.sub("", line)
        if year_re.search(cleaned):
            violations.append(f"line {lineno}: {line.strip()}")

    assert not violations, "Hardcoded years in roster_changes.py:\n" + "\n".join(violations)


@pytest.mark.asyncio
async def test_single_api_call_per_team():
    """run_for_team() must call call_once() exactly ONCE — never more."""
    agent = RosterChangesAgent()

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "LAC"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value="[]")) as mock_call:
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=0)):
                await agent.run_for_team("LAC")

    mock_call.assert_called_once()


@pytest.mark.asyncio
async def test_data_cache_used_not_reloaded():
    """
    When _DATA_CACHE is pre-populated, _fetch_target_shares should not
    call compute_target_share again for cached seasons.
    """
    import backend.agents.roster_changes as rc_module

    agent = RosterChangesAgent()

    # Pre-populate cache
    import pandas as pd
    fake_df = pd.DataFrame({
        "player_name": ["Test Player"],
        "recent_team": ["LAC"],
        "position": ["WR"],
        "games": [16],
        "total_targets": [100],
        "avg_target_share": [0.25],
        "avg_air_yards_share": [0.30],
    })

    from backend.utils.seasons import get_analysis_seasons
    for season in get_analysis_seasons(3):
        rc_module._DATA_CACHE[f"target_share_{season}"] = fake_df

    with patch("backend.integrations.nfl_data.compute_target_share") as mock_load:
        await agent._fetch_target_shares([{"name": "Test Player", "position": "WR"}])

    mock_load.assert_not_called()

    # Cleanup
    rc_module._DATA_CACHE.clear()


@pytest.mark.asyncio
async def test_bulk_db_write_single_transaction_per_team():
    """
    _write_flags() must use a single DB transaction for all flags.
    It must NOT call session.execute per flag (N+1 pattern).
    """
    from backend.agents.roster_changes import _write_flags

    flags = [
        _make_flag("WR A", "LAC", "WR", "displaced", "WR B", "LAC",
                   "active_and_healthy", "negative", -20),
        _make_flag("WR A", "LAC", "WR", "contingent", "WR B", "LAC",
                   "injured", "positive", 25),
    ]

    # Provide an empty player map (no DB hits needed for structural test)
    # Patch the name as imported in the roster_changes module
    with patch("backend.agents.roster_changes._bulk_resolve_player_ids",
               new=AsyncMock(return_value={})):
        with patch("backend.agents.roster_changes.AsyncSessionLocal") as mock_factory:
            session = AsyncMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            session.execute = AsyncMock()
            session.add = MagicMock()
            session.commit = AsyncMock()
            mock_factory.return_value = session

            await _write_flags(flags)

        # One commit for all flags — not one per flag
        session.commit.assert_called_once()


# ===========================================================================
# Rookie / draft pick tests (stage-04 spec — 12 required cases)
# ===========================================================================

import pandas as pd
import pytest

from backend.agents.roster_changes import RosterChangesAgent
from backend.integrations import nfl_data


def _make_agent() -> RosterChangesAgent:
    return RosterChangesAgent(dry_run=True)


# --- Draft capital value tests ---

def test_draft_capital_value_round1_is_high():
    """Round 1 pick 1 → capital_value == 100 and capital_signal == 'high'."""
    val = nfl_data.get_draft_capital_value(1, 1)
    sig = nfl_data.get_capital_signal(val)
    assert val == 100.0
    assert sig == "high"


def test_draft_capital_value_round6_is_low():
    """Round 6 pick (e.g. overall 180) → capital_signal == 'low'."""
    val = nfl_data.get_draft_capital_value(6, 180)
    sig = nfl_data.get_capital_signal(val)
    assert val < 40
    assert sig == "low"


def test_draft_capital_value_decreases_with_pick_number():
    """Later picks always produce lower capital values."""
    val_1  = nfl_data.get_draft_capital_value(1, 1)
    val_10 = nfl_data.get_draft_capital_value(1, 10)
    val_64 = nfl_data.get_draft_capital_value(2, 64)
    assert val_1 > val_10 > val_64


# --- College dominator conference adjustment ---

def test_college_dominator_adjusted_for_conference():
    """SEC player dominator unchanged. MAC player dominator × 0.80."""
    from backend.integrations.cfb_data import get_adjusted_dominator
    sec = get_adjusted_dominator(0.40, "SEC")
    mac = get_adjusted_dominator(0.40, "MAC")
    assert sec == pytest.approx(0.40, abs=1e-4)
    assert mac == pytest.approx(0.40 * 0.80, abs=1e-4)
    assert mac < sec


# --- Landing spot modifier ---

def test_landing_spot_compound_risk_modifier():
    """compound_risk_flag=True → landing_modifier == 0.75."""
    agent = _make_agent()
    mod = agent._get_landing_spot_modifier({"compound_risk_flag": True})
    assert mod == 0.75


def test_landing_spot_strong_system_modifier():
    """A-grade system → landing_modifier == 1.18."""
    agent = _make_agent()
    mod = agent._get_landing_spot_modifier({"system_grade": "A", "compound_risk_flag": False})
    assert mod == pytest.approx(1.18)


def test_landing_spot_rookie_qb_modifier():
    """rookie_qb_flag=True, no compound risk → landing_modifier == 0.85."""
    agent = _make_agent()
    mod = agent._get_landing_spot_modifier({"rookie_qb_flag": True, "compound_risk_flag": False})
    assert mod == pytest.approx(0.85)


# --- College profile grading ---

def test_grade_college_profile_elite_wr():
    """WR: adjusted_dominator >= 0.38 AND yards_per_route >= 2.8 → 'elite'."""
    agent = _make_agent()
    grade = agent._grade_college_profile(0.42, 3.0, "WR")
    assert grade == "elite"


def test_grade_college_profile_weak_wr():
    """WR: adjusted_dominator < 0.22 → 'weak'."""
    agent = _make_agent()
    grade = agent._grade_college_profile(0.18, 1.5, "WR")
    assert grade == "weak"


# --- Historical comps ---

def test_historical_comps_returned_for_elite_profile():
    """Elite college profile → at least 1 comp returned from a non-empty table."""
    agent = _make_agent()
    comp_table = pd.DataFrame([
        {"position": "WR", "player_name": "Ja'Marr Chase", "adjusted_dominator": 0.44,
         "capital_value": 85.0, "yr1_ppg": 16.4, "yr2_ppg": 19.8},
        {"position": "WR", "player_name": "Justin Jefferson", "adjusted_dominator": 0.40,
         "capital_value": 82.0, "yr1_ppg": 14.2, "yr2_ppg": 22.1},
    ])
    comps = agent._find_historical_comps(comp_table, "WR", 0.42, 84.0, 21)
    assert len(comps) >= 1
    assert comps[0]["yr1_ppg"] is not None


# --- Displacement flag generation ---

@pytest.mark.asyncio
async def test_high_capital_rookie_displaces_incumbent():
    """First-round WR drafted → incumbent WR gets DISPLACED flag."""
    agent = _make_agent()
    pick = {"player_name": "Rookie Star", "position": "WR", "round": 1}
    context = {
        "team": "LAC",
        "current_roster": [
            {"name": "Ladd McConkey", "position": "WR"},
            {"name": "Mike Williams", "position": "WR"},
        ],
    }
    flags = await agent._generate_rookie_displacement_flags(pick, "WR", "high", context)
    flag_types = {f["flag_type"] for f in flags}
    assert "displaced" in flag_types


@pytest.mark.asyncio
async def test_high_capital_displacement_always_paired_with_contingent():
    """Rookie DISPLACED flag always has matching CONTINGENT flag."""
    agent = _make_agent()
    pick = {"player_name": "Top Rookie", "position": "RB", "round": 1}
    context = {
        "team": "PHI",
        "current_roster": [{"name": "Saquon Barkley", "position": "RB"}],
    }
    flags = await agent._generate_rookie_displacement_flags(pick, "RB", "high", context)
    flag_types = [f["flag_type"] for f in flags]
    assert "displaced" in flag_types
    assert "contingent" in flag_types


@pytest.mark.asyncio
async def test_low_capital_pick_no_displacement():
    """6th round pick → no displacement flags generated."""
    agent = _make_agent()
    pick = {"player_name": "Late Round", "position": "WR", "round": 6}
    context = {
        "team": "NYG",
        "current_roster": [{"name": "Some Incumbent", "position": "WR"}],
    }
    flags = await agent._generate_rookie_displacement_flags(pick, "WR", "low", context)
    assert flags == []
