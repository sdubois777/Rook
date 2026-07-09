"""
Sleeper LeaguePlatformAPI implementation.
Public API — no auth required. Username only.
"""
from __future__ import annotations

import logging

import httpx

from datetime import datetime, timezone

from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, LeagueMetadata, RosteredPlayer, TeamRoster,
    Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague

logger = logging.getLogger(__name__)

SLEEPER_BASE = "https://api.sleeper.app/v1"


def _scoring_from_rec(rec) -> str | None:
    """Points-per-reception → canonical scoring. None when not derivable."""
    try:
        r = float(rec)
    except (TypeError, ValueError):
        return None
    if r >= 1.0:
        return "ppr"
    if r >= 0.5:
        return "half_ppr"
    return "standard"


class SleeperLeagueAPI(LeaguePlatformAPI):
    """Sleeper Fantasy — public API, no auth required."""

    def __init__(self, league: UserLeague):
        self._league = league

    async def _get(self, path: str) -> dict | list:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SLEEPER_BASE}{path}")
            resp.raise_for_status()
            return resp.json()

    async def get_roster_slots(self) -> dict | None:
        """Sleeper `/v1/league/{id}.roster_positions` (unauthenticated) → canonical
        {slot_type: count}. The array IS the token list; bench = explicit BN count.
        VERIFIED LIVE in recon. None on any failure → default lineup."""
        from backend.services.roster_slots import slots_from_sleeper_league
        try:
            lg = await self._get(f"/league/{self._league.league_id}")
        except Exception:
            return None
        if not isinstance(lg, dict):
            return None
        return slots_from_sleeper_league(
            lg.get("roster_positions"), league=str(self._league.league_id)
        )

    async def get_league_metadata(self) -> LeagueMetadata:
        """Sleeper `/league/{id}` (name, scoring_settings.rec, total_rosters) +
        `/league/{id}/drafts` (draft type + start_time) — the same objects sync
        already touches, previously mined only for roster_positions. Fails soft:
        any missing field stays None (won't overwrite)."""
        meta = LeagueMetadata()
        try:
            lg = await self._get(f"/league/{self._league.league_id}")
            if isinstance(lg, dict):
                meta.name = lg.get("name") or None
                meta.team_count = lg.get("total_rosters") or None
                meta.scoring = _scoring_from_rec((lg.get("scoring_settings") or {}).get("rec"))
        except Exception as exc:
            logger.warning("Sleeper league metadata fetch failed: %s", exc)
        try:
            drafts = await self._get(f"/league/{self._league.league_id}/drafts")
            if isinstance(drafts, list) and drafts:
                draft = drafts[0]
                dtype = str(draft.get("type", "")).lower()
                meta.draft_type = "auction" if dtype == "auction" else "snake"
                start_ms = draft.get("start_time")
                if start_ms:
                    meta.draft_date = datetime.fromtimestamp(int(start_ms) / 1000, tz=timezone.utc)
        except Exception as exc:
            logger.warning("Sleeper draft metadata fetch failed: %s", exc)
        return meta

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
