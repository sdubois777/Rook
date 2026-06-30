# Rook — Fantasy Football AI Platform — Claude Code Entry Point

This file is read automatically at the start of every session.
Read it fully before writing any code. (Branding/repo are all **Rook**. The Railway
service hostname remains `fantasymanager-production.up.railway.app` — the extension's
hardcoded API base; it works, intentionally left as-is.)

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
```

**Releasing develop → main (reconcile-branch — IMPORTANT).** A direct
`develop → main` PR comes up `BEHIND` (main carries squash/merge release commits
develop lacks), and develop's branch protection **blocks `gh pr update-branch`**.
So releases use a reconcile branch whose tree equals develop's:

```
git fetch origin main develop
git checkout -B release-NN origin/main
git merge origin/develop -X theirs --no-edit       # develop is authoritative
git diff origin/develop release-NN --stat          # MUST be empty (tree == develop)
git push -u origin release-NN
gh pr create --base main --head release-NN --title "Release: ... ; develop authoritative"
# CI green → MERGE COMMIT (not squash), then verify:
git diff origin/main origin/develop --stat         # MUST be empty
```

- **Stephen drives every `release → main` manually** — never auto-release; a bug
  report or a "fix it" is NOT a release go-ahead.
- Backend changes do **nothing in prod until released** (Railway deploys from main).
  Extension changes take effect on local rebuild + reload — no release needed to
  *test*, but they're not live for users until shipped.
- Keep unrelated working-tree files out of commits. Known junk artifact:
  `frontend/public/android-chrome-192x192.png` keeps reappearing —
  `git checkout --` it, never commit. Also `docs/PROJECT_STATE.md` (untracked audit).

---

## What This Project Is

**Rook** (rookff.com) — a full-season fantasy football management SaaS powered by
AI agents. Multi-user, multi-platform: **Yahoo, ESPN, and Sleeper**, both **auction
and snake** draft formats (all four platform×format combinations live in production
as of June 2026).

Three phases:
1. **Pre-draft pipeline** — 6 research agents build a structured "draft bible"
2. **Live draft** — a **sideloaded browser extension** reads the draft room and
   relays events to the backend, which gives real-time AI recommendations. One
   poller per platform/format; all map onto a single backend event contract. (The
   old Playwright bridge is superseded — see the Live-Draft Extension section.)
3. **In-season** — Trade analyzer, lineup optimizer, waiver wire agent (not yet built)

Core philosophy: **never trust third-party projections**. Build valuations from raw data and chain-of-reasoning. The canonical failure case this system exists to prevent: Keenan Allen signing with LAC should have automatically flagged Ladd McConkey's target share as capped. It didn't in 2024. It must in this system.

Monetization: free-to-play-style **subscription tiers** (intro/standard/pro) gated by
`User.tier`; Stripe billing is **designed but not yet implemented** — see
`docs/stripe_billing_design.md`.

---

## Mandatory Reading Before Writing Any Code

| Task | Read first |
|------|-----------|
| Any agent | `docs/rules/COST_RULES.md` + `docs/rules/PATTERNS.md` |
| Any agent | `docs/AGENTS.md` for that agent's spec |
| Database work | `docs/SCHEMA.md` |
| Live-draft extension (any platform) | the "Live Draft — Browser Extension Architecture" section below |
| ESPN / Sleeper resolvers | `docs/espn_resolver_design.md` · `docs/sleeper_resolver_design.md` |
| Stripe / billing | `docs/stripe_billing_design.md` (decisions locked) |
| Trade agent / acceptability model | `docs/trade_agent_design.md` · `docs/trade_acceptability_design.md` (locked) |
| Trade value / lineup objective | `docs/trade_value_trajectory_design.md` · `docs/trade_lineup_value_design.md` (locked) |
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
| Live draft control | **Browser extension** (MV3, content scripts) — `extension/` |
| Auth | Clerk (production mode) |
| Billing | Stripe (designed, not yet implemented) |
| Frontend | React + Vite + Tailwind + Zustand |
| Hosting | Railway (backend deploys from `main`) |
| CI/CD | GitHub Actions (`backend` / `frontend` / `extension` checks, all green) |

> Playwright (`yahoo_playwright.py`) was the original draft bridge — **superseded**
> by the browser-extension pollers. Kept only for reference.

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
│   ├── routers/                # draft.py = live-draft relay; webhooks.py = Clerk (+Stripe future)
│   └── websocket/
├── extension/                  # MV3 browser extension — live-draft pollers (Yahoo/ESPN/Sleeper)
│   ├── src/content_scripts/    # *_draft.js pollers + *_resolve.mjs pure parse logic
│   ├── manifest.json
│   └── test/fixtures/{auction,espn,sleeper}/   # REAL captured DOM/frames
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

**~1401 backend tests · ~203 frontend tests · 113 extension tests (112 pass / 1
skip).** All three CI checks green. Last major work (June 2026): **ESPN + Sleeper
live-draft pollers shipped to prod; draft refresh-resilience hardened; Stripe billing
design locked (not implemented).**

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
- [x] Stage 27: Landing Page — Rook marketing site
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
- [~] Stage 29: Snake Draft — MOSTLY COMPLETE (June 2026)
  ADP data layer + UI toggle + engine path all shipped to main.
  - ADP columns on players: adp_fantasypros, adp_ai, adp_scoring
    (migration e4f5a6b7c8d9, applied to prod)
  - scripts/sync_adp.py — scrapes FantasyPros ADP (get_adp), matches by
    normalized name. Wired into run_predraft_pipeline before agent phases.
    FP table is now 4-col "Player Team (Bye)"; parser handles the combined cell.
  - adp_ai generated by valuation_agent (NOT player_profiles) alongside
    ai_bid_ceiling. ADP_POSITION_RANGES clamps: QB (25,170), RB (1,100),
    WR (1,120), TE (10,150), K (140,200), DEF (130,200). Prompt has a QB
    tier framework (elite 25-40 / strong 45-80 / standard 85-130 / backup 130+)
    to prevent QB clustering. adp_ai is MANDATORY in the prompt + first JSON
    field (Sonnet was dropping it when last).
  - /draftboard emits adp_ai/adp_fantasypros/adp_scoring.
  - LeagueContext (frontend/src/context) + sidebar LeagueSelector persist the
    selected league to localStorage; draft-room components (AvailablePlayers,
    RecommendationPanel, SuggestedTargets) toggle auction $ vs snake ADP on
    isSnake.
  - LiveDraftEngine.on_nomination branches on state.draft_type: snake →
    _on_nomination_snake (Sonnet, _SNAKE_SYSTEM_PROMPT, DRAFT/WAIT + ADP +
    roster-need). draft_type threads LeagueConfig → DraftStateManager →
    StartDraftRequest. /draft/start passes draft_type from the league.
  - Production data populated: adp_ai on all 645 valued players,
    adp_fantasypros on 335 (Allen 28, Lamar 32, Burrow 41, Mahomes 68).
  - LeagueContext UI toggle now wired on BOTH the draft-room AvailablePlayers
    AND the standalone /draftboard page (DraftBoard reads useLeague: snake ->
    AI ADP / FP ADP / Diff sorted by adp_ai asc; auction -> ceiling/market/gap).
    App.jsx collapsed to a single LeagueProvider (was duplicated across the
    full-screen and Layout branches). Note: DraftBoard is a bespoke page — it
    does NOT use the draft-room AvailablePlayers component.
  - Extension snake poller SHIPPED (June 2026): yahoo_snake_resolve.mjs reads
    Yahoo's React snake room (the room migrated onto the shared auction root).
    Non-destructive Board-view read (turn banner + "Last:" indicator + serpentine
    board grid) → your_turn / your_turn_soon / snake_status / snake_pick. Gate is
    content-based in both directions (see Cross-Poller Rule). Verified against
    real captures (snake-{onclock,waiting,postpick}.html).
  Yahoo snake (yahoo_snake_resolve.mjs) + auction both live in prod.
  Snake board mapping (incl. the round boundary) confirmed working in real drafts.
  Minor open: 2-3 low-tier QBs (Tua, Purdy) adp_ai ~38, slightly early.
- [x] ESPN live-draft poller — SHIPPED to prod (both formats), June 2026
  React + styled-jsx SPA, no stable root → content-gated. Two resolvers:
  espn_salarycap_resolve.mjs (nomination/bid_update/clock/draft_pick/teams_update),
  espn_snake_resolve.mjs (your_turn/your_turn_soon/snake_status/snake_pick).
  Board column→team via .draft-board-grid-header-cell + .myTeam/.onTheClock;
  sale = completedPick delta (.winningPrice); high-bidder = the auction-pick whose
  .bid-amount == the current offer. Name-only surfaces → name backstop.
  Design: docs/espn_resolver_design.md. Fixtures: extension/test/fixtures/espn/.
  Required a CORS allowlist add for https://fantasy.espn.com (#127).
- [x] Sleeper live-draft poller — SHIPPED to prod (snake + auction), June 2026
  Phoenix Channels over WebSocket — cleanest transport (pure JSON, no DOM).
  world:"MAIN" interceptor (sleeper_draft_main.js) patches window.WebSocket (CSP
  blocks inline injection); relays frames to the ISOLATED poller via postMessage.
  sleeper_resolve.mjs (Phoenix parse + serpentine), sleeper_snake_resolve.mjs,
  sleeper_auction_resolve.mjs. player_id is a Sleeper id → exact match on
  players.sleeper_id (backend find_by_sleeper_id, the cleanest resolution).
  Self = localStorage user_id (JSON-quoted → parseUserId unwraps) → draft_order slot.
  Design: docs/sleeper_resolver_design.md. Fixtures: extension/test/fixtures/sleeper/.
  Required CORS for sleeper.com/sleeper.app (#131).
- [x] Draft refresh-resilience + own-pick attribution — SHIPPED, June 2026
  - is_yours flag now drives backend pick attribution (record_pick(is_yours)):
    Sleeper/ESPN slot-label winners ("Team 5") no longer mis-file the user's own
    buys into opponent_rosters (#140).
  - /draft/state sources the snake roster from _my_picks (was the empty auction
    your_roster) (#136).
  - RESUME_WINDOW_SECONDS 1h → 6h so a live draft survives a connection gap without
    a refresh 409'ing to the empty board (#136).
  - Extension orphaned-context recovery: a reload/auto-update orphans the content
    script ("Extension context invalidated") → it reloads the tab once (capped) to
    re-inject a fresh poller, so a live draft survives an extension update (#138).
- [x] Stripe billing — DESIGN PASS complete (NOT implemented), June 2026
  docs/stripe_billing_design.md. Decisions LOCKED: entitlement SoT = DB users.tier
  (Stripe syncs via webhook, read on hot path); monthly recurring subs only
  (/season deferred); cancel → downgrade to intro at period_end, credits persist;
  honor Stripe retries; monthly credit grant on invoice.payment_succeeded +
  billing_reason=subscription_cycle, idempotent on event.id + invoice.id; no Clerk
  mirror. Security: card data never touches Rook (Stripe Checkout redirect → PCI
  SAQ-A), webhook is sole entitlement grantor (mandatory signature verify), prices
  server-defined, customer bound to authed user. Entitlement+gate layer already
  exists (TIER_LIMITS, require_feature/require_credits, upgrade_tier waiting on the
  webhook). NEXT PASS = actual billing code.
- [ ] Stage 30: Half PPR — see docs/stages/stage-30-half-ppr.md
  Half PPR scoring, replacement level adjustments. Note: valuation_agent +
  sync_adp currently hardcode scoring="ppr" (VALUATION_SCORING); make
  configurable for half-PPR.

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

## Live Draft — Browser Extension Architecture (Yahoo / ESPN / Sleeper)

The extension (`extension/`, MV3, sideloaded) reads each draft room and POSTs events
to the backend; the backend enriches + runs the AI rec + broadcasts to the React room
over WebSocket. **One poller per platform/format**, all mapping onto **one backend
event contract** (so backend/frontend are platform-agnostic — a new platform that maps
onto the contract needs zero downstream changes).

**Event contract** (`backend/routers/draft.py` keys on `event.type`, not platform):
- Auction: `nomination`, `bid_update`, `clock`, `draft_pick`, `teams_update`
- Snake: `your_turn`, `your_turn_soon`, `snake_status`, `snake_pick`

**Pollers / resolvers** (pure parse/detect logic in `*_resolve.mjs`, linkedom/JSON-
tested against real captures in `extension/test/fixtures/<platform>/`):
- Yahoo: `yahoo_draft.js` + `yahoo_auction_resolve.mjs`; `yahoo_snake_draft.js` +
  `yahoo_snake_resolve.mjs`. React DOM, shared root `#main-0-DraftClientBootstrap-Proxy`.
- ESPN: `espn_draft.js` + `espn_salarycap_resolve.mjs` / `espn_snake_resolve.mjs` +
  `espn_shared.mjs`. React/styled-jsx, content-gated, no stable root.
- Sleeper: `sleeper_draft.js` (ISOLATED) + `sleeper_draft_main.js` (world:MAIN WS
  interceptor) + `sleeper_resolve.mjs` / `sleeper_snake_resolve.mjs` /
  `sleeper_auction_resolve.mjs`. Phoenix Channels over WS (JSON, no DOM).

**Hard-won universal rules (each cost a prod bug — keep them):**
1. **Content-based cross-poller gate.** When pollers share a page/root, each MUST
   positively detect its OWN format from positive content — host/root presence is
   never a discriminator. Asserted active-on-own + inert-on-others, both directions,
   in the same test pass. (Yahoo auction↔snake share a React root; the test net is
   non-negotiable.)
2. **CSP blocks inline WS interception → use a `world:"MAIN"` content script.** Pages
   (Yahoo, Sleeper) block injected inline `<script>`. A manifest `world:"MAIN"` entry
   at `document_start` is browser-injected (CSP-exempt) and patches `window.WebSocket`
   before the page opens it. Relay frames to the ISOLATED poller via
   `window.postMessage` (CustomEvent `detail` does NOT cross worlds in Chrome).
3. **Each platform's page origin needs a CORS allowlist entry** in `backend/main.py`
   `allow_origin_regex` (bounded, dot-escaped; Starlette uses `fullmatch`). The poller
   posts from the page origin — ESPN (`fantasy.espn.com`) and Sleeper (`sleeper.com`/
   `.app`) each 400'd until added. **Backend change → needs a release to take effect.**
4. **The SPA persistent socket opens at app boot.** Sleeper opens one WS on the lobby
   and joins the draft as a channel — so the interceptor + poller match ALL of the
   site (`sleeper.com/*`), not just `/draft/*`, or it misses early picks.
5. **Orphaned-context recovery.** An extension reload/auto-update orphans the running
   content script (`browser.*` throws "Extension context invalidated"); the poller
   detects a dead `browser.runtime.id` on a draft frame and reloads the tab once
   (capped, reset on healthy relay) to re-inject a fresh poller.
6. **Anchor policy.** `data-testid` + hand-authored semantic classes = PRIMARY;
   build-hash classes (`_ys_*`, `jsx-<digits>`) ROTATE per deploy → FALLBACK ONLY,
   behind a text/structure check, with loud `console.warn` + `selector_health`.
7. **Player resolution: id-first, then name backstop.** Sleeper id → exact
   `players.sleeper_id` (`find_by_sleeper_id`); else name+pos fuzzy
   (`find_by_name_fuzzy`). ESPN/Yahoo surfaces are name-only → name backstop.
8. **`is_yours` is authoritative for own-pick attribution** (slot labels like "Team 5"
   don't equal `your_team_id`); `record_pick(is_yours=...)` routes own buys to
   `your_roster`. Self-team label "You" so the frontend folds it into `myTeamName`.

---

## Known Issues / Backlog

### Extension
- CROSS-POLLER RULE (non-negotiable): the snake
  and auction Yahoo pollers SHARE the same URL
  match patterns AND (as of June 2026) the SAME
  React root #main-0-DraftClientBootstrap-Proxy.
  Each MUST positively detect its OWN draft type
  from POSITIVE CONTENT before acting — the shared
  root is NOT a discriminator. Auction content =
  a Proj-$ nominee (structural: a ys-player[data-id]
  whose short text carries "Proj $") OR >=1 .ys-team
  carrying a $-budget span. Snake content = the turn
  banner ("Your Turn • Round R, Pick P" / "{Name}'s
  Pick • You're up in N Picks • Round R, Pick P").
  Gates: shouldAuctionActivate (yahoo_auction_resolve
  .mjs — content-only: NO timer arm, snake has a
  00:xx clock too; NO bare-.ys-team arm, snake's 180
  board cells are budget-LESS) and shouldSnakeActivate
  (yahoo_snake_resolve.mjs). The snake poller is now
  NON-DESTRUCTIVE — it reads the Board view only
  (banner + "Last:" indicator + serpentine board
  grid), no "Picks"-tab click. History: (1) the old
  snake poller's clickPicksTab() ran on auction pages
  and took the auction room down; (2) fixing auction
  then broke snake when both rooms moved to the shared
  root and the auction gate's timer/bare-.ys-team arms
  false-tripped on snake's 180 budget-less cells. Both
  are why the guard is content-positive in BOTH
  directions now.
- STANDING RULE: snake changes MUST be verified
  against AUCTION (and vice versa). This is the
  2nd snake change to break auction (1st: the
  VORP classifier; 2nd: the poller). Tests cover
  BOTH directions so this class is caught in CI,
  not prod — keep it that way.
- AUCTION REACT CLIENT (2026 replatform): Yahoo
  migrated the AUCTION room to a React app, root
  #main-0-DraftClientBootstrap-Proxy, with NO
  semantic selectors on live data. Two class
  families: `ys-*` KEBAB classes (e.g. .ys-team,
  .ys-player) are hand-authored/semantic — OK as
  anchors; `_ys_*` HASH classes are build-
  generated and ROTATE every Yahoo deploy — NEVER
  a primary key, only a fallback layered behind a
  text/structure check, and using one MUST emit
  loud telemetry (console.warn + selector_health
  heartbeat) so a rotation alarms instead of
  silently stalling. Auction selectors must be
  TEXT / STRUCTURE / kebab-`ys-` anchored
  (resolveAuctionState in yahoo_auction_resolve
  .mjs): gate = root + (timer SPAN /^\d{2}:\d{2}$/
  not in a dialog OR >=1 .ys-team) AND NOT draft-
  complete; nominee identity = ys-player[data-id]
  (stable player ID) primary; team self-id =
  <span>You</span> + .ys-team[data-id] primary.
  Fixtures = REAL captured Yahoo outerHTML under
  extension/test/fixtures/auction/ (re-runnable
  after each deploy), parsed with linkedom.
- SNAKE-MIGRATION LANDMINE — RESOLVED (June 2026):
  Yahoo migrated SNAKE onto the shared auction React
  root (#main-0-DraftClientBootstrap-Proxy), exactly
  as predicted. The old `hasAuctionRoot` veto then
  silently disabled snake on its own page, and the
  auction gate's timer / bare-.ys-team arms false-
  tripped on snake (grabbed the 180-cell board as
  "opponents"). Fix: a React snake resolver
  (yahoo_snake_resolve.mjs) + a content-positive
  guard in BOTH gates — the `hasAuctionRoot` veto is
  RETIRED entirely (root presence is never a
  discriminator). The old yahoo_snake_draft_observer
  .mjs (#app innerText + pick-card scan) is deleted.
  SERPENTINE board mapping: pickSlotIndex() reverses
  on even rounds (pick 12 == pick 13 slot in a 12-team
  league); the captures are early Round 1 only, so the
  round-boundary case is asserted from the rule —
  re-verify against a real round-turn capture to lock.
  Fixtures: extension/test/fixtures/auction/snake-
  {onclock,waiting,postpick}.html.
- Yahoo passive sync removed — Yahoo CSP
  blocks content script injection in both
  Chrome and Firefox. window.__rook__
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
- valuation_agent cache now versions on
  prompt changes via VALUATION_AGENT_VERSION
  in the input_data cache key — prompt edits
  auto-invalidate (no manual clear needed).

### SaaS / Auth
- Clerk is in **production mode** on the live `rookff.com` domain (no longer
  pk_test_/dev). The custom-domain + /api-prefix items that depended on it are
  resolved.
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

### Snake / ADP (Stage 29 follow-ups)
- [x] LeagueContext toggle wired on the
      standalone /draftboard page + App.jsx
      collapsed to a single LeagueProvider.
- [ ] Selector lives only in the sidebar (not
      reachable from the full-screen draft room);
      league must be chosen before entering.
- [x] Extension snake poller SHIPPED — React
      resolver (yahoo_snake_resolve.mjs) maps the
      shared-root snake room: turn banner (pick /
      on-the-clock / countdown), "Last:" indicator
      + serpentine board (snake_pick), all relayed
      via /draft/event. Content-based cross-poller
      guard; non-destructive (no Picks-tab click).
- [x] Serpentine board mapping (incl. round boundary) — confirmed in real drafts.
- [ ] A few low-tier QBs (Tua, Purdy) get
      adp_ai ~38, slightly early.
- [x] Snake signal quality: two-sided draftable
      window in classify_snake_flag (fp_rank >
      WINDOW now neutralizes to TARGET, not just
      adp_rank) — killed ~27 bogus deep-FP
      SLEEPERs (Singletary fp405 etc). Dashboard
      panels now sort by adp_rank asc
      (actionability) not |adp_diff|. adp_diff /
      can_wait semantics unchanged. Recompute via
      scripts/recompute_adp_diff.py (no pipeline
      run). recompute_snake_flags.py latent bug
      (skipped the window) fixed too.
- [x] Positional VALUE/SLEEPER skew RESOLVED via
      VORP. Root cause was definitional, not the
      model: classify_snake_flag split VALUE vs
      SLEEPER on an ABSOLUTE PPR bar (_STRONG_PPR
      TE170 etc). TE170 sits barely above TE
      replacement (~140) so every startable TE
      cleared it, while equally-strong ~200-PPR WRs
      (WR replacement ~200) did not — a WR and TE
      with the same projection got opposite flags.
      Fix: VALUE if the VORP-derived tier <= 2 (the
      valuation engine's PAR-ratio tier, position-
      relative, built on forward projected_ppr_
      season), else SLEEPER. _STRONG_PPR removed.
      Now position-consistent + replacement-aware:
      VALUE 6->13 (TE7/WR4/RB2), and it correctly
      keeps near-replacement ~200 WRs (Addison T3,
      Pittman T3) as SLEEPER while promoting genuine
      separators (Metcalf T2). Also fixed the
      ppr_points(backward) vs projected_ppr_season
      (forward) inconsistency — tier uses forward,
      so e.g. Kyle Pitts now flags off his T1
      projection not his stale ppr_points=96.
      Auction untouched (already VORP $ + surplus
      signal). Recompute via recompute_adp_diff.py.
- [ ] OPEN QUESTION (not yet decided): separate
      injury-faded elites (Kittle) from healthy
      fungible-middle (Warren/Fannin) within VALUE.
      Tier-based VORP leaves Warren/Fannin as VALUE
      (both T2, 188 ~= realized TE6 — a defensible
      separator), so total-points VORP can't make
      this distinction — that's the KNOWN LIMITATION
      this item addresses. Options to EVALUATE (pick
      before building): (a) projected-games per-game
      scoring — needs a projected-games field from
      the agent (pipeline change + VERSION bump,
      expensive; injury prediction is noisy), vs
      (b) a ceiling/variance tiebreaker from the
      EXISTING upside_ppr/downside_ppr already in
      clean_season_baseline (possibly $0, no agent
      change). Decide approach before committing to
      the agent path.

### Stages Remaining
- [x] Stage 29: Snake draft — shipped (all platforms)
- [x] ESPN + Sleeper live-draft pollers — shipped to prod
- [x] CI/CD: GitHub Actions — DONE (backend/frontend/extension checks gate every PR)
- [~] **In-season feature build (PRE-SEASON PRIORITY): Trade page + Trade agent**
      (analyzer + proposals). Design: `docs/trade_agent_design.md`. Built behind a
      clean league-state interface so the SAME agents later run on real data. The
      VALUE MODEL is the differentiator: in-season value is driven by actual
      production + usage TRAJECTORY (target/snap share rising or falling,
      opportunity-vs-production gap), NOT preseason projections — with a name-bias
      guard. **Slices shipped to develop:**
      - per-week NFL data layer `backend/integrations/nfl_weekly.py` (snap%/target
        share/fantasy pts keyed (canonical_player_id, season, week); 2025 production
        from PBP since import_weekly_data([2025]) 404s).
      - league-state seam `backend/services/trade/league_state.py` + in-season value
        engine `backend/services/trade/value_engine.py` (forward_value, usage
        trend, buy_low/sell_high, name-bias guard, ragged-history `confidence`
        full/limited/insufficient).
      - **flag-gated demo harness (TEARDOWN before prod):**
        `backend/services/trade/trade_demo_source.py` (provider + `TRADE_DEMO_MODE`
        gate + a realistic **12-team / 15-slot** league snake-drafted from the real
        ADP pool — `DEMO_TEAM_NAMES` + forced `CASTING` + `_draft_league` — with
        `starter_slot`/`nfl_team` populated + demo anchor `DEMO_SEASON`/
        `DEMO_CURRENT_WEEK` pinned HERE, currently **week 14** — not week 5, not in
        the engine/data layer), `scripts/seed_demo_league.py` (CLI),
        `tests/unit/services/trade/test_trade_demo.py`.
      - analyzer agent + `POST /api/trade/analyze`; proposals agent +
        `POST /api/trade/ideas` (pro-only). PERMANENT.
      - **trade page** `frontend/src/pages/Trade.jsx` + `frontend/src/api/trade.js`
        (PERMANENT minus the team-switcher) and the read-only demo
        **`GET /api/trade/league`** + the page **team-switcher** (TEARDOWN — both
        demo-only, gated/greppable via `TRADE_DEMO` / `fetchTradeLeague`).
      ⚠️ **ALL demo scaffolding (the demo source/seeder/tests, the pinned week, the
      `GET /api/trade/league` endpoint, the page team-switcher) is gated behind
      `TRADE_DEMO_MODE` (default false) and MUST be removed before prod — see the
      teardown checklist in the design doc; grep `TRADE_DEMO` / `DEMO_TEAM_NAMES` /
      `CASTING` / `fetchTradeLeague`. Permanent = the interface, value engine, agents, the
      trade page (minus team-switcher), `/analyze` + `/ideas`, and the gates ONLY.**
      **Remaining:** teardown + real league-state provider (slice 6). Then: lineup
      optimizer, waiver wire, roster monitor, opponent analyzer, gameday.
- [ ] **Stripe billing implementation** (design locked — docs/stripe_billing_design.md)
- [ ] Stage 30: Half PPR support
- [ ] Generalize extension orphaned-context recovery to the Yahoo/ESPN pollers
      (currently only Sleeper)
- [ ] Browser extension Chrome/Firefox store submission
- [ ] my_nomination/my_bid → DraftStateManager
      integration (auto-roster + budget sync)
- [ ] teams_snapshot → engine state reconcile
- [ ] Soft-delete stale player rows
- [ ] Convert dense div-grid views (DraftBoard,
      Teams) to semantic <table> w/ th scope for
      screen-reader accessibility — deferred from
      mobile-responsive work to keep scope isolated.

Old-name infra rename is **DONE** — branding/repo are Rook; the only remaining
old-name reference is the Railway service hostname `fantasymanager-production`
(intentional — the extension's working API base, see title note).

---

## Frontend Responsive / Mobile Conventions

Mobile use case = pre-draft PREP and REVIEW (browse/research), NOT live drafting
(live drafting needs the desktop browser extension). Presentation layer only —
responsive work never changes logic, stores, data fetching, WS, or contracts.

- **Mobile-first.** Base classes target small phones; layer up with `sm:`/`md:`/
  `lg:` prefixes. Tailwind v4 (CSS-first, default breakpoints; v4 has native
  `@container` — use it for components in variable-width slots, viewport
  breakpoints elsewhere).
- **Desktop = the `lg` (≥1024px) tier and MUST NOT regress** — it is the approved
  design and must render pixel-equivalent to before at ≥1024px. Add smaller
  styles beneath it; never strip the desktop classes (move them behind `lg:`).
- **Device tiers:** base <640 (phones) · `sm` 640 (large/landscape phone) ·
  `md` 768 (tablet portrait) · **`lg` 1024 = approved desktop** · `xl` 1280.
- **≥44px touch targets** for interactive elements on mobile (`min-h-11` etc.),
  reverted at `lg` so desktop density is unchanged.
- **Dense data views** (DraftBoard, Teams) stay div-grids for now (see backlog);
  pick the responsive pattern PER VIEW — pinned-player-column horizontal scroll,
  breakpoint column-hiding, or row→card — not one blanket transform. On the
  DraftBoard, `adp_diff` and the snake flags (TARGET/SLEEPER/VALUE/REACH) are the
  core product signal and must ALWAYS stay visible on mobile; hide raw/duplicate
  rank or PPR cells instead. New tables use semantic `<table>` w/ `th scope`.

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

**Source of truth:** `backend/models/user.py` (`TIER_LIMITS` / `CREDIT_COSTS` /
`CREDIT_PACKS`) — the prose above mirrors it. The stale `stage-25` tier block
(`free|starter|pro|league`) is superseded; remove it in a cleanup PR.

**Stripe billing design:** `docs/stripe_billing_design.md` (decisions LOCKED).
Entitlement layer (tier store + `FeatureService` + `require_feature`/`require_credits`
+ `upgrade_tier`/`apply_signup_bonus`) is already built and waiting on the Stripe
webhook. Billing code (Checkout, `/webhooks/stripe`, gate-attach, frontend CTAs) is
the next implementation pass — not yet started. `/season` deferred (monthly only v1).
