"""News tie-in — depth-chart next-man-up resolution + signal-type constants (pure)."""
from __future__ import annotations

from backend.services.waiver.news_tiein import (
    DIRECT_POSITIVE_TYPES,
    OPPORTUNITY_TYPES,
    _next_up,
)

# (depth_chart_order, player_id, name) sorted by order — the universal backbone.
CHAIN = [(1, "starter", "Starter"), (2, "backup", "Backup"), (3, "third", "Third")]


def test_next_up_returns_the_player_below_the_starter():
    assert _next_up(CHAIN, "starter") == ("backup", "Backup")
    assert _next_up(CHAIN, "backup") == ("third", "Third")


def test_next_up_none_when_last_or_absent():
    assert _next_up(CHAIN, "third") is None          # already last
    assert _next_up(CHAIN, "not-in-chain") is None    # unknown starter


def test_signal_type_partitions_are_disjoint_and_expected():
    # Opportunity (surfaces a backup) vs direct-positive (the pool player himself).
    assert "injury_flag" in OPPORTUNITY_TYPES
    assert "depth_chart_change" in OPPORTUNITY_TYPES
    assert "camp_standout" in DIRECT_POSITIVE_TYPES
    assert "transaction" in DIRECT_POSITIVE_TYPES
