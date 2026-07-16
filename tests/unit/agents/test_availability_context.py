"""Tests for availability context + prompt changes in player_profiles.py."""
from __future__ import annotations

import pytest

from backend.agents import player_profiles
from backend.agents.player_profiles import (
    HAIKU_SYSTEM_PROMPT,
    PLAYER_PROFILES_PROMPT_VERSION,
    ROOKIE_PROJECTION_PROMPT,
    SONNET_SYSTEM_PROMPT,
    needs_sonnet_reasoning,
)

_FORBIDDEN_TERMS = [
    "ACL", "MCL", "hamstring", "shoulder", "chronic",
    "diagnosis", "surgery", "torn", "fracture",
]


def test_prompt_version_v7():
    assert PLAYER_PROFILES_PROMPT_VERSION == "v7"


def test_sonnet_prompt_no_forbidden_terms_in_guidance():
    """The Sonnet prompt's availability guidance forbids injury terms — but only
    as a FORBIDDEN list, never as usage. Confirm each term appears at most inside
    the forbidden-list line, not as instruction to use it."""
    # The FORBIDDEN line itself names the terms; everywhere else they must not
    # appear. Strip the forbidden-list sentence, then assert none remain.
    prompt = SONNET_SYSTEM_PROMPT
    assert "FORBIDDEN" in prompt
    # Remove the forbidden-list block (from "FORBIDDEN" to the next blank line)
    lines = prompt.splitlines()
    kept = []
    skipping = False
    for ln in lines:
        if "FORBIDDEN" in ln:
            skipping = True
        if skipping and ln.strip() == "":
            skipping = False
        if not skipping:
            kept.append(ln)
    remainder = "\n".join(kept).lower()
    for term in _FORBIDDEN_TERMS:
        assert term.lower() not in remainder, f"'{term}' leaked outside FORBIDDEN list"


def test_sonnet_prompt_has_availability_framework():
    assert "AVAILABILITY" in SONNET_SYSTEM_PROMPT
    assert "availability_risk" in SONNET_SYSTEM_PROMPT
    assert "risk_modifier" in SONNET_SYSTEM_PROMPT


def test_haiku_prompt_has_no_injury_section():
    """Haiku never received an injury section; confirm it stays clean."""
    lowered = HAIKU_SYSTEM_PROMPT.lower()
    for term in _FORBIDDEN_TERMS:
        assert term.lower() not in lowered


def test_rookie_prompt_uses_availability_not_injury_risk():
    assert "AVAILABILITY:" in ROOKIE_PROJECTION_PROMPT
    assert "{availability_risk}" in ROOKIE_PROJECTION_PROMPT
    assert "INJURY RISK" not in ROOKIE_PROJECTION_PROMPT


# ---------------------------------------------------------------------------
# Sonnet routing now keys off availability, not the removed risk fields
# ---------------------------------------------------------------------------

def test_routing_concern_availability_triggers_sonnet():
    player = {"position": "WR", "age": 26, "injury_profile": {"availability_risk": "concern"}}
    assert needs_sonnet_reasoning(player) is True


def test_routing_full_season_absence_triggers_sonnet():
    player = {"position": "WR", "age": 26, "injury_profile": {"full_season_absence": True}}
    assert needs_sonnet_reasoning(player) is True


def test_routing_durable_player_stays_haiku():
    player = {"position": "WR", "age": 26, "injury_profile": {"availability_risk": "durable"}}
    assert needs_sonnet_reasoning(player) is False


# ---------------------------------------------------------------------------
# _get_team_injury_profiles returns availability, not injury narrative
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return _Result(self._rows)


@pytest.mark.asyncio
async def test_injury_context_no_narrative_fields(monkeypatch):
    """Context dict carries availability keys and none of the removed narrative keys."""
    from types import SimpleNamespace

    ip = SimpleNamespace(
        availability_risk="monitor",
        availability_trend="stable",
        projected_games=14,
        avg_games_per_season=13.7,
        games_played_history=[{"season": 2025, "games": 14}],
        full_season_absence_flag=False,
        availability_risk_modifier=-0.05,
    )
    rows = [("Tee Higgins", ip), ("No Profile Guy", None)]
    monkeypatch.setattr(
        player_profiles, "AsyncSessionLocal", lambda: _Session(rows)
    )

    agent = player_profiles.PlayerProfilesAgent.__new__(
        player_profiles.PlayerProfilesAgent
    )
    out = await agent._get_team_injury_profiles("CIN")

    higgins = out["Tee Higgins"]
    assert higgins["availability_risk"] == "monitor"
    assert higgins["projected_games"] == 14
    assert higgins["risk_modifier"] == -0.05
    for removed in (
        "chronic_conditions", "pattern_flags", "risk_notes",
        "overall_risk_level", "recovery_assessment",
    ):
        assert removed not in higgins

    # LEFT JOIN: player with no profile row still gets unknown availability
    assert out["No Profile Guy"]["availability_risk"] == "unknown"
    assert out["No Profile Guy"]["risk_modifier"] == 0.0
