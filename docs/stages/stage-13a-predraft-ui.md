# Stage 13a: Pre-Draft Application UI

This stage builds the full pre-draft application — everything visible
and usable before the live draft begins. It replaces the previous Stage 13
which only covered the draft room UI.

The draft room UI is now Stage 13b (builds after Stage 12).

## Before starting, read:
- `docs/APP_DESIGN.md` — full application design spec
- `docs/rules/PATTERNS.md`

**ASK USER before building anything:**
1. Dark mode or light mode default?
2. Any color scheme preferences?
3. Any layout preferences not covered in APP_DESIGN.md?

---

## Goal
A fully functional pre-draft research application that surfaces the
draft bible data in a usable, visually clear UI. All data comes from
the existing database — no new pipeline work needed for this stage.

---

## Tech stack
- React + Vite
- Tailwind CSS (utility classes only — no custom CSS files)
- Zustand (global state)
- React Query (data fetching and caching)
- socket.io-client (for news feed live updates)
- React Router (page routing)

---

## Build order (do these in sequence)

### Step 1: Backend API endpoints

Before touching React, build all the FastAPI endpoints the frontend needs.
All read from existing DB tables — no new pipeline work.

Create `backend/routers/players.py`:
```python
GET /players                  # Paginated, filterable player list
GET /players/{id}             # Full player detail with all subsections
GET /players/search           # Query param: q=name
```

Create `backend/routers/teams.py`:
```python
GET /teams                    # All 32 teams with system grades
GET /teams/{abbr}             # Team detail with players
```

Create `backend/routers/news.py`:
```python
GET /news                     # Beat reporter signals feed
                              # Query params: team, player_id, days, type
```

Create `backend/routers/draftboard.py`:
```python
GET /draftboard               # Ranked players, all valuation fields
                              # Query params: strategy, position, tier
```

Create `backend/routers/preferences.py`:
```python
GET /preferences/watchlist
POST /preferences/watchlist   # body: {player_id}
DELETE /preferences/watchlist/{player_id}
GET /preferences/strategy
PUT /preferences/strategy     # body: {primary, fallback, pivot_sensitivity}
```

Add `user_preferences` table migration (see APP_DESIGN.md schema).

Verify all endpoints return correct data before starting React.

### Step 2: React project setup

```bash
cd frontend
npm create vite@latest . -- --template react
npm install tailwindcss @tailwindcss/vite
npm install zustand @tanstack/react-query react-router-dom
npm install socket.io-client axios
npm install lucide-react  # icons
```

Set up:
- React Router with routes for all 6 pages
- React Query provider
- Zustand stores (one for user preferences/watchlist)
- Axios instance with base URL pointing to FastAPI
- Tailwind configured

### Step 3: Component library

Build shared components FIRST — they're used everywhere.

`frontend/src/components/shared/`:

**FlagBadge.jsx**
Props: `flagType` (displaced|contingent|beneficiary|committee|breakout|
        injury_high|injury_moderate|scheme_fit|college_trust|
        workload_cliff|compound_risk|rookie)
Renders colored badge with icon and label.
See APP_DESIGN.md Component Library for color specs.

**PlayerCardCompact.jsx**
Props: `player` (full player object from API)
Renders: position badge, name, team, tier, bid ceiling, top flag
Used in: lists, watchlists, search results

**PlayerCardExpanded.jsx**
Props: `player`
Renders: full row with system/market value comparison bar, all flags
Used in: draft board, player list

**ValueComparisonBar.jsx**
Props: `systemValue`, `marketValue`
Renders the horizontal bar showing under/overvalued relationship
with plain English label ("Market overvalues — let opponents pay")

**SystemGradeBadge.jsx**
Props: `grade` (A+, A, A-, B+, etc.)
Color coded: green (A), teal (B), yellow (C), orange (D), red (F)

**NewsFeedItem.jsx**
Props: `signal` (from beat_reporter_signals)
Renders: signal type icon, headline, AI interpretation, affected players

**PositionBadge.jsx**
Props: `position`
Color coded: QB=purple, RB=green, WR=blue, TE=orange, K=gray, DEF=gray

### Step 4: Players page

`frontend/src/pages/Players.jsx`

Two views: list (default) and detail panel (slide-in).

**List view features:**
- Paginated player list (50 per page)
- Filter bar: position, tier, team, flags, value gap direction
- Sort: bid ceiling, system value, market value, name
- Search bar (live search as you type, debounced 300ms)
- Each row uses PlayerCardExpanded component
- Click row → opens PlayerDetailPanel

**PlayerDetailPanel.jsx** (slide-in from right, ~480px wide)
All sections from APP_DESIGN.md Player Detail View:
- Valuation section (bid ceiling, system/market, value gap)
- Active flags (each flag expanded with reasoning text)
- System context (QB, O-line, OC scheme)
- Production profile (target share, efficiency metrics, baseline)
- Injury risk (level, pattern flags, modifier)
- Schedule (all three windows, playoff grade prominent)
- Recent news (from beat_reporter_signals for this player)
- Watchlist toggle button

### Step 5: Teams page

`frontend/src/pages/Teams.jsx`

**Team list view:**
- All 32 teams in a filterable table
- Columns: team, system grade badge, QB name, O-line pass grade, scheme, flags
- Click row → team detail view

**Team detail view:**
- All sections from APP_DESIGN.md Team Detail View
- QB/WR trust scores listed
- All skill position players for this team (links to player detail)
- Compound risk flag prominently displayed when active

### Step 6: News page

`frontend/src/pages/News.jsx`

- Chronological feed of beat_reporter_signals
- Filter by: signal type, team, last N days
- Each item uses NewsFeedItem component
- Items expand on click to show full AI reasoning
- "Affected players" links open PlayerDetailPanel
- Live updates via WebSocket (new signals push to feed without refresh)

### Step 7: Dashboard

`frontend/src/pages/Dashboard.jsx`

Build this AFTER Players, Teams, News — it reuses their components.

Sections (see APP_DESIGN.md Dashboard layout):
- Draft prep status bar (days until draft, pipeline freshness)
- Recent alerts (top 5 from news feed, highest severity)
- Top value gaps (top 10 players where system > market)
- Position scarcity bars
- Watchlist (user's pinned players)

"Days until draft" — **ASK USER** for their draft date so it can be
stored in league_settings table and displayed here.

### Step 8: Draft Board

`frontend/src/pages/DraftBoard.jsx`

- Tiered view showing all players grouped by tier
- Strategy selector at top (Hero RB / Zero RB / Balanced / etc.)
  → Changes which positions are highlighted/dimmed
  → Connected to LEAGUE_RULES.md strategy definitions
- Each player row uses PlayerCardExpanded
- Click row → PlayerDetailPanel slides in
- Star icon on each row for watchlist
- Position filter at top
- "Print" or "Export" button for a clean draft cheat sheet

**Strategy mode visual treatment:**
- Hero RB selected: tier-1 RBs get a highlight border, RBs boosted
- Zero RB selected: RBs dimmed, WRs highlighted
- Stars and Scrubs: tier-1 players at any position highlighted

### Step 9: Pipeline Admin

`frontend/src/pages/PipelineAdmin.jsx`

- Agent status table (last run, status, freshness indicator)
- Freshness warnings based on thresholds from LEAGUE_RULES.md
- Manual trigger buttons per agent + "Run All" button
- Dry run button (shows cost estimate without running)
- Cost report section (from api_usage_log)
- **ASK USER** to test each trigger and confirm agents run correctly

### Step 10: Navigation and layout

`frontend/src/components/layout/`:

**Sidebar.jsx**
- All 6 page links with icons (lucide-react)
- Draft Room link disabled/grayed until draft is active
- Active page highlighted
- Pipeline freshness indicator (green/yellow/red dot) at bottom

**Layout.jsx**
- Sidebar + main content area
- Responsive: sidebar collapses on smaller screens

---

## Required test cases

```javascript
// tests/unit/components/

// Shared components
test('FlagBadge renders correct color for DISPLACED')
test('FlagBadge renders correct color for BREAKOUT')
test('ValueComparisonBar shows overvalued label when market > system by 5+')
test('ValueComparisonBar shows undervalued label when system > market by 5+')
test('SystemGradeBadge renders green for A grades')
test('SystemGradeBadge renders red for D/F grades')

// Players page
test('PlayerList renders correct number of players')
test('PlayerList filter by position works')
test('PlayerList search filters by name')
test('PlayerDetailPanel shows all flag sections')
test('PlayerDetailPanel watchlist button toggles correctly')

// News page
test('NewsFeedItem renders signal type icon')
test('NewsFeedItem expands on click')
test('News filter by team reduces results')

// Draft board
test('DraftBoard groups players by tier')
test('DraftBoard strategy selection highlights correct positions')
```

---

## Verification before marking complete

1. **ASK USER** to navigate all pages and give feedback
2. Players page: can search for Saquon Barkley and see his flags, bid ceiling, system context
3. Teams page: LAC shows Herbert's trust scores and McConkey's DISPLACED flag
4. News page: shows recent beat reporter signals with AI reasoning
5. Dashboard: recent alerts and value gaps populated
6. Draft board: tiers render correctly, strategy mode changes highlighting
7. Pipeline admin: can trigger Beat Reporter agent from UI
8. All shared components render correctly
9. No TypeErrors or console errors on any page
10. All unit tests pass

---

## Commit
```
feat(app-ui): implement pre-draft application UI

Player browser with search, filters, and full detail panels.
Team intelligence page with system grades and trust scores.
News feed with AI interpretation of transactions.
Dashboard with alerts, value gaps, and watchlist.
Draft board with tier groupings and strategy mode.
Pipeline admin with agent triggers and cost reporting.
All shared components (FlagBadge, ValueComparison, etc.).
```
