"""
ESPN LeaguePlatformAPI implementation.
Cookie-based unofficial API. Validates cookies on first use.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import AppError
from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, RosteredPlayer, TeamRoster,
    Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague
from backend.repositories.credential_repo import CredentialRepository

logger = logging.getLogger(__name__)

ESPN_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

# ESPN position ID mapping
_ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}


class ESPNLeagueAPI(LeaguePlatformAPI):
    """ESPN Fantasy Football — cookie-based unofficial API."""

    def __init__(
        self,
        league: UserLeague,
        espn_s2: str,
        swid: str,
    ):
        self._league = league
        self._cookies = {"espn_s2": espn_s2, "SWID": swid}

    @classmethod
    async def create(
        cls,
        league: UserLeague,
        db: AsyncSession,
    ) -> ESPNLeagueAPI:
        repo = CredentialRepository(db)
        cookies = await repo.get_espn_cookies(league.user_id)
        if not cookies:
            raise AppError(
                "ESPN not connected — use the ESPN bookmarklet",
                {"platform": "espn", "action": "bookmarklet"},
            )
        espn_s2, swid = cookies
        return cls(league=league, espn_s2=espn_s2, swid=swid)

    async def _get(
        self,
        view: str,
        season: int | None = None,
    ) -> dict:
        season = season or self._league.season_year
        url = (
            f"{ESPN_BASE}/seasons/{season}/segments/0"
            f"/leagues/{self._league.league_id}"
        )
        async with httpx.AsyncClient(
            cookies=self._cookies, timeout=15.0
        ) as client:
            resp = await client.get(url, params={"view": view})
            if resp.status_code == 401:
                raise AppError(
                    "ESPN cookies expired — please reconnect",
                    {
                        "platform": "espn",
                        "action": "reconnect",
                        "bookmarklet_url": "/league-setup?platform=espn",
                    },
                )
            resp.raise_for_status()
            return resp.json()

    async def validate_cookies(self) -> bool:
        """Verify cookies work before storing."""
        await self._get("mSettings")
        return True

    async def get_rosters(self) -> list[TeamRoster]:
        data = await self._get("mRoster")
        teams = data.get("teams", [])
        result: list[TeamRoster] = []
        for team in teams:
            players: list[RosteredPlayer] = []
            roster = team.get("roster", {}).get("entries", [])
            for entry in roster:
                p = entry.get("playerPoolEntry", {}).get("player", {})
                players.append(RosteredPlayer(
                    platform_player_id=str(p.get("id", "")),
                    player_name=p.get("fullName", ""),
                    position=_ESPN_POS.get(p.get("defaultPositionId", 0), ""),
                    team_abbr="",
                ))
            result.append(TeamRoster(
                platform_team_id=str(team.get("id", "")),
                manager_name="",
                team_name=team.get("name", team.get("abbrev", "")),
                players=players,
            ))
        return result

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        return []

    async def get_draft_picks(
        self, *, league_key: str | None = None,
    ) -> list[DraftPick]:
        data = await self._get("mDraftDetail")
        picks_raw = data.get("draftDetail", {}).get("picks", [])
        result: list[DraftPick] = []
        for pick in picks_raw:
            player_id = pick.get("playerId", "")
            result.append(DraftPick(
                platform_player_id=str(player_id),
                player_name="",
                position="",
                team_abbr="",
                picked_by_team_id=str(pick.get("teamId", "")),
                manager_name="",
                pick_number=pick.get("overallPickNumber", 0),
                round_number=pick.get("roundId", 0),
                auction_price=pick.get("bidAmount"),
            ))
        return result

    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        return []

    async def get_transactions(self, week: int) -> list[Transaction]:
        return []

    async def get_standings(self) -> list[TeamRoster]:
        return await self.get_rosters()
