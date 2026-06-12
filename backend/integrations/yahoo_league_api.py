"""
Yahoo LeaguePlatformAPI implementation.
Loads per-user tokens from DB. Auto-refreshes on expiry.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.core.exceptions import AppError
from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, RosteredPlayer, TeamRoster,
    Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague
from backend.repositories.credential_repo import CredentialRepository

logger = logging.getLogger(__name__)

_YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
_YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


class YahooLeagueAPI(LeaguePlatformAPI):
    """Yahoo Fantasy Sports API — OAuth 2.0 per user."""

    def __init__(
        self,
        league: UserLeague,
        access_token: str,
        refresh_token: str,
        expires_at: datetime | None,
        credential_repo: CredentialRepository,
        user_id,
    ):
        self._league = league
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._repo = credential_repo
        self._user_id = user_id

    @classmethod
    async def create(
        cls,
        league: UserLeague,
        db: AsyncSession,
    ) -> YahooLeagueAPI:
        repo = CredentialRepository(db)
        tokens = await repo.get_yahoo_tokens(league.user_id)
        if not tokens:
            raise AppError(
                "Yahoo not connected — connect via /auth/yahoo/connect",
                {"platform": "yahoo", "action": "connect"},
            )
        access_token, refresh_token, expires_at = tokens
        return cls(
            league=league,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            credential_repo=repo,
            user_id=league.user_id,
        )

    async def _get_token(self) -> str:
        """Return valid access token, refreshing if expired."""
        if (
            self._expires_at
            and datetime.now(timezone.utc) >= self._expires_at
        ):
            await self._refresh()
        return self._access_token

    async def _refresh(self) -> None:
        """Exchange refresh token for new access token."""
        raw = f"{settings.yahoo_client_id}:{settings.yahoo_client_secret}"
        auth_header = f"Basic {base64.b64encode(raw.encode()).decode()}"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _YAHOO_TOKEN_URL,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", self._refresh_token)
        expires_in = int(data.get("expires_in", 3600))
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

        await self._repo.upsert_yahoo(
            self._user_id, new_access, new_refresh, new_expiry
        )
        self._access_token = new_access
        self._refresh_token = new_refresh
        self._expires_at = new_expiry
        logger.info("Yahoo token refreshed for user %s", self._user_id)

    async def _api_get(self, path: str) -> dict[str, Any]:
        """Authenticated GET against Yahoo Fantasy API."""
        token = await self._get_token()
        url = f"{_YAHOO_API_BASE}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                params={"format": "json"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    def _league_key(self) -> str:
        from backend.integrations.yahoo_api import yahoo_league_key
        return yahoo_league_key(
            self._league.league_id, self._league.season_year
        )

    async def get_rosters(self) -> list[TeamRoster]:
        data = await self._api_get(
            f"league/{self._league_key()}/teams//roster"
        )
        content = data.get("fantasy_content", {}).get("league", [{}, {}])
        teams_raw = content[1].get("teams", {}) if len(content) > 1 else {}

        result: list[TeamRoster] = []
        for key, val in teams_raw.items():
            if key == "count":
                continue
            team_data = val.get("team", [{}, {}])
            if len(team_data) < 2:
                continue
            team_info = team_data[0]
            if isinstance(team_info, list):
                merged: dict = {}
                for item in team_info:
                    if isinstance(item, dict):
                        merged.update(item)
                team_info = merged

            team_key = team_info.get("team_key", key)
            roster_data = team_data[1].get("roster", {}).get("players", {})
            players: list[RosteredPlayer] = []
            for pkey, pval in roster_data.items():
                if pkey == "count":
                    continue
                p = pval.get("player", [{}])[0]
                if isinstance(p, list):
                    info: dict = {}
                    for item in p:
                        if isinstance(item, dict):
                            info.update(item)
                    p = info
                name_data = p.get("name", {})
                full_name = name_data.get("full", "") if isinstance(name_data, dict) else ""
                players.append(RosteredPlayer(
                    platform_player_id=p.get("player_key", ""),
                    player_name=full_name,
                    position=p.get("display_position", "").split(",")[0],
                    team_abbr=p.get("editorial_team_abbr", ""),
                ))

            result.append(TeamRoster(
                platform_team_id=str(team_key),
                manager_name=team_info.get("name", ""),
                team_name=team_info.get("name", ""),
                players=players,
            ))

        return result

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        # Yahoo free agent endpoint requires active league
        return []

    async def get_draft_picks(
        self, *, league_key: str | None = None,
    ) -> list[DraftPick]:
        key = league_key or self._league_key()
        try:
            data = await self._api_get(
                f"league/{key}/draftresults"
            )
        except httpx.HTTPStatusError as exc:
            # Yahoo returns 400/403/404 on draftresults for leagues that
            # have not drafted yet (or did not exist that season).
            # That is valid empty history, not an error.
            if exc.response.status_code in (400, 403, 404):
                logger.info(
                    "No draft history for league %s — new league or pre-draft",
                    key,
                )
                return []
            raise  # Re-raise unexpected errors
        content = data.get("fantasy_content", {}).get("league", [{}, {}])
        results_raw = content[1].get("draft_results", {}) if len(content) > 1 else {}

        picks: list[DraftPick] = []
        for key, val in results_raw.items():
            if key == "count":
                continue
            pick = val.get("draft_result", {})
            picks.append(DraftPick(
                platform_player_id=pick.get("player_key", ""),
                player_name="",  # Resolved separately if needed
                position="",
                team_abbr="",
                picked_by_team_id=pick.get("team_key", ""),
                manager_name="",
                pick_number=int(pick.get("pick", 0)),
                round_number=int(pick.get("round", 0)),
                auction_price=int(pick["cost"]) if pick.get("cost") else None,
            ))
        return picks

    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        return []

    async def get_transactions(self, week: int) -> list[Transaction]:
        return []

    async def get_standings(self) -> list[TeamRoster]:
        return await self.get_rosters()
