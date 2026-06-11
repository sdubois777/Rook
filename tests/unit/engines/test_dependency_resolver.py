"""Direct tests for backend/engines/dependency_resolver.py.

The resolver activates a player's dependency flags against the set of
already-drafted player IDs — the live-draft half of the McConkey/Allen
canonical scenario.
"""
from __future__ import annotations

import pytest

from backend.engines.dependency_resolver import DependencyResolver


def _displaced_flag(trigger_id="allen-1", impact=-0.35, name="Keenan Allen"):
    return {
        "flag_type": "displaced",
        "trigger_yahoo_player_id": trigger_id,
        "trigger_player_name": name,
        "trigger_condition": "active_and_healthy",
        "value_impact_pct": impact,
        "confidence": "high",
    }


def test_displaced_flag_activates_when_trigger_drafted():
    """A displaced flag fires once its trigger player is drafted."""
    resolver = DependencyResolver()

    active, modifier = resolver.apply_active_flags(
        [_displaced_flag()], drafted_player_ids={"allen-1"}
    )

    assert len(active) == 1
    assert active[0]["active"] is True
    assert "Keenan Allen" in active[0]["reason"]
    assert modifier == -0.35


def test_displaced_flag_inactive_when_trigger_not_drafted():
    """A displaced flag stays dormant while the trigger is undrafted."""
    resolver = DependencyResolver()

    active, modifier = resolver.apply_active_flags(
        [_displaced_flag()], drafted_player_ids={"someone-else"}
    )

    assert active == []
    assert modifier == 0.0


def test_whole_percentage_impact_normalized_to_fraction():
    """AI-emitted whole percentages (35) normalize to fractions (0.35)."""
    resolver = DependencyResolver()

    _, modifier = resolver.apply_active_flags(
        [_displaced_flag(impact=-35)], drafted_player_ids={"allen-1"}
    )

    assert modifier == -0.35


def test_beneficiary_departed_team_always_active():
    """A beneficiary flag for a departed player fires regardless of draft state."""
    resolver = DependencyResolver()
    flag = {
        "flag_type": "beneficiary",
        "trigger_yahoo_player_id": "departed-9",
        "trigger_player_name": "Old Teammate",
        "trigger_condition": "departed_team",
        "value_impact_pct": 0.15,
    }

    active, modifier = resolver.apply_active_flags([flag], drafted_player_ids=set())

    assert len(active) == 1
    assert "departed team" in active[0]["reason"]
    assert modifier == 0.15


def test_contingent_flag_never_activates_during_draft():
    """Contingent flags need injury status, unknowable mid-draft — never active."""
    resolver = DependencyResolver()
    flag = {
        "flag_type": "contingent",
        "trigger_yahoo_player_id": "starter-1",
        "trigger_condition": "injured_or_absent",
        "value_impact_pct": 0.5,
    }

    active, modifier = resolver.apply_active_flags([flag], drafted_player_ids={"starter-1"})

    assert active == []
    assert modifier == 0.0


def test_flag_without_trigger_id_skipped():
    """Flags missing a trigger player ID are ignored entirely."""
    resolver = DependencyResolver()
    flag = _displaced_flag()
    flag["trigger_yahoo_player_id"] = None

    active, modifier = resolver.apply_active_flags([flag], drafted_player_ids={"allen-1"})

    assert active == []
    assert modifier == 0.0


def test_multiple_active_flags_sum_modifiers():
    """Several active flags combine into one total value modifier."""
    resolver = DependencyResolver()
    flags = [
        _displaced_flag(trigger_id="a", impact=-0.35),
        _displaced_flag(trigger_id="b", impact=-0.10, name="Other WR"),
    ]

    active, modifier = resolver.apply_active_flags(flags, drafted_player_ids={"a", "b"})

    assert len(active) == 2
    assert modifier == pytest.approx(-0.45)
