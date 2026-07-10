"""
ESPN LeaguePlatformAPI implementation.
Cookie-based unofficial API. Validates cookies on first use.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import AppError
from datetime import datetime, timezone

from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, LeagueMetadata, RosteredPlayer, TeamRoster,
    Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague
from backend.repositories.credential_repo import CredentialRepository

logger = logging.getLogger(__name__)

ESPN_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

# ESPN position ID mapping
_ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# ESPN proTeamId → NFL abbr (Rook's DEF team_abbr set). Populates the roster entry's
# NFL team — REQUIRED for DETERMINISTIC DST resolution (a team defense has no espn
# player id, so resolve_player routes position=DEF by team abbr → find_by_dst_team).
_ESPN_PROTEAM = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
    9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LA", 15: "MIA", 16: "MIN",
    17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",
    25: "SF", 26: "SEA", 27: "TB", 28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}


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

    async def _team_meta(self) -> dict[str, dict]:
        """{team_id: {"name", "owners"}} from the mTeam view. The mRoster view carries
        only {id, roster} — team names AND owner SWIDs live in mTeam, so this is the ONLY
        source of both ESPN team names and the owner-identity list (is_me binding)."""
        try:
            data = await self._get("mTeam")
        except Exception as exc:
            logger.warning("ESPN team-meta (mTeam) fetch failed: %s", exc)
            return {}
        out: dict[str, dict] = {}
        for t in data.get("teams", []):
            tid = str(t.get("id", ""))
            name = (
                t.get("name")
                or " ".join(x for x in (t.get("location"), t.get("nickname")) if x).strip()
                or t.get("abbrev", "")
            )
            # ALL owners (SWID GUIDs), not just primaryOwner — co-owned teams bind too.
            owners = [str(o) for o in (t.get("owners") or []) if o]
            if not owners and t.get("primaryOwner"):
                owners = [str(t["primaryOwner"])]
            if tid:
                out[tid] = {"name": name, "owners": owners}
        return out

    async def get_rosters(self) -> list[TeamRoster]:
        data = await self._get("mRoster")
        teams = data.get("teams", [])
        meta = await self._team_meta()        # mTeam — the only source of ESPN names + owners
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
                    # NFL team from proTeamId (deterministic DST resolution needs it).
                    team_abbr=_ESPN_PROTEAM.get(p.get("proTeamId"), ""),
                ))
            tid = str(team.get("id", ""))
            tmeta = meta.get(tid, {})
            result.append(TeamRoster(
                platform_team_id=tid,
                manager_name="",
                team_name=tmeta.get("name") or team.get("name") or team.get("abbrev", "") or f"Team {tid}",
                players=players,
                owner_ids=tmeta.get("owners", []),
            ))
        return result

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        return []

    async def get_roster_slots(self) -> dict | None:
        """ESPN `mSettings.settings.rosterSettings.lineupSlotCounts` = {slot_id:
        count} → canonical {slot_type: count}. The slot-id enum is CONFIRMED (real
        mSettings sample), so this is AUTHORITATIVE for synced ESPN leagues. The
        DEFENSIVE guard stays: a nonzero unknown id (enum is NOT _ESPN_POS, the
        player enum) → whole-league fallback; None on any failure → default
        lineup."""
        from backend.services.roster_slots import slots_from_espn_lineup_slots
        try:
            data = await self._get("mSettings")
        except Exception:
            return None
        roster = (data.get("settings", {}) or {}).get("rosterSettings", {}) or {}
        counts = roster.get("lineupSlotCounts")
        if not isinstance(counts, dict):
            return None
        return slots_from_espn_lineup_slots(counts, league=str(self._league.league_id))

    async def get_league_metadata(self) -> LeagueMetadata:
        """ESPN `mSettings` (settings.name, .size, .scoringSettings, .draftSettings.date)
        — the SAME view already fetched for roster_slots, previously mined only for
        lineupSlotCounts. draft_type comes from detect_draft_type (mDraftDetail), not
        here. Fails soft: any missing field stays None."""
        meta = LeagueMetadata()
        try:
            data = await self._get("mSettings")
        except Exception as exc:
            logger.warning("ESPN league metadata fetch failed: %s", exc)
            return meta
        s = data.get("settings", {}) or {}
        meta.name = s.get("name") or None
        meta.team_count = s.get("size") or None
        # scoring: reception points (statId 53) → ppr/half/standard
        items = (s.get("scoringSettings", {}) or {}).get("scoringItems", []) or []
        for it in items:
            if it.get("statId") == 53:
                pts = it.get("points", it.get("pointsOverrides", {}))
                try:
                    p = float(pts) if not isinstance(pts, dict) else None
                except (TypeError, ValueError):
                    p = None
                if p is not None:
                    meta.scoring = "ppr" if p >= 1.0 else ("half_ppr" if p >= 0.5 else "standard")
                break
        date_ms = (s.get("draftSettings", {}) or {}).get("date")
        if date_ms:
            try:
                meta.draft_date = datetime.fromtimestamp(int(date_ms) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                pass
        return meta

    async def detect_draft_type(self) -> tuple[str, int | None]:
        """Auction vs snake + budget.

        PRIMARY: ``mSettings.settings.draftSettings.type`` ('AUCTION'/'SNAKE') — the
        authoritative flag, present PRE-DRAFT, with the real ``auctionBudget``. The old
        code inferred auction ONLY from ``bidAmount > 0`` in mDraftDetail picks, which
        are all 0 until the auction runs → every UNDRAFTED ESPN auction league was
        mis-stored as snake (the exact mirror of the Yahoo is_auction_draft bug).

        FALLBACK: the mDraftDetail bidAmount check, used ONLY when draftSettings.type is
        absent/ambiguous — an empty pre-draft picks array can no longer override the
        explicit type. Loud-warns when neither signal is available.

        Returns (draft_type, budget): budget = real auctionBudget for auction, None for
        snake (so snake never carries a stale budget).
        """
        # PRIMARY — authoritative draft settings (available pre-draft)
        try:
            s = (await self._get("mSettings")).get("settings", {}) or {}
            ds = s.get("draftSettings", {}) or {}
            dtype = str(ds.get("type", "")).strip().upper()
            if dtype == "AUCTION":
                budget = ds.get("auctionBudget")
                try:
                    budget = int(budget) if budget else 200
                except (TypeError, ValueError):
                    budget = 200
                return ("auction", budget)
            if dtype == "SNAKE":
                return ("snake", None)
        except Exception as exc:
            logger.warning(
                "ESPN %s draftSettings.type read failed: %s — falling back to picks",
                self._league.league_id, exc,
            )

        # FALLBACK — post-draft bidAmount signal (unreliable pre-draft)
        try:
            data = await self._get("mDraftDetail")
            picks = data.get("draftDetail", {}).get("picks", [])
            if any((p.get("bidAmount") or 0) > 0 for p in picks):
                return ("auction", 200)
            if not picks:
                logger.warning(
                    "ESPN %s: draftSettings.type absent AND no draft picks — draft type "
                    "undetectable, defaulting snake", self._league.league_id,
                )
            return ("snake", None)
        except Exception as exc:
            logger.warning("ESPN draft type detection failed: %s", exc)
            return ("snake", None)

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
