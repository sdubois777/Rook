# Stage 25: SaaS Foundation — Database Architecture & LeagueConfig

## Before starting, read:
- `docs/ARCHITECTURE.md`
- `docs/SCHEMA.md`
- `docs/rules/PATTERNS.md`
- `docs/LEAGUE_RULES.md`

**This stage must be completed before all other SaaS stages.**
Everything else (auth, league sync, snake draft) depends on this foundation.

---

## Goal
Split the database into a central shared database (player data, projections,
pipeline outputs) and per-user tables (league settings, auction history,
preferences, rosters). Introduce `LeagueConfig` as the single source of
truth for all league-specific parameters, replacing hardcoded assumptions
throughout the codebase.

---

## The Core Problem
The codebase has hardcoded assumptions everywhere:
```python
LEAGUE_SKILL_DOLLAR_POOL = 185 * 12   # hardcoded 12 teams
budget = 200                            # hardcoded $200
WR_POOL_SIZE = 60                       # hardcoded pool
replacement_rank = 48                   # hardcoded for 12-team
```
Every one of these breaks for an 8-team or 14-team league. Fix them all.

---

## Part 1 — LeagueConfig

Create `backend/models/league_config.py`:

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class LeagueConfig:
    # Core settings (user-provided)
    team_count: int = 12
    draft_type: Literal["auction", "snake"] = "auction"
    scoring: Literal["ppr", "half_ppr"] = "ppr"
    budget: int = 200                    # auction only
    pick_position: int | None = None     # snake only (1-N)
    platform: Literal["yahoo", "espn", "sleeper"] = "yahoo"
    league_id: str = ""
    season_year: int = 2026

    # Roster slots (standard for all supported leagues)
    # Non-standard leagues (2QB, superflex, IDP) not supported
    qb_slots: int = 1
    rb_slots: int = 2
    wr_slots: int = 2
    te_slots: int = 1
    flex_slots: int = 1          # RB/WR/TE
    k_slots: int = 1
    def_slots: int = 1
    bench_slots: int = 7

    # Derived — computed from above, never set directly
    @property
    def total_teams(self) -> int:
        return self.team_count

    @property
    def skill_starter_slots(self) -> int:
        """QB+RB+WR+TE+FLEX — the $185 target positions"""
        return (self.qb_slots + self.rb_slots + 
                self.wr_slots + self.te_slots + self.flex_slots)

    @property
    def skill_budget_pct(self) -> float:
        """Fraction of budget for skill starters"""
        total_slots = (self.skill_starter_slots + 
                      self.k_slots + self.def_slots + self.bench_slots)
        # K + DEF + bench = low value, ~$15 of $200
        return 0.925  # consistent across league sizes

    @property
    def total_skill_pool(self) -> float:
        """Total auction dollars across all teams for skill positions"""
        return self.budget * self.team_count * self.skill_budget_pct

    @property
    def wr_replacement_rank(self) -> int:
        """WR rank below which player has no surplus value"""
        # 2 WR starters + 0.6 flex share per team
        return round((self.wr_slots + 0.6) * self.team_count)

    @property
    def rb_replacement_rank(self) -> int:
        return round((self.rb_slots + 0.4) * self.team_count)

    @property
    def qb_replacement_rank(self) -> int:
        return round(self.qb_slots * self.team_count * 1.2)

    @property
    def te_replacement_rank(self) -> int:
        return round(self.te_slots * self.team_count * 1.3)

    @property
    def is_auction(self) -> bool:
        return self.draft_type == "auction"

    @property
    def is_snake(self) -> bool:
        return self.draft_type == "snake"

    @property
    def rec_points(self) -> float:
        """Points per reception for scoring calculations"""
        return {"ppr": 1.0, "half_ppr": 0.5}.get(self.scoring, 1.0)

    def positional_budget_pct(self, position: str) -> float:
        """Fraction of skill pool allocated to each position"""
        return {
            "RB": 0.38,
            "WR": 0.32,
            "QB": 0.10,
            "TE": 0.10,
            "K":  0.05,
            "DEF": 0.05,
        }.get(position, 0.0)

    def positional_budget(self, position: str) -> float:
        return self.total_skill_pool * self.positional_budget_pct(position)

    # Tier count targets scale with league size
    @property
    def tier_counts(self) -> dict[str, dict[int, int]]:
        scale = self.team_count / 12  # 1.0 for 12-team
        return {
            "WR": {
                1: max(2, round(3 * scale)),
                2: max(4, round(6 * scale)),
                3: max(8, round(10 * scale)),
                4: max(12, round(15 * scale)),
            },
            "RB": {
                1: max(2, round(3 * scale)),
                2: max(4, round(6 * scale)),
                3: max(8, round(10 * scale)),
                4: max(10, round(12 * scale)),
            },
            "QB": {
                1: max(1, round(2 * scale)),
                2: max(2, round(4 * scale)),
                3: max(4, round(6 * scale)),
            },
            "TE": {
                1: max(1, round(2 * scale)),
                2: max(2, round(4 * scale)),
                3: max(4, round(6 * scale)),
            },
        }


# Default config (current single-league behavior, unchanged)
DEFAULT_LEAGUE_CONFIG = LeagueConfig(
    team_count=12,
    draft_type="auction",
    scoring="ppr",
    budget=200,
)
```

---

## Part 2 — Propagate LeagueConfig through valuation engine

### Files that need updating

**backend/engines/valuation.py**

Replace every hardcoded constant:
```python
# BEFORE — scattered hardcoded values
LEAGUE_SKILL_DOLLAR_POOL = 185 * 12
WR_POOL_SIZE = 60
REPLACEMENT_LEVELS = {"WR": 119, "RB": 136, ...}

# AFTER — all derived from config
def compute_valuations(
    players: list[Player],
    profiles: list[PlayerProfile],
    config: LeagueConfig = DEFAULT_LEAGUE_CONFIG,
    db: AsyncSession = None,
) -> None:
    skill_pool = config.total_skill_pool
    wr_pool_size = config.wr_replacement_rank
    rb_pool_size = config.rb_replacement_rank
    # etc.
```

**backend/engines/backtest.py**
Pass config to all valuation calls.

**backend/agents/valuation_agent.py**
Pass league context (team_count, budget, scoring) to Sonnet prompt.

**scripts/compute_valuations.py**
Accept --league-config flag or load from DB.

---

## Part 3 — Database architecture split

### Central DB tables (unchanged, shared by all users)
These tables stay exactly as they are. They contain data that is
the same regardless of which user is viewing it:

```
players                    — NFL player records
player_profiles            — AI projections
player_dependencies        — dependency flags
player_injury_profiles     — injury risk
team_systems               — offensive system grades
schedule_data              — NFL schedule
beat_reporter_signals      — news/transactions
nfl_comp_table             — historical comp data
api_usage_log              — global cost tracking
```

### Per-user tables (add user_id foreign key)
These tables are user-specific and must be isolated:

```sql
-- Add to existing tables
ALTER TABLE league_auction_history
  ADD COLUMN user_id UUID REFERENCES users(id);

ALTER TABLE opponent_profiles
  ADD COLUMN user_id UUID REFERENCES users(id);

ALTER TABLE user_preferences
  ADD COLUMN user_id UUID REFERENCES users(id);

-- Already user-scoped (watchlist, strategy)
-- Just ensure user_id index exists
CREATE INDEX IF NOT EXISTS idx_watchlist_user
  ON user_preferences(user_id);
```

### New tables for SaaS

```sql
-- User accounts (auth handled by Clerk/Supabase)
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_id   VARCHAR(100) UNIQUE NOT NULL,
  -- ID from auth provider (Clerk user ID)
  email         VARCHAR(255) UNIQUE NOT NULL,
  display_name  VARCHAR(100),
  tier          VARCHAR(20) DEFAULT 'free',
  -- free | starter | pro | league
  credits_remaining INTEGER DEFAULT 50,
  credits_monthly_limit INTEGER DEFAULT 50,
  created_at    TIMESTAMP DEFAULT NOW(),
  updated_at    TIMESTAMP DEFAULT NOW()
);

-- League configurations per user
CREATE TABLE user_leagues (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID REFERENCES users(id) NOT NULL,
  platform      VARCHAR(20) NOT NULL,
  -- yahoo | espn | sleeper
  league_id     VARCHAR(100) NOT NULL,
  league_name   VARCHAR(200),
  team_count    INTEGER DEFAULT 12,
  draft_type    VARCHAR(20) DEFAULT 'auction',
  -- auction | snake
  scoring       VARCHAR(20) DEFAULT 'ppr',
  -- ppr | half_ppr
  budget        INTEGER DEFAULT 200,
  -- auction only
  pick_position INTEGER,
  -- snake only
  season_year   INTEGER,
  is_active     BOOLEAN DEFAULT TRUE,
  last_synced   TIMESTAMP,
  sync_token    TEXT,
  -- platform OAuth token
  created_at    TIMESTAMP DEFAULT NOW(),
  UNIQUE(user_id, platform, league_id, season_year)
);

-- Credit usage tracking per user
CREATE TABLE credit_usage_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID REFERENCES users(id) NOT NULL,
  action      VARCHAR(100) NOT NULL,
  -- trade_analysis | trade_finder | waiver_wire
  -- (live_draft is NOT here — it's a tier entitlement)
  credits_used INTEGER NOT NULL,
  agent_name  VARCHAR(100),
  cost_usd    NUMERIC(10, 6),
  created_at  TIMESTAMP DEFAULT NOW()
);

-- Live draft activation tracking per league
-- Live draft is a tier entitlement, not a credit purchase.
-- Standard: up to 2 leagues. Pro: unlimited leagues.
-- One activation record per league per season.
CREATE TABLE live_draft_activations (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID REFERENCES users(id) NOT NULL,
  user_league_id  UUID REFERENCES user_leagues(id) NOT NULL,
  season_year     INTEGER NOT NULL,
  activated_at    TIMESTAMP DEFAULT NOW(),
  draft_completed BOOLEAN DEFAULT FALSE,
  completed_at    TIMESTAMP,
  UNIQUE(user_id, user_league_id, season_year)
);
```

### Alembic migration
```
alembic revision --autogenerate -m "saas_foundation"
alembic upgrade head
```

---

## Part 4 — Row-level security middleware

All per-user table queries must be scoped to the current user.
Add a middleware dependency that enforces this:

```python
# backend/middleware/user_scope.py

async def get_user_scoped_db(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[ScopedDB, None]:
    """
    Returns a DB session that automatically applies
    user_id filter to all per-user table queries.
    
    Central tables (players, profiles, etc.) are
    unaffected — they're read-only for all users.
    """
    yield ScopedDB(db=db, user_id=user_id)


class ScopedDB:
    def __init__(self, db: AsyncSession, user_id: str):
        self.db = db
        self.user_id = user_id

    async def get_user_leagues(self) -> list[UserLeague]:
        result = await self.db.execute(
            select(UserLeague)
            .where(UserLeague.user_id == self.user_id)
        )
        return result.scalars().all()

    async def get_auction_history(
        self, league_id: str
    ) -> list[LeagueAuctionHistory]:
        result = await self.db.execute(
            select(LeagueAuctionHistory)
            .where(
                LeagueAuctionHistory.user_id == self.user_id,
                LeagueAuctionHistory.league_id == league_id,
            )
        )
        return result.scalars().all()
    
    # Add methods for all per-user tables
```

---

## Part 5 — Credit deduction middleware

Before any expensive operation, check and deduct credits:

```python
# backend/middleware/credits.py

CREDIT_COSTS = {
    # Live draft is a tier entitlement — NOT a credit cost.
    # Standard: included for up to 2 synced leagues.
    # Pro: included for unlimited synced leagues.
    # No credits deducted for live draft usage.

    "trade_analysis":    10,  # ~$0.15 AI cost — Standard+
    "trade_finder":      20,  # ~$0.50 AI cost — Pro only
    "waiver_wire":        8,  # ~$0.05 AI cost — Standard+

    # Always free — 0 credits, no tier gate:
    # projections, draft board, news feed, player profiles,
    # injury monitoring (all tiers), league sync,
    # draft history, manager tendencies
}

async def require_credits(
    action: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    cost = CREDIT_COSTS.get(action, 1)
    if user.credits_remaining < cost:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_credits",
                "required": cost,
                "available": user.credits_remaining,
                "upgrade_url": "/pricing",
            }
        )
    # Deduct credits
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(credits_remaining=User.credits_remaining - cost)
    )
    # Log usage
    await db.execute(
        insert(CreditUsageLog).values(
            user_id=user.id,
            action=action,
            credits_used=cost,
        )
    )
    await db.commit()
```

---

## Part 6 — PPR scoring formula update

Update `compute_ppr_points()` to accept scoring format:

```python
def compute_ppr_points(
    receptions: float,
    yards: float,
    touchdowns: float,
    scoring: str = "ppr",
) -> float:
    """
    PPR scoring formula parameterized for format.
    
    ppr:      1.0 point per reception
    half_ppr: 0.5 points per reception
    standard: 0.0 points per reception
    """
    REC_MULTIPLIERS = {
        "ppr":      1.0,
        "half_ppr": 0.5,
        "standard": 0.0,
    }
    rec_pts = REC_MULTIPLIERS.get(scoring, 1.0)
    return (
        receptions * rec_pts +
        yards * 0.1 +
        touchdowns * 6.0
    )
```

Update all callers to pass `config.scoring`.

---

## Required test cases

```python
# tests/unit/models/test_league_config.py

def test_12_team_replacement_levels()
def test_8_team_replacement_levels_lower()
    """8-team: fewer teams = higher replacement rank per team"""
def test_14_team_replacement_levels_higher()
def test_total_skill_pool_scales_with_teams()
def test_rec_points_ppr_is_1()
def test_rec_points_half_ppr_is_0_5()
def test_positional_budget_sums_to_1()
def test_tier_counts_scale_with_team_count()
def test_default_config_matches_current_behavior()
    """DEFAULT_LEAGUE_CONFIG produces identical output
    to current hardcoded values"""

# tests/unit/engines/test_valuation_config.py

def test_valuation_accepts_league_config()
def test_8_team_produces_different_values_than_12()
def test_half_ppr_reduces_wr_relative_to_rb()
def test_hardcoded_12_no_longer_in_valuation_py()
    """grep for hardcoded 12 or 200 — should be zero"""

# tests/unit/middleware/test_credits.py

def test_insufficient_credits_raises_402()
def test_credits_deducted_after_action()
def test_usage_logged_per_action()
def test_free_actions_dont_deduct_credits()
```

---

## Verification before marking complete

1. `grep -rn "185 \* 12\|budget = 200\|team_count = 12" backend/` returns zero results
2. Valuation engine produces identical output for DEFAULT_LEAGUE_CONFIG vs old hardcoded values
3. Valuation engine produces correct output for 8-team $100 budget league (test manually)
4. Half PPR reduces WR values relative to RBs (RBs have more rush yards, fewer receptions)
5. All migrations apply cleanly: `alembic upgrade head`
6. Credit middleware correctly blocks requests with 0 credits
7. Row-level security: user A cannot query user B's auction history
8. All tests passing

---

## Commit
```
feat(saas): foundation — LeagueConfig + database architecture

LeagueConfig replaces all hardcoded league assumptions.
Team count (8-14), scoring (ppr/half_ppr), budget all parameterized.
Replacement levels, tier counts, pool sizes all derived from config.
Central DB tables unchanged — shared by all users.
Per-user tables scoped with user_id foreign key.
New tables: users, user_leagues, credit_usage_log.
Credit deduction middleware with CREDIT_COSTS map.
Row-level security middleware for per-user data.
PPR scoring formula accepts scoring format parameter.
Zero hardcoded league constants remain in codebase.
Coverage: X%.
```
