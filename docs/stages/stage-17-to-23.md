# Stage 17: Trade Value Agent

## Before starting, read:
- `docs/INSEASON.md` — Trade Value section
- `docs/rules/COST_RULES.md`

---

## Goal
Weekly player valuations for every player in the league (not just your roster).
Buy-low and sell-high signals identify trade opportunities before opponents notice.

---

## Model: `claude-haiku-4-5-20251001`

---

## Tasks

### 1. Current trade value calculation
For every player in the league, compute current trade value from:
- Recent performance trend (last 3 weeks weighted, most recent heaviest)
- Injury risk modifier (from draft bible, updated by Roster Monitor)
- Remaining schedule quality (from schedule agent data)
- Situation score (system still intact?)
- Weeks remaining in season (affects floor importance)

### 2. Buy-low signal detection
Fire `buy_low_flag` when:
- Recent slump confirmed as matchup-driven (favorable matchups coming)
- Snap count temporarily reduced due to game script (not role change)
- Returning from injury — conservative snaps, value will recover
- Recency bias clearly suppressing perceived value

### 3. Sell-high signal detection
Fire `sell_high_flag` when:
- TD production outpacing target share (TDs regress toward targets)
- Snap count quietly declining 2+ weeks (role change incoming)
- Upcoming brutal schedule opponent hasn't noticed
- Overperforming efficiency metrics — can't sustain

### 4. Valuation asymmetry per opponent
For each opponent, compare your system valuation vs their likely valuation
(based on their management style from Opponent Analyzer).
Store top 3 asymmetry opportunities per opponent.

---

## Required test cases
```python
def test_sell_high_flag_td_outpacing_targets()
def test_sell_high_flag_snap_count_decline()
def test_buy_low_flag_matchup_driven_slump()
def test_buy_low_flag_injury_return()
def test_asymmetry_detected_reactive_manager()
def test_trade_value_updates_weekly()
def test_all_league_players_have_current_value()
```

---

## Commit
```
feat(trade-value): implement Trade Value Agent

Weekly valuations for all league players.
Buy-low and sell-high signals automated.
Per-opponent valuation asymmetry detection.
Coverage: X%.
```

---
---

# Stage 18: Trade Analyzer

## Before starting, read:
- `docs/INSEASON.md` — Trade Analyzer section
- `docs/ARCHITECTURE.md`

---

## Goal
On-demand trade analysis. User submits a trade, gets back verdict,
timing flags, acceptance probability, and counter proposals.

---

## Model: `claude-sonnet-4-6`
## Max tokens: 1500

---

## Tasks

### 1. API endpoint
```
POST /trades/analyze
Body: {
  "give": [player_id, ...],
  "receive": [player_id, ...],
  "opponent_team_id": "..."
}
```

### 2. Fairness analysis
Compare system values of both sides.
Apply roster context adjustment — is this position of need or surplus?
State clearly: favorable / slightly_favorable / fair / slightly_unfavorable / unfavorable

### 3. Timing flags
Check Trade Value Agent's current signals on all players in the trade:
- SELL_HIGH on received player = warning (you're being sold high on)
- BUY_LOW on given player = warning (you'd be selling low)
- State both sides explicitly

### 4. Acceptance probability
Pull opponent profile from Opponent Analyzer.
Estimate probability (0.0-1.0) and state reason clearly:
"High — they're 3-4 and thin at your surplus position"
"Low — they have no clear need for what you're offering"

### 5. Counter proposals
If verdict is unfavorable:
Find nearest adjustment (swap one player, adjust ask) that flips to favorable
while keeping acceptance_probability above 0.40.
Return up to 3 ranked counter proposals.

### 6. React UI
Trade input form: two columns (give / receive), player search for each side.
Analysis results: verdict badge, timing flags, acceptance probability bar,
counter proposals as clickable alternatives.

---

## Required test cases
```python
def test_lopsided_trade_unfavorable_verdict()
def test_balanced_trade_fair_verdict()
def test_sell_high_flag_on_received_player()
def test_buy_low_flag_on_given_player()
def test_acceptance_probability_high_needy_opponent()
def test_acceptance_probability_low_no_need()
def test_counter_proposal_flips_to_favorable()
def test_counter_proposal_acceptance_above_threshold()
def test_roster_context_adjusts_verdict()
```

---

## Verification before marking complete
1. Submit known lopsided trade → unfavorable verdict
2. Submit balanced trade → fair verdict
3. Counter proposals make intuitive sense
4. **ASK USER** to test with a few real trade scenarios and give feedback

---

## Commit
```
feat(trade-analyzer): implement Trade Analyzer

Fairness analysis with roster context, timing flags, acceptance probability.
Counter proposal generation. React UI complete.
Coverage: X%.
```

---
---

# Stage 19: Trade Proposal Engine

## Before starting, read:
- `docs/INSEASON.md` — Trade Proposal section

---

## Goal
Proactive weekly trade suggestions. Surfaces only proposals with
realistic acceptance probability, not theoretical best trades.

---

## Model: `claude-sonnet-4-6`
## Max tokens: 2000

---

## Tasks

### 1. Weekly proposal generation
Every Wednesday after Roster Monitor, Trade Value, and Opponent Analyzer complete:
- Cross-reference your surplus positions vs each opponent's weakness
- Check Trade Value asymmetry flags
- Filter to proposals with acceptance_probability > 0.40

### 2. Proposal calibration per opponent
Use management style from Opponent Analyzer to frame proposals:
- Reactive manager: lead with recent performance, not projections
- Name-brand biased: offer recognizable names when possible
- Urgency-driven: highlight their upcoming schedule problems
- Analytical: straightforward value comparison works

### 3. Ranking
Sort proposals by: (acceptance_probability × value_advantage).
Surface top 5 per week maximum — don't overwhelm.

### 4. React UI
Weekly suggestions list:
- Proposed trade with both sides
- Value advantage for you
- Acceptance probability
- One-sentence rationale
- "Use in Trade Analyzer" button (pre-fills analyzer with this proposal)

---

## Required test cases
```python
def test_proposal_generated_for_known_asymmetry()
def test_low_acceptance_proposals_filtered_out()
def test_proposals_ranked_by_value_times_probability()
def test_framing_adjusted_for_reactive_manager()
def test_max_5_proposals_returned()
```

---

## Commit
```
feat(trade-proposal): implement Trade Proposal Engine

Weekly proactive suggestions with acceptance filtering.
Opponent-style calibration. React UI complete.
Coverage: X%.
```

---
---

# Stage 20: Lineup Optimizer

## Before starting, read:
- `docs/INSEASON.md` — Lineup Optimizer section
- `docs/rules/COST_RULES.md`

---

## Goal
Weekly start/sit recommendations with clear reasoning.
User can override any decision with full context.

---

## Model: `claude-haiku-4-5-20251001`
## Max tokens: 1000

---

## Tasks

### 1. Weekly scoring
Every Thursday after injury reports and Vegas lines are set.
Score each rostered player for the week:

Inputs (pre-aggregated in Python before API call):
- Matchup grade (from schedule agent data for this specific opponent)
- Vegas implied team total (scrape from free odds site)
- Practice participation (Wed/Thu/Fri reports from Beat Reporter)
- Recent snap count trend (from Roster Monitor)
- Weather data (outdoor stadiums, wind >15mph)

### 2. Vegas implied totals
Scrape from a free odds aggregator.
Vegas implied total is the most predictive single weekly input.
High implied total = elevated ceiling for all players on that team.

### 3. Lineup optimization
Given Yahoo roster slot rules (pulled from league settings in Stage 10),
find optimal starting lineup by position.
Handle flex slots — optimize RB/WR/TE flex for maximum projected score.

### 4. Output format
Per player: start/sit recommendation, confidence (High/Medium/Low), key reasons.
Always explain. User must be able to make informed overrides.

Never just output a lineup — always output reasoning.

### 5. React UI
Lineup card: all roster slots with start/sit badge and reasoning.
"Lock" toggle to manually lock a player into lineup regardless of recommendation.

---

## Required test cases
```python
def test_high_vegas_total_boosts_player_score()
def test_weather_penalty_applied_passing_game()
def test_injury_report_limited_reduces_confidence()
def test_flex_slot_optimized_correctly()
def test_reasoning_always_included_in_output()
def test_locked_player_not_overridden()
```

---

## Commit
```
feat(lineup-optimizer): implement Lineup Optimizer

Weekly start/sit with Vegas lines, matchups, and injury reports.
Reasoning always included. React UI with lock toggle.
Coverage: X%.
```

---
---

# Stage 21: Waiver Wire Agent

## Before starting, read:
- `docs/INSEASON.md` — Waiver Wire section
- `docs/rules/COST_RULES.md`

---

## Goal
Weekly waiver pickup recommendations identifying emerging players
before the market prices them in.

---

## Model: `claude-haiku-4-5-20251001`
## Max tokens: 800

---

## Tasks

### 1. Available player identification
Pull all unrostered players from Yahoo API.
Filter to players with projected value window of 2+ weeks.

### 2. Signal detection
- Snap count spike in most recent game (new role emerging)
- Depth chart promotion due to injury above them
- Usage pattern change (target share jump, carry spike)
- Beat reporter signal about increased role
- Favorable schedule next 2-3 weeks

### 3. Value window estimation
How many weeks is this player relevant?
Injury fill-in: estimate based on typical recovery timeline.
Emerging role: flag as ongoing if snap count trend is clear.
One-week streaming: label clearly, don't oversell.

### 4. React UI
Waiver list: ranked recommendations with signal type, value window, confidence.
Filter by position. "Add" button links to Yahoo waiver wire.

---

## Required test cases
```python
def test_snap_count_spike_flagged()
def test_depth_chart_promotion_flagged()
def test_one_week_streamers_labeled_correctly()
def test_value_window_estimated()
def test_already_rostered_players_excluded()
```

---

## Commit
```
feat(waiver-wire): implement Waiver Wire Agent

Snap count and usage pattern signal detection.
Value window estimation. React UI complete.
Coverage: X%.
```

---
---

# Stage 22: Pipeline Admin UI

## Goal
User can trigger agent pipeline runs and monitor data freshness from the app.

---

## Tasks

### 1. Backend endpoints
```
GET /admin/pipeline-status    → last run time per agent, data freshness
POST /admin/pipeline/run      → trigger specific agent (body: {agent, team?})
GET /admin/cost-report        → usage summary from api_usage_log
GET /admin/cost-report/weekly → weekly cost breakdown
```

### 2. React admin page
- Pipeline status dashboard: each agent with last run timestamp and freshness indicator
- Manual trigger buttons per agent (with dry-run option)
- Cost report: total season spend, per-agent breakdown, per-week chart
- Data freshness warnings: alert when any agent hasn't run within expected window

### 3. Freshness thresholds
- Team Systems: warn if >30 days since last run
- Roster Changes: warn if >7 days
- Beat Reporter: warn if >2 days
- All agents: warn if >1 day within 1 week of draft

---

## Verification
1. **ASK USER** to trigger each agent from the UI and confirm it runs
2. Cost report shows accurate historical spend
3. Freshness warnings fire correctly

---

## Commit
```
feat(pipeline-ui): implement pipeline admin UI

Per-agent status, manual triggers, cost reporting.
Data freshness warnings for pre-draft monitoring.
```

---
---

# Stage 23: Final Integration, Testing, and Deployment

## Before starting, read:
- `docs/rules/GIT_RULES.md`
- `docs/ARCHITECTURE.md`

---

## Goal
Everything deployed, tested end-to-end, ready for real draft.
Two mock drafts through the app before the real thing.

---

## Tasks

### 1. Railway production setup
**ASK USER** to confirm Railway account is set up with:
- Postgres instance provisioned
- All production environment variables set
- GitHub repo connected for auto-deploy

**ASK USER** — review all env vars in Railway dashboard:
`ANTHROPIC_API_KEY`, `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`,
`YAHOO_LEAGUE_ID`, `YAHOO_REFRESH_TOKEN`, `DATABASE_URL`, `SECRET_KEY`

### 2. GitHub Actions CI
Verify `.github/workflows/ci.yml` is configured and passing on all branches.
**ASK USER** to add `ANTHROPIC_API_KEY_TEST` as GitHub Actions secret.

### 3. Production database migration
Run `alembic upgrade head` against production Railway Postgres.
Verify all tables created correctly.

### 4. Full pipeline run in production
**ASK USER** to approve cost estimate from `--dry-run` first.
Run full pre-draft pipeline in production.
Verify draft bible populated correctly.

### 5. Mock draft 1
**ASK USER** to organize a mock draft with the Playwright bridge.
Checklist:
- [ ] Nomination detection fires in under 100ms
- [ ] Bid placement works correctly
- [ ] Budget trackers stay accurate
- [ ] Dependency flags activate correctly
- [ ] Block flags fire when appropriate
- [ ] MANUAL_ACTION_REQUIRED fires on simulated bridge failure
- [ ] No crashes, no silent failures

Fix all issues before mock draft 2.

### 6. Mock draft 2
Second mock draft after all fixes from mock draft 1.
All checklist items must pass again.

### 7. Final pipeline refresh
Morning of real draft:
- Run Beat Reporter agent
- Run Roster Changes agent (catch any late moves)
- Run `scripts/refresh_market_values.py` (FantasyPros)
- Verify all player records have `last_updated` within 24 hours

### 8. Draft day checklist
**ASK USER** to confirm before draft starts:
- [ ] App is running and accessible
- [ ] Yahoo OAuth token is fresh (not expired)
- [ ] Playwright bridge connects to draft room
- [ ] All 32 teams have current system grades
- [ ] Top 200 players have complete draft bible records
- [ ] Market values updated within 24 hours
- [ ] Backup plan confirmed: if bridge fails, user knows to bid manually in Yahoo tab

---

## Final verification
1. Both mock drafts completed successfully
2. All CI checks passing
3. Full unit test suite green
4. Production pipeline data fresh
5. **ASK USER**: are you confident going into the real draft?

---

## Commit
```
chore(deployment): production deployment and pre-draft verification

Full pipeline run complete. Two mock drafts passed.
All systems verified for real draft.
```
