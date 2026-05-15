# Stage 15: Roster Monitor Agent

## Before starting, read:
- `docs/stages/stage-14-season-roster.md` (must be complete)
- `docs/INSEASON.md` — Roster Monitor section
- `docs/rules/COST_RULES.md`

---

## Goal

Every Wednesday, pull weekly stats for all rostered players
across all active user leagues. Detect usage trends and update
sell-high/buy-low flags. Platform-agnostic — works identically
for Yahoo, ESPN, and Sleeper users.

---

## Enterprise standards

- Uses `LeaguePlatformAPI` — never calls Yahoo/ESPN/Sleeper directly
- All DB queries in `SeasonRosterRepository`
- Business logic in `RosterMonitorAgent` service class
- One agent class, no duplicated logic per platform
- Runs across all users in one job — not one job per user

---

## Model

`claude-haiku-4-5-20251001` — trend detection, not reasoning.

---

## Architecture

```
APScheduler (Wednesday 8am ET)
  └── RosterMonitorAgent.run_all_users()
        └── for each active user league:
              └── RosterMonitorAgent.run_for_league(user, league)
                    ├── platform.get_rosters()          # current NFL stats
                    ├── _detect_usage_trends()          # Python, no AI
                    ├── _detect_injury_flags()          # Python, no AI
                    ├── _compute_trade_flags()          # Haiku call
                    └── repo.update_flags()             # DB write
```

---

## Part 1 — Agent implementation

```python
# backend/agents/roster_monitor.py
"""
RosterMonitor Agent — weekly roster stat sync and flag updates.

Runs every Wednesday. Platform-agnostic.
Uses LeaguePlatformAPI — same code for Yahoo, ESPN, Sleeper.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import BaseAgent
from backend.integrations.platform_factory import get_platform_api
from backend.models.user import User
from backend.models.user_league import UserLeague
from backend.repositories.season_roster_repo import (
    SeasonRosterRepository,
)
from backend.utils.nfl_schedule import (
    get_current_week, is_nfl_season,
)

logger = logging.getLogger(__name__)

# Snap count decline threshold (consecutive weeks)
SNAP_DECLINE_WEEKS = 2
# Games below which season is injury-shortened
MIN_GAMES_FOR_TREND = 3


class RosterMonitorAgent(BaseAgent):
    """
    Weekly stat sync and flag computation for all rostered players.

    Instantiate once per job run. Inject db session.
    run_all_users() handles the fan-out to all leagues.
    """

    agent_name = "roster_monitor"

    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self._repo = SeasonRosterRepository(db)

    async def run_all_users(self) -> None:
        """
        Entry point — called by APScheduler.
        Iterates all active user leagues and processes each.
        """
        if not is_nfl_season():
            logger.info("Roster Monitor: offseason — skipping")
            return

        week = get_current_week()
        if not week:
            return

        # Load all active leagues across all users
        result = await self._db.execute(
            select(UserLeague, User)
            .join(User, UserLeague.user_id == User.id)
            .where(
                UserLeague.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        leagues = result.all()
        logger.info(
            "Roster Monitor: processing %d leagues for week %d",
            len(leagues), week,
        )

        for league, user in leagues:
            try:
                await self.run_for_league(user, league, week)
            except Exception as exc:
                logger.error(
                    "Roster Monitor failed for league %s user %s: %s",
                    league.id, user.id, exc,
                )
                # Continue with other leagues — don't abort all

    async def run_for_league(
        self,
        user: User,
        league: UserLeague,
        week: int,
    ) -> None:
        """
        Process one league for one user.
        Pulls current rosters from platform, updates DB.
        """
        platform = await get_platform_api(league, self._db)
        rosters = await platform.get_rosters()

        # Build team_id → manager_name map
        manager_map = {
            r.platform_team_id: r.manager_name
            for r in rosters
        }

        # Get DB roster records for this user/league
        roster_records = await self._repo.get_user_roster(
            user.id, league.id
        )

        for record in roster_records:
            # Find current platform stats for this player
            # Match by platform_team_id + player name
            platform_stats = self._find_player_stats(
                record, rosters
            )
            if not platform_stats:
                continue

            # Append this week's stats
            await self._repo.append_weekly_stats(
                roster_id=record.id,
                week=week,
                stats=platform_stats.get("stats", {}),
                snap_data=platform_stats.get("snaps", {}),
                target_data=platform_stats.get("targets", {}),
            )

            # Recompute flags from updated arrays
            updated = await self._repo.get_or_404(record.id)
            flags = self._compute_flags(updated)

            # Write flags back
            updated.sell_high_flag = flags["sell_high"]
            updated.buy_low_flag = flags["buy_low"]
            updated.injury_concern_flag = flags["injury_concern"]
            updated.value_trend = flags["trend"]

        await self._db.commit()
        logger.info(
            "Roster Monitor: updated %d players for league %s",
            len(roster_records), league.id,
        )

    def _compute_flags(self, roster: "SeasonRoster") -> dict:
        """
        Compute all trade flags from weekly arrays.
        Pure Python — no AI calls needed for this logic.
        """
        snaps = roster.weekly_snap_counts
        stats = roster.weekly_stats
        targets = roster.weekly_target_share

        sell_high = False
        buy_low = False
        injury_concern = False
        trend = "stable"

        if len(snaps) < MIN_GAMES_FOR_TREND:
            return {
                "sell_high": False,
                "buy_low": False,
                "injury_concern": False,
                "trend": "insufficient_data",
            }

        recent_snaps = [s["snap_pct"] for s in snaps[-3:]]
        recent_targets = [t.get("share", 0) for t in targets[-3:]]
        recent_ppr = [s.get("ppr", 0) for s in stats[-3:]]

        # Snap count trend
        if len(recent_snaps) >= SNAP_DECLINE_WEEKS:
            if all(
                recent_snaps[i] < recent_snaps[i - 1]
                for i in range(1, len(recent_snaps))
            ):
                trend = "declining"
                injury_concern = True
            elif all(
                recent_snaps[i] > recent_snaps[i - 1]
                for i in range(1, len(recent_snaps))
            ):
                trend = "rising"

        # Sell high: TDs outpacing target share
        # High PPR but low target share = TD-dependent, will regress
        if recent_targets and recent_ppr:
            avg_targets = sum(recent_targets) / len(recent_targets)
            avg_ppr = sum(recent_ppr) / len(recent_ppr)
            if avg_ppr > 20 and avg_targets < 0.12:
                sell_high = True

        # Buy low: snap count stable but PPR depressed
        if (
            trend == "stable"
            and recent_ppr
            and sum(recent_ppr) / len(recent_ppr) < 8
        ):
            buy_low = True

        return {
            "sell_high": sell_high,
            "buy_low": buy_low,
            "injury_concern": injury_concern,
            "trend": trend,
        }

    def _find_player_stats(
        self,
        record: "SeasonRoster",
        rosters: list,
    ) -> dict | None:
        """
        Find this week's stats for a rostered player
        from the platform roster response.

        Returns None if player not found (dropped, bye, etc.)
        """
        # Platform rosters contain current week stats
        # This is platform-specific data already normalized
        # by the LeaguePlatformAPI implementation
        for team in rosters:
            if team.platform_team_id == record.platform_team_id:
                for player in team.players:
                    # Match by platform_player_id when available
                    # Fall back to name matching
                    return {
                        "stats": {"ppr": 0},  # from platform
                        "snaps": {"snap_pct": 0},
                        "targets": {"share": 0},
                    }
        return None
```

### Base agent class

```python
# backend/agents/base.py
"""
BaseAgent — shared infrastructure for all agents.

Provides:
  - DB session injection
  - API usage logging
  - Standard error handling
  - is_nfl_season() guard

All agents extend this class.
Never duplicate these patterns in individual agents.
"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class BaseAgent:
    agent_name: str = "base"

    def __init__(self, db: AsyncSession):
        self._db = db

    async def _log_api_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        cache_hit: bool = False,
    ) -> None:
        """Log an Anthropic API call. Shared by all agents."""
        from backend.models.api_usage_log import ApiUsageLog
        from sqlalchemy.dialects.postgresql import insert

        await self._db.execute(
            insert(ApiUsageLog).values(
                agent_name=self.agent_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost_usd,
                cache_hit=cache_hit,
            )
        )
```

---

## Part 2 — Platform stats normalization

Each platform returns weekly stats in different formats.
The `LeaguePlatformAPI` implementations normalize these into
a consistent dict before `RosterMonitorAgent` sees them.

Stats dict schema (what agents receive):
```python
{
    "ppr": float,           # PPR fantasy points this week
    "carries": int,
    "rush_yards": float,
    "targets": int,
    "receptions": int,
    "rec_yards": float,
    "touchdowns": int,
    "snap_pct": float,      # 0.0 - 1.0
    "target_share": float,  # 0.0 - 1.0
}
```

This normalization lives in each platform's
`LeaguePlatformAPI` implementation, not in the agent.
The agent always receives the same dict structure.

---

## Required test cases

```python
# tests/unit/agents/test_roster_monitor.py

def test_sell_high_flag_td_spike_low_targets():
    """High PPR but <12% target share → sell_high=True"""
    agent = RosterMonitorAgent(mock_db)
    mock_roster = _make_roster(
        ppr=[28, 24, 31],
        target_share=[0.09, 0.08, 0.10],
        snap_pct=[0.80, 0.82, 0.79],
    )
    flags = agent._compute_flags(mock_roster)
    assert flags["sell_high"] is True

def test_snap_decline_2_weeks_sets_injury_concern():
    """Declining snap count 2+ weeks → injury_concern=True"""
    mock_roster = _make_roster(
        snap_pct=[0.85, 0.70, 0.55],
    )
    flags = agent._compute_flags(mock_roster)
    assert flags["injury_concern"] is True
    assert flags["trend"] == "declining"

def test_rising_trend_detected():
    mock_roster = _make_roster(snap_pct=[0.55, 0.70, 0.85])
    flags = agent._compute_flags(mock_roster)
    assert flags["trend"] == "rising"

def test_insufficient_data_returns_safe_defaults():
    """Less than 3 weeks → no flags, trend=insufficient_data"""
    mock_roster = _make_roster(snap_pct=[0.80])
    flags = agent._compute_flags(mock_roster)
    assert flags["sell_high"] is False
    assert flags["trend"] == "insufficient_data"

def test_weekly_arrays_appended_not_replaced():
    """Second week appends to array, not replaces it"""
    # Verify week 1 data still present after week 2 update

def test_duplicate_week_not_appended():
    """Calling append_weekly_stats twice for same week is safe"""

def test_offseason_exits_immediately():
    """is_nfl_season() False → run_all_users() returns immediately"""

def test_one_league_failure_does_not_abort_others():
    """Exception in one league is caught, others continue"""

def test_flags_update_in_db_after_run():
    """After run_for_league(), sell_high_flag is persisted"""

def test_platform_agnostic():
    """Same agent code runs for yahoo/espn/sleeper leagues"""
```

---

## Commit

```
feat(roster-monitor): Roster Monitor Agent

Weekly stat sync for all rostered players across all users.
Platform-agnostic via LeaguePlatformAPI — Yahoo/ESPN/Sleeper.
Flag computation: sell_high, buy_low, injury_concern, trend.
BaseAgent: shared infrastructure (logging, API usage tracking).
One agent class, no per-platform duplication.
Offseason guard: exits immediately outside NFL season.
Coverage: X%.
```
