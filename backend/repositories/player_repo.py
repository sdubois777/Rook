"""
PlayerRepository — all Player table queries.

Owns query construction (filters, sorting, pagination, eager loads)
so routers never touch SQLAlchemy directly.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
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
    """
    return or_(
        Player.market_value_fantasypros.isnot(None),
        Player.ai_bid_ceiling > 1,
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

    async def find_by_name_fuzzy(self, name: str) -> Player | None:
        """Resolve a (possibly inexact) display name to a single Player.

        Draft-room DOM names aren't always canonical ("Sam LaPorta" vs
        "Samuel LaPorta", "Brian Thomas" vs "Brian Thomas Jr."), so we try
        progressively looser matches and stop at the first hit:
          1. exact, case-insensitive
          2. suffix-normalized equality (reuses roster_changes._norm_name,
             which strips Jr/Sr/II/III/IV/V and lowercases)
          3. first-initial + last name (handles Sam vs Samuel)
          4. ILIKE-contains on the last name, best bid ceiling first
        Returns None when nothing plausibly matches.
        """
        from backend.agents.roster_changes import _norm_name

        raw = (name or "").strip()
        if not raw:
            return None
        normalized = _norm_name(raw)

        # 1. Exact (case-insensitive)
        result = await self._session.execute(
            select(Player)
            .where(func.lower(Player.name) == raw.lower())
            .order_by(Player.recommended_bid_ceiling.desc().nulls_last())
            .limit(1)
        )
        exact = result.scalar_one_or_none()
        if exact:
            return exact

        parts = normalized.split()
        if not parts:
            return None
        first, last = parts[0], parts[-1]

        # Narrow to candidates whose name contains the last name, best
        # bid ceiling first so the contains-fallback prefers real players.
        result = await self._session.execute(
            select(Player)
            .where(Player.name.ilike(f"%{last}%"))
            .order_by(Player.recommended_bid_ceiling.desc().nulls_last())
        )
        candidates = list(result.scalars().all())
        if not candidates:
            return None

        # 2. Suffix-normalized equality
        for c in candidates:
            if _norm_name(c.name) == normalized:
                return c

        # 3. First-initial + last name (Sam LaPorta -> Samuel LaPorta)
        for c in candidates:
            cn = _norm_name(c.name).split()
            if len(cn) >= 2 and cn[-1] == last and cn[0][:1] == first[:1]:
                return c

        # 4. Best-ceiling contains-match fallback
        return candidates[0]

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
