# Stage 16: Opponent Analyzer Agent

## Before starting, read:
- `docs/stages/stage-14-season-roster.md` (platform abstraction, must be complete)
- `docs/stages/stage-15-roster-monitor.md` (must be complete)
- `docs/INSEASON.md` — Opponent Analyzer section

---

## Goal

Build and maintain per-opponent profiles for every manager in a
user's league. Updated every Wednesday after Roster Monitor runs.
Management style detection enables realistic trade proposal targeting
in Stage 19 and acceptance probability modeling in Stage 18.

Platform-agnostic — works for Yahoo, ESPN, and Sleeper leagues.

---

## Enterprise standards

- `LeaguePlatformAPI` for all platform data — no direct API calls
- `OpponentProfileRepository` for all DB access
- `OpponentAnalyzerService` for all business logic
- Sonnet reasoning isolated to management style detection only
- Python handles all data aggregation before AI call

---

## Model

`claude-sonnet-4-6` — behavioral reasoning required for management
style detection. Everything else is Python.

---

## Part 1 — OpponentProfile model

```python
# backend/models/opponent_profile.py
"""
OpponentProfile — per-manager behavioral profile.
One record per manager per user per league per season.
Updated weekly by OpponentAnalyzerAgent.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class OpponentProfile(Base):
    __tablename__ = "opponent_profiles"

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
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)

    # Manager identity
    platform_team_id: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    manager_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    team_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )

    # Roster strength
    threat_score: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # 0-100 composite of roster quality

    # Standing
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    points_for: Mapped[Optional[float]] = mapped_column(
        Numeric(7, 2), nullable=True
    )
    playoff_position: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    # Management style — detected by Sonnet
    management_style: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True
    )
    # reactive | analytical | name_brand | urgency_driven

    management_style_confidence: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True
    )
    # high | medium | low

    style_evidence: Mapped[Optional[list]] = mapped_column(
        JSONB, default=list
    )
    # Evidence points that informed style detection

    # Roster vulnerabilities
    vulnerabilities: Mapped[Optional[list]] = mapped_column(
        JSONB, default=list
    )
    # [{"type": "bye_conflict", "detail": "3 starters on week 7 bye"}]

    # Trade history with this manager
    trade_history: Mapped[Optional[list]] = mapped_column(
        JSONB, default=list
    )
    # [{"week": 3, "gave": [...], "received": [...], "accepted": True}]

    # Roster composition (positional scores 0-100)
    positional_scores: Mapped[Optional[dict]] = mapped_column(
        JSONB, default=dict
    )
    # {"QB": 85, "RB": 60, "WR": 72, "TE": 45}

    last_analyzed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
```

---

## Part 2 — OpponentProfile repository

```python
# backend/repositories/opponent_profile_repo.py
"""
OpponentProfileRepository — all opponent profile DB queries.
All queries scoped to user_id — row-level security enforced.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models.opponent_profile import OpponentProfile
from backend.repositories.base import BaseRepository


class OpponentProfileRepository(BaseRepository[OpponentProfile]):
    model = OpponentProfile

    async def get_for_league(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
    ) -> list[OpponentProfile]:
        """All opponent profiles for a user's league."""
        result = await self._session.execute(
            select(OpponentProfile)
            .where(
                OpponentProfile.user_id == user_id,
                OpponentProfile.user_league_id == user_league_id,
            )
            .order_by(OpponentProfile.threat_score.desc())
        )
        return list(result.scalars().all())

    async def get_by_team_id(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
        platform_team_id: str,
    ) -> OpponentProfile | None:
        result = await self._session.execute(
            select(OpponentProfile)
            .where(
                OpponentProfile.user_id == user_id,
                OpponentProfile.user_league_id == user_league_id,
                OpponentProfile.platform_team_id == platform_team_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
        platform_team_id: str,
        season_year: int,
        **kwargs,
    ) -> OpponentProfile:
        """Create or update an opponent profile."""
        await self._session.execute(
            pg_insert(OpponentProfile)
            .values(
                user_id=user_id,
                user_league_id=user_league_id,
                platform_team_id=platform_team_id,
                season_year=season_year,
                last_analyzed_at=datetime.now(timezone.utc),
                **kwargs,
            )
            .on_conflict_do_update(
                index_elements=[
                    "user_id", "user_league_id",
                    "platform_team_id", "season_year",
                ],
                set_={
                    "last_analyzed_at": datetime.now(timezone.utc),
                    **kwargs,
                },
            )
        )
        return await self.get_by_team_id(
            user_id, user_league_id, platform_team_id
        )
```

---

## Part 3 — Agent implementation

```python
# backend/agents/opponent_analyzer.py
"""
OpponentAnalyzer Agent — weekly opponent profiling.

Runs every Wednesday after Roster Monitor.
Uses Sonnet to detect management style from behavioral evidence.
Everything else is Python.
"""
import json
import logging
from collections import Counter

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import BaseAgent
from backend.integrations.platform_factory import get_platform_api
from backend.integrations.platform_models import TeamRoster
from backend.models.user import User
from backend.models.user_league import UserLeague
from backend.repositories.opponent_profile_repo import (
    OpponentProfileRepository,
)
from backend.repositories.season_roster_repo import (
    SeasonRosterRepository,
)
from backend.utils.nfl_schedule import (
    get_current_week, is_nfl_season,
)

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 600


STYLE_DETECTION_PROMPT = """You are analyzing a fantasy football manager's
behavioral patterns to classify their management style.

Manager: {manager_name}
Record: {wins}-{losses}
Roster composition: {roster_summary}
Recent transactions: {transactions}
Trade history: {trade_history}
Positional scores: {positional_scores}

Classify management style as ONE of:
- reactive: frequently starts players off big recent games; holds
  players based on recency rather than projections
- analytical: trade offers show schedule/usage awareness; drops
  players before others recognize decline
- name_brand: holds big-name players past their value; resistant
  to trading away recognizable names
- urgency_driven: losing streak drives overreaction; willing to
  overpay in trades when desperate

Output ONLY valid JSON, no preamble:
{
  "management_style": "reactive|analytical|name_brand|urgency_driven",
  "confidence": "high|medium|low",
  "evidence": ["key observation 1", "key observation 2"]
}"""


class OpponentAnalyzerAgent(BaseAgent):
    agent_name = "opponent_analyzer"

    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self._profile_repo = OpponentProfileRepository(db)
        self._roster_repo = SeasonRosterRepository(db)
        self._client = anthropic.AsyncAnthropic()

    async def run_all_users(self) -> None:
        if not is_nfl_season():
            return

        week = get_current_week()
        if not week:
            return

        result = await self._db.execute(
            select(UserLeague, User)
            .join(User, UserLeague.user_id == User.id)
            .where(
                UserLeague.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        for league, user in result.all():
            try:
                await self.run_for_league(user, league, week)
            except Exception as exc:
                logger.error(
                    "OpponentAnalyzer failed for league %s: %s",
                    league.id, exc,
                )

    async def run_for_league(
        self,
        user: User,
        league: UserLeague,
        week: int,
    ) -> None:
        from backend.utils.seasons import get_current_season

        platform = await get_platform_api(league, self._db)
        rosters = await platform.get_standings()
        transactions = await platform.get_transactions(week)
        season_year = get_current_season()

        for team in rosters:
            # Skip the user's own team
            # (identified by matching their platform team ID)
            if team.platform_team_id == league.platform_team_id:
                continue

            try:
                await self._analyze_opponent(
                    user=user,
                    league=league,
                    team=team,
                    transactions=[
                        t for t in transactions
                        if t.added_by_team_id == team.platform_team_id
                    ],
                    season_year=season_year,
                )
            except Exception as exc:
                logger.error(
                    "Failed to analyze opponent %s: %s",
                    team.platform_team_id, exc,
                )

        await self._db.commit()

    async def _analyze_opponent(
        self,
        user: User,
        league: UserLeague,
        team: TeamRoster,
        transactions: list,
        season_year: int,
    ) -> None:
        """Build or update one opponent's profile."""
        positional_scores = self._compute_positional_scores(team)
        threat_score = self._compute_threat_score(team, positional_scores)
        vulnerabilities = self._detect_vulnerabilities(team)

        # Only call Sonnet if we have enough behavioral data
        # (at least 3 weeks in — enough transaction history)
        style_data = {}
        current_week = get_current_week() or 0
        if current_week >= 3:
            style_data = await self._detect_management_style(
                team, transactions, positional_scores
            )

        await self._profile_repo.upsert(
            user_id=user.id,
            user_league_id=league.id,
            platform_team_id=team.platform_team_id,
            season_year=season_year,
            manager_name=team.manager_name,
            team_name=team.team_name,
            threat_score=threat_score,
            wins=team.wins,
            losses=team.losses,
            points_for=float(team.points_for),
            positional_scores=positional_scores,
            vulnerabilities=vulnerabilities,
            management_style=style_data.get("management_style"),
            management_style_confidence=style_data.get("confidence"),
            style_evidence=style_data.get("evidence", []),
        )

    def _compute_positional_scores(
        self, team: TeamRoster
    ) -> dict[str, int]:
        """
        Score each position 0-100 based on projected PPR.
        Uses player baseline_value from the central players table.
        Pure Python — no AI.
        """
        scores: dict[str, list[float]] = {}
        for player in team.players:
            pos = player.position
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            # Would join with players table for baseline_value
            # Simplified here — full impl queries DB
            scores.setdefault(pos, []).append(0.0)

        return {
            pos: min(100, int(sum(vals) / max(1, len(vals)) * 0.5))
            for pos, vals in scores.items()
        }

    def _compute_threat_score(
        self,
        team: TeamRoster,
        positional_scores: dict,
    ) -> int:
        """
        Composite threat score 0-100.
        Weighted average of positional scores.
        Pure Python.
        """
        weights = {"QB": 0.20, "RB": 0.35, "WR": 0.35, "TE": 0.10}
        total = sum(
            positional_scores.get(pos, 0) * weight
            for pos, weight in weights.items()
        )
        # Bonus for winning record
        win_rate = team.wins / max(1, team.wins + team.losses)
        record_bonus = int(win_rate * 10)
        return min(100, int(total + record_bonus))

    def _detect_vulnerabilities(
        self, team: TeamRoster
    ) -> list[dict]:
        """
        Identify roster vulnerabilities. Pure Python.
        - Bye week conflicts
        - Multiple high-injury-risk players
        - Positional weakness
        """
        vulnerabilities = []
        position_counts = Counter(
            p.position for p in team.players if p.is_starter
        )

        if position_counts.get("RB", 0) < 2:
            vulnerabilities.append({
                "type": "positional_weakness",
                "detail": "Thin at RB — only 1 reliable starter",
            })
        if position_counts.get("WR", 0) < 2:
            vulnerabilities.append({
                "type": "positional_weakness",
                "detail": "Thin at WR — only 1 reliable starter",
            })

        return vulnerabilities

    async def _detect_management_style(
        self,
        team: TeamRoster,
        transactions: list,
        positional_scores: dict,
    ) -> dict:
        """
        Sonnet call — behavioral pattern classification.
        Only called when sufficient data exists (week >= 3).
        """
        roster_summary = {
            pos: positional_scores.get(pos, 0)
            for pos in ("QB", "RB", "WR", "TE")
        }

        prompt = STYLE_DETECTION_PROMPT.format(
            manager_name=team.manager_name,
            wins=team.wins,
            losses=team.losses,
            roster_summary=json.dumps(roster_summary),
            transactions=json.dumps([
                {
                    "type": t.type,
                    "player": t.player_name,
                    "week": t.week,
                }
                for t in transactions[:10]  # Last 10 transactions
            ]),
            trade_history="[]",  # Populated from DB in full impl
            positional_scores=json.dumps(positional_scores),
        )

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

        await self._log_api_usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=self._estimate_cost(response.usage, "sonnet"),
        )

        text = response.content[0].text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error(
                "OpponentAnalyzer: invalid JSON from Sonnet: %s", text
            )
            return {}

    def _estimate_cost(self, usage, model: str) -> float:
        if model == "sonnet":
            return (
                usage.input_tokens / 1_000_000 * 3.0
                + usage.output_tokens / 1_000_000 * 15.0
            )
        return 0.0
```

---

## Part 4 — API endpoints

```python
# Add to backend/routers/season_roster.py or new file

@router.get("/{league_id}/opponents")
async def get_opponent_profiles(
    league_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    All opponent profiles for a league.
    Sorted by threat score descending.
    """
    league = await _get_user_league(league_id, user, db)
    repo = OpponentProfileRepository(db)
    profiles = await repo.get_for_league(user.id, league.id)
    return {
        "league_id": str(league_id),
        "opponents": [
            {
                "team_id": p.platform_team_id,
                "manager_name": p.manager_name,
                "record": f"{p.wins}-{p.losses}",
                "threat_score": p.threat_score,
                "management_style": p.management_style,
                "vulnerabilities": p.vulnerabilities,
            }
            for p in profiles
        ],
    }
```

---

## Required test cases

```python
# tests/unit/agents/test_opponent_analyzer.py

def test_threat_score_scales_with_positional_scores()
def test_threat_score_bonus_for_winning_record()
def test_vulnerability_detected_thin_at_rb()
def test_vulnerability_detected_thin_at_wr()
def test_management_style_not_called_before_week_3()
    """Insufficient data guard: no Sonnet call in weeks 1-2"""
def test_management_style_reactive_detected()
def test_management_style_urgency_driven_losing_streak()
def test_own_team_excluded_from_opponent_analysis()
def test_offseason_exits_immediately()
def test_one_opponent_failure_does_not_abort_others()
def test_profile_upserted_not_duplicated()
def test_row_level_security_user_a_cannot_see_user_b_opponents()
def test_threat_score_0_to_100_clamped()
```

---

## Commit

```
feat(opponent-analyzer): Opponent Analyzer Agent

Per-opponent behavioral profiles for all league managers.
Management style detection via Sonnet (reactive/analytical/
name_brand/urgency_driven) — only called after week 3.
Threat scores, positional scores, vulnerability detection in Python.
Platform-agnostic via LeaguePlatformAPI.
OpponentProfileRepository: user-scoped, row-level security.
Offseason guard and per-opponent error isolation.
Coverage: X%.
```
