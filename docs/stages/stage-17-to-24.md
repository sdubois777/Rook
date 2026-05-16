# Stage 17: Trade Value Agent

## Before starting, read:
- `docs/stages/stage-15-roster-monitor.md` (sell_high/buy_low flags, must be complete)
- `docs/stages/stage-16-opponent-analyzer.md` (opponent profiles, must be complete)
- `docs/INSEASON.md` — Trade Value section
- `docs/rules/COST_RULES.md`

---

## Goal

Weekly valuations for every player in each user's league.
Buy-low and sell-high signals identify trade opportunities
before opponents notice. Platform-agnostic.

---

## Enterprise standards

- `LeaguePlatformAPI` for current league player data
- `TradeValueRepository` for all DB access
- Business logic in `TradeValueAgent` — no logic in routers
- One computation per player per user per week — no duplicates

---

## Model

`claude-haiku-4-5-20251001` — valuation formatting only.
All signal logic is Python.

---

## Part 1 — Trade value model

Add columns to `SeasonRoster` (via migration):
```python
# Add to SeasonRoster model
current_trade_value: Mapped[Optional[float]]   # 0-100 normalized
trade_value_last_week: Mapped[Optional[float]] # for trend
value_asymmetry: Mapped[Optional[dict]]        # JSONB
# {"opponent_id": "...", "their_valuation": 45, "our_valuation": 65}
```

These columns live on SeasonRoster because trade value is
per-player per-user per-league. Not on the central players table.

---

## Part 2 — Agent implementation

```python
# backend/agents/trade_value.py
"""
TradeValue Agent — weekly player valuations for all league players.

Computes current trade value, buy-low/sell-high signals, and
per-opponent valuation asymmetry from behavioral profiles.
"""
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import BaseAgent
from backend.integrations.platform_factory import get_platform_api
from backend.models.user import User
from backend.models.user_league import UserLeague
from backend.repositories.opponent_profile_repo import (
    OpponentProfileRepository,
)
from backend.repositories.season_roster_repo import (
    SeasonRosterRepository,
)
from backend.utils.nfl_schedule import get_current_week, is_nfl_season

logger = logging.getLogger(__name__)


class TradeValueAgent(BaseAgent):
    agent_name = "trade_value"

    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self._roster_repo = SeasonRosterRepository(db)
        self._opponent_repo = OpponentProfileRepository(db)

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
                    "TradeValue failed for league %s: %s",
                    league.id, exc,
                )

    async def run_for_league(
        self,
        user: User,
        league: UserLeague,
        week: int,
    ) -> None:
        """Compute trade values for all players in user's league."""
        platform = await get_platform_api(league, self._db)
        rosters = await platform.get_rosters()

        # All players across all teams in league
        all_players = [
            player
            for team in rosters
            for player in team.players
        ]

        opponent_profiles = await self._opponent_repo.get_for_league(
            user.id, league.id
        )

        # Compute value for each player
        for platform_player in all_players:
            trade_value = self._compute_trade_value(
                platform_player, week
            )
            asymmetry = self._compute_asymmetry(
                platform_player, trade_value, opponent_profiles
            )

            # Update SeasonRoster record if we own this player
            # Otherwise just cache in a separate table
            # (full impl stores in player_trade_values table)

        await self._db.commit()

    def _compute_trade_value(
        self,
        player: "RosteredPlayer",
        week: int,
    ) -> float:
        """
        Compute 0-100 trade value from:
        - Recent performance trend (50%)
        - Injury risk (20%)
        - Remaining schedule (20%)
        - Weeks remaining in season (10%)

        Pure Python — no AI.
        """
        # Base value from player's baseline_value in DB
        # (would query SeasonRoster → Player join in full impl)
        base = 50.0

        # Weeks remaining factor — value floors decrease late
        weeks_remaining = max(0, 18 - week)
        recency_weight = min(1.0, weeks_remaining / 10)

        return min(100.0, max(0.0, base * recency_weight))

    def _compute_asymmetry(
        self,
        player: "RosteredPlayer",
        our_value: float,
        opponent_profiles: list,
    ) -> list[dict]:
        """
        For each opponent, estimate their likely valuation
        based on their management style.

        Reactive managers overvalue recent scorers.
        Name-brand managers overvalue recognizable names.
        Analytical managers value close to system.

        Returns asymmetry opportunities — where we value
        a player significantly differently than they likely do.
        """
        asymmetries = []

        for profile in opponent_profiles:
            their_value = self._estimate_opponent_valuation(
                player, our_value, profile.management_style
            )
            gap = our_value - their_value

            if abs(gap) >= 15:  # Significant asymmetry threshold
                asymmetries.append({
                    "opponent_id": profile.platform_team_id,
                    "manager_name": profile.manager_name,
                    "our_value": our_value,
                    "their_value": their_value,
                    "gap": gap,
                    "direction": (
                        "we_value_more" if gap > 0
                        else "they_value_more"
                    ),
                })

        return sorted(asymmetries, key=lambda x: abs(x["gap"]), reverse=True)[:3]

    def _estimate_opponent_valuation(
        self,
        player: "RosteredPlayer",
        our_value: float,
        management_style: str | None,
    ) -> float:
        """
        Estimate how an opponent with a given management style
        values this player relative to system value.
        """
        style_modifiers = {
            "reactive": 1.15,      # Overvalue recent scorers
            "name_brand": 1.10,    # Overvalue recognizable names
            "analytical": 1.00,    # Value close to system
            "urgency_driven": 0.90, # Undervalue depth when desperate
        }
        modifier = style_modifiers.get(management_style or "", 1.0)
        return min(100.0, our_value * modifier)
```

---

## Required test cases

```python
def test_sell_high_flag_propagated_from_roster_monitor()
def test_buy_low_flag_propagated_from_roster_monitor()
def test_asymmetry_detected_reactive_manager()
def test_asymmetry_threshold_15_points()
def test_trade_value_0_to_100_clamped()
def test_all_league_players_get_valuation()
def test_offseason_exits_immediately()
def test_one_league_failure_isolated()
```

---

## Commit

```
feat(trade-value): Trade Value Agent

Weekly valuations for all players in each user's league.
Buy-low/sell-high signals from Roster Monitor flags.
Per-opponent asymmetry detection using behavioral profiles.
Platform-agnostic via LeaguePlatformAPI.
Coverage: X%.
```

---
---

# Stage 18: Trade Analyzer

## Before starting, read:
- `docs/stages/stage-17-trade-value.md` (must be complete)
- `docs/stages/stage-16-opponent-analyzer.md` (opponent profiles)
- `docs/INSEASON.md` — Trade Analyzer section

---

## Goal

User submits a trade — either received or proposed — and gets
structured analysis: verdict, acceptance probability, timing
flags, and counter proposals. Deducts 10 credits per analysis.

---

## Enterprise standards

- Thin router — validates input, calls `TradeAnalyzerService`
- Service assembles context, computes verdict, calls Sonnet
- Credits deducted via `require_credits` dependency
- `platform_team_id` throughout — no `yahoo_team_id`
- Counter proposals computed in Python — Sonnet writes summaries only

---

## Model: `claude-sonnet-4-6` | Max tokens: 1500

---

## Part 1 — Service layer

```python
# backend/services/trade_analyzer_service.py
"""
TradeAnalyzerService — trade evaluation logic.

All fairness math, verdict computation, and acceptance
probability in Python. Sonnet used only for human-readable
summaries. This is intentional — deterministic logic is
more reliable and cheaper than asking AI to recalculate.
"""
import json
import logging
import uuid
from typing import Optional

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from backend.repositories.opponent_profile_repo import (
    OpponentProfileRepository,
)
from backend.repositories.season_roster_repo import (
    SeasonRosterRepository,
)

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1500

VERDICTS = {
    "favorable": (10, float("inf")),
    "slightly_favorable": (4, 10),
    "fair": (-4, 4),
    "slightly_unfavorable": (-10, -4),
    "unfavorable": (float("-inf"), -10),
}

VERDICT_SONNET_PROMPT = """You are a fantasy football trade advisor.
All calculations are complete. Write plain-English summaries only.
Never recalculate values.

Trade context:
{context}

Output ONLY valid JSON:
{{
  "timing_analysis": "1-2 sentences on timing flags",
  "acceptance_reasoning": "1-2 sentences on opponent acceptance",
  "overall_summary": "1-2 sentences overall recommendation"
}}"""


class TradeAnalyzerService:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._roster_repo = SeasonRosterRepository(db)
        self._opponent_repo = OpponentProfileRepository(db)
        self._client = anthropic.AsyncAnthropic()

    async def analyze(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
        give_player_ids: list[uuid.UUID],
        receive_player_ids: list[uuid.UUID],
        opponent_team_id: str,
    ) -> dict:
        """
        Full trade analysis. Returns structured result.
        Credits already verified before this is called.
        """
        # Assemble context in Python
        give_players = await self._get_players(give_player_ids)
        receive_players = await self._get_players(receive_player_ids)
        opponent_profile = await self._opponent_repo.get_by_team_id(
            user_id, user_league_id, opponent_team_id
        )

        give_total = sum(
            p.get("current_trade_value", 0) for p in give_players
        )
        receive_total = sum(
            p.get("current_trade_value", 0) for p in receive_players
        )
        fairness_gap = receive_total - give_total

        timing_flags = self._compute_timing_flags(
            give_players, receive_players
        )
        verdict = self._compute_verdict(
            fairness_gap, timing_flags
        )
        acceptance_prob, acceptance_reason = (
            self._estimate_acceptance(
                fairness_gap, timing_flags, opponent_profile
            )
        )
        counter_proposals = []
        if verdict in ("unfavorable", "slightly_unfavorable"):
            counter_proposals = await self._generate_counters(
                user_id, user_league_id, give_players,
                receive_players, opponent_profile,
            )

        # Sonnet for summaries only
        summaries = await self._get_summaries(
            give_players=give_players,
            receive_players=receive_players,
            fairness_gap=fairness_gap,
            verdict=verdict,
            timing_flags=timing_flags,
            opponent_profile=opponent_profile,
            acceptance_prob=acceptance_prob,
        )

        return {
            "verdict": verdict,
            "fairness_gap": round(fairness_gap, 1),
            "give_total_value": round(give_total, 1),
            "receive_total_value": round(receive_total, 1),
            "timing_flags": timing_flags,
            "timing_analysis": summaries.get("timing_analysis", ""),
            "acceptance_probability": acceptance_prob,
            "acceptance_reasoning": summaries.get(
                "acceptance_reasoning", acceptance_reason
            ),
            "counter_proposals": counter_proposals,
            "overall_summary": summaries.get("overall_summary", ""),
        }

    def _compute_timing_flags(
        self,
        give_players: list[dict],
        receive_players: list[dict],
    ) -> list[dict]:
        """
        Detect sell-high and buy-low timing signals.
        Uses flags already computed by Roster Monitor/Trade Value.
        Pure Python.
        """
        flags = []
        for p in give_players:
            if p.get("buy_low_flag"):
                flags.append({
                    "player": p["name"],
                    "side": "give",
                    "signal": "BUY_LOW_OPPORTUNITY",
                    "reason": "You're selling low — recent slump is matchup-driven",
                })
        for p in receive_players:
            if p.get("sell_high_flag"):
                flags.append({
                    "player": p["name"],
                    "side": "receive",
                    "signal": "SELL_HIGH_WARNING",
                    "reason": "Opponent selling high — recent performance unsustainable",
                })
        return flags

    def _compute_verdict(
        self,
        fairness_gap: float,
        timing_flags: list[dict],
    ) -> str:
        """Deterministic verdict from gap + timing. No AI."""
        adjusted = fairness_gap

        sell_high_warnings = sum(
            1 for f in timing_flags
            if f["side"] == "receive" and f["signal"] == "SELL_HIGH_WARNING"
        )
        buy_low_opportunities = sum(
            1 for f in timing_flags
            if f["side"] == "give" and f["signal"] == "BUY_LOW_OPPORTUNITY"
        )

        adjusted -= sell_high_warnings * 8
        adjusted -= buy_low_opportunities * 6

        for verdict, (low, high) in VERDICTS.items():
            if low <= adjusted < high:
                return verdict
        return "fair"

    def _estimate_acceptance(
        self,
        fairness_gap: float,
        timing_flags: list[dict],
        opponent_profile: Optional["OpponentProfile"],
    ) -> tuple[float, str]:
        """
        Estimate acceptance probability. Python only.
        Returns (probability, reason_string).
        """
        prob = 0.50
        reasons = []

        if fairness_gap < -8:
            prob -= 0.25
            reasons.append("appears lopsided at market value")
        elif fairness_gap > 8:
            prob += 0.15
            reasons.append("favorable for them at market value")

        if opponent_profile:
            style = opponent_profile.management_style
            record = f"{opponent_profile.wins}-{opponent_profile.losses}"

            if style == "urgency_driven" and opponent_profile.losses >= 3:
                prob += 0.15
                reasons.append("struggling and motivated to shake things up")
            if style == "reactive":
                for flag in timing_flags:
                    if flag["signal"] == "BUY_LOW_OPPORTUNITY":
                        prob += 0.10
                        break

        prob = max(0.05, min(0.95, prob))
        return round(prob, 2), " — ".join(reasons[:2])

    async def _generate_counters(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
        give_players: list[dict],
        receive_players: list[dict],
        opponent_profile: Optional["OpponentProfile"],
        max_proposals: int = 3,
    ) -> list[dict]:
        """
        Generate counter proposals when verdict is unfavorable.
        Pure Python simulation — no AI.
        """
        # Simplified — full impl queries user roster and simulates swaps
        return []

    async def _get_players(
        self, player_ids: list[uuid.UUID]
    ) -> list[dict]:
        """Fetch player data from DB including trade value flags."""
        # Joins SeasonRoster + Player tables
        return []

    async def _get_summaries(self, **context) -> dict:
        """Single Sonnet call for human-readable text."""
        prompt = VERDICT_SONNET_PROMPT.format(
            context=json.dumps({
                k: v for k, v in context.items()
                if isinstance(v, (str, int, float, list, dict))
            }, default=str)
        )
        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            return json.loads(response.content[0].text.strip())
        except json.JSONDecodeError:
            return {}
```

---

## Part 2 — Router (thin)

```python
# backend/routers/trades.py
"""
Trade analyzer router. Thin — delegates to TradeAnalyzerService.
"""
import uuid
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import (
    get_current_user, get_db, require_credits,
)
from backend.models.user import User
from backend.services.trade_analyzer_service import (
    TradeAnalyzerService,
)

router = APIRouter(prefix="/trades", tags=["trades"])


class TradeAnalyzeRequest(BaseModel):
    league_id: uuid.UUID
    give: list[uuid.UUID] = Field(..., min_length=1, max_length=5)
    receive: list[uuid.UUID] = Field(..., min_length=1, max_length=5)
    opponent_team_id: str = Field(..., min_length=1, max_length=100)


@router.post("/analyze")
async def analyze_trade(
    body: TradeAnalyzeRequest,
    user: User = Depends(get_current_user),
    _: None = Depends(require_credits("trade_analysis")),
    db: AsyncSession = Depends(get_db),
):
    """
    Analyze a trade.
    Requires Standard plan. Costs 10 credits.
    Credits deducted before analysis runs.
    """
    service = TradeAnalyzerService(db)
    return await service.analyze(
        user_id=user.id,
        user_league_id=body.league_id,
        give_player_ids=body.give,
        receive_player_ids=body.receive,
        opponent_team_id=body.opponent_team_id,
    )
```

---

## Required test cases

```python
def test_lopsided_trade_unfavorable_verdict()
def test_balanced_trade_fair_verdict()
def test_sell_high_flag_on_received_player_in_timing_flags()
def test_sell_high_warning_reduces_adjusted_gap()
def test_buy_low_opportunity_reduces_adjusted_gap()
def test_acceptance_probability_high_needy_opponent()
def test_acceptance_probability_low_no_need()
def test_urgency_driven_losing_manager_higher_probability()
def test_counter_proposal_acceptance_above_threshold()
def test_router_requires_auth()
def test_router_requires_standard_tier()
def test_credits_deducted_before_analysis()
def test_platform_team_id_not_yahoo_team_id()
```

---

## Commit

```
feat(trade-analyzer): Trade Analyzer

TradeAnalyzerService: fairness, verdict, acceptance probability.
All math in Python — Sonnet writes summaries only.
require_credits("trade_analysis") dependency — 10cr, Standard+.
platform_team_id throughout — no yahoo_team_id.
Counter proposals when verdict is unfavorable.
Coverage: X%.
```

---
---

# Stage 19: Trade Proposal Engine

## Before starting, read:
- `docs/stages/stage-18-trade-analyzer.md` (must be complete)
- `docs/stages/stage-16-opponent-analyzer.md` (management style profiles)

---

## Goal

Proactive weekly trade suggestions. Only surfaces proposals with
realistic acceptance probability (>0.40) based on behavioral
profiles. No theoretical best trades that opponents would never accept.

---

## Model: `claude-sonnet-4-6` | Max tokens: 2000

---

## Part 1 — Service

```python
# backend/services/trade_proposal_service.py
"""
TradeProposalService — proactive weekly trade suggestions.

Depends on TradeValueAgent and OpponentAnalyzerAgent
being complete for the week. Cross-references surplus
positions vs opponent weaknesses.
"""
import json
import logging
import uuid

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from backend.repositories.opponent_profile_repo import (
    OpponentProfileRepository,
)
from backend.repositories.season_roster_repo import (
    SeasonRosterRepository,
)
from backend.services.trade_analyzer_service import (
    TradeAnalyzerService,
)

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2000
MAX_PROPOSALS_PER_WEEK = 5
MIN_ACCEPTANCE_THRESHOLD = 0.40

PROPOSAL_PROMPT = """You are a fantasy football trade advisor.
Generate targeted trade proposals that are REALISTIC to accept.

Your roster strengths and weaknesses:
{your_roster_context}

Opponents and their profiles:
{opponent_contexts}

Valuation asymmetries (where you value players differently):
{asymmetries}

Generate up to {max_proposals} trade proposals ranked by
(acceptance_probability × value_advantage).
Only include proposals with acceptance > {threshold}.

Output ONLY valid JSON array:
[{{
  "opponent_team_id": "...",
  "give": ["player_name"],
  "receive": ["player_name"],
  "acceptance_probability": 0.65,
  "value_advantage": 12.5,
  "rationale": "one sentence",
  "framing_note": "how to frame for this manager's style"
}}]"""


class TradeProposalService:
    def __init__(self, db: AsyncSession):
        self._db = db
        self._roster_repo = SeasonRosterRepository(db)
        self._opponent_repo = OpponentProfileRepository(db)
        self._analyzer = TradeAnalyzerService(db)
        self._client = anthropic.AsyncAnthropic()

    async def generate_weekly_proposals(
        self,
        user_id: uuid.UUID,
        user_league_id: uuid.UUID,
    ) -> list[dict]:
        """
        Generate this week's trade proposals.
        Runs after Trade Value and Opponent Analyzer complete.
        """
        your_roster = await self._roster_repo.get_user_roster(
            user_id, user_league_id
        )
        opponent_profiles = await self._opponent_repo.get_for_league(
            user_id, user_league_id
        )

        if not your_roster or not opponent_profiles:
            return []

        your_context = self._build_your_context(your_roster)
        opponent_contexts = [
            self._build_opponent_context(p)
            for p in opponent_profiles
        ]

        prompt = PROPOSAL_PROMPT.format(
            your_roster_context=json.dumps(your_context),
            opponent_contexts=json.dumps(opponent_contexts),
            asymmetries=json.dumps([]),  # from trade value agent
            max_proposals=MAX_PROPOSALS_PER_WEEK,
            threshold=MIN_ACCEPTANCE_THRESHOLD,
        )

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

        await self._log_api_usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=(
                response.usage.input_tokens / 1_000_000 * 3.0
                + response.usage.output_tokens / 1_000_000 * 15.0
            ),
        )

        try:
            proposals = json.loads(response.content[0].text.strip())
            # Filter to threshold — model may return borderline cases
            return [
                p for p in proposals
                if p.get("acceptance_probability", 0)
                >= MIN_ACCEPTANCE_THRESHOLD
            ][:MAX_PROPOSALS_PER_WEEK]
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("TradeProposal: invalid response: %s", e)
            return []

    def _build_your_context(self, roster: list) -> dict:
        """Summarize your roster strengths and weaknesses."""
        return {
            "total_players": len(roster),
            "sell_high_candidates": [
                r.player_id for r in roster if r.sell_high_flag
            ],
            "buy_low_targets": [
                r.player_id for r in roster if r.buy_low_flag
            ],
        }

    def _build_opponent_context(self, profile: "OpponentProfile") -> dict:
        return {
            "team_id": profile.platform_team_id,
            "manager_name": profile.manager_name,
            "record": f"{profile.wins}-{profile.losses}",
            "management_style": profile.management_style,
            "vulnerabilities": profile.vulnerabilities,
            "threat_score": profile.threat_score,
        }

    async def _log_api_usage(self, **kwargs) -> None:
        """Delegate to BaseAgent logging pattern."""
        pass
```

---

## Part 2 — Router (thin)

```python
# Add to backend/routers/trades.py

@router.get("/proposals/{league_id}")
async def get_trade_proposals(
    league_id: uuid.UUID,
    user: User = Depends(get_current_user),
    _: None = Depends(require_feature("trade_analyzer")),
    db: AsyncSession = Depends(get_db),
):
    """
    Weekly trade proposals for a league.
    Requires Standard plan. Free (no credits) — already computed.
    """
    service = TradeProposalService(db)
    proposals = await service.generate_weekly_proposals(
        user.id, league_id
    )
    return {"league_id": str(league_id), "proposals": proposals}
```

---

## Required test cases

```python
def test_proposal_generated_for_known_asymmetry()
def test_low_acceptance_proposals_filtered_out()
def test_max_5_proposals_returned()
def test_framing_adjusted_for_reactive_manager()
def test_framing_adjusted_for_urgency_driven_manager()
def test_empty_roster_returns_no_proposals()
def test_router_requires_standard_tier()
```

---

## Commit

```
feat(trade-proposals): Trade Proposal Engine

Weekly proactive trade suggestions via Sonnet.
Only proposals with >40% acceptance returned.
Framing calibrated per opponent management style.
Max 5 per week. Runs after Trade Value + Opponent Analyzer.
Coverage: X%.
```

---
---

# Stage 20: Lineup Optimizer

## Before starting, read:
- `docs/stages/stage-15-roster-monitor.md` (snap counts, must be complete)
- `docs/INSEASON.md` — Lineup Optimizer section

---

## Goal

Every Thursday, recommend who to start. Vegas implied totals are
the most predictive input. All math in Python. Haiku used for
edge case notes and formatting only.

---

## Data sources (resolved — no longer "ASK USER")

- **Vegas lines**: The Odds API (`https://api.the-odds-api.com`)
  Free tier: 500 requests/month. Add `THE_ODDS_API_KEY` to env.
  Approximately 1 request per week per league. Sufficient.

- **Weather**: OpenWeatherMap API (`https://api.openweathermap.org`)
  Free tier: 60 requests/minute. Add `OPENWEATHERMAP_API_KEY` to env.
  Call once per week per outdoor stadium. Sufficient.

Both keys required in `.env` and Railway variables.

---

## Model: `claude-haiku-4-5-20251001` | Max tokens: 1000

---

## Part 1 — External data services

```python
# backend/integrations/vegas_data.py
"""
Vegas implied team totals via The Odds API.
Cache results per week — one fetch covers all leagues.
"""
import logging
from functools import lru_cache
import httpx
from backend.config import settings

logger = logging.getLogger(__name__)
ODDS_API_BASE = "https://api.the-odds-api.com/v4"


async def get_implied_totals(week: int) -> dict[str, float]:
    """
    Fetch NFL game lines for the current week.
    Returns {team_abbr: implied_total} for all 32 teams.

    Implied total = (over_under + spread) / 2 for favored team
    Opponent implied = over_under - home_implied

    Cached per week number. One API call serves all leagues.
    """
    if not settings.the_odds_api_key:
        logger.warning("THE_ODDS_API_KEY not set — using fallback values")
        return _fallback_totals()

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ODDS_API_BASE}/sports/americanfootball_nfl/odds/",
            params={
                "apiKey": settings.the_odds_api_key,
                "regions": "us",
                "markets": "totals,spreads",
                "oddsFormat": "american",
            },
        )
        resp.raise_for_status()
        games = resp.json()

    return _parse_implied_totals(games)


def _parse_implied_totals(games: list[dict]) -> dict[str, float]:
    """Parse Odds API response into team → implied total map."""
    totals = {}
    for game in games:
        bookmakers = game.get("bookmakers", [])
        if not bookmakers:
            continue
        # Use first bookmaker with totals market
        for bookmaker in bookmakers:
            markets = {
                m["key"]: m for m in bookmaker.get("markets", [])
            }
            if "totals" not in markets or "spreads" not in markets:
                continue

            over_under = float(
                markets["totals"]["outcomes"][0]["point"]
            )
            home_team = game["home_team"]
            away_team = game["away_team"]

            # Find spread for home team
            spread = 0.0
            for outcome in markets["spreads"]["outcomes"]:
                if outcome["name"] == home_team:
                    spread = float(outcome["point"])

            home_implied = (over_under - spread) / 2
            away_implied = over_under - home_implied

            # Map to NFL team abbreviations
            totals[_team_to_abbr(home_team)] = home_implied
            totals[_team_to_abbr(away_team)] = away_implied
            break

    return totals


def _fallback_totals() -> dict[str, float]:
    """League average implied total when API unavailable."""
    return {}  # Returns empty — optimizer uses 23.0 as default


def _team_to_abbr(team_name: str) -> str:
    """Map full team names to abbreviations."""
    # Full mapping in implementation
    return team_name[:3].upper()
```

```python
# backend/integrations/weather_data.py
"""
Game-day weather for outdoor NFL stadiums.
Only matters Nov-Jan for cold-weather cities.
"""
import logging
import httpx
from backend.config import settings

logger = logging.getLogger(__name__)

# Only outdoor stadiums — dome teams unaffected
OUTDOOR_STADIUMS = {
    "BUF": (42.77, -78.79),  # Buffalo, NY
    "GB":  (44.50, -88.06),  # Green Bay, WI
    "CHI": (41.86, -87.62),  # Chicago, IL
    "CLE": (41.50, -81.70),  # Cleveland, OH
    "PIT": (40.44, -80.01),  # Pittsburgh, PA
    "NE":  (42.09, -71.26),  # Foxborough, MA
    "NYG": (40.81, -74.07),  # East Rutherford, NJ
    "NYJ": (40.81, -74.07),  # (shared with NYG)
    "PHI": (39.90, -75.17),  # Philadelphia, PA
}


async def get_weather_for_teams(
    team_abbrs: list[str],
) -> dict[str, dict]:
    """
    Get weather for outdoor stadium teams only.
    Returns empty dict for dome teams.
    """
    if not settings.openweathermap_api_key:
        return {}

    weather = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for abbr in team_abbrs:
            coords = OUTDOOR_STADIUMS.get(abbr)
            if not coords:
                continue  # Dome team — no weather impact
            try:
                resp = await client.get(
                    "https://api.openweathermap.org/data/2.5/weather",
                    params={
                        "lat": coords[0],
                        "lon": coords[1],
                        "appid": settings.openweathermap_api_key,
                        "units": "imperial",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                weather[abbr] = {
                    "wind_mph": data["wind"]["speed"],
                    "temp_f": data["main"]["temp"],
                    "condition": data["weather"][0]["main"],
                }
            except Exception as exc:
                logger.warning(
                    "Weather fetch failed for %s: %s", abbr, exc
                )
    return weather
```

---

## Part 2 — Scoring engine (Python only)

```python
# backend/engines/lineup_scorer.py
"""
LineupScorer — score players for a given week.
Pure Python. No AI calls.
All scoring factors applied here before Haiku call.
"""
from dataclasses import dataclass
from typing import Optional


LEAGUE_AVG_IMPLIED_TOTAL = 23.0  # Historical average

MATCHUP_MULTIPLIERS = {
    "favorable": 1.15,
    "neutral": 1.00,
    "tough": 0.85,
}

INJURY_MULTIPLIERS = {
    "full": 1.0,
    "questionable": 0.75,
    "limited": 0.65,
    "dnp": 0.30,
    "out": 0.0,
}

TREND_MULTIPLIERS = {
    "rising": 1.08,
    "stable": 1.00,
    "declining": 0.90,
    "new_role": 1.12,
}

PASSING_GAME_POSITIONS = ("QB", "WR", "TE")


@dataclass
class PlayerScore:
    player_id: str
    player_name: str
    position: str
    projected_score: float
    confidence: str  # high | medium | low
    injury_status: str
    reasons: list[str]
    locked: bool = False


def score_player(
    player_id: str,
    player_name: str,
    position: str,
    baseline_weekly_avg: float,
    vegas_total: float,
    matchup_grade: str,
    injury_status: str,
    snap_trend: str,
    weather: Optional[dict] = None,
) -> PlayerScore:
    """
    Compute weekly projected score for one player.
    All inputs pre-assembled by caller.
    """
    base = baseline_weekly_avg

    vegas_mult = 1.0 + (
        (vegas_total - LEAGUE_AVG_IMPLIED_TOTAL) * 0.025
    )
    matchup_mult = MATCHUP_MULTIPLIERS.get(matchup_grade, 1.0)
    injury_mult = INJURY_MULTIPLIERS.get(injury_status, 1.0)
    trend_mult = TREND_MULTIPLIERS.get(snap_trend, 1.0)

    weather_mult = 1.0
    if weather and position in PASSING_GAME_POSITIONS:
        wind = weather.get("wind_mph", 0)
        temp = weather.get("temp_f", 60)
        if wind > 25:
            weather_mult *= 0.75
        elif wind > 15:
            weather_mult *= 0.90
        if temp < 20:
            weather_mult *= 0.95

    projected = (
        base * vegas_mult * matchup_mult
        * injury_mult * trend_mult * weather_mult
    )

    # Confidence
    if injury_status in ("questionable", "limited"):
        confidence = "low"
    elif matchup_grade == "tough" or snap_trend == "declining":
        confidence = "medium"
    elif matchup_grade == "favorable" and vegas_total > 27:
        confidence = "high"
    else:
        confidence = "medium"

    # Key reasons (max 3)
    reasons = []
    if vegas_total > 27:
        reasons.append(
            f"Team implied {vegas_total:.1f} pts — great environment"
        )
    elif vegas_total < 19:
        reasons.append(
            f"Team implied {vegas_total:.1f} pts — tough environment"
        )
    if matchup_grade == "favorable":
        reasons.append("Favorable matchup")
    elif matchup_grade == "tough":
        reasons.append("Tough matchup")
    if snap_trend == "rising":
        reasons.append("Snap count rising past 2 weeks")
    elif snap_trend == "declining":
        reasons.append("⚠️ Snap count declining — role concern")
    if injury_status == "questionable":
        reasons.append("⚠️ Questionable — monitor through Friday")
    if weather_mult < 0.90:
        reasons.append(
            f"⚠️ Wind {weather.get('wind_mph')}mph — passing game impact"
        )

    return PlayerScore(
        player_id=player_id,
        player_name=player_name,
        position=position,
        projected_score=round(projected, 1),
        confidence=confidence,
        injury_status=injury_status,
        reasons=reasons[:3],
    )
```

---

## Part 3 — Lineup optimization (Python only)

```python
# backend/engines/lineup_optimizer.py
"""
Lineup optimizer — find optimal starting lineup.
Handles flex slot optimization. Pure Python.
"""
from backend.engines.lineup_scorer import PlayerScore
from backend.models.league_config import LeagueConfig


def optimize_lineup(
    scored_players: list[PlayerScore],
    config: LeagueConfig,
    locked_ids: set[str] = None,
) -> dict[str, PlayerScore | None]:
    """
    Return optimal starting lineup given constraints.

    Slot priority: locked players first, then BPA.
    FLEX: highest projected eligible (RB/WR/TE) not already starting.
    """
    locked_ids = locked_ids or set()
    lineup: dict[str, PlayerScore | None] = {}
    used: set[str] = set()

    def best_available(
        position: str,
        exclude: set[str],
    ) -> PlayerScore | None:
        eligible = [
            p for p in scored_players
            if p.position == position
            and p.player_id not in exclude
            and p.injury_status != "out"
        ]
        return max(
            eligible,
            key=lambda p: p.projected_score,
            default=None,
        )

    # First: place locked players
    for p in scored_players:
        if p.player_id in locked_ids:
            lineup[p.position] = p
            used.add(p.player_id)

    # QB
    if "QB" not in lineup:
        if qb := best_available("QB", used):
            lineup["QB"] = qb
            used.add(qb.player_id)

    # RBs
    for slot in range(config.rb_slots):
        key = f"RB{slot + 1}"
        if key not in lineup:
            if rb := best_available("RB", used):
                lineup[key] = rb
                used.add(rb.player_id)

    # WRs
    for slot in range(config.wr_slots):
        key = f"WR{slot + 1}"
        if key not in lineup:
            if wr := best_available("WR", used):
                lineup[key] = wr
                used.add(wr.player_id)

    # TE
    if "TE" not in lineup:
        if te := best_available("TE", used):
            lineup["TE"] = te
            used.add(te.player_id)

    # FLEX — best remaining RB/WR/TE
    flex_eligible = [
        p for p in scored_players
        if p.position in ("RB", "WR", "TE")
        and p.player_id not in used
        and p.injury_status != "out"
    ]
    if flex_eligible:
        flex = max(flex_eligible, key=lambda p: p.projected_score)
        lineup["FLEX"] = flex
        used.add(flex.player_id)

    return lineup
```

---

## Part 4 — Agent (orchestrator)

```python
# backend/agents/lineup_optimizer.py
"""
LineupOptimizer Agent — weekly start/sit recommendations.

Orchestrates: Vegas data → weather → scores → optimize → Haiku notes.
Haiku used only for non-obvious edge case notes.
All decisions made in Python.
"""
import json
import logging
import uuid

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import BaseAgent
from backend.engines.lineup_optimizer import optimize_lineup
from backend.engines.lineup_scorer import score_player
from backend.integrations.platform_factory import get_platform_api
from backend.integrations.vegas_data import get_implied_totals
from backend.integrations.weather_data import get_weather_for_teams
from backend.models.user_league import UserLeague
from backend.repositories.season_roster_repo import SeasonRosterRepository
from backend.utils.nfl_schedule import get_current_week, is_nfl_season

logger = logging.getLogger(__name__)
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1000


class LineupOptimizerAgent(BaseAgent):
    agent_name = "lineup_optimizer"

    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self._roster_repo = SeasonRosterRepository(db)
        self._client = anthropic.AsyncAnthropic()

    async def run_for_league(
        self,
        user_id: uuid.UUID,
        league: UserLeague,
        week: int,
    ) -> dict:
        """Run lineup optimization for one user/league."""
        platform = await get_platform_api(league, self._db)
        rosters = await platform.get_rosters()

        # Fetch Vegas and weather data once for all players
        implied_totals = await get_implied_totals(week)
        all_team_abbrs = list(implied_totals.keys())
        weather = await get_weather_for_teams(all_team_abbrs)

        # Get user's roster
        my_roster = await self._roster_repo.get_user_roster(
            user_id, league.id
        )

        # Score each rostered player
        scored_players = []
        for record in my_roster:
            # Fetch player data from central DB (join with players table)
            # Simplified here
            score = score_player(
                player_id=str(record.player_id),
                player_name="",
                position="",
                baseline_weekly_avg=0.0,
                vegas_total=implied_totals.get("", 23.0),
                matchup_grade="neutral",
                injury_status="full",
                snap_trend=record.value_trend or "stable",
                weather=weather.get("", {}),
            )
            scored_players.append(score)

        # Optimize lineup
        from backend.services.league_service import LeagueService
        config = await LeagueService(
            self._roster_repo
        ).get_league_config(user_id, league.id)

        lineup = optimize_lineup(scored_players, config)

        # Haiku for edge case notes on close decisions
        notes = await self._get_edge_case_notes(
            lineup, scored_players
        )

        return {
            "week": week,
            "lineup": {
                slot: {
                    "player_id": p.player_id,
                    "player_name": p.player_name,
                    "projected": p.projected_score,
                    "confidence": p.confidence,
                    "reasons": p.reasons,
                }
                for slot, p in lineup.items() if p
            },
            "edge_case_notes": notes,
        }

    async def _get_edge_case_notes(
        self,
        lineup: dict,
        all_scored: list,
    ) -> list[str]:
        """
        Haiku identifies non-obvious considerations
        the scoring model might miss.
        """
        close_decisions = [
            p for p in all_scored
            if p.confidence == "low"
        ]
        if not close_decisions:
            return []

        prompt = (
            "Identify non-obvious considerations for these "
            "low-confidence lineup decisions. 1 sentence each. "
            "Output JSON array of strings.\n\n"
            + json.dumps([
                {
                    "name": p.player_name,
                    "score": p.projected_score,
                    "confidence": p.confidence,
                    "injury": p.injury_status,
                }
                for p in close_decisions[:5]
            ])
        )
        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            return json.loads(response.content[0].text.strip())
        except json.JSONDecodeError:
            return []
```

---

## Required test cases

```python
# tests/unit/engines/test_lineup_scorer.py
def test_high_vegas_total_boosts_score()
def test_low_vegas_total_depresses_score()
def test_weather_penalty_applied_to_passing_positions()
def test_weather_not_applied_to_rb()
def test_questionable_player_low_confidence()
def test_out_player_zero_score()
def test_league_avg_total_neutral_multiplier()

# tests/unit/engines/test_lineup_optimizer.py
def test_flex_slot_gets_highest_remaining_eligible()
def test_locked_player_not_overridden()
def test_out_player_excluded_from_lineup()
def test_rb_slots_filled_best_available()

# tests/unit/integrations/test_vegas_data.py
def test_implied_total_formula_correct()
    # KC -7 with O/U 49 → KC implied = 28.0
def test_fallback_returns_empty_on_missing_key()
def test_dome_team_returns_no_weather()
```

---

## Commit

```
feat(lineup-optimizer): Lineup Optimizer Agent

Vegas implied totals via The Odds API (configured, not "ASK USER").
Weather via OpenWeatherMap — outdoor stadiums only.
LineupScorer: Vegas, matchup, injury, snap trend, weather factors.
LineupOptimizer: FLEX optimization, locked player support.
Haiku for edge case notes only — all decisions in Python.
Platform-agnostic via LeaguePlatformAPI.
Coverage: X%.
```

---
---

# Stage 21: Waiver Wire Agent

## Before starting, read:
- `docs/stages/stage-14-season-roster.md` (LeaguePlatformAPI)
- `docs/stages/stage-15-roster-monitor.md` (must be complete)
- `docs/INSEASON.md` — Waiver Wire section
- `docs/rules/COST_RULES.md`

---

## Goal

Weekly waiver wire recommendations. Surfaces free agents with
emerging opportunity signals ranked by value window. Platform-
agnostic. Costs 8 credits per run (Standard+ tier).

---

## Model: `claude-haiku-4-5-20251001` | Max tokens: 800

---

## Part 1 — Signal detection (Python)

```python
# backend/agents/waiver_wire.py
"""
WaiverWire Agent — weekly free agent recommendations.

Signal detection is pure Python.
Haiku used only for value window estimation and ranking notes.
"""
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import BaseAgent
from backend.integrations.platform_factory import get_platform_api
from backend.integrations.platform_models import FreeAgent
from backend.models.user_league import UserLeague
from backend.repositories.season_roster_repo import SeasonRosterRepository
from backend.utils.nfl_schedule import get_current_week, is_nfl_season

logger = logging.getLogger(__name__)
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 800

SIGNAL_TYPES = (
    "snap_spike",
    "depth_promotion",
    "target_surge",
    "schedule_stream",
)


@dataclass
class WaiverSignal:
    player_name: str
    position: str
    team_abbr: str
    ownership_pct: float
    signal_type: str
    signal_strength: str    # strong | moderate | weak
    value_window: str       # ongoing | 2_weeks | 1_week
    confidence: str         # high | medium | low
    reasoning: str


class WaiverWireAgent(BaseAgent):
    agent_name = "waiver_wire"

    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self._roster_repo = SeasonRosterRepository(db)
        self._client = anthropic.AsyncAnthropic()

    async def run_for_league(
        self,
        user_id: uuid.UUID,
        league: UserLeague,
        week: int,
    ) -> list[WaiverSignal]:
        """
        Analyze free agents for a league.
        Returns ranked waiver recommendations.
        Costs 8 credits — verified by caller.
        """
        platform = await get_platform_api(league, self._db)
        free_agents = await platform.get_free_agents()

        # Get rostered player IDs to exclude them
        my_roster = await self._roster_repo.get_user_roster(
            user_id, league.id
        )
        rostered_ids = {
            str(r.player_id) for r in my_roster
        }

        # Detect signals in Python
        candidates = []
        for fa in free_agents:
            if fa.platform_player_id in rostered_ids:
                continue  # Already owned

            signal = self._detect_signal(fa, week)
            if signal:
                candidates.append(signal)

        if not candidates:
            return []

        # Haiku for value windows and ranking
        return await self._rank_with_haiku(candidates, league)

    def _detect_signal(
        self,
        player: FreeAgent,
        week: int,
    ) -> Optional[WaiverSignal]:
        """
        Detect waiver opportunity signals. Pure Python.
        Uses beat_reporter_signals and player_profiles tables.
        """
        # In full implementation:
        # 1. Check beat_reporter_signals for recent injury above player
        # 2. Check player snap count history (from NflDataWarehouse)
        # 3. Check target share trend in recent games
        # 4. Check upcoming schedule matchups

        # Skip players with high ownership — not a waiver wire value
        if player.ownership_pct > 0.60:
            return None

        # Placeholder — full impl queries central tables
        return None

    async def _rank_with_haiku(
        self,
        candidates: list[WaiverSignal],
        league: UserLeague,
    ) -> list[WaiverSignal]:
        """
        Haiku ranks candidates and estimates value windows.
        Only called when candidates exist.
        """
        prompt = (
            "Rank these waiver wire candidates for a fantasy "
            f"football league. Format: {league.scoring} "
            f"{league.draft_type}. "
            "Assign value_window: ongoing | 2_weeks | 1_week. "
            "Output ONLY JSON array maintaining all input fields "
            "plus updated value_window and confidence.\n\n"
            + json.dumps([
                {
                    "name": c.player_name,
                    "position": c.position,
                    "signal": c.signal_type,
                    "strength": c.signal_strength,
                    "ownership": c.ownership_pct,
                }
                for c in candidates[:20]
            ])
        )

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

        await self._log_api_usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=(
                response.usage.input_tokens / 1_000_000 * 0.8
                + response.usage.output_tokens / 1_000_000 * 4.0
            ),
        )

        try:
            ranked = json.loads(response.content[0].text.strip())
            return ranked[:10]  # Top 10 recommendations
        except json.JSONDecodeError:
            return candidates[:10]
```

---

## Part 2 — Router

```python
# backend/routers/waiver_wire.py

@router.get("/{league_id}/recommendations")
async def get_waiver_recommendations(
    league_id: uuid.UUID,
    position: Optional[str] = None,
    user: User = Depends(get_current_user),
    _: None = Depends(require_credits("waiver_wire")),
    db: AsyncSession = Depends(get_db),
):
    """
    Weekly waiver wire recommendations.
    Requires Standard plan. Costs 8 credits.
    """
    week = get_current_week()
    if not week:
        raise HTTPException(status_code=400, detail="NFL season not active")

    league = await _get_user_league(league_id, user, db)
    agent = WaiverWireAgent(db)
    recommendations = await agent.run_for_league(user.id, league, week)

    if position:
        recommendations = [
            r for r in recommendations
            if r.position == position.upper()
        ]

    return {
        "week": week,
        "league_id": str(league_id),
        "recommendations": [
            {
                "player_name": r.player_name,
                "position": r.position,
                "signal_type": r.signal_type,
                "value_window": r.value_window,
                "confidence": r.confidence,
                "ownership_pct": r.ownership_pct,
            }
            for r in recommendations
        ],
    }
```

---

## Required test cases

```python
def test_rostered_players_excluded()
def test_high_ownership_players_excluded()  # >60% owned
def test_snap_spike_signal_detected()
def test_depth_promotion_signal_detected()
def test_one_week_streamers_labeled_correctly()
def test_value_window_estimated()
def test_position_filter_works()
def test_credits_deducted_before_recommendations()
def test_offseason_returns_error()
def test_platform_agnostic()
```

---

## Commit

```
feat(waiver-wire): Waiver Wire Agent

Signal detection in Python: snap spikes, depth promotions,
target surges, schedule streams.
Haiku for value window estimation and ranking (top 10).
8cr cost via require_credits("waiver_wire") — Standard+.
Platform-agnostic via LeaguePlatformAPI.
Coverage: X%.
```

---
---

# Stage 22: Pipeline Admin UI

## Before starting, read:
- `docs/stages/stage-14-season-roster.md` (in-season agent list)
- The admin endpoints already exist in `backend/routers/admin.py`

---

## Goal

React admin page for monitoring pipeline freshness, triggering
runs, and reviewing API costs. Most backend is already built.
This stage is primarily frontend with minor backend additions.

---

## What already exists

From the current `backend/routers/admin.py`:
- `GET /admin/pipeline-status` ✓
- `POST /admin/pipeline/run` ✓
- `POST /admin/pipeline/dry-run` ✓
- `GET /admin/cost-report` ✓

---

## Backend additions needed

### 1. Per-agent freshness thresholds

Replace the uniform 7-day threshold with per-agent values:

```python
# backend/routers/admin.py — update KNOWN_AGENTS

AGENT_FRESHNESS_THRESHOLDS = {
    # Pre-draft agents
    "team_systems":      timedelta(days=30),
    "roster_changes":    timedelta(days=7),
    "player_profiles":   timedelta(days=7),
    "injury_risk":       timedelta(days=7),
    "schedule":          timedelta(days=7),
    # In-season agents
    "beat_reporter":     timedelta(days=2),
    "roster_monitor":    timedelta(days=7),
    "trade_value":       timedelta(days=7),
    "opponent_analyzer": timedelta(days=7),
    "waiver_wire":       timedelta(days=7),
    "lineup_optimizer":  timedelta(days=7),
    # Gameday (Stage 24)
    "beat_reporter_ingame":   timedelta(hours=1),
    "beat_reporter_pregame":  timedelta(hours=3),
    "beat_reporter_postgame": timedelta(days=1),
}
```

### 2. Game day monitor status endpoint

```python
# backend/routers/admin.py

@router.get("/gameday-status")
async def get_gameday_status():
    """
    Game day polling status for the monitor panel.
    """
    from backend.utils.nfl_schedule import (
        is_game_day, is_in_game_window,
        is_pre_game_window, is_nfl_season,
        get_current_week,
    )

    return {
        "is_nfl_season": is_nfl_season(),
        "is_game_day": is_game_day(),
        "is_pre_game_window": is_pre_game_window(),
        "is_in_game_window": is_in_game_window(),
        "current_week": get_current_week(),
        "polling_active": is_in_game_window() or is_pre_game_window(),
    }
```

---

## Frontend

`frontend/src/pages/Admin.jsx`

Sections:
1. **Pipeline Status** — freshness grid, manual trigger buttons
2. **Cost Report** — 30-day spend by agent
3. **Game Day Monitor** — polling status, last scan, signal count
4. **In-Season Status** — active user leagues, last run per agent

### Freshness indicators

```
● Green  — within threshold
● Yellow — approaching threshold (>75% of threshold elapsed)
● Red    — stale (past threshold)
```

### Trigger buttons

Each agent row has:
- `[Trigger]` — runs agent immediately
- `[Dry Run]` — estimates cost without running
- Last run timestamp
- Freshness badge

---

## Required test cases

```python
# Backend
def test_freshness_threshold_per_agent()
def test_team_systems_stale_after_30_days()
def test_beat_reporter_stale_after_2_days()
def test_gameday_status_returns_correct_window()

# Frontend
test('pipeline status renders all agents')
test('stale agent shows red badge')
test('trigger button fires POST /admin/pipeline/run')
test('cost report shows 30-day totals')
test('game day monitor shows inactive when offseason')
```

---

## Commit

```
feat(admin): Pipeline Admin UI and game day monitor

Per-agent freshness thresholds (2d–30d based on agent type).
Game day monitor status endpoint and UI panel.
In-season agent status section.
React admin page: pipeline status, cost report, gameday monitor.
Coverage: X%.
```

---
---

# Stage 23: Deployment, CI/CD, and Pre-Draft Verification

## Before starting, read:
- All stage docs for completed stages
- This is a SaaS deployment — not single-user

---

## Goal

Production-ready deployment with CI/CD, correct environment
configuration, database migrations automated, and pre-draft
verification checklist. Updated from original single-user spec.

---

## What changed from original spec

The original Stage 23 assumed:
- Single `YAHOO_REFRESH_TOKEN` env var
- Single `YAHOO_LEAGUE_ID` env var
- Single-user deployment

The SaaS deployment uses:
- Per-user OAuth tokens in DB (`platform_credentials` table)
- Per-user league configs in DB (`user_leagues` table)
- `PLATFORM_TOKEN_ENCRYPTION_KEY` for token encryption
- `CLERK_SECRET_KEY` + `VITE_CLERK_PUBLISHABLE_KEY` for auth
- `THE_ODDS_API_KEY` + `OPENWEATHERMAP_API_KEY` for in-season

---

## Part 1 — Railway environment variables

Verify all of these are set in Railway before deploy:

```
# Core
DATABASE_URL=postgresql+asyncpg://...
ENVIRONMENT=production
SECRET_KEY=<generated>

# AI
ANTHROPIC_API_KEY=sk-ant-...

# Auth (Clerk)
CLERK_SECRET_KEY=sk_test_...
VITE_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_WEBHOOK_SECRET=whsec_...

# Platform token encryption
PLATFORM_TOKEN_ENCRYPTION_KEY=<Fernet key>

# In-season data (Stage 20)
THE_ODDS_API_KEY=...
OPENWEATHERMAP_API_KEY=...

# App URL (for ESPN bookmarklet callback)
APP_URL=https://fantasymanager-production.up.railway.app
VITE_API_URL=https://fantasymanager-production.up.railway.app

# Yahoo (YOUR developer app credentials — server-side only)
YAHOO_CLIENT_ID=...
YAHOO_CLIENT_SECRET=...
YAHOO_REDIRECT_URI=https://fantasymanager-production.up.railway.app/auth/yahoo/callback
# Note: YAHOO_REFRESH_TOKEN no longer an env var — stored per-user in DB

# RapidAPI (for FantasyPros ADP data)
RAPIDAPI_KEY=...
```

---

## Part 2 — GitHub Actions CI

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: fantasy_test
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432

    env:
      DATABASE_URL: postgresql+asyncpg://postgres:postgres@localhost:5432/fantasy_test
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY_TEST }}
      ENVIRONMENT: test
      SECRET_KEY: test-secret-key
      CLERK_SECRET_KEY: sk_test_placeholder
      PLATFORM_TOKEN_ENCRYPTION_KEY: ${{ secrets.PLATFORM_TOKEN_ENCRYPTION_KEY_TEST }}

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Install uv
        run: pip install uv

      - name: Install dependencies
        run: uv sync

      - name: Run migrations
        run: uv run alembic upgrade head

      - name: Run tests
        run: uv run pytest tests/ -x --tb=short -q --cov=backend --cov-report=term

      - name: Lint
        run: |
          uv run ruff check backend/ tests/
          uv run mypy backend/ --ignore-missing-imports

  build-frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
      - run: cd frontend && npm ci
      - run: cd frontend && npm run build
        env:
          VITE_CLERK_PUBLISHABLE_KEY: pk_test_placeholder
          VITE_API_URL: https://fantasymanager-production.up.railway.app
```

Required GitHub Actions secrets:
- `ANTHROPIC_API_KEY_TEST` — test API key for CI
- `PLATFORM_TOKEN_ENCRYPTION_KEY_TEST` — test encryption key

---

## Part 3 — Automated database migrations

Add to Railway's deploy command in `railway.toml`:

```toml
[deploy]
startCommand = "alembic upgrade head && uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"
```

This ensures migrations run automatically before app starts
on every deploy. Safe because Alembic is idempotent.

---

## Part 4 — Pre-draft checklist

Run before any real draft. Verify each item passes.

```bash
# 1. Health check
curl https://fantasymanager-production.up.railway.app/health
# Expected: {"status":"ok","environment":"production"}

# 2. Auth works
# Open app in browser → sign in → reach dashboard

# 3. Database connected
curl https://fantasymanager-production.up.railway.app/players/summary
# Expected: 200 with player counts

# 4. Pipeline data fresh
curl https://fantasymanager-production.up.railway.app/admin/pipeline-status
# Expected: all key agents green (within threshold)

# 5. Market values fresh
curl https://fantasymanager-production.up.railway.app/pipeline/market-values/status
# Expected: year=2026, refreshed_at within 24 hours

# 6. Encryption key configured
python -c "
from backend.integrations.token_encryption import encrypt_token, decrypt_token
token = 'test'
assert decrypt_token(encrypt_token(token)) == token
print('Encryption: OK')
"

# 7. Yahoo OAuth works (for your personal league)
# GET /auth/yahoo/connect → complete OAuth flow
# Should redirect back to /account?connected=yahoo

# 8. League synced
# Connect your Yahoo league via /league-setup
# Verify draft history imported

# 9. Draft board populated
curl https://fantasymanager-production.up.railway.app/draftboard
# Expected: 200+ players with valuations

# 10. Live draft Playwright bridge (auction users)
# POST /draft/start with your team ID
# Verify bridge connects to Yahoo draft room
```

---

## Part 5 — Morning-of-draft refresh

```bash
# Run day of draft, in this order:
# 1. Refresh market values
POST /pipeline/refresh-market-values

# 2. Run beat reporter (latest news)
POST /pipeline/run-beat-reporter

# 3. Run roster changes (any last-minute moves)
POST /pipeline/run-roster-changes

# 4. Verify everything looks right
GET /admin/pipeline-status
```

---

## Mock draft requirements

Two mock drafts through the app before real draft.

Auction mock draft checklist:
- [ ] Nomination detection under 100ms
- [ ] Bid placement works on platform
- [ ] Budget trackers accurate throughout
- [ ] Dependency flags activate correctly
- [ ] Combo threat alerts fire correctly
- [ ] MANUAL_ACTION_REQUIRED fires on bridge failure
- [ ] No silent failures — all errors surface in UI

Snake mock draft checklist:
- [ ] Pick recommendations arrive before clock expires
- [ ] Positional need correctly identified by round
- [ ] Strategy mode changes recommendations
- [ ] ADP displayed correctly
- [ ] VOE values positive for undervalued players

Fix all issues before mock draft 2. Both must pass.

---

## Commit

```
chore(deployment): CI/CD, automated migrations, pre-draft verification

GitHub Actions: pytest + frontend build on push.
Railway: alembic upgrade head runs before server start.
All 10 pre-draft checks documented and passing.
SaaS env vars fully documented (no single YAHOO_REFRESH_TOKEN).
Mock draft checklists for auction and snake formats.
```

---
---

# Stage 24: Real-Time Game Day Injury Monitoring

## Before starting, read:
- `docs/stages/stage-21-waiver-wire.md` (must be complete)
- `docs/AGENTS.md` — Agent 6: Beat Reporter
- `docs/INSEASON.md`

---

## Goal

During NFL games, poll for injury news every 5 minutes and push
real-time alerts to all connected users. A player carted off
appears as an in-app push notification within 5-10 minutes.

This is a server-side feature — alerts go to all users who have
that player on their roster, not just one user.

---

## Multi-user alert routing

The original spec assumed a single user. For SaaS:
- Injury alerts must be routed to users who own that player
- WebSocket connections are per-user
- A player getting injured alerts only their fantasy owners

```python
# backend/websocket/manager.py — extend existing ws_manager

async def broadcast_to_player_owners(
    player_id: uuid.UUID,
    message: dict,
) -> None:
    """
    Send an alert only to users who have this player
    on an active fantasy roster.
    """
    from backend.database import AsyncSessionLocal
    from backend.models.season_roster import SeasonRoster
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SeasonRoster.user_id)
            .where(
                SeasonRoster.player_id == player_id,
                SeasonRoster.is_active.is_(True),
            )
            .distinct()
        )
        owner_ids = {str(row[0]) for row in result.all()}

    # Send only to connected sockets for owners
    for connection_user_id, websocket in ws_manager.connections.items():
        if connection_user_id in owner_ids:
            await websocket.send_json(message)
```

---

## Part 1 — NFL schedule utilities

```python
# backend/utils/nfl_schedule.py
"""
NFL schedule and game window utilities.
Used by all game-day polling jobs.
All jobs must call is_nfl_season() first and return
immediately if False. This prevents any polling
during offseason and development.
"""
from datetime import datetime, time
import pytz

ET = pytz.timezone("America/New_York")

# Game windows per day (Eastern Time)
GAME_WINDOWS = {
    "thursday": {"pre_start": "18:45", "poll_start": "20:15", "poll_end": "23:30"},
    "saturday": {"pre_start": "11:30", "poll_start": "13:00", "poll_end": "23:30"},
    "sunday":   {"pre_start": "11:30", "poll_start": "13:00", "poll_end": "23:30"},
    "monday":   {"pre_start": "18:45", "poll_start": "20:15", "poll_end": "23:30"},
}

NFL_SEASON_START_MONTH = 9   # September
NFL_SEASON_END_MONTH = 2     # February (playoffs)


def is_nfl_season() -> bool:
    """
    True during regular season and playoffs (Sep–Feb).
    False during offseason (Mar–Aug).
    All polling jobs check this first.
    """
    month = datetime.now(ET).month
    return month >= NFL_SEASON_START_MONTH or month <= NFL_SEASON_END_MONTH


def is_game_day() -> bool:
    """Is today a day with NFL games?"""
    today = datetime.now(ET).strftime("%A").lower()
    return today in GAME_WINDOWS


def _parse_time(t: str) -> time:
    h, m = t.split(":")
    return time(int(h), int(m))


def is_pre_game_window() -> bool:
    """
    90-minute pre-game window — inactives declarations.
    Poll every 10 minutes.
    """
    now = datetime.now(ET)
    day = now.strftime("%A").lower()
    window = GAME_WINDOWS.get(day)
    if not window:
        return False
    current = now.time()
    return (
        _parse_time(window["pre_start"])
        <= current
        < _parse_time(window["poll_start"])
    )


def is_in_game_window() -> bool:
    """
    Active game window — poll every 5 minutes.
    """
    now = datetime.now(ET)
    day = now.strftime("%A").lower()
    window = GAME_WINDOWS.get(day)
    if not window:
        return False
    current = now.time()
    return (
        _parse_time(window["poll_start"])
        <= current
        <= _parse_time(window["poll_end"])
    )


def get_current_week() -> int | None:
    """
    Returns current NFL week (1–18).
    Returns None if offseason.
    Saturday games only active weeks 15–18.
    """
    if not is_nfl_season():
        return None
    # Simplified — full impl uses NFL schedule data
    # to compute exact week number from date
    now = datetime.now(ET)
    if now.month == 9:
        return max(1, (now.day - 1) // 7 + 1)
    return None  # Full impl handles all months
```

---

## Part 2 — WebSocket manager (multi-user)

```python
# backend/websocket/manager.py — update existing manager
"""
WebSocket manager with per-user connection tracking.
Injury alerts routed only to users who own the affected player.
"""
import uuid
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    def __init__(self):
        # {user_id_str: WebSocket}
        # One active connection per user (latest wins)
        self.connections: dict[str, WebSocket] = {}

    async def connect(
        self, websocket: WebSocket, user_id: str
    ) -> None:
        await websocket.accept()
        # Close any existing connection for this user
        if user_id in self.connections:
            try:
                await self.connections[user_id].close()
            except Exception:
                pass
        self.connections[user_id] = websocket
        logger.info("WS connected: user=%s total=%d",
                    user_id[:8], len(self.connections))

    def disconnect(self, user_id: str) -> None:
        self.connections.pop(user_id, None)

    async def broadcast_to_player_owners(
        self,
        player_id: uuid.UUID,
        message: dict,
    ) -> None:
        """
        Send alert only to users who own this player
        on an active fantasy roster.
        Never broadcasts to all users — privacy and performance.
        """
        from backend.database import AsyncSessionLocal
        from backend.models.season_roster import SeasonRoster
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SeasonRoster.user_id)
                .where(
                    SeasonRoster.player_id == player_id,
                    SeasonRoster.is_active.is_(True),
                )
                .distinct()
            )
            owner_ids = {str(row[0]) for row in result.all()}

        sent = 0
        for uid, websocket in list(self.connections.items()):
            if uid in owner_ids:
                try:
                    await websocket.send_json(message)
                    sent += 1
                except Exception:
                    self.disconnect(uid)

        logger.info(
            "Injury alert sent to %d/%d owners of player %s",
            sent, len(owner_ids), player_id,
        )

    async def broadcast_all(self, message: dict) -> None:
        """
        Broadcast to all connected users.
        Use sparingly — only for truly global events.
        """
        for uid, websocket in list(self.connections.items()):
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(uid)


ws_manager = WebSocketManager()
```

WebSocket endpoints must pass `user_id` on connect:

```python
# In backend/main.py or relevant router

@app.websocket("/ws/news")
async def news_websocket(
    websocket: WebSocket,
    user_id: str = Query(...),
    # user_id passed as query param: /ws/news?user_id=clerk_xxx
):
    await news_ws_manager.connect(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        news_ws_manager.disconnect(user_id)
```

Frontend connects with user_id from Clerk:
```javascript
const { userId } = useAuth()
const ws = new WebSocket(
  `wss://api.example.com/ws/news?user_id=${userId}`
)
```

---

## Part 3 — APScheduler game-day jobs

Add to `backend/main.py` startup, alongside existing daily job:

```python
# Existing daily job — unchanged
scheduler.add_job(
    beat_reporter_agent.run,
    "cron",
    hour=7,
    id="beat_reporter_daily",
)

# Pre-game: every 10 min (90 min before kickoff)
# Catches inactives and late scratches
scheduler.add_job(
    _run_beat_reporter_pregame,
    "cron",
    minute="*/10",
    id="beat_reporter_pregame",
    misfire_grace_time=60,
)

# In-game: every 5 min
# Catches in-game injuries
scheduler.add_job(
    _run_beat_reporter_ingame,
    "cron",
    minute="*/5",
    id="beat_reporter_ingame",
    misfire_grace_time=30,
)

# Post-game: once at 11:30pm ET on game days
# Captures snap counts, triggers waiver wire
scheduler.add_job(
    _run_beat_reporter_postgame,
    "cron",
    day_of_week="thu,sat,sun,mon",
    hour=23,
    minute=30,
    timezone="America/New_York",
    id="beat_reporter_postgame",
)


async def _run_beat_reporter_pregame():
    if not is_pre_game_window() or not is_nfl_season():
        return  # Fast exit — called every 10 min
    from backend.agents.beat_reporter import BeatReporterAgent
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        agent = BeatReporterAgent(db)
        await agent.run_pregame()


async def _run_beat_reporter_ingame():
    if not is_in_game_window() or not is_nfl_season():
        return  # Fast exit — called every 5 min
    from backend.agents.beat_reporter import BeatReporterAgent
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        agent = BeatReporterAgent(db)
        await agent.run_ingame()


async def _run_beat_reporter_postgame():
    if not is_game_day() or not is_nfl_season():
        return
    from backend.agents.beat_reporter import BeatReporterAgent
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        agent = BeatReporterAgent(db)
        await agent.run_postgame()
```

---

## Part 4 — Beat reporter game-day methods

Add to `backend/agents/beat_reporter.py`:

```python
async def run_pregame(self) -> None:
    """
    Pre-game window (90 min before kickoff).
    Runs every 10 minutes via scheduler.
    Focus: inactives, late scratches, GTD resolutions.
    """
    if not is_pre_game_window() or not is_nfl_season():
        return
    await self._scan_rotowire_feed(
        signal_types=["out", "doubtful", "inactive", "questionable"]
    )


async def run_ingame(self) -> None:
    """
    In-game polling — every 5 minutes.
    Focus: in-game injuries, ejections, emergency situations.
    """
    if not is_in_game_window() or not is_nfl_season():
        return
    await self._scan_rotowire_feed(
        signal_types=["injury", "inactive", "out"]
    )
    await self._scan_espn_injury_feed()


async def run_postgame(self) -> None:
    """
    Post-game scan at 11:30pm ET on game days.
    Focus: snap counts, post-game injury updates.
    Also triggers waiver wire agent for all active leagues.
    """
    if not is_game_day() or not is_nfl_season():
        return
    await self._scan_rotowire_feed(
        signal_types=["snap_count", "injury_update", "transaction"]
    )
    await self._trigger_waiver_wire_all_users()


async def _scan_rotowire_feed(
    self,
    signal_types: list[str] | None = None,
) -> int:
    """
    Scrape Rotowire public NFL news feed.
    URL: https://www.rotowire.com/football/news.php
    Deduplicates via content hash.
    Returns count of new signals written.
    """
    import hashlib
    import httpx
    from bs4 import BeautifulSoup

    ROTOWIRE_URL = "https://www.rotowire.com/football/news.php"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(ROTOWIRE_URL)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    # Parse news items — each has player name, text, type
    # Full implementation parses Rotowire's specific HTML structure

    new_signals = 0
    for item in self._parse_rotowire(soup):
        if signal_types and item["type"] not in signal_types:
            continue

        content_hash = hashlib.md5(
            item["text"].encode()
        ).hexdigest()

        # Skip if already seen
        if await self._signal_exists(content_hash):
            continue

        player_id = await self._match_player_by_name(
            item["player_name"], item.get("position")
        )

        await self._write_signal(item, content_hash, player_id)

        if item["type"] in ("injury", "out", "inactive"):
            await self._update_player_injury_status(
                player_id, item
            )
            if player_id:
                await ws_manager.broadcast_to_player_owners(
                    player_id=player_id,
                    message={
                        "type": "injury_alert",
                        "severity": "high",
                        "player_name": item["player_name"],
                        "signal_type": item["type"],
                        "raw_text": item["text"],
                        "replacement": await self._find_replacement(
                            player_id, item.get("position")
                        ),
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                )

        new_signals += 1

    logger.info(
        "Rotowire scan: %d new signals", new_signals
    )
    return new_signals


async def _trigger_waiver_wire_all_users(self) -> None:
    """
    After post-game, trigger waiver wire analysis
    for all active user leagues.
    Only runs if draft has happened (roster records exist).
    """
    from sqlalchemy import select, func
    from backend.models.season_roster import SeasonRoster

    roster_count = await self._db.scalar(
        select(func.count(SeasonRoster.id))
    )
    if roster_count == 0:
        return  # Draft hasn't happened yet

    logger.info("Triggering post-game waiver wire for all users")
    from backend.models.user_league import UserLeague
    from backend.models.user import User

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
            from backend.agents.waiver_wire import WaiverWireAgent
            from backend.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                agent = WaiverWireAgent(db)
                week = get_current_week()
                if week:
                    await agent.run_for_league(
                        user.id, league, week
                    )
        except Exception as exc:
            logger.error(
                "Post-game waiver failed for league %s: %s",
                league.id, exc,
            )
```

---

## Part 5 — React injury alert UI

```javascript
// frontend/src/components/InjuryAlert.jsx
import { useEffect } from 'react'
import { useAuth } from '@clerk/clerk-react'

export function InjuryAlertProvider({ children }) {
  const { userId } = useAuth()

  useEffect(() => {
    if (!userId) return

    // Request browser notification permission once
    if (Notification.permission === 'default') {
      Notification.requestPermission()
    }

    // Connect WebSocket with user_id for targeted alerts
    const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'
    const wsUrl = API_URL.replace('http', 'ws')
    const ws = new WebSocket(`${wsUrl}/ws/news?user_id=${userId}`)

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type !== 'injury_alert') return

      // In-app alert (toasts/alert store — existing system)
      window.dispatchEvent(
        new CustomEvent('injury_alert', { detail: data })
      )

      // Browser push notification (works when minimized)
      if (Notification.permission === 'granted') {
        new Notification(`Fantasy Alert: ${data.player_name}`, {
          body: data.replacement
            ? `Consider adding ${data.replacement.name}`
            : data.raw_text,
          tag: `injury-${data.player_name}`,
          // tag prevents duplicate notifications
        })
      }
    }

    return () => ws.close()
  }, [userId])

  return children
}

// Alert card rendered in the existing alert system:
//
// ┌────────────────────────────────────────────┐
// │ 🚨 INJURY ALERT                     [✕]   │
// │                                            │
// │ Bijan Robinson — Carted off, knee          │
// │ "Robinson was helped off the field..."     │
// │                                            │
// │ Suggested pickup: Tyler Allgeier (ATL)     │
// │ Available · Projected: 12.4 pts            │
// │                                            │
// │ [View Waiver Wire]  [Dismiss]              │
// └────────────────────────────────────────────┘
```

---

## Part 6 — Pipeline Admin game-day panel

Add to `frontend/src/pages/Admin.jsx`:

```
GAME DAY MONITOR
┌──────────────────────────────────────────────┐
│ Status: ACTIVE — Sunday in-game window       │
│ Last scan: 3 minutes ago                     │
│ Signals today: 14 new                        │
│ Injury alerts sent: 2 (to 6 unique users)    │
│                                              │
│ Schedule today:                              │
│  Pre-game:  every 10 min (11:30am–1:00pm)   │
│  In-game:   every 5 min (1:00pm–11:30pm)    │
│  Post-game: once at 11:30pm                  │
│                                              │
│ [Force Scan Now]                             │
└──────────────────────────────────────────────┘
```

Off-season display:
```
GAME DAY MONITOR
Status: INACTIVE (offseason)
Polling resumes: September 2026
Daily scan: 7am ET (active year-round)
```

Fetches from `GET /admin/gameday-status` (defined in Stage 22).

---

## Cost estimate

```
Per scan: ~$0.006 (20 signals × 300 tokens × Haiku rate)

Sunday:
  Pre-game:  9 scans × $0.006  = $0.054
  In-game:  84 scans × $0.006  = $0.504
  Post-game: 1 scan × $0.006   = $0.006
  Total Sunday:                  ~$0.56

Full 17-week season:
  Sundays (17):                  $9.52
  Thursdays (15 TNF):            $4.20
  Mondays (17 MNF):              $4.76
  Saturdays weeks 15-18 (4):    $2.24
  Total:                         ~$20.72
```

Many scans find zero new signals and exit in milliseconds —
actual cost will be lower than estimate.

---

## Required test cases

```python
# tests/unit/utils/test_nfl_schedule.py
def test_is_game_day_thursday()
def test_is_game_day_tuesday_returns_false()
def test_is_in_game_window_during_window()
def test_is_in_game_window_outside_window_returns_false()
def test_is_pre_game_window_90_min_before_kickoff()
def test_is_pre_game_window_after_kickoff_returns_false()
def test_saturday_games_only_weeks_15_to_18()
def test_is_nfl_season_false_in_may()
def test_is_nfl_season_true_in_october()

# tests/unit/agents/test_beat_reporter_gameday.py
def test_run_ingame_exits_if_not_in_window()
def test_run_ingame_exits_if_offseason()
def test_run_pregame_exits_if_not_in_window()
def test_run_postgame_exits_if_not_game_day()
def test_rotowire_dedup_skips_seen_signals()
def test_content_hash_prevents_duplicate_writes()
def test_injury_signal_calls_broadcast_to_player_owners()
def test_non_injury_signal_does_not_broadcast()
def test_waiver_wire_triggered_after_postgame()
def test_waiver_wire_not_triggered_before_draft()
def test_cost_logged_with_correct_agent_name()

# tests/unit/websocket/test_ws_manager.py
def test_injury_alert_routed_to_player_owners_only()
def test_non_owner_does_not_receive_alert()
def test_player_on_multiple_rosters_alerts_all_owners()
def test_websocket_connection_tracked_by_user_id()
def test_duplicate_connection_for_same_user_closes_old()
def test_disconnected_socket_removed_from_connections()
```

---

## Commit

```
feat(gameday): Real-time game day injury monitoring

Targeted polling: 10min pre-game, 5min in-game, once post-game.
Rotowire scraping with content-hash deduplication.
Multi-user WebSocket routing: alerts only to player owners.
Browser push notifications when app minimized.
Waiver wire auto-triggered after post-game scan.
Pipeline Admin: game day monitor panel.
Estimated cost: ~$21/season.
Coverage: X%.
```
