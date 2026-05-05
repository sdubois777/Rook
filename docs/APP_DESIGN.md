# Application Design

This document specifies the full user-facing application — every page,
every component, and how they connect to the backend pipeline data.

The application is usable year-round, not just on draft day:
- **Offseason (now → July):** Player research, news monitoring, draft prep
- **Pre-draft (August):** Draft board refinement, strategy configuration
- **Draft day:** Live auction control
- **In-season (Sept → Jan):** Trade analysis, lineup decisions, waiver wire

All pages read from the draft bible database. The pipeline keeps it current.

---

## Navigation Structure

```
Sidebar (always visible):
  📊  Dashboard          — Overview and recent activity
  👤  Players            — Full player browser and search
  🏟️  Teams              — NFL team intelligence
  📰  News               — Transactions and beat reporter feed
  📋  Draft Board        — Pre-draft rankings and bid ceilings
  🎯  Draft Room         — Live auction (draft day only)
  📈  Trades             — Trade analyzer and proposals (in-season)
  🏆  Lineup             — Weekly lineup optimizer (in-season)
  🔄  Waivers            — Waiver wire recommendations (in-season)
  ⚙️  Pipeline           — Admin: data freshness and pipeline triggers
```

---

## Page 1: Dashboard

**Purpose:** At-a-glance overview of what's changed and what needs attention.
This is the landing page every time you open the app.

### Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  DRAFT PREP STATUS                          [X days until draft] │
│  Pipeline last run: 2h ago  •  463 players  •  All agents green  │
├─────────────────────────────────────────────────────────────────┤
│  RECENT ALERTS (from Beat Reporter)                              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ 🔴 DISPLACEMENT  Keenan Allen signs with LAC             │    │
│  │    Ladd McConkey's target share ceiling now capped.      │    │
│  │    Bid ceiling dropped: $28 → $14    [View McConkey]     │    │
│  ├─────────────────────────────────────────────────────────┤    │
│  │ 🟡 SCHEME CHANGE  New OC hired in Chicago                │    │
│  │    Shane Waldron (pass-heavy) replaces run-first scheme. │    │
│  │    D.J. Moore value improving.         [View Moore]      │    │
│  ├─────────────────────────────────────────────────────────┤    │
│  │ 🟢 BREAKOUT FLAG  Rashee Rice — Year 2 WR               │    │
│  │    Clear WR1 role, strong system, favorable schedule.    │    │
│  │    System value: $34  Market value: $22  [View Rice]     │    │
│  └─────────────────────────────────────────────────────────┘    │
├──────────────────────────┬──────────────────────────────────────┤
│  TOP VALUE GAPS          │  POSITION SCARCITY                   │
│  Players where system    │  How many viable starters remain     │
│  value >> market value   │  at each position                    │
│                          │                                      │
│  Player A  +$18 edge     │  QB  ████████░░  18 viable          │
│  Player B  +$14 edge     │  RB  ███░░░░░░░   8 viable  ⚠️      │
│  Player C  +$12 edge     │  WR  █████░░░░░  14 viable          │
│  Player D  +$11 edge     │  TE  ████░░░░░░  10 viable          │
│  [View all →]            │                                      │
├──────────────────────────┴──────────────────────────────────────┤
│  WATCHLIST                                                       │
│  [Your pinned players with latest flags and price changes]      │
└─────────────────────────────────────────────────────────────────┘
```

### Data sources
- Recent alerts: `beat_reporter_signals` table, last 7 days, sorted by severity
- Value gaps: `players` table, `value_gap` field, top 10 positive gaps
- Position scarcity: `players` table, count of tier 1-3 players per position
- Watchlist: user preference table (new — see below)

---

## Page 2: Players

**Purpose:** Browse, search, and deep-dive on any draftable player.
This is the most-used page in pre-draft prep.

### Player List View (default)

```
Filters: [All Positions ▼] [All Tiers ▼] [All Teams ▼] [Sort: Bid Ceiling ▼]

Search: [___________________]  Showing 463 players

┌────────────────────────────────────────────────────────────────────┐
│ Pos │ Player            │ Team │ Tier │ Bid Ceil │ System │ Market │
├─────┼───────────────────┼──────┼──────┼──────────┼────────┼────────┤
│ RB  │ Saquon Barkley    │ PHI  │  1   │   $68    │  $71   │  $65   │
│     │ ⚠️ WORKLOAD_CLIFF │      │      │          │        │        │
├─────┼───────────────────┼──────┼──────┼──────────┼────────┼────────┤
│ WR  │ Justin Jefferson  │ MIN  │  1   │   $58    │  $61   │  $55   │
│     │ ✓ No flags        │      │      │          │        │        │
├─────┼───────────────────┼──────┼──────┼──────────┼────────┼────────┤
│ WR  │ Ladd McConkey     │ LAC  │  2   │   $14    │  $14   │  $31   │
│     │ 🔴 DISPLACED      │      │      │          │        │        │
└────────────────────────────────────────────────────────────────────┘
```

**Flag color coding:**
- 🔴 Red: DISPLACED, HIGH injury risk, VOLATILE, compound_risk_flag
- 🟡 Yellow: COMMITTEE, MODERATE injury risk, SCHEME_FIT mismatch
- 🟢 Green: BREAKOUT candidate, COLLEGE_TRUST, BENEFICIARY upside
- ⚪ Gray: no flags

**Filters:**
- Position: All / QB / RB / WR / TE / K / DEF
- Tier: All / Tier 1 / Tier 2 / Tier 3 / Tier 4-5
- Team: dropdown of all 32 teams
- Flags: Show only flagged / Show only clean / Show all
- Value: Undervalued (system > market) / Overvalued / Aligned
- Sort: Bid ceiling / System value / Market value / Name / Position

### Player Detail View

Clicking any player opens a full detail panel (slide-in from right, or full page):

```
┌──────────────────────────────────────────────────────────────────┐
│  Ladd McConkey  ·  WR  ·  LAC               ★ Add to Watchlist  │
├──────────────────────────────────────────────────────────────────┤
│  VALUATION                                                        │
│  Bid ceiling:   $14    ██░░░░░░░░░░░░░░░░░░░░  (of $80 max)     │
│  System value:  $14    Market value: $31                         │
│  Value gap:     -$17   Market OVERVALUES this player             │
│  Tier: 2       Situation: WEAK    Risk: MODERATE                 │
├──────────────────────────────────────────────────────────────────┤
│  ⚠️ ACTIVE FLAGS                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ 🔴 DISPLACED by Keenan Allen                               │  │
│  │    Allen commands 27% historical target share with Herbert. │  │
│  │    Direct slot role overlap. Ceiling now capped.           │  │
│  │    Impact: -35% to value  ·  Confidence: HIGH              │  │
│  ├────────────────────────────────────────────────────────────┤  │
│  │ 🟢 CONTINGENT upside if Allen is absent                    │  │
│  │    McConkey absorbs near-full WR1 volume when Kupp/Allen   │  │
│  │    miss time. Historical precedent from 2023.              │  │
│  │    Impact: +45% to value  ·  Confidence: HIGH              │  │
│  └────────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────────┤
│  SYSTEM CONTEXT                                     [LAC →]      │
│  QB: Justin Herbert  ·  Tier: Solid  ·  Trust score: 74         │
│  O-line protection: B+  ·  Run blocking: B                       │
│  OC scheme: Balanced  ·  Personnel: 11  ·  System: A-           │
├──────────────────────────────────────────────────────────────────┤
│  PRODUCTION PROFILE                                              │
│  Role: Slot specialist                                           │
│  Target share (avg clean seasons): 18%  [was 26% before Allen]  │
│  Targets/route: 0.24  ·  Air yards share: 0.19                  │
│  Efficiency: Above avg  ·  Age curve: Ascending (age 23)        │
│                                                                  │
│  Clean season baseline:                                          │
│  Rec: 82  ·  Yds: 890  ·  TD: 5  ·  PPR: 187pts               │
├──────────────────────────────────────────────────────────────────┤
│  INJURY RISK                                                     │
│  Overall: LOW  ·  No pattern flags  ·  Risk modifier: -3%       │
├──────────────────────────────────────────────────────────────────┤
│  SCHEDULE                                                        │
│  Early (wks 1-6): Favorable  ·  Full season: Neutral            │
│  Playoffs (wks 14-17): FAVORABLE  ·  Bye: Week 5                │
│  Schedule score: 6.8/10                                          │
├──────────────────────────────────────────────────────────────────┤
│  RECENT NEWS                                                     │
│  May 12: Allen signed — target share analysis updated            │
│  Apr 3:  McConkey praised in OTA reports (camp standout)        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Page 3: Teams

**Purpose:** Browse NFL team offensive system grades. Understand the context
behind player valuations. Especially useful for evaluating players on
teams with recent OC changes, new QBs, or notable O-line changes.

### Team List View

```
Filter: [All Divisions ▼]  Sort: [System Grade ▼]

┌──────────────────────────────────────────────────────────────────┐
│ Team │ Grade │ QB              │ O-line Pass │ Scheme    │ Flags  │
├──────┼───────┼─────────────────┼─────────────┼───────────┼────────┤
│ PHI  │  A+   │ Jalen Hurts     │     A       │ Balanced  │        │
│ KC   │  A    │ Patrick Mahomes │     A-      │ Pass-heavy│        │
│ LAC  │  A-   │ Justin Herbert  │     B+      │ Balanced  │        │
│ CHI  │  C    │ Caleb Williams  │     C+      │ Pass-heavy│ 🔴 RQB │
│ CAR  │  D    │ [Rookie]        │     D       │ Unknown   │ 🔴 CRF │
└──────────────────────────────────────────────────────────────────┘
RQB = Rookie QB flag  ·  CRF = Compound Risk Flag (rookie QB + bad line)
```

### Team Detail View

```
┌──────────────────────────────────────────────────────────────────┐
│  Los Angeles Chargers  ·  System Grade: A-                       │
├──────────────────────────────────────────────────────────────────┤
│  QUARTERBACK                                                      │
│  Justin Herbert  ·  Tier: Solid  ·  Experience: 6 years         │
│  CPOE: +2.4  ·  Air yards/att: 8.2  ·  Pressure perf: Above avg │
│  Rookie QB: No  ·  Compound risk: No                             │
├──────────────────────────────────────────────────────────────────┤
│  OFFENSIVE LINE                                                   │
│  Pass protection: B+    Run blocking: B                          │
│  (Graded separately — pass and run don't always correlate)       │
├──────────────────────────────────────────────────────────────────┤
│  OFFENSIVE COORDINATOR                                           │
│  Scheme: Balanced  ·  Run/pass split: 52% pass                  │
│  Personnel tendency: 11 (1 RB, 1 TE, 3 WR)                     │
│  Red zone philosophy: WR1                                        │
├──────────────────────────────────────────────────────────────────┤
│  SKILL POSITION PLAYERS                                          │
│                                                                  │
│  WR  Keenan Allen      Tier 2   $28    ✓ No displacement flags  │
│  WR  Ladd McConkey     Tier 2   $14    🔴 DISPLACED by Allen    │
│  RB  [Primary back]    Tier 2   $32    🟡 COMMITTEE             │
│  TE  [Starter]         Tier 3   $12    ✓                        │
│                                                                  │
│  QB/WR Trust Scores:                                            │
│  Herbert → Allen: 91  (4 years NFL, 27% avg target share)       │
│  Herbert → McConkey: 58  (1 year NFL)                           │
└──────────────────────────────────────────────────────────────────┘
```

---

## Page 4: News

**Purpose:** Real-time feed of NFL transactions, depth chart changes,
injuries, and coaching moves — with the AI's interpretation of what
each event means for fantasy values.

### Feed Layout

```
Filter: [All Signal Types ▼]  [All Teams ▼]  [Last 7 days ▼]

┌──────────────────────────────────────────────────────────────────┐
│ 🔴  SIGNING — High Impact                          May 14, 2026  │
│     Keenan Allen signs 2yr/$28M with LA Chargers                │
│                                                                  │
│     AI Analysis: Allen's return to Herbert creates direct        │
│     slot role overlap with Ladd McConkey. Historical target      │
│     share data shows Allen commanded 27% when healthy with       │
│     Herbert. McConkey's ceiling is now structurally capped.      │
│                                                                  │
│     Players affected:                                            │
│     ↓ McConkey (LAC WR)  Bid ceiling: $28 → $14                │
│     ↑ Allen (LAC WR)     Added to player pool: $28 ceiling      │
│                                          [View McConkey] [View Allen] │
├──────────────────────────────────────────────────────────────────┤
│ 🟡  COACHING — Medium Impact                       May 12, 2026  │
│     Shane Waldron hired as CHI offensive coordinator             │
│                                                                  │
│     AI Analysis: Waldron brings a pass-heavy scheme (62% pass   │
│     rate at SEA). Replaces run-first approach. D.J. Moore's     │
│     role in a Waldron offense historically elevates slot WRs.   │
│                                                                  │
│     Players affected:                                            │
│     ↑ D.J. Moore (CHI WR)   System grade improving              │
│     ↓ [CHI RB] (CHI RB)     Run game volume likely reduced      │
│                                            [View Moore] [View Team] │
├──────────────────────────────────────────────────────────────────┤
│ 🟢  DRAFT PICK — Rookie Added                      May 10, 2026  │
│     [Team] selects [WR] in Round 1, Pick 12                     │
│                                                                  │
│     AI Analysis: Elite college profile (42% adjusted dominator, │
│     SEC). High draft capital (value: 81/100). Strong landing    │
│     spot modifier (1.15). Historical comps: Ja'Marr Chase,      │
│     Justin Jefferson. Year 1 role: likely starter.              │
│                                                                  │
│     🟢 BREAKOUT CANDIDATE flagged (elite profile + high capital) │
│     Bid ceiling: $28  Variance: HIGH  Confidence: LOW           │
│                                              [View Player Profile] │
└──────────────────────────────────────────────────────────────────┘
```

**Signal type filters:**
- Signing / Free agent
- Trade
- Draft pick
- Release / Cut
- Coaching change
- Injury report
- Depth chart change
- Camp standout

---

## Page 5: Draft Board

**Purpose:** Your pre-draft rankings with full system valuations.
This is the strategic prep page — building your mental model of
who to target, what to pay, and what the market will do.

### Board Layout

```
View: [Tiers ▼]  Position: [All ▼]  Sort: [Bid Ceiling ▼]

STRATEGY: [Hero RB ▼]  Budget: $200  Skill starters: $185

── TIER 1 — ELITE ──────────────────────────────────────────────────

RB  Saquon Barkley    PHI   Ceil: $68   Sys: $71  Mkt: $65  ⚠️ WC
RB  Bijan Robinson    ATL   Ceil: $58   Sys: $62  Mkt: $58  ✓
WR  Justin Jefferson  MIN   Ceil: $58   Sys: $61  Mkt: $55  ✓
WR  CeeDee Lamb       DAL   Ceil: $55   Sys: $59  Mkt: $54  ✓

── TIER 2 — STRONG STARTERS ────────────────────────────────────────

WR  Puka Nacua        LAR   Ceil: $32   Sys: $33  Mkt: $28  🟢 BRK
RB  Derrick Henry     BAL   Ceil: $30   Sys: $29  Mkt: $32  ⚠️ AGE
TE  Brock Bowers      LV    Ceil: $35   Sys: $37  Mkt: $30  🟢 +$7
...

── TIER 3 — SOLID STARTERS ─────────────────────────────────────────
...

Legend:
  WC = WORKLOAD_CLIFF  ·  AGE = age curve declining
  BRK = BREAKOUT candidate  ·  +$7 = system undervalued by $7
```

**Strategy mode** (connected to LEAGUE_RULES.md):
When Hero RB is selected, tier-1 RBs are highlighted as priorities.
When Zero RB is selected, WRs are highlighted and RBs are dimmed.

**Watchlist button:** Star any player to add to your watchlist.

**Side panel:** Clicking any player opens the Player Detail panel
from Page 2 without leaving the draft board.

---

## Page 6: Draft Room (Draft Day Only)

Fully specified in `docs/stages/stage-13-draft-ui.md`.
This page is only active and accessible on draft day.
A banner shows "Draft starts [date]" when the draft is not active.

---

## Page 7: Pipeline Admin

**Purpose:** Monitor data freshness, trigger pipeline runs, track API costs.

```
┌──────────────────────────────────────────────────────────────────┐
│  PIPELINE STATUS                                                  │
├──────────────────────────────────────────────────────────────────┤
│  Agent              Last Run          Status    Freshness        │
│  Team Systems       2026-05-14 09:00  ✓ OK      30 days ago      │
│  Roster Changes     2026-05-14 09:02  ✓ OK      30 days ago      │
│  Player Profiles    2026-05-14 09:45  ✓ OK      30 days ago      │
│  Injury Risk        2026-05-14 10:01  ✓ OK      30 days ago      │
│  Schedule           2026-05-14 10:15  ✓ OK      30 days ago      │
│  Beat Reporter      2026-05-16 07:00  ✓ OK      2 hours ago      │
├──────────────────────────────────────────────────────────────────┤
│  [Run All Agents]  [Run Beat Reporter]  [Refresh Market Values]  │
│  [Dry Run (estimate cost)]                                        │
├──────────────────────────────────────────────────────────────────┤
│  COST REPORT                                                      │
│  This week: $0.23   This month: $1.47   Season total: $1.47      │
│  [View breakdown by agent →]                                      │
└──────────────────────────────────────────────────────────────────┘
```

---

## New Database Table: User Preferences

The watchlist and any user settings need a simple storage table.

```sql
CREATE TABLE user_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    preference_type VARCHAR(50),  -- 'watchlist', 'draft_strategy', 'settings'
    entity_id VARCHAR(100),       -- player_id for watchlist, null for settings
    value JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

Watchlist entry: `{preference_type: 'watchlist', entity_id: player_id, value: {notes: ''}}`
Draft strategy: `{preference_type: 'draft_strategy', value: {primary: 'hero_rb', fallback: 'balanced'}}`

---

## Component Library

Shared components used across all pages:

### PlayerCard (compact)
Used in lists, watchlists, news feed:
```
[POS] Player Name    Team  Tier  $CEIL  [flags]
```

### PlayerCard (expanded)
Used in draft board rows:
```
[POS] Player Name    Team  Ceil: $XX  Sys: $XX  Mkt: $XX  [flags]
```

### FlagBadge
Colored badge for each flag type:
- DISPLACED: red background, white text
- CONTINGENT: green outline, green text
- BREAKOUT: green background, white text
- COMMITTEE: yellow background, dark text
- INJURY HIGH: red background
- INJURY MODERATE: yellow background
- SCHEME_FIT: gray background
- COLLEGE_TRUST: blue background
- WORKLOAD_CLIFF: orange background
- COMPOUND_RISK: red background, bold

### ValueComparison
Horizontal bar showing system vs market value relationship:
```
Market: $31  [████████████░░░░░░░░]  System: $14
              Market overvalues — let opponents pay
```

### SystemGradeBadge
Letter grade badge: A+/A/A-/B+/B/B-/C+/C/C-/D/F
Color coded: green (A), teal (B), yellow (C), orange (D), red (F)

### NewsFeedItem
Signal type icon + headline + AI interpretation + affected players

---

## Build Priority

Build these pages in this order — earlier pages use data already in the DB,
later pages depend on features not yet built:

1. **Players page** (browser + detail view) — all data exists in DB right now
2. **Teams page** — all data exists in DB right now
3. **Dashboard** — depends on Players and Teams being done
4. **News page** — depends on beat_reporter_signals table (already populated)
5. **Draft Board** — depends on Players page components being reusable
6. **Pipeline Admin** — straightforward, read from api_usage_log
7. **Draft Room** — Stage 13, depends on Stages 11-12

Pages 1-6 can all be built now without an active Yahoo league.
Page 7 (Draft Room) waits until the Playwright bridge is complete.

---

## API Endpoints Needed (Frontend → Backend)

All of these read from the existing database — no new pipeline work needed.

```
# Players
GET /players                     → paginated player list with filters
GET /players/{id}                → full player detail
GET /players/search?q={name}     → name search
GET /players/watchlist           → user's watchlist
POST /players/{id}/watchlist     → add to watchlist
DELETE /players/{id}/watchlist   → remove from watchlist

# Teams
GET /teams                       → all 32 teams with system grades
GET /teams/{abbr}                → team detail with skill position players

# News
GET /news                        → beat reporter signals feed
GET /news?team={abbr}            → filtered by team
GET /news?player_id={id}         → filtered by player

# Draft Board
GET /draftboard                  → ranked players with all valuation fields
GET /draftboard?strategy={name}  → strategy-adjusted ceilings

# Pipeline Admin
GET /admin/pipeline-status       → agent status and freshness
POST /admin/pipeline/run         → trigger agent run
GET /admin/cost-report           → usage summary

# User Preferences
GET /preferences/watchlist
POST /preferences/watchlist
GET /preferences/strategy
PUT /preferences/strategy
```
