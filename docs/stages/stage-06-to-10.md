# Stage 6: Injury Risk Agent

## Before starting, read:
- `docs/AGENTS.md` — Agent 4: Injury Risk spec
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`

---

## Goal
Every draftable player has a risk profile and `risk_adjusted_value_modifier` in DB.
These modifiers are applied to `risk_adjusted_value` in the players table.

---

## Model and cost parameters
- Model: `claude-haiku-4-5-20251001`
- Max tokens: 1000 per team batch
- Total API calls: 32

---

## Tasks

### 1. Injury categorization
Implement all injury categories from `docs/AGENTS.md`:
- `soft_tissue` — HIGH recurrence (hamstring, groin, calf, hip flexor)
- `ligament_acl` — MODERATE
- `high_ankle_sprain` — MODERATE (underrated lingering effect)
- `fracture_traumatic` — LOW (heals cleanly, recency flag only)
- `fracture_stress` — MODERATE (biomechanical issue indicator)
- `concussion` — SPECIAL (count total career, 2+ = compounding modifier)
- `chronic` — ONGOING (turf toe, back, plantar fasciitis — does not reset)

### 2. Pattern flags
Auto-detect and set:
- `RECURRING_SOFT_TISSUE` — 2+ soft tissue to same area within 3 seasons
- `CONCUSSION_HISTORY` — 2+ documented concussions
- `HIGH_MILEAGE` — RB with 600+ career carries
- `POST_ACL` — within 18 months of ACL return
- `CHRONIC_CONDITION` — any chronic issue present
- `WORKLOAD_CLIFF` — coming off 300+ carry season

### 3. Age risk multiplier
Under 26: 1.0x | 26-28: 1.1x | 29-30: 1.25x | 31+: 1.5x
Applied to base risk modifier before writing final value.

### 4. Risk-adjusted value modifier
Apply to players table `risk_adjusted_value` field:
- Low: 0 to -0.05
- Moderate: -0.10 to -0.20
- High: -0.20 to -0.35
- Volatile: -0.35 or worse

### 5. Data sources
Pull injury history from `nfl_data.get_injury_data(season)` for each of `get_analysis_seasons()`.
Never hardcode seasons.

---

## Required test cases
```python
def test_soft_tissue_single_event_moderate_flag()
def test_soft_tissue_two_same_area_three_seasons_high_flag()
def test_soft_tissue_pattern_flag_set()
def test_acl_recent_post_acl_flag()
def test_fracture_traumatic_low_risk()
def test_fracture_stress_moderate_risk()
def test_concussion_single_no_compounding()
def test_concussion_two_plus_compounding_modifier()
def test_chronic_condition_does_not_reset()
def test_workload_cliff_300_plus_carries()
def test_high_mileage_600_plus_career_carries()
def test_age_multiplier_under_26_baseline()
def test_age_multiplier_31_plus_elevated()
def test_risk_modifier_applied_to_risk_adjusted_value()
def test_no_hardcoded_years()
def test_single_api_call_per_team()
```

---

## Verification before marking complete
1. High-mileage RBs have appropriate flags
2. Known soft-tissue-prone players have RECURRING_SOFT_TISSUE
3. Risk modifiers applied to `risk_adjusted_value` in players table
4. No hardcoded years
5. All tests passing, coverage 80%+

---

## Commit
```
feat(injury-risk): implement Injury Risk Agent

All injury categories and pattern flags implemented.
Age risk multiplier applied. Risk modifiers written to players table.
Coverage: X%.
```

---
---

# Stage 7: Schedule Agent

## Before starting, read:
- `docs/AGENTS.md` — Agent 5: Schedule spec
- `docs/rules/COST_RULES.md`
- `docs/rules/PATTERNS.md`

---

## Goal
Every player has schedule grades across three windows: early (weeks 1-6),
full season, and playoff (weeks 14-17). Playoff grade is a first-class DB column.

---

## Model and cost parameters
- Model: `claude-haiku-4-5-20251001`
- Max tokens: 1500 per team batch (800 is insufficient — 3-position JSON with playoff_matchups arrays requires ~1100-1200 tokens)
- Total API calls: 32

---

## Tasks

### 1. Three windows — all stored as separate columns
- `early_window_grade` — weeks 1-6
- `full_season_grade` — full season
- `playoff_window_grade` — weeks 14-17 — **FIRST-CLASS COLUMN, NOT NOTES**

If `playoff_window_grade` only appears in a text notes field, that is a bug.

### 2. Defensive grade construction
Do NOT use last year's raw defensive stats directly.
Adjust for:
- FA losses/additions weighted by position relevance
  (corner departure = WR schedule penalty, DT departure = RB schedule penalty)
- Draft picks by projected role
- Coordinator changes (scheme impact on position-specific grades)

Build separate grades: vs WR1, vs slot WR, vs TE, vs RB rushing, vs RB receiving.

### 3. Additional factors
- Bye week stored as `bye_week` (integer week number)
- Bye conflict detection: flag if bye matches other projected roster players
- Weather risk: BUF, GB, CHI, NE, CLE, PIT — Nov/Dec outdoor passing modifier
- Divisional game weeks: mild suppression flag

### 4. Schedule score
Composite `schedule_score` (1-10) combining all windows weighted:
playoff window > early window > full season (for most players).

---

## Required test cases
```python
def test_playoff_grade_is_first_class_column_not_notes()
def test_early_window_correct_weeks_1_to_6()
def test_playoff_window_correct_weeks_14_to_17()
def test_defensive_grade_adjusted_for_fa_departure()
def test_weather_flag_outdoor_cold_city_november()
def test_bye_week_stored_correctly()
def test_position_specific_grades_stored_separately()
def test_no_hardcoded_years()
def test_single_api_call_per_team()
```

---

## Verification before marking complete
1. All players have all three schedule window grades
2. Playoff grades exist as queryable columns
3. Weather flags applied to correct teams
4. No hardcoded years
5. All tests passing, coverage 80%+

---

## Commit
```
feat(schedule): implement Schedule Agent

Three-window schedule analysis with playoff grade as first-class field.
Defensive grades adjusted for offseason changes.
Weather risk and bye week tracking implemented.
Coverage: X%.
```

---
---

# Stage 8: Beat Reporter Agent

## Before starting, read:
- `docs/AGENTS.md` — Agent 6: Beat Reporter spec
- `docs/rules/COST_RULES.md`

---

## Goal
Pre-draft news ingestion running daily. Draft bible notes updated with last-mile signals.

---

## Model and cost parameters
- Model: `claude-haiku-4-5-20251001`
- Max tokens: 300 per signal
- Run schedule: daily via APScheduler

---

## Tasks

### 1. RSS feed ingestion
Pull from beat reporter RSS feeds:
- ESPN team pages (one per team)
- NFL.com team news
- Rotowire transaction feed

Parse articles, extract player names, classify signal type:
- `practice_limited` — player reported limited/DNP
- `depth_chart_change` — official or reported
- `injury_flag` — any injury mention
- `camp_standout` — emerging role signal
- `transaction` — move not yet in OverTheCap

### 2. APScheduler job
```python
scheduler.add_job(
    beat_reporter_agent.run,
    'cron',
    hour=7,  # 7am daily
    id='beat_reporter_daily'
)
```

### 3. Player name entity recognition
Match article mentions to player records in DB.
Write signals to `beat_reporter_signals` table.
Update `notes` and `last_updated` on affected player records.

### 4. Injury report integration
Pull official NFL injury reports (PDF, Wed-Fri).
Flag any rostered players listed as Limited or DNP.
Feed into Injury Risk agent's `recovery_assessment` field.

---

## Required test cases
```python
def test_rss_feed_parsed_correctly()
def test_player_name_matched_to_db_record()
def test_signal_written_to_beat_reporter_signals_table()
def test_player_notes_updated_after_signal()
def test_scheduler_job_registered()
def test_duplicate_signals_not_written_twice()
```

---

## Verification before marking complete
1. Run agent manually — signals appear in `beat_reporter_signals`
2. At least one player record `notes` field updated from a feed signal
3. Scheduler job registered and fires on schedule
4. All tests passing, coverage 80%+

---

## Commit
```
feat(beat-reporter): implement Beat Reporter Agent

RSS feed ingestion, player name matching, APScheduler daily job.
beat_reporter_signals table populated. Player notes updated.
Coverage: X%.
```

---
---

# Stage 9: Draft Bible Valuation Pass

## Before starting, read:
- `docs/ARCHITECTURE.md` — Two-Value Auction System section
- `docs/rules/COST_RULES.md`

---

## Goal
Every player in the draft bible has complete valuation fields computed.
All agent outputs synthesized into bid ceilings and tier assignments.
No AI calls needed — pure Python computation.

---

## Tasks

### 1. seasons.py for analysis year
```python
from backend.utils.seasons import get_analysis_year
ANALYSIS_YEAR = get_analysis_year()
```
All valuation records stamped with dynamic analysis year.

### 2. Tier assignment
Assign tiers 1-5 per position based on projected production:
- Tier 1: top 3 per position (elite, positional scarcity applies)
- Tier 2: next 6 (strong starter)
- Tier 3: next 10 (solid starter)
- Tier 4: next 15 (streamer/backup)
- Tier 5: remainder (depth only)

### 3. Baseline value calculation
From Player Profiles clean season baseline PPR points,
map to auction dollar value using league budget and player universe.

### 4. Bid ceiling calculation
Implement exact formulas from `docs/ARCHITECTURE.md`:

Tier 1: `(system_value × (1 - anchor)) + (market_value × anchor) × scarcity × risk_modifier`
Tier 2-3: `(system_value × 0.85 + market_value × 0.15) × risk_modifier`
Tier 4-5: `system_value × risk_modifier`

Anchor weights: T1=0.80, T2=0.40, T3=0.15, T4-5=0.00
Scarcity: T1 RB=1.35, T1 WR=1.20, T1 QB=1.10, T2+=1.00

### 5. Market value refresh
**ASK USER** to run `scripts/refresh_market_values.py` to pull current FantasyPros data.
Market values must be current within 72 hours of the draft.

### 6. Value gap computation
`value_gap = system_value - market_value`
`value_gap_signal`: "market_overvalues" if gap < -5, "market_undervalues" if gap > 5, "aligned" otherwise

---

## Required test cases
```python
def test_tier1_bid_ceiling_uses_anchor_weight()
def test_tier4_bid_ceiling_ignores_market_value()
def test_risk_modifier_reduces_ceiling_correctly()
def test_scarcity_modifier_applied_to_tier1_rb()
def test_value_gap_signal_market_overvalues()
def test_value_gap_signal_market_undervalues()
def test_all_top_200_players_have_bid_ceiling()
def test_let_go_threshold_is_ceiling_plus_15_pct()
def test_analysis_year_dynamic_not_hardcoded()
```

---

## Verification before marking complete
1. **ASK USER** to review bid ceilings for 10 known players — do they look reasonable?
2. Tier distribution looks realistic — not everyone tier 1
3. Value gap signals make sense for known over/undervalued players
4. All tests passing, coverage 80%+

---

## Commit
```
feat(valuations): implement draft bible valuation pass

Two-value bid ceiling system implemented.
Tier assignment, scarcity modifiers, risk adjustments all applied.
All top-200 players have complete valuation fields.
Coverage: X%.
```

---
---

# Stage 10: Yahoo API Integration

## Before starting, read:
- `docs/LIVE_DRAFT.md` — Yahoo Official API section
- `docs/rules/GIT_RULES.md`

---

## Goal
League data, rosters, and player universe pulling from Yahoo official API.
Player IDs matched to draft bible records.

---

## Tasks

### 1. Yahoo Developer account setup
**ASK USER** — required before any code:
1. Do you have a Yahoo Developer account?
   - If not: developer.yahoo.com → create account → New App → Fantasy Sports scope
2. Provide `YAHOO_CLIENT_ID` and `YAHOO_CLIENT_SECRET`
3. Provide `YAHOO_LEAGUE_ID` (found in league URL)

### 2. OAuth 2.0 implementation
```python
# backend/integrations/yahoo_api.py
async def get_authorization_url() -> str
async def exchange_code_for_tokens(code: str) -> dict
async def refresh_access_token() -> str
async def get_league() -> dict
async def get_teams() -> list[dict]
async def get_players(count: int = 300) -> list[dict]
async def get_draft_results() -> list[dict]
async def get_rosters() -> dict
```

### 3. Auth routes
```python
GET /auth/yahoo           → redirect to Yahoo
GET /auth/yahoo/callback  → exchange code, store refresh token
```

**ASK USER** to run the OAuth flow once — they need to click through the browser.
Store `YAHOO_REFRESH_TOKEN` in `.env` after first successful auth.

### 4. Player ID matching
Match Yahoo player IDs to draft bible records.
Write `yahoo_player_id` to `players` table.
Handle name variations and mismatches gracefully — log unmatched players.

### 5. League settings
Pull and store: scoring format (PPR/0.5PPR/standard), roster slots, auction budget,
team count, playoff weeks. These configure the valuation formulas.

---

## Required test cases
```python
def test_oauth_url_generated_correctly()
def test_token_refresh_updates_stored_token()
def test_get_players_returns_list()
def test_player_id_matched_to_draft_bible()
def test_unmatched_players_logged_not_crashed()
def test_league_settings_stored()
```

---

## Verification before marking complete
1. **ASK USER** to complete OAuth flow
2. Can retrieve league team names
3. Player IDs matched to draft bible — **ASK USER** to check a few known players
4. League settings stored and correct
5. All tests passing, coverage 80%+

---

## Commit
```
feat(yahoo-api): implement Yahoo Fantasy API integration

OAuth 2.0 flow, league data, roster sync.
Player IDs matched to draft bible records.
League settings stored for valuation configuration.
Coverage: X%.
```
