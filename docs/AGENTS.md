# Pre-Draft Agent Specifications

Six research agents run before draft day. They run in order — Team Systems first,
others can parallelize after. Each writes structured output to the PostgreSQL draft bible.

All agents must:
- Import season years from `backend.utils.seasons` — never hardcode
- Use `BaseAgent.call_once()` — never `run_agent()`
- Batch by team — one API call per team maximum
- Pre-aggregate all data in Python before the API call
- See `docs/rules/COST_RULES.md` and `docs/rules/PATTERNS.md`

---

## Agent 1: Team Systems

**Model:** `claude-haiku-4-5-20251001` (data extraction)
**Max tokens:** 500 per team
**Total API calls:** 32 (one per NFL team)

**Purpose:** Grade every NFL team as an offensive system. Runs first.
Output inherited by all other agents — do not run other agents until this completes.

**Data inputs (pre-aggregated in Python):**
- O-line pass protection grade (proxy from pressure rate allowed, sack rate)
- O-line run blocking grade (adjusted line yards)
- QB: starter name, experience years, CPOE, air yards per attempt, pressure performance
- OC: name, scheme tendencies from last 3 coaching stops, run/pass split, personnel groupings
- Red zone usage patterns

**Key logic:**
- `rookie_qb_flag`: true for any first-year NFL starter
- `compound_risk_flag`: true when `rookie_qb_flag` AND pass protection grade C or below
  → This flag cascades as a severe system penalty to ALL skill positions on that roster
- Split O-line into pass protection AND run blocking separately — they don't always correlate

**Output per team → `team_systems` table:**
```json
{
  "team_abbr": "LAC",
  "pass_protection_grade": "B+",
  "run_blocking_grade": "B",
  "qb_name": "Justin Herbert",
  "qb_tier": "solid",
  "qb_experience_years": 6,
  "qb_pressure_performance": "above_avg",
  "qb_cpoe": 2.4,
  "qb_air_yards_per_attempt": 8.2,
  "qb_downfield_aggressiveness": "moderate",
  "rookie_qb_flag": false,
  "compound_risk_flag": false,
  "oc_name": "[Name]",
  "oc_scheme": "balanced",
  "oc_run_pass_split_tendency": 0.52,
  "personnel_tendency": "11",
  "red_zone_philosophy": "wr1",
  "system_ceiling": "high",
  "system_grade": "A-",
  "notes": "2-3 sentence summary"
}
```

---

## Agent 2: Roster Changes

**Model:** `claude-sonnet-4-6` (causal reasoning required)
**Max tokens:** 2000 per team
**Total API calls:** 32

**Purpose:** Track every offseason transaction and reason through downstream consequences.
The McConkey/Allen scenario is the canonical test case.
NFL draft picks are treated as a special transaction category requiring full prospect evaluation.

**The McConkey/Allen chain (canonical example):**
```
Event: Keenan Allen signs with LAC
→ Allen's historical role: slot receiver, 27% target share with Herbert
→ McConkey's role: slot receiver, same alignment as Allen
→ Conclusion: direct role overlap → DISPLACED flag on McConkey
→ Both sides flagged: McConkey also gets CONTINGENT flag (value rises if Allen absent)
```

**Dependency flag types:**
- `displaced`: Role directly overlapped by new arrival (negative when trigger is healthy)
- `contingent`: Value tied to trigger player's health (positive modifier when trigger is out)
- `beneficiary`: Clear value increase when trigger is absent
- `committee`: RB sharing backfield, snap share unclear
- `scheme_fit`: Player profile mismatches new OC tendency
- `college_trust`: QB/WR college connection on same NFL roster (positive, especially Year 1)

**Always flag BOTH sides of a displacement:**
Displaced player gets DISPLACED (negative) + CONTINGENT (positive if trigger absent).

**QB Trust Model:**
- Build trust score (0-100) for every QB/receiver pairing
- NFL shared history = primary source
- College shared history = secondary source (~70% weight)
- College flag especially important for rookie QBs in Year 1 — default to college targets under pressure

**Output → `player_dependencies` table (multiple records per player):**
```json
{
  "player_name": "Ladd McConkey",
  "player_team": "LAC",
  "player_position": "WR",
  "flag_type": "displaced",
  "trigger_player_name": "Keenan Allen",
  "trigger_player_team": "LAC",
  "trigger_condition": "active_and_healthy",
  "effect_on_value": "negative",
  "value_impact_pct": -0.35,
  "confidence": "high",
  "reasoning": "Allen commands 27% historical target share with Herbert. Direct slot role overlap.",
  "season_year": 2026
}
```

**Canonical test — must pass before stage is complete:**
```bash
pytest tests/unit/agents/test_roster_changes.py::test_mcconkey_allen_displacement -v
```

---

### NFL Draft Picks — Special Transaction Handling

Draft picks are not the same as free agent signings. A first-round WR in an
elite system with a 40% SEC dominator rating is a completely different asset
than a sixth-round WR with a 20% MAC dominator rating on a bad offense.
The agent must trigger a full prospect evaluation for every draft pick.

**Pipeline when `transaction_type == "draft"` is detected:**

**Step 1 — College profile (pre-aggregated, no extra API call):**
```python
college_profile = {
  "dominator_rating": 0.42,       # Raw % of team's receiving production
  "target_share": 0.31,
  "yards_per_route_run": 2.8,
  "conference": "SEC",
  "conference_multiplier": 1.00,
  "adjusted_dominator": 0.42,     # dominator × conference_multiplier
}
```

**Step 2 — Draft capital:**
```python
capital_value = get_draft_capital_value(round, pick_number)
# Pick 1 = 100, Pick 32 = 74, Pick 33 = 73 ... Pick 256 = 1
capital_signal = "high" if capital_value >= 70 else "medium" if capital_value >= 40 else "low"
```

**Step 3 — Conference adjustment:**
Apply multiplier to dominator_rating before comparing across prospects.
SEC = 1.00 (baseline), MAC = 0.80, full table in `docs/stages/stage-02-data-ingestion.md`.

**Step 4 — Historical comps (from pre-loaded comp table):**
Find the 3-5 most similar drafted players from the last 8-10 seasons.
Similarity based on: adjusted_dominator, capital_value, position, age_at_draft.
Pull their actual NFL outcomes: PPR points per game in Year 1, Year 2, Year 3.

**Step 5 — Landing spot modifier:**
```python
LANDING_SPOT_MODIFIERS = {
    "compound_risk": 0.75,    # Rookie QB + bad line — worst case for skill positions
    "rookie_qb":    0.85,     # Rookie QB alone
    "A_system":     1.18,     # Elite offense
    "B_system":     1.08,
    "C_system":     1.00,     # Baseline
    "D_system":     0.88,
    "F_system":     0.78,
}
```

**Step 6 — College profile grade:**
```python
# WR / TE grading
if adjusted_dominator >= 0.38 and yards_per_route >= 2.8: grade = "elite"
elif adjusted_dominator >= 0.30 or yards_per_route >= 2.5: grade = "strong"
elif adjusted_dominator >= 0.22: grade = "average"
else: grade = "weak"
```

**Step 7 — Write rookie evaluation fields to player record:**
```json
{
  "is_rookie": true,
  "college_profile_grade": "elite",
  "draft_capital_signal": "high",
  "draft_capital_value": 81,
  "adjusted_dominator_rating": 0.42,
  "conference": "SEC",
  "historical_comp_names": ["Ja'Marr Chase", "Justin Jefferson", "CeeDee Lamb"],
  "comp_yr1_avg_ppg": 16.4,
  "comp_yr2_avg_ppg": 19.8,
  "landing_spot_modifier": 1.15,
  "projection_confidence": "low",
  "variance_flag": true
}
```

**Step 8 — Displacement flags for incumbents:**
- High capital picks (rounds 1-2): generate DISPLACED + CONTINGENT for incumbent at same position
- Medium capital picks (rounds 3-4): generate DISPLACED with lower confidence
- Low capital picks (rounds 5-7): no displacement flags — rarely unseat starters

---

## Agent 3: Player Profiles (Synthesis Agent)

**Model:** Mixed — Haiku batch (32 calls) + Sonnet per complex player (~80-120 calls)
**Max tokens:** 4000 (Haiku batch), 800 (Sonnet individual)
**Total API calls:** ~120
**Pipeline position:** 6th (runs AFTER all upstream agents, before valuation)

**Purpose:** Synthesize ALL upstream agent outputs into forward-looking PPR projections.
Runs last so it has access to: team systems, dependency flags, injury risk profiles,
schedule grades, and beat reporter signals. Complex players (rookies, flagged,
contract year, high injury risk) get Sonnet for causal reasoning; stable veterans
get Haiku batch processing.
Uses rookie evaluation fields written by Agent 2 for first-year players.

**Role classifications:**
WR: `wr1_alpha`, `slot_specialist`, `deep_threat`, `possession_wr2`, `gadget`
RB: `workhorse`, `early_down_thumper`, `pass_catching_specialist`, `committee_back`

**Key metrics (pre-aggregated from nfl_data_py + nflfastR):**
- Target share % (per season and per game)
- Targets per route run
- Air yards share
- Snap percentage and route participation rate
- Separation score (at snap and at catch)
- Yards after catch
- Yards after contact (RBs)
- Broken tackle rate (RBs)

**Clean season baseline:**
Strip injury-shortened seasons (<10 games) and anomalous situations (backup QB 4+ games).
**AVERAGE across clean seasons — never sum them.**
A player with 3 clean seasons averages them. Showing 2,800+ receiving yards = bug.

**PPR formula — exactly this:**
`ppr_points = (receptions × 1.0) + (yards × 0.1) + (touchdowns × 6.0)`
Any divergence > 5 points = double-counting bug. Fix before proceeding.

**Age curve modifiers:**
- RBs: peak 24-26, decline flag after 28
- WRs: peak 24-29
- TEs: peak 26-29 (slow development position)
- Contract year flag: final year of contract → mild upward bias, note it

**Breakout candidate detection (veterans):**
- Year 2 or Year 3 spike window
- Clear path to increased target share from depth chart departure
- New OC scheme elevates this player type
- Efficiency already above production level

---

### Rookie Profiling Branch

Rookies have no NFL history. The agent must detect rookie status and
route them to a completely separate profiling path.

```python
def build_player_profile(player: dict, team_context: dict) -> dict:
    if player.get("is_rookie") or player.get("nfl_seasons_played", 0) == 0:
        return _build_rookie_profile(player, team_context)
    return _build_veteran_profile(player, team_context)
```

**Rookie profile inputs (pre-populated by Agent 2 — already in player record):**
- `college_profile_grade` — elite / strong / average / weak
- `comp_yr1_avg_ppg` — historical comp average Year 1 PPR points per game
- `draft_capital_signal` — high / medium / low
- `landing_spot_modifier` — from team system grade (0.75-1.18)

**Confidence discounts by position:**
```python
ROOKIE_CONFIDENCE_DISCOUNT = {
    "QB":  0.65,   # Most QBs take 2-3 years — highest discount
    "WR":  0.75,   # Route running takes time against NFL coverage
    "TE":  0.70,   # Hardest position to translate from college
    "RB":  0.85,   # Translate fastest — smallest discount
}
```

**Ceiling/floor width:**
```
Veterans: ceiling = baseline × 1.25 | floor = baseline × 0.75
Rookies:  ceiling = baseline × 1.45 | floor = baseline × 0.55
```
The wider range reflects genuine uncertainty — not pessimism.

**Development timeline flags by position:**
```python
DEVELOPMENT_TIMELINE = {
    "QB": "year_2_to_4",    # Most QBs need 2+ years
    "WR": "year_2_to_3",    # Route running develops over time
    "TE": "year_3_to_4",    # Hardest transition from college
    "RB": "year_1",         # RBs contribute immediately
}
```

**Elite breakout flag:**
`college_profile_grade == "elite"` AND `draft_capital_signal == "high"`
→ `breakout_candidate = True` even in Year 1.
This is the Ja'Marr Chase / Justin Jefferson tier — rare but real.

**Output → `player_profiles` table**

---

## Agent 4: Injury Risk

**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 1000 per team batch
**Total API calls:** 32

**Purpose:** Risk-adjust every player's value. Not predicting injuries — pricing in variance.

**Injury categories:**
| Category | Recurrence | Key note |
|----------|-----------|----------|
| `soft_tissue` (hamstring, groin, calf) | HIGH | Two same-area events in 3yr = RECURRING_SOFT_TISSUE flag |
| `ligament_acl` | MODERATE | Low re-tear on same knee, elevated contralateral risk |
| `high_ankle_sprain` | MODERATE | Underrated lingering effect |
| `fracture_traumatic` | LOW | Heals cleanly, flag recency only |
| `fracture_stress` | MODERATE | Indicates biomechanical issue |
| `concussion` | SPECIAL | 2+ = compounding modifier |
| `chronic` (turf toe, back, plantar) | ONGOING | Does not reset between seasons |

**Pattern flags:**
`RECURRING_SOFT_TISSUE`, `CONCUSSION_HISTORY`, `HIGH_MILEAGE` (RB 600+ carries),
`POST_ACL` (within 18 months), `CHRONIC_CONDITION`, `WORKLOAD_CLIFF` (RB 300+ carries)

**Age risk multiplier:**
Under 26: 1.0x | 26-28: 1.1x | 29-30: 1.25x | 31+: 1.5x

**Risk-adjusted value modifier** (applied to baseline):
Low: 0 to -5% | Moderate: -10 to -20% | High: -20 to -35% | Volatile: -35%+

**Note for rookies:** Rookies should receive a neutral injury risk modifier
unless they have a documented pre-draft injury concern (e.g. ACL in final
college season). The `variance_flag` from Agent 3 already captures their
projection uncertainty — don't double-penalize.

**Output → `player_injury_profiles` table**

---

## Agent 5: Schedule

**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 1500 per team batch
**Total API calls:** 32

**Purpose:** Grade each player's schedule across three distinct windows.

**Three windows — store all three separately:**
1. Early (weeks 1-6): Determines fast start. More weight for immediate contributors.
2. Full season: Standard quality-of-schedule.
3. **Playoff (weeks 14-17): Most underrated metric. First-class field, not buried in notes.**

**Defensive grade construction:**
Start from last season's stats. Adjust for:
- FA losses/additions weighted by position relevance
- Draft picks by projected role
- Coordinator changes (scheme impact)
Build separate grades: vs WR1, vs slot WR, vs TE, vs RB rushing, vs RB receiving

**Additional factors:**
- Bye week number
- Weather risk (outdoor cold-weather cities: BUF, GB, CHI, NE, CLE, PIT — Nov/Dec modifier)
- Divisional game weeks (mild suppression)

**Output → `player_schedules` table**

The `playoff_window_grade` must be a queryable column. Never store it only in notes.

---

## Agent 6: Beat Reporter

**Model:** `claude-haiku-4-5-20251001`
**Max tokens:** 300 per signal
**Run schedule:** Daily the week of draft; weekly during season

**Purpose:** Freshness layer. Catch last-mile signals before other agents do.

**Signal types:**
- Practice limitation (Limited/DNP on injury report)
- Depth chart change (official or reported)
- Coach evasive about player status
- Camp standout / emerging role
- Transaction not yet in OverTheCap

**Data sources:**
- Team beat reporter RSS feeds (ESPN, NFL.com, The Athletic)
- Official NFL injury reports (Wed-Fri)
- Rotowire transaction feed

**Output:** Updates `notes` and `last_updated` on player records.
Also writes to `beat_reporter_signals` table with timestamp, source, signal type, raw text.

This agent runs on a daily schedule via APScheduler. It feeds into the
Injury Risk agent's `recovery_assessment` field for recently injured players.
