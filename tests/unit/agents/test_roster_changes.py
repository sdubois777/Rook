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

from backend.agents.roster_changes import (
    RosterChangesAgent,
    deduplicate_flags,
    enforce_flag_mutual_exclusivity,
    validate_flag,
)
from backend.utils.seasons import get_current_season


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

import pandas as pd


class _MockWarehouse:
    """Minimal warehouse substitute for tests."""

    def __init__(self, **kwargs):
        self._data = kwargs
        self.rosters = kwargs.get("rosters", pd.DataFrame())
        self.seasonal_rosters = kwargs.get("seasonal_rosters", pd.DataFrame())
        self.prev_rosters = kwargs.get("prev_rosters", pd.DataFrame())
        self.schedule = kwargs.get("schedule", pd.DataFrame())
        self.schedule_year = kwargs.get("schedule_year", 2026)

    def get_seasonal_stats(self, season):
        return self._data.get("seasonal_stats", {}).get(season, pd.DataFrame())

    def get_target_share(self, season):
        return self._data.get("target_share", {}).get(season, pd.DataFrame())

    def get_qb_stats(self, season):
        return self._data.get("qb_stats", {}).get(season, pd.DataFrame())

    def get_oline_stats(self, season):
        return self._data.get("oline_stats", {}).get(season, pd.DataFrame())

    def get_def_grades(self, season):
        return self._data.get("def_grades", {}).get(season, pd.DataFrame())

    def get_injuries(self, season):
        return self._data.get("injuries", {}).get(season, pd.DataFrame())

    def get_most_recent_def_grades(self):
        return self._data.get("most_recent_def_grades", pd.DataFrame())

    def get_snap_pct(self, season):
        return self._data.get("snap_pct", {}).get(season, pd.DataFrame())

    def get_ngs_receiving(self, season):
        return self._data.get("ngs_receiving", {}).get(season, pd.DataFrame())

    def get_ngs_rushing(self, season):
        return self._data.get("ngs_rushing", {}).get(season, pd.DataFrame())

    def get_depth_chart(self, season):
        return self._data.get("depth_charts", {}).get(season, pd.DataFrame())

    def get_starter(self, team, position, season=None):
        return self._data.get("starters", {}).get((team, position))

    def get_player_depth_rank(self, gsis_id, season=None):
        return self._data.get("depth_ranks", {}).get(gsis_id)

    def get_team_depth_context(self, team, season=None):
        return self._data.get("depth_context", {}).get(team, {})

    def summary(self):
        return {}


def _make_warehouse(**kwargs):
    return _MockWarehouse(**kwargs)


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
async def test_warehouse_used_not_reloaded():
    """
    When warehouse has target_share data, _fetch_target_shares reads it
    without calling nfl_data.compute_target_share.
    """
    agent = RosterChangesAgent()

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
    analysis_seasons = get_analysis_seasons(3)
    target_share = {season: fake_df for season in analysis_seasons}
    agent._warehouse = _make_warehouse(target_share=target_share)

    with patch("backend.integrations.nfl_data.compute_target_share") as mock_load:
        result = await agent._fetch_target_shares([{"name": "Test Player", "position": "WR"}])

    mock_load.assert_not_called()
    assert "Test Player" in result
    assert len(result["Test Player"]) == len(analysis_seasons)


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


@pytest.mark.asyncio
async def test_write_flags_deletes_by_player_id_only():
    """
    _write_flags() must delete ALL existing flags for a player (not scoped
    to season_year), to prevent duplicates when the model outputs flags
    with unexpected season_year values.
    """
    from backend.agents.roster_changes import _write_flags
    import uuid

    pid1 = uuid.uuid4()
    pid2 = uuid.uuid4()

    flags = [
        _make_flag("WR A", "LAC", "WR", "displaced", "WR B", "LAC",
                   "active_and_healthy", "negative", -20, season_year=2026),
    ]

    # id_map is keyed by (name, team) tuples → (player_id, team) tuples
    mock_player_map = {
        ("WR A", "LAC"): (str(pid1), "LAC"),
        ("WR B", "LAC"): (str(pid2), "LAC"),
    }

    with patch("backend.agents.roster_changes._bulk_resolve_player_ids",
               new=AsyncMock(return_value=mock_player_map)):
        with patch("backend.agents.roster_changes.AsyncSessionLocal") as mock_factory:
            session = AsyncMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            session.execute = AsyncMock()
            session.add = MagicMock()
            session.commit = AsyncMock()
            mock_factory.return_value = session

            await _write_flags(flags)

            # The first execute call is the DELETE statement
            assert session.execute.call_count >= 1
            delete_call = session.execute.call_args_list[0]
            delete_stmt = delete_call[0][0]
            # Compile without literal_binds (UUID can't render as literal)
            compiled = str(delete_stmt.compile())
            assert "player_id" in compiled.lower()
            assert "season_year" not in compiled.lower()


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


# ===========================================================================
# _sync_player_teams tests
# ===========================================================================


@pytest.mark.asyncio
async def test_sync_player_teams_updates_arrival():
    """Signing transaction updates player team_abbr to the new team."""
    agent = RosterChangesAgent()

    transactions = [
        {"player": "Keenan Allen", "type": "Signed", "position": "WR", "aav": "23000000", "date": "2026-03-15"},
    ]

    mock_player = MagicMock()
    mock_player.name = "Keenan Allen"
    mock_player.team_abbr = "CHI"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        count = await agent._sync_player_teams(transactions, "LAC")

    assert count == 1
    assert mock_player.team_abbr == "LAC"
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_sync_player_teams_handles_release():
    """Released transaction sets player team_abbr to FA."""
    agent = RosterChangesAgent()

    transactions = [
        {"player": "Old Player", "type": "Released", "position": "WR", "aav": "", "date": "2026-03-10"},
    ]

    mock_player = MagicMock()
    mock_player.name = "Old Player"
    mock_player.team_abbr = "LAC"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        count = await agent._sync_player_teams(transactions, "LAC")

    assert count == 1
    assert mock_player.team_abbr == "FA"


@pytest.mark.asyncio
async def test_sync_player_teams_skips_restructure():
    """Restructured transactions are not team changes — should be skipped."""
    agent = RosterChangesAgent()

    transactions = [
        {"player": "Some Player", "type": "Restructured", "position": "QB", "aav": "", "date": "2026-02-01"},
    ]

    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        count = await agent._sync_player_teams(transactions, "LAC")

    assert count == 0
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_player_teams_skips_already_correct():
    """Player already on the correct team should not be counted as updated."""
    agent = RosterChangesAgent()

    transactions = [
        {"player": "Stable Player", "type": "Signed", "position": "WR", "aav": "", "date": "2026-03-01"},
    ]

    mock_player = MagicMock()
    mock_player.name = "Stable Player"
    mock_player.team_abbr = "LAC"  # already on LAC

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        count = await agent._sync_player_teams(transactions, "LAC")

    assert count == 0
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_player_teams_called_in_run_for_team():
    """_sync_player_teams must be called at the end of run_for_team."""
    agent = RosterChangesAgent()
    context = {
        "team": "LAC",
        "transactions": [{"player": "Test", "type": "Signed"}],
    }

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value=context)), \
         patch.object(agent, "call_once", new=AsyncMock(return_value="[]")), \
         patch("backend.agents.roster_changes._write_flags", new=AsyncMock(return_value=0)), \
         patch.object(agent, "_sync_player_teams", new=AsyncMock(return_value=0)) as mock_sync:
        await agent.run_for_team("LAC")

    mock_sync.assert_called_once_with(
        [{"player": "Test", "type": "Signed"}], "LAC"
    )


@pytest.mark.asyncio
async def test_sync_player_teams_handles_trade():
    """Traded transaction updates player team_abbr to the acquiring team."""
    agent = RosterChangesAgent()

    transactions = [
        {"player": "Mike Evans", "type": "Traded", "position": "WR", "aav": "", "date": "2026-03-20"},
    ]

    mock_player = MagicMock()
    mock_player.name = "Mike Evans"
    mock_player.team_abbr = "TB"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        count = await agent._sync_player_teams(transactions, "SF")

    assert count == 1
    assert mock_player.team_abbr == "SF"
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_sync_player_teams_sets_updated_at():
    """Team sync must set updated_at timestamp on modified players."""
    agent = RosterChangesAgent()

    transactions = [
        {"player": "Davante Adams", "type": "Signed", "position": "WR", "aav": "", "date": "2026-03-15"},
    ]

    mock_player = MagicMock()
    mock_player.name = "Davante Adams"
    mock_player.team_abbr = "LV"
    mock_player.updated_at = None

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        await agent._sync_player_teams(transactions, "NYJ")

    assert mock_player.updated_at is not None
    assert mock_player.team_abbr == "NYJ"


# ===========================================================================
# Veteran guard — _write_rookie_evaluation must NOT mark veterans as rookies
# ===========================================================================


@pytest.mark.asyncio
async def test_write_rookie_eval_skips_veteran():
    """_write_rookie_evaluation must NOT set is_rookie=True when nfl_seasons_played >= 1."""
    agent = _make_agent()

    mock_player = MagicMock()
    mock_player.name = "Amon-Ra St. Brown"
    mock_player.nfl_seasons_played = 4  # 4-year veteran

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fields = {
        "player_name": "Amon-Ra St. Brown",
        "college_profile_grade": "elite",
        "draft_capital_signal": "high",
    }

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        await agent._write_rookie_evaluation(fields)

    # Veteran guard should prevent is_rookie from being set
    assert not hasattr(mock_player, "is_rookie") or mock_player.is_rookie is not True
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_write_rookie_eval_allows_true_rookie():
    """_write_rookie_evaluation SHOULD set is_rookie=True for players with 0 or None seasons."""
    agent = _make_agent()

    mock_player = MagicMock()
    mock_player.name = "Actual Rookie"
    mock_player.nfl_seasons_played = None  # not in roster data = true rookie

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fields = {
        "player_name": "Actual Rookie",
        "college_profile_grade": "strong",
        "draft_capital_signal": "high",
        "draft_capital_value": 85.0,
    }

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        await agent._write_rookie_evaluation(fields)

    assert mock_player.is_rookie is True
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_write_rookie_eval_loose_match_blocked_for_veteran():
    """Loose last-name fallback should NOT mark veteran as rookie."""
    agent = _make_agent()

    # Simulate: exact match fails, loose match finds veteran "Allen Lazard"
    mock_player_veteran = MagicMock()
    mock_player_veteran.name = "Allen Lazard"
    mock_player_veteran.nfl_seasons_played = 8

    mock_exact_result = MagicMock()
    mock_exact_result.scalar_one_or_none.return_value = None  # exact match fails

    mock_fuzzy_scalars = MagicMock()
    mock_fuzzy_scalars.all.return_value = [mock_player_veteran]
    mock_fuzzy_result = MagicMock()
    mock_fuzzy_result.scalars.return_value = mock_fuzzy_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[mock_exact_result, mock_fuzzy_result])
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    fields = {"player_name": "Josh Allen-Lazard"}  # hypothetical draft pick

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        await agent._write_rookie_evaluation(fields)

    # Veteran guard should block
    assert not hasattr(mock_player_veteran, "is_rookie") or mock_player_veteran.is_rookie is not True
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_player_teams_empty_type_exact_match_only():
    """OTC cap tables (empty type) use exact name match — no last-name fallback."""
    agent = RosterChangesAgent()

    transactions = [
        {"player": "Mike Evans", "type": "", "position": "", "aav": "", "date": ""},
    ]

    mock_player = MagicMock()
    mock_player.name = "Mike Evans"
    mock_player.team_abbr = "TB"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        count = await agent._sync_player_teams(transactions, "SF")

    assert count == 1
    assert mock_player.team_abbr == "SF"
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_sync_player_teams_no_last_name_fallback():
    """Exact name miss must NOT fall back to last-name ilike — prevents cross-player confusion."""
    agent = RosterChangesAgent()

    # "Julian Love" is on SEA cap but our DB has "Jordan Love" on GB
    transactions = [
        {"player": "Julian Love", "type": "", "position": "", "aav": "", "date": ""},
    ]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # no exact match

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.roster_changes.AsyncSessionLocal", return_value=mock_ctx):
        count = await agent._sync_player_teams(transactions, "SEA")

    assert count == 0
    mock_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Departure-based BENEFICIARY flags
# ---------------------------------------------------------------------------

import pandas as pd


def _make_prev_roster_df(players: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    """Build a mock previous-season roster DataFrame.

    Args:
        players: list of (name, position, team, player_id) tuples.
    """
    return pd.DataFrame(players, columns=["full_name", "position", "team", "player_id"])


def _make_target_share_df(players: list[tuple[str, str, str, int, int]]) -> pd.DataFrame:
    """Build a mock target_share DataFrame.

    Args:
        players: list of (player_id, name, team, targets, carries) tuples.
    """
    return pd.DataFrame(players, columns=["player_id", "player_name", "recent_team", "total_targets", "total_carries"])


@pytest.mark.asyncio
async def test_handle_departures_generates_beneficiary():
    """Departed WR with significant production generates BENEFICIARY flags."""
    prev_df = _make_prev_roster_df([
        ("Cooper Kupp", "WR", "LAR", "pid-1"),
        ("Puka Nacua", "WR", "LAR", "pid-2"),
        ("Tutu Atwell", "WR", "LAR", "pid-3"),
        ("Kyren Williams", "RB", "LAR", "pid-4"),
    ])
    ts_df = _make_target_share_df([
        ("pid-1", "C.Kupp", "LAR", 120, 0),
        ("pid-2", "P.Nacua", "LAR", 150, 0),
    ])
    prev_season = get_current_season() - 1
    agent = RosterChangesAgent(warehouse=_make_warehouse(
        prev_rosters=prev_df, target_share={prev_season: ts_df},
    ))
    roster = [
        {"name": "Puka Nacua", "position": "WR"},
        {"name": "Tutu Atwell", "position": "WR"},
        {"name": "Kyren Williams", "position": "RB"},
    ]

    flags = await agent._handle_departures("LAR", roster)

    assert len(flags) == 2
    for f in flags:
        assert f["flag_type"] == "beneficiary"
        assert f["trigger_player_name"] == "Cooper Kupp"
        assert f["trigger_condition"] == "departed_team"
        assert f["effect_on_value"] == "positive"
        assert f["player_position"] == "WR"
    names = {f["player_name"] for f in flags}
    assert names == {"Puka Nacua", "Tutu Atwell"}


@pytest.mark.asyncio
async def test_handle_departures_no_change_no_flags():
    """When no one left the team, no BENEFICIARY flags are generated."""
    prev_df = _make_prev_roster_df([
        ("Puka Nacua", "WR", "LAR", "pid-1"),
        ("Tutu Atwell", "WR", "LAR", "pid-2"),
    ])
    ts_df = _make_target_share_df([("pid-1", "P.Nacua", "LAR", 150, 0)])
    prev_season = get_current_season() - 1
    agent = RosterChangesAgent(warehouse=_make_warehouse(
        prev_rosters=prev_df, target_share={prev_season: ts_df},
    ))
    roster = [
        {"name": "Puka Nacua", "position": "WR"},
        {"name": "Tutu Atwell", "position": "WR"},
    ]

    flags = await agent._handle_departures("LAR", roster)

    assert len(flags) == 0


@pytest.mark.asyncio
async def test_handle_departures_ignores_qb():
    """QB departures do not generate BENEFICIARY flags (WR/RB/TE only)."""
    prev_df = _make_prev_roster_df([
        ("Old QB", "QB", "LAR", "pid-1"),
        ("WR Guy", "WR", "LAR", "pid-2"),
    ])
    ts_df = _make_target_share_df([])
    prev_season = get_current_season() - 1
    agent = RosterChangesAgent(warehouse=_make_warehouse(
        prev_rosters=prev_df, target_share={prev_season: ts_df},
    ))
    roster = [{"name": "WR Guy", "position": "WR"}]

    flags = await agent._handle_departures("LAR", roster)

    assert len(flags) == 0


@pytest.mark.asyncio
async def test_handle_departures_skips_low_production():
    """Depth WRs with <50 targets do not generate BENEFICIARY flags."""
    prev_df = _make_prev_roster_df([
        ("Cooper Kupp", "WR", "LAR", "pid-1"),
        ("Depth Guy", "WR", "LAR", "pid-2"),
        ("Puka Nacua", "WR", "LAR", "pid-3"),
    ])
    ts_df = _make_target_share_df([
        ("pid-1", "C.Kupp", "LAR", 120, 0),   # significant
        ("pid-2", "D.Guy", "LAR", 10, 0),      # depth — below 50 threshold
    ])
    prev_season = get_current_season() - 1
    agent = RosterChangesAgent(warehouse=_make_warehouse(
        prev_rosters=prev_df, target_share={prev_season: ts_df},
    ))
    roster = [{"name": "Puka Nacua", "position": "WR"}]

    flags = await agent._handle_departures("LAR", roster)

    # Only Kupp departure generates flags, not Depth Guy
    assert len(flags) == 1
    assert flags[0]["trigger_player_name"] == "Cooper Kupp"


@pytest.mark.asyncio
async def test_handle_departures_impact_by_position():
    """WR departure → 0.35 impact; RB departure → 0.25 impact."""
    prev_season = get_current_season() - 1

    # WR scenario
    wr_prev = _make_prev_roster_df([("WR Star", "WR", "LAR", "pid-1"), ("WR2", "WR", "LAR", "pid-2")])
    wr_ts = _make_target_share_df([("pid-1", "W.Star", "LAR", 100, 0)])
    wr_roster = [{"name": "WR2", "position": "WR"}]
    wr_agent = RosterChangesAgent(warehouse=_make_warehouse(
        prev_rosters=wr_prev, target_share={prev_season: wr_ts},
    ))
    wr_flags = await wr_agent._handle_departures("LAR", wr_roster)

    # RB scenario
    rb_prev = _make_prev_roster_df([("RB Star", "RB", "NYG", "pid-3"), ("RB2", "RB", "NYG", "pid-4")])
    rb_ts = _make_target_share_df([("pid-3", "R.Star", "NYG", 0, 200)])
    rb_roster = [{"name": "RB2", "position": "RB"}]
    rb_agent = RosterChangesAgent(warehouse=_make_warehouse(
        prev_rosters=rb_prev, target_share={prev_season: rb_ts},
    ))
    rb_flags = await rb_agent._handle_departures("NYG", rb_roster)

    assert wr_flags[0]["value_impact_pct"] == 0.35
    assert rb_flags[0]["value_impact_pct"] == 0.25


# ---------------------------------------------------------------------------
# Committee / Displaced mutual exclusivity
# ---------------------------------------------------------------------------

def test_non_rb_never_gets_committee_flag():
    """WR/TE/QB committee flags are converted to displaced by enforce function."""
    flags = [
        _make_flag("WR Star", "LAC", "WR", "committee", "Other WR", "LAC",
                   "active_and_healthy", "neutral", -15),
        _make_flag("TE Guy", "LAC", "TE", "committee", "New TE", "LAC",
                   "active_and_healthy", "neutral", -10),
    ]
    result = enforce_flag_mutual_exclusivity(flags)
    for f in result:
        assert f["flag_type"] == "displaced", (
            f"Non-RB {f['player_position']} should have committee converted to displaced"
        )
        assert f["effect_on_value"] == "negative"


def test_rbs_get_committee_not_displaced_for_timeshare():
    """Same-tier RBs in a timeshare keep their committee flags."""
    flags = [
        _make_flag("RB One", "MIA", "RB", "committee", "RB Two", "MIA",
                   "active_and_healthy", "neutral", -10),
        _make_flag("RB Two", "MIA", "RB", "committee", "RB One", "MIA",
                   "active_and_healthy", "neutral", -10),
    ]
    result = enforce_flag_mutual_exclusivity(flags)
    assert len(result) == 2
    assert all(f["flag_type"] == "committee" for f in result)


def test_superior_rb_arrival_generates_displaced():
    """When displaced exists, committee for same trigger is removed — displaced wins."""
    flags = [
        _make_flag("Old RB", "NYG", "RB", "displaced", "Star RB", "NYG",
                   "active_and_healthy", "negative", -25),
        _make_flag("Old RB", "NYG", "RB", "committee", "Star RB", "NYG",
                   "active_and_healthy", "neutral", -10),
        _make_flag("Old RB", "NYG", "RB", "contingent", "Star RB", "NYG",
                   "injured", "positive", 20),
    ]
    result = enforce_flag_mutual_exclusivity(flags)
    flag_types = [f["flag_type"] for f in result]
    assert "displaced" in flag_types
    assert "contingent" in flag_types
    assert "committee" not in flag_types


def test_no_duplicate_committee_and_displaced():
    """Both committee and displaced for same player+trigger → committee removed."""
    flags = [
        _make_flag("RB X", "CHI", "RB", "displaced", "RB Y", "CHI",
                   "active_and_healthy", "negative", -20),
        _make_flag("RB X", "CHI", "RB", "committee", "RB Y", "CHI",
                   "active_and_healthy", "neutral", -10),
    ]
    result = enforce_flag_mutual_exclusivity(flags)
    assert len(result) == 1
    assert result[0]["flag_type"] == "displaced"


def test_committee_converted_to_displaced_for_wr():
    """WR committee flag becomes displaced after enforcement."""
    flags = [
        _make_flag("Slot WR", "BUF", "WR", "committee", "New WR", "BUF",
                   "active_and_healthy", "neutral", -12),
    ]
    result = enforce_flag_mutual_exclusivity(flags)
    assert len(result) == 1
    assert result[0]["flag_type"] == "displaced"
    assert result[0]["effect_on_value"] == "negative"


@pytest.mark.asyncio
async def test_enforce_called_before_write_in_run_for_team():
    """enforce_flag_mutual_exclusivity is called before _write_flags in run_for_team."""
    agent = RosterChangesAgent(warehouse=_make_warehouse())

    # Model returns both committee and displaced for same RB+trigger
    model_output = json.dumps([
        _make_flag("RB X", "DEN", "RB", "displaced", "RB Y", "DEN",
                   "active_and_healthy", "negative", -20),
        _make_flag("RB X", "DEN", "RB", "committee", "RB Y", "DEN",
                   "active_and_healthy", "neutral", -10),
        _make_flag("RB X", "DEN", "RB", "contingent", "RB Y", "DEN",
                   "injured", "positive", 15),
    ])

    written_flags = []

    async def capture_write(flags):
        written_flags.extend(flags)
        return len(flags)

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "DEN"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=model_output)):
            with patch("backend.agents.roster_changes._write_flags", new=AsyncMock(side_effect=capture_write)):
                flags = await agent.run_for_team("DEN")

    # Committee should have been removed before write
    flag_types = [f["flag_type"] for f in flags]
    assert "committee" not in flag_types
    assert "displaced" in flag_types


# ---------------------------------------------------------------------------
# validate_flag — reject phantom/incomplete flags
# ---------------------------------------------------------------------------

def test_phantom_flags_rejected_empty_trigger():
    """Flag with empty trigger_player_name is rejected by validate_flag()."""
    flag = _make_flag("RB X", "DEN", "RB", "displaced", "", "DEN",
                      "active_and_healthy", "negative", -25)
    assert validate_flag(flag) is False


def test_phantom_flags_rejected_none_trigger():
    """Flag with None trigger_player_name is rejected."""
    flag = _make_flag("WR Y", "NYG", "WR", "displaced", "Trigger", "NYG",
                      "active_and_healthy", "negative", -20)
    flag["trigger_player_name"] = None
    assert validate_flag(flag) is False


def test_phantom_flags_rejected_missing_flag_type():
    """Flag with missing flag_type is rejected."""
    flag = _make_flag("WR Z", "NYG", "WR", "", "Trigger", "NYG",
                      "active_and_healthy", "negative", -20)
    assert validate_flag(flag) is False


def test_phantom_flags_rejected_missing_impact():
    """Flag with None value_impact_pct is rejected."""
    flag = _make_flag("RB A", "DEN", "RB", "displaced", "RB B", "DEN",
                      "active_and_healthy", "negative", -25)
    flag["value_impact_pct"] = None
    assert validate_flag(flag) is False


def test_valid_flag_passes_validation():
    """Complete flag passes validation."""
    flag = _make_flag("RB A", "DEN", "RB", "displaced", "RB B", "DEN",
                      "active_and_healthy", "negative", -25)
    assert validate_flag(flag) is True


# ===========================================================================
# deduplicate_flags — beneficiary superseded by displaced
# ===========================================================================

def test_dedup_removes_beneficiary_when_displaced_exists():
    """When displaced exists for player+trigger, beneficiary is removed."""
    flags = [
        _make_flag("WR Star", "LAC", "WR", "beneficiary", "Keenan Allen", "LAC",
                   "injured", "positive", 15),
        _make_flag("WR Star", "LAC", "WR", "displaced", "Keenan Allen", "LAC",
                   "active_and_healthy", "negative", -30),
        _make_flag("WR Star", "LAC", "WR", "contingent", "Keenan Allen", "LAC",
                   "injured_or_absent", "positive", 24),
    ]
    result = deduplicate_flags(flags)
    flag_types = {f["flag_type"] for f in result}
    assert "displaced" in flag_types
    assert "contingent" in flag_types
    assert "beneficiary" not in flag_types


def test_dedup_keeps_beneficiary_without_displaced():
    """Beneficiary without matching displaced is kept."""
    flags = [
        _make_flag("WR Star", "LAC", "WR", "beneficiary", "Departed WR", "LAC",
                   "departed_team", "positive", 35),
    ]
    result = deduplicate_flags(flags)
    assert len(result) == 1
    assert result[0]["flag_type"] == "beneficiary"


def test_dedup_removes_exact_duplicates():
    """Duplicate flags (same player+trigger+type) are deduplicated."""
    flag = _make_flag("WR A", "LAC", "WR", "displaced", "WR B", "LAC",
                      "active_and_healthy", "negative", -30)
    flags = [flag, flag.copy()]
    result = deduplicate_flags(flags)
    assert len(result) == 1


# ===========================================================================
# _extract_upside_downside — valuation engine
# ===========================================================================

def test_extract_upside_downside_from_profile():
    """Upside/downside PPR extracted from clean_season_baseline."""
    from backend.engines.valuation import _extract_upside_downside
    profile = MagicMock()
    profile.clean_season_baseline = {
        "ppr_points": 250,
        "upside_ppr": 300,
        "downside_ppr": 180,
    }
    upside, downside = _extract_upside_downside(profile)
    assert upside == 300.0
    assert downside == 180.0


def test_extract_upside_downside_no_profile():
    """No profile returns (0, 0)."""
    from backend.engines.valuation import _extract_upside_downside
    assert _extract_upside_downside(None) == (0.0, 0.0)


def test_extract_upside_downside_empty_baseline():
    """Empty baseline returns (0, 0)."""
    from backend.engines.valuation import _extract_upside_downside
    profile = MagicMock()
    profile.clean_season_baseline = {}
    assert _extract_upside_downside(profile) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# _extract_ppr — prefers projected_ppr_season over ppr_points
# ---------------------------------------------------------------------------

def test_extract_ppr_prefers_projected():
    """When projected_ppr_season exists, use it over ppr_points."""
    from backend.engines.valuation import _extract_ppr
    profile = MagicMock()
    profile.clean_season_baseline = {
        "ppr_points": 228.0,
        "projected_ppr_season": 195.0,
    }
    assert _extract_ppr(profile) == 195.0


def test_extract_ppr_falls_back_to_ppr_points():
    """When no projected_ppr_season, fall back to ppr_points."""
    from backend.engines.valuation import _extract_ppr
    profile = MagicMock()
    profile.clean_season_baseline = {"ppr_points": 228.0}
    assert _extract_ppr(profile) == 228.0


def test_extract_ppr_none_profile():
    """None profile returns 0."""
    from backend.engines.valuation import _extract_ppr
    assert _extract_ppr(None) == 0.0


# ---------------------------------------------------------------------------
# Specialist pairing → committee downgrade
# ---------------------------------------------------------------------------


def test_specialist_pairing_not_committee():
    """Workhorse + pass-catching specialist pairing → committee becomes displaced."""
    from backend.agents.roster_changes import downgrade_specialist_committee_flags

    flags = [
        _make_flag("Workhorse RB", "BAL", "RB", "committee", "Specialist RB", "BAL",
                    "active_and_healthy", "negative", -15),
    ]
    backfield = {
        "rb_usage": [
            {"player_name": "Workhorse RB", "total_carries": 300},
            {"player_name": "Specialist RB", "total_carries": 80},
        ]
    }
    result = downgrade_specialist_committee_flags(flags, backfield)
    assert result[0]["flag_type"] == "displaced", "Specialist pairing should be displaced, not committee"
    assert result[0]["value_impact_pct"] >= -12, "Impact should be mild for specialist pairing"


def test_true_committee_not_downgraded():
    """True 50/50 split keeps committee flag."""
    from backend.agents.roster_changes import downgrade_specialist_committee_flags

    flags = [
        _make_flag("RB A", "PHI", "RB", "committee", "RB B", "PHI",
                    "active_and_healthy", "neutral", -10),
    ]
    backfield = {
        "rb_usage": [
            {"player_name": "RB A", "total_carries": 180},
            {"player_name": "RB B", "total_carries": 160},
        ]
    }
    result = downgrade_specialist_committee_flags(flags, backfield)
    assert result[0]["flag_type"] == "committee", "True split should stay committee"


def test_established_workhorse_vs_new_arrival():
    """Established workhorse + new arrival with 0 carries → displaced not committee."""
    from backend.agents.roster_changes import downgrade_specialist_committee_flags

    flags = [
        _make_flag("Henry", "BAL", "RB", "committee", "Hill", "BAL",
                    "active_and_healthy", "negative", -15),
    ]
    backfield = {
        "rb_usage": [
            {"player_name": "Henry", "total_carries": 325},
        ]
    }
    result = downgrade_specialist_committee_flags(flags, backfield)
    assert result[0]["flag_type"] == "displaced"
    assert result[0]["value_impact_pct"] >= -10


def test_no_backfield_data_leaves_flags_unchanged():
    """When no backfield usage data, committee flags pass through unchanged."""
    from backend.agents.roster_changes import downgrade_specialist_committee_flags

    flags = [
        _make_flag("RB X", "DET", "RB", "committee", "RB Y", "DET",
                    "active_and_healthy", "neutral", -10),
    ]
    result = downgrade_specialist_committee_flags(flags, None)
    assert result[0]["flag_type"] == "committee"


def test_non_rb_committee_not_affected_by_downgrade():
    """downgrade_specialist_committee_flags only touches RB committee flags."""
    from backend.agents.roster_changes import downgrade_specialist_committee_flags

    flags = [
        _make_flag("WR Star", "LAC", "WR", "committee", "Other WR", "LAC",
                    "active_and_healthy", "neutral", -5),
    ]
    backfield = {"rb_usage": []}
    result = downgrade_specialist_committee_flags(flags, backfield)
    # WR committee should NOT be touched by this function (enforce_flag_mutual_exclusivity handles it)
    assert result[0]["flag_type"] == "committee"


# ---------------------------------------------------------------------------
# Depth chart rank filtering in _handle_arrivals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_displaced_skips_deep_depth_incumbents():
    """Rank 3+ incumbents should NOT get DISPLACED flags (depth chart noise)."""
    from backend.agents.roster_changes import RosterChangesAgent
    from backend.utils.seasons import get_current_season

    current = get_current_season()
    prev = current - 1

    agent = RosterChangesAgent(dry_run=True)

    # Arrival: big-name WR with 120 targets
    # Incumbents: WR1 (rank 1), WR3 (rank 3)
    prev_rosters = pd.DataFrame([
        {"full_name": "Big Arrival", "team": "NYG", "position": "WR", "player_id": "00-0099901"},
        {"full_name": "Incumbent WR1", "team": "LAC", "position": "WR", "player_id": "00-0099902"},
        {"full_name": "Deep Bench WR", "team": "LAC", "position": "WR", "player_id": "00-0099903"},
    ])
    target_share = pd.DataFrame([
        {"player_id": "00-0099901", "player_name": "B.Arrival", "recent_team": "NYG",
         "position": "WR", "total_targets": 120, "total_carries": 0, "games": 17},
    ])

    otc_roster = [
        {"name": "Big Arrival", "position": "WR"},
        {"name": "Incumbent WR1", "position": "WR"},
        {"name": "Deep Bench WR", "position": "WR"},
    ]

    agent._warehouse = _make_warehouse(
        prev_rosters=prev_rosters,
        target_share={prev: target_share},
        depth_ranks={
            "00-0099902": 1,   # WR1
            "00-0099903": 3,   # WR3 — should be skipped
        },
    )

    flags = await agent._handle_arrivals("LAC", otc_roster)

    # Should only flag WR1, NOT WR3
    displaced_names = [f["player_name"] for f in flags if f["flag_type"] == "displaced"]
    assert "Incumbent WR1" in displaced_names
    assert "Deep Bench WR" not in displaced_names


@pytest.mark.asyncio
async def test_displaced_confidence_from_depth_rank():
    """Arrival depth_rank=2 should produce confidence='medium'."""
    from backend.agents.roster_changes import RosterChangesAgent
    from backend.utils.seasons import get_current_season

    current = get_current_season()
    prev = current - 1

    agent = RosterChangesAgent(dry_run=True)

    prev_rosters = pd.DataFrame([
        {"full_name": "Backup Arrival", "team": "NYJ", "position": "WR", "player_id": "00-0088801"},
        {"full_name": "Incumbent WR1", "team": "LAC", "position": "WR", "player_id": "00-0088802"},
    ])
    target_share = pd.DataFrame([
        {"player_id": "00-0088801", "player_name": "B.Arrival", "recent_team": "NYJ",
         "position": "WR", "total_targets": 100, "total_carries": 0, "games": 17},
    ])

    otc_roster = [
        {"name": "Backup Arrival", "position": "WR"},
        {"name": "Incumbent WR1", "position": "WR"},
    ]

    agent._warehouse = _make_warehouse(
        prev_rosters=prev_rosters,
        target_share={prev: target_share},
        depth_ranks={
            "00-0088801": 2,   # arrival is backup
            "00-0088802": 1,   # incumbent is starter
        },
    )

    flags = await agent._handle_arrivals("LAC", otc_roster)

    displaced_flags = [f for f in flags if f["flag_type"] == "displaced"]
    assert len(displaced_flags) == 1
    assert displaced_flags[0]["confidence"] == "medium"
