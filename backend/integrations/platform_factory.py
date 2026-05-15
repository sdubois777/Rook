"""
Factory for platform API implementations.
Single entry point — never import platform classes directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.integrations.platform_api import LeaguePlatformAPI

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from backend.models.user_league import UserLeague


async def get_platform_api(
    league: "UserLeague",
    db: "AsyncSession",
) -> LeaguePlatformAPI:
    """
    Return the correct LeaguePlatformAPI implementation
    for the given league's platform.
    """
    platform = league.platform.lower()

    if platform == "yahoo":
        from backend.integrations.yahoo_league_api import YahooLeagueAPI
        return await YahooLeagueAPI.create(league, db)
    elif platform == "espn":
        from backend.integrations.espn_league_api import ESPNLeagueAPI
        return await ESPNLeagueAPI.create(league, db)
    elif platform == "sleeper":
        from backend.integrations.sleeper_league_api import SleeperLeagueAPI
        return SleeperLeagueAPI(league)
    else:
        raise ValueError(f"Unsupported platform: {platform}")
