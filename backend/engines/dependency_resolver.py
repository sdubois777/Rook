"""
Dependency resolver — activates player flags based on live draft state.

Pure Python, no API calls. Checks each player's dependency flags against
the set of already-drafted player IDs. This is what catches the
McConkey/Allen scenario in real time.

Canonical example:
  McConkey has DISPLACED flag with trigger=Allen (active_and_healthy).
  If Allen's yahoo_player_id is in drafted_player_ids → flag activates,
  McConkey's bid ceiling drops by value_impact_pct (-35%).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class DependencyResolver:
    """Resolve dependency flags against current draft state."""

    def apply_active_flags(
        self,
        dependencies: list[dict],
        drafted_player_ids: set[str],
    ) -> tuple[list[dict], float]:
        """
        Check all dependency flags for a player against who has been drafted.

        Args:
            dependencies: List of dependency flag dicts, each with:
                flag_type, trigger_yahoo_player_id, trigger_player_name,
                trigger_condition, value_impact_pct, confidence
            drafted_player_ids: Set of yahoo_player_id strings already drafted.

        Returns:
            (active_flags, total_value_modifier)
            active_flags: list of flag dicts with active=True and reason
            total_value_modifier: combined fractional modifier (e.g. -0.35)
        """
        active_flags: list[dict] = []
        total_modifier = 0.0

        for flag in dependencies:
            trigger_id = flag.get("trigger_yahoo_player_id")
            if not trigger_id:
                continue

            flag_type = flag.get("flag_type", "")
            trigger_condition = flag.get("trigger_condition", "")
            impact = float(flag.get("value_impact_pct", 0))

            # Normalize: AI model may output whole percentages (35 = 35%),
            # Python-generated flags use fractions (0.35 = 35%).
            if abs(impact) > 1.0:
                impact /= 100.0

            trigger_drafted = trigger_id in drafted_player_ids

            # DISPLACED: active when trigger IS drafted (playing on the team)
            if (
                flag_type == "displaced"
                and trigger_condition == "active_and_healthy"
                and trigger_drafted
            ):
                active_flags.append({
                    **flag,
                    "active": True,
                    "reason": f"{flag.get('trigger_player_name', '?')} already drafted",
                })
                total_modifier += impact

            # BENEFICIARY with departed_team: always active (trade already happened)
            elif (
                flag_type == "beneficiary"
                and trigger_condition == "departed_team"
            ):
                active_flags.append({
                    **flag,
                    "active": True,
                    "reason": f"{flag.get('trigger_player_name', '?')} departed team",
                })
                total_modifier += impact

            # CONTINGENT: surface as info but not "active" during auction
            # (can't determine injury status during a live draft)

            # BENEFICIARY with injured/absent: skip during draft
            # (can't confirm health status)

        return active_flags, total_modifier
