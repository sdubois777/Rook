# System Architecture

## Three Phases

```
PRE-DRAFT PIPELINE          LIVE DRAFT              IN-SEASON
──────────────────          ──────────              ─────────
6 Research Agents           Live Draft Agent        Trade Analyzer
      ↓                     + Playwright Bridge     Trade Proposal Engine
  Draft Bible          →    + React Draft UI   →    Lineup Optimizer
(PostgreSQL)                + Opponent Modeling     Waiver Wire Agent
                                                    Roster Monitor
```

---

## Phase 1: Pre-Draft Pipeline

Six agents run before draft day. Team Systems runs first (other agents inherit its output).
Others can run in parallel after Team Systems completes.

Run schedule:
- Once in early June after offseason programs
- Once in late July when training camp opens
- Weekly through August
- Daily the week of the draft
- Morning of the draft (final freshness pass)

Trigger: `python scripts/run_predraft_pipeline.py [--agent NAME] [--team ABBR] [--dry-run]`

---

## Phase 2: Live Draft

The Playwright bridge controls Yahoo's draft room directly from the app.
No need to switch tabs during the auction.

Event chain (zero polling anywhere):
```
Yahoo WS frames
  → Playwright WebSocket interception
  → FastAPI WebSocket push
  → React draft UI

User action (bid/nominate)
  → React
  → FastAPI endpoint
  → Playwright page.evaluate()
  → Yahoo draft room
```

Target round-trip latency: under 100ms.

Bridge failure handling: emit `MANUAL_ACTION_REQUIRED` immediately to UI.
Never crash silently.

---

## Phase 3: In-Season

Weekly agents run on APScheduler jobs. On-demand engines respond to user requests.

Weekly jobs:
- Roster Monitor (Wednesday, after MNF stats finalize)
- Trade Value (Wednesday)
- Waiver Wire (Tuesday night/Wednesday morning)
- Opponent Analyzer (Wednesday)
- Beat Reporter (daily)

On-demand:
- Trade Analyzer (user submits trade)
- Trade Proposal (user requests suggestions)
- Lineup Optimizer (Thursday, after injury reports + lines set)

---

## Database Schema

### `players` (master record)
```sql
CREATE TABLE players (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    yahoo_player_id VARCHAR(50) UNIQUE,
    name VARCHAR(100) NOT NULL,
    team_abbr VARCHAR(5),
    position VARCHAR(5),
    age INTEGER,
    contract_year BOOLEAN DEFAULT false,

    -- Valuations
    tier INTEGER,
    baseline_value DECIMAL(5,2),
    ceiling_value DECIMAL(5,2),
    floor_value DECIMAL(5,2),
    risk_adjusted_value DECIMAL(5,2),

    -- Market value
    market_value DECIMAL(5,2),
    market_value_fantasypros DECIMAL(5,2),
    market_value_sleeper DECIMAL(5,2),
    market_value_confidence VARCHAR(20),
    market_value_updated_at TIMESTAMP,

    -- Bid strategy
    value_gap DECIMAL(5,2),
    value_gap_signal VARCHAR(30),
    recommended_bid_ceiling DECIMAL(5,2),
    let_go_threshold DECIMAL(5,2),
    elite_anchor_weight DECIMAL(3,2),

    -- Situation
    situation_score VARCHAR(20),
    positional_scarcity_modifier DECIMAL(3,2),
    breakout_flag BOOLEAN DEFAULT false,
    notes TEXT,

    -- Metadata
    last_pipeline_run TIMESTAMP,
    data_confidence VARCHAR(20),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### `team_systems`
One record per NFL team per season. See Agent 1 output schema in AGENTS.md.

### `player_profiles`
One record per player per season. See Agent 3 output schema in AGENTS.md.

### `player_injury_profiles`
One record per player, updated across seasons.

### `player_schedules`
One record per player per season.
`playoff_window_grade` is a first-class column, not stored in notes.

### `player_dependencies`
Multiple records per player (one per flag).
See Agent 2 output schema in AGENTS.md.

### `beat_reporter_signals`
```sql
CREATE TABLE beat_reporter_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id UUID REFERENCES players(id),
    signal_type VARCHAR(50),
    source VARCHAR(100),
    raw_text TEXT,
    confidence VARCHAR(20),
    flagged_at TIMESTAMP DEFAULT NOW()
);
```

### `season_roster`
Populated after draft completes. Extends draft bible with weekly tracking.
```sql
CREATE TABLE season_roster (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id UUID REFERENCES players(id),
    yahoo_team_id VARCHAR(50),
    acquisition_price DECIMAL(5,2),
    acquisition_week INTEGER DEFAULT 0,
    weekly_stats JSONB DEFAULT '[]',
    weekly_snap_counts JSONB DEFAULT '[]',
    weekly_target_share JSONB DEFAULT '[]',
    current_trade_value DECIMAL(5,2),
    value_trend VARCHAR(20),
    sell_high_flag BOOLEAN DEFAULT false,
    buy_low_flag BOOLEAN DEFAULT false,
    injury_concern_flag BOOLEAN DEFAULT false,
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### `api_usage_log`
Every API call logged here. Required — see COST_RULES.md.
```sql
CREATE TABLE api_usage_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name VARCHAR(50),
    model VARCHAR(50),
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd DECIMAL(8,6),
    cache_hit BOOLEAN DEFAULT false,
    entity_id VARCHAR(100),
    called_at TIMESTAMP DEFAULT NOW()
);
```

### `agent_cache`
Input hash + output JSON cache for every agent call.
```sql
CREATE TABLE agent_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name VARCHAR(50) NOT NULL,
    entity_id VARCHAR(100) NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    output_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(agent_name, entity_id, input_hash)
);
```

---

## Two-Value Auction System

Every player carries two distinct value fields:

**Market value** — what the room expects to pay.
Source: FantasyPros consensus, Sleeper implied ADP, Underdog implied ADP.
Purpose: predicts room behavior, not player worth.

**System value** — what the research pipeline says the player is worth.
This is the number we believe.

The gap between them is the edge.

### Bid ceiling calculation

Risk is applied as a discount to market_value **before** blending, not as a
multiplier on the final ceiling. This prevents elite injured players from
becoming undraftable.

```
risk_adjusted_market = market_value × (1 - RISK_MARKET_DISCOUNT[risk_level])
```

Risk market discounts: low=0%, moderate=8%, high=15%, volatile=22%

Tier 1 (elite, positional scarcity applies):
```
blend = (system_value × (1 - anchor_weight)) + (risk_adjusted_market × anchor_weight)
bid_ceiling = blend × positional_scarcity_modifier
```

Tier 2-3:
```
bid_ceiling = (system_value × 0.85) + (risk_adjusted_market × 0.15)
```

Tier 4-5:
```
bid_ceiling = system_value
```

Let-go threshold (risk-adjusted walk-away price):
```
let_go = bid_ceiling × LET_GO_MULTIPLIER[risk_level]
```
Let-go multipliers: low=1.20×, moderate=1.15×, high=1.10×, volatile=1.05×

Anchor weights: T1=0.80, T2=0.40, T3=0.15, T4-5=0.00
Scarcity modifiers: T1 RB=1.35, T1 WR=1.20, T1 QB=1.10, T2+=1.00

---

## Opponent Modeling

Live draft agent maintains per-opponent profiles:
- Positional strength scores (updated after every pick)
- Remaining budget
- Threat score (0-100)
- Apparent draft strategy
- Combo threat flags

**Block flag logic:**
```
block_value = what player is worth to that opponent given their roster
personal_value = what player is worth to you

Fire block flag when:
  block_value > personal_value
  AND budget allows (won't drop you below minimum completion threshold)
  AND opponent has sufficient budget to be a threat
```

Suppress block flag when opponent has under $15 remaining — they can't afford danger.

**Nomination strategy:**
When it's your turn to nominate, recommend players with HIGH market value
that you do NOT want. Forces opponents to spend, drains their budgets.

---

## Environment Variables Required

Ask user before running any code that needs these:

```
ANTHROPIC_API_KEY=
YAHOO_CLIENT_ID=
YAHOO_CLIENT_SECRET=
YAHOO_REDIRECT_URI=http://localhost:8000/auth/yahoo/callback
YAHOO_LEAGUE_ID=
YAHOO_REFRESH_TOKEN=
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/fantasy_football
SECRET_KEY=
ENVIRONMENT=development
```
