# Fantasy Football AI Platform — Claude Code Entry Point

This file is read automatically at the start of every session.
Read it fully before writing any code.

---

## Development Workflow (enforced by CI)

All changes must go through PRs. Direct pushes to `main` and `develop` are
blocked by a branch-protection ruleset that requires the `backend`, `frontend`,
and `extension` CI checks to pass (and the branch to be up to date).

```
git checkout develop
git pull origin develop
git checkout -b feature/your-change
# ... make changes, commit ...
git push origin feature/your-change
gh pr create --base develop --fill
# Wait for CI green, then:
gh pr merge --squash --delete-branch

# To release develop -> production (main):
gh pr create --base main --head develop --title "Release"
gh pr merge --squash
```

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
| App design and UI | `docs/APP_DESIGN.md` |
| Data sources | `backend/integrations/sleeper.py` + `backend/integrations/nfl_data.py` |

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

CURRENT_SEASON   = get_current_season()      # e.g. 2026 in May 2026
ANALYSIS_SEASONS = get_analysis_seasons(3)   # e.g. [2023, 2024, 2025] — last 3 completed
ANALYSIS_YEAR    = get_analysis_year()       # e.g. 2026 — season we're drafting FOR
```

**Season calendar logic (cutoff = March):**
- January/February → current season = prior year (playoffs still in progress)
- March onward → current season = this calendar year (new league year begins)
- `get_analysis_seasons(3)` always returns 3 **completed** seasons, never the current year
- `get_analysis_year()` = `get_current_season()` — the season being drafted for
- Backtest default = `get_current_season() - 1` — most recently completed season

If you see `CURRENT_SEASON = 2024` or `for season in [2022, 2023, 2024]` anywhere in the codebase, that is a bug. Fix it.

---

## Architecture Rules (Full Detail in docs/rules/PATTERNS.md)

1. **One API call per team** — pre-aggregate all data in Python first, then call the model once
2. **No iterative tool-use loops** in pre-draft pipeline agents — `run_agent()` is only for live draft
3. **No polling** anywhere in the live draft event chain — event-driven only
4. **All agents go through BaseAgent** — never call `client.messages.create()` directly in agent files
5. **Batch by team, never by player** — never loop over players calling the API inside the loop
6. **All data flows through NflDataWarehouse** — agents never fetch data independently.
   Built once at pipeline start, passed to every agent.
   `grep _data_cache backend/agents/` must return zero results.
7. **Player identity uses ID-first matching** — always match by
   `sleeper_id` → `sportradar_id` → `gsis_id` → full name + position.
   Never match by last name alone. Never cross positions.
8. **Sleeper is the primary data source** for player identity, rosters, depth charts,
   injuries, and season stats. nfl_data_py kept only for schedules, PBP, and NGS.

---

## Data Sources

### Sleeper API — PRIMARY (`backend/integrations/sleeper.py`)
Free public API, no auth required, updated daily. Always current.

| Data | Function | Replaces |
|------|----------|---------|
| Current rosters + team assignments | `fetch_sleeper_players()` | `fetch_rosters()` |
| Season stats (pts_ppr, gp, rec, rush) | `get_sleeper_seasonal_stats(season)` | `get_seasonal_stats()` |
| Depth charts (depth_chart_order) | `get_sleeper_depth_charts()` | `fetch_depth_charts()` |
| Injury status | `get_sleeper_injuries()` | `fetch_injuries()` |

Key facts:
- 3,936 active skill position players (includes Inactive/IR)
- `sportradar_id` at 98%+ coverage — primary cross-source ID
- `depth_chart_order=1` reliably identifies starters
- Correctly shows Rodgers as FA, Geno Smith at NYJ depth=1
- Cache TTL: 24h for current data, permanent for historical seasons

### nfl_data_py — SECONDARY (`backend/integrations/nfl_data.py`)
Kept only for data Sleeper doesn't provide:

| Data | Function | Why kept |
|------|----------|---------|
| NFL schedules | `fetch_schedules(season)` | No Sleeper equivalent |
| Oline sack rates | `compute_team_oline_stats(season)` | Needs PBP pass_attempt/sack |
| NGS metrics | `fetch_ngs_data(stat_type, season)` | CPOE, air yards, time-to-throw |

**CRITICAL — never call `import_pbp_data()` with `columns=` kwarg.**
Triggers `KeyError: 'game_id'` for 2025 data due to nflverse schema change.
Load full PBP, then slice the columns you need afterward.

### Known Data Gaps
- `player_stats_{year}.parquet` on nflverse publishes 2-3 months after season ends.
  Sleeper fills this gap natively — 2025 stats available immediately.
- Full pipeline refresh should run in late July when training camp data publishes.
- nfl_data_py depth chart feed has stale entries — use Sleeper depth charts only.

---

## NflDataWarehouse — Single Source of Truth

```python
from backend.integrations.nfl_data import NflDataWarehouse

# Built ONCE at pipeline start — never rebuilt per agent
warehouse = NflDataWarehouse.build()

# Accessors — never raises, returns empty DataFrame on miss
warehouse.get_seasonal_stats(season)        # Sleeper stats
warehouse.get_target_share(season)          # nfl_data_py PBP
warehouse.get_qb_stats(season)              # Sleeper (filtered to passers)
warehouse.get_oline_stats(season)           # nfl_data_py PBP
warehouse.get_def_grades(season)            # nfl_data_py weekly
warehouse.get_injuries(season)              # Sleeper
warehouse.get_starter(team, position)       # depth_chart_order=1
warehouse.get_player_depth_rank(gsis_id)    # pos_rank from Sleeper
warehouse.get_team_depth_context(team)      # full depth at all positions
warehouse.rosters                           # Sleeper current players
warehouse.depth_charts                      # Sleeper depth charts
warehouse.schedule                          # nfl_data_py schedules
```

---

## Player Identity — IDs on players Table

Three ID columns, all indexed:
- `sleeper_id` — Sleeper's own ID, 100% for active players
- `sportradar_id` — 98%+ coverage, most reliable cross-source ID
- `gsis_id` — 29% coverage from Sleeper (supplemented from nfl_data_py)

Stat lookup priority in `_get_player_season_stats()`:
1. `sleeper_id` exact match (best)
2. `sportradar_id` exact match
3. `gsis_id` exact match
4. Full name + position match (Sleeper names are reliable)
5. Return `{}` — never return wrong-player stats

---

## Pipeline Dependency Order (CRITICAL)

Always run in this order — agents depend on upstream outputs:

```
1. sync_rosters      ← Sleeper sync, always first
2. team_systems      ← no deps, runs first
3. roster_changes    ← needs team_systems
4. injury_risk       ← no deps on other agents
5. schedule          ← no deps on other agents
6. beat_reporter     ← no deps on other agents
7. player_profiles   ← runs LAST, synthesizes all above
8. valuation         ← needs profiles
9. valuation_agent   ← needs valuation pass
```

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
│   ├── ARCHITECTURE.md
│   ├── SCHEMA.md
│   ├── AGENTS.md
│   ├── LIVE_DRAFT.md
│   ├── INSEASON.md
│   ├── rules/
│   │   ├── COST_RULES.md        # API cost efficiency — mandatory
│   │   ├── GIT_RULES.md         # Testing and commit workflow — mandatory
│   │   └── PATTERNS.md          # Code patterns with examples — mandatory
│   └── stages/
│       └── stage-XX-name.md     # One file per build stage
├── backend/
│   ├── utils/
│   │   └── seasons.py           # Dynamic season year calculations — always use this
│   ├── agents/
│   ├── engines/
│   ├── integrations/
│   │   ├── sleeper.py           # PRIMARY — players, stats, depth, injuries
│   │   ├── nfl_data.py          # NflDataWarehouse + schedules/PBP/NGS only
│   │   ├── nfl_comp_builder.py  # Historical rookie comp table
│   │   ├── fantasypros.py       # ADP/market values
│   │   ├── overthecap.py        # Contract data
│   │   ├── yahoo_api.py         # Yahoo Fantasy OAuth + league data
│   │   └── yahoo_playwright.py  # Yahoo draft room automation
│   ├── models/
│   ├── routers/
│   └── websocket/
├── frontend/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── scripts/
    ├── run_predraft_pipeline.py  # Main pipeline runner
    ├── sync_rosters.py           # Sleeper-based player sync (NOT nfl_data_py)
    ├── compute_valuations.py     # Valuation pass
    ├── backtest_accuracy.py      # Operator validation — NOT user-facing
    └── seed_nfl_data.py          # Initial data seeding
```

---

## Current Project Status

1228 backend tests. 33 frontend tests. 12 extension tests.

- [x] Stage 1: Foundation
- [x] Stage 2: Data ingestion
- [x] Stage 3: Team Systems agent
  - Haiku, 500 tokens, 19 tests
  - All 32 teams, concurrency=4, dynamic seasons
  - QB1 identification via Sleeper depth_chart_order=1
  - Falls back to passing_yards if no depth chart entry
- [x] Stage 4: Roster Changes agent
  - Sonnet, 4000 tokens, 66 tests
  - All 6 dependency flag types, McConkey/Allen canonical test passing
  - QB Trust Model, draft pick comp evaluation
  - Depth chart rank filtering: pos_rank=3+ never generates flags against starters
  - _write_flags() deletes by player_id only (not player_id+season_year) — no duplicates
- [x] Stage 5: Player Profiles agent
  - Haiku/Sonnet two-pass, 4000 tokens, 130+ tests
  - Smart cache invalidation: profile_needs_refresh() with prompt version system
  - 682 profiles in current pipeline run (Sleeper data)
  - Rookie profiling via nfl_comp_builder.py with college comps
  - RB role thresholds: workhorse/featured_back/committee/pass_catching/
    early_down/backup/depth
  - committee_back ONLY when no back has 50%+ of carries (true timeshare)
  - Weighted baseline: 50% most recent, 30% prior, 20% two years ago
  - Injury-shortened seasons (< 10 games) excluded from baseline
  - ID-first stat matching: sleeper_id → sportradar_id → gsis_id → name+position
  - Position verification enforced at every fallback level
  - Fringe players with no stats in any analysis season → depth profile (not garbage)
  - _build_depth_profile() positional_scarcity_tier = "deep" (String, NOT integer 5)
  - JJ McCarthy: is_rookie=False, nfl_seasons_played=1 (IR year-1)
  - exc_info=True on all run_for_team() exception catches (surfaces type errors)
- [x] Stage 6: Injury Risk agent
  - Haiku, 1000 tokens, 67 tests
  - 6 injury classifications, 6 pattern flags (all auto-detected in Python)
  - Age risk multiplier, risk modifiers (low/moderate/high/volatile)
- [x] Stage 7: Schedule agent
  - Haiku, 1500 tokens, 61 tests
  - 3 schedule windows, defensive grades from weekly PPR, bye_in_playoff_window
  - Defensive grades from Sleeper-sourced weekly data via warehouse
- [x] Stage 8: Beat Reporter agent
  - Haiku, 300 tokens, 48 tests
  - feedparser RSS, APScheduler 7am daily, WebSocket broadcast
- [x] Stage 9: Valuation pass
  - Pure Python (zero AI calls), 86 tests
  - PAR method, 5-tier, anchor-weighted bid ceilings
  - value_gap_signal uses ai_bid_ceiling not baseline_value
  - Signal: value_assessment + pay_up_flag (not purely math gap)
  - Cheap player rule: price <= $8 never generates avoid
  - Small gap rule: -8 to 0 downgraded to neutral
- [x] Stage 10: Yahoo API integration
  - OAuth, all API functions, multi-year league history, auction engine
  - All credentials in .env
- [x] Stage 11: Playwright draft bridge
  - WS interception + MutationObserver fallback + health check
  - Synthetic WS frames (replace with real frames ~August)
  - 35 tests, no-polling AST verification passing
- [x] Stage 11.5: Backtest & Validation (operator tool — NOT user-facing)
  - Default season: get_current_season() - 1 (never current year — no results yet)
  - Latest results (2025 season, post QB stats fix):
    88.7% signal accuracy, 94% buy accuracy, 42.8 MAE, 0.779 correlation
  - RB correlation 0.940 (excellent), avoid accuracy 71% (up from 38%)
  - CMC correctly appears as VALUE: $50 paid / $72 ceiling / 415 actual PPR
- [x] Stage 12: Live draft agent
  - DraftStateManager, DependencyResolver, OpponentThreatAnalyzer, LiveDraftEngine
  - Historical tendencies wired into threat analysis
  - Sonnet recommendations <2s, 400 tokens
  - 15 tests passing
- [x] Stage 13a: Pre-draft UI — COMPLETE
  - React 19 + Vite + Tailwind 4 + Zustand 5
  - 7 pages, 15 components, 3 stores, 9 API modules
  - Dark theme, responsive sidebar, WebSocket live news
- [x] Stage 13b: Draft Room UI — COMPLETE
  - 4-zone full-screen layout
  - RecommendationPanel, NominationPanel, MyRoster, OpponentTracker
  - WebSocket auto-reconnect, color-coded recommendations
  - 10 frontend + 3 backend tests
- [x] NflDataWarehouse refactor (cross-cutting architectural change)
  - Single source of truth for all pipeline data
  - All 5 agents receive warehouse — no independent data fetching
  - `grep _data_cache backend/agents/` = 0 results
  - Built once in run_predraft_pipeline.py, passed to all agents
  - Warehouse summary logged at pipeline start showing per-season player counts
- [x] Sleeper integration (feature/sleeper-integration → merge to develop)
  - `backend/integrations/sleeper.py` — new file, 16 tests
  - `sportradar_id` + `sleeper_id` columns on players table (Alembic migration)
  - `sync_rosters.py` rewritten: Sleeper-based sync replaces nfl_data_py roster sync
  - Warehouse updated: seasonal_stats, rosters, depth_charts, injuries from Sleeper
  - nfl_data_py retained only for schedules, PBP oline, NGS
  - Verified: BUF QB1=Josh Allen, NYJ QB1=Geno Smith, Rodgers=FA
  - CMC: 416.6 PPR from Sleeper, correctly profiled
  - J.Taylor (280 PPR, IND) correctly separated from J.J. Taylor (12 PPR, FA)
- [x] seasons.py fixes
  - get_current_season(): cutoff changed from month>=6 to month>=3
  - get_analysis_seasons(): returns [2023,2024,2025] in May 2026
  - get_analysis_year(): returns get_current_season() (not +1)
  - Backtest default: get_current_season() - 1
- [ ] Stage 14: Season roster store
- [ ] Stage 15–16: Roster monitor + opponent analyzer
- [ ] Stage 17–19: Trade value + trade analyzer + trade proposals
- [ ] Stage 20: Lineup optimizer
- [ ] Stage 21: Waiver wire agent
- [~] Stage 22: Pipeline admin UI — MOSTLY COMPLETE (8/10 spec items)
  - Missing: agent-specific freshness thresholds (all use 7d uniform)
  - Missing: GET /admin/cost-report/weekly endpoint
- [ ] Stage 23: Deployment + testing
- [ ] Stage 24: Gameday Monitoring
- [x] Stage 25: SaaS Foundation
  LeagueConfig, users table, credit system, feature gating, enterprise architecture
  SecurityHeaders + RequestLogging middleware, /account/* endpoints, 868 tests
- [~] Stage 26: User Auth — Clerk JWT verification, webhook lifecycle, protected routes, account dashboard
  Clerk auth, 3 tiers (intro $5/standard $9/pro $18), Stripe billing
  Credits: intro=25 signup, standard=75 signup+20/mo, pro=200 signup+50/mo
  Live draft = tier entitlement (not credit cost)
  9 auth tests, user_id scoped preferences, real email from Clerk API
  - Stripe not implemented
- [x] Stage 27: Landing Page — DraftMind marketing site
  Public landing at /, pricing at /pricing, 9 components, dark theme
  Hero, social proof, how-it-works, validation stats, feature comparison,
  3-tier pricing table, FAQ accordion, footer CTA, SEO meta tags
- [x] Stage 28: League Sync — Yahoo/ESPN/Sleeper multi-user
  Fernet token encryption, PlatformCredential model, LeaguePlatformAPI abstraction
  Yahoo multi-user OAuth (state=user_id CSRF)
  ESPN cookie extraction via browser extension
    (replaces bookmarklet — espn_s2 + SWID read from document.cookie,
    sent to backend automatically when user visits ESPN)
  Sleeper public API, LeagueSyncService (4yr history)
  League setup wizard
  Browser extension handles league connection for ESPN and draft room
    relay for all platforms (Yahoo/ESPN/Sleeper) — see extension/ directory
  56 new tests, 955 total
  PENDING — extension not yet built:
    Extension scaffold, WS interceptor, content scripts
    (yahoo_draft, espn_draft, sleeper_draft, espn_auth),
    popup UI, draft_token endpoint on backend,
    POST /draft/event relay endpoint
- [x] Yahoo draft room DOM poller
  yahoo_draft.js: 300ms #draft poller +
  console.error hook for own B/N events.
  Pure parse logic in yahoo_draft_parse.mjs.
  Popup shows draft active indicator.
  Tested against live Yahoo mock draft
  June 2026 — DOM structure confirmed.
- [x] Live draft engine wired to extension
  POST /draft/event nomination events →
  fuzzy name resolution (find_by_name_fuzzy
  reusing _norm_name) → LiveDraftEngine
  .on_nomination() → Sonnet recommendation
  broadcast. draft_pick → on_pick_confirmed.
  engine guarded on /draft/start.
- [x] Draft room UI handlers
  NominationPanel: player name, current bid,
  clock (red <10s), team budgets with threat
  flags. Handles nomination/bid_update/clock/
  teams_update/recommendation events.
- [ ] Stage 29: Snake Draft — see docs/stages/stage-29-snake-draft.md
  SnakeValuationEngine, VOE metric, SnakeDraftAgent
- [ ] Stage 30: Half PPR — see docs/stages/stage-30-half-ppr.md
  Half PPR scoring, replacement level adjustments

---

## Current Pipeline State

Pipeline last run: June 14, 2026
Prompt version: v6 (availability model)
Players valued: 580
Visible on draftboard: 339
Profiles: 775
Availability model: 3-year games-based
QB availability: active (Burrow/Murray/Daniels correctly flagged concern)
Forbidden injury language: enforced (v6)

---

## Backtest Results (June 2026 — final config)
## 3-year availability + QB extension + dedup fix

| Metric | Value | Notes |
|--------|-------|-------|
| Projection MAE | 34.3 PPR | Best ever (was 42.8) |
| Correlation | 0.849 | Strong |
| Overall bias | +10.3 | Best ever |
| Within 20% | 62% | Best ever |
| Signal accuracy | 81.0% | Strong |
| Buy accuracy | 97% | Excellent |
| Avoid accuracy | 55% | Improving |
| QB MAE | 49.7 | Was 71.1 (-30%) |
| QB bias | +32.0 | Was +64.4 (-50%) |
| Tier monotonic | Yes | ✓ |
| RB correlation | 0.903 | Outstanding |

Key improvements vs original baseline:
  MAE: 42.8 → 34.3 (-20%)
  QB MAE: 71.1 → 49.7 (-30%)
  QB bias: +64.4 → +32.0 (-50%)
  Buy accuracy: 94% → 97%

---

## Backtest Results (2025 Season — post QB stats fix)

| Metric | Value | Grade |
|--------|-------|-------|
| Signal accuracy | 88.7% | EXCELLENT |
| Projection MAE | 42.8 PPR | Good (30-50) |
| Correlation | 0.779 | Strong |
| Buy accuracy | 94% (48 players) | Excellent |
| Avoid accuracy | 71% (14 players) | Strong |
| Tier monotonic | T1(309) > T2(198) > T3(180) > T4(108) | Yes |

Key validated calls: CMC VALUE at $50→415 PPR ✓, JSN neutral→360 PPR ✓,
Kyle Pitts buy at $3→211 PPR ✓, Barkley avoid→230 PPR at $61 ✓,
Etienne buy at $3→254 PPR ✓, Olave buy at $9→268 PPR ✓

By position: RB r=0.940 (excellent), WR r=0.790, TE r=0.715, QB r=0.376
QB MAE=77.2, bias=+64.4 — injury-driven (Daniels, Burrow, Purdy missed games).
Lamar Jackson proj=368 vs actual=213 is the main non-injury QB miss.

---

## Known Issues / Backlog

### Extension
- Yahoo passive sync removed — Yahoo CSP
  blocks content script injection in both
  Chrome and Firefox. window.__draftmind__
  detection still works for LeagueSetup.
- my_nomination/my_bid console.error events
  relayed to UI but not yet folded into
  engine DraftStateManager budget/roster
  state. Auto-roster updates and scraped-
  budget reconciliation are future work.
- DOM selectors (#draft, position regex,
  budget line format) confirmed against
  June 2026 mock draft. Re-verify against
  real August draft room — Yahoo may change
  their DOM between now and then.
- Extension not yet published to Chrome
  Web Store or Firefox Add-ons. Sideload
  only (Load unpacked / Temporary Add-on).

### Pipeline / Accuracy
- CMC #1 projection miss (238 vs 415 actual)
  — one-off injury over-penalization. 2024
  was 4 games (injury), 2025 was 17 (bounce-
  back). 3-year avg correctly flags concern
  but the miss is large. Future: recency-
  weighted hybrid (concern requires recent
  season AND career pattern).
- 5-year availability window tested and
  rejected — dilutes recent signal, Burrow
  softened from concern to monitor. 3-year
  is the better predictor. Do not revisit
  unless recency-weighted hybrid proposed.
- Prospective validation deferred — requires
  pipeline re-run capped at 2024. Post-
  August draft validation planned using
  real 2026 actuals.
- QB projections still sensitive to in-season
  injury — no model can predict this.
  Lamar Jackson historical bias from injury-
  shortened seasons remains.
- FULL_SEASON_ABSENCE detection implemented
  via games-based availability model.
- BACKLOG: valuation_agent cache doesn't
  version on prompt changes — prompt edits
  require manual cache clear to take effect.
  Add prompt_version to cache key hash like
  player_profiles already does.

### SaaS / Auth
- Clerk running in dev mode (pk_test_) in
  production. Custom domain required for
  production Clerk instance. Deferred until
  domain purchased. See backlog item below.
- Production /api prefix mismatch — frontend
  calls /api/* but FastAPI serves /* in
  production. Vite proxy handles in dev.
  Fix requires custom domain + nginx rewrite.
  Both items must be done together.
- Yahoo OAuth multi-user — buddy confirmed
  his own leagues loaded (not Stephen's),
  so OAuth is working for other users.
  Full multi-user load test still pending.

### Data
- 3,880 stale player rows in DB (retired/
  irrelevant players). Display filter hides
  them (visible count: 339). Physical deletion
  deferred — FK-safe soft-delete needed since
  453 rows have child records across 9 tables.
- Ben Roethlisberger still in DB (hidden by
  display filter). sync_rosters now gates
  on recent activity so he won't be refreshed.

### Stages Remaining
- [ ] Stage 29: Snake draft support
- [ ] Stage 30: Half PPR support
- [ ] Browser extension Chrome/Firefox
      store submission
- [ ] my_nomination/my_bid → DraftStateManager
      integration (auto-roster + budget sync)
- [ ] teams_snapshot → engine state reconcile
- [ ] CI/CD: GitHub Actions workflow
      (highest leverage safety improvement)
- [ ] Soft-delete stale player rows

---

## SaaS Pricing (Stages 25-30)

```
Intro    $5/mo or $15/season:  25cr signup, 0cr/mo, 1 league, no live draft
Standard $9/mo or $29/season:  75cr signup, 20cr/mo, 2 leagues, live draft
Pro     $18/mo or $49/season: 200cr signup, 50cr/mo, unlimited, live draft + trade finder

Credit costs: trade=10cr, trade finder=20cr (Pro), waiver=8cr/week
Credit packs: $5=75cr, $10=175cr, $25=500cr
Credits carry over month to month (never reset)
No free tier, no battle passes, no stash tab monetization
```
