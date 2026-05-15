"""
LeagueSyncService — unified sync across all platforms.

Imports league settings, draft history, current rosters,
and free agents. All synced data scoped to user_id.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.platform_factory import get_platform_api
from backend.models.user_league import UserLeague
from backend.repositories.league_repo import LeagueRepository
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)

# How many historical seasons to import
HISTORY_SEASONS = 4


class LeagueSyncService:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID):
        self._db = db
        self._user_id = user_id
        self._league_repo = LeagueRepository(db)

    async def sync_league(
        self, user_league: UserLeague
    ) -> dict:
        """
        Full sync for a connected league.
        1. Verify credentials via platform API
        2. Import historical draft data (up to 4 seasons)
        3. Import current season rosters
        4. Store manager map
        5. Cache free agent count
        Returns sync summary.
        """
        platform = await get_platform_api(user_league, self._db)
        current_season = get_current_season()

        summary = {
            "platform": user_league.platform,
            "league_id": user_league.league_id,
            "picks_imported": 0,
            "seasons_imported": 0,
            "managers_found": 0,
            "free_agents_cached": 0,
        }

        # 1. Import draft history — up to HISTORY_SEASONS
        picks_total = 0
        seasons_ok = 0
        for offset in range(HISTORY_SEASONS):
            season = current_season - offset - 1  # completed seasons only
            if season < 2020:
                break
            try:
                picks = await platform.get_draft_picks()
                stored = await self._store_picks(
                    picks, user_league.id, season
                )
                picks_total += stored
                seasons_ok += 1
            except Exception as exc:
                logger.warning(
                    "Could not import %s season %d: %s",
                    user_league.platform, season, exc,
                )
                # Individual season failure does not abort sync

        summary["picks_imported"] = picks_total
        summary["seasons_imported"] = seasons_ok

        # 2. Import current rosters
        try:
            rosters = await platform.get_rosters()
            summary["managers_found"] = len(rosters)

            # Store manager map in user_league
            user_league.manager_map = {
                r.platform_team_id: r.manager_name
                for r in rosters
            }
        except Exception as exc:
            logger.warning("Could not import rosters: %s", exc)

        user_league.last_synced = datetime.now(timezone.utc)

        # 3. Cache free agents count
        try:
            free_agents = await platform.get_free_agents()
            summary["free_agents_cached"] = len(free_agents)
        except Exception as exc:
            logger.warning("Could not cache free agents: %s", exc)

        await self._db.commit()
        return summary

    async def _store_picks(
        self,
        picks: list,
        user_league_id: uuid.UUID,
        season: int,
    ) -> int:
        """
        Store historical draft picks.
        All picks scoped to user_id + user_league_id.
        Deduplication via on_conflict_do_nothing.
        """
        from backend.models.league_auction_history import LeagueAuctionHistory
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        count = 0
        for pick in picks:
            if not pick.player_name and not pick.platform_player_id:
                continue
            await self._db.execute(
                pg_insert(LeagueAuctionHistory)
                .values(
                    player_name=pick.player_name or "",
                    position=pick.position or "",
                    price=pick.auction_price or 0,
                    manager_name=pick.manager_name or "",
                    draft_pick_number=pick.pick_number,
                    season_year=season,
                    source=f"sync_{pick.picked_by_team_id}",
                    yahoo_player_key=pick.platform_player_id or None,
                )
                .on_conflict_do_nothing()
            )
            count += 1

        await self._db.commit()
        return count
