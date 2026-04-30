# Stage 11: Playwright Yahoo Draft Room Bridge

## Before starting, read:
- `docs/LIVE_DRAFT.md` — full Playwright bridge spec
- `docs/rules/PATTERNS.md` — Pattern 7: No polling

---

## Goal
App can receive live draft events from Yahoo and send nominations/bids back.
Zero lag. Zero polling. Graceful failure handling.

---

## Tasks

### 1. Yahoo draft room URL
**ASK USER** for the Yahoo draft room URL format before writing any code.
Ask them to navigate to last year's draft recap or this year's draft lobby.

### 2. WebSocket frame capture
**ASK USER** to capture real Yahoo WebSocket frames:
- Open Yahoo draft room → Chrome DevTools → Network → WS tab
- Record payloads for: nomination, bid update, draft pick confirmed, clock warning
- Save to `tests/fixtures/yahoo_ws_frames.json`
These are required for the test suite.

### 3. Playwright bridge implementation
`backend/integrations/yahoo_playwright.py`:

```python
class YahooPlaywrightBridge:
    async def connect(self, draft_room_url: str)
        # 1. Launch Playwright (headless=False for testing)
        # 2. Navigate to draft room
        # 3. Set up WS interception (primary)
        # 4. Inject MutationObserver (fallback)
        # 5. Start health check loop

    async def intercept_draft_websocket(self, page)
        # Parses frames: nomination, bid_update, draft_pick, clock_warning
        # Emits events to WebSocket manager — NO asyncio.sleep() here

    async def nominate_player(self, yahoo_player_id: str, opening_bid: int)
    async def place_bid(self, amount: int)
    async def pass_nomination(self)

    async def health_check_loop(self)
        # Ping every 10 seconds, auto-reconnect on failure

    async def on_bridge_failure(self, action: str, amount: int = None)
        # IMMEDIATELY emit MANUAL_ACTION_REQUIRED
        # Never crash silently
```

### 4. WebSocket manager
`backend/websocket/manager.py`:
- Manages WebSocket connections to React frontend
- Push-based, no polling
- Broadcast draft events to connected clients

### 5. Zero polling verification
`test_no_polling_in_event_chain` must inspect source code for:
- `asyncio.sleep()` inside event loops
- `time.sleep()`
- `setInterval` equivalents
Any found → test fails.

---

## Required test cases
```python
def test_nomination_event_parsed_from_ws_frame()  # uses fixture
def test_bid_update_event_parsed()
def test_draft_pick_confirmed_event_parsed()
def test_clock_warning_event_parsed()
def test_bridge_failure_emits_manual_action_alert()
def test_health_check_triggers_reconnect_on_failure()
def test_no_polling_in_event_chain()  # code inspection test
def test_nomination_fires_playwright_action()
def test_bid_fires_playwright_action()
```

---

## Mock draft testing
**ASK USER** to set up a practice/mock draft on Yahoo.
Run bridge against mock draft before real draft.
Verify nomination detection under 100ms.
Test bid placement under time pressure.
Simulate bridge failure → verify manual action alert fires.

---

## Verification before marking complete
1. **ASK USER** to capture WS frames and provide URL
2. All unit tests passing with fixture frames
3. Mock draft run successfully with zero lag
4. Bridge failure alert fires and appears in UI
5. No polling anywhere — `test_no_polling_in_event_chain` passes
6. Coverage 80%+

---

## Commit
```
feat(yahoo-playwright): implement Playwright draft room bridge

WebSocket interception, MutationObserver fallback.
Zero polling — fully event-driven.
Bridge failure alert implemented.
Coverage: X%.
```

---
---

# Stage 12: Live Draft Agent

## Before starting, read:
- `docs/LIVE_DRAFT.md` — Live Draft Agent section
- `docs/ARCHITECTURE.md` — Two-Value Auction System, Opponent Modeling

---

## Goal
Agent queries draft bible and produces real-time recommendations during auction.
Recommendations fire within 2 seconds of nomination event.

---

## Model and cost parameters
- Model: `claude-sonnet-4-6` (real-time reasoning)
- Max tokens: 400 per recommendation
- This agent uses `agent_loop.py` (iterative) only if needed — prefer direct call

---

## Tasks

### 1. Draft state tracker
Maintain live state updated after every pick:
- Players drafted: {player_id, price, team_id}
- Per-opponent: roster, budget_remaining, positional_scores, threat_score
- Your roster: picks, prices, remaining budget, slots remaining

### 2. Dependency flag activation
When a player is nominated:
- Check all their dependency flags
- For each flag, check if trigger player is already drafted
- If yes: activate flag, apply value_impact_pct to bid ceiling

Example: McConkey has DISPLACED flag triggered by Allen.
If Allen is already drafted → McConkey's bid ceiling drops by 35%.

### 3. Bid ceiling calculation
Apply live state adjustments on top of pre-computed valuations:
- Active dependency flags
- Remaining budget constraint
- Positional scarcity update (how many of this tier remain)

### 4. Opponent threat scoring
After every pick, update each opponent's:
- Positional strength scores
- Overall threat score (0-100)
- Combo flags (e.g. "Elite RB stack" when 2nd tier-1 RB drafted)

### 5. Block flag logic
```python
if block_value > personal_value and budget_allows_block:
    emit block flag
if opponent_budget_remaining < 15:
    suppress block flag  # they can't afford danger
```

### 6. Nomination suggestion
When it's user's turn: recommend nominating players with HIGH market value
that user does NOT want — drain opponent budgets.

### 7. 2-second response requirement
Recommendation must complete within 2000ms (mocked DB and API).
This is a hard requirement — test it explicitly.

---

## Required test cases
```python
def test_displaced_flag_activates_when_trigger_drafted()
def test_displaced_flag_inactive_when_trigger_not_drafted()
def test_block_flag_fires_on_combo_threat()
def test_block_flag_suppressed_low_opponent_budget()
def test_block_flag_suppressed_insufficient_own_budget()
def test_bid_ceiling_tier1_uses_anchor_weight()
def test_bid_ceiling_tier4_ignores_anchor()
def test_nomination_suggestion_drains_opponent_budget()
def test_budget_summary_accurate_mid_draft()
def test_recommendation_fires_under_2_seconds()
def test_opponent_threat_score_updates_after_pick()
def test_combo_threat_flag_fires_second_elite_rb()
```

---

## Verification before marking complete
1. Recommendation fires in under 2 seconds (all named tests pass)
2. Dependency flags activate correctly when trigger already drafted
3. Block flags fire appropriately, suppressed correctly
4. Budget tracker stays accurate through simulated draft sequence
5. Coverage 80%+

---

## Commit
```
feat(live-draft): implement Live Draft Agent

Real-time recommendations with dependency flag activation.
Opponent threat modeling and block flag logic.
All recommendations under 2 seconds.
Coverage: X%.
```

---
---

# Stage 13: Draft UI (React Frontend)

## Before starting, read:
- `docs/LIVE_DRAFT.md` — Draft UI section

---

## Goal
Full draft room controllable from the app. No need to touch Yahoo's tab during auction.

---

## Tasks

### 1. Ask user for preferences
**ASK USER** before building:
- Color scheme preferences?
- Dark mode or light mode default?
- Any specific layout preferences for the draft board?

### 2. Draft room page components
Build in React + Tailwind + Zustand:

**Nomination panel** (top/center):
- Nominated player name, position, team, headshot if available
- Clock countdown with visual urgency (green → yellow → red)
- Current bid amount

**Recommendation card** (prominent, immediately visible):
- Action badge: BUY (green) / BLOCK (orange) / PASS (gray)
- Bid ceiling amount
- Active dependency flags listed
- 2-3 sentence notes from draft bible
- Market value vs system value comparison

**Bid controls**:
- Current bid display
- Increment (+$1, +$5, +$10) and decrement buttons
- Submit bid button
- Pass button

**Nominate panel**:
- Player search with position filter
- Opening bid selector
- Submit nomination button

**Live draft board** (scrollable):
- All picks so far: player, position, team, price, owner
- Filterable by position

**Opponent budget tracker**:
- All teams listed with remaining budget and roster slots
- Visual indicator when opponent is nearly out of budget

**Your roster panel**:
- Your picks with prices and position

**Alert banner** (top of screen, dismissable):
- Combo threat alerts (red)
- Block flag alerts (orange)
- MANUAL_ACTION_REQUIRED alerts (red, pulsing — cannot be dismissed)

### 3. WebSocket connection
Connect to FastAPI WebSocket endpoint via socket.io-client.
All draft events update Zustand store, which triggers React re-renders.
No polling — pure push from server.

### 4. Action handlers
Bid submit → POST to `/draft/bid`
Nomination submit → POST to `/draft/nominate`
Pass → POST to `/draft/pass`

---

## Verification before marking complete
1. Full mock draft completable without touching Yahoo tab
2. Recommendation appears within 2 seconds of nomination
3. Budget trackers accurate throughout mock draft
4. MANUAL_ACTION_REQUIRED alert is unmissable
5. **ASK USER** to run mock draft and give feedback on UI

---

## Commit
```
feat(draft-ui): implement React draft room UI

Full auction draft control from app.
Real-time recommendations, opponent tracking, alert system.
Mock draft tested successfully.
```

---
---

# Stage 14: Season Roster Store + Post-Draft Sync

## Before starting, read:
- `docs/INSEASON.md`
- `docs/ARCHITECTURE.md` — season_roster table schema

---

## Goal
After draft completes, all drafted players flow into season roster store.
Draft bible records preserved and extended with weekly tracking fields.

---

## Tasks

### 1. Post-draft sync
After draft ends, pull final results from Yahoo API.
Match each pick to draft bible player records.
Populate `season_roster` table:
- `player_id` — linked to draft bible
- `yahoo_team_id` — who owns them
- `acquisition_price` — what was paid
- `acquisition_week` — 0 (draft)

### 2. Initialize weekly tracking arrays
`weekly_stats`, `weekly_snap_counts`, `weekly_target_share` — all initialized as `[]`.

### 3. APScheduler in-season jobs
Register all weekly jobs (don't implement the agents yet — just register the jobs):
- Roster Monitor: Wednesday 8am
- Trade Value: Wednesday 9am
- Opponent Analyzer: Wednesday 10am
- Waiver Wire: Tuesday 11pm
- Beat Reporter: daily 7am (already registered from Stage 8)

### 4. Season roster API endpoints
```
GET /roster/mine                 → your current roster
GET /roster/league               → all teams' rosters
GET /roster/opponent/{team_id}   → specific opponent roster
```

---

## Required test cases
```python
def test_draft_results_synced_to_season_roster()
def test_acquisition_price_stored_correctly()
def test_weekly_arrays_initialized_empty()
def test_scheduler_jobs_registered()
def test_roster_endpoint_returns_correct_players()
```

---

## Commit
```
feat(season-store): implement season roster store and post-draft sync

Draft results synced to season_roster table.
APScheduler weekly jobs registered.
Roster API endpoints implemented.
Coverage: X%.
```

---
---

# Stage 15: Roster Monitor Agent

## Before starting, read:
- `docs/INSEASON.md` — Roster Monitor section
- `docs/rules/COST_RULES.md`

---

## Goal
Weekly data refresh keeps season roster store current.
Sell-high and buy-low flags updated after every week.

---

## Model: `claude-haiku-4-5-20251001`

---

## Tasks

### 1. Weekly stats pull
Every Wednesday: pull stats from Yahoo API for all rostered players.
Update `weekly_stats`, `weekly_snap_counts`, `weekly_target_share` arrays.

### 2. Usage trend detection
Detect snap count dropping 2+ consecutive weeks → set `injury_concern_flag`.
Detect target share rising 2+ consecutive weeks → positive signal.

### 3. Injury report monitoring
Pull Wednesday injury report practice participation from Yahoo API.
Flag any rostered players listed as Limited or DNP.
Set `injury_concern_flag` = true.

### 4. Trade value flags
`sell_high_flag`: recent TDs outpacing target share (TD regression likely).
`buy_low_flag`: recent slump confirmed as matchup-driven by schedule data.
`value_trend`: compare current trade value to last week's.

---

## Required test cases
```python
def test_snap_count_decline_2_weeks_sets_flag()
def test_injury_report_limited_sets_flag()
def test_sell_high_flag_td_spike_low_targets()
def test_buy_low_flag_matchup_slump()
def test_value_trend_updated_weekly()
def test_weekly_arrays_appended_not_replaced()
```

---

## Commit
```
feat(roster-monitor): implement Roster Monitor Agent

Weekly stats sync, usage trend detection, injury flags.
Sell-high and buy-low flags automated.
Coverage: X%.
```

---
---

# Stage 16: Opponent Analyzer Agent

## Before starting, read:
- `docs/INSEASON.md` — Opponent Analyzer section

---

## Goal
Running profiles on all other managers, updated weekly.
Management style detection enables acceptance probability modeling for trades.

---

## Model: `claude-sonnet-4-6` (behavioral reasoning required)

---

## Tasks

### 1. Per-opponent profile
Build and maintain in DB (new table `opponent_profiles`):
```json
{
  "team_id": "...",
  "team_name": "...",
  "positional_scores": {},
  "threat_score": 0,
  "apparent_management_style": "reactive|analytical|name_brand|urgency_driven",
  "roster_vulnerabilities": [],
  "trade_history": [],
  "current_record": "",
  "playoff_position": 0
}
```

### 2. Management style detection
- **Reactive**: frequently starts players off big recent games
- **Name-brand biased**: holds big names past their value
- **Analytical**: trade offers show schedule/usage awareness
- **Urgency-driven**: losing streak = willing to overpay

### 3. Vulnerability detection
- Bye week conflicts (multiple starters on same bye)
- Injury exposure (multiple high-risk players)
- Playoff schedule problems (brutal weeks 14-17)
- Positional weakness (bottom-tier at a starting position)

### 4. Threat score
0-100 composite of roster quality, updated after every weekly sync.

---

## Required test cases
```python
def test_management_style_reactive_detected()
def test_management_style_analytical_detected()
def test_bye_conflict_vulnerability_detected()
def test_playoff_schedule_vulnerability_detected()
def test_threat_score_updates_weekly()
def test_opponent_profile_created_for_all_teams()
```

---

## Commit
```
feat(opponent-analyzer): implement Opponent Analyzer Agent

Per-opponent profiles with management style and vulnerability detection.
Threat scores updated weekly. Coverage: X%.
```
