"""
PlayerRepository — all Player table queries.

Owns query construction (filters, sorting, pagination, eager loads)
so routers never touch SQLAlchemy directly.
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from backend.models.dependency import PlayerDependency
from backend.models.player import Player
from backend.repositories.base import BaseRepository


def draftable_filter():
    """WHERE clause hiding pure-noise players (retired / deep practice squad).

    A player is shown only if FantasyPros lists them (has an ADP) OR the
    pipeline valued them above the $1 floor. This hides Roethlisberger-type
    rows — valued at $1 with no ADP — without deleting anything. Used by the
    player list and draft board so the same definition of "draftable" applies
    to both.

    K/DEF exception: they are $1 streamers BY DESIGN (ai_bid_ceiling == 1, and no
    FantasyPros ADP), so the generic "$1 + no ADP = noise" gate would hide every
    valued kicker/defense. A VALUED K/DEF (tier assigned by the T1 static pass) is
    legitimately draftable — the tier check keeps any future unvalued row hidden.
    """
    return or_(
        Player.market_value_fantasypros.isnot(None),
        Player.ai_bid_ceiling > 1,
        and_(Player.position.in_(("K", "DEF")), Player.tier.isnot(None)),
    )

# Relationships needed to build a PlayerSummary response.
_SUMMARY_LOADS = (
    selectinload(Player.dependencies),
    selectinload(Player.injury_profile),
    selectinload(Player.schedule),
    selectinload(Player.historic_prices),
)

# Additional relationships for the full PlayerDetail response.
_DETAIL_LOADS = _SUMMARY_LOADS + (
    selectinload(Player.profile),
    selectinload(Player.beat_signals),
)

# Whitelist of sortable columns exposed by GET /players.
SORTABLE_COLUMNS = {
    "bid_ceiling": Player.recommended_bid_ceiling,
    "ai_ceiling": Player.ai_bid_ceiling,
    "system_value": Player.baseline_value,
    "market_value": Player.market_value,
    "value_gap": Player.value_gap,
    "name": Player.name,
    "tier": Player.tier,
    "adp_diff": Player.adp_diff,
    "adp_rank": Player.adp_rank,
}

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


class PlayerRepository(BaseRepository[Player]):
    """Read access to players and their pipeline-generated relations."""

    model = Player

    async def list_with_league_market_values(self) -> list[Player]:
        """Skill-position players that have a league market value set."""
        result = await self._session.execute(
            select(Player)
            .where(Player.market_value_league.isnot(None))
            .where(Player.position.in_(SKILL_POSITIONS))
        )
        return list(result.scalars().all())

    async def count_by_team(self) -> dict[str, int]:
        """Player counts keyed by team abbreviation."""
        result = await self._session.execute(
            select(Player.team_abbr, func.count(Player.id))
            .where(Player.team_abbr.isnot(None))
            .group_by(Player.team_abbr)
        )
        return dict(result.all())

    async def list_skill_players_for_team(self, team_abbr: str) -> list[Player]:
        """A team's skill-position players, best bid ceilings first."""
        result = await self._session.execute(
            select(Player)
            .where(Player.team_abbr == team_abbr)
            .where(Player.position.in_(SKILL_POSITIONS))
            .options(selectinload(Player.dependencies))
            .order_by(Player.recommended_bid_ceiling.desc().nulls_last())
        )
        return list(result.scalars().all())

    async def _find_by_id_col(self, column, value: str | None) -> Player | None:
        """Exact match on one indexed platform-id column. Returns None if unset."""
        v = (value or "").strip()
        if v.endswith(".0") and v[:-2].isdigit():
            v = v[:-2]
        if not v:
            return None
        result = await self._session.execute(
            select(Player).where(column == v).limit(1)
        )
        return result.scalar_one_or_none()

    async def find_by_sleeper_id(self, sleeper_id: str) -> Player | None:
        """Exact match on the indexed Sleeper id — the canonical id for Sleeper
        drafts (whose pick/nomination frames are id-only)."""
        return await self._find_by_id_col(Player.sleeper_id, sleeper_id)

    async def find_by_espn_id(self, espn_id: str) -> Player | None:
        """Exact match on the ESPN player id (from ESPN roster entries)."""
        return await self._find_by_id_col(Player.espn_id, espn_id)

    async def find_by_yahoo_id(self, yahoo_id: str) -> Player | None:
        """Exact match on the REAL Yahoo id (bare numeric; the tail of a Yahoo
        player_key "449.p.<id>"). NOT yahoo_player_id (the gsis-derived trap)."""
        return await self._find_by_id_col(Player.yahoo_id, yahoo_id)

    async def find_by_dst_team(self, team_or_name: str | None) -> Player | None:
        """DETERMINISTIC DST resolution — a team defense isn't an NFL player (0/32
        crosswalk to espn/yahoo ids), so DST NEVER fuzzy-matches. Resolve by team
        abbr (exact) or the full DEF name ("Denver Broncos"), position='DEF'. 32
        teams → exact. Mirrors the team-keyed DEF-prior/dst_team_map convention."""
        key = (team_or_name or "").strip()
        if not key:
            return None
        result = await self._session.execute(
            select(Player)
            .where(Player.position == "DEF")
            .where(or_(
                func.upper(Player.team_abbr) == key.upper(),
                func.lower(Player.name) == key.lower(),
            ))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def resolve_player(
        self,
        *,
        sleeper_id: str | None = None,
        espn_id: str | None = None,
        yahoo_id: str | None = None,
        gsis_id: str | None = None,
        sportradar_id: str | None = None,
        name: str | None = None,
        position: str | None = None,
        team: str | None = None,
    ) -> Player | None:
        """THE canonical resolver: stable IDs first (deterministic), guarded name
        LAST. Tries sleeper → sportradar → gsis → espn → yahoo (exact, indexed);
        DST routes to the team map; only if no id resolves does it fall to the
        shared #217 guard (position filter + first-name agreement + collision
        REFUSAL + loud-warn) — never an unverified candidates[0]."""
        from backend.utils.player_resolver import guarded_name_pick

        # DST: team-keyed, never name-fuzzy.
        if (position or "").upper() == "DEF":
            return await self.find_by_dst_team(team or name)

        # ID-first — deterministic, exact.
        for col, val in (
            (Player.sleeper_id, sleeper_id),
            (Player.sportradar_id, sportradar_id),
            (Player.gsis_id, gsis_id),
            (Player.espn_id, espn_id),
            (Player.yahoo_id, yahoo_id),
        ):
            hit = await self._find_by_id_col(col, val)
            if hit is not None:
                return hit

        # Guarded name fallback (last resort). Candidates by last-name contains,
        # position-filtered in-query when known; the guard makes the final call.
        if not name:
            return None
        from backend.agents.roster_changes import _norm_name

        parts = _norm_name(name).split()
        if not parts:
            return None
        query = select(Player).where(Player.name.ilike(f"%{parts[-1]}%"))
        if position:
            query = query.where(Player.position == position.upper())
        query = query.order_by(Player.recommended_bid_ceiling.desc().nulls_last())
        candidates = list((await self._session.execute(query)).scalars().all())
        return guarded_name_pick(candidates, name, team=team, position=position)

    async def find_by_name_fuzzy(self, name: str) -> Player | None:
        """Resolve a display name to a single Player via the canonical resolver's
        GUARDED name path (draft-room DOM / demo names are name-only). Delegates to
        ``resolve_player`` so the #217 collision guard is inherited from ONE place —
        the old unguarded ``candidates[0]`` fallback is gone (a last-name-only
        collision now returns None + loud-warn, never a wrong same-surname pick)."""
        return await self.resolve_player(name=name)

    async def search_by_name(self, q: str, limit: int = 20) -> list[Player]:
        """Case-insensitive name search, best bid ceilings first."""
        result = await self._session.execute(
            select(Player)
            .where(Player.name.ilike(f"%{q}%"))
            .options(*_SUMMARY_LOADS)
            .order_by(Player.recommended_bid_ceiling.desc().nulls_last())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_detail(self, player_id: uuid.UUID) -> Player | None:
        """Fetch one player with every relation the detail view needs."""
        result = await self._session.execute(
            select(Player)
            .where(Player.id == player_id)
            .options(*_DETAIL_LOADS)
        )
        return result.scalar_one_or_none()

    async def count_by_position_tier(self) -> list[tuple[str, int | None, int]]:
        """(position, tier, count) rows for skill positions."""
        result = await self._session.execute(
            select(
                Player.position,
                Player.tier,
                func.count(Player.id),
            )
            .where(Player.position.in_(SKILL_POSITIONS))
            .group_by(Player.position, Player.tier)
        )
        return list(result.all())

    async def count_all(self) -> int:
        """Total number of player rows."""
        result = await self._session.execute(select(func.count(Player.id)))
        return result.scalar() or 0

    async def list_filtered(
        self,
        *,
        position: str | None = None,
        tier: int | None = None,
        team: str | None = None,
        flag: str | None = None,
        value_gap_dir: str | None = None,
        snake_flag: str | None = None,
        sort: str = "bid_ceiling",
        order: str = "desc",
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[Player], int]:
        """Filtered, sorted, paginated player list.

        Returns (players for the requested page, total matching count).
        Unknown sort keys fall back to bid_ceiling.
        """
        query = select(Player).options(*_SUMMARY_LOADS).where(draftable_filter())

        if position:
            query = query.where(Player.position == position.upper())
        if tier is not None:
            query = query.where(Player.tier == tier)
        if team:
            query = query.where(Player.team_abbr == team.upper())
        if value_gap_dir == "undervalued":
            query = query.where(Player.value_gap_signal == "market_undervalues")
        elif value_gap_dir == "overvalued":
            query = query.where(Player.value_gap_signal == "market_overvalues")
        elif value_gap_dir == "aligned":
            query = query.where(Player.value_gap_signal == "aligned")

        if snake_flag:
            query = query.where(Player.snake_flag == snake_flag)

        if flag == "flagged":
            query = query.where(
                Player.id.in_(select(PlayerDependency.player_id).distinct())
            )
        elif flag == "clean":
            query = query.where(
                ~Player.id.in_(select(PlayerDependency.player_id).distinct())
            )

        count_result = await self._session.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = count_result.scalar() or 0

        sort_col = SORTABLE_COLUMNS.get(sort, Player.recommended_bid_ceiling)
        if order == "asc":
            query = query.order_by(sort_col.asc().nulls_last())
        else:
            query = query.order_by(sort_col.desc().nulls_last())

        query = query.offset((page - 1) * per_page).limit(per_page)
        result = await self._session.execute(query)
        return list(result.scalars().all()), total
