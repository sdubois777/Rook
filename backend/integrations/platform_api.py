"""
LeaguePlatformAPI — abstract interface for all fantasy platforms.

Yahoo, ESPN, and Sleeper each implement this interface.
All in-season agents call methods on this interface — never
on platform-specific classes directly.

Usage:
    platform = await get_platform_api(user_league, db)
    rosters = await platform.get_rosters()
    # Works identically for Yahoo, ESPN, and Sleeper
"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from backend.integrations.platform_models import (
    DraftPick, FreeAgent, LeagueMetadata, TeamRoster,
    Transaction, WeeklyMatchup,
)

if TYPE_CHECKING:
    from backend.models.user_league import UserLeague


class LeaguePlatformAPI(ABC):
    """
    Abstract interface all platforms implement.
    Never instantiate directly — use get_platform_api().
    """

    @abstractmethod
    async def get_rosters(self) -> list[TeamRoster]:
        """
        Current rosters for all teams in the league.
        Includes manager names, team names, players,
        injury statuses, and FAAB remaining.
        """

    @abstractmethod
    async def get_free_agents(
        self,
        position: str | None = None,
    ) -> list[FreeAgent]:
        """
        All unowned players available on waiver wire.
        Optionally filtered by position.
        Includes ownership percentages.
        """

    @abstractmethod
    async def get_draft_picks(
        self, *, league_key: str | None = None,
    ) -> list[DraftPick]:
        """
        All picks from the completed draft.
        Includes auction prices (if auction format).

        Optional league_key overrides the default key — used by
        sync to fetch historical seasons with season-specific keys.
        """

    @abstractmethod
    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        """Matchups for a given week."""

    @abstractmethod
    async def get_transactions(self, week: int) -> list[Transaction]:
        """Transactions (adds, drops, trades) for a given week."""

    @abstractmethod
    async def get_standings(self) -> list[TeamRoster]:
        """Current standings with wins/losses/points."""

    async def get_roster_slots(self) -> dict | None:
        """Per-league starting-lineup config as the canonical {slot_type: count}
        (T3), or None to fall back to the default lineup. Default: None (platforms
        that carry it override — Sleeper/ESPN). Yahoo populates it via
        get_league_settings on the settings-sync path instead."""
        return None

    async def get_league_metadata(self) -> LeagueMetadata:
        """Pre-draft league metadata (name / scoring / team_count / draft_type /
        draft_date), read from a response the platform already fetches. Default:
        empty (Yahoo uses its own get_league_settings path). Sleeper/ESPN override."""
        return LeagueMetadata()
