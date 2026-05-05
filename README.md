# Fantasy Football AI Platform

Full-season fantasy football management platform powered by AI agents. Built for Yahoo Fantasy auction drafts in a 12-team PPR league.

The system builds independent player valuations from raw data using chain-of-reasoning AI agents, controls the Yahoo draft room in real time via browser automation, and provides in-season trade analysis, lineup optimization, and waiver wire recommendations.

## Core Philosophy

**Never trust third-party projections.** Build valuations from raw data and causal reasoning chains.

The canonical failure case this system prevents: Keenan Allen signing with a team should automatically flag overlapping receivers' target share as capped. Traditional projection systems miss these cascading effects. This platform catches them by design.

---

## Architecture

The platform operates in three phases:

```
PRE-DRAFT PIPELINE          LIVE DRAFT              IN-SEASON
──────────────────          ──────────              ─────────
6 Research Agents           Live Draft Agent        Trade Analyzer
      │                     + Playwright Bridge     Trade Proposal Engine
  Draft Bible          →    + React Draft UI   →    Lineup Optimizer
(PostgreSQL)                + Opponent Modeling     Waiver Wire Agent
                                                    Roster Monitor
```

### Two-Value Auction System

Every player carries two distinct valuations:

- **System value** — what the research pipeline says the player is worth (the number we believe)
- **Market value** — what the room expects to pay (predicts opponent behavior)

The gap between them is the edge. Bid ceilings blend both values based on player tier, with elite players anchored more heavily to market value (to avoid being outbid) and late-round targets anchored to system value (to find underpriced players).

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Package manager | uv |
| AI model (reasoning) | Claude Sonnet (`claude-sonnet-4-6`) |
| AI model (extraction) | Claude Haiku (`claude-haiku-4-5-20251001`) |
| Database | PostgreSQL 16 + pgvector |
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Backend | FastAPI + WebSockets |
| Task scheduling | APScheduler |
| Yahoo draft control | Playwright |
| Frontend | React + Vite + Tailwind + Zustand |
| CI/CD | GitHub Actions |

---

## Pre-Draft Pipeline (6 Research Agents)

All agents batch by team (32 API calls max per agent), use hash-based caching to skip unchanged data, and pre-aggregate statistics in Python before calling the AI model. A full pipeline run costs under $1.50.

| # | Agent | Model | Purpose |
|---|-------|-------|---------|
| 1 | Team Systems | Haiku | Grade all 32 NFL offensive systems (OL, QB, OC scheme, personnel) |
| 2 | Roster Changes | Sonnet | Track transactions, reason through downstream effects (displacement/contingency flags) |
| 3 | Player Profiles | Haiku | Individual player profiles with role classification, efficiency metrics, age curves |
| 4 | Injury Risk | Haiku | Risk-adjust values based on injury history patterns and recurrence rates |
| 5 | Schedule | Haiku | Grade schedules across three windows (early, full, playoff weeks 14-17) |
| 6 | Beat Reporter | Haiku | Daily freshness layer — RSS feeds, practice reports, depth chart changes |

Agent 1 runs first (others inherit its output). Agents 2-6 can run in parallel afterward.

---

## Live Draft System

The Playwright bridge intercepts Yahoo's draft room WebSocket traffic and exposes clean Python events. Zero polling anywhere — fully event-driven.

```
Yahoo WS frames → Playwright interception → FastAPI WS → React UI
                                                              │
Yahoo ← Playwright page.evaluate() ← FastAPI endpoint ← User action
```

- Target round-trip latency: under 100ms
- Bridge failure: immediately emits `MANUAL_ACTION_REQUIRED` to UI (never crashes silently)
- Per-opponent modeling: tracks budgets, positional strength, threat scores, and apparent strategy
- Block flag logic: recommends defensive bids when an opponent's gain exceeds your personal value

---

## AI Assistant

A chat interface powered by Claude Sonnet that gives full access to the draft bible data. Ask questions about players, trades, draft strategy, and lineup decisions with real context from the platform's research agents.

- **Streaming responses** via Server-Sent Events for immediate feedback
- **Auto-detects players** mentioned in your question and injects their full profile (valuation, flags, injury, schedule)
- **Context toggles** for roster and opponent data inclusion
- **Accessible everywhere** via floating button (bottom-right corner, all pages)
- **Player detail integration** — "Ask about this player" button in every player profile panel
- **Conversation history** maintained during the session

The assistant references actual system values, bid ceilings, dependency flags, and schedule grades rather than speaking in generalities. It uses the two-value auction system to frame all bid/trade advice.

---

## In-Season Features

| Feature | Schedule | Purpose |
|---------|----------|---------|
| Roster Monitor | Wednesday weekly | Track snap counts, target shares, usage trends, set sell-high/buy-low flags |
| Trade Value | Wednesday weekly | Calculate current trade values for all players, identify asymmetry |
| Trade Proposal Engine | On-demand + weekly | Generate proactive trade suggestions calibrated to opponent psychology |
| Trade Analyzer | On-demand | Analyze submitted trades with fairness, timing, acceptance probability, counter-proposals |
| Lineup Optimizer | Thursday weekly | Set optimal lineup using matchup grades, Vegas implied totals, injury reports, weather |
| Waiver Wire | Tuesday/Wednesday | Identify pickups from snap count spikes, depth chart promotions, usage changes |
| Opponent Analyzer | Wednesday weekly | Maintain per-opponent profiles including management style and vulnerabilities |

---

## Project Status

### Completed

- [x] **Stage 1: Foundation** — Repo structure, ORM models, FastAPI app, Alembic migrations
- [x] **Stage 2: Data Ingestion** — nfl_data_py wrapper, OTC scraper, FantasyPros Playwright scraper, seed script
- [x] **Stage 3: Team Systems Agent** — Haiku, 500 tokens, 32 calls, dynamic season years
- [x] **Stage 4: Roster Changes Agent** — Sonnet, 4000 tokens, causal reasoning, displacement/contingency flags
- [x] **Stage 5: Player Profiles Agent** — Haiku, 4000 tokens, role classification, 463 player profiles
- [x] **Stage 6: Injury Risk Agent** — Haiku, 1000 tokens, pattern flags pre-computed in Python
- [x] **Stage 7: Schedule Agent** — Haiku, 1500 tokens, defensive grades inverted from weekly PPR, bye-in-playoff flag
- [x] **Stage 8: Beat Reporter Agent** — Haiku, 300 tokens, feedparser RSS, APScheduler daily at 7am, dedup
- [x] **Stage 9: Valuation Pass** — Pure Python, PAR method, two-value bid ceiling, positional scarcity modifiers

### In Progress

- [~] **Stage 10: Yahoo API Integration** — OAuth flow implemented, `get_players()` works year-round. League endpoints (`get_league`, `get_teams`, `get_rosters`, `get_draft_results`) implemented but untestable until league is active (~August)
- [~] **Stage 11: Playwright Draft Bridge** — Foundation complete. WebSocket interception, MutationObserver fallback, health check, `MANUAL_ACTION_REQUIRED` failure handling, WebSocketManager singleton, draft router with WS endpoint. Synthetic test fixtures in place; real Yahoo WS frames needed in August

### Remaining

- [ ] **Stage 12:** Live Draft Agent (Sonnet, real-time decision engine with opponent modeling)
- [ ] **Stage 13:** Draft UI (React — nomination panel, recommendation cards, bid controls, draft board, opponent tracker)
- [ ] **Stage 14:** Season Roster Store (post-draft sync via Yahoo API)
- [ ] **Stages 15-21:** In-season features (Roster Monitor, Trade Analyzer, Trade Proposals, Lineup Optimizer, Waiver Wire, Opponent Analyzer)
- [ ] **Stage 22:** Pipeline Admin UI (status dashboard, manual triggers, cost reports)
- [ ] **Stage 23:** Deployment + testing (Railway, GitHub Actions CI/CD)

---

## Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 16 with pgvector extension
- Node.js (for frontend, when built)
- [uv](https://github.com/astral-sh/uv) package manager

### Environment Variables

```env
ANTHROPIC_API_KEY=
YAHOO_CLIENT_ID=
YAHOO_CLIENT_SECRET=
YAHOO_REDIRECT_URI=http://localhost:8000/auth/yahoo/callback
YAHOO_LEAGUE_ID=          # Set once league is created (~August)
YAHOO_REFRESH_TOKEN=      # Set after completing OAuth flow
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/fantasy_football
SECRET_KEY=
ENVIRONMENT=development
```

### Running

```bash
# Install dependencies
uv sync

# Run database migrations
alembic upgrade head

# Start the backend
uvicorn backend.main:app --reload

# Run the pre-draft pipeline (with cost estimate)
python scripts/run_predraft_pipeline.py --dry-run
python scripts/run_predraft_pipeline.py --agent all

# Run a single agent for one team
python scripts/run_predraft_pipeline.py --agent roster_changes --team LAC

# Run tests
pytest tests/unit/ -v
```

### Yahoo OAuth Flow

1. Navigate to `GET /auth/yahoo` in your browser
2. Authorize the app on Yahoo's consent screen
3. Copy the refresh token from the callback into `.env` as `YAHOO_REFRESH_TOKEN`
4. Run `POST /pipeline/sync-yahoo-players` to populate the player universe

---

## Cost Efficiency

The platform is designed to minimize API costs while maximizing analytical depth:

| Scope | Expected Cost |
|-------|--------------|
| Full pre-draft pipeline (all 6 agents) | ~$1.00 |
| Weekly in-season refresh | ~$0.20 |
| Full season (pipeline + weekly + draft day) | Under $20 |

Key cost controls: batch by team (never by player), hash-based caching skips unchanged data, explicit `max_tokens` on every call, Haiku for extraction and Sonnet only for reasoning tasks.

---

## Repository Structure

```
fantasy-football-ai/
├── CLAUDE.md                    # AI assistant instructions
├── README.md                    # This file
├── docs/
│   ├── ARCHITECTURE.md          # Full system architecture
│   ├── SCHEMA.md                # Database schema
│   ├── AGENTS.md                # Pre-draft agent specifications
│   ├── LIVE_DRAFT.md            # Yahoo integration + Playwright bridge
│   ├── INSEASON.md              # In-season features spec
│   ├── rules/
│   │   ├── COST_RULES.md        # API cost efficiency rules
│   │   ├── GIT_RULES.md         # Testing and commit workflow
│   │   ├── LEAGUE_RULES.md      # League settings and draft strategy
│   │   └── PATTERNS.md          # Code patterns and conventions
│   └── stages/                  # Per-stage build documents
├── backend/
│   ├── agents/                  # All AI agent implementations
│   ├── engines/                 # Valuation, live draft, trade analysis
│   ├── integrations/            # Yahoo API, Playwright bridge
│   ├── models/                  # SQLAlchemy ORM models
│   ├── routers/                 # FastAPI route handlers
│   ├── utils/                   # Shared utilities (seasons.py, etc.)
│   └── websocket/               # WebSocket manager
├── frontend/                    # React + Vite + Tailwind + Zustand
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── scripts/                     # Pipeline runners, seed scripts
└── alembic/                     # Database migrations
```
