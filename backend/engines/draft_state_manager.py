"""
Live draft state manager — pure Python, no API calls.

Maintains the complete draft state updated after every pick event.
Used by LiveDraftEngine to calculate budget constraints, track rosters,
and identify drafted players for dependency resolution.

File is intentionally named draft_state_manager.py (not draft_state.py)
because backend/models/draft_state.py already exists with the ORM models.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default roster slots per LEAGUE_RULES.md
_DEFAULT_ROSTER_SLOTS: dict[str, int] = {
    "QB": 1, "RB": 2, "WR": 2, "FLEX": 1, "TE": 1,
    "K": 1, "DEF": 1, "BENCH": 7,
}


@dataclass
class LeagueConfig:
    """Runtime league settings — loaded from DB or constructed with defaults."""

    auction_budget: int = 200
    min_bid: int = 1
    roster_slots: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_ROSTER_SLOTS))

    @property
    def total_roster_size(self) -> int:
        """Derived from roster_slots to avoid the seeded column bug (15 vs 16)."""
        return sum(self.roster_slots.values())

    @classmethod
    async def from_db(cls) -> LeagueConfig:
        """Load from league_settings table, fall back to defaults."""
        from sqlalchemy import select
        from backend.database import AsyncSessionLocal
        from backend.models.league_settings import LeagueSettings

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(LeagueSettings).limit(1))
            row = result.scalar_one_or_none()

        if row is None:
            logger.warning("No league_settings row found — using defaults")
            return cls()

        return cls(
            auction_budget=row.auction_budget,
            min_bid=row.min_bid,
            roster_slots=row.roster_slots or dict(_DEFAULT_ROSTER_SLOTS),
        )


@dataclass
class DraftPick:
    """Immutable record of a single draft pick."""

    player_id: str        # yahoo_player_id
    team_id: str          # yahoo_team_id of the drafting team
    price: int
    player_name: str = ""
    position: str = ""
    tier: int | None = None


class DraftStateManager:
    """
    Maintains live draft state updated after every pick.

    Pure Python — no API calls, no DB writes.
    All methods are O(1) or O(n) where n is the number of picks so far.
    """

    def __init__(self, league_config: LeagueConfig, your_team_id: str):
        self.league_config = league_config
        self.your_team_id = your_team_id

        self.picks: list[DraftPick] = []
        self.opponent_rosters: dict[str, list[DraftPick]] = {}
        self.your_roster: list[DraftPick] = []
        self.opponent_budgets: dict[str, int] = {}
        self.your_budget: int = league_config.auction_budget

    def record_pick(self, pick: DraftPick) -> None:
        """Called after every draft_pick event from the bridge."""
        self.picks.append(pick)

        if pick.team_id == self.your_team_id:
            self.your_roster.append(pick)
            self.your_budget -= pick.price
        else:
            self.opponent_rosters.setdefault(pick.team_id, []).append(pick)
            self.opponent_budgets[pick.team_id] = (
                self.opponent_budgets.get(
                    pick.team_id, self.league_config.auction_budget
                )
                - pick.price
            )

    def get_drafted_player_ids(self) -> set[str]:
        """All player_ids that have been drafted so far."""
        return {p.player_id for p in self.picks}

    def get_your_remaining_budget(self) -> int:
        """Your remaining auction budget."""
        return self.your_budget

    def get_roster_slots_remaining(self) -> int:
        """How many roster slots you still need to fill."""
        return self.league_config.total_roster_size - len(self.your_roster)

    def get_minimum_completion_budget(self) -> int:
        """Minimum $1 per remaining roster slot (including current)."""
        return self.get_roster_slots_remaining() * self.league_config.min_bid

    def get_spendable_on_this_player(self) -> int:
        """Maximum you can bid on the current nomination and still complete your roster."""
        return max(0, self.your_budget - self.get_minimum_completion_budget())

    def get_your_positional_counts(self) -> dict[str, int]:
        """Count of players at each position in your roster."""
        counts: dict[str, int] = {}
        for pick in self.your_roster:
            counts[pick.position] = counts.get(pick.position, 0) + 1
        return counts
