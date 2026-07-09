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

from backend.config import settings
from backend.integrations.platform_factory import get_platform_api
from backend.models.user_league import UserLeague
from backend.repositories.league_repo import LeagueRepository
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)

# How many historical seasons to import
HISTORY_SEASONS = settings.league_sync_history_seasons


class LeagueSyncService:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID):
        self._db = db
        self._user_id = user_id
        self._league_repo = LeagueRepository(db)

    async def sync_league(
        self,
        user_league_id: uuid.UUID,
        league_key: str | None = None,
    ) -> dict:
        """
        Full sync for a connected league.

        Accepts UUID, reloads the ORM object using the service's
        own session to avoid detached/expired instance errors.
        """
        from backend.integrations.yahoo_api import yahoo_league_key

        # Reload within THIS session — not the router's
        user_league = await self._league_repo.get_user_league(
            self._user_id, user_league_id
        )
        if not user_league:
            from backend.core.exceptions import NotFoundError
            raise NotFoundError(
                f"League {user_league_id} not found"
            )

        platform = await get_platform_api(user_league, self._db)
        current_season = get_current_season()

        # 0. Fetch and store platform-specific league settings
        if user_league.platform == "yahoo":
            await self._sync_yahoo_settings(user_league, league_key)  # sets roster_slots
        elif user_league.platform == "espn":
            try:
                draft_type, budget = await platform.detect_draft_type()
                if user_league.draft_type != draft_type:
                    logger.info(
                        "ESPN draft type updated: %s → %s",
                        user_league.draft_type, draft_type,
                    )
                    user_league.draft_type = draft_type
                    if budget is not None:
                        user_league.budget = budget
            except Exception as exc:
                logger.warning("ESPN draft type re-detection failed: %s", exc)
            # T3 lineup config (defensive/sample-gated — unknown id → default).
            try:
                user_league.roster_slots = await platform.get_roster_slots()
            except Exception as exc:
                logger.warning("ESPN roster-slots sync failed: %s", exc)
            # Name / scoring / draft_date from the SAME mSettings response (stop discarding).
            try:
                self._apply_league_metadata(user_league, await platform.get_league_metadata())
            except Exception as exc:
                logger.warning("ESPN league-metadata sync failed: %s", exc)
        elif user_league.platform == "sleeper":
            # T3 lineup config from /v1/league roster_positions (verified live).
            try:
                user_league.roster_slots = await platform.get_roster_slots()
            except Exception as exc:
                logger.warning("Sleeper roster-slots sync failed: %s", exc)
            # Name / scoring / draft_type / draft_date from /league + /drafts.
            try:
                self._apply_league_metadata(user_league, await platform.get_league_metadata())
            except Exception as exc:
                logger.warning("Sleeper league-metadata sync failed: %s", exc)

        summary = {
            "platform": user_league.platform,
            "league_id": user_league.league_id,
            "picks_imported": 0,
            "seasons_imported": 0,
            "managers_found": 0,
            "free_agents_cached": 0,
            "warnings": [],
        }

        # 1. Import current rosters — required, fail hard.
        # A league we cannot read rosters for is not synced in any sense.
        rosters = await platform.get_rosters()
        summary["managers_found"] = len(rosters)
        # manager_name OR team_name — ESPN leaves manager_name blank but carries the
        # real team_name, so this stores a real name for every platform (fixes the
        # ESPN all-blank opponent-names bug).
        user_league.manager_map = {
            r.platform_team_id: (r.manager_name or r.team_name or "")
            for r in rosters
        }
        # Real team count from the actual roster list (was stuck at connect default 12
        # for ESPN/Sleeper; Yahoo sets it from settings).
        if rosters and user_league.platform in ("espn", "sleeper"):
            user_league.team_count = len(rosters)

        # is_active SELF-HEAL: recompute from the season on EVERY sync (initial +
        # re-sync). Previously set once at connect and never touched here, so it went
        # stale over the calendar rollover and a re-sync couldn't repair it.
        user_league.is_active = (user_league.season_year == current_season)

        # 2. Stamp last_synced NOW. Draft history is optional context —
        # a new league with no draft yet must still read as synced.
        user_league.last_synced = datetime.now(timezone.utc)
        await self._db.commit()

        # 3. Import draft history — up to HISTORY_SEASONS, best-effort
        picks_total = 0
        seasons_ok = 0
        for offset in range(HISTORY_SEASONS):
            season = current_season - offset - 1  # completed seasons only
            if season < 2020:
                break

            # Build season-specific league key for Yahoo
            season_key = None
            if user_league.platform == "yahoo":
                season_key = yahoo_league_key(
                    user_league.league_id, season
                )

            try:
                logger.info(
                    "Fetching draft picks: platform=%s key=%s season=%d",
                    user_league.platform,
                    season_key or user_league.league_id,
                    season,
                )
                picks = await platform.get_draft_picks(
                    league_key=season_key
                )
                logger.info(
                    "Got %d picks for season %d", len(picks), season
                )
                if picks:
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
                summary["warnings"].append(
                    f"No draft history for {season}"
                )
                # Rollback so the transaction isn't permanently aborted
                await self._db.rollback()

        summary["picks_imported"] = picks_total
        summary["seasons_imported"] = seasons_ok

        # 4. Cache free agents count — best-effort
        try:
            free_agents = await platform.get_free_agents()
            summary["free_agents_cached"] = len(free_agents)
        except Exception as exc:
            logger.warning("Could not cache free agents: %s", exc)
            summary["warnings"].append("Free agent sync failed")

        await self._db.commit()
        return summary

    def _apply_league_metadata(self, user_league: UserLeague, meta) -> None:
        """Store the non-None fields of a LeagueMetadata onto the league. None means
        the platform didn't expose it → keep the existing value (never clobber with a
        default)."""
        if meta.name:
            user_league.league_name = meta.name
        if meta.scoring:
            user_league.scoring = meta.scoring
        if meta.team_count:
            user_league.team_count = meta.team_count
        if meta.draft_type:
            user_league.draft_type = meta.draft_type
        if meta.draft_date:
            user_league.draft_date = meta.draft_date

    async def _sync_yahoo_settings(
        self, user_league: UserLeague, league_key: str | None = None,
    ) -> None:
        """Fetch Yahoo league settings and update user_league record."""
        try:
            from backend.integrations.yahoo_api import (
                get_league_settings,
                refresh_access_token_for_user,
                yahoo_league_key,
            )
            from backend.repositories.credential_repo import CredentialRepository

            key = league_key or yahoo_league_key(
                user_league.league_id, user_league.season_year
            )
            repo = CredentialRepository(self._db)
            tokens = await repo.get_yahoo_tokens(self._user_id)
            if not tokens:
                logger.warning(
                    "No Yahoo tokens for user %s — skipping settings sync",
                    self._user_id,
                )
                return

            access_token, refresh_token, expires_at = tokens
            if expires_at and datetime.now(timezone.utc) >= expires_at:
                access_token, refresh_token, new_expiry = (
                    await refresh_access_token_for_user(refresh_token)
                )
                await repo.upsert_yahoo(
                    self._user_id, access_token, refresh_token, new_expiry,
                )

            settings = await get_league_settings(access_token, key)
            user_league.league_name = settings["name"]
            user_league.team_count = settings["num_teams"]
            user_league.draft_type = settings["draft_type"]
            user_league.scoring = settings["scoring_type"]
            user_league.budget = settings.get("auction_budget")
            # Per-league lineup (T3): authoritative when the settings parse; null →
            # default lineup. A re-sync updates it (idempotent).
            user_league.roster_slots = settings.get("roster_slots")
            # Settings get_league_settings already pulls but sync used to drop.
            if settings.get("draft_date"):
                user_league.draft_date = settings["draft_date"]
            user_league.trade_deadline = settings.get("trade_deadline") or None
            user_league.waiver_type = settings.get("waiver_type") or None
            user_league.playoff_start_week = settings.get("playoff_start_week")
            await self._db.flush()
            logger.info(
                "Yahoo settings synced: name=%s teams=%d draft=%s scoring=%s",
                settings["name"], settings["num_teams"],
                settings["draft_type"], settings["scoring_type"],
            )
        except Exception as exc:
            logger.warning("Could not fetch Yahoo league settings: %s", exc)

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
                    user_id=self._user_id,
                    user_league_id=user_league_id,
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
