# Fantasy Football AI Platform — Claude Code Entry Point

This file is read automatically at the start of every session.
Read it fully before writing any code.

---

## What This Project Is

Full-season fantasy football management platform powered by AI agents.
The user's league is on **Yahoo Fantasy**, **auction draft format**.

Three phases:
1. **Pre-draft pipeline** — 6 research agents build a structured "draft bible"
2. **Live draft** — Agent controls Yahoo draft room via Playwright, gives real-time recommendations
3. **In-season** — Trade analyzer, lineup optimizer, waiver wire agent

Core philosophy: **never trust third-party projections**. Build valuations from raw data and chain-of-reasoning. The canonical failure case this system exists to prevent: Keenan Allen signing with LAC should have automatically flagged Ladd McConkey's target share as capped. It didn't in 2024. It must in this system.

---

## Mandatory Reading Before Writing Any Code

| Task | Read first |
|------|-----------|
| Any agent | `docs/rules/COST_RULES.md` + `docs/rules/PATTERNS.md` |
| Any agent | `docs/AGENTS.md` for that agent's spec |
| Database work | `docs/SCHEMA.md` |
| Yahoo/Playwright | `docs/LIVE_DRAFT.md` |
| Testing/commits | `docs/rules/GIT_RULES.md` |
| In-season features | `docs/INSEASON.md` |
| Current stage | `docs/stages/stage-XX-name.md` |
| Bid ceilings, live draft, valuations, lineup optimizer | `docs/rules/LEAGUE_RULES.md` |
| App design and UI | docs/APP_DESIGN.md |

---

## Tech Stack (Quick Reference)

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Package manager | uv |
| AI model (reasoning) | `claude-sonnet-4-6` |
| AI model (extraction) | `claude-haiku-4-5-20251001` |
| Database | PostgreSQL 16 + pgvector |
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Backend | FastAPI + WebSockets |
| Task scheduling | APScheduler |
| Yahoo draft control | Playwright |
| Frontend | React + Vite + Tailwind + Zustand |
| Hosting | Railway |
| CI/CD | GitHub Actions |

---

## Model Selection — Non-Negotiable

**Haiku** (`claude-haiku-4-5-20251001`) for:
- Team Systems agent
- Player Profiles agent
- Injury Risk agent
- Schedule agent
- Beat Reporter agent
- Waiver Wire agent
- Any data extraction or formatting task

**Sonnet** (`claude-sonnet-4-6`) for:
- Roster Changes agent (chain-of-reasoning)
- Live Draft agent (real-time decisions)
- Trade Analyzer engine
- Trade Proposal engine
- Opponent Analyzer agent
- Any multi-step causal reasoning task

If you are unsure which to use: **default to Haiku**. Upgrade to Sonnet only if the task requires reasoning through cause-and-effect chains, not just retrieving and formatting data.

---

## Season Year Handling — Critical

**Never hardcode season years.** All agents must derive years dynamically from `backend/utils/seasons.py`:

```python
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

CURRENT_SEASON = get_current_season()        # e.g. 2025
ANALYSIS_SEASONS = get_analysis_seasons(3)   # e.g. [2023, 2024, 2025] — last 3 seasons
ANALYSIS_YEAR = get_analysis_year()          # e.g. 2026 — the draft we're preparing for
```

If you see `CURRENT_SEASON = 2024` or `for season in [2022, 2023, 2024]` anywhere in the codebase, that is a bug. Fix it.

---

## Architecture Rules (Full Detail in docs/rules/PATTERNS.md)

1. **One API call per team** — pre-aggregate all data in Python first, then call the model once
2. **No iterative tool-use loops** in pre-draft pipeline agents — `run_agent()` is only for live draft
3. **No polling** anywhere in the live draft event chain — event-driven only
4. **All agents go through BaseAgent** — never call `client.messages.create()` directly in agent files
5. **Batch by team, never by player** — never loop over players calling the API inside the loop

---

## When to Ask the User

Stop and ask the user before:
- Any step requiring account creation (Railway, Yahoo Developer, GitHub)
- Any OAuth flow that requires browser interaction
- Any step requiring API keys or credentials not already in `.env`
- Running the full pipeline for the first time (show dry-run cost estimate first)
- Any destructive database operation

---

## Repository Structure

```
fantasy-football-ai/
├── CLAUDE.md                    # This file — auto-read by Claude Code
├── docs/
│   ├── ARCHITECTURE.md          # Full system architecture
│   ├── SCHEMA.md                # Complete database schema
│   ├── AGENTS.md                # All 6 pre-draft agent specs
│   ├── LIVE_DRAFT.md            # Yahoo integration + Playwright bridge
│   ├── INSEASON.md              # In-season features spec
│   ├── rules/
│   │   ├── COST_RULES.md        # API cost efficiency — mandatory
│   │   ├── GIT_RULES.md         # Testing and commit workflow — mandatory
│   │   └── PATTERNS.md          # Code patterns with examples — mandatory
│   └── stages/
│       ├── stage-01-foundation.md
│       ├── stage-02-data-ingestion.md
│       ├── stage-03-team-systems.md
│       ├── stage-04-roster-changes.md
│       └── ... (one file per build stage)
├── backend/
│   ├── utils/
│   │   └── seasons.py           # Dynamic season year calculations — always use this
│   ├── agents/
│   ├── engines/
│   ├── integrations/
│   ├── models/
│   ├── routers/
│   └── websocket/
├── frontend/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── scripts/
```

---

## Current Project Status

Update this section as stages complete.
719 unit tests passing across 37 test files. 58 Python backend files, 43 JS/JSX frontend files.

- [x] Stage 1: Foundation
- [x] Stage 2: Data ingestion
- [x] Stage 3: Team Systems agent
  - Haiku, 500 tokens, 19 tests (12 spec + 7 bonus)
  - All 32 teams, concurrency=4, dynamic seasons
- [x] Stage 4: Roster Changes agent
  - Sonnet, 4000 tokens (spec was 2000 — needed for draft pick complexity), 66 tests
  - All 6 dependency flag types, McConkey/Allen canonical test passing
  - QB Trust Model (NFL + college), draft pick comp evaluation
- [x] Stage 5: Player Profiles agent
  - Haiku, 4000 tokens (spec was 1000 — needed for ~15 players/team), 130 tests
  - Smart cache invalidation: profile_needs_refresh() checks prompt version,
    team system changes, dependency flag changes, and staleness
  - 825 total profiles (498 veterans + 327 newly added)
  - Rookie profiling via nfl_comp_builder.py with college comps
  - Prompt version system for cache invalidation on prompt changes
  - Committee back classification fixed with explicit RB role thresholds
  - JJ McCarthy corrected from rookie to IR-year-1 player
  - Two-pass model: Haiku batch for stable veterans, Sonnet per-player
    for complex cases (rookies, QBs, aging, team changes, high injury risk)
- [x] Stage 6: Injury Risk agent
  - Haiku, 1000 tokens, 67 tests
  - 6 injury classifications, 6 pattern flags (all auto-detected in Python)
  - Age risk multiplier, risk modifiers (low/moderate/high/volatile)
- [x] Stage 7: Schedule agent
  - Haiku, 1500 tokens, 61 tests
  - 3 schedule windows, defensive grades from weekly PPR, bye_in_playoff_window
- [x] Stage 8: Beat Reporter agent
  - Haiku, 300 tokens, 48 tests
  - feedparser RSS (ESPN/Rotowire/NFL.com), APScheduler 7am daily
  - Dedup pre-load, WebSocket broadcast for live news
- [x] Stage 9: Valuation pass
  - Pure Python (zero AI calls), 86 tests
  - PAR method, 5-tier assignment, bid ceiling with anchor weights
  - Risk discount applied to market BEFORE blending (not on final ceiling)
  - value_gap_signal now uses ai_bid_ceiling not baseline_value
  - Signal derivation uses value_assessment + pay_up_flag (not purely math gap)
  - Cheap player rule: price <= $8 never generates avoid signal
  - Small gap rule: -8 to 0 range downgraded to neutral (auction noise)
- [x] Stage 10: Yahoo API integration
  - OAuth flow complete (GET /auth/yahoo, GET /auth/yahoo/callback)
  - All API functions: get_players, get_league, get_teams, get_rosters, get_draft_results
  - Multi-year league history: get_all_user_leagues, get_draft_results_for_league,
    get_player_details_batch, get_teams_in_league
  - League auction engine: CSV import, Yahoo sync, multi-year tendencies,
    manager style classification, market_value_league refresh
  - rematch_unmatched_auction_history() for post-sync re-matching
  - Pipeline endpoints: sync-yahoo-players, sync-league-settings,
    sync-league-history, import-league-auction, refresh-market-values,
    rematch-auction-history
  - Yahoo credentials (YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_LEAGUE_ID,
    YAHOO_REFRESH_TOKEN) all set in .env
- [x] Stage 11: Playwright draft bridge
  - YahooPlaywrightBridge: WS interception + MutationObserver fallback + health check
  - WebSocketManager singleton for React client push
  - Draft router: WS /ws/draft, POST /draft/bid, /draft/nominate, /draft/pass
  - Synthetic WS frame fixtures (replace with real frames ~August)
  - 35 tests, no-polling AST verification test passing
- [x] Stage 11.5: Backtest & Validation (operator tool)
  - scripts/backtest_accuracy.py — full accuracy report against actual season
  - backend/integrations/nfl_data.py — compute_seasonal_stats_from_pbp()
    fallback when nflverse parquet unavailable for current season
  - 2025 actual stats computed from play-by-play data
  - Verified: 81.5% overall signal accuracy (STRONG grade)
  - Buy signals: 95% accurate (41 players)
  - Avoid signals: 38% accurate (13 high-conviction calls)
  - Top opportunities: 13/15 delivered value (87%)
  - backtest_results_2025.csv generated for manual review
  - NOTE: Backtest is operator-only. Not a user-facing feature.
- [x] Stage 12: Live draft agent
  - DraftStateManager: pure Python state tracker (budget, rosters, spendable calc)
  - DependencyResolver: flag activation (McConkey/Allen displaced scenario)
  - OpponentThreatAnalyzer: combo detection, block values, nomination strategy,
    historical manager tendencies (positional bias from league_auction.py)
  - LiveDraftEngine: Sonnet-powered real-time recommendations in <2s,
    single messages.create() call (400 tokens), JSON-only output
  - Draft router: /start, /state, /frame, /recommendation, /end endpoints
  - Bridge event callbacks for engine integration
  - 15 tests, all passing (12 spec + 3 tendencies)
- [x] Stage 13a: Pre-draft UI — COMPLETE
  - React 19 + Vite + Tailwind 4 + Zustand 5 + React Query 5
  - 7 pages: Dashboard, DraftBoard, Players, Teams, TeamDetail, News, PipelineAdmin
  - 15 shared components: FlagBadge, PlayerCardCompact, PlayerCardExpanded,
    ValueComparisonBar, SystemGradeBadge, NewsFeedItem, PositionBadge,
    PlayerDetailPanel, FilterBar, SearchInput, Pagination, Sidebar, Layout,
    AssistantButton, AssistantPanel
  - 3 Zustand stores (ui, preferences, assistant)
  - 9 API client modules (players, teams, news, draftboard, preferences,
    admin, league, assistant, client)
  - getDisplaySignal() in lib/signals.js mirrors backend signal derivation
  - 4 frontend test files (FlagBadge, SystemGradeBadge, ValueComparisonBar, NewsFeedItem)
  - All pages fetch live data from backend (no mocks)
  - Dark theme, responsive sidebar, WebSocket live news updates
- [x] Stage 13b: Draft Room UI — COMPLETE
  - Full-screen 4-zone grid layout (no sidebar)
  - DraftRoom.jsx: RecommendationPanel, NominationPanel, MyRoster, AvailablePlayers
  - OpponentTracker collapsible sidebar with threat scores + combo alerts
  - DraftSetup overlay (team ID + optional draft room URL)
  - Zustand draft store + native WebSocket hook with auto-reconnect
  - Color-coded recommendations (buy/bid_to/block/pass), one-click bid, pass confirm
  - GET /draft/opponents endpoint for opponent budgets + threats
  - 10 API functions in api/draft.js, reuses existing /draftboard endpoint
  - 10 frontend tests + 3 backend tests, all passing
- [ ] Stage 14: Season roster store
- [ ] Stage 15–16: Roster monitor + opponent analyzer
- [ ] Stage 17–19: Trade value + trade analyzer + trade proposals
- [ ] Stage 20: Lineup optimizer
- [ ] Stage 21: Waiver wire agent
- [~] Stage 22: Pipeline admin UI — MOSTLY COMPLETE (8/10 spec items)
  - PipelineAdmin.jsx: agent status, run/dry-run buttons, cost report, backtest section
  - Backend: GET /admin/pipeline-status, POST /admin/pipeline/run,
    POST /admin/pipeline/dry-run, GET /admin/cost-report, GET /admin/backtest
  - Missing: agent-specific freshness thresholds (all use 7d, spec says
    team_systems=30d, beat_reporter=2d)
  - Missing: GET /admin/cost-report/weekly dedicated endpoint
- [ ] Stage 23: Deployment + testing
- [ ] Stage 24: Gameday Monitoring
- [ ] Stage 25: SaaS Foundation — see docs/stages/stage-25-saas-foundation.md
  LeagueConfig dataclass, DB split, credit system, row-level security
- [ ] Stage 26: User Auth — see docs/stages/stage-26-user-auth.md
  Clerk auth, 3 tiers (intro/standard/pro), Stripe billing
- [ ] Stage 27: Landing Page — see docs/stages/stage-27-landing-page.md
  Marketing site, validation stats, pricing table
- [ ] Stage 28: League Sync — see docs/stages/stage-28-league-sync.md
  Yahoo multi-user OAuth, Sleeper API, ESPN cookie API
- [ ] Stage 29: Snake Draft — see docs/stages/stage-29-snake-draft.md
  SnakeValuationEngine, VOE metric, SnakeDraftAgent
- [ ] Stage 30: Half PPR — see docs/stages/stage-30-half-ppr.md
  Half PPR scoring, replacement level adjustments
