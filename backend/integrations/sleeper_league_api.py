"""
Sleeper LeaguePlatformAPI implementation.
Public API — no auth required. Username only.
"""
from __future__ import annotations

import logging

import httpx

from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, RosteredPlayer, TeamRoster,
    Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague

logger = logging.getLogger(__name__)

SLEEPER_BASE = "https://api.sleeper.app/v1"


class SleeperLeagueAPI(LeaguePlatformAPI):
    """Sleeper Fantasy — public API, no auth required."""

    def __init__(self, league: UserLeague):
        self._league = league

    async def _get(self, path: str) -> dict | list:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SLEEPER_BASE}{path}")
            resp.raise_for_status()
            return resp.json()

    async def get_rosters(self) -> list[TeamRoster]:
        rosters = await self._get(
            f"/league/{self._league.league_id}/rosters"
        )
        users = await self._get(
            f"/league/{self._league.league_id}/users"
        )
        user_map = {u["user_id"]: u for u in users}

        result: list[TeamRoster] = []
        for roster in rosters:
            user = user_map.get(roster.get("owner_id"), {})
            player_ids = roster.get("players") or []
            players = [
                RosteredPlayer(
                    platform_player_id=pid,
                    player_name="",
                    position="",
                    team_abbr="",
                )
                for pid in player_ids
            ]
            result.append(TeamRoster(
                platform_team_id=str(roster["roster_id"]),
                manager_name=user.get("display_name", ""),
                team_name=user.get("metadata", {}).get("team_name", ""),
                players=players,
                faab_remaining=roster.get("settings", {}).get(
                    "waiver_budget_used", 0
                ),
                wins=roster.get("settings", {}).get("wins", 0),
                losses=roster.get("settings", {}).get("losses", 0),
            ))
        return result

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        # Sleeper doesn't have a free agent endpoint.
        # Derive: all NFL players NOT on any roster.
        return []

    async def get_draft_picks(
        self, *, league_key: str | None = None,
    ) -> list[DraftPick]:
        drafts = await self._get(
            f"/league/{self._league.league_id}/drafts"
        )
        all_picks: list[DraftPick] = []
        for draft in drafts:
            picks = await self._get(
                f"/draft/{draft['draft_id']}/picks"
            )
            for pick in picks:
                metadata = pick.get("metadata", {})
                all_picks.append(DraftPick(
                    platform_player_id=pick.get("player_id", ""),
                    player_name=(
                        f"{metadata.get('first_name', '')} "
                        f"{metadata.get('last_name', '')}"
                    ).strip(),
                    position=metadata.get("position", ""),
                    team_abbr=metadata.get("team", ""),
                    picked_by_team_id=str(pick.get("roster_id", "")),
                    manager_name="",
                    pick_number=pick.get("pick_no", 0),
                    round_number=pick.get("round", 0),
                    auction_price=pick.get("amount"),
                ))
        return all_picks

    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        return []

    async def get_transactions(self, week: int) -> list[Transaction]:
        return []

    async def get_standings(self) -> list[TeamRoster]:
        return await self.get_rosters()
