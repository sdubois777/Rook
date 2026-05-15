# Stage 14: Season Roster Store + Post-Draft Sync

## Before starting, read:
- `docs/stages/stage-28-league-sync.md` (must be complete — platform credentials in DB)
- `docs/stages/stage-25-saas-foundation.md` (LeagueConfig, repositories, services)
- `docs/INSEASON.md`

---

## Goal

After a user's draft completes, sync their drafted roster into the season
roster store. All in-season agents (Stages 15-24) read from this store.
This stage also defines the **platform abstraction layer** — a single
interface that Yahoo, ESPN, and Sleeper all implement, used by every
in-season agent. Define it once here. Never redefine it.

---

## Enterprise standards enforced in this stage

- Platform abstraction: all platform-specific code behind `LeaguePlatformAPI`
- Repository pattern: all DB queries in `SeasonRosterRepository`
- Service layer: all business logic in `RosterSyncService`
- No direct DB queries in routers or agents
- Dependency injection via FastAPI `Depends()`
- Row-level security: all queries filtered by `user_id`

---

## Part 1 — Platform abstraction layer

**Define once. Never duplicate. All in-season stages import from here.**

### Data models

```python
# backend/integrations/platform_models.py
"""
Shared data models for platform API responses.
All three platforms (Yahoo, ESPN, Sleeper) map their
responses to these models before returning.

Agents and services work with these models exclusively —
never with raw platform API responses.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RosteredPlayer:
    """A player on a fantasy team's roster."""
    platform_player_id: str
    player_name: str
    position: str           # QB, RB, WR, TE, K, DEF
    team_abbr: str          # NFL team
    is_starter: bool = False
    injury_status: Optional[str] = None
    # full | questionable | doubtful | out | None


@dataclass
class TeamRoster:
    """One fantasy team's full roster."""
    platform_team_id: str
    manager_name: str
    team_name: str
    players: list[RosteredPlayer] = field(default_factory=list)
    faab_remaining: Optional[int] = None
    wins: int = 0
    losses: int = 0
    points_for: float = 0.0


@dataclass
class FreeAgent:
    """An unowned player available on waiver wire."""
    platform_player_id: str
    player_name: str
    position: str
    team_abbr: str
    ownership_pct: float = 0.0
    waiver_priority: Optional[int] = None


@dataclass
class DraftPick:
    """A single pick from a completed draft."""
    platform_player_id: str
    player_name: str
    position: str
    team_abbr: str
    picked_by_team_id: str
    manager_name: str
    pick_number: int
    round_number: int
    auction_price: Optional[int] = None  # None for snake drafts


@dataclass
class WeeklyMatchup:
    """One matchup between two teams for a week."""
    week: int
    home_team_id: str
    away_team_id: str
    home_score: float
    away_score: float
    is_complete: bool


@dataclass
class Transaction:
    """A waiver claim, trade, or free agent add."""
    type: str               # add | drop | trade
    player_name: str
    position: str
    added_by_team_id: Optional[str] = None
    dropped_by_team_id: Optional[str] = None
    week: int = 0
    faab_bid: Optional[int] = None
```

### Abstract base class

```python
# backend/integrations/platform_api.py
"""
LeaguePlatformAPI — abstract interface for all fantasy platforms.

Yahoo, ESPN, and Sleeper each implement this interface.
All in-season agents call methods on this interface — never
on platform-specific classes directly.

Usage:
    platform = await get_platform_api(user_league, db)
    rosters = await platform.get_rosters()
    # Works identically for Yahoo, ESPN, and Sleeper
"""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from backend.integrations.platform_models import (
    DraftPick, FreeAgent, TeamRoster,
    Transaction, WeeklyMatchup,
)

if TYPE_CHECKING:
    from backend.models.user_league import UserLeague


class LeaguePlatformAPI(ABC):
    """
    Abstract interface all platforms implement.
    Never instantiate directly — use get_platform_api().
    """

    @abstractmethod
    async def get_rosters(self) -> list[TeamRoster]:
        """
        Current rosters for all teams in the league.
        Includes manager names, team names, players,
        injury statuses, and FAAB remaining.
        """

    @abstractmethod
    async def get_free_agents(
        self,
        position: str | None = None,
    ) -> list[FreeAgent]:
        """
        All unowned players available on waiver wire.
        Optionally filtered by position.
        Includes ownership percentages.
        """

    @abstractmethod
    async def get_draft_picks(self) -> list[DraftPick]:
        """
        All picks from the most recent completed draft.
        Includes auction prices for auction leagues,
        pick numbers for snake leagues.
        """

    @abstractmethod
    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        """
        All matchups for a given week.
        Includes scores if week is complete.
        """

    @abstractmethod
    async def get_transactions(
        self,
        week: int,
    ) -> list[Transaction]:
        """
        Waiver claims and free agent adds for a week.
        Used to track FAAB spending and roster moves.
        """

    @abstractmethod
    async def get_standings(self) -> list[TeamRoster]:
        """
        Current standings — rosters sorted by record.
        Same as get_rosters() but guaranteed to have
        wins/losses/points_for populated.
        """
```

### Platform factory

```python
# backend/integrations/platform_factory.py
"""
Single entry point for getting a platform API instance.

Import and use this function — never instantiate
Yahoo/ESPN/Sleeper classes directly in agents or services.
"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import AppError
from backend.integrations.platform_api import LeaguePlatformAPI
from backend.models.user_league import UserLeague

logger = logging.getLogger(__name__)


async def get_platform_api(
    user_league: UserLeague,
    db: AsyncSession,
) -> LeaguePlatformAPI:
    """
    Returns the correct platform API for a user's league.
    Loads credentials from DB — never from environment.

    Raises AppError if platform not connected or
    credentials are invalid/expired.
    """
    platform = user_league.platform

    if platform == "yahoo":
        from backend.integrations.yahoo_league_api import (
            YahooLeagueAPI,
        )
        return await YahooLeagueAPI.create(user_league, db)

    if platform == "espn":
        from backend.integrations.espn_league_api import (
            ESPNLeagueAPI,
        )
        return await ESPNLeagueAPI.create(user_league, db)

    if platform == "sleeper":
        from backend.integrations.sleeper_league_api import (
            SleeperLeagueAPI,
        )
        return SleeperLeagueAPI(user_league)

    raise AppError(
        f"Unknown platform: {platform}",
        {"supported": ["yahoo", "espn", "sleeper"]},
    )
```

### Platform implementations

Each platform implements `LeaguePlatformAPI` and maps its
API response format to the shared data models.

**Yahoo** (`backend/integrations/yahoo_league_api.py`):
- Uses per-user OAuth tokens from `platform_credentials`
- Auto-refreshes expired tokens before any API call
- Maps Yahoo team/player format to shared models

**ESPN** (`backend/integrations/espn_league_api.py`):
- Uses per-user `espn_s2` / `SWID` cookies from `platform_credentials`
- Uses unofficial ESPN fantasy API endpoints
- Validates cookies on first call, raises `AppError` if expired

**Sleeper** (`backend/integrations/sleeper_league_api.py`):
- No auth required — public API
- Uses `sleeper_user_id` from `platform_credentials` for user lookup
- Maps Sleeper player IDs via `sleeper_id` column on `players` table

Each implementation lives in its own file. No shared code between
implementations — the abstraction layer handles that.

---

## Part 2 — Season roster model

```python
# backend/models/season_roster.py
"""
SeasonRoster — tracks each drafted player through the season.

One record per player per user per season.
Updated weekly by the Roster Monitor agent (Stage 15).
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer,
    Numeric, String, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class SeasonRoster(Base):
    __tablename__ = "season_rosters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_leagues.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("players.id"),
        nullable=False,
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)

    # Acquisition
    platform_team_id: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    # Which fantasy team owns this player
    acquisition_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft"
    )
    # draft | waiver | free_agent | trade
    acquisition_price: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # Auction price — null for snake/waiver adds
    acquisition_week: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # 0 = draft, 1-18 = in-season week

    # Weekly tracking arrays — appended each week
    weekly_stats: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    # [{"week": 1, "ppr": 24.5, "carries": 18, ...}]

    weekly_snap_counts: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    # [{"week": 1, "snaps": 42, "snap_pct": 0.67}]

    weekly_target_share: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    # [{"week": 1, "targets": 8, "share": 0.22}]

    # Trade value flags — updated by Trade Value agent (Stage 17)
    sell_high_flag: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    buy_low_flag: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    injury_concern_flag: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    value_trend: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )
    # rising | stable | declining

    # Current status
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    # False = dropped/traded away

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
```

Register in `backend/models/__init__.py`.

---

## Part 3 — Season roster repository

```python
# backend/repositories/season_roster_repo.py
"""
SeasonRosterRepository — all season roster DB queries.

All queries automatically scoped to user_id.
Never returns another user's roster data.
"""
import uuid
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models.season_roster import SeasonRoster
from backend.repositories.base import BaseRepository


class SeasonRosterRepository(BaseRepository[SeasonRoster]):
    model = SeasonRoster

    async def get_user_roster(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
        active_only: bool = True,
    ) -> list[SeasonRoster]:
        """Get all players on a user's roster for a league."""
        query = (
            select(SeasonRoster)
            .where(
                SeasonRoster.user_id == user_id,
                SeasonRoster.user_league_id == user_league_id,
            )
        )
        if active_only:
            query = query.where(SeasonRoster.is_active.is_(True))
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_all_league_rosters(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
    ) -> dict[str, list[SeasonRoster]]:
        """
        Get all teams' rosters for a league, keyed by team_id.
        Used by opponent analysis agents.
        """
        result = await self._session.execute(
            select(SeasonRoster)
            .where(
                SeasonRoster.user_id == user_id,
                SeasonRoster.user_league_id == user_league_id,
                SeasonRoster.is_active.is_(True),
            )
        )
        rosters: dict[str, list[SeasonRoster]] = {}
        for row in result.scalars().all():
            rosters.setdefault(row.platform_team_id, []).append(row)
        return rosters

    async def upsert_player(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
        player_id: uuid.UUID,
        platform_team_id: str,
        season_year: int,
        acquisition_type: str,
        acquisition_price: Optional[int],
        acquisition_week: int,
    ) -> SeasonRoster:
        """
        Insert or update a roster record.
        Safe to call multiple times — idempotent.
        """
        await self._session.execute(
            pg_insert(SeasonRoster)
            .values(
                user_id=user_id,
                user_league_id=user_league_id,
                player_id=player_id,
                platform_team_id=platform_team_id,
                season_year=season_year,
                acquisition_type=acquisition_type,
                acquisition_price=acquisition_price,
                acquisition_week=acquisition_week,
                weekly_stats=[],
                weekly_snap_counts=[],
                weekly_target_share=[],
            )
            .on_conflict_do_update(
                index_elements=[
                    "user_id", "user_league_id",
                    "player_id", "season_year",
                ],
                set_={
                    "platform_team_id": platform_team_id,
                    "acquisition_type": acquisition_type,
                    "acquisition_price": acquisition_price,
                    "is_active": True,
                },
            )
        )

    async def append_weekly_stats(
        self,
        roster_id: uuid.UUID,
        week: int,
        stats: dict,
        snap_data: dict,
        target_data: dict,
    ) -> None:
        """
        Append one week of stats to the arrays.
        Never replaces existing weeks — appends only.
        """
        roster = await self.get_or_404(roster_id)

        # Avoid duplicate week entries
        existing_weeks = {
            w["week"] for w in roster.weekly_stats
        }
        if week in existing_weeks:
            return

        roster.weekly_stats = [
            *roster.weekly_stats, {"week": week, **stats}
        ]
        roster.weekly_snap_counts = [
            *roster.weekly_snap_counts,
            {"week": week, **snap_data},
        ]
        roster.weekly_target_share = [
            *roster.weekly_target_share,
            {"week": week, **target_data},
        ]
        await self._session.flush()
```

---

## Part 4 — Roster sync service

```python
# backend/services/roster_sync_service.py
"""
RosterSyncService — post-draft and in-season roster sync.

Handles:
  - Post-draft sync: map draft picks to player DB records
  - In-season adds/drops: update roster when transactions happen
  - Multi-user: each user's roster is independently scoped

Never calls platform APIs directly — receives DraftPick
and TeamRoster objects from the platform abstraction layer.
"""
import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.platform_models import DraftPick, Transaction
from backend.models.player import Player
from backend.repositories.season_roster_repo import (
    SeasonRosterRepository,
)

logger = logging.getLogger(__name__)


class RosterSyncService:
    def __init__(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
        season_year: int,
    ):
        self._db = db
        self._user_id = user_id
        self._user_league_id = user_league_id
        self._season_year = season_year
        self._repo = SeasonRosterRepository(db)

    async def sync_draft_results(
        self,
        picks: list[DraftPick],
    ) -> dict:
        """
        Sync draft results into season_roster table.
        Maps each pick to a player DB record by name + position.
        Returns summary: {synced, unmatched, skipped}.
        """
        synced = 0
        unmatched = []

        for pick in picks:
            player_id = await self._match_player(
                pick.player_name, pick.position
            )
            if not player_id:
                unmatched.append(pick.player_name)
                logger.warning(
                    "Post-draft sync: no DB match for '%s' (%s)",
                    pick.player_name,
                    pick.position,
                )
                continue

            await self._repo.upsert_player(
                user_id=self._user_id,
                user_league_id=self._user_league_id,
                player_id=player_id,
                platform_team_id=pick.picked_by_team_id,
                season_year=self._season_year,
                acquisition_type="draft",
                acquisition_price=pick.auction_price,
                acquisition_week=0,
            )
            synced += 1

        await self._db.commit()
        logger.info(
            "Post-draft sync: %d synced, %d unmatched",
            synced, len(unmatched),
        )
        return {
            "synced": synced,
            "unmatched": unmatched,
            "unmatched_count": len(unmatched),
        }

    async def sync_transaction(
        self,
        tx: Transaction,
        week: int,
    ) -> None:
        """
        Apply a waiver/FA transaction to the roster store.
        Marks dropped players inactive, adds new players.
        """
        if tx.added_by_team_id and tx.player_name:
            player_id = await self._match_player(
                tx.player_name, tx.position
            )
            if player_id:
                await self._repo.upsert_player(
                    user_id=self._user_id,
                    user_league_id=self._user_league_id,
                    player_id=player_id,
                    platform_team_id=tx.added_by_team_id,
                    season_year=self._season_year,
                    acquisition_type=tx.type,
                    acquisition_price=tx.faab_bid,
                    acquisition_week=week,
                )

        await self._db.commit()

    async def _match_player(
        self,
        player_name: str,
        position: str,
    ) -> Optional[uuid.UUID]:
        """
        Match a platform player name to a DB player record.
        Uses name + position to avoid cross-position collisions.
        Never matches by last name alone.
        """
        result = await self._db.execute(
            select(Player.id)
            .where(
                Player.name.ilike(player_name),
                Player.position == position,
            )
            .limit(1)
        )
        row = result.first()
        return row[0] if row else None
```

---

## Part 5 — API endpoints

```python
# backend/routers/season_roster.py
"""
Season roster endpoints.
All endpoints require authentication and league membership.
All data scoped to current user.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import get_current_user, get_db
from backend.core.exceptions import NotFoundError
from backend.models.user import User
from backend.repositories.league_repo import LeagueRepository
from backend.repositories.season_roster_repo import (
    SeasonRosterRepository,
)
from backend.services.roster_sync_service import RosterSyncService

router = APIRouter(prefix="/roster", tags=["season-roster"])


class RosterPlayer(BaseModel):
    player_id: str
    player_name: str
    position: str
    platform_team_id: str
    acquisition_type: str
    acquisition_price: Optional[int]
    acquisition_week: int
    sell_high_flag: bool
    buy_low_flag: bool
    injury_concern_flag: bool


class RosterResponse(BaseModel):
    league_id: str
    players: list[RosterPlayer]
    total: int


async def _get_user_league(
    league_id: uuid.UUID,
    user: User,
    db: AsyncSession,
):
    """Shared helper — get league and verify ownership."""
    repo = LeagueRepository(db)
    league = await repo.get_user_league(user.id, league_id)
    if not league:
        raise NotFoundError(f"League {league_id} not found")
    return league


@router.get("/{league_id}/mine", response_model=RosterResponse)
async def get_my_roster(
    league_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Your drafted roster for a league."""
    league = await _get_user_league(league_id, user, db)
    repo = SeasonRosterRepository(db)
    players = await repo.get_user_roster(user.id, league.id)
    return _build_response(str(league_id), players)


@router.get("/{league_id}/all", response_model=dict)
async def get_all_rosters(
    league_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """All teams' rosters in a league (for opponent analysis)."""
    league = await _get_user_league(league_id, user, db)
    repo = SeasonRosterRepository(db)
    rosters = await repo.get_all_league_rosters(user.id, league.id)
    return {
        team_id: _build_response(str(league_id), players)
        for team_id, players in rosters.items()
    }


@router.post("/{league_id}/sync-draft")
async def sync_draft_results(
    league_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger post-draft sync for a league.
    Pulls draft picks from platform API and populates
    the season_roster table.
    """
    from backend.integrations.platform_factory import (
        get_platform_api,
    )
    from backend.utils.seasons import get_current_season

    league = await _get_user_league(league_id, user, db)
    platform = await get_platform_api(league, db)

    picks = await platform.get_draft_picks()
    service = RosterSyncService(
        db=db,
        user_id=user.id,
        user_league_id=league.id,
        season_year=get_current_season(),
    )
    result = await service.sync_draft_results(picks)
    return {"status": "complete", **result}


def _build_response(
    league_id: str,
    players: list,
) -> RosterResponse:
    return RosterResponse(
        league_id=league_id,
        players=[
            RosterPlayer(
                player_id=str(p.player_id),
                player_name="",  # joined in real impl
                position="",
                platform_team_id=p.platform_team_id,
                acquisition_type=p.acquisition_type,
                acquisition_price=p.acquisition_price,
                acquisition_week=p.acquisition_week,
                sell_high_flag=p.sell_high_flag,
                buy_low_flag=p.buy_low_flag,
                injury_concern_flag=p.injury_concern_flag,
            )
            for p in players
        ],
        total=len(players),
    )
```

Register in `backend/main.py`:
```python
from backend.routers import season_roster
app.include_router(season_roster.router)
# Add "roster" to _API_PREFIXES
```

---

## Part 6 — APScheduler job registration

Register all in-season weekly jobs in `backend/main.py`.
Jobs are registered here but agents are implemented in
their respective stages (15-24).

```python
# In startup_checks() — add after existing Beat Reporter job

from backend.utils.nfl_schedule import is_nfl_season

# Only register in-season jobs if NFL season is active.
# In offseason, these jobs are registered but exit immediately
# when called (each agent checks is_nfl_season() first).

_scheduler.add_job(
    _run_roster_monitor,
    "cron",
    day_of_week="wed",
    hour=8,
    timezone="America/New_York",
    id="roster_monitor_weekly",
    replace_existing=True,
)

_scheduler.add_job(
    _run_trade_value,
    "cron",
    day_of_week="wed",
    hour=9,
    timezone="America/New_York",
    id="trade_value_weekly",
    replace_existing=True,
)

_scheduler.add_job(
    _run_opponent_analyzer,
    "cron",
    day_of_week="wed",
    hour=10,
    timezone="America/New_York",
    id="opponent_analyzer_weekly",
    replace_existing=True,
)

_scheduler.add_job(
    _run_waiver_wire,
    "cron",
    day_of_week="tue",
    hour=23,
    timezone="America/New_York",
    id="waiver_wire_weekly",
    replace_existing=True,
)

_scheduler.add_job(
    _run_lineup_optimizer,
    "cron",
    day_of_week="thu",
    hour=13,
    timezone="America/New_York",
    id="lineup_optimizer_weekly",
    replace_existing=True,
)


# Each job wrapper checks is_nfl_season() and exits fast if offseason.
# Wrappers are thin — delegate to agent implementations.

async def _run_roster_monitor():
    if not is_nfl_season():
        return
    from backend.agents.roster_monitor import RosterMonitorAgent
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        agent = RosterMonitorAgent(db)
        await agent.run_all_users()


async def _run_trade_value():
    if not is_nfl_season():
        return
    from backend.agents.trade_value import TradeValueAgent
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        agent = TradeValueAgent(db)
        await agent.run_all_users()

# ... same pattern for opponent_analyzer, waiver_wire,
# lineup_optimizer
```

---

## Part 7 — Alembic migration

```bash
alembic revision --autogenerate \
  -m "stage14_season_rosters"

# Verify migration creates:
#   - season_rosters table
#   - Indexes on user_id, user_league_id, player_id
# Apply:
alembic upgrade head
```

---

## Required test cases

```python
# tests/unit/integrations/test_platform_models.py
def test_team_roster_defaults_empty_player_list()
def test_free_agent_ownership_pct_defaults_zero()
def test_draft_pick_auction_price_optional()

# tests/unit/services/test_roster_sync.py
def test_sync_draft_results_matches_players_to_db()
def test_sync_draft_results_logs_unmatched()
def test_sync_is_idempotent()  # running twice doesn't duplicate
def test_append_weekly_stats_does_not_duplicate_week()
def test_player_match_uses_position_not_just_name()
    # J.Taylor (IND RB) never matches J.J. Taylor (FA WR)

# tests/unit/repositories/test_season_roster_repo.py
def test_get_user_roster_scoped_to_user()
def test_get_all_league_rosters_keyed_by_team()
def test_user_a_cannot_see_user_b_roster()
def test_upsert_player_is_idempotent()

# tests/unit/routers/test_season_roster.py
def test_sync_draft_endpoint_requires_auth()
def test_get_my_roster_requires_league_ownership()
def test_get_all_rosters_returns_all_teams()
```

---

## Verification

```bash
# 1. Platform abstraction compiles
python -c "
from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_factory import get_platform_api
from backend.integrations.platform_models import (
    TeamRoster, FreeAgent, DraftPick
)
print('Platform abstraction: OK')
"

# 2. Season roster model registered
python -c "
from backend.models.season_roster import SeasonRoster
print('SeasonRoster model: OK')
"

# 3. Migration applied
alembic current  # should show head

# 4. Post-draft sync endpoint works
# (requires a real connected Yahoo/ESPN/Sleeper league)
```

---

## Commit order

```
Commit 1:
feat(platform): LeaguePlatformAPI abstraction layer

Platform models: TeamRoster, FreeAgent, DraftPick,
WeeklyMatchup, Transaction.
Abstract base: LeaguePlatformAPI with 5 methods.
Factory: get_platform_api() — single entry point.
Yahoo, ESPN, Sleeper implementations.
No platform-specific code outside integration layer.

Commit 2:
feat(season-roster): SeasonRosterRepository
and RosterSyncService

SeasonRoster model with weekly tracking arrays.
Repository: user-scoped all queries.
RosterSyncService: draft sync, transaction sync,
player matching by name+position.
Idempotent upserts — safe to re-run.

Commit 3:
feat(season-roster): API endpoints and scheduler

GET /roster/{league_id}/mine
GET /roster/{league_id}/all
POST /roster/{league_id}/sync-draft
APScheduler weekly job registration for all
in-season agents. All jobs check is_nfl_season()
and exit immediately in offseason.
Migration: season_rosters table.
Coverage: X%.
```
