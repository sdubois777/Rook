"""Derived own-team label: generic default, mid-session upgrade, non-destructive,
and — critically — attribution never depends on the label (is_yours does)."""
from backend.engines.draft_state_manager import (
    DEFAULT_TEAM_LABEL,
    DraftPick,
    DraftStateManager,
    LeagueConfig,
)


def _mgr(label=""):
    return DraftStateManager(LeagueConfig(auction_budget=200), label)


def test_default_is_generic_label():
    assert _mgr().your_team_id == DEFAULT_TEAM_LABEL
    assert _mgr("").your_team_id == DEFAULT_TEAM_LABEL


def test_set_name_upgrades_from_generic():
    m = _mgr()
    assert m.set_your_team_name("Gridiron Gang") is True
    assert m.your_team_id == "Gridiron Gang"


def test_set_name_is_non_destructive_once_real():
    m = _mgr()
    m.set_your_team_name("Real Name")
    assert m.set_your_team_name("Later Name") is False  # keep the first real name
    assert m.your_team_id == "Real Name"


def test_set_name_blank_or_none_is_noop():
    m = _mgr()
    assert m.set_your_team_name("") is False
    assert m.set_your_team_name(None) is False
    assert m.set_your_team_name("   ") is False
    assert m.your_team_id == DEFAULT_TEAM_LABEL


def test_attribution_uses_is_yours_not_label():
    """A generic label never matches a slot label — is_yours must still route the
    pick to your roster, and an opponent's slot pick must not."""
    m = _mgr()  # label = "Your Team"
    m.record_pick(DraftPick(player_id="p1", team_id="Team 5", price=10), is_yours=True)
    assert [p.player_id for p in m.your_roster] == ["p1"]

    m.record_pick(DraftPick(player_id="p2", team_id="Team 3", price=5), is_yours=False)
    assert [p.player_id for p in m.your_roster] == ["p1"]  # unchanged
    assert "Team 3" in m.opponent_rosters


def test_snapshot_roundtrip_preserves_label():
    m = _mgr()
    m.set_your_team_name("Snapshot Squad")
    restored = DraftStateManager.from_dict(m.to_dict())
    assert restored.your_team_id == "Snapshot Squad"


def test_snapshot_roundtrip_generic_stays_generic():
    restored = DraftStateManager.from_dict(_mgr().to_dict())
    assert restored.your_team_id == DEFAULT_TEAM_LABEL
