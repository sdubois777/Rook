"""
Opponent threat analyzer — combo detection, block values, nomination strategy.

Pure Python, no API calls. Evaluates opponent roster compositions to detect
dangerous combos and determine block value for nominated players.

Combo patterns from docs/stages/stage-12-live-draft.md:
  - Elite RB Stack: 2+ tier-1 RBs on same roster (critical)
  - Elite RB + Elite TE: tier-1 RB + tier-1 TE (high)
  - QB/WR Stack: QB + WR1 from same NFL team (medium)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.engines.draft_state_manager import DraftPick

logger = logging.getLogger(__name__)

# Threat score weights
_TIER_WEIGHT: dict[int, int] = {1: 25, 2: 15, 3: 8, 4: 3, 5: 1}
_POSITION_WEIGHT: dict[str, float] = {
    "RB": 1.3, "WR": 1.0, "TE": 0.9, "QB": 0.7,
    "K": 0.0, "DEF": 0.0,
}

# Block flag suppression threshold per LEAGUE_RULES.md
_BLOCK_BUDGET_THRESHOLD = 15


class OpponentThreatAnalyzer:
    """Evaluate opponent rosters for combo threats and blocking decisions."""

    def __init__(self, tendencies: dict[str, dict] | None = None) -> None:
        """
        Args:
            tendencies: Optional historical manager data, keyed by team_id.
                Each value: {
                    "style": "hero_rb"|"zero_rb"|"balanced",
                    "management_style": "stars_and_scrubs"|"conservative"|"analytical",
                    "positional_bias": {"RB": 1.3, "WR": 0.9, ...},
                }
                Loaded from OpponentProfile records at draft start.
        """
        self.tendencies = tendencies or {}

    def get_threat_score(self, roster: list[DraftPick], team_id: str | None = None) -> int:
        """
        Composite threat score 0-100 for an opponent's current roster.

        Higher score = more dangerous roster composition.
        """
        bias = (
            self.tendencies.get(team_id, {}).get("positional_bias", {})
            if team_id
            else {}
        )

        raw = 0.0
        for pick in roster:
            tier_w = _TIER_WEIGHT.get(pick.tier or 5, 1)
            pos_w = _POSITION_WEIGHT.get(pick.position, 0.5)
            pos_bias = bias.get(pick.position, 1.0)
            raw += tier_w * pos_w * pos_bias
        return min(100, int(raw))

    def get_block_value(
        self,
        player: dict,
        opponent_roster: list[DraftPick],
        opponent_budget: int,
    ) -> float:
        """
        What is this player worth to a specific opponent?

        Returns 0.0 when blocking is not warranted:
          - opponent budget < $15 (tapped out, per LEAGUE_RULES.md)
          - no combo threat would be created

        Returns value > player's system_value when blocking IS warranted.
        """
        if opponent_budget < _BLOCK_BUDGET_THRESHOLD:
            return 0.0

        player_tier = player.get("tier")
        player_pos = player.get("position", "")
        system_value = float(player.get("system_value", 0))

        # Check if adding this player creates a named combo pattern
        t1_rbs = sum(
            1 for p in opponent_roster
            if p.position == "RB" and p.tier == 1
        )
        t1_tes = sum(
            1 for p in opponent_roster
            if p.position == "TE" and p.tier == 1
        )

        # Elite RB Stack: opponent already has T1 RB, nominated player is T1 RB
        if player_pos == "RB" and player_tier == 1 and t1_rbs >= 1:
            return system_value * 1.5

        # Elite RB + Elite TE: opponent has T1 RB, nominated player is T1 TE
        if player_pos == "TE" and player_tier == 1 and t1_rbs >= 1:
            return system_value * 1.3

        # Elite RB + Elite TE (reverse): opponent has T1 TE, nominated is T1 RB
        if player_pos == "RB" and player_tier == 1 and t1_tes >= 1:
            return system_value * 1.3

        return 0.0

    def get_active_combo_flags(
        self, opponent_roster: list[DraftPick]
    ) -> list[str]:
        """Check if an opponent's current roster matches named combo patterns."""
        flags: list[str] = []

        t1_rbs = [p for p in opponent_roster if p.position == "RB" and p.tier == 1]
        t1_tes = [p for p in opponent_roster if p.position == "TE" and p.tier == 1]
        qbs = [p for p in opponent_roster if p.position == "QB"]
        wrs = [p for p in opponent_roster if p.position == "WR"]

        # Elite RB Stack: 2+ tier-1 RBs
        if len(t1_rbs) >= 2:
            names = ", ".join(p.player_name for p in t1_rbs if p.player_name)
            flags.append(
                f"Elite RB Stack — {names}. Historically dominant. Block if possible."
            )

        # Elite RB + Elite TE
        if len(t1_rbs) >= 1 and len(t1_tes) >= 1:
            flags.append(
                "Elite RB + Elite TE — Positional scarcity lock. Dangerous floor."
            )

        # QB/WR Stack: QB + WR from same NFL team
        qb_teams = {
            getattr(p, "team_abbr", None) or ""
            for p in qbs
            if getattr(p, "team_abbr", None)
        }
        for wr in wrs:
            wr_team = getattr(wr, "team_abbr", None) or ""
            if wr_team and wr_team in qb_teams:
                flags.append(
                    f"QB/WR Stack ({wr_team}) — Stack bonus upside. Volatile ceiling."
                )
                break  # Only flag once per roster

        return flags

    def get_nomination_targets(
        self,
        all_players: list[dict],
        your_roster: list[DraftPick],
        your_budget: int,
        drafted_ids: set[str] | None = None,
    ) -> list[dict]:
        """
        Identify players to nominate when it's your turn.

        Strategy: nominate high market-value players you DON'T want.
        Forces opponents to spend, draining their budgets.

        Args:
            all_players: List of player dicts with system_value, market_value, etc.
            your_roster: Your current roster picks.
            your_budget: Your remaining budget.
            drafted_ids: Set of already-drafted player IDs to exclude.

        Returns:
            Top 5 nomination targets with reason.
        """
        drafted = drafted_ids or set()
        your_positions = {p.position for p in your_roster}

        # Compute average positional bias across all opponents
        avg_bias = self._get_aggregate_positional_bias()

        candidates = []
        for p in all_players:
            pid = p.get("yahoo_player_id", "")
            if pid in drafted:
                continue

            mv = float(p.get("market_value", 0))
            sv = float(p.get("system_value", 0))
            if mv <= 0:
                continue

            overpay = mv - sv
            if overpay <= 0:
                continue  # Only nominate overvalued players

            # Weight by how much opponents historically overpay at this position
            pos = p.get("position", "")
            pos_weight = avg_bias.get(pos, 1.0)
            drain_score = round(overpay * pos_weight, 1)

            candidates.append({
                "yahoo_player_id": pid,
                "player_name": p.get("name", ""),
                "position": pos,
                "market_value": mv,
                "system_value": sv,
                "overpay_amount": round(overpay, 1),
                "drain_score": drain_score,
                "reason": f"Market overvalues by ${overpay:.0f} — drain opponent budgets",
            })

        # Sort by bias-weighted drain score descending
        candidates.sort(key=lambda c: c["drain_score"], reverse=True)
        return candidates[:5]

    def _get_aggregate_positional_bias(self) -> dict[str, float]:
        """Average positional bias across all opponents with tendencies."""
        if not self.tendencies:
            return {}

        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for t in self.tendencies.values():
            for pos, bias in t.get("positional_bias", {}).items():
                totals[pos] = totals.get(pos, 0.0) + bias
                counts[pos] = counts.get(pos, 0) + 1

        return {
            pos: totals[pos] / counts[pos]
            for pos in totals
        }
